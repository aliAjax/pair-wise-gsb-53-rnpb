"""案组期限联动的纯规则，不触碰数据库。

主案改期或补件以"顺延天数"的形式进入案组：只有出具回执的主案与
直接依赖它的关联案会受影响；其中未决定案件失效并重算，已决定或
归档案件的期限保持定格，只产生差异（deadline diff）。
"""
from typing import Any, Dict, List

from .domain import ValidationError

# 已决定或归档：期限不再改写，回执只追加差异
FROZEN_STATES = frozenset({"decided", "closed"})


def is_frozen(state: str) -> bool:
    return state in FROZEN_STATES


def affected_members(members: List[Dict[str, Any]], origin_record_id: int) -> List[Dict[str, Any]]:
    """返回受回执影响的成员顺序：回执主案在前，直接依赖者按案件号排后。"""
    origin_record_id = int(origin_record_id)
    active = [m for m in members if int(m["active"]) == 1]
    by_id = {int(m["record_id"]): m for m in active}
    origin = by_id.get(origin_record_id)
    if origin is None:
        raise ValidationError("回执案件不在该案组")
    if origin["member_role"] != "main":
        raise ValidationError("材料回执必须由主案登记")
    ordered = [origin]
    dependents = [m for m in active if int(m.get("depends_on_record_id") or 0) == origin_record_id]
    ordered.extend(sorted(dependents, key=lambda m: int(m["record_id"])))
    return ordered


def shifted_deadline(deadline: Dict[str, Any], shift_days: int) -> Dict[str, int]:
    """在既有顺延之上叠加本次回执，返回新的累计顺延与期限日。"""
    applied = int(deadline["applied_shift_days"]) + int(shift_days)
    return {
        "applied_shift_days": applied,
        "deadline_day": int(deadline["base_deadline_day"]) + applied,
    }


def diff_preview(current_deadline_day: int, shift_days: int) -> int:
    """已决定案件本应顺延到的期限日（仅用于追加差异，不改写期限）。"""
    return int(current_deadline_day) + int(shift_days)
