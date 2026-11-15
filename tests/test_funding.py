import csv
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import funding
from amount_reviews import resolve_amount
from app import SOURCE_ROOT, create_app
from approvals import decide_approval, submit_approval
from corrections import apply_correction
from payments import confirm_account, list_payments, record_reply, resolve_bank_review, send_mock
from store import create_funding_database, open_db, receive_month, sync_sources
from test_intake import running_server
from test_workflow_http import Session
from workflow_security import USERS


STAFF, EXEC, TREASURY = (USERS[role] for role in ("staff", "executive", "treasury"))
SCOPE = "시연 Corevia 원화 전용자금"
DAY, LATER = "2026-07-10", "2026-07-13"
ACCOUNT = "모의은행 1200-300"


class FundingLedgerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "funding.sqlite3"
        create_funding_database(self.path)
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE_ROOT)
        receive_month(self.db, "2026-06")
        approval_id = submit_approval(self.db, STAFF, "2026-06", "D-011", "실제 3건 합산")
        decide_approval(self.db, EXEC, approval_id, "approve", "임원 승인")
        self.ids = [r[0] for r in self.db.execute("SELECT instruction_id FROM payment_instructions ORDER BY instruction_id")]
        for iid in self.ids:
            funding.schedule(self.db, TREASURY, iid, DAY, "원 지급예정일")
        today = patch("funding._today", return_value="2026-07-14")
        today.start()
        self.addCleanup(today.stop)

    def baseline(self, amount=3_000_000, day="2026-07-09", **fields):
        return funding.set_balance(self.db, TREASURY, amount, day, "잔액증명 대조", SCOPE, **fields)

    def balance(self):
        v = funding.overview(self.db)
        return v["confirmed"], v["reserved"], v["free"], v["days"][-1]["shortage"]

    def concurrent(self, action):
        def run(_):
            with closing(open_db(self.path)) as db:
                return action(db)

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, range(2)))

    def treasury_post(self, path, **fields):
        client = create_app(self.path).test_client()
        with client.session_transaction() as session:
            session["user_id"] = TREASURY.user_id
            session["csrf_token"] = "funding-test"
        return client.post(path, data={"csrf_token": "funding-test", "month": "2026-06", **fields},
                           follow_redirects=True)

    def test_only_500000_received_leaves_388097_short_on_original_date(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        self.assertEqual(self.balance()[-1], 888_097)

        receipt = self.treasury_post("/payments/plan/deposit", reference="부분입금-1", amount="500000",
                                     received_on=DAY, evidence="50만원 입금 확인서")
        self.assertEqual(receipt.status_code, 200)
        self.assertIn("388,097원", receipt.get_data(as_text=True))
        allocation = self.treasury_post("/payments/plan/allocate", due_date=DAY)
        self.assertEqual(allocation.status_code, 200)
        self.assertIn("새로 배정한 지급지시 0건", allocation.get_data(as_text=True))

        view = funding.overview(self.db)
        self.assertEqual([(d["due_date"], d["total"], d["shortage"]) for d in view["days"]],
                         [(DAY, 3_888_097, 388_097)])
        self.assertEqual([(r["reference"], r["amount"]) for r in view["receipts"]], [("부분입금-1", 500_000)])
        self.assertEqual((view["confirmed"], view["reserved"], view["free"]), (3_500_000, 2_562_561, 937_439))
        self.assertEqual([r["agency_id"] for r in view["lines"] if not r["allocated"]], ["D-037"])

    def test_full_allocation_still_blocks_unverified_account_and_correction(self):
        self.baseline(3_888_097)
        funding.allocate(self.db, TREASURY, DAY)
        initial = funding.overview(self.db)
        self.assertEqual((initial["reserved"], initial["free"]), (3_888_097, 0))
        lines = {line["agency_id"]: line for line in initial["lines"]}
        self.assertEqual(set(lines), {"D-011", "D-024", "D-037"})
        self.assertTrue(all(line["funds_ready"] for line in lines.values()))

        def assert_send_blocked(iid, message):
            response = self.treasury_post(f"/payments/{iid}/send")
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.get_data(as_text=True))
            self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)
            self.assertEqual(self.balance(), (3_888_097, 3_888_097, 0, 0))

        self.assertEqual(lines["D-024"]["queue_state"], "held")
        assert_send_blocked(lines["D-024"]["instruction_id"], "수취계좌와 증빙을 확인할 때까지 지급 보류")

        iid = lines["D-011"]["instruction_id"]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "수취계좌 확인서")
        apply_correction(self.db, STAFF, "D-011", {"address": "검증시 새 도로 123"},
                         "2026-06-15", "승인 후 주소 정정", "변경 확인서")
        corrected = next(r for r in funding.overview(self.db)["lines"] if r["instruction_id"] == iid)
        self.assertEqual((corrected["state"], corrected["queue_state"], corrected["funds_ready"]),
                         ("ready", "reapproval", True))
        assert_send_blocked(iid, "등록정보 정정으로 지급 보류")

    def test_two_dates_compete_for_the_same_cash(self):
        self.baseline()
        funding.schedule(self.db, TREASURY, self.ids[-1], LATER, "다음 예정일")

        def reserve(day):
            with closing(open_db(self.path)) as db:
                return funding.allocate(db, TREASURY, day)

        with ThreadPoolExecutor(max_workers=2) as pool:
            booked = [iid for result in pool.map(reserve, (DAY, LATER)) for iid in result]
        self.assertCountEqual(booked, self.ids[:2])
        view = funding.overview(self.db)
        self.assertEqual([(d["due_date"], d["shortage"]) for d in view["days"]], [(DAY, 0), (LATER, 888_097)])
        self.assertEqual((view["reserved"], view["free"]), (2_562_561, 437_439))

    def test_source_amounts_funding_and_two_concurrent_claimants(self):
        self.baseline()
        self.assertEqual(self.balance(), (3_000_000, 0, 3_000_000, 888_097))
        results = self.concurrent(lambda db: funding.allocate(db, TREASURY, DAY))
        self.assertCountEqual(results, [self.ids[:2], []])
        self.assertEqual(self.balance(), (3_000_000, 2_562_561, 437_439, 888_097))
        receive = lambda db: funding.confirm_receipt(db, TREASURY, "은행고유거래-1", 900_000, DAY, "보통예금 입금 증명")
        self.assertCountEqual(self.concurrent(receive), [True, False])
        self.assertEqual(self.balance(), (3_900_000, 2_562_561, 1_337_439, 0))
        self.assertEqual(funding.allocate(self.db, TREASURY, DAY), self.ids[2:])
        self.assertEqual(self.balance(), (3_900_000, 3_888_097, 11_903, 0))
        with self.assertRaises(ValueError):
            funding.confirm_receipt(self.db, TREASURY, "은행고유거래-1", 1_800_000, DAY, "다른 내용")
        with closing(open_db(self.path)) as other:
            self.assertFalse(receive(other))
            self.assertEqual(funding.overview(other)["free"], 11_903)
            self.assertEqual(other.execute("SELECT count(*) FROM cash_plan_events WHERE action='reserve'").fetchone()[0], 3)
            self.assertEqual(other.execute("SELECT count(*) FROM cash_receipts").fetchone()[0], 1)

    def test_defer_does_not_cover_next_date_and_partial_income_counts_once(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        funding.schedule(self.db, TREASURY, self.ids[-1], LATER, "거래처 조정 동의서", expected_date=DAY)
        days = funding.overview(self.db)["days"]
        self.assertEqual([(d["due_date"], d["total"], d["shortage"]) for d in days],
                         [(DAY, 2_562_561, 0), (LATER, 1_325_536, 888_097)])
        self.assertEqual(funding.allocate(self.db, TREASURY, LATER), [])
        funding.confirm_receipt(self.db, TREASURY, "부분입금", 500_000, DAY, "첫 입금 확인")
        self.assertEqual(self.balance(), (3_500_000, 2_562_561, 937_439, 388_097))
        self.baseline(3_500_000, DAY)
        self.assertEqual(self.balance(), (3_500_000, 2_562_561, 937_439, 388_097))
        self.assertFalse(funding.confirm_receipt(self.db, TREASURY, "부분입금", 500_000, DAY, "첫 입금 확인"))
        with self.assertRaises(ValueError):
            funding.confirm_receipt(self.db, TREASURY, "중복 계상 시도", 500_000, DAY, "마감 잔액에 포함된 거래")
        with self.assertRaises(ValueError):
            funding.confirm_receipt(self.db, TREASURY, "약정", 400_000, "2026-07-15", "도착 예정")

    def test_due_account_and_bank_outcome_gates_keep_one_debit(self):
        iid = self.ids[0]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "시연 수취 증빙")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        self.baseline()
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        funding.allocate(self.db, TREASURY, DAY)
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, self.ids[1])   # fully reserved, no account
        with patch("funding._today", return_value="2026-07-09"), self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        attempt = send_mock(self.db, TREASURY, iid)
        self.assertEqual(send_mock(self.db, TREASURY, iid), attempt)
        with self.assertRaises(ValueError):
            funding.release(self.db, TREASURY, iid, "전송 중")
        with self.assertRaises(ValueError):
            funding.schedule(self.db, TREASURY, iid, LATER, "전송 중")
        self.assertEqual(record_reply(self.db, TREASURY, "불일치", attempt, 3000, ACCOUNT, "success"), "review")
        self.assertEqual(self.balance(), (3_000_000, 2_562_561, 437_439, 888_097))
        self.assertEqual(record_reply(self.db, TREASURY, "실패", attempt, 1_373_515, ACCOUNT, "failure"), "review")
        order = next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == iid)
        resolve_bank_review(self.db, TREASURY, iid, "retry", "거래번호 확인 · 미지급",
                            order["reply_version"])
        retry = send_mock(self.db, TREASURY, iid, expected_attempt=attempt)
        self.assertNotEqual(retry, attempt)
        for _ in range(2):
            self.assertEqual(record_reply(self.db, TREASURY, "성공", retry, 1_373_515, ACCOUNT, "success"), "matched")
        self.assertEqual(self.balance(), (1_626_485, 1_189_046, 437_439, 888_097))
        self.baseline(1_626_485, "2026-07-14")
        self.assertEqual(self.balance(), (1_626_485, 1_189_046, 437_439, 888_097))

    def test_revised_cash_and_correction_keep_existing_reservations(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        iid = self.ids[0]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "기존 증빙")
        self.baseline(3_000_000, DAY, restricted=50_000, external=450_000)
        self.assertEqual(funding.overview(self.db)["overcommitted"], 62_561)
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        self.baseline(3_000_000, LATER)
        apply_correction(self.db, STAFF, "D-011", {"address": "검증시 새 도로 123"},
                         "2026-06-15", "등록정보 정정", "증빙")
        self.assertTrue(any(r["queue_state"] == "reapproval" for r in funding.overview(self.db)["lines"]))
        self.assertEqual(funding.overview(self.db)["reserved"], 2_562_561)
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, iid)
        with self.assertRaises(PermissionError):
            funding.release(self.db, STAFF, iid, "권한 확인")

    def test_failed_payment_released_then_disputed_relocks_overcommitted_cash(self):
        self.baseline(1_373_515)
        funding.allocate(self.db, TREASURY, DAY)
        first, second, _ = self.ids
        confirm_account(self.db, TREASURY, first, ACCOUNT, "수취계좌 확인서")
        attempt = send_mock(self.db, TREASURY, first)
        record_reply(self.db, TREASURY, "거절", attempt, 1_373_515, ACCOUNT, "failure")
        funding.release(self.db, TREASURY, first, "실패 확정, 영업지원과 지급일 재협의")
        funding.schedule(self.db, TREASURY, first, LATER, "일정 협의 완료")
        self.assertEqual(funding.allocate(self.db, TREASURY, DAY), [second])
        record_reply(self.db, TREASURY, "상충", attempt, 1_373_515, ACCOUNT, "success")
        disputed = funding.overview(self.db)
        self.assertEqual((disputed["reserved"], disputed["overcommitted"]), (2_562_561, 1_189_046))
        confirm_account(self.db, TREASURY, second, ACCOUNT, "별도 증빙")
        with self.assertRaises(ValueError):
            send_mock(self.db, TREASURY, second)
        record_reply(self.db, TREASURY, "최종확인", attempt, 1_373_515, ACCOUNT, "success")
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 0)
        order = next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == first)
        resolve_bank_review(self.db, TREASURY, first, "completed", "회계 지급확인서", order["reply_version"], "최종확인")
        final = funding.overview(self.db)
        self.assertEqual((final["confirmed"], final["reserved"], final["free"]), (0, 1_189_046, 0))

    def test_stale_release_cannot_release_a_new_reservation(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        first = funding.overview(self.db)["lines"][0]
        iid, token = first["instruction_id"], first["reservation_version"]
        self.assertTrue(funding.release(self.db, TREASURY, iid, "신청 취소", expected_reservation=token))
        self.assertFalse(funding.release(self.db, TREASURY, iid, "신청 취소", expected_reservation=token))
        self.assertEqual(funding.allocate(self.db, TREASURY, DAY), [iid])
        with self.assertRaises(ValueError):
            funding.release(self.db, TREASURY, iid, "신청 취소", expected_reservation=token)
        active = funding.overview(self.db)["lines"][0]
        self.assertTrue(active["allocated"])
        self.assertNotEqual(active["reservation_version"], token)

    def test_late_first_success_then_second_failure_holds_until_treasury_decision(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        iid = self.ids[0]
        amount = 1_373_515
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "D-011 수취계좌 확인서")
        first = send_mock(self.db, TREASURY, iid)
        record_reply(self.db, TREASURY, "1차 실패", first, amount, ACCOUNT, "failure")
        second = send_mock(self.db, TREASURY, iid, first)
        record_reply(self.db, TREASURY, "1차 뒤늦은 성공", first, amount, ACCOUNT, "success")
        held = next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == iid)
        self.assertTrue(held["bank_conflict"])
        record_reply(self.db, TREASURY, "2차 실패", second, amount, ACCOUNT, "failure")
        with closing(open_db(self.path)) as restarted:
            with self.assertRaises(ValueError):
                send_mock(restarted, TREASURY, iid, second)
            with self.assertRaises(ValueError):
                resolve_bank_review(restarted, TREASURY, iid, "retry", "오래된 화면", held["reply_version"])
        held = next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == iid)
        version = held["reply_version"]
        self.assertEqual((len(held["attempts"]), len(held["replies"])), (2, 3))
        self.assertEqual(self.balance(), (3_000_000, 2_562_561, 437_439, 888_097))
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 0)
        with self.assertRaises(PermissionError):
            resolve_bank_review(self.db, EXEC, iid, "retry", "미지급", version)
        with self.assertRaises(ValueError):
            resolve_bank_review(self.db, TREASURY, iid, "completed", "은행 확인", version, "2차 실패")
        resolve_bank_review(self.db, TREASURY, iid, "retry", "1·2차 모두 미지급, 은행 조회 명세", version)
        third = send_mock(self.db, TREASURY, iid, second)
        self.assertEqual(record_reply(self.db, TREASURY, "3차 성공", third, amount, ACCOUNT, "success"), "matched")
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 1)

    def test_conflict_after_completion_keeps_history_and_never_books_a_second_debit(self):
        self.baseline()
        funding.allocate(self.db, TREASURY, DAY)
        iid = self.ids[0]
        amount = 1_373_515
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "D-011 확인서")
        first = send_mock(self.db, TREASURY, iid)
        record_reply(self.db, TREASURY, "첫 성공", first, amount, ACCOUNT, "success")
        record_reply(self.db, TREASURY, "상충 실패", first, amount, ACCOUNT, "failure")
        record_reply(self.db, TREASURY, "추가 성공", first, amount, ACCOUNT, "success")

        def order():
            return next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == iid)

        self.assertEqual((order()["state"], order()["bank_conflict"]), ("completed", True))
        self.assertEqual(self.db.execute("SELECT sum(amount) FROM cash_mock_debits").fetchone()[0], amount)
        resolve_bank_review(self.db, TREASURY, iid, "completed", "은행 원장에 단 한 건 지급",
                            order()["reply_version"], "첫 성공")
        self.assertEqual(self.db.execute("SELECT sum(amount) FROM cash_mock_debits").fetchone()[0], amount)
        record_reply(self.db, TREASURY, "추가 실패", first, amount, ACCOUNT, "failure")
        resolve_bank_review(self.db, TREASURY, iid, "retry", "첫 이체가 취소되어 최종 미지급",
                            order()["reply_version"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT sum(amount) FROM reversed_mock_debits").fetchone()[0], amount)
        self.assertEqual(self.balance(), (3_000_000, 2_562_561, 437_439, 888_097))
        second = send_mock(self.db, TREASURY, iid, first)
        record_reply(self.db, TREASURY, "재송금 성공", second, amount, ACCOUNT, "success")
        self.assertEqual(self.db.execute("SELECT sum(amount) FROM cash_mock_debits").fetchone()[0], amount)

    def test_completed_payment_mismatches_require_evidence_and_preserve_the_original_debit(self):
        approval = submit_approval(self.db, STAFF, "2026-06", "D-001", "원본 정산서")
        decide_approval(self.db, USERS["lead"], approval, "approve", "팀 전결")
        iid = self.db.execute("SELECT instruction_id FROM payment_instructions WHERE rebate_id='RB-00006'").fetchone()[0]
        self.baseline(6_000_000)
        funding.schedule(self.db, TREASURY, iid, DAY, "확인한 지급일")
        funding.allocate(self.db, TREASURY, DAY)
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "D-001 수취계좌 확인서")
        attempt = send_mock(self.db, TREASURY, iid)
        amount = 988_932
        self.assertEqual(record_reply(self.db, TREASURY, "PAID", attempt, amount, ACCOUNT, "success"), "matched")
        paid = self.db.execute("SELECT * FROM mock_bank_replies WHERE reply_id='PAID'").fetchone()
        debit = self.db.execute("SELECT * FROM cash_mock_debits WHERE instruction_id=?", (iid,)).fetchone()
        self.assertEqual((debit["reply_id"], debit["amount"]), ("PAID", amount))

        def order():
            return next(o for o in list_payments(self.db, "2026-06")["orders"] if o["instruction_id"] == iid)

        record_reply(self.db, TREASURY, "PAID-COPY", attempt, amount, ACCOUNT.replace("-", ""), "success")
        self.assertEqual(order()["bank_reviews"], [])
        for _ in range(2):
            record_reply(self.db, TREASURY, "AMOUNT", attempt, amount - 1, ACCOUNT, "success")
        current = order()
        self.assertEqual((current["state"], current["bank_conflict"], len(current["bank_reviews"])),
                         ("completed", True, 1))
        old_version = current["reply_version"]
        other_account = "모의은행 1200-301"
        for _ in range(2):
            record_reply(self.db, TREASURY, "ACCOUNT", attempt, amount, other_account, "success")
        current = order()
        self.assertEqual(len(current["bank_reviews"]), 1)
        self.assertEqual(current["bank_reviews"][0]["opened_by_reply"], "AMOUNT")
        self.assertEqual((current["queue_state"], current["bank_review_kind"]), ("bank_check", "discrepancy"))
        version = current["reply_version"]
        evidence = "은행 명세서 DOC-1로 원래 수취계좌의 988,932원 1회 집행을 대조"
        choice = dict(decision="completed", evidence=evidence, reply_version=version, success_reply="PAID")
        review = f"/payments/{iid}/bank-review"

        with running_server(self.path) as url:
            client = Session(url)
            self.assertEqual(client.post(review, **choice)[0], 403)
            self.assertEqual(client.post("/identity", user_id="treasury", next="/")[0], 200)
            status, page = client.get("/payments?month=2026-06&group=D-001")
            self.assertEqual(status, 200)
            self.assertIn("대사 완료 · 회계 재검토 필요", page)
            self.assertIn(f'action="{review}"', page)
            self.assertEqual(client.post(f"/payments/{iid}/send", expected_attempt=attempt)[0], 400)
            for invalid in (dict(reply_version=old_version), dict(evidence=""), dict(success_reply="ACCOUNT"),
                            dict(success_reply="AMOUNT"), dict(decision="retry")):
                self.assertEqual(client.post(review, **{**choice, **invalid})[0], 400)
            self.assertEqual(client.post(review, **choice)[0], 200)

        with running_server(self.path) as url:
            client = Session(url)
            client.post("/identity", user_id="treasury", next="/")
            self.assertEqual(client.post(review, **choice)[0], 200)
            page = client.get("/payments?month=2026-06&group=D-001")[1]
            self.assertIn(evidence, page)
            self.assertIn("검토한 불일치 회신", page)
            self.assertNotIn(f'action="{review}"', page)
            for rid, reported, account in (("AMOUNT", amount - 1, ACCOUNT), ("ACCOUNT", amount, other_account),
                                           ("SAME-AFTER", amount, ACCOUNT)):
                self.assertEqual(client.post("/payments/reply", reply_id=rid, attempt_id=attempt,
                                             amount=reported, account=account, result="success")[0], 200)
        current = order()
        self.assertEqual((current["state"], current["bank_conflict"], len(current["attempts"])), ("completed", False, 1))
        decision, = current["bank_reviews"]
        self.assertCountEqual(decision["mismatched_reply_ids"], ["AMOUNT", "ACCOUNT"])
        self.assertEqual((decision["decision"], decision["success_reply"], decision["through_reply"],
                          decision["reviewed_by"], decision["evidence"]), ("completed", "PAID", version, "treasury", evidence))
        self.assertCountEqual([r["reply_id"] for r in current["replies"]],
                              ["PAID", "PAID-COPY", "AMOUNT", "ACCOUNT", "SAME-AFTER"])
        record_reply(self.db, TREASURY, "ACCOUNT-COPY", attempt, amount, other_account.replace("-", ""), "success")
        current = order()
        self.assertFalse(current["bank_conflict"])
        self.assertEqual(len(current["bank_reviews"]), 1)

        # A newly reported wrong account after the decision needs its own examination.
        record_reply(self.db, TREASURY, "ACCOUNT-NEW", attempt, amount, "모의은행 1200-302", "success")
        current = order()
        self.assertTrue(current["bank_conflict"])
        self.assertEqual(len(current["bank_reviews"]), 2)
        self.assertEqual(sum(r["decision"] is None for r in current["bank_reviews"]), 1)
        self.assertEqual(self.db.execute("SELECT * FROM cash_mock_debits WHERE instruction_id=?", (iid,)).fetchall(), [debit])
        self.assertEqual(self.db.execute("SELECT * FROM mock_bank_replies WHERE reply_id='PAID'").fetchone(), paid)
        self.assertEqual(self.balance()[0], 6_000_000 - amount)

    def test_treasury_changes_completed_debit_to_first_success_without_another_withdrawal(self):
        iid = self.db.execute(
            "SELECT instruction_id FROM payment_instructions WHERE agency_id='D-011'"
        ).fetchone()[0]
        amount = 1_373_515
        self.baseline(3_000_000)
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "D-011 수취계좌 확인서")
        funding.allocate(self.db, TREASURY, DAY)
        first = send_mock(self.db, TREASURY, iid)
        record_reply(self.db, TREASURY, "F1", first, amount, ACCOUNT, "failure")
        second = send_mock(self.db, TREASURY, iid, first)
        record_reply(self.db, TREASURY, "S2", second, amount, ACCOUNT, "success")
        debit_query = "SELECT reply_id,amount,paid_on,balance_id FROM cash_mock_debits WHERE instruction_id=?"
        initial = self.db.execute(debit_query, (iid,)).fetchone()
        self.assertEqual(initial["reply_id"], "S2")
        self.assertEqual(self.balance(), (1_626_485, 1_189_046, 437_439, 888_097))

        # The next bank closing balance already includes the original withdrawal.
        self.baseline(1_626_485, "2026-07-14")
        record_reply(self.db, TREASURY, "S1", first, amount, ACCOUNT, "success")
        order = next(row for row in list_payments(self.db, "2026-06")["orders"] if row["instruction_id"] == iid)
        self.assertTrue(order["bank_conflict"])
        reply_history = self.db.execute("SELECT * FROM mock_bank_replies ORDER BY rowid").fetchall()
        self.assertEqual([(r["reply_id"], r["attempt_id"]) for r in reply_history],
                         [("F1", first), ("S2", second), ("S1", first)])
        consume = "SELECT * FROM cash_plan_events WHERE instruction_id=? AND action='consume'"
        original_events = self.db.execute(consume, (iid,)).fetchall()
        self.assertEqual(len(original_events), 1)
        self.assertIn("S2", original_events[0]["reason"])
        evidence = "은행 거래조회서로 1차만 출금됐음을 대조"
        decision = dict(decision="completed", evidence=evidence, reply_version=order["reply_version"], success_reply="S1")
        url = f"/payments/{iid}/bank-review"
        self.assertEqual(self.treasury_post(url, **decision).status_code, 200)
        self.assertEqual(self.db.execute(debit_query, (iid,)).fetchone()["reply_id"], "S1")
        with running_server(self.path) as restarted_url:
            restarted = Session(restarted_url)
            self.assertEqual(restarted.post("/identity", user_id="treasury", next="/")[0], 200)
            self.assertEqual(restarted.post(url, **decision)[0], 200)
            self.assertEqual(restarted.post(url, **{**decision, "success_reply": "S2"})[0], 400)
            status, page = restarted.get("/payments?month=2026-06&group=D-011")
            self.assertEqual(status, 200)
            self.assertIn("현재 모의 출금 <strong>1,373,515원</strong>", page)
            self.assertIn("출금 근거 1차 성공 회신 <strong>S1</strong>", page)
            self.assertIn("출금 근거 변경 S2 → S1", page)
            self.assertIn(evidence, page)
        with closing(open_db(self.path)) as reopened:
            debits = reopened.execute(debit_query, (iid,)).fetchall()
            self.assertEqual([tuple(row) for row in debits], [("S1", amount, initial["paid_on"], initial["balance_id"])])
            self.assertEqual(reopened.execute(consume, (iid,)).fetchall(), original_events)
            self.assertEqual(reopened.execute("SELECT * FROM mock_bank_replies ORDER BY rowid").fetchall(), reply_history)
            reviews = reopened.execute(
                """SELECT c.previous_reply_id,b.success_reply,b.reviewed_by,b.evidence,b.reviewed_at
                   FROM bank_result_reviews b LEFT JOIN debit_evidence_changes c USING(review_id)
                   WHERE b.instruction_id=?""", (iid,),
            ).fetchall()
            self.assertEqual([tuple(row[:4]) for row in reviews], [("S2", "S1", TREASURY.user_id, evidence)])
            self.assertTrue(reviews[0]["reviewed_at"])
            self.assertEqual(reopened.execute("SELECT count(*) FROM reversed_mock_debits").fetchone()[0], 0)
            book = funding.overview(reopened)
            self.assertEqual((book["confirmed"], book["reserved"], book["free"]), (1_626_485, 1_189_046, 437_439))

    def test_prior_amount_success_cannot_complete_reapproved_larger_order(self):
        version = submit_approval(self.db, STAFF, "2026-06", "D-001", "단건 전결")
        decide_approval(self.db, USERS["lead"], version, "approve", "988,932원 확인")
        iid = self.db.execute("SELECT instruction_id FROM payment_instructions WHERE rebate_id='RB-00006'").fetchone()[0]
        confirm_account(self.db, TREASURY, iid, ACCOUNT, "D-001 확인서")
        self.baseline(6_000_000)
        funding.schedule(self.db, TREASURY, iid, DAY, "지급 예정")
        funding.allocate(self.db, TREASURY, DAY)
        first = send_mock(self.db, TREASURY, iid)
        record_reply(self.db, TREASURY, "OLD-FAIL", first, 988_932, ACCOUNT, "failure")
        source = self.path.parent / "source"
        shutil.copytree(SOURCE_ROOT, source)
        ledger = source / "sales/sales_rebate_payment_cv.csv"
        with ledger.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter="|")
            changed = next(r for r in reader if r["rebate_id"] == "RB-00006")
        changed["rebate_amount_krw"] = "1200000"
        with ledger.open("a", encoding="utf-8", newline="") as file:
            csv.DictWriter(file, fieldnames=list(changed), delimiter="|").writerow(changed)
        sync_sources(self.db, source)
        resolve_amount(self.db, STAFF, "RB-00006", 1_200_000, "원금 정정 확인서", 1)
        revised = submit_approval(self.db, STAFF, "2026-06", "D-001", "최신 금액 재상신")
        decide_approval(self.db, USERS["lead"], revised, "approve", "1,200,000원 확정")
        funding.allocate(self.db, TREASURY, DAY)
        second = send_mock(self.db, TREASURY, iid, first)
        record_reply(self.db, TREASURY, "OLD-OK", first, 988_932, ACCOUNT, "success")
        record_reply(self.db, TREASURY, "NEW-OK", second, 1_200_000, ACCOUNT, "success")
        order = next(r for r in list_payments(self.db, "2026-06")["orders"] if r["instruction_id"] == iid)
        with self.assertRaisesRegex(ValueError, "현재 승인 전액"):
            resolve_bank_review(self.db, TREASURY, iid, "completed", "과거 성공을 선택", order["reply_version"], "OLD-OK")
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 0)
        resolve_bank_review(self.db, TREASURY, iid, "completed", "은행 확인 · 새 지급액 1회 출금",
                            order["reply_version"], "NEW-OK")
        self.assertEqual(tuple(self.db.execute("SELECT count(*),sum(amount) FROM cash_mock_debits").fetchone()),
                         (1, 1_200_000))
