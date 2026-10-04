"""关联案组：案组/成员/材料回执表，以及可恢复的组事务（检查点与续算）。

设计要点：
- 案组有两个水位线：revision（改组结构修订号）与 op_seq（写入总序号）。
  改组（拆分/合并）才增加 revision；回执携带提交时的 revision，且按 op_seq
  仲裁"先到生效"。
- 回执按案件拆成组事务步骤，每完成一个案件立即提交检查点；中断后续算从
  最近完成的案件继续。case_receipt_applications 以(案件,回执)幂等，重试或
  重复回执不会重复顺延。
- 修订号不匹配（改组先到）或有活动组事务时，回执/改组保留输入停在
  "待合组"，不触碰任何案件。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound, ValidationError
from .rules import DECIDED_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 活动组事务的步骤执行回调：传入当前案件与回执，返回(新状态, 新payload, 动作, 详情)。
StepHandler = Callable[[Dict[str, Any], Dict[str, Any]], Tuple[str, Dict[str, Any], str, Dict[str, Any]]]


class GroupRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

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
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    op_seq INTEGER NOT NULL DEFAULT 0,
                    active_tx_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_memberships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES case_groups(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    role TEXT NOT NULL,
                    depends_on_receipt TEXT NOT NULL DEFAULT '',
                    joined_revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_members_record ON group_memberships(record_id);
                CREATE TABLE IF NOT EXISTS group_revision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    op_seq INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    details TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_revision_group ON group_revision_events(group_id, id);
                CREATE TABLE IF NOT EXISTS material_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_key TEXT NOT NULL UNIQUE,
                    anchor_record_id INTEGER NOT NULL REFERENCES records(id),
                    kind TEXT NOT NULL,
                    shift_days INTEGER NOT NULL DEFAULT 0,
                    group_revision INTEGER NOT NULL,
                    base_op_seq INTEGER,
                    submitted_group_id INTEGER NOT NULL,
                    effective_group_id INTEGER,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    tx_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_status ON material_receipts(status, id);
                CREATE TABLE IF NOT EXISTS group_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    receipt_id INTEGER,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_tx_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tx_id INTEGER NOT NULL REFERENCES group_transactions(id),
                    position INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    details TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(tx_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_steps_tx ON group_tx_steps(tx_id, position);
                CREATE TABLE IF NOT EXISTS case_receipt_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    receipt_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    UNIQUE(record_id, receipt_id)
                );
                CREATE TABLE IF NOT EXISTS group_pending_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pending_group ON group_pending_requests(group_id, status, id);
                CREATE TABLE IF NOT EXISTS group_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_group_audit ON group_audit_events(group_id, id);
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _group_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if "active_tx_id" in item and item["active_tx_id"] is not None:
            item["active_tx_id"] = int(item["active_tx_id"])
        return item

    def _audit(self, connection: sqlite3.Connection, group_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO group_audit_events(group_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (group_id, action, actor_id, self._json(details), _now()),
        )

    def _revision_event(self, connection: sqlite3.Connection, group_id: int, op_seq: int, revision: int, kind: str, actor_id: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO group_revision_events(group_id,op_seq,revision,kind,details,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (group_id, op_seq, revision, kind, self._json(details), actor_id, _now()),
        )

    # ---------------------------------------------------------------- 读

    def get_group(self, group_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            raise NotFound("案组不存在")
        return self._group_row(row)

    def list_groups(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM case_groups ORDER BY id").fetchall()
        return [self._group_row(row) for row in rows]

    def get_membership(self, connection: sqlite3.Connection, group_id: int, record_id: int) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM group_memberships WHERE group_id=? AND record_id=?",
            (group_id, record_id),
        ).fetchone()

    def list_members(self, group_id: int) -> List[Dict[str, Any]]:
        self.get_group(group_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT m.*, r.state, r.version, r.payload, r.reference FROM group_memberships m "
                "JOIN records r ON r.id = m.record_id WHERE m.group_id=? ORDER BY m.id",
                (group_id,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            items.append(item)
        return items

    def get_receipt_by_key(self, receipt_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM material_receipts WHERE receipt_key=?", (receipt_key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def get_receipt(self, receipt_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM material_receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise NotFound("回执不存在")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_pending(self, group_id: int) -> Dict[str, List[Dict[str, Any]]]:
        self.get_group(group_id)
        with self._connect() as connection:
            receipt_rows = connection.execute(
                "SELECT * FROM material_receipts WHERE submitted_group_id=? AND status='parked' ORDER BY id",
                (group_id,),
            ).fetchall()
            request_rows = connection.execute(
                "SELECT * FROM group_pending_requests WHERE group_id=? AND status='pending' ORDER BY id",
                (group_id,),
            ).fetchall()
        receipts = []
        for row in receipt_rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            receipts.append(item)
        requests = []
        for row in request_rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            requests.append(item)
        return {"receipts": receipts, "change_requests": requests}

    def group_audit_timeline(self, group_id: int) -> List[Dict[str, Any]]:
        self.get_group(group_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM group_audit_events WHERE group_id=? ORDER BY id", (group_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def tx_progress(self, tx_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            tx = connection.execute("SELECT * FROM group_transactions WHERE id=?", (tx_id,)).fetchone()
            if tx is None:
                raise NotFound("组事务不存在")
            rows = connection.execute("SELECT * FROM group_tx_steps WHERE tx_id=? ORDER BY position", (tx_id,)).fetchall()
        steps = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            steps.append(item)
        completed = sum(1 for step in steps if step["status"] == "done")
        return {"transaction": dict(tx), "completed": completed, "total": len(steps), "steps": steps}

    # ---------------------------------------------------------------- 建组

    def create_group(self, name: str, members: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            principals = [m for m in members if m["role"] == "principal"]
            if len(principals) != 1:
                connection.rollback()
                raise ValidationError("案组必须恰好包含一个主案")
            record_ids = [m["record_id"] for m in members]
            if len(set(record_ids)) != len(record_ids):
                connection.rollback()
                raise Conflict("案件在案组成员中重复")
            cursor = connection.execute(
                "INSERT INTO case_groups(name,revision,op_seq,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (name, 1, 0, "active", actor_id, now, now),
            )
            group_id = int(cursor.lastrowid)
            for member in members:
                record = connection.execute("SELECT id FROM records WHERE id=?", (member["record_id"],)).fetchone()
                if record is None:
                    connection.rollback()
                    raise NotFound("案件%s不存在" % member["record_id"])
                already = connection.execute(
                    "SELECT group_id FROM group_memberships WHERE record_id=?", (member["record_id"],)
                ).fetchone()
                if already is not None:
                    connection.rollback()
                    raise Conflict("案件%s已属于案组%s" % (member["record_id"], int(already["group_id"])))
                connection.execute(
                    "INSERT INTO group_memberships(group_id,record_id,role,depends_on_receipt,joined_revision,created_at) VALUES(?,?,?,?,?,?)",
                    (group_id, member["record_id"], member["role"], member.get("depends_on_receipt", ""), 1, now),
                )
            self._audit(connection, group_id, actor_id, "group_created", {"name": name, "members": members})
            connection.commit()
        return self.get_group(group_id)

    # ------------------------------------------------- 回执：停放与重取

    def park_receipt(self, receipt_key: str, anchor_record_id: int, kind: str, shift_days: int,
                     group_revision: int, base_op_seq: Optional[int], group_id: int,
                     payload: Dict[str, Any], actor_id: str, reason: str,
                     receipt_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            if receipt_id is None:
                cursor = connection.execute(
                    "INSERT INTO material_receipts(receipt_key,anchor_record_id,kind,shift_days,group_revision,"
                    "base_op_seq,submitted_group_id,status,payload,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_key, anchor_record_id, kind, shift_days, group_revision, base_op_seq, group_id,
                     "parked", self._json(payload), actor_id, now, now),
                )
                receipt_id = int(cursor.lastrowid)
            else:
                connection.execute(
                    "UPDATE material_receipts SET status='parked', updated_at=? WHERE id=?",
                    (now, receipt_id),
                )
            self._audit(connection, group_id, actor_id, "receipt_parked",
                        {"receipt_id": receipt_id, "receipt_key": receipt_key, "group_revision": group_revision, "reason": reason})
            connection.commit()
        return self.get_receipt(receipt_id)

    def park_change_request(self, group_id: int, kind: str, payload: Dict[str, Any], actor_id: str) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO group_pending_requests(group_id,kind,payload,status,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (group_id, kind, self._json(payload), "pending", actor_id, now),
            )
            request_id = int(cursor.lastrowid)
            self._audit(connection, group_id, actor_id, "change_parked",
                        {"request_id": request_id, "kind": kind, "input": payload})
            connection.commit()
        return request_id

    # ------------------------------------------------------- 回执组事务

    def run_receipt_transaction(self, receipt: Dict[str, Any], actor_id: str, step_handler: StepHandler,
                                existing_receipt_id: Optional[int] = None) -> Dict[str, Any]:
        """为回执开启（或继续）一个可恢复组事务，并把所有待完成步骤跑完。

        头事务负责水位线仲裁；步骤逐个检查点提交。返回案组最新状态与事务进度。
        """
        group_id = int(receipt["submitted_group_id"])
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
            if group is None:
                connection.rollback()
                raise NotFound("案组不存在")
            if group["status"] != "active":
                connection.rollback()
                raise Conflict("案组已%s，不能再写入回执" % group["status"])
            if group["active_tx_id"] is not None:
                connection.rollback()
                raise Conflict("案组有正在执行的组事务#%s，请先完成或续算" % int(group["active_tx_id"]))
            if int(group["revision"]) != int(receipt["group_revision"]):
                connection.rollback()
                # 返回 None 让服务层把回执停在待合组。
                return {"parked": True, "reason": "stale_revision", "group": self._group_row(group)}
            if receipt.get("base_op_seq") is not None and int(group["op_seq"]) != int(receipt["base_op_seq"]):
                connection.rollback()
                return {"parked": True, "reason": "concurrent_write", "group": self._group_row(group)}

            op_seq = int(group["op_seq"]) + 1
            receipt_id = existing_receipt_id
            if receipt_id is None:
                cursor = connection.execute(
                    "INSERT INTO material_receipts(receipt_key,anchor_record_id,kind,shift_days,group_revision,"
                    "base_op_seq,submitted_group_id,effective_group_id,status,payload,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt["receipt_key"], receipt["anchor_record_id"], receipt["kind"], receipt["shift_days"],
                     receipt["group_revision"], receipt.get("base_op_seq"), group_id, group_id, "applying",
                     self._json(receipt["payload"]), actor_id, now, now),
                )
                receipt_id = int(cursor.lastrowid)
            else:
                connection.execute(
                    "UPDATE material_receipts SET status='applying', effective_group_id=?, group_revision=?, "
                    "updated_at=? WHERE id=?",
                    (group_id, int(group["revision"]), now, receipt_id),
                )
            tx_cursor = connection.execute(
                "INSERT INTO group_transactions(group_id,kind,receipt_id,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (group_id, "receipt", receipt_id, "running", actor_id, now, now),
            )
            tx_id = int(tx_cursor.lastrowid)

            anchor = int(receipt["anchor_record_id"])
            member_rows = connection.execute(
                "SELECT * FROM group_memberships WHERE group_id=? ORDER BY id", (group_id,)
            ).fetchall()
            position = 0
            steps: List[Tuple[int, str]] = []
            # 主案（回执锚点）始终处理；关联案只处理声明依赖该回执、且仍在本组的案件。
            for row in member_rows:
                record_id = int(row["record_id"])
                is_anchor = record_id == anchor
                if is_anchor or (row["depends_on_receipt"] and row["depends_on_receipt"] == receipt["receipt_key"]):
                    state = connection.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()["state"]
                    mode = "diff_only" if state in DECIDED_STATES else "recompute"
                    connection.execute(
                        "INSERT INTO group_tx_steps(tx_id,position,record_id,mode,status) VALUES(?,?,?,?,?)",
                        (tx_id, position, record_id, mode, "pending"),
                    )
                    steps.append((record_id, mode))
                    position += 1
            connection.execute(
                "UPDATE case_groups SET op_seq=?, active_tx_id=?, updated_at=? WHERE id=?",
                (op_seq, tx_id, now, group_id),
            )
            self._revision_event(connection, group_id, op_seq, int(group["revision"]), "receipt", actor_id,
                                 {"receipt_id": receipt_id, "receipt_key": receipt["receipt_key"], "kind": receipt["kind"]})
            self._audit(connection, group_id, actor_id, "receipt_tx_opened",
                        {"receipt_id": receipt_id, "tx_id": tx_id, "op_seq": op_seq,
                         "group_revision": int(group["revision"]), "steps": steps})
            connection.commit()

        progress = self._complete_steps(tx_id, receipt_id, actor_id, step_handler)
        return {"parked": False, "receipt_id": receipt_id, "tx_id": tx_id, "group": self.get_group(group_id), "progress": progress}

    def _complete_steps(self, tx_id: int, receipt_id: int, actor_id: str, step_handler: StepHandler) -> Dict[str, Any]:
        """逐个执行未完成步骤；每步独立事务提交（检查点），可中断后续算。"""
        while True:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                step = connection.execute(
                    "SELECT * FROM group_tx_steps WHERE tx_id=? AND status!='done' ORDER BY position LIMIT 1",
                    (tx_id,),
                ).fetchone()
                if step is None:
                    tx = connection.execute("SELECT * FROM group_transactions WHERE id=?", (tx_id,)).fetchone()
                    group_id = int(tx["group_id"])
                    connection.execute("UPDATE group_transactions SET status='completed', updated_at=? WHERE id=?", (_now(), tx_id))
                    connection.execute(
                        "UPDATE material_receipts SET status='applied', tx_id=?, updated_at=? WHERE id=?",
                        (tx_id, _now(), receipt_id),
                    )
                    connection.execute("UPDATE case_groups SET active_tx_id=NULL, updated_at=? WHERE id=?", (_now(), group_id))
                    self._audit(connection, group_id, actor_id, "receipt_tx_completed",
                                {"receipt_id": receipt_id, "tx_id": tx_id})
                    connection.commit()
                    return self.tx_progress(tx_id)

                record_id = int(step["record_id"])
                applied = connection.execute(
                    "SELECT 1 FROM case_receipt_applications WHERE record_id=? AND receipt_id=?",
                    (record_id, receipt_id),
                ).fetchone()
                if applied is not None:
                    # 上次中断发生在"案件已写、步骤未标记"之间：直接补检查点，不再顺延。
                    connection.execute("UPDATE group_tx_steps SET status='done', details=? WHERE id=?",
                                       (self._json({"deduped": True}), int(step["id"])))
                    connection.commit()
                    continue

                record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                if record_row is None:
                    connection.rollback()
                    raise NotFound("步骤中的案件%s已不存在" % record_id)
                record = dict(record_row)
                record["payload"] = json.loads(record_row["payload"])
                receipt_row = connection.execute("SELECT * FROM material_receipts WHERE id=?", (receipt_id,)).fetchone()
                receipt = dict(receipt_row)
                receipt["payload"] = json.loads(receipt_row["payload"])

                new_state, new_payload, action, details = step_handler(record, receipt)
                now = _now()
                new_version = int(record_row["version"]) + 1
                connection.execute(
                    "UPDATE records SET state=?, version=?, payload=?, updated_by=?, updated_at=? WHERE id=?",
                    (new_state, new_version, self._json(new_payload), actor_id, now, record_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, action, actor_id, new_version,
                     self._json(dict(details, tx_id=tx_id, receipt_id=receipt_id)), now),
                )
                connection.execute(
                    "INSERT INTO case_receipt_applications(record_id,receipt_id,mode,applied_at) VALUES(?,?,?,?)",
                    (record_id, receipt_id, step["mode"], now),
                )
                connection.execute(
                    "UPDATE group_tx_steps SET status='done', details=? WHERE id=?",
                    (self._json(dict(details, version=new_version)), int(step["id"])),
                )
                connection.commit()

    def resume_transaction(self, tx_id: int, actor_id: str, step_handler: StepHandler) -> Dict[str, Any]:
        """写入中断后从最近完成案件继续。"""
        with self._connect() as connection:
            tx = connection.execute("SELECT * FROM group_transactions WHERE id=?", (tx_id,)).fetchone()
            if tx is None:
                raise NotFound("组事务不存在")
            if tx["kind"] != "receipt":
                raise Conflict("非回执事务无需续算")
            receipt_id = int(tx["receipt_id"])
            receipt_row = connection.execute("SELECT * FROM material_receipts WHERE id=?", (receipt_id,)).fetchone()
            group_id = int(receipt_row["submitted_group_id"])
            group = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
            active = group["active_tx_id"]
        if active is None or int(active) != tx_id:
            raise Conflict("组事务#%s不在活动状态，无需续算" % tx_id)
        progress = self._complete_steps(tx_id, receipt_id, actor_id, step_handler)
        return {"tx_id": tx_id, "group": self.get_group(group_id), "progress": progress}

    def resume_all(self, actor_id: str, step_handler: StepHandler) -> List[Dict[str, Any]]:
        """启动恢复：把所有因中断残留的活动回执事务续算完。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT gt.id AS tx_id FROM group_transactions gt "
                "JOIN case_groups g ON g.active_tx_id = gt.id "
                "WHERE gt.kind='receipt' AND gt.status='running'"
            ).fetchall()
            tx_ids = [int(row["tx_id"]) for row in rows]
        return [self.resume_transaction(tx_id, actor_id, step_handler) for tx_id in tx_ids]

    # ----------------------------------------------------------- 拆分/合并

    def restructure(self, op: str, payload: Dict[str, Any], actor_id: str,
                    base_revision: int, base_op_seq: Optional[int], request_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_id = int(payload["source_group_id"])
            group = connection.execute("SELECT * FROM case_groups WHERE id=?", (source_id,)).fetchone()
            if group is None:
                connection.rollback()
                raise NotFound("案组不存在")
            if int(group["revision"]) != int(base_revision):
                connection.rollback()
                return {"parked": True, "reason": "stale_revision", "group": self._group_row(group)}
            if base_op_seq is not None and int(group["op_seq"]) != int(base_op_seq):
                connection.rollback()
                return {"parked": True, "reason": "concurrent_write", "group": self._group_row(group)}
            if group["active_tx_id"] is not None:
                connection.rollback()
                return {"parked": True, "reason": "active_transaction", "group": self._group_row(group)}

            # 拆分在原组上递增修订；合并的新修订属于目标组，按目标组当前修订递增。
            new_revision = int(group["revision"]) + 1
            op_seq = int(group["op_seq"]) + 1

            if op == "split":
                target_records = [int(item) for item in payload["target_record_ids"]]
                target_name = payload["target_name"]
                members = connection.execute(
                    "SELECT * FROM group_memberships WHERE group_id=?", (source_id,)
                ).fetchall()
                moving = {int(row["record_id"]) for row in members if int(row["record_id"]) in target_records}
                missing = moving.symmetric_difference(set(target_records))
                if missing:
                    connection.rollback()
                    raise ValidationError("待拆分案件不属于本案组：%s" % sorted(missing))
                principals = [row for row in members if row["role"] == "principal" and int(row["record_id"]) in moving]
                if principals:
                    connection.rollback()
                    raise ValidationError("主案不能从原案组拆出")
                if len(moving) == len(members):
                    connection.rollback()
                    raise ValidationError("不能把案组全部拆空")
                cursor = connection.execute(
                    "INSERT INTO case_groups(name,revision,op_seq,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (target_name, 1, 0, "active", actor_id, now, now),
                )
                target_id = int(cursor.lastrowid)
                for record_id in sorted(moving):
                    connection.execute(
                        "UPDATE group_memberships SET group_id=?, joined_revision=1 WHERE group_id=? AND record_id=?",
                        (target_id, source_id, record_id),
                    )
                restructure_detail = {"op": "split", "source_group_id": source_id, "target_group_id": target_id,
                                      "moved_record_ids": sorted(moving), "from_revision": int(group["revision"])}
                self._audit(connection, target_id, actor_id, "group_created_by_split",
                            {"source_group_id": source_id, "moved_record_ids": sorted(moving)})
            else:
                target_id = int(payload["target_group_id"])
                target = connection.execute("SELECT * FROM case_groups WHERE id=?", (target_id,)).fetchone()
                if target is None:
                    connection.rollback()
                    raise NotFound("并入案组不存在")
                if int(target["id"]) == source_id:
                    connection.rollback()
                    raise ValidationError("案组不能与自身合并")
                if target["active_tx_id"] is not None:
                    connection.rollback()
                    return {"parked": True, "reason": "target_active_transaction", "group": self._group_row(group)}
                target_members = connection.execute(
                    "SELECT role FROM group_memberships WHERE group_id=?", (target_id,)
                ).fetchall()
                source_members = connection.execute(
                    "SELECT record_id, role FROM group_memberships WHERE group_id=?", (source_id,)
                ).fetchall()
                target_has_principal = any(row["role"] == "principal" for row in target_members)
                source_principal = next((int(row["record_id"]) for row in source_members if row["role"] == "principal"), None)
                if target_has_principal and source_principal is not None:
                    connection.rollback()
                    raise ValidationError("两个案组都含主案，无法合并")
                new_revision = int(target["revision"]) + 1
                op_seq = int(target["op_seq"]) + 1
                moved = [int(row["record_id"]) for row in source_members]
                for row in source_members:
                    connection.execute(
                        "UPDATE group_memberships SET group_id=?, joined_revision=? WHERE group_id=? AND record_id=?",
                        (target_id, new_revision, source_id, int(row["record_id"])),
                    )
                connection.execute(
                    "UPDATE case_groups SET status='merged', active_tx_id=NULL, updated_at=? WHERE id=?",
                    (now, source_id),
                )
                # 旧组的待合组项继续挂在旧组下，永不写进新组（由显式合组决定去向）。
                restructure_detail = {"op": "merge", "source_group_id": source_id, "target_group_id": target_id,
                                      "source_revision": int(group["revision"]),
                                      "moved_record_ids": moved, "from_revision": int(target["revision"])}

            connection.execute(
                "UPDATE case_groups SET revision=?, op_seq=?, updated_at=? WHERE id=?",
                (new_revision, op_seq, now, target_id if op == "merge" else source_id),
            )
            revision_group_id = target_id if op == "merge" else source_id
            # 合并后新修订属于并入目标组；源组只保留改组审计并标记 merged。
            self._revision_event(connection, revision_group_id, op_seq, new_revision, op, actor_id, restructure_detail)
            self._audit(connection, source_id, actor_id, "group_restructured", restructure_detail)
            if op == "merge":
                self._audit(connection, target_id, actor_id, "group_absorbed", restructure_detail)
            if request_id is not None:
                connection.execute(
                    "UPDATE group_pending_requests SET status='resolved', resolved_at=? WHERE id=? AND status='pending'",
                    (now, request_id),
                )
            connection.commit()

        result = {"parked": False, "op": op, "revision": new_revision, "op_seq": op_seq,
                  "detail": restructure_detail, "group": self.get_group(source_id)}
        if op == "split":
            result["target_group_id"] = target_id
            result["target_group"] = self.get_group(target_id)
        else:
            result["target_group"] = self.get_group(target_id)
        return result

    # ------------------------------------------------------- 待合组的处置

    def resolve_parked_receipt(self, receipt_id: int, actor_id: str) -> Dict[str, Any]:
        """决定让旧修订回执进入当前组：刷新修订水位，随后由正常回执流程仲裁/执行。"""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM material_receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise NotFound("回执不存在")
            if row["status"] != "parked":
                raise Conflict("回执不是待合组状态")
            group_id = int(row["submitted_group_id"])
            group = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
            if group["status"] != "active":
                raise Conflict("案组已%s，回执不能合入" % group["status"])
            anchor = connection.execute(
                "SELECT 1 FROM group_memberships WHERE group_id=? AND record_id=?",
                (group_id, int(row["anchor_record_id"])),
            ).fetchone()
            if anchor is None:
                raise Conflict("回执主案已不在案组中，请改在新组提交或弃用该回执")
            payload = json.loads(row["payload"])
            now = _now()
            connection.execute(
                "UPDATE material_receipts SET group_revision=?, base_op_seq=NULL, status='queued', updated_at=? WHERE id=?",
                (int(group["revision"]), now, receipt_id),
            )
            self._audit(connection, group_id, actor_id, "receipt_unparked",
                        {"receipt_id": receipt_id, "new_revision": int(group["revision"])})
            connection.commit()
        return self.get_receipt(receipt_id)

    def discard_parked_receipt(self, receipt_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM material_receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise NotFound("回执不存在")
            if row["status"] != "parked":
                raise Conflict("回执不是待合组状态")
            connection.execute("UPDATE material_receipts SET status='discarded', updated_at=? WHERE id=?", (_now(), receipt_id))
            self._audit(connection, int(row["submitted_group_id"]), actor_id, "receipt_discarded",
                        {"receipt_id": receipt_id})
            connection.commit()
        return self.get_receipt(receipt_id)

    def resolve_parked_change(self, request_id: int, actor_id: str, step_handler: StepHandler) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM group_pending_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFound("待合组改组请求不存在")
            if row["status"] != "pending":
                raise Conflict("该改组请求已处理")
            payload = json.loads(row["payload"])
            group_id = int(row["group_id"])
            group = connection.execute("SELECT * FROM case_groups WHERE id=?", (group_id,)).fetchone()
            base_revision = int(group["revision"])
            base_op_seq = int(group["op_seq"])
        # 复用改组主流程；成功时由其关闭 pending 请求。
        result = self.restructure(row["kind"], payload, actor_id, base_revision, base_op_seq, request_id=request_id)
        return result

    def discard_parked_change(self, request_id: int, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM group_pending_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFound("待合组改组请求不存在")
            if row["status"] != "pending":
                raise Conflict("该改组请求已处理")
            connection.execute(
                "UPDATE group_pending_requests SET status='discarded', resolved_at=? WHERE id=?",
                (_now(), request_id),
            )
            self._audit(connection, int(row["group_id"]), actor_id, "change_discarded", {"request_id": request_id})
            connection.commit()
        return {"id": request_id, "status": "discarded"}
