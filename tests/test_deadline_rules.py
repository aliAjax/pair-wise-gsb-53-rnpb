import unittest

from src.deadlines import affected_members, diff_preview, is_frozen, shifted_deadline
from src.domain import ValidationError


def member(record_id, role="related", depends=None, active=1):
    return {"record_id": record_id, "member_role": role, "depends_on_record_id": depends,
            "active": active}


class DeadlinesRulesTest(unittest.TestCase):
    def test_affected_only_origin_and_direct_dependents(self):
        members = [member(1, "main"), member(2, depends=1), member(3, depends=1),
                   member(9, "main"), member(10, depends=9)]
        affected = affected_members(members, 1)
        self.assertEqual([m["record_id"] for m in affected], [1, 2, 3])

    def test_affected_rejects_related_origin(self):
        with self.assertRaises(ValidationError):
            affected_members([member(1, "main"), member(2, depends=1)], 2)

    def test_affected_rejects_inactive_origin(self):
        with self.assertRaises(ValidationError):
            affected_members([member(1, "main", active=0), member(2, depends=1)], 1)

    def test_frozen_states(self):
        self.assertTrue(is_frozen("decided"))
        self.assertTrue(is_frozen("closed"))
        self.assertFalse(is_frozen("submitted"))
        self.assertFalse(is_frozen("response_received"))

    def test_shift_accumulates(self):
        deadline = {"base_deadline_day": 130, "applied_shift_days": 10, "deadline_day": 140}
        first = shifted_deadline(deadline, 15)
        self.assertEqual(first, {"applied_shift_days": 25, "deadline_day": 155})
        second = shifted_deadline({**deadline, **first}, 5)
        self.assertEqual(second, {"applied_shift_days": 30, "deadline_day": 160})

    def test_diff_preview_keeps_record(self):
        self.assertEqual(diff_preview(130, 15), 145)


if __name__ == "__main__":
    unittest.main()
