import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from approvals import decide_approval, list_approvals, list_cases, reply_case, request_case, resolve_case, submit_approval
from rules import month_overview
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


JUNE = "2026-06"
SOURCE = Path(__file__).resolve().parents[1] / "01_참가자_배포용/04_원본데이터/01_corevia"
STAFF, LEAD, EXEC, TREASURY = [USERS[r] for r in ("staff", "lead", "executive", "treasury")]


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "workflow.db"
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE)
        receive_month(self.db, JUNE)

    def group(self, group_id):
        return next(g for g in month_overview(self.db, JUNE)["groups"] if g["group_id"] == group_id)

    def add_new_rebate(self, amount):
        self.db.execute(
            """INSERT INTO rebates(rebate_id, agency_id, period_ym, sales_performance_krw,
               rebate_amount_krw,approval_status,approver,payment_date)
               VALUES('DEMO-NEW','D-001',?,15000000,?,'미지급','NONE','NONE')""", (JUNE, amount),
        )

    def test_executive_group_requires_clearance_and_stores_decision_evidence(self):
        case_id = request_case(self.db, STAFF, "RB-00120", "등록·회수계좌 차이 설명", due_date="2026-07-15")
        with self.assertRaises(ValueError):
            submit_approval(self.db, STAFF, JUNE, "D-011", "필수 소명 대기")
        with self.assertRaises(PermissionError):
            reply_case(self.db, TREASURY, case_id, "대리점 담당", "공동 회수계좌")
        with self.assertRaises(ValueError):
            resolve_case(self.db, STAFF, case_id, "아직 회신 없음")
        reply_case(self.db, STAFF, case_id, "대리점 담당", "공동 회수계좌", "모의 소명서 1")
        resolve_case(self.db, LEAD, case_id, "회수 위탁 소명 확인")
        approval_id = submit_approval(self.db, STAFF, JUNE, "D-011", "세 대리점 합산")
        for actor in [STAFF, LEAD, TREASURY]:
            for action in ["approve", "reject", "supplement"]:
                with self.assertRaises(PermissionError):
                    decide_approval(self.db, actor, approval_id, action, "권한 확인")
        decide_approval(self.db, EXEC, approval_id, "approve", "월 누계와 소명 확인")
        with closing(open_db(self.path)) as reopened:
            [saved] = list_approvals(reopened, JUNE)
            [case] = list_cases(reopened, JUNE)
            self.assertEqual((saved["monthly_total"], saved["new_amount"]), (3_888_097, 3_888_097))
            self.assertEqual((saved["status"], saved["decided_by"], saved["decided_role"]),
                             ("승인", EXEC.user_id, EXEC.role))
            self.assertEqual(saved["reason"], "월 누계와 소명 확인")
            self.assertTrue(saved["decided_at"])
            evidence = saved["decision_snapshot"]
            self.assertEqual(evidence["cases"], [case])
            self.assertEqual(evidence["members"], ["D-011", "D-024", "D-037"])
            self.assertEqual(len(evidence["edges"]), 2)
            self.assertEqual(len(evidence["master"]), 3)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 3)

    def test_decision_retries_and_reference_lines_never_issue_twice(self):
        first = submit_approval(self.db, STAFF, JUNE, "D-001", "전결 요청")
        def approve(_):
            with closing(open_db(self.path)) as other:
                return decide_approval(other, LEAD, first, "approve", "전결 승인")

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(approve, range(2))), [first, first])
        self.assertEqual(self.group("D-001")["approved_amount"], 988_932)
        self.add_new_rebate(1_100_000)
        receive_month(self.db, JUNE)
        second = submit_approval(self.db, STAFF, JUNE, "D-001", "추가 접수 건")
        [second_saved, first_saved] = list_approvals(self.db, JUNE)
        self.assertEqual(second_saved["monthly_total"], 2_088_932)
        self.assertEqual(second_saved["new_amount"], 1_100_000)
        self.assertEqual(first_saved["monthly_total"], 988_932)
        self.assertEqual(second_saved["authority"], "영업임원")
        with self.assertRaises(PermissionError):
            decide_approval(self.db, LEAD, second, "approve", "한도 초과")
        decide_approval(self.db, EXEC, second, "approve", "추가 금액 승인")
        orders = [tuple(row) for row in self.db.execute(
            "SELECT approval_id, rebate_id FROM payment_instructions ORDER BY approval_id"
        )]
        self.assertEqual(orders, [(first, "RB-00006"), (second, "DEMO-NEW")])

    def test_reject_then_supplement_and_reapply(self):
        first = submit_approval(self.db, STAFF, JUNE, "D-001", "접수 서류")
        decide_approval(self.db, LEAD, first, "reject", "증빙 누락")
        self.assertEqual(self.group("D-001")["total"], 0)
        with self.assertRaises(ValueError):
            submit_approval(self.db, STAFF, JUNE, "D-001", "명시적 재상신 필요")
        second = submit_approval(self.db, STAFF, JUNE, "D-001", "증빙 첨부", previous_id=first)
        decide_approval(self.db, LEAD, second, "supplement", "소명 추가")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 0)
        third = submit_approval(self.db, STAFF, JUNE, "D-001", "보완 확인", previous_id=second)
        decide_approval(self.db, LEAD, third, "approve", "승인")
        saved = list_approvals(self.db, JUNE)
        self.assertEqual([(s["status"], s["version"], s["previous_id"]) for s in saved],
                         [("승인", 3, second), ("보완 요청", 2, first), ("반려", 1, None)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 1)

    def test_new_ingestion_invalidates_pending_decision_and_changes_authority(self):
        first = submit_approval(self.db, STAFF, JUNE, "D-001", "최초 상신")
        self.add_new_rebate(1_100_000)
        with self.assertRaises(ValueError):
            decide_approval(self.db, LEAD, first, "approve", "미접수 추가 건 존재")
        receive_month(self.db, JUNE)
        with self.assertRaises(PermissionError):
            decide_approval(self.db, LEAD, first, "approve", "결재권자 변경")
        second = submit_approval(self.db, STAFF, JUNE, "D-001", "누계와 경로 변경", previous_id=first)
        decide_approval(self.db, EXEC, second, "approve", "임원 승인")
        self.assertEqual([a["status"] for a in list_approvals(self.db, JUNE)], ["승인","재상신"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 2)
