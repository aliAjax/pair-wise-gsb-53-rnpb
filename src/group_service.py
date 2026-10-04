"""案组事务用例：建组、材料回执、改组与待合组，以及崩溃后的断点恢复。

组事务采用"登记 + 逐案件检查点"两段式：登记段在一把写锁内把回执、
任务、失效标记和案组修订号一起提交；执行段逐案件独立提交。写入
中断后，启动（或重试）时从最近完成的案件继续；重复回执或重试命中
已完成条目时直接返回，不会再次顺延。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import (Actor, GroupDissolved, PermissionDenied, StaleRevision,
                     ValidationError, integer, text)
from .group_repository import GroupRepository
from .rules import DomainRules

GROUP_ROLES = {"case_officer", "supervisor", "admin"}
REGROUP_ROLES = {"supervisor", "admin"}


class GroupService:
    def __init__(self, repository: Any, groups: GroupRepository, rules: DomainRules,
                 audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.groups = groups
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 测试钩子：登记段提交前回调（用于并发交错）
        self.before_receipt_commit = None
        self.before_regroup_commit = None
        # 测试钩子：执行第 N 个案件条目后模拟写入中断
        self.crash_after_items = 0
        self._items_seen = 0

    # ---- 身份 ----

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require_group_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role) or (
            actor.role not in GROUP_ROLES and not actor.role == "admin"
        ):
            raise PermissionDenied("角色无权操作案组")

    def _require_regroup_role(self, actor: Actor) -> None:
        if actor.role != "admin" and actor.role not in REGROUP_ROLES:
            raise PermissionDenied("角色无权拆分或合并案组")

    # ---- 建组 ----

    def create_group(self, actor: Actor, reference: str, members: List[Dict[str, Any]], note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        reference = text({"reference": reference}, "reference")
        if not isinstance(members, list) or not members:
            raise ValidationError("members必须是非空列表")
        prepared: List[Dict[str, Any]] = []
        seen = set()
        main_count = 0
        for item in members:
            if not isinstance(item, dict):
                raise ValidationError("成员必须是对象")
            record_id = integer(item, "record_id", 1)
            if record_id in seen:
                raise ValidationError("成员案件不能重复")
            seen.add(record_id)
            if not self._record_exists(record_id):
                raise ValidationError("案件%s不存在" % record_id)
            role = item.get("member_role", "related")
            if role not in {"main", "related"}:
                raise ValidationError("member_role只能是main/related")
            depends = item.get("depends_on_record_id")
            prepared.append({"record_id": record_id, "member_role": role,
                             "depends_on_record_id": int(depends) if depends is not None else None})
            if role == "main":
                main_count += 1
                if depends is not None:
                    raise ValidationError("主案不能依赖其他案件")
        if main_count < 1:
            raise ValidationError("案组至少需要一个主案")
        by_id = {m["record_id"] for m in prepared}
        for member in prepared:
            if member["member_role"] == "related" and member["depends_on_record_id"] not in by_id:
                raise ValidationError("依赖主案必须在同一案组内")
            if member["depends_on_record_id"] is not None and not any(
                m["record_id"] == member["depends_on_record_id"] and m["member_role"] == "main"
                for m in prepared
            ):
                raise ValidationError("depends_on_record_id必须指向同组主案")
        return self.groups.create_group(reference, prepared, note or "", actor.user_id)

    def _record_exists(self, record_id: int) -> bool:
        try:
            self.repository.get(record_id)
            return True
        except Exception:
            return False

    # ---- 查询 ----

    def get_group(self, actor: Actor, group_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return self.groups.get_group(group_id)

    def find_receipt(self, receipt_key: str) -> Optional[Dict[str, Any]]:
        return self.groups.find_receipt(receipt_key)

    def get_pending(self, actor: Actor, pending_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return self.groups.get_pending(int(pending_id))

    def list_groups(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return self.groups.list_groups(status=status, limit=limit)

    def group_detail(self, actor: Actor, group_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return {
            "group": self.groups.get_group(group_id),
            "members": self.groups.members(group_id),
            "deadlines": self.groups.deadlines(group_id),
            "diffs": self.groups.diffs(group_id),
        }

    def group_timeline(self, actor: Actor, group_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return self.groups.group_audit(group_id)

    # ---- 材料回执（可恢复组事务入口） ----

    def submit_receipt(self, actor: Actor, group_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        group_id = integer({"group_id": group_id}, "group_id", 1)
        receipt_key = text(payload, "receipt_key")
        origin_record_id = integer(payload, "origin_record_id", 1)
        expected_revision = integer(payload, "expected_revision", 1)
        shift_days = integer(payload, "shift_days", 0)
        note = str(payload.get("note", "") or "")

        existing = self.groups.find_receipt(receipt_key)
        if existing is not None:
            # 重复回执：不重复顺延，直接返回原任务的恢复执行结果
            if int(existing["group_id"]) != group_id:
                raise ValidationError("回执编号已用于其他案组")
            return self._resume_receipt(existing, actor, duplicate=True)

        try:
            registered = self.groups.insert_receipt_job(
                group_id=group_id,
                receipt_key=receipt_key,
                origin_record_id=origin_record_id,
                expected_revision=expected_revision,
                shift_days=shift_days,
                note=note,
                actor_id=actor.user_id,
                before_commit=self.before_receipt_commit,
            )
        except (StaleRevision, GroupDissolved) as exc:
            # 后到者：修订落后或案组已被拆分/合并，输入原样保留在待合组
            current = getattr(exc, "current_revision", None)
            dedupe_key = "receipt:%s" % receipt_key
            already = self.groups.waiting_pending(dedupe_key)
            if already is not None:
                # 同一回执重试：不重复挂起
                return {"status": "pending_merge", "pending_merge_id": already["id"],
                        "duplicate": True,
                        "message": "案组已变化，回执保留在待合组",
                        "current_revision": current}
            parked = self.groups.park(
                kind="receipt",
                dedupe_key=dedupe_key,
                payload={"receipt_key": receipt_key, "origin_record_id": origin_record_id,
                         "shift_days": shift_days, "note": note, "expected_revision": expected_revision,
                         "requested_group_id": group_id, "current_revision": current,
                         "reason": exc.code},
                actor_id=actor.user_id,
                origin_group_id=group_id,
                origin_revision=expected_revision,
            )
            return {"status": "pending_merge", "pending_merge_id": parked["id"],
                    "message": "案组已变化，回执保留在待合组",
                    "current_revision": current}

        result = self._drive_job(registered["job_id"], actor)
        result["status"] = result.get("status", "applied")
        result["group_id"] = group_id
        result["group_revision"] = registered["target_revision"]
        if registered.get("duplicate"):
            result["duplicate"] = True
        return result

    def _resume_receipt(self, receipt: Dict[str, Any], actor: Actor, duplicate: bool = False) -> Dict[str, Any]:
        if receipt["status"] == "applied":
            job = self.groups.get_job(int(receipt["job_id"]))
            return {"status": "applied", "duplicate": True, "already_applied": True,
                    "group_id": int(receipt["group_id"]), "group_revision": job["input"]["target_revision"],
                    "receipt_id": int(receipt["id"]), "items": self.groups.job_items(int(receipt["job_id"]))}
        result = self._drive_job(int(receipt["job_id"]), actor)
        result["duplicate"] = duplicate
        result["group_id"] = int(receipt["group_id"])
        return result

    def _drive_job(self, job_id: int, actor: Actor = None) -> Dict[str, Any]:
        """逐案件执行并检查点；从最近完成案件继续。"""
        actor_id = actor.user_id if actor else "system"
        job = self.groups.get_job(job_id)
        items = self.groups.job_items(job_id)
        executed = []
        # 计数仅覆盖本次驱动；恢复时先跳过的已完成条目不计入
        self._items_seen = 0
        for item in items:
            if item["status"] == "done":
                # 中断恢复：跳过已完成条目，不重复顺延
                continue
            outcome = self.groups.run_job_item(job_id, int(item["record_id"]), actor_id=actor_id)
            executed.append(outcome)
            if self.crash_after_items:
                self._items_seen += 1
                if self._items_seen >= self.crash_after_items:
                    raise RuntimeError("simulated write interruption")
        refreshed = self.groups.get_job(job_id)
        return {"status": "applied" if refreshed["status"] == "completed" else refreshed["status"],
                "receipt_id": refreshed["ref_id"],
                "job_id": job_id, "executed": executed, "items": self.groups.job_items(job_id),
                "last_completed_record_id": refreshed["last_completed_record_id"]}

    def resume_interrupted(self, actor: Actor = None) -> List[Dict[str, Any]]:
        """写入中断后恢复：所有 running 的回执任务从最近完成案件继续。"""
        results = []
        for job in self.groups.running_jobs():
            if job["kind"] != "apply_receipt":
                continue
            results.append(self._drive_job(int(job["id"]), actor))
        return results

    # ---- 拆分 / 合并 ----

    def regroup(self, actor: Actor, payload: Dict[str, Any]) -> Any:
        actor = self._actor(actor)
        self._require_regroup_role(actor)
        plan = self._validate_regroup_plan(payload)
        try:
            created = self.groups.execute_regroup(plan, actor.user_id,
                                                  before_commit=self.before_regroup_commit)
        except StaleRevision as exc:
            # 改组与回执同时提交，先到修订已生效：改组输入保留，停在待合组
            dedupe_key = "regroup:" + "|".join("%s:%s" % (s["group_id"], s["expected_revision"])
                                               for s in plan["sources"])
            already = self.groups.waiting_pending(dedupe_key)
            if already is not None:
                return {"status": "pending_merge", "pending_merge_id": already["id"],
                        "duplicate": True,
                        "message": "案组修订已变化，改组计划保留在待合组",
                        "current_revision": exc.current_revision}
            parked = self.groups.park(
                kind="regroup",
                dedupe_key=dedupe_key,
                payload=payload,
                actor_id=actor.user_id,
                origin_group_id=plan["sources"][0]["group_id"],
                origin_revision=exc.current_revision,
            )
            return {"status": "pending_merge", "pending_merge_id": parked["id"],
                    "message": "案组修订已变化，改组计划保留在待合组",
                    "current_revision": exc.current_revision}
        return {"status": "created", "groups": created}

    @staticmethod
    def _validate_regroup_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
        sources = payload.get("sources")
        plans = payload.get("plans")
        if not isinstance(sources, list) or not sources:
            raise ValidationError("sources必须是非空列表")
        if not isinstance(plans, list) or not plans:
            raise ValidationError("plans必须是非空列表")
        prepared_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValidationError("sources项必须是对象")
            prepared_sources.append({
                "group_id": integer(source, "group_id", 1),
                "expected_revision": integer(source, "expected_revision", 1),
            })
        source_ids = [s["group_id"] for s in prepared_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValidationError("来源案组不能重复")
        prepared_plans = []
        for plan in plans:
            if not isinstance(plan, dict):
                raise ValidationError("plans项必须是对象")
            reference = text(plan, "reference")
            members = plan.get("members")
            if not isinstance(members, list) or not members:
                raise ValidationError("新案组成员必须是非空列表")
            prepared_members = []
            seen = set()
            main_count = 0
            for member in members:
                if not isinstance(member, dict):
                    raise ValidationError("成员必须是对象")
                record_id = integer(member, "record_id", 1)
                if record_id in seen:
                    raise ValidationError("新案组内成员重复")
                seen.add(record_id)
                role = member.get("member_role", "related")
                if role not in {"main", "related"}:
                    raise ValidationError("member_role只能是main/related")
                depends = member.get("depends_on_record_id")
                prepared_members.append({
                    "record_id": record_id,
                    "member_role": role,
                    "depends_on_record_id": int(depends) if depends is not None else None,
                })
                if role == "main":
                    main_count += 1
            if main_count < 1:
                raise ValidationError("新案组%s至少需要一个主案" % reference)
            member_ids = {m["record_id"] for m in prepared_members}
            for member in prepared_members:
                if member["depends_on_record_id"] is not None and (
                    member["depends_on_record_id"] not in member_ids
                    or not any(m["record_id"] == member["depends_on_record_id"] and m["member_role"] == "main"
                               for m in prepared_members)
                ):
                    raise ValidationError("新案组内依赖必须指向同组主案")
            prepared_plans.append({"reference": reference, "members": prepared_members,
                                   "note": str(plan.get("note", "") or "")})
        return {"sources": prepared_sources, "plans": prepared_plans}

    # ---- 待合组 ----

    def list_pending(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._require_group_role(actor)
        return self.groups.list_pending()

    def discard_pending(self, actor: Actor, pending_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_regroup_role(actor)
        self.groups.discard_pending(int(pending_id))
        return {"status": "discarded", "pending_merge_id": int(pending_id)}

    def resolve_pending(self, actor: Actor, pending_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """合组：只有显式指定目标新组并重新通过修订校验后，旧输入才会生效。"""
        actor = self._actor(actor)
        self._require_regroup_role(actor)
        pending = self.groups.get_pending(int(pending_id))
        if pending["kind"] == "receipt":
            target_group_id = integer(payload, "target_group_id", 1)
            registered = self.groups.merge_receipt_pending(
                pending["id"], target_group_id, actor.user_id,
                before_commit=self.before_receipt_commit,
            )
            result = self._drive_job(registered["job_id"], actor)
            result["status"] = result.get("status", "applied")
            result["group_id"] = target_group_id
            result["group_revision"] = registered["target_revision"]
            result["merged_pending_merge_id"] = pending["id"]
            return result
        if pending["kind"] == "regroup":
            # 改组输入保留原样；合组时可携带最新来源修订号做人工确认
            merged_payload = dict(pending["payload"])
            if isinstance(payload, dict) and payload.get("sources"):
                merged_payload["sources"] = payload["sources"]
            created = self.groups.execute_regroup(
                self._validate_regroup_plan(merged_payload),
                actor.user_id, pending_merge_id=pending["id"],
                before_commit=self.before_regroup_commit,
            )
            return {"status": "created", "groups": created, "merged_pending_merge_id": pending["id"]}
        raise ValidationError("未知的待合组类型")
