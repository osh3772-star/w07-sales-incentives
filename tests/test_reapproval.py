import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ai_drafts import adopt_draft, generate_draft, list_drafts
from approvals import decide_approval, list_approvals, list_cases, reply_case, request_case, submit_approval
from corrections import apply_correction, list_tasks
from payments import confirm_account, list_payments, send_mock
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


JUNE = "2026-06"
SOURCE = Path(__file__).resolve().parents[1] / "01_참가자_배포용/04_원본데이터/01_corevia"
STAFF, LEAD, EXEC, TREASURY = [USERS[r] for r in ("staff", "lead", "executive", "treasury")]


def mock_writer(kind, context):
    return {"text": f"시험 초안 {kind} {context['monthly_total_krw']}", "provider": "test-stub", "model": "mock"}


class ReapprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "corrections.db"
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE)
        receive_month(self.db, JUNE)

    def merge(self):
        ref = self.db.execute("SELECT representative_name, address FROM agencies WHERE agency_id='D-011'").fetchone()
        return apply_correction(self.db, STAFF, "D-001", dict(ref), "2026-06-01",
                                "대표자·주소 정정 시험", "모의 등록 증빙")

    def approve(self, group, actor):
        approval_id = submit_approval(self.db, STAFF, JUNE, group, "6월 단위 상신")
        decide_approval(self.db, actor, approval_id, "approve", "6월 결재 근거 확인")
        return approval_id

    def test_merged_approval_preserves_snapshots_and_adopted_memos(self):
        memo = generate_draft(self.db, STAFF, JUNE, "D-001", "summary", provider=mock_writer)
        adopt_draft(self.db, LEAD, memo, "단독 그룹 근거 검토 완료")
        first = self.approve("D-001", LEAD)
        first_order = list_payments(self.db, JUNE)["orders"][0]
        order_id = first_order["instruction_id"]
        confirm_account(self.db, TREASURY, order_id, "모의은행 111-111", "시험 수취 확인")
        self.assertEqual(list_payments(self.db, JUNE)["orders"][0]["queue_state"], "ready")
        old_basis = tuple(self.db.execute(
            "SELECT snapshot, decision_snapshot, reason FROM approvals WHERE approval_id=?", (first,)).fetchone())
        case = request_case(self.db, STAFF, "RB-00120", "공동 회수 근거 확인", required=False)
        old_proposal = generate_draft(self.db, STAFF, JUNE, "D-011", "proposal", provider=mock_writer)
        pending = submit_approval(self.db, STAFF, JUNE, "D-011", "정정 전 그룹 상신")
        original_drafts = self.db.execute("SELECT * FROM ai_drafts ORDER BY draft_id").fetchall()
        correction = self.merge()
        self.assertEqual(list_payments(self.db, JUNE)["orders"][0]["queue_state"], "reapproval")
        with self.assertRaisesRegex(ValueError, "재승인"):
            send_mock(self.db, TREASURY, order_id)
        with self.assertRaisesRegex(ValueError, "대체"):
            decide_approval(self.db, EXEC, pending, "approve", "정정 전 버전")
        with self.assertRaisesRegex(ValueError, "바뀌었"):
            adopt_draft(self.db, STAFF, old_proposal, "기존 안 채택")
        [linked_case] = list_cases(self.db, JUNE)
        self.assertEqual((linked_case["case_id"], linked_case["group_id"], linked_case["current_group_id"]),
                         (case, "D-011", "D-001"))
        drafts = list_drafts(self.db, JUNE, "D-001")
        self.assertEqual([d["draft_id"] for d in drafts], [old_proposal, memo])
        self.assertTrue(all(d["review_required"] and d["correction_ids"] == [correction] for d in drafts))
        self.assertEqual(original_drafts, self.db.execute("SELECT * FROM ai_drafts ORDER BY draft_id").fetchall())
        [legacy, approved] = list_approvals(self.db, JUNE)
        self.assertEqual((legacy["status"], legacy["can_resubmit"], legacy["visible_groups"]),
                         ("정정 재검토", False, ["D-001", "D-011"]))
        self.assertEqual(approved["status"], "승인")
        self.assertTrue(approved["review_required"])

        proposal = generate_draft(self.db, STAFF, JUNE, "D-001", "proposal", provider=mock_writer)
        packet = json.loads(list_drafts(self.db, JUNE, "D-001")[0]["evidence_json"])
        self.assertEqual(packet["cases"][0]["case_id"], case)
        self.assertEqual((packet["monthly_total_krw"], packet["new_amount_krw"], packet["reapproval_amount_krw"]),
                         (4_877_029, 3_888_097, 988_932))
        self.assertEqual(packet["review_memos"], [])
        self.assertEqual(packet["registration_changes"][0]["changed_fields"], ["address", "representative_name"])
        revised = adopt_draft(self.db, STAFF, proposal, "정정 후 미집행분 재승인 및 세 대리점 신규")["approval_id"]
        evidence = list_approvals(self.db, JUNE)[0]["snapshot"]
        self.assertEqual(evidence["authority"], "영업임원")
        self.assertEqual(evidence["members"], ["D-001", "D-011", "D-024", "D-037"])
        self.assertEqual((evidence["monthly_total"], evidence["new_amount"], evidence["reapproval_amount"]),
                         (4_877_029, 3_888_097, 988_932))
        self.assertNotIn("review_memos", evidence)
        self.assertEqual(len(evidence["reapproval_task_ids"]), 1)
        self.assertEqual(evidence["master"][0]["representative_name"], evidence["master"][1]["representative_name"])
        with self.assertRaises(PermissionError):
            decide_approval(self.db, LEAD, revised, "approve", "권한 검사")
        decide_approval(self.db, EXEC, revised, "approve", "변경된 누계 검토")
        with closing(open_db(self.path)) as reopened:
            self.assertEqual(old_basis, tuple(reopened.execute(
                "SELECT snapshot,decision_snapshot,reason FROM approvals WHERE approval_id=?", (first,)).fetchone()))
            orders = list_payments(reopened, JUNE)["orders"]
            self.assertEqual(len(orders), 4)
            retained = next(o for o in orders if o["instruction_id"] == order_id)
            self.assertEqual((retained["queue_state"], retained["approval_id"], retained["effective_approval_id"]),
                             ("ready", first, revised))
            [task] = list_tasks(reopened, JUNE)
            self.assertEqual((task["status"], task["reapproval_id"], task["resolved_by"]),
                             ("done", revised, EXEC.user_id))

    def test_reapproval_without_new_lines_reject_supplement_and_repeat_correction(self):
        self.approve("D-001", LEAD)
        self.approve("D-011", EXEC)
        originals = [o["instruction_id"] for o in list_payments(self.db, JUNE)["orders"]]
        self.merge()
        first = submit_approval(self.db, STAFF, JUNE, "D-001", "네 지급지시 재심사")
        apply_correction(self.db, STAFF, "D-001", {"address": "모의 단독 주소"}, "2026-06-02",
                         "그룹 재분리", "원본 대조 시험")
        with self.assertRaises(ValueError):
            decide_approval(self.db, EXEC, first, "approve", "이전 병합 그룹")
        single = submit_approval(self.db, STAFF, JUNE, "D-001", "재분리 후 재승인")
        single_doc = list_approvals(self.db, JUNE)[0]
        self.assertEqual((single_doc["new_amount"], single_doc["monthly_total"],
                          single_doc["snapshot"]["reapproval_amount"]), (0, 988_932, 988_932))
        self.assertEqual(len(single_doc["snapshot"]["reapproval_task_ids"]), 2)
        decide_approval(self.db, LEAD, single, "reject", "보완 필요")
        self.assertTrue(all(t["status"] == "open" for t in list_tasks(self.db, JUNE)))
        with self.assertRaisesRegex(ValueError, "반려"):
            submit_approval(self.db, STAFF, JUNE, "D-001", "이력 없는 상신")
        corrected = submit_approval(self.db, STAFF, JUNE, "D-001", "수정 후 재상신", previous_id=single)
        decide_approval(self.db, LEAD, corrected, "supplement", "재검토 필요")
        final = submit_approval(self.db, STAFF, JUNE, "D-001", "소명 확인", previous_id=corrected)
        decide_approval(self.db, LEAD, final, "approve", "검토 완료")
        other = submit_approval(self.db, STAFF, JUNE, "D-011", "나머지 세 대리점 재승인")
        others = list_approvals(self.db, JUNE)[0]
        self.assertEqual((others["new_amount"], others["snapshot"]["reapproval_amount"]), (0, 3_888_097))
        self.assertEqual(len(others["snapshot"]["reapproval_task_ids"]), 6)
        decide_approval(self.db, EXEC, other, "approve", "검토 완료")
        self.assertTrue(all(t["status"] == "done" for t in list_tasks(self.db, JUNE)))
        self.assertEqual(originals, [o["instruction_id"] for o in list_payments(self.db, JUNE)["orders"]])

    def test_late_ai_result_is_saved_and_flagged_in_same_transaction(self):
        def writer(kind, packet):
            with closing(open_db(self.path)) as other:
                ref = other.execute("SELECT representative_name,address FROM agencies WHERE agency_id='D-011'").fetchone()
                apply_correction(other, STAFF, "D-001", dict(ref), "2026-06-01", "응답 대기 중 정정", "모의 증빙")
            return mock_writer(kind, packet)

        draft = generate_draft(self.db, STAFF, JUNE, "D-011", "summary", provider=writer)
        [row] = list_drafts(self.db, JUNE, "D-001")
        self.assertEqual((row["draft_id"], row["review_required"], row["original_text"]),
                         (draft, True, "시험 초안 summary 3888097"))
        self.assertEqual(json.loads(row["evidence_json"])["members"], ["D-011", "D-024", "D-037"])
        with self.assertRaisesRegex(ValueError, "정정"):
            adopt_draft(self.db, STAFF, draft, "이전 입력으로 작성된 요약")

    def test_source_approved_unpaid_is_reviewed_without_creating_an_order(self):
        # The source ledger may already record approval, with no locally authorized instruction.
        self.db.execute("UPDATE rebates SET approval_status='승인',approver='모의 전결자' WHERE rebate_id='RB-00006'")
        apply_correction(self.db, STAFF, "D-001", {"representative_name": "시험대표자"}, "2026-06-01",
                         "단독 대리점 대표자 변경", "시험용 등기")
        case = request_case(self.db, STAFF, "RB-00006", "현재 대표자 확인", required=False)
        reply_case(self.db, STAFF, case, "모의 회신인", "시험대표자 변경을 확인함")
        draft = generate_draft(self.db, STAFF, JUNE, "D-001", "proposal", provider=mock_writer)
        packet = json.loads(list_drafts(self.db, JUNE, "D-001")[0]["evidence_json"])
        self.assertEqual(packet["cases"][0]["answer"], "[비공개] 변경을 확인함")
        approval = adopt_draft(self.db, STAFF, draft, "기존 원장 승인 기록 재검토")["approval_id"]
        decide_approval(self.db, LEAD, approval, "approve", "정정 후 확인")
        self.assertFalse(list_payments(self.db, JUNE)["orders"])
        [task] = list_tasks(self.db, JUNE)
        self.assertEqual((task["status"], task["instruction_id"], task["reapproval_id"]), ("done", None, approval))

    def test_split_rejected_group_can_reapply_each_portion(self):
        self.merge()
        reject = submit_approval(self.db, STAFF, JUNE, "D-001", "병합 후 최초 상신")
        decide_approval(self.db, EXEC, reject, "reject", "소명 요청")
        apply_correction(self.db, STAFF, "D-001", {"address": "분리 주소"}, "2026-06-02",
                         "분리 적용", "모의 등기")
        for group, actor, amount in (("D-001", LEAD, 988_932), ("D-011", EXEC, 3_888_097)):
            doc = next(r for r in list_approvals(self.db, JUNE) if r["approval_id"] == reject)
            self.assertIn(group, doc["resubmittable_groups"])
            fresh = submit_approval(self.db, STAFF, JUNE, group, "정정 근거 보완", previous_id=reject)
            self.assertEqual(list_approvals(self.db, JUNE)[0]["new_amount"], amount)
            decide_approval(self.db, actor, fresh, "approve", "그룹 단위 승인")
        self.assertEqual(len(list_payments(self.db, JUNE)["orders"]), 4)
