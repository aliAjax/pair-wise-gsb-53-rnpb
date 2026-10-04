"""关联案组用例编排：校验、权限、回执组事务与改组仲裁。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, choice, integer, text, text_list
from .group_repository import GroupRepository
from .rules import (
    DECIDED_STATES,
    RECEIPT_KINDS,
    RECEIPT_ROLES,
    RESTRUCTURE_OPS,
    RESTRUCTURE_ROLES,
    GROUP_CREATE_ROLES,
    append_deadline_diff,
    shift_group_deadline,
)


class GroupService:
    def __init__(self, repository: Any, group_repository: GroupRepository, audit: AuditRecorder = None,
                 step_observer: Any = None) -> None:
        self.repository = repository
        self.groups = group_repository
        self.audit = audit or AuditRecorder(repository)
        # 测试钩子：每个案件步骤完成后调用，可抛错模拟写入中断。
        self.step_observer = step_observer

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _role(self, actor: Actor, roles: set, action: str) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行%s" % action)

    # ------------------------------------------------------------- 建组

    def create_group(self, actor: Actor, name: str, members: List[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, GROUP_CREATE_ROLES, "建组")
        name = text({"name": name}, "name")
        if not isinstance(members, list) or not members:
            raise ValidationError("members必须是非空列表")
        normalized = []
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                raise ValidationError("members[%s]必须是对象" % index)
            record_id = integer(member, "record_id", 1)
            role = choice(member, "role", ["principal", "dependent"])
            depends = str(member.get("depends_on_receipt", "") or "").strip()
            if role == "dependent" and not depends:
                raise ValidationError("关联案必须声明依赖的回执键 depends_on_receipt")
            normalized.append({"record_id": record_id, "role": role, "depends_on_receipt": depends})
        return self.groups.create_group(name, normalized, actor.user_id)

    def get_group(self, actor: Actor, group_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        return self.groups.get_group(group_id)

    def list_groups(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.groups.list_groups()

    def list_members(self, actor: Actor, group_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.groups.list_members(group_id)

    def pending(self, actor: Actor, group_id: int) -> Dict[str, List[Dict[str, Any]]]:
        actor = self._actor(actor)
        return self.groups.list_pending(group_id)

    def group_timeline(self, actor: Actor, group_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        return self.groups.group_audit_timeline(group_id)

    def tx_progress(self, actor: Actor, tx_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        return self.groups.tx_progress(tx_id)

    # ----------------------------------------------------- 回执步骤处理

    def _handle_step(self, record: Dict[str, Any], receipt: Dict[str, Any]) -> tuple:
        """组事务中单个案件的失效重算或差异追加。"""
        kind = receipt["kind"]
        shift_days = int(receipt["shift_days"])
        payload = record["payload"]
        state = record["state"]
        details_base = {
            "receipt_id": int(receipt["id"]),
            "receipt_key": receipt["receipt_key"],
            "kind": kind,
            "shift_days": shift_days,
            "group_revision": int(receipt["group_revision"]),
        }
        if state in DECIDED_STATES:
            # 已决定/归档案件：只追加差异，状态与期限不变。
            diff = {"receipt_key": receipt["receipt_key"], "kind": kind,
                    "shift_days": shift_days, "deadline_day_before": int(payload.get("deadline_day", 0)),
                    "deadline_day_after": int(payload.get("deadline_day", 0)) + shift_days}
            new_payload = append_deadline_diff(payload, diff)
            details = dict(details_base, mode="diff_only", diff=diff)
            if self.step_observer is not None:
                self.step_observer(record["id"], "diff_only")
            return state, new_payload, "receipt_diff_appended", details

        # 未决定案件：期限先失效，随后按回执顺延重算并刷新派生字段。
        new_payload, before = shift_group_deadline(payload, shift_days)
        # 补件宽限也顺延同样的天数，避免关联案期限断档。
        if "evidence_due_day" in new_payload:
            new_payload["evidence_due_day"] = int(new_payload["evidence_due_day"]) + shift_days
        details = dict(details_base, mode="recompute", invalidated=True,
                       deadline_day_before=before, deadline_day_after=new_payload["deadline_day"])
        if self.step_observer is not None:
            self.step_observer(record["id"], "recompute")
        return state, new_payload, "deadline_recomputed", details

    # ------------------------------------------------------------- 回执

    def _validate_receipt(self, data: Dict[str, Any]) -> Dict[str, Any]:
        receipt_key = text(data, "receipt_key")
        record_id = integer(data, "anchor_record_id", 1)
        kind = choice(data, "kind", list(RECEIPT_KINDS))
        shift_days = integer(data, "shift_days", 0)
        if kind == "reschedule" and shift_days <= 0:
            raise ValidationError("改期回执的shift_days必须为正")
        return {
            "receipt_key": receipt_key,
            "anchor_record_id": record_id,
            "kind": kind,
            "shift_days": shift_days,
            "documents": text_list(data, "documents", 1),
            "note": str(data.get("note", "") or "").strip(),
        }

    def submit_receipt(self, actor: Actor, group_id: int, data: Dict[str, Any],
                       base_revision: Optional[int] = None,
                       base_op_seq: Optional[int] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RECEIPT_ROLES, "提交材料回执")
        group = self.groups.get_group(group_id)
        receipt_input = self._validate_receipt(data or {})

        membership = self._membership(group_id, receipt_input["anchor_record_id"])
        if membership is None:
            if group["status"] != "active":
                # 案组拆分/合并后旧修订回执不能写进新组：保留输入，停在待合组。
                parked = self.groups.park_receipt(
                    receipt_input["receipt_key"], receipt_input["anchor_record_id"],
                    receipt_input["kind"], receipt_input["shift_days"],
                    int(base_revision) if base_revision is not None else int(group["revision"]),
                    base_op_seq, group_id, receipt_input, actor.user_id,
                    "group_%s" % group["status"])
                return {"status": "parked", "reason": "group_%s" % group["status"], "receipt": parked,
                        "group": group}
            raise ValidationError("回执主案不属于案组%s" % group_id)

        # 回执带案组修订号：未显式给出时按当前修订盖戳（向后兼容）。
        group_revision = int(base_revision) if base_revision is not None else int(group["revision"])
        existing = self.groups.get_receipt_by_key(receipt_input["receipt_key"])
        if existing is not None:
            return self._handle_existing_receipt(actor, existing, group, receipt_input)

        receipt = dict(receipt_input)
        receipt.update({"group_revision": group_revision, "base_op_seq": base_op_seq,
                        "submitted_group_id": group_id, "payload": receipt_input})
        return self._open_or_park(actor, receipt)

    def _membership(self, group_id: int, record_id: int) -> Optional[Dict[str, Any]]:
        for member in self.groups.list_members(group_id):
            if int(member["record_id"]) == int(record_id):
                return member
        return None

    def _handle_existing_receipt(self, actor: Actor, existing: Dict[str, Any], group: Dict[str, Any],
                                 receipt_input: Dict[str, Any]) -> Dict[str, Any]:
        """重复回执/重试：不重复顺延，按已有状态给出确定结果。"""
        status = existing["status"]
        if status == "applied":
            progress = self.groups.tx_progress(int(existing["tx_id"])) if existing.get("tx_id") else None
            return {"status": "applied", "receipt": existing, "idempotent": True, "progress": progress}
        if status in ("applying",):
            # 上次写入中断：从最近完成案件继续。
            with self.groups._connect() as connection:
                tx_id = connection.execute(
                    "SELECT id FROM group_transactions WHERE receipt_id=? AND status='running' ORDER BY id DESC LIMIT 1",
                    (existing["id"],),
                ).fetchone()
            if tx_id is not None and group.get("active_tx_id") == int(tx_id["id"]):
                result = self.groups.resume_transaction(int(tx_id["id"]), actor.user_id, self._handle_step)
                result["idempotent"] = True
                result["status"] = "resumed"
                return result
            return {"status": "applying", "receipt": existing, "idempotent": True}
        if status == "parked":
            return {"status": "parked", "receipt": existing, "idempotent": True}
        if status == "queued":
            receipt = self._receipt_from_row(existing)
            return self._open_or_park(actor, receipt)
        return {"status": status, "receipt": existing, "idempotent": True}

    @staticmethod
    def _receipt_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
        payload = row["payload"]
        return {
            "receipt_key": row["receipt_key"],
            "anchor_record_id": int(row["anchor_record_id"]),
            "kind": row["kind"],
            "shift_days": int(row["shift_days"]),
            "group_revision": int(row["group_revision"]),
            "base_op_seq": row["base_op_seq"],
            "submitted_group_id": int(row["submitted_group_id"]),
            "payload": payload,
            "existing_receipt_id": int(row["id"]),
        }

    def _open_or_park(self, actor: Actor, receipt: Dict[str, Any]) -> Dict[str, Any]:
        existing_id = receipt.pop("existing_receipt_id", None)
        try:
            result = self.groups.run_receipt_transaction(receipt, actor.user_id, self._handle_step,
                                                         existing_receipt_id=existing_id)
        except Conflict as exc:
            if "活动" in str(exc) or "组事务" in str(exc):
                # 后到者：保留输入，停在待合组。
                parked = self.groups.park_receipt(
                    receipt["receipt_key"], receipt["anchor_record_id"], receipt["kind"],
                    receipt["shift_days"], receipt["group_revision"], receipt.get("base_op_seq"),
                    receipt["submitted_group_id"], receipt["payload"], actor.user_id, "active_transaction",
                    receipt_id=existing_id,
                )
                return {"status": "parked", "reason": "active_transaction", "receipt": parked}
            raise
        if result.get("parked"):
            parked = self.groups.park_receipt(
                receipt["receipt_key"], receipt["anchor_record_id"], receipt["kind"],
                receipt["shift_days"], receipt["group_revision"], receipt.get("base_op_seq"),
                receipt["submitted_group_id"], receipt["payload"], actor.user_id, result["reason"],
                receipt_id=existing_id,
            )
            return {"status": "parked", "reason": result["reason"], "receipt": parked,
                    "group": result["group"]}
        return {"status": "applied", "receipt": self.groups.get_receipt(result["receipt_id"]),
                "tx_id": result["tx_id"], "group": result["group"], "progress": result["progress"]}

    # ----------------------------------------------------------- 拆分合并

    def restructure(self, actor: Actor, op: str, data: Dict[str, Any],
                    base_revision: Optional[int] = None, base_op_seq: Optional[int] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RESTRUCTURE_ROLES, "案组拆分/合并")
        op = choice({"op": op}, "op", list(RESTRUCTURE_OPS))
        if not isinstance(data, dict):
            raise ValidationError("data必须是对象")
        source_group_id = integer(data, "source_group_id", 1)
        payload: Dict[str, Any] = {"source_group_id": source_group_id}
        if op == "split":
            payload["target_name"] = text(data, "target_name")
            raw_ids = data.get("target_record_ids", [])
            if not isinstance(raw_ids, list) or not raw_ids or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in raw_ids
            ):
                raise ValidationError("target_record_ids必须是非空正整数列表")
            payload["target_record_ids"] = raw_ids
        else:
            payload["target_group_id"] = integer(data, "target_group_id", 1)

        group = self.groups.get_group(source_group_id)
        if base_revision is None:
            base_revision = int(group["revision"])
        if base_op_seq is None:
            base_op_seq = int(group["op_seq"])
        try:
            result = self.groups.restructure(op, payload, actor.user_id, int(base_revision),
                                             int(base_op_seq) if base_op_seq is not None else None)
        except Conflict as exc:
            if "组事务" in str(exc) or "活动" in str(exc):
                request_id = self.groups.park_change_request(source_group_id, op, payload, actor.user_id)
                return {"status": "parked", "reason": "active_transaction", "request_id": request_id,
                        "group": self.groups.get_group(source_group_id)}
            raise
        if result.get("parked"):
            request_id = self.groups.park_change_request(source_group_id, op, payload, actor.user_id)
            return {"status": "parked", "reason": result["reason"], "request_id": request_id,
                    "group": result["group"]}
        result["status"] = "applied"
        return result

    # --------------------------------------------------------- 待合组处置

    def resolve_receipt(self, actor: Actor, receipt_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RECEIPT_ROLES, "合组回执")
        queued = self.groups.resolve_parked_receipt(receipt_id, actor.user_id)
        receipt = self._receipt_from_row(queued)
        return self._open_or_park(actor, receipt)

    def discard_receipt(self, actor: Actor, receipt_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RECEIPT_ROLES, "弃用回执")
        return self.groups.discard_parked_receipt(receipt_id, actor.user_id)

    def resolve_change(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RESTRUCTURE_ROLES, "合组改组")
        result = self.groups.resolve_parked_change(request_id, actor.user_id, self._handle_step)
        result["status"] = "applied"
        return result

    def discard_change(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RESTRUCTURE_ROLES, "弃用改组")
        return self.groups.discard_parked_change(request_id, actor.user_id)

    def resume(self, actor: Actor, tx_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._role(actor, RECEIPT_ROLES, "续算组事务")
        return self.groups.resume_transaction(tx_id, actor.user_id, self._handle_step)

    def recover_on_startup(self, actor: Actor = None) -> List[Dict[str, Any]]:
        """服务启动时恢复所有中断的活动组事务。"""
        actor = actor or Actor("system", "admin")
        return self.groups.resume_all(actor.user_id, self._handle_step)
