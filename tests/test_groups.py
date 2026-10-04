import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.group_service import GroupService


def case_data(applicant):
    return {'applicant_id': applicant, 'case_type': 'family', 'received_day': 100,
            'deadline_days': 30, 'response_day': 110, 'representation_active': True,
            'required_documents': ['passport', 'sponsor_letter']}


class GroupTransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "group.db")
        self.service = build_service(self.db_path)
        self.gs: GroupService = self.service.groups

    def tearDown(self):
        self.temp.cleanup()

    def make_case(self, ref, applicant):
        return self.service.create(Actor("creator", "intake_officer"), ref, case_data(applicant))

    def decide(self, record):
        flow = [
            ("submit", "legal_rep", {"documents": ["passport", "sponsor_letter"]}),
            ("request_evidence", "case_officer", {"evidence_request_day": 112, "allowed_days": 10, "evidence_request": "x"}),
            ("respond", "legal_rep", {"response_day": 113, "documents": ["income_proof"]}),
            ("decide", "case_officer", {"decision": "granted", "decision_reason": "ok"}),
        ]
        for action, role, data in flow:
            record = self.service.act(Actor("o", role), record["id"], record["version"], action, data)
        return record

    def make_group(self, refs=("P-1", "D-1", "D-2", "D-3"), key="R-1"):
        principal = self.make_case(refs[0], refs[0])
        group = self.gs.create_group(Actor("officer", "case_officer"), "G",
                                     [{"record_id": principal["id"], "role": "principal"}] + [
                                         {"record_id": self.make_case(ref, ref)["id"], "role": "dependent",
                                          "depends_on_receipt": key}
                                         for ref in refs[1:]
                                     ])
        return group["id"], principal

    # ----------------------------------------------- 1. 选择性失效 + 差异追加

    def test_receipt_only_invalidates_dependent_undecided_and_appends_diff_for_decided(self):
        principal = self.make_case("P-1", "P-1")
        d1 = self.make_case("D-1", "D-1")
        d2 = self.make_case("D-2", "D-2")
        decided = self.decide(self.make_case("D-3", "D-3"))
        unrelated = self.make_case("U-1", "U-1")
        group = self.gs.create_group(Actor("officer", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"},
            {"record_id": d1["id"], "role": "dependent", "depends_on_receipt": "R-1"},
            {"record_id": d2["id"], "role": "dependent", "depends_on_receipt": "R-1"},
            {"record_id": decided["id"], "role": "dependent", "depends_on_receipt": "R-1"},
        ])
        # 无关案件单独建组，不应受 R-1 影响
        ug = self.gs.create_group(Actor("officer", "case_officer"), "UG", [
            {"record_id": unrelated["id"], "role": "principal"}])
        result = self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
            "receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 15, "documents": ["court_notice"]})
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["progress"]["completed"], 4)

        for record_id in (principal["id"], d1["id"], d2["id"]):
            row = self.service.repository.get(record_id)
            self.assertEqual(row["payload"]["deadline_day"], 145)  # 130 + 15
            self.assertTrue(row["payload"]["deadline_valid"])
            self.assertEqual(row["state"], "draft")
        decided_row = self.service.repository.get(decided["id"])
        self.assertEqual(decided_row["payload"]["deadline_day"], 130)  # 期限不变
        self.assertEqual(decided_row["state"], "decided")
        diffs = decided_row["payload"]["deadline_diffs"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["shift_days"], 15)
        self.assertEqual(diffs[0]["mode"] if "mode" in diffs[0] else "diff_only", "diff_only")
        # 无关案组案件不受影响
        self.assertEqual(self.service.repository.get(unrelated["id"])["payload"]["deadline_day"], 130)
        # 审计事件标记失效重算/差异追加
        timeline = self.service.repository.audit_timeline(d1["id"])
        self.assertEqual(timeline[-1]["action"], "deadline_recomputed")
        self.assertTrue(timeline[-1]["details"]["invalidated"])
        self.assertEqual(self.service.repository.audit_timeline(decided["id"])[-1]["action"], "receipt_diff_appended")

    def test_non_dependent_member_not_touched(self):
        principal = self.make_case("P-1", "P-1")
        dep = self.make_case("D-1", "D-1")
        other = self.make_case("O-1", "O-1")
        group = self.gs.create_group(Actor("officer", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"},
            {"record_id": dep["id"], "role": "dependent", "depends_on_receipt": "R-1"},
            {"record_id": other["id"], "role": "dependent", "depends_on_receipt": "R-OTHER"},
        ])
        result = self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
            "receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 7, "documents": ["n"]})
        self.assertEqual(result["progress"]["total"], 2)  # 主案 + 唯一依赖者
        self.assertEqual(self.service.repository.get(other["id"])["payload"]["deadline_day"], 130)

    # ------------------------------------------------- 2. 重复回执幂等

    def test_duplicate_receipt_does_not_extend_twice(self):
        gid, principal = self.make_group()
        data = {"receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
                "shift_days": 15, "documents": ["n"]}
        first = self.gs.submit_receipt(Actor("lr", "legal_rep"), gid, data)
        second = self.gs.submit_receipt(Actor("lr", "legal_rep"), gid, dict(data, shift_days=99))
        self.assertTrue(second["idempotent"])
        row = self.service.repository.get(principal["id"])
        self.assertEqual(row["payload"]["deadline_day"], 145)
        self.assertEqual(row["version"], 2)  # 没有第二次写入

    # --------------------------------------------- 3. 拆分后旧修订回执被拒

    def test_split_creates_new_revision_and_stale_receipt_parks(self):
        gid, principal = self.make_group()
        members = self.gs.list_members(Actor("o", "case_officer"), gid)
        moved = next(m["record_id"] for m in members if m["role"] == "dependent")
        split = self.gs.restructure(Actor("off", "case_officer"), "split", {
            "source_group_id": gid, "target_name": "G2", "target_record_ids": [moved]})
        self.assertEqual(split["status"], "applied")
        self.assertEqual(split["group"]["revision"], 2)
        # 带着旧修订号 1 的回执不能写进修订号 2 的组
        parked = self.gs.submit_receipt(Actor("lr", "legal_rep"), gid, {
            "receipt_key": "R-OLD", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 5, "documents": ["n"]}, base_revision=1)
        self.assertEqual(parked["status"], "parked")
        self.assertEqual(parked["reason"], "stale_revision")
        pending = self.gs.pending(Actor("o", "case_officer"), gid)
        self.assertEqual(len(pending["receipts"]), 1)
        # 案件期限未被触碰
        self.assertEqual(self.service.repository.get(principal["id"])["payload"]["deadline_day"], 130)
        # 显式合组后按当前修订执行
        receipt_id = parked["receipt"]["id"]
        resolved = self.gs.resolve_receipt(Actor("off", "case_officer"), receipt_id)
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(self.service.repository.get(principal["id"])["payload"]["deadline_day"], 135)
        # 已拆出的关联案不在组内，不受影响
        self.assertEqual(self.service.repository.get(moved)["payload"]["deadline_day"], 130)

    def test_cannot_split_principal_or_empty_group(self):
        gid, principal = self.make_group()
        with self.assertRaises(ValidationError):
            self.gs.restructure(Actor("off", "case_officer"), "split", {
                "source_group_id": gid, "target_name": "G2", "target_record_ids": [principal["id"]]})
        members = [m["record_id"] for m in self.gs.list_members(Actor("o", "case_officer"), gid)]
        with self.assertRaises(ValidationError):
            self.gs.restructure(Actor("off", "case_officer"), "split", {
                "source_group_id": gid, "target_name": "G2", "target_record_ids": members})

    def test_merge_old_group_receipt_cannot_write_into_new_group(self):
        gid, principal = self.make_group(refs=("P-1", "D-1", "D-2"))
        p2 = self.make_case("P-2", "P-2")
        g2 = self.gs.create_group(Actor("officer", "case_officer"), "G2X",
                                  [{"record_id": p2["id"], "role": "principal"}])
        # 两主案不能合并
        with self.assertRaises(ValidationError):
            self.gs.restructure(Actor("off", "case_officer"), "merge", {
                "source_group_id": gid, "target_group_id": g2["id"]})
        # 构造一个无主案的目标组：先把含主案组并入一个纯关联组不可行，改为拆分得到无主案组
        members = self.gs.list_members(Actor("o", "case_officer"), gid)
        dep_ids = [m["record_id"] for m in members if m["role"] == "dependent"]
        split = self.gs.restructure(Actor("off", "case_officer"), "split", {
            "source_group_id": gid, "target_name": "DEP-GROUP", "target_record_ids": dep_ids})
        target_id = split["target_group_id"]
        # 把只有一个关联案的组合并进含主案的组（源组无主案）
        lone = self.make_case("L-1", "L-1")
        lone_group = self.gs.create_group(Actor("off", "case_officer"), "LONE", [
            {"record_id": lone["id"], "role": "principal"}])
        merge = self.gs.restructure(Actor("off", "case_officer"), "merge", {
            "source_group_id": target_id, "target_group_id": lone_group["id"]})
        self.assertEqual(merge["status"], "applied")
        self.assertEqual(self.gs.get_group(Actor("o", "case_officer"), target_id)["status"], "merged")
        remaining = [m["record_id"] for m in self.gs.list_members(Actor("o", "case_officer"), target_id)]
        # 合并后该组已无成员（成员全部并入新组），针对新组中旧成员的旧修订回执不能写进新组
        self.assertEqual(remaining, [])
        parked = self.gs.submit_receipt(Actor("lr", "legal_rep"), target_id, {
            "receipt_key": "R-MERGE-OLD", "anchor_record_id": dep_ids[0], "kind": "reschedule",
            "shift_days": 9, "documents": ["n"]}, base_revision=1)
        self.assertEqual(parked["status"], "parked")
        self.assertEqual(parked["reason"], "group_merged")
        # 案件期限没有被改动
        self.assertEqual(self.service.repository.get(dep_ids[0])["payload"]["deadline_day"], 130)

    # ------------------------------------------ 4. 回执与改组同时提交：先到生效

    def test_concurrent_restructure_and_receipt_first_writer_wins(self):
        gid, principal = self.make_group()
        # 客户端 A 先提交改组（修订 1 -> 2，op_seq 0 -> 1）
        members = self.gs.list_members(Actor("o", "case_officer"), gid)
        moved = [m["record_id"] for m in members if m["role"] == "dependent"][0]
        self.gs.restructure(Actor("off", "case_officer"), "split", {
            "source_group_id": gid, "target_name": "G2", "target_record_ids": [moved]})
        # 客户端 B 拿着旧水位线（op_seq=0）后到的回执：保留输入，停在待合组
        late = self.gs.submit_receipt(Actor("lr", "legal_rep"), gid, {
            "receipt_key": "R-LATE", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 11, "documents": ["n"]}, base_revision=2, base_op_seq=0)
        self.assertEqual(late["status"], "parked")
        self.assertEqual(late["reason"], "concurrent_write")
        self.assertEqual(self.service.repository.get(principal["id"])["payload"]["deadline_day"], 130)
        # 反方向：回执先到（op_seq 推进），后到的改组停在待合组
        gid2, principal2 = self.make_group(refs=("Q-1", "E-1", "E-2"))
        self.gs.submit_receipt(Actor("lr", "legal_rep"), gid2, {
            "receipt_key": "R-1", "anchor_record_id": principal2["id"], "kind": "reschedule",
            "shift_days": 3, "documents": ["n"]})
        dep = [m["record_id"] for m in self.gs.list_members(Actor("o", "case_officer"), gid2)
               if m["role"] == "dependent"][0]
        late_split = self.gs.restructure(Actor("off", "case_officer"), "split", {
            "source_group_id": gid2, "target_name": "G3", "target_record_ids": [dep]},
            base_revision=1, base_op_seq=0)
        self.assertEqual(late_split["status"], "parked")
        pending = self.gs.pending(Actor("o", "case_officer"), gid2)
        self.assertEqual(len(pending["change_requests"]), 1)
        # 改组请求可在水位线对齐后显式合组执行
        resolved = self.gs.resolve_change(Actor("off", "case_officer"), pending["change_requests"][0]["id"])
        self.assertEqual(resolved["status"], "applied")

    # --------------------------------------------- 5. 写入中断后从断点续算

    def test_interrupted_transaction_resumes_from_last_completed_case(self):
        principal = self.make_case("P-1", "P-1")
        deps = [self.make_case("D-%d" % i, "D-%d" % i) for i in (1, 2, 3, 4)]
        all_cases = [principal] + deps
        group = self.gs.create_group(Actor("officer", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"}] + [
            {"record_id": d["id"], "role": "dependent", "depends_on_receipt": "R-1"} for d in deps])

        calls = {"n": 0}
        crash_at = 3  # 第三个案件处理时中断

        def observer(record_id, mode):
            calls["n"] += 1
            if calls["n"] == crash_at:
                raise RuntimeError("simulated write interruption")

        self.gs.step_observer = observer
        with self.assertRaises(RuntimeError):
            self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
                "receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
                "shift_days": 20, "documents": ["n"]})

        # 头事务与前两个案件已落检查点；活动事务仍挂在组上
        g = self.gs.get_group(Actor("o", "case_officer"), group["id"])
        tx_id = g["active_tx_id"]
        self.assertIsNotNone(tx_id)
        progress = self.gs.tx_progress(Actor("o", "case_officer"), tx_id)
        self.assertEqual(progress["completed"], 2)
        self.assertEqual(progress["total"], 5)
        self.assertEqual(self.service.repository.get(all_cases[0]["id"])["payload"]["deadline_day"], 150)
        self.assertEqual(self.service.repository.get(all_cases[1]["id"])["payload"]["deadline_day"], 150)
        self.assertEqual(self.service.repository.get(all_cases[2]["id"])["payload"]["deadline_day"], 130)

        # 活动事务期间，后到回执只能停在待合组
        parked = self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
            "receipt_key": "R-BUSY", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 2, "documents": ["n"]})
        self.assertEqual(parked["status"], "parked")
        self.assertEqual(parked["reason"], "active_transaction")

        # 中断恢复：从最近完成案件继续；重复提交同一回执触发续算而非重放
        self.gs.step_observer = None
        resumed = self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
            "receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 20, "documents": ["n"]})
        self.assertEqual(resumed["status"], "resumed")
        progress = resumed["progress"]
        self.assertEqual(progress["completed"], 5)
        for case in all_cases:
            self.assertEqual(self.service.repository.get(case["id"])["payload"]["deadline_day"], 150)
            # 每案只顺延一次：version 恰好 +1
            self.assertEqual(self.service.repository.get(case["id"])["version"], 2)
        self.assertIsNone(self.gs.get_group(Actor("o", "case_officer"), group["id"])["active_tx_id"])

    def test_resume_endpoint_and_restart_recovery(self):
        principal = self.make_case("P-1", "P-1")
        dep = self.make_case("D-1", "D-1")
        group = self.gs.create_group(Actor("officer", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"},
            {"record_id": dep["id"], "role": "dependent", "depends_on_receipt": "R-9"}])
        calls = {"n": 0}

        def observer(record_id, mode):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")

        self.gs.step_observer = observer
        with self.assertRaises(RuntimeError):
            self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
                "receipt_key": "R-9", "anchor_record_id": principal["id"], "kind": "supplement",
                "shift_days": 10, "documents": ["n"]})
        tx_id = self.gs.get_group(Actor("o", "case_officer"), group["id"])["active_tx_id"]
        # 显式续算
        self.gs.step_observer = None
        result = self.gs.resume(Actor("lr", "legal_rep"), tx_id)
        self.assertEqual(result["progress"]["completed"], 2)
        # 重新构建服务（模拟进程重启）应自动恢复，且无残留活动事务
        rebuilt = build_service(self.db_path)
        self.assertIsNone(rebuilt.groups.get_group(Actor("o", "case_officer"), group["id"])["active_tx_id"])
        self.assertEqual(rebuilt.repository.get(dep["id"])["payload"]["deadline_day"], 140)
        # 再次重启是幂等空操作
        self.assertEqual(build_service(self.db_path).groups.recover_on_startup(Actor("s", "admin")), [])

    # ------------------------------------------------------- 权限与校验

    def test_permissions_and_validation(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.gs.create_group(Actor("x", "legal_rep"), "G", [])
        principal = self.make_case("P-1", "P-1")
        with self.assertRaises(ValidationError):
            # 缺少主案
            self.gs.create_group(Actor("o", "case_officer"), "G", [
                {"record_id": principal["id"], "role": "dependent", "depends_on_receipt": "R-1"}])
        with self.assertRaises(ValidationError):
            # 关联案未声明依赖回执
            self.gs.create_group(Actor("o", "case_officer"), "G", [
                {"record_id": principal["id"], "role": "principal"},
                {"record_id": self.make_case("D-X", "D-X")["id"], "role": "dependent"}])
        with self.assertRaises(PermissionDenied):
            # legal_rep 不能改组
            self.gs.restructure(Actor("x", "legal_rep"), "split", {
                "source_group_id": 999, "target_name": "X", "target_record_ids": [1]})

    def test_duplicate_group_membership_rejected(self):
        principal = self.make_case("P-1", "P-1")
        self.gs.create_group(Actor("o", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"}])
        with self.assertRaises(Conflict):
            # 案件已属于其他案组
            self.gs.create_group(Actor("o", "case_officer"), "G2", [
                {"record_id": principal["id"], "role": "principal"}])

    def test_closed_case_only_appends_diff_and_merge_bumps_target_revision(self):
        principal = self.make_case("P-1", "P-1")
        closed = self.decide(self.make_case("D-C", "D-C"))
        closed = self.service.act(Actor("s", "supervisor"), closed["id"], closed["version"], "close",
                                  {"closure_note": "归档"})
        group = self.gs.create_group(Actor("officer", "case_officer"), "G", [
            {"record_id": principal["id"], "role": "principal"},
            {"record_id": closed["id"], "role": "dependent", "depends_on_receipt": "R-1"}])
        self.gs.submit_receipt(Actor("lr", "legal_rep"), group["id"], {
            "receipt_key": "R-1", "anchor_record_id": principal["id"], "kind": "reschedule",
            "shift_days": 8, "documents": ["n"]})
        closed_row = self.service.repository.get(closed["id"])
        self.assertEqual(closed_row["state"], "closed")
        self.assertEqual(closed_row["payload"]["deadline_day"], 130)
        self.assertEqual(len(closed_row["payload"]["deadline_diffs"]), 1)

        # 拆分出无主案关联组，再并入一个含主案的新组，目标组修订号递增
        members = self.gs.list_members(Actor("o", "case_officer"), group["id"])
        split = self.gs.restructure(Actor("off", "case_officer"), "split", {
            "source_group_id": group["id"], "target_name": "DEP",
            "target_record_ids": [closed["id"]]})
        target = self.make_case("H-1", "H-1")
        target_group = self.gs.create_group(Actor("off", "case_officer"), "HOME",
                                            [{"record_id": target["id"], "role": "principal"}])
        merge = self.gs.restructure(Actor("off", "case_officer"), "merge", {
            "source_group_id": split["target_group_id"], "target_group_id": target_group["id"]})
        self.assertEqual(merge["status"], "applied")
        self.assertEqual(merge["target_group"]["revision"], 2)
        self.assertEqual(merge["group"]["status"], "merged")


if __name__ == "__main__":
    unittest.main()
