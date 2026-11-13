import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import SOURCE_ROOT
from approvals import decide_approval, submit_approval
from corrections import apply_correction, complete_post_review
from payments import confirm_account, list_payments, record_reply, send_mock
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


MONTH = "2026-06"
REBATE = "RB-00006"
AMOUNT = 988_932
ACCOUNT = "모의은행 001-999"
STAFF, LEAD, EXEC, TREASURY = (USERS[key] for key in ("staff", "lead", "executive", "treasury"))


class CorrectionPaymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "corrected.sqlite3"
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE_ROOT)
        receive_month(self.db, MONTH)
        self.original = submit_approval(self.db, STAFF, MONTH, "D-001", "6월 신규")
        decide_approval(self.db, LEAD, self.original, "approve", "988,932원 팀 전결")
        self.iid = self.order()["instruction_id"]
        confirm_account(self.db, TREASURY, self.iid, ACCOUNT, "시험 수취계좌 확인 증빙")

    def order(self, db=None):
        return next(order for order in list_payments(db or self.db, MONTH)["orders"]
                    if order["rebate_id"] == REBATE)

    def merge(self):
        other = self.db.execute(
            "SELECT representative_name, address FROM agencies WHERE agency_id='D-011'"
        ).fetchone()
        return apply_correction(self.db, STAFF, "D-001", dict(other), "2026-06-01",
                                "시험 등록정보 정정", "시험 확인서")

    def test_sent_waits_existing_result_and_creates_post_review(self):
        attempt = send_mock(self.db, TREASURY, self.iid)
        self.merge()
        self.assertEqual((self.order()["state"], self.order()["queue_state"]), ("sent", "bank_check"))
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.iid)
        self.assertEqual(record_reply(self.db, TREASURY, "MISMATCH", attempt, 17, ACCOUNT, "success"), "review")
        self.assertEqual(self.order()["queue_state"], "bank_check")
        with closing(open_db(self.path)) as restarted:
            with self.assertRaises(ValueError):
                send_mock(restarted, TREASURY, self.iid, attempt)
            for _ in range(2):
                self.assertEqual(record_reply(restarted, TREASURY, "OK", attempt, AMOUNT, ACCOUNT, "success"), "matched")
            paid = self.order(restarted)
            self.assertEqual((paid["instruction_id"], paid["approval_id"], paid["state"]), (self.iid, self.original, "completed"))
            self.assertTrue(paid["post_review"])
            self.assertEqual((len(paid["attempts"]), len(paid["replies"])), (1, 2))
            todo, = [t for t in paid["correction_tasks"] if t["status"] == "open"]
            self.assertEqual(todo["kind"], "post_review")
            complete_post_review(restarted, TREASURY, todo["task_id"], "원 지시와 일치하는 모의 성공 회신 확인")
        self.assertFalse(self.order()["post_review"])
        self.assertEqual(self.order()["state"], "completed")

    def test_sent_failure_needs_reapproval_before_retry_of_same_instruction(self):
        first = send_mock(self.db, TREASURY, self.iid)
        self.merge()
        record_reply(self.db, TREASURY, "FAIL", first, AMOUNT, ACCOUNT, "failure")
        self.assertEqual((self.order()["state"], self.order()["queue_state"]), ("failed", "reapproval"))
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.iid, first)
        revised = submit_approval(self.db, STAFF, MONTH, "D-001", "정정 후 근거 확인 및 임원 재상신")
        with self.assertRaises(PermissionError):
            decide_approval(self.db, LEAD, revised, "approve", "팀 권한 시도")
        decide_approval(self.db, EXEC, revised, "approve", "4,877,029원 합산과 실패 회신 검토")
        order = self.order()
        self.assertEqual((order["approval_id"], order["effective_approval_id"], order["queue_state"]),
                         (self.original, revised, "failed"))
        second = send_mock(self.db, TREASURY, self.iid, first)
        self.assertNotEqual(second, first)
        self.assertEqual(send_mock(self.db, TREASURY, self.iid, first), second)
        self.assertEqual(record_reply(self.db, TREASURY, "FINAL", second, AMOUNT, ACCOUNT, "success"), "matched")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions WHERE rebate_id=?", (REBATE,)).fetchone()[0], 1)

    def test_completed_order_stays_identical_and_only_post_review_is_open(self):
        attempt = send_mock(self.db, TREASURY, self.iid)
        record_reply(self.db, TREASURY, "PAID", attempt, AMOUNT, ACCOUNT, "success")
        before = self.order()
        self.merge()
        after = self.order()
        for field in ("instruction_id", "approval_id", "amount_krw", "state", "account", "attempts", "replies"):
            self.assertEqual(before[field], after[field])
        self.assertEqual(after["queue_state"], "completed")
        task, = [t for t in after["correction_tasks"] if t["status"] == "open"]
        self.assertEqual(task["kind"], "post_review")
        self.assertEqual(send_mock(self.db, TREASURY, self.iid), attempt)
        self.assertEqual(len(self.order()["attempts"]), 1)

    def test_registered_account_correction_requires_fresh_treasury_evidence(self):
        new_account = "모의은행 200-333"
        apply_correction(self.db, STAFF, "D-001", {"bank_account_no": new_account},
                         "2026-06-01", "등록계좌 변경", "대리점 등록계좌 변경 신청")
        with self.assertRaises(ValueError):
            confirm_account(self.db, TREASURY, self.iid, new_account, "재승인 전 변경 시도")
        revised = submit_approval(self.db, STAFF, MONTH, "D-001", "변경계좌와 금액 확인")
        decide_approval(self.db, LEAD, revised, "approve", "등록계좌 변경 재승인")
        self.assertEqual(self.order()["queue_state"], "account_recheck")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.iid)
        with self.assertRaises(PermissionError):
            confirm_account(self.db, STAFF, self.iid, new_account, "담당자 증빙")
        with closing(open_db(self.path)) as restarted:
            confirm_account(restarted, TREASURY, self.iid, new_account, "모의 계좌 소유 확인서")
            confirm_account(restarted, TREASURY, self.iid, new_account, "모의 계좌 소유 확인서")
            order = self.order(restarted)
            self.assertEqual((order["queue_state"], order["account"]), ("ready", new_account))
            evidence, = order["account_rechecks"]
            self.assertEqual((evidence["previous_account"], evidence["previous_evidence"]),
                             (ACCOUNT, "시험 수취계좌 확인 증빙"))
            self.assertEqual((evidence["previous_verified_by"], evidence["verified_by"]),
                             (TREASURY.user_id, TREASURY.user_id))
            send_mock(restarted, TREASURY, self.iid)
            self.assertEqual(self.order(restarted)["attempts"][0]["account"], new_account)

    def test_failed_attempt_keeps_original_account_after_treasury_recheck(self):
        new_account = "모의은행 200-333"
        first = send_mock(self.db, TREASURY, self.iid)
        apply_correction(self.db, STAFF, "D-001", {"bank_account_no": new_account},
                         "2026-06-01", "등록계좌 변경", "대리점 등록계좌 변경 신청")
        record_reply(self.db, TREASURY, "FAIL", first, AMOUNT, ACCOUNT, "failure")
        revised = submit_approval(self.db, STAFF, MONTH, "D-001", "기존 송금 실패 확인 후 재상신")
        decide_approval(self.db, LEAD, revised, "approve", "변경계좌와 기존 송금 실패 확인")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.iid, first)
        confirm_account(self.db, TREASURY, self.iid, new_account, "계좌번호 독립 확인")
        self.assertEqual(self.order()["queue_state"], "failed")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.iid)
        second = send_mock(self.db, TREASURY, self.iid, first)
        self.assertEqual([a["account"] for a in self.order()["attempts"]], [ACCOUNT, new_account])
        self.assertEqual(record_reply(self.db, TREASURY, "OK", second, AMOUNT, new_account, "success"), "matched")
