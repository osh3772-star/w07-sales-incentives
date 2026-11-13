import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app import SOURCE_ROOT
from approvals import decide_approval, submit_approval
from payments import confirm_account, create_instructions, list_payments, record_reply, send_mock
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


MONTH = "2026-06"
ACCOUNT = "모의은행 001-000-123"
SUPPORT = USERS["staff"]
TEAM_LEAD = USERS["lead"]
EXECUTIVE = USERS["executive"]
TREASURY = USERS["treasury"]


class PaymentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db_path = Path(folder.name) / "payments.sqlite3"
        self.db = open_db(self.db_path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE_ROOT)
        receive_month(self.db, MONTH)

    def approve(self, group, actor):
        version = submit_approval(self.db, SUPPORT, MONTH, group, "6월 미지급 신규 명세")
        decide_approval(self.db, actor, version, "approve", "등록 근거와 귀속월 누계 확인")
        return version

    def orders(self):
        return list_payments(self.db, MONTH)["orders"]

    def send(self, order):
        iid = order["instruction_id"]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "모의 수취인 증빙 문서")
        return send_mock(self.db, TREASURY, iid)

    def test_group_authority_holds_and_partial_settlement_survive_restart(self):
        approval = submit_approval(self.db, SUPPORT, MONTH, "D-011", "관련자 합산")
        with self.assertRaises(PermissionError):
            decide_approval(self.db, TEAM_LEAD, approval, "approve", "전결 시도")
        self.assertEqual(self.orders(), [])
        decide_approval(self.db, EXECUTIVE, approval, "approve", "3,888,097원 합산 확인")
        group = self.orders()
        self.assertEqual({o["agency_id"] for o in group}, {"D-011", "D-024", "D-037"})
        self.assertEqual(sum(o["amount_krw"] for o in group), 3_888_097)
        self.assertTrue(all(o["state"] == "held" and o["account"] is None for o in group))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.assertEqual(create_instructions(self.db, approval), 0)
        order = next(o for o in group if o["agency_id"] == "D-024")
        iid = order["instruction_id"]
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        with self.assertRaises(PermissionError):
            confirm_account(self.db, EXECUTIVE, iid, ACCOUNT, "수취인 증빙")
        with self.assertRaises(ValueError):
            confirm_account(self.db, TREASURY, iid, ACCOUNT, "")
        attempt = self.send(order)
        result = record_reply(self.db, TREASURY, "MOCK-GROUP", attempt, order["amount_krw"], ACCOUNT, "success")
        self.assertEqual(result, "matched")
        before = list_payments(self.db, MONTH)
        with closing(open_db(self.db_path)) as reopened:
            self.assertEqual(list_payments(reopened, MONTH), before)
        self.assertCountEqual([o["state"] for o in self.orders()], ["completed", "held", "held"])

    def test_mismatch_failure_retry_then_ignored_delayed_reply(self):
        self.approve("D-001", TEAM_LEAD)
        order, = self.orders()
        self.assertEqual((order["rebate_id"], order["amount_krw"]), ("RB-00006", 988_932))
        first = self.send(order)
        self.assertEqual(record_reply(self.db, TREASURY, "AMOUNT", first, 933, ACCOUNT, "success"), "review")
        self.assertEqual(record_reply(self.db, TREASURY, "ACCOUNT", first, 988_932, "타행 001-000-123", "failure"),
                         "review")
        self.assertEqual(self.orders()[0]["state"], "review")
        self.assertEqual(send_mock(self.db, TREASURY, order["instruction_id"]), first)
        self.assertEqual(record_reply(self.db, TREASURY, "FAILED", first, 988_932, ACCOUNT, "failure"), "failed")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, order["instruction_id"], "잘못된 시도번호")
        second = send_mock(self.db, TREASURY, order["instruction_id"], first)
        self.assertNotEqual(first, second)
        self.assertEqual(send_mock(self.db, TREASURY, order["instruction_id"], first), second)
        self.assertEqual(record_reply(self.db, TREASURY, "LATE", first, 988_932, ACCOUNT, "success"), "late")
        self.assertEqual(self.orders()[0]["state"], "sent")
        for _ in range(2):
            self.assertEqual(record_reply(self.db, TREASURY, "DONE", second, 988_932, "모의은행 001000123", "success"),
                             "matched")
        with self.assertRaises(ValueError):
            record_reply(self.db, TREASURY, "DONE", second, 988_931, ACCOUNT, "success")
        order, = self.orders()
        self.assertEqual((order["state"], len(order["attempts"]), len(order["replies"])), ("completed", 2, 5))
        self.assertEqual(record_reply(self.db, TREASURY, "UNKNOWN", "MOCK-UNKNOWN", 1, ACCOUNT, "success"), "unmatched")
        self.assertEqual(list_payments(self.db, MONTH)["unmatched"][0]["reply_id"], "UNKNOWN")
        self.assertEqual(self.orders()[0]["state"], "completed")

    def test_concurrent_duplicate_send_and_reply_recorded_once(self):
        self.approve("D-001", TEAM_LEAD)
        order, = self.orders()
        iid = order["instruction_id"]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "모의 수취계좌 확인 문서")

        def send(_):
            with closing(open_db(self.db_path)) as db:
                return send_mock(db, TREASURY, iid)

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(send, range(2)))
        self.assertEqual(attempts[0], attempts[1])

        def reply(_):
            with closing(open_db(self.db_path)) as db:
                return record_reply(db, TREASURY, "DUPLICATE", attempts[0], order["amount_krw"], ACCOUNT, "success")

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(reply, range(2))), ["matched", "matched"])
        final, = self.orders()
        self.assertEqual((final["state"], len(final["attempts"]), len(final["replies"])), ("completed", 1, 1))
