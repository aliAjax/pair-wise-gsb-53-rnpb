"""案组、材料回执、期限联动与可恢复组事务的 SQLite 访问。

所有写操作都在 ``BEGIN IMMEDIATE`` 下进行：同一时刻只有一个组事务
能进入临界区，先到者持锁推进案组修订号，后到者带着旧修订号进来时
只会得到 StaleRevision，由服务层把输入原样转入待合组。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import deadlines as deadline_rules
from .domain import Conflict, GroupDissolved, NotFound, StaleRevision, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class GroupRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def locked_connection(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS case_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    member_role TEXT NOT NULL,
                    depends_on_record_id INTEGER,
                    joined_revision INTEGER NOT NULL,
                    left_revision INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, record_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_member_active_record
                    ON group_members(record_id) WHERE active = 1;
                CREATE INDEX IF NOT EXISTS idx_member_group ON group_members(group_id, active);
                CREATE TABLE IF NOT EXISTS case_deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    base_deadline_day INTEGER NOT NULL,
                    applied_shift_days INTEGER NOT NULL DEFAULT 0,
                    deadline_day INTEGER NOT NULL,
                    computed_revision INTEGER NOT NULL DEFAULT 1,
                    stale INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_deadline_group ON case_deadlines(group_id);
                CREATE TABLE IF NOT EXISTS material_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_key TEXT NOT NULL UNIQUE,
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    origin_group_id INTEGER NOT NULL,
                    origin_record_id INTEGER NOT NULL REFERENCES records(id),
                    group_revision INTEGER NOT NULL,
                    source_receipt_id INTEGER,
                    shift_days INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    job_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                );
                CREATE TABLE IF NOT EXISTS group_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    input_json TEXT NOT NULL,
                    last_completed_record_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS group_job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES group_jobs(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    detail_json TEXT NOT NULL DEFAULT '',
                    UNIQUE(job_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_job_status ON group_jobs(status);
                CREATE TABLE IF NOT EXISTS deadline_diffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL REFERENCES material_receipts(id),
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    record_state TEXT NOT NULL,
                    previous_deadline_day INTEGER NOT NULL,
                    shift_days INTEGER NOT NULL,
                    projected_deadline_day INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_merges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    origin_group_id INTEGER,
                    origin_revision INTEGER,
                    source_receipt_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_waiting_key
                    ON pending_merges(dedupe_key) WHERE status = 'waiting';
                CREATE TABLE IF NOT EXISTS group_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # ---- 基础查询 ----

    @staticmethod
    def _group_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def get_group(self, group_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            raise NotFound("案组不存在")
        return dict(row)

    def list_groups(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM case_groups WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM case_groups ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def active_members(self, group_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_members WHERE group_id=? AND active=1 ORDER BY id", (group_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def members(self, group_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_members WHERE group_id=? ORDER BY id", (group_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def deadlines(self, group_id: int) -> List[Dict[str, Any]]:
        self.get_group(group_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM case_deadlines WHERE group_id=? ORDER BY record_id", (group_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def diffs(self, group_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deadline_diffs WHERE group_id=? ORDER BY id", (group_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def group_audit(self, group_id: int) -> List[Dict[str, Any]]:
        self.get_group(group_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_audit_events WHERE group_id=? ORDER BY id", (group_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---- 锁内原语 ----

    @staticmethod
    def lock_group(connection: sqlite3.Connection, group_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            raise NotFound("案组不存在")
        group = dict(row)
        if group["status"] != "active":
            raise GroupDissolved("案组已拆分或归档，旧修订输入只能进入待合组")
        return group

    @staticmethod
    def active_members_conn(connection: sqlite3.Connection, group_id: int) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM group_members WHERE group_id=? AND active=1 ORDER BY id", (group_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def record_states(connection: sqlite3.Connection, record_ids: Iterable[int]) -> Dict[int, str]:
        ids = list(record_ids)
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = connection.execute(
            "SELECT id, state FROM records WHERE id IN (%s)" % marks, list(ids)
        ).fetchall()
        return {int(row["id"]): row["state"] for row in rows}

    @staticmethod
    def record_exists(connection: sqlite3.Connection, record_id: int) -> bool:
        return connection.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone() is not None

    @staticmethod
    def active_membership(connection: sqlite3.Connection, record_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM group_members WHERE record_id=? AND active=1", (record_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def audit_conn(connection: sqlite3.Connection, group_id: Optional[int], actor_id: str, action: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO group_audit_events(group_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (group_id, action, actor_id, _dumps(details), _now()),
        )

    # ---- 建组 ----

    def create_group(self, reference: str, members: List[Dict[str, Any]], note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self.locked_connection() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM case_groups WHERE reference=?", (reference,)
            ).fetchone()
            if duplicate is not None:
                raise Conflict("案组编号已存在")
            record_ids = [int(m["record_id"]) for m in members]
            states = self.record_states(connection, record_ids)
            if len(states) != len(set(record_ids)):
                raise ValidationError("成员案件不存在或重复")
            for record_id in record_ids:
                if self.active_membership(connection, record_id) is not None:
                    raise Conflict("案件%s已在其他案组中" % record_id)
            cursor = connection.execute(
                "INSERT INTO case_groups(reference,revision,status,note,created_by,created_at,updated_at)"
                " VALUES(?,1,'active',?,?,?,?)",
                (reference, note, actor_id, now, now),
            )
            group_id = int(cursor.lastrowid)
            for member in members:
                record_id = int(member["record_id"])
                record = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
                base_day = int(json.loads(record["payload"])["deadline_day"])
                connection.execute(
                    "INSERT INTO group_members(group_id,record_id,member_role,depends_on_record_id,"
                    "joined_revision,left_revision,active,created_at) VALUES(?,?,?,?,?,NULL,1,?)",
                    (group_id, record_id, member["member_role"], member["depends_on_record_id"], 1, now),
                )
                connection.execute(
                    "INSERT INTO case_deadlines(group_id,record_id,base_deadline_day,applied_shift_days,"
                    "deadline_day,computed_revision,stale,updated_at) VALUES(?,?,?,0,?,1,0,?)",
                    (group_id, record_id, base_day, base_day, now),
                )
            self.audit_conn(connection, group_id, actor_id, "group_created",
                            {"reference": reference, "members": record_ids, "note": note})
            row = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
        return dict(row)

    # ---- 回执（组事务入口） ----

    def find_receipt(self, receipt_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM material_receipts WHERE receipt_key=?", (receipt_key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_receipt_job(self, group_id: int, receipt_key: str, origin_record_id: int,
                           expected_revision: int, shift_days: int, note: str, actor_id: str,
                           before_commit=None) -> Dict[str, Any]:
        """在同一把写锁内完成：校验修订号、登记回执、建任务、失效依赖、推进修订号。"""
        now = _now()
        with self.locked_connection() as connection:
            group = self.lock_group(connection, group_id)
            members = self.active_members_conn(connection, group_id)
            affected = deadline_rules.affected_members(members, origin_record_id)
            affected_ids = [int(m["record_id"]) for m in affected]
            duplicate_row = connection.execute(
                "SELECT * FROM material_receipts WHERE receipt_key=?", (receipt_key,)
            ).fetchone()
            if duplicate_row is not None and int(duplicate_row["group_id"]) == group_id:
                # 回执已登记：并发重复提交，不重复顺延，直接交回既有任务
                return {"receipt_id": int(duplicate_row["id"]), "job_id": int(duplicate_row["job_id"]),
                        "target_revision": int(group["revision"]),
                        "affected": affected_ids, "duplicate": True}
            if int(group["revision"]) != int(expected_revision):
                raise StaleRevision(
                    "案组修订号已变化：期望%s，当前%s" % (expected_revision, group["revision"]),
                    current_revision=int(group["revision"]),
                )
            states = self.record_states(connection, affected_ids)

            cursor = connection.execute(
                "INSERT INTO material_receipts(receipt_key,group_id,origin_group_id,origin_record_id,"
                "group_revision,source_receipt_id,shift_days,note,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,NULL,?,?, 'pending',?,?)",
                (receipt_key, group_id, group_id, origin_record_id, expected_revision,
                 shift_days, note, actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            target_revision = int(expected_revision) + 1
            job_cursor = connection.execute(
                "INSERT INTO group_jobs(group_id,kind,ref_id,status,input_json,created_by,created_at)"
                " VALUES(?,'apply_receipt',?,'running',?,?,?)",
                (group_id, receipt_id,
                 _dumps({"receipt_id": receipt_id, "target_revision": target_revision,
                         "shift_days": shift_days, "affected": affected_ids}),
                 actor_id, now),
            )
            job_id = int(job_cursor.lastrowid)
            connection.execute(
                "UPDATE material_receipts SET job_id=? WHERE id=?", (job_id, receipt_id)
            )
            for ordinal, record_id in enumerate(affected_ids):
                connection.execute(
                    "INSERT INTO group_job_items(job_id,record_id,ordinal,status) VALUES(?,?,?,'pending')",
                    (job_id, record_id, ordinal),
                )
            # 只有依赖该回执且尚未决定的案件失效；已决定/归档不置 stale，只等差异追加
            stale_ids = [rid for rid in affected_ids if not deadline_rules.is_frozen(states[rid])]
            if stale_ids:
                marks = ",".join("?" for _ in stale_ids)
                connection.execute(
                    "UPDATE case_deadlines SET stale=1,updated_at=? WHERE group_id=? AND record_id IN (%s)" % marks,
                    [now, group_id, *stale_ids],
                )
            connection.execute(
                "UPDATE case_groups SET revision=?,updated_at=? WHERE id=?",
                (target_revision, now, group_id),
            )
            self.audit_conn(connection, group_id, actor_id, "receipt_registered",
                            {"receipt_key": receipt_key, "origin_record_id": origin_record_id,
                             "base_revision": expected_revision, "shift_days": shift_days,
                             "affected": affected_ids})
            if before_commit is not None:
                before_commit()
        return {"receipt_id": receipt_id, "job_id": job_id, "target_revision": target_revision,
                "affected": affected_ids}

    def get_job(self, job_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM group_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("组任务不存在")
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        return item

    def job_items(self, job_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_job_items WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def running_jobs(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM group_jobs WHERE status='running' ORDER BY id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["input"] = json.loads(item.pop("input_json"))
            result.append(item)
        return result

    def run_job_item(self, job_id: int, record_id: int, actor_id: str = "system") -> Dict[str, Any]:
        """逐案件独立提交；重复执行已完成条目直接返回，不再顺延。"""
        now = _now()
        with self.locked_connection() as connection:
            item = connection.execute(
                "SELECT * FROM group_job_items WHERE job_id=? AND record_id=?", (job_id, record_id)
            ).fetchone()
            if item is None:
                raise NotFound("任务条目不存在")
            if item["status"] == "done":
                return {"record_id": record_id, "already_done": True, "detail": json.loads(item["detail_json"] or "{}")}
            job = connection.execute("SELECT * FROM group_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "running":
                raise ValidationError("组任务不在运行中")
            receipt = connection.execute(
                "SELECT * FROM material_receipts WHERE id=?", (job["ref_id"],)
            ).fetchone()
            group_id = int(receipt["group_id"])
            shift_days = int(receipt["shift_days"])
            deadline = connection.execute(
                "SELECT * FROM case_deadlines WHERE group_id=? AND record_id=?", (group_id, record_id)
            ).fetchone()
            if deadline is None:
                raise NotFound("案件期限行缺失")
            state_row = connection.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
            state = state_row["state"]
            target_revision = int(json.loads(job["input_json"])["target_revision"])
            detail: Dict[str, Any]
            if deadline_rules.is_frozen(state):
                projected = int(deadline["deadline_day"]) + shift_days
                connection.execute(
                    "INSERT INTO deadline_diffs(receipt_id,group_id,record_id,record_state,"
                    "previous_deadline_day,shift_days,projected_deadline_day,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (int(receipt["id"]), group_id, record_id, state,
                     int(deadline["deadline_day"]), shift_days, projected, now),
                )
                detail = {"mode": "diff_only", "previous_deadline_day": int(deadline["deadline_day"]),
                          "projected_deadline_day": projected}
            else:
                updated = deadline_rules.shifted_deadline(dict(deadline), shift_days)
                connection.execute(
                    "UPDATE case_deadlines SET applied_shift_days=?,deadline_day=?,"
                    "computed_revision=?,stale=0,updated_at=? WHERE id=?",
                    (updated["applied_shift_days"], updated["deadline_day"], target_revision, now,
                     deadline["id"]),
                )
                detail = {"mode": "recalculated", **updated}
            connection.execute(
                "UPDATE group_job_items SET status='done',detail_json=? WHERE id=?",
                (_dumps(detail), item["id"]),
            )
            connection.execute(
                "UPDATE group_jobs SET last_completed_record_id=? WHERE id=?", (record_id, job_id)
            )
            pending = connection.execute(
                "SELECT COUNT(*) AS total FROM group_job_items WHERE job_id=? AND status='pending'",
                (job_id,),
            ).fetchone()
            completed = int(pending["total"]) == 0
            if completed:
                connection.execute(
                    "UPDATE group_jobs SET status='completed',completed_at=? WHERE id=?", (now, job_id)
                )
                connection.execute(
                    "UPDATE material_receipts SET status='applied',applied_at=? WHERE id=?",
                    (now, int(receipt["id"])),
                )
                self.audit_conn(connection, group_id, actor_id, "receipt_applied",
                                {"receipt_id": int(receipt["id"]), "receipt_key": receipt["receipt_key"],
                                 "shift_days": shift_days, "revision": target_revision})
            result = {"record_id": record_id, "already_done": False, "detail": detail,
                      "job_completed": completed}
        return result

    # ---- 拆分 / 合并 ----

    def execute_regroup(self, plan: Dict[str, Any], actor_id: str, before_commit=None,
                        pending_merge_id: Optional[int] = None) -> List[Dict[str, Any]]:
        now = _now()
        sources = plan["sources"]
        source_ids = sorted(int(s["group_id"]) for s in sources)
        expected = {int(s["group_id"]): int(s["expected_revision"]) for s in sources}
        with self.locked_connection() as connection:
            marks = ",".join("?" for _ in source_ids)
            rows = connection.execute(
                "SELECT * FROM case_groups WHERE id IN (%s) ORDER BY id" % marks, source_ids
            ).fetchall()
            groups = {int(row["id"]): dict(row) for row in rows}
            if len(groups) != len(source_ids):
                raise NotFound("部分案组不存在")
            for group_id, group in groups.items():
                if group["status"] != "active":
                    raise ValidationError("案组%s已解散，不能再次改组" % group_id)
                if int(group["revision"]) != expected[group_id]:
                    raise StaleRevision(
                        "案组%s修订号已变化：期望%s，当前%s" % (
                            group_id, expected[group_id], group["revision"]),
                        current_revision=int(group["revision"]),
                    )
            old_members: List[Dict[str, Any]] = []
            for group_id in source_ids:
                old_members.extend(self.active_members_conn(connection, group_id))
            old_by_record = {int(m["record_id"]): m for m in old_members}

            planned_groups = plan["plans"]
            plan_records: List[int] = []
            for new_group in planned_groups:
                ids = [int(m["record_id"]) for m in new_group["members"]]
                if len(ids) != len(set(ids)):
                    raise ValidationError("新案组%s内成员重复" % new_group["reference"])
                plan_records.extend(ids)
            if len(plan_records) != len(set(plan_records)):
                raise ValidationError("同一案件不能同时进入多个新案组")
            if set(plan_records) != set(old_by_record):
                raise ValidationError("改组必须覆盖原案组的全部在组案件，不能遗漏或新增")
            states = self.record_states(connection, old_by_record.keys())

            # 结构修订生效：旧组定格
            for group_id, group in groups.items():
                connection.execute(
                    "UPDATE case_groups SET status='dissolved',updated_at=? WHERE id=?", (now, group_id)
                )
                connection.execute(
                    "UPDATE group_members SET active=0,left_revision=? WHERE group_id=? AND active=1",
                    (int(group["revision"]), group_id),
                )

            created: List[Dict[str, Any]] = []
            for new_group in planned_groups:
                reference = new_group["reference"]
                if connection.execute("SELECT 1 FROM case_groups WHERE reference=?", (reference,)).fetchone() is not None:
                    raise Conflict("案组编号已存在：%s" % reference)
                cursor = connection.execute(
                    "INSERT INTO case_groups(reference,revision,status,note,created_by,created_at,updated_at)"
                    " VALUES(?,1,'active',?,?,?,?)",
                    (reference, new_group.get("note", ""), actor_id, now, now),
                )
                new_group_id = int(cursor.lastrowid)
                member_rows = []
                lead_applied: Dict[int, int] = {}
                for member in new_group["members"]:
                    record_id = int(member["record_id"])
                    role = member["member_role"]
                    depends = member.get("depends_on_record_id")
                    old = old_by_record[record_id]
                    old_deadline = connection.execute(
                        "SELECT * FROM case_deadlines WHERE group_id=? AND record_id=?",
                        (int(old["group_id"]), record_id),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO group_members(group_id,record_id,member_role,depends_on_record_id,"
                        "joined_revision,left_revision,active,created_at) VALUES(?,?,?,?,?,NULL,1,?)",
                        (new_group_id, record_id, role, depends, 1, now),
                    )
                    base_day = int(old_deadline["base_deadline_day"])
                    applied = int(old_deadline["applied_shift_days"])
                    deadline_day = int(old_deadline["deadline_day"])
                    if role == "main":
                        lead_applied[record_id] = applied
                    member_rows.append((record_id, role, depends, base_day, applied, deadline_day))
                # 关联案在新组内按新主案重新锚定：未决定者带着主案累计顺延重算，
                # 已决定/归档保持原期限定格，等待后续回执只追加差异。
                for record_id, role, depends, base_day, applied, deadline_day in member_rows:
                    if role == "related" and depends in lead_applied and not deadline_rules.is_frozen(states[record_id]):
                        applied = lead_applied[depends]
                        deadline_day = base_day + applied
                    connection.execute(
                        "INSERT INTO case_deadlines(group_id,record_id,base_deadline_day,applied_shift_days,"
                        "deadline_day,computed_revision,stale,updated_at) VALUES(?,?,?,?,?,1,0,?)",
                        (new_group_id, record_id, base_day, applied, deadline_day, now),
                    )
                job_input = {"source_groups": source_ids, "reference": reference,
                             "members": [int(m["record_id"]) for m in new_group["members"]]}
                connection.execute(
                    "INSERT INTO group_jobs(group_id,kind,ref_id,status,input_json,created_by,created_at,completed_at)"
                    " VALUES(?,'regroup',0,'completed',?,?,?,?)",
                    (new_group_id, _dumps(job_input), actor_id, now, now),
                )
                self.audit_conn(connection, new_group_id, actor_id, "group_created_by_regroup",
                                {"reference": reference, "sources": source_ids, **job_input})
                created.append(dict(connection.execute(
                    "SELECT * FROM case_groups WHERE id=?", (new_group_id,)).fetchone()))
            for group_id in source_ids:
                self.audit_conn(connection, group_id, actor_id, "group_dissolved",
                                {"revision": int(groups[group_id]["revision"]),
                                 "into": [g["reference"] for g in created]})
            if pending_merge_id is not None:
                pending = connection.execute(
                    "SELECT * FROM pending_merges WHERE id=?", (pending_merge_id,)
                ).fetchone()
                if pending is None or pending["status"] != "waiting":
                    raise Conflict("待合组记录不存在或已处理")
                connection.execute(
                    "UPDATE pending_merges SET status='merged',resolved_at=? WHERE id=?",
                    (now, pending_merge_id),
                )
                self.audit_conn(connection, source_ids[0], actor_id, "pending_regroup_merged",
                                {"pending_merge_id": pending_merge_id})
            if before_commit is not None:
                before_commit()
        return created

    # ---- 待合组 ----

    def waiting_pending(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_merges WHERE dedupe_key=? AND status='waiting'", (dedupe_key,)
            ).fetchone()
        return self._pending_row(row) if row is not None else None

    @staticmethod
    def _pending_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def park(self, kind: str, dedupe_key: str, payload: Dict[str, Any], actor_id: str,
             origin_group_id: Optional[int], origin_revision: Optional[int],
             source_receipt_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self.locked_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM pending_merges WHERE dedupe_key=? AND status='waiting'", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return self._pending_row(existing)
            cursor = connection.execute(
                "INSERT INTO pending_merges(kind,dedupe_key,payload_json,origin_group_id,origin_revision,"
                "source_receipt_id,status,created_by,created_at) VALUES(?,?,?,?,?,?,'waiting',?,?)",
                (kind, dedupe_key, _dumps(payload), origin_group_id, origin_revision,
                 source_receipt_id, actor_id, now),
            )
            pending_id = int(cursor.lastrowid)
            self.audit_conn(connection, origin_group_id, actor_id, "pending_merge_parked",
                            {"kind": kind, "pending_merge_id": pending_id, "dedupe_key": dedupe_key})
            row = connection.execute("SELECT * FROM pending_merges WHERE id=?", (pending_id,)).fetchone()
        return self._pending_row(row)

    def list_pending(self, status: str = "waiting") -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_merges WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return [self._pending_row(row) for row in rows]

    def get_pending(self, pending_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM pending_merges WHERE id=?", (pending_id,)).fetchone()
        if row is None:
            raise NotFound("待合组记录不存在")
        return self._pending_row(row)

    def discard_pending(self, pending_id: int) -> None:
        now = _now()
        with self.locked_connection() as connection:
            row = connection.execute("SELECT * FROM pending_merges WHERE id=?", (pending_id,)).fetchone()
            if row is None:
                raise NotFound("待合组记录不存在")
            if row["status"] != "waiting":
                raise Conflict("待合组记录已处理")
            connection.execute(
                "UPDATE pending_merges SET status='discarded',resolved_at=? WHERE id=?", (now, pending_id)
            )
            self.audit_conn(connection, row["origin_group_id"], "system", "pending_merge_discarded",
                            {"pending_merge_id": pending_id, "kind": row["kind"]})

    def merge_receipt_pending(self, pending_id: int, target_group_id: int, actor_id: str,
                              before_commit=None) -> Dict[str, Any]:
        """把旧修订回执合进目标新组：以目标组当前修订重新登记并建新任务。"""
        now = _now()
        with self.locked_connection() as connection:
            pending = connection.execute(
                "SELECT * FROM pending_merges WHERE id=?", (pending_id,)
            ).fetchone()
            if pending is None:
                raise NotFound("待合组记录不存在")
            if pending["status"] != "waiting" or pending["kind"] != "receipt":
                raise Conflict("待合组记录不能按回执合入")
            payload = json.loads(pending["payload_json"])
            target = self.lock_group(connection, target_group_id)
            members = self.active_members_conn(connection, target_group_id)
            affected = deadline_rules.affected_members(members, int(payload["origin_record_id"]))
            affected_ids = [int(m["record_id"]) for m in affected]
            states = self.record_states(connection, affected_ids)
            base_revision = int(target["revision"])
            target_revision = base_revision + 1
            shift_days = int(payload["shift_days"])
            receipt_key = str(payload["receipt_key"])
            if connection.execute(
                "SELECT 1 FROM material_receipts WHERE receipt_key=? AND group_id=?",
                (receipt_key, target_group_id),
            ).fetchone() is not None:
                raise Conflict("该回执已写进目标案组")
            cursor = connection.execute(
                "INSERT INTO material_receipts(receipt_key,group_id,origin_group_id,origin_record_id,"
                "group_revision,source_receipt_id,shift_days,note,status,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (receipt_key, target_group_id, pending["origin_group_id"], int(payload["origin_record_id"]),
                 base_revision, pending["source_receipt_id"], shift_days, str(payload.get("note", "")),
                 actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            job_cursor = connection.execute(
                "INSERT INTO group_jobs(group_id,kind,ref_id,status,input_json,created_by,created_at)"
                " VALUES(?,'apply_receipt',?,'running',?,?,?)",
                (target_group_id, receipt_id,
                 _dumps({"receipt_id": receipt_id, "target_revision": target_revision,
                         "shift_days": shift_days, "affected": affected_ids, "from_pending_merge": pending_id}),
                 actor_id, now),
            )
            job_id = int(job_cursor.lastrowid)
            connection.execute("UPDATE material_receipts SET job_id=? WHERE id=?", (job_id, receipt_id))
            for ordinal, record_id in enumerate(affected_ids):
                connection.execute(
                    "INSERT INTO group_job_items(job_id,record_id,ordinal,status) VALUES(?,?,?,'pending')",
                    (job_id, record_id, ordinal),
                )
            stale_ids = [rid for rid in affected_ids if not deadline_rules.is_frozen(states[rid])]
            if stale_ids:
                marks = ",".join("?" for _ in stale_ids)
                connection.execute(
                    "UPDATE case_deadlines SET stale=1,updated_at=? WHERE group_id=? AND record_id IN (%s)" % marks,
                    [now, target_group_id, *stale_ids],
                )
            connection.execute(
                "UPDATE case_groups SET revision=?,updated_at=? WHERE id=?",
                (target_revision, now, target_group_id),
            )
            connection.execute(
                "UPDATE pending_merges SET status='merged',resolved_at=? WHERE id=?", (now, pending_id)
            )
            self.audit_conn(connection, target_group_id, actor_id, "pending_receipt_merged",
                            {"pending_merge_id": pending_id, "receipt_key": receipt_key,
                             "base_revision": base_revision, "affected": affected_ids})
            if before_commit is not None:
                before_commit()
        return {"receipt_id": receipt_id, "job_id": job_id, "target_revision": target_revision,
                "affected": affected_ids}
