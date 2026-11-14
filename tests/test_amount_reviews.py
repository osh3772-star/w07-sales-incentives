import csv
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import funding
from amount_reviews import has_open_issue
from app import SOURCE_ROOT, create_app
from approvals import decide_approval, submit_approval
from payments import confirm_account, record_reply
from rules import month_overview
from store import create_funding_database, open_db, receive_month, sync_sources
from workflow_security import USERS


MONTH, DUE, REBATE = "2026-06", "2026-07-10", "RB-00006"
ACCOUNT = "모의은행 100-010"
STAFF, LEAD, CASH = (USERS[role] for role in ("staff", "lead", "treasury"))


class AmountReviewTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "source"
        for name in ("master/m_agency_cv.csv", "financials/fin_collection_history_cv.csv",
                     "sales/sales_rebate_payment_cv.csv"):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE_ROOT / name, target)
        self.ledger = self.source / "sales/sales_rebate_payment_cv.csv"
        self.path = self.root / "amount-review.sqlite3"
        create_funding_database(self.path)
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        self.client = create_app(self.path, self.source).test_client()
        receive_month(self.db, MONTH)
        self.approval = submit_approval(self.db, STAFF, MONTH, "D-001", "단독 기승인")
        decide_approval(self.db, LEAD, self.approval, "approve", "988932 승인")
        self.iid = self.db.execute("SELECT instruction_id FROM payment_instructions").fetchone()[0]
        confirm_account(self.db, CASH, self.iid, ACCOUNT, "모의 증빙")
        funding.set_balance(self.db, CASH, 3_000_000, "2026-07-09", "잔액 증빙", "원화 전용")
        funding.schedule(self.db, CASH, self.iid, DUE, "지급일")
        funding.allocate(self.db, CASH, DUE)
        self.send_path = f"/payments/{self.iid}/send"
        self.review_path = f"/intake/{REBATE}/resolve-amount"

    def post(self, actor, path, status, **fields):
        with self.client.session_transaction() as session:
            session["user_id"] = actor.user_id
            session["csrf_token"] = "intake-test"
        response = self.client.post(path, data={"month": MONTH, "csrf_token": "intake-test", **fields},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True).partition('role="alert">')[2][:300])
        return response.get_data(as_text=True)

    def add_input(self, amount, rebate_id=REBATE, *, copy_from=REBATE, **updates):
        with self.ledger.open(encoding="utf-8-sig", newline="") as file:
            rows = csv.DictReader(file, delimiter="|")
            names = rows.fieldnames
            incoming = next(r for r in rows if r["rebate_id"] == copy_from)
        incoming["rebate_amount_krw"] = amount
        incoming["rebate_id"] = rebate_id
        incoming.update(updates)
        with self.ledger.open("a", encoding="utf-8", newline="") as file:
            csv.DictWriter(file, fieldnames=names, delimiter="|", lineterminator="\n").writerow(incoming)
        self.post(STAFF, "/intake", 200)

    def check_new_amount(self, amount, decider):
        self.add_input(amount)
        self.post(CASH, self.send_path, 400)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)
        html = self.post(STAFF, self.review_path, 200, expected_input=1, amount=amount, reason="정산 증빙 대조")
        for word in ("988,932원", f"{amount:,}원", "정산 증빙 대조", "영업지원 담당자"):
            self.assertIn(word, html)
        self.assertEqual(self.db.execute("SELECT rebate_amount_krw FROM rebates WHERE rebate_id=?", (REBATE,)).fetchone()[0], 988_932)
        self.assertEqual(self.db.execute("SELECT confirmed_amount,decided_by FROM amount_decisions").fetchone()[:], (amount, "staff"))
        self.assertEqual(self.db.execute("SELECT json_extract(observed_row,'$.rebate_amount_krw') FROM intake_issues").fetchone()[0], amount)
        group = next(g for g in month_overview(self.db, MONTH)["groups"] if g["group_id"] == "D-001")
        self.assertEqual(group["total"], amount)
        self.assertEqual(group["authority"], "영업임원" if decider == "executive" else "영업지원팀")
        self.post(CASH, self.send_path, 400)
        recovery = submit_approval(self.db, STAFF, MONTH, "D-001", "확정된 금액으로 재상신")
        approval_row = self.db.execute("SELECT monthly_total,new_amount FROM approvals WHERE approval_id=?", (recovery,)).fetchone()
        self.assertEqual(tuple(approval_row), (amount, 0))
        if decider == "executive":
            self.post(LEAD, f"/approvals/{recovery}/decide", 403, action="approve", reason="권한 검사")
        self.post(USERS[decider], f"/approvals/{recovery}/decide", 200, action="approve", reason="확정 근거 검토")
        self.assertEqual(self.db.execute("SELECT amount_krw FROM payment_instructions").fetchall()[0][0], amount)
        self.assertEqual(funding.overview(self.db)["reserved"], 0)
        self.post(CASH, self.send_path, 400)
        self.assertEqual(funding.allocate(self.db, CASH, DUE), [self.iid])
        self.post(CASH, self.send_path, 200)
        attempt = self.db.execute("SELECT attempt_id,amount_krw FROM mock_payment_attempts").fetchone()
        self.assertEqual(attempt["amount_krw"], amount)
        self.assertEqual(record_reply(self.db, CASH, "성공-1", attempt["attempt_id"], amount, ACCOUNT, "success"), "matched")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT monthly_total FROM approvals WHERE approval_id=?", (self.approval,)).fetchone()[0], 988_932)

    def test_1200000_needs_new_team_decision_and_full_reservation(self):
        self.check_new_amount(1_200_000, "lead")

    def test_group_peers_wait_for_disputed_amount_but_separate_group_can_send(self):
        approval = submit_approval(self.db, STAFF, MONTH, "D-011", "관련자 세 지시 심사")
        decide_approval(self.db, USERS["executive"], approval, "approve", "합산 근거 확인")
        funding.set_balance(self.db, CASH, 6_000_000, DUE, "그룹 지급 잔액 증빙", "원화 전용")
        orders = {r["agency_id"]: r["instruction_id"] for r in self.db.execute(
            "SELECT agency_id,instruction_id FROM payment_instructions WHERE approval_id=?", (approval,),
        )}
        for iid in orders.values():
            confirm_account(self.db, CASH, iid, ACCOUNT, "시연 증빙")
            funding.schedule(self.db, CASH, iid, DUE, "그룹 지급일")
        self.assertCountEqual(funding.allocate(self.db, CASH, DUE), orders.values())
        self.add_input(1_200_000, "RB-00120", copy_from="RB-00120")
        sibling = f"/payments/{orders['D-011']}/send"
        self.assertIn("RB-00120", self.post(CASH, sibling, 400))
        self.post(CASH, self.send_path, 200)  # D-001 has a separate June approval and scope.
        self.post(STAFF, "/intake/RB-00120/resolve-amount", 200, amount=1_189_046,
                  expected_input=1, reason="D-024 최초 승인 명세와 증빙 일치")
        self.post(CASH, sibling, 200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 2)

    def test_amount_decision_does_not_clear_a_different_agency_input(self):
        self.add_input(1_200_000, agency_id="D-002")
        self.add_input(1_200_000)
        self.post(STAFF, self.review_path, 200, expected_input=1, amount=988_932, reason="금액 대조 완료")
        self.assertTrue(has_open_issue(self.db, REBATE))
        self.post(CASH, self.send_path, 400)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)

    def test_2188932_needs_executive_decision_and_full_reservation(self):
        self.check_new_amount(2_188_932, "executive")

    def test_amount_decided_while_sent_preserves_actual_completed_amount(self):
        self.post(CASH, self.send_path, 200)
        attempt = self.db.execute("SELECT attempt_id FROM mock_payment_attempts").fetchone()[0]
        self.add_input(2_188_932)
        self.post(STAFF, self.review_path, 200, amount=2_188_932, reason="정산 금액 차이 확인", expected_input=1)
        self.post(CASH, self.send_path, 400)
        self.assertEqual(record_reply(self.db, CASH, "기존금액-성공", attempt, 988_932, ACCOUNT, "success"), "matched")
        group = next(g for g in month_overview(self.db, MONTH)["groups"] if g["group_id"] == "D-001")
        self.assertEqual((group["paid_amount"], group["total"]), (988_932, 988_932))
        self.assertIn("기지급 기록 988,932원과 확정액에 차이", self.client.get("/?month=" + MONTH).get_data(as_text=True))
        self.assertEqual(self.db.execute("SELECT amount FROM cash_mock_debits").fetchone()[0], 988_932)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 1)

    def test_decision_to_keep_original_validates_inputs_and_survives_restart(self):
        self.add_input(1_200_000)
        self.add_input(2_188_932)
        fields = {"amount": 988_932, "reason": "원장 증빙 확인 후 추가 두 입력 배제", "expected_input": 2}
        self.post(CASH, self.review_path, 403, **fields)
        self.post(STAFF, self.review_path, 400, **{**fields, "expected_input": 1})
        self.post(STAFF, self.review_path, 400, **{**fields, "reason": ""})
        self.post(CASH, self.send_path, 400)
        self.post(STAFF, self.review_path, 200, **fields)
        self.post(STAFF, self.review_path, 400, **fields)
        with closing(open_db(self.path)) as restarted:
            sync_sources(restarted, self.source)
            self.assertFalse(has_open_issue(restarted, REBATE))
            self.assertEqual(restarted.execute("SELECT count(*) FROM amount_decisions").fetchone()[0], 1)
            self.assertEqual(restarted.execute("SELECT count(*) FROM approvals").fetchone()[0], 1)
            self.assertEqual(restarted.execute("SELECT count(*) FROM reviewed_amount_inputs").fetchone()[0], 2)
        self.client = create_app(self.path, self.source).test_client()
        self.post(CASH, self.send_path, 200)
        self.assertEqual(self.db.execute("SELECT amount_krw FROM mock_payment_attempts").fetchone()[0], 988_932)

    def test_bank_amount_and_monthly_holds_clear_independently(self):
        self.post(CASH, self.send_path, 200)
        first = self.db.execute("SELECT attempt_id FROM mock_payment_attempts").fetchone()[0]
        record_reply(self.db, CASH, "FAIL-1", first, 988_932, ACCOUNT, "failure")
        self.post(CASH, self.send_path, 200, expected_attempt=first)
        second = self.db.execute("SELECT attempt_id FROM mock_payment_attempts WHERE sequence_no=2").fetchone()[0]
        record_reply(self.db, CASH, "LATE-1", first, 988_932, ACCOUNT, "success")
        record_reply(self.db, CASH, "FAIL-2", second, 988_932, ACCOUNT, "failure")
        self.add_input(1_200_000)
        self.add_input(1_100_000, "TEST-ADDITIONAL")

        # A restarted server must still require three independent decisions.
        self.client = create_app(self.path, self.source).test_client()
        self.post(CASH, self.send_path, 400, expected_attempt=second)
        self.assertEqual(funding.overview(self.db)["reserved"], 988_932)
        version = self.db.execute("SELECT max(rowid) FROM mock_bank_replies").fetchone()[0]
        self.post(CASH, f"/payments/{self.iid}/bank-review", 200, decision="retry", reply_version=version,
                  evidence="두 시도 모두 은행의 미출금 확인서 대조")
        self.post(CASH, self.send_path, 400, expected_attempt=second)
        self.post(STAFF, self.review_path, 200, amount=988_932, reason="원금액을 증빙으로 확정", expected_input=1)
        self.post(CASH, self.send_path, 400, expected_attempt=second)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM cash_mock_debits").fetchone()[0], 0)

        self.post(STAFF, "/approvals/submit", 200, group="D-001", note="6월 추가 신청을 포함한 심사")
        approval = self.db.execute("SELECT max(approval_id) FROM approvals").fetchone()[0]
        self.post(USERS["executive"], f"/approvals/{approval}/decide", 200, action="approve", reason="합산 심사")
        self.post(CASH, self.send_path, 200, expected_attempt=second)
        third = self.db.execute("SELECT attempt_id FROM mock_payment_attempts WHERE sequence_no=3").fetchone()[0]
        self.assertEqual(record_reply(self.db, CASH, "PAID-3", third, 988_932, ACCOUNT, "success"), "matched")
        self.assertEqual(self.db.execute("SELECT amount FROM cash_mock_debits").fetchone()[0], 988_932)
