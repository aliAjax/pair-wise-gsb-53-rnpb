import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError

OFFICER = lambda: Actor("officer", "case_officer")
SUPERVISOR = lambda: Actor("super", "supervisor")
LEGAL = lambda: Actor("rep", "legal_rep")


def case_payload(applicant):
    return {'applicant_id': applicant, 'case_type': 'family', 'received_day': 100,
            'deadline_days': 30, 'response_day': 110,
            'representation_active': True, 'required_documents': ['passport']}


DECIDE_FLOW = [('submit', 'legal_rep', {'documents': ['passport']}),
               ('request_evidence', 'case_officer',
                {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补'}),
               ('respond', 'legal_rep', {'response_day': 120, 'documents': ['passport']}),
               ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': 'ok'})]


class GroupTransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.groups = self.service.group_service
        self.seq = 0

    def tearDown(self):
        self.temp.cleanup()

    def create_case(self, decided=False, closed=False):
        self.seq += 1
        record = self.service.create(
            Actor("creator", "intake_officer"), "IMM-%05d" % self.seq,
            case_payload("A-%05d" % self.seq))
        if decided or closed:
            for action, role, data in DECIDE_FLOW:
                record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
            if closed:
                record = self.service.act(Actor("op", "supervisor"), record["id"], record["version"],
                                          "close", {"closure_note": "归档"})
        return record

    def make_group(self, reference="FAM-1", members=None, actor=None):
        if members is None:
            main = self.create_case()
            related = self.create_case()
            members = [{"record_id": main["id"], "member_role": "main"},
                       {"record_id": related["id"], "member_role": "related",
                        "depends_on_record_id": main["id"]}]
            return self.groups.create_group(actor or SUPERVISOR(), reference, members), main, related
        return self.groups.create_group(actor or SUPERVISOR(), reference, members), None, None

    def deadline_map(self, group_id):
        return {d["record_id"]: d for d in self.groups.group_detail(SUPERVISOR(), group_id)["deadlines"]}

    def test_receipt_recalculates_only_unresolved_dependents(self):
        group, main, related = self.make_group()
        unrelated = self.create_case()
        other_main = self.create_case()
        self.groups.create_group(SUPERVISOR(), "FAM-X", [
            {"record_id": other_main["id"], "member_role": "main"},
            {"record_id": unrelated["id"], "member_role": "related",
             "depends_on_record_id": other_main["id"]}])

        result = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-1", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 15})
        self.assertEqual(result["status"], "applied")
        self.assertEqual([e["record_id"] for e in result["executed"]], [main["id"], related["id"]])

        deadlines = self.deadline_map(group["id"])
        self.assertEqual(deadlines[main["id"]]["deadline_day"], 145)
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 145)
        self.assertEqual(deadlines[main["id"]]["computed_revision"], 2)
        self.assertEqual(deadlines[main["id"]]["stale"], 0)
        # 另一个案组不受影响
        other = self.deadline_map(2)
        self.assertEqual(other[unrelated["id"]]["deadline_day"], 130)
        self.assertEqual(self.groups.get_group(SUPERVISOR(), group["id"])["revision"], 2)

    def test_decided_and_closed_cases_receive_diffs_only(self):
        main = self.create_case()
        decided = self.create_case(decided=True)
        closed = self.create_case(closed=True)
        pending = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-D", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": decided["id"], "member_role": "related",
             "depends_on_record_id": main["id"]},
            {"record_id": closed["id"], "member_role": "related",
             "depends_on_record_id": main["id"]},
            {"record_id": pending["id"], "member_role": "related",
             "depends_on_record_id": main["id"]}])

        result = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-D", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 12})
        modes = {e["record_id"]: e["detail"]["mode"] for e in result["executed"]}
        self.assertEqual(modes[main["id"]], "recalculated")
        self.assertEqual(modes[decided["id"]], "diff_only")
        self.assertEqual(modes[closed["id"]], "diff_only")
        self.assertEqual(modes[pending["id"]], "recalculated")

        deadlines = self.deadline_map(group["id"])
        # 已决定/归档案件期限定格在130，不顺延
        self.assertEqual(deadlines[decided["id"]]["deadline_day"], 130)
        self.assertEqual(deadlines[closed["id"]]["deadline_day"], 130)
        self.assertEqual(deadlines[pending["id"]]["deadline_day"], 142)
        self.assertEqual(deadlines[decided["id"]]["applied_shift_days"], 0)

        diffs = self.groups.group_detail(SUPERVISOR(), group["id"])["diffs"]
        diff_records = {d["record_id"]: d for d in diffs}
        self.assertEqual(diff_records[decided["id"]]["projected_deadline_day"], 142)
        self.assertEqual(diff_records[decided["id"]]["shift_days"], 12)
        self.assertEqual(diff_records[decided["id"]]["record_state"], "decided")
        self.assertEqual(diff_records[closed["id"]]["record_state"], "closed")

    def test_duplicate_receipt_does_not_extend_twice(self):
        group, main, related = self.make_group()
        payload = {"receipt_key": "RC-DUP", "origin_record_id": main["id"],
                   "expected_revision": 1, "shift_days": 15}
        first = self.groups.submit_receipt(OFFICER(), group["id"], payload)
        self.assertEqual(first["status"], "applied")
        again = self.groups.submit_receipt(OFFICER(), group["id"], payload)
        self.assertTrue(again["already_applied"])
        deadlines = self.deadline_map(group["id"])
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 145)
        self.assertEqual(deadlines[related["id"]]["applied_shift_days"], 15)

    def test_stale_revision_parks_receipt_without_touching_deadlines(self):
        group, main, related = self.make_group()
        first = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-A", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 10})
        self.assertEqual(first["group_revision"], 2)
        late = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-B", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 5})
        self.assertEqual(late["status"], "pending_merge")
        self.assertEqual(late["current_revision"], 2)
        pending = self.groups.list_pending(SUPERVISOR())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "receipt")
        # 期限未被后到回执改动
        deadlines = self.deadline_map(group["id"])
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 140)

    def test_parked_receipt_retry_does_not_duplicate(self):
        group, main, related = self.make_group()
        self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-A", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 10})
        payload = {"receipt_key": "RC-B", "origin_record_id": main["id"],
                   "expected_revision": 1, "shift_days": 5}
        first = self.groups.submit_receipt(OFFICER(), group["id"], payload)
        retry = self.groups.submit_receipt(OFFICER(), group["id"], payload)
        self.assertEqual(first["pending_merge_id"], retry["pending_merge_id"])
        self.assertTrue(retry["duplicate"])
        waiting = [p for p in self.groups.list_pending(SUPERVISOR())]
        self.assertEqual(len(waiting), 1)

    def test_parked_receipt_merges_into_explicit_target(self):
        group, main, related = self.make_group()
        self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-A", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 10})
        parked = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-B", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 5})
        merged = self.groups.resolve_pending(SUPERVISOR(), parked["pending_merge_id"],
                                             {"target_group_id": group["id"]})
        self.assertEqual(merged["status"], "applied")
        self.assertEqual(merged["group_revision"], 3)
        deadlines = self.deadline_map(group["id"])
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 145)
        # 待合组记录结案
        self.assertEqual(self.groups.get_pending(SUPERVISOR(), parked["pending_merge_id"])["status"], "merged")

    def test_merge_into_wrong_group_rejected_and_kept(self):
        group, main, related = self.make_group()
        other = self.create_case()
        other_group = self.groups.create_group(SUPERVISOR(), "FAM-O", [
            {"record_id": other["id"], "member_role": "main"}])
        self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-A", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 10})
        parked = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-B", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 5})
        with self.assertRaises(ValidationError):
            self.groups.resolve_pending(SUPERVISOR(), parked["pending_merge_id"],
                                        {"target_group_id": other_group["id"]})
        still = self.groups.get_pending(SUPERVISOR(), parked["pending_merge_id"])
        self.assertEqual(still["status"], "waiting")

    def test_regroup_splits_and_old_revision_cannot_write_new_group(self):
        main = self.create_case()
        r1 = self.create_case()
        main2 = self.create_case()
        r2 = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": r1["id"], "member_role": "related", "depends_on_record_id": main["id"]},
            {"record_id": main2["id"], "member_role": "main"},
            {"record_id": r2["id"], "member_role": "related", "depends_on_record_id": main2["id"]}])
        result = self.groups.regroup(SUPERVISOR(), {
            "sources": [{"group_id": group["id"], "expected_revision": 1}],
            "plans": [
                {"reference": "FAM-A", "members": [
                    {"record_id": main["id"], "member_role": "main"},
                    {"record_id": r1["id"], "member_role": "related",
                     "depends_on_record_id": main["id"]}]},
                {"reference": "FAM-B", "members": [
                    {"record_id": main2["id"], "member_role": "main"},
                    {"record_id": r2["id"], "member_role": "related",
                     "depends_on_record_id": main2["id"]}]}]})
        self.assertEqual(result["status"], "created")
        old = self.groups.get_group(SUPERVISOR(), group["id"])
        self.assertEqual(old["status"], "dissolved")
        new_a = result["groups"][0]
        self.assertEqual(new_a["revision"], 1)
        # 旧修订回执不能直接写进新组（预期修订号5不等于当前1）
        parked = self.groups.submit_receipt(OFFICER(), new_a["id"], {
            "receipt_key": "RC-OLD", "origin_record_id": main["id"],
            "expected_revision": 5, "shift_days": 3})
        self.assertEqual(parked["status"], "pending_merge")
        pending = self.groups.get_pending(SUPERVISOR(), parked["pending_merge_id"])
        self.assertEqual(pending["origin_group_id"], new_a["id"])
        self.assertEqual(pending["payload"]["receipt_key"], "RC-OLD")
        # 原组旧成员关系全部失效；旧修订输入不能写进旧组，只能进入待合组
        old_parked = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-X", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 1})
        self.assertEqual(old_parked["status"], "pending_merge")
        old_pending = self.groups.get_pending(SUPERVISOR(), old_parked["pending_merge_id"])
        self.assertEqual(old_pending["payload"]["reason"], "group_dissolved")

    def test_regroup_reanchors_related_to_new_lead(self):
        main_a = self.create_case()
        main_b = self.create_case()
        related = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main_a["id"], "member_role": "main"},
            {"record_id": main_b["id"], "member_role": "main"},
            {"record_id": related["id"], "member_role": "related",
             "depends_on_record_id": main_a["id"]}])
        # A 主案先收10天回执，关联案同步
        self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-1", "origin_record_id": main_a["id"],
            "expected_revision": 1, "shift_days": 10})
        result = self.groups.regroup(SUPERVISOR(), {
            "sources": [{"group_id": group["id"], "expected_revision": 2}],
            "plans": [{"reference": "FAM-M", "members": [
                {"record_id": main_b["id"], "member_role": "main"},
                {"record_id": related["id"], "member_role": "related",
                 "depends_on_record_id": main_b["id"]}]},
                {"reference": "FAM-A", "members": [
                    {"record_id": main_a["id"], "member_role": "main"}]}]})
        merged_id = [g for g in result["groups"] if g["reference"] == "FAM-M"][0]["id"]
        deadlines = self.deadline_map(merged_id)
        # 关联案在新组按新主案重新锚定，期限不再带A的10天顺延
        self.assertEqual(deadlines[main_b["id"]]["deadline_day"], 130)
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 130)
        self.assertEqual(deadlines[related["id"]]["applied_shift_days"], 0)

    def test_merge_two_groups(self):
        main = self.create_case()
        r1 = self.create_case()
        main2 = self.create_case()
        g1 = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": r1["id"], "member_role": "related", "depends_on_record_id": main["id"]}])
        g2 = self.groups.create_group(SUPERVISOR(), "FAM-2", [
            {"record_id": main2["id"], "member_role": "main"}])
        result = self.groups.regroup(SUPERVISOR(), {
            "sources": [{"group_id": g1["id"], "expected_revision": 1},
                        {"group_id": g2["id"], "expected_revision": 1}],
            "plans": [{"reference": "FAM-BIG", "members": [
                {"record_id": main["id"], "member_role": "main"},
                {"record_id": main2["id"], "member_role": "main"},
                {"record_id": r1["id"], "member_role": "related",
                 "depends_on_record_id": main["id"]}]}]})
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(self.groups.get_group(SUPERVISOR(), g1["id"])["status"], "dissolved")
        self.assertEqual(self.groups.get_group(SUPERVISOR(), g2["id"])["status"], "dissolved")

    def test_regroup_rejects_partial_coverage_and_bad_plan(self):
        group, main, related = self.make_group()
        with self.assertRaises(ValidationError):
            self.groups.regroup(SUPERVISOR(), {
                "sources": [{"group_id": group["id"], "expected_revision": 1}],
                "plans": [{"reference": "FAM-N", "members": [
                    {"record_id": main["id"], "member_role": "main"}]}]})
        with self.assertRaises(ValidationError):
            self.groups.regroup(SUPERVISOR(), {
                "sources": [{"group_id": group["id"], "expected_revision": 1}],
                "plans": [{"reference": "FAM-N", "members": [
                    {"record_id": related["id"], "member_role": "related",
                     "depends_on_record_id": main["id"]}]}]})

    def test_concurrent_receipt_and_regroup_loser_parks(self):
        main = self.create_case()
        related = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": related["id"], "member_role": "related",
             "depends_on_record_id": main["id"]}])
        receipt_inside = threading.Event()
        release = threading.Event()

        def hook():
            receipt_inside.set()
            release.wait(timeout=5)

        self.groups.before_receipt_commit = hook
        errors = []

        def submit():
            try:
                self.groups.submit_receipt(OFFICER(), group["id"], {
                    "receipt_key": "RC-CONC", "origin_record_id": main["id"],
                    "expected_revision": 1, "shift_days": 9})
            except Exception as exc:  # pragma: no cover - 断言在主线程
                errors.append(exc)

        t = threading.Thread(target=submit)
        t.start()
        self.assertTrue(receipt_inside.wait(timeout=5))
        # 回执持锁在先；改组在锁外等待，修订号必然落后
        regroup_result = {}

        def regroup():
            regroup_result["value"] = self.groups.regroup(SUPERVISOR(), {
                "sources": [{"group_id": group["id"], "expected_revision": 1}],
                "plans": [{"reference": "FAM-C", "members": [
                    {"record_id": main["id"], "member_role": "main"},
                    {"record_id": related["id"], "member_role": "related",
                     "depends_on_record_id": main["id"]}]}]})

        t2 = threading.Thread(target=regroup)
        t2.start()
        release.set()
        t.join()
        t2.join()
        self.assertFalse(errors)
        self.assertEqual(regroup_result["value"]["status"], "pending_merge")
        # 回执修订生效；改组输入保留
        self.assertEqual(self.groups.get_group(SUPERVISOR(), group["id"])["revision"], 2)
        pending = self.groups.list_pending(SUPERVISOR())
        self.assertEqual(pending[0]["kind"], "regroup")
        # 改组以最新修订确认后执行
        resolved = self.groups.resolve_pending(
            SUPERVISOR(), pending[0]["id"],
            {"sources": [{"group_id": group["id"], "expected_revision": 2}]})
        self.assertEqual(resolved["status"], "created")
        self.assertEqual(self.groups.get_group(SUPERVISOR(), group["id"])["status"], "dissolved")

    def test_concurrent_regroup_first_late_receipt_parks(self):
        main = self.create_case()
        related = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": related["id"], "member_role": "related",
             "depends_on_record_id": main["id"]}])
        inside = threading.Event()
        release = threading.Event()
        self.groups.before_regroup_commit = lambda: (inside.set(), release.wait(timeout=5))
        outcome = {}

        def regroup():
            outcome["regroup"] = self.groups.regroup(SUPERVISOR(), {
                "sources": [{"group_id": group["id"], "expected_revision": 1}],
                "plans": [{"reference": "FAM-C", "members": [
                    {"record_id": main["id"], "member_role": "main"},
                    {"record_id": related["id"], "member_role": "related",
                     "depends_on_record_id": main["id"]}]}]})

        t = threading.Thread(target=regroup)
        t.start()
        self.assertTrue(inside.wait(timeout=5))
        t2 = threading.Thread(target=lambda: outcome.update(
            receipt=self.groups.submit_receipt(OFFICER(), group["id"], {
                "receipt_key": "RC-LATE", "origin_record_id": main["id"],
                "expected_revision": 1, "shift_days": 8})))
        t2.start()
        release.set()
        t.join()
        t2.join()
        self.assertEqual(outcome["regroup"]["status"], "created")
        self.assertEqual(outcome["receipt"]["status"], "pending_merge")
        new_group_id = outcome["regroup"]["groups"][0]["id"]
        parked = self.groups.get_pending(SUPERVISOR(), outcome["receipt"]["pending_merge_id"])
        self.assertEqual(parked["payload"]["reason"], "group_dissolved")
        # 人工合入新组后顺延才生效
        merged = self.groups.resolve_pending(SUPERVISOR(), parked["id"],
                                             {"target_group_id": new_group_id})
        self.assertEqual(merged["status"], "applied")
        deadlines = {d["record_id"]: d for d in
                     self.groups.group_detail(SUPERVISOR(), new_group_id)["deadlines"]}
        self.assertEqual(deadlines[related["id"]]["deadline_day"], 138)

    def test_write_interruption_resumes_from_last_completed_case(self):
        main = self.create_case()
        r1 = self.create_case()
        r2 = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": r1["id"], "member_role": "related", "depends_on_record_id": main["id"]},
            {"record_id": r2["id"], "member_role": "related", "depends_on_record_id": main["id"]}])
        self.groups.crash_after_items = 2
        with self.assertRaises(RuntimeError):
            self.groups.submit_receipt(OFFICER(), group["id"], {
                "receipt_key": "RC-CRASH", "origin_record_id": main["id"],
                "expected_revision": 1, "shift_days": 20})
        # 登记段已提交：修订推进、回执在、任务running、两个案件已完成
        self.assertEqual(self.groups.get_group(SUPERVISOR(), group["id"])["revision"], 2)
        receipt = self.groups.find_receipt("RC-CRASH")
        self.assertEqual(receipt["status"], "pending")
        deadlines = self.deadline_map(group["id"])
        self.assertEqual(deadlines[r1["id"]]["deadline_day"], 150)
        self.assertEqual(deadlines[r2["id"]]["deadline_day"], 130)
        self.assertEqual(deadlines[r2["id"]]["stale"], 1)
        # 新实例重建（模拟重启）：构造服务时自动从最近完成案件之后继续
        rebuilt = build_service(str(Path(self.temp.name) / "test.db"))
        receipt = rebuilt.group_service.find_receipt("RC-CRASH")
        items = rebuilt.group_service.groups.job_items(int(receipt["job_id"]))
        done_order = [it["record_id"] for it in items if it["status"] == "done"]
        self.assertEqual(done_order, [main["id"], r1["id"], r2["id"]])
        # 检查点停在第二个完成案件，续跑从其后继续
        self.assertEqual(
            rebuilt.group_service.groups.get_job(int(receipt["job_id"]))["last_completed_record_id"],
            r2["id"])
        deadlines2 = {d["record_id"]: d for d in
                      rebuilt.group_service.group_detail(SUPERVISOR(), group["id"])["deadlines"]}
        self.assertEqual(deadlines2[main["id"]]["deadline_day"], 150)
        self.assertEqual(deadlines2[r1["id"]]["deadline_day"], 150)  # 已完成者没有二次顺延
        self.assertEqual(deadlines2[r2["id"]]["deadline_day"], 150)
        self.assertEqual(deadlines2[r2["id"]]["stale"], 0)
        self.assertEqual(rebuilt.group_service.find_receipt("RC-CRASH")["status"], "applied")
        # 再次恢复为空操作
        self.assertEqual(rebuilt.group_service.resume_interrupted(SUPERVISOR()), [])

    def test_resume_is_idempotent_on_same_instance(self):
        main = self.create_case()
        r1 = self.create_case()
        r2 = self.create_case()
        group = self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": r1["id"], "member_role": "related", "depends_on_record_id": main["id"]},
            {"record_id": r2["id"], "member_role": "related", "depends_on_record_id": main["id"]}])
        self.groups.crash_after_items = 2
        with self.assertRaises(RuntimeError):
            self.groups.submit_receipt(OFFICER(), group["id"], {
                "receipt_key": "RC-IDEM", "origin_record_id": main["id"],
                "expected_revision": 1, "shift_days": 7})
        # 同一实例上显式重试（重复提交）：已完成的两个案件不重复顺延
        result = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-IDEM", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 7})
        self.assertEqual(result["status"], "applied")
        self.assertEqual([e["record_id"] for e in result["executed"]], [r2["id"]])
        deadlines = self.deadline_map(group["id"])
        for rid in (main["id"], r1["id"], r2["id"]):
            self.assertEqual(deadlines[rid]["applied_shift_days"], 7)
            self.assertEqual(deadlines[rid]["deadline_day"], 137)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.make_group(actor=LEGAL())
        group, main, related = self.make_group(actor=SUPERVISOR())
        with self.assertRaises(PermissionDenied):
            self.groups.regroup(LEGAL(), {
                "sources": [{"group_id": group["id"], "expected_revision": 1}],
                "plans": [{"reference": "FAM-N", "members": [
                    {"record_id": main["id"], "member_role": "main"},
                    {"record_id": related["id"], "member_role": "related",
                     "depends_on_record_id": main["id"]}]}]})

    def test_create_group_validation(self):
        main = self.create_case()
        with self.assertRaises(ValidationError):
            self.groups.create_group(SUPERVISOR(), "BAD", [
                {"record_id": main["id"], "member_role": "related",
                 "depends_on_record_id": 999}])
        with self.assertRaises(ValidationError):
            self.groups.create_group(SUPERVISOR(), "BAD", [
                {"record_id": main["id"], "member_role": "main"},
                {"record_id": main["id"], "member_role": "main"}])
        # 同一案件不能重复入组
        other = self.create_case()
        self.groups.create_group(SUPERVISOR(), "FAM-1", [
            {"record_id": main["id"], "member_role": "main"},
            {"record_id": other["id"], "member_role": "related",
             "depends_on_record_id": main["id"]}])
        third = self.create_case()
        with self.assertRaises(Conflict):
            self.groups.create_group(SUPERVISOR(), "FAM-2", [
                {"record_id": main["id"], "member_role": "main"},
                {"record_id": third["id"], "member_role": "related",
                 "depends_on_record_id": main["id"]}])

    def test_discard_pending(self):
        group, main, related = self.make_group()
        self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-A", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 10})
        parked = self.groups.submit_receipt(OFFICER(), group["id"], {
            "receipt_key": "RC-B", "origin_record_id": main["id"],
            "expected_revision": 1, "shift_days": 5})
        result = self.groups.discard_pending(SUPERVISOR(), parked["pending_merge_id"])
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(self.groups.list_pending(SUPERVISOR()), [])


if __name__ == "__main__":
    unittest.main()
