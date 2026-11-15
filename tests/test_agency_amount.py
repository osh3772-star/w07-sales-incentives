import csv
import json
from contextlib import closing
from html.parser import HTMLParser
from unittest.mock import Mock

import funding
from ai_drafts import generate_draft
from amount_reviews import has_open_issue
from approvals import decide_approval, submit_approval
from payments import confirm_account, record_reply
from rules import month_overview
from store import open_db
from test_ai_drafts import mock_writer
from test_amount_reviews import AmountReviewFixture, CASH, LEAD, STAFF
from test_intake import running_server
from test_workflow_http import Session
from workflow_security import USERS


JULY, AUGUST, DUE = "2026-07", "2026-08", "2026-09-10"
REVIEW = "/intake/TH-X/resolve-amount"
OLD_ACCOUNT, NEW_ACCOUNT = "시험은행 700-011", "시험은행 700-037"


class PageText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts = []
        self.feed(html)

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return " ".join("".join(self.parts).split())


class AgencyLedgerFixture(AmountReviewFixture):
    def append(self, rid, agency, month, amount):
        with self.ledger.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle, delimiter="|", lineterminator="\n").writerow(
                [rid, agency, month, 5_000_000, amount, "미지급", "NONE", "NONE"]
            )


class BeforeFirstApprovalTests(AgencyLedgerFixture):
    def receive_conflict(self, original, alternative):
        self.append("RAW-X", "D-011", JULY, original)
        self.append("RAW-Y", "D-024", JULY, 775_000)
        self.post(STAFF, "/intake", 200, month=JULY)
        self.append("RAW-X", "D-037", JULY, alternative)
        for _ in range(2):
            self.post(STAFF, "/intake", 200, month=JULY)
        self.assertEqual(self.db.execute("SELECT count(*) FROM rebates WHERE period_ym=?", (JULY,)).fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM open_intake_issues WHERE rebate_id='RAW-X'").fetchone()[0], 1)

    def assert_candidate_range(self, low, high, authority):
        query = f"?month={JULY}&group=D-011"
        bounds = f"{low:,}원 ~ {high:,}원"
        intake = PageText(self.client.get("/" + query).text).text()
        self.assertIn(bounds, intake)
        self.assertIn(authority, intake.split(bounds, 1)[1].split("판정 근거", 1)[0])
        approval = PageText(self.client.get("/approvals" + query).text).text()
        self.assertIn(bounds, approval)
        self.assertIn(f"승인권자 {authority}", approval)

        writer = Mock(wraps=mock_writer)
        draft_id = generate_draft(self.db, STAFF, JULY, "D-011", "proposal", provider=writer)
        kind, evidence = writer.call_args.args
        self.assertEqual(kind, "proposal")
        self.assertEqual(evidence["unresolved_monthly_range_krw"], [low, high])
        self.assertEqual(evidence["authority"], authority)
        self.assertIsNone(evidence["monthly_total_krw"])
        self.assertIsNone(evidence["new_amount_krw"])
        self.assertEqual(evidence["booked_monthly_total_krw"], 775_000)
        self.assertCountEqual([line["rebate_id"] for line in evidence["lines"]], ["RAW-X", "RAW-Y"])

        self.post(STAFF, "/approvals/submit", 400, month=JULY, group="D-011", note="미해결 후보 상신 시도")
        self.post(STAFF, f"/approvals/ai/{draft_id}/adopt", 400, month=JULY,
                  group="D-011", edited_text="미확정 후보로 결재안 상신 시도")
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM payment_instructions").fetchone()[0], 0)
        response = self.post(CASH, "/payments/1/send", 400, month=JULY)
        self.assertIn("지급지시를 찾을 수 없습니다", response)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)

    def test_crossing_limit_waits_until_original_is_confirmed_and_submitted(self):
        self.receive_conflict(1_300_001, 1_150_000)
        self.assert_candidate_range(1_925_000, 2_075_001, "확정 대기")
        query = f"?month={JULY}&group=D-011"
        for _ in range(2):
            with running_server(self.path) as url:
                session = Session(url)
                for path in ("/", "/approvals"):
                    status, html = session.get(path + query)
                    self.assertEqual(status, 200)
                    page = PageText(html).text()
                    self.assertIn("1,925,000원 ~ 2,075,001원", page)
                    self.assertIn("승인권자 확정 대기", page)
                self.assertEqual(session.post("/approvals/submit", month=JULY, group="D-011", note="후보 대기")[0], 400)
        self.post(STAFF, "/intake/RAW-X/resolve-amount", 200, month=JULY, confirmed_agency_id="D-011",
                  amount=1_300_001, reason="최초 정산서의 귀속과 금액을 대조", expected_input=1)
        self.post(STAFF, "/approvals/submit", 200, month=JULY, group="D-011", note="확정한 원본 상신")
        doc = self.db.execute("SELECT * FROM approvals").fetchone()
        self.assertEqual((doc["authority"], doc["monthly_total"], doc["new_amount"], doc["status"]),
                         ("영업임원", 2_075_001, 2_075_001, "승인 대기"))
        self.assertCountEqual([(r["rebate_id"], r["agency_id"], r["amount"]) for r in json.loads(doc["snapshot"])["lines"]],
                              [("RAW-X", "D-011", 1_300_001), ("RAW-Y", "D-024", 775_000)])
        self.post(LEAD, f"/approvals/{doc['approval_id']}/decide", 403, month=JULY, action="approve", reason="권한 확인")

    def test_candidates_up_to_two_million_show_team_and_remain_blocked(self):
        self.receive_conflict(1_225_000, 1_150_000)
        self.assert_candidate_range(1_925_000, 2_000_000, "영업지원팀")

    def test_candidates_above_two_million_show_executive_and_remain_blocked(self):
        self.receive_conflict(1_300_001, 1_225_001)
        self.assert_candidate_range(2_000_001, 2_075_001, "영업임원")


class AgencyAmountTests(AgencyLedgerFixture):
    def setUp(self):
        super().setUp()
        self.append("TH-X", "D-011", JULY, 1_150_000)
        self.append("TH-Y", "D-024", JULY, 775_000)
        self.append("AUG", "D-037", AUGUST, 50_019)
        self.append("OTHER", "D-001", JULY, 32_123)
        for month in (JULY, AUGUST):
            self.post(STAFF, "/intake", 200, month=month)
        funding.set_balance(self.db, CASH, 3_000_000, "2026-07-01", "검증용 잔액 증빙", "원화 전용")
        self.orders = {}
        for month, group in ((JULY, "D-011"), (JULY, "D-001"), (AUGUST, "D-011")):
            aid = submit_approval(self.db, STAFF, month, group, "당월 명세 상신")
            decide_approval(self.db, LEAD, aid, "approve", "원본 명세 팀 전결")
            for rid, iid in self.db.execute(
                "SELECT rebate_id,instruction_id FROM payment_instructions WHERE approval_id=?", (aid,),
            ):
                self.orders[rid] = iid
                account = OLD_ACCOUNT if rid == "TH-X" else f"시험은행 900-{iid:03d}"
                confirm_account(self.db, CASH, iid, account, "최초 수취계좌 증빙")
                funding.schedule(self.db, CASH, iid, DUE, "지급일 확인")
        self.assertCountEqual(funding.allocate(self.db, CASH, DUE), self.orders.values())
        self.initial = self.db.execute(
            "SELECT * FROM approvals WHERE period_ym=? AND group_id='D-011'", (JULY,),
        ).fetchone()
        self.assertEqual((self.initial["decided_by"], self.initial["monthly_total"]), ("lead", 1_925_000))

    def dispute(self):
        self.append("TH-X", "D-037", JULY, 1_350_000)
        for _ in range(2):
            self.post(STAFF, "/intake", 200, month=JULY)
        self.assertEqual(self.db.execute("SELECT count(*) FROM open_intake_issues WHERE rebate_id='TH-X'").fetchone()[0], 1)

    def route(self, rid):
        return f"/payments/{self.orders[rid]}/send"

    def pay(self, rid, amount, account=None):
        iid = self.orders[rid]
        account = account or f"시험은행 900-{iid:03d}"
        self.post(CASH, self.route(rid), 200, month=AUGUST if rid == "AUG" else JULY)
        attempt = self.db.execute("SELECT * FROM mock_payment_attempts WHERE instruction_id=?", (iid,)).fetchone()
        self.assertEqual((attempt["amount_krw"], attempt["account"]), (amount, account))
        self.assertEqual(record_reply(self.db, CASH, rid+"-OK", attempt["attempt_id"], amount, account, "success"), "matched")

    def select_input(self, agency, amount, status=200):
        self.post(STAFF, REVIEW, status, month=JULY, amount=amount, confirmed_agency_id=agency,
                  expected_input=1, reason=f"{agency} 귀속과 금액을 신청서·계약서와 대조")

    def test_group_peer_cannot_send_and_new_payee_needs_executive_funds_and_account(self):
        self.dispute()
        with running_server(self.path) as url:
            session = Session(url)
            self.assertEqual(session.post("/identity", user_id="treasury", next="/")[0], 200)
            for rid in ("TH-Y", "TH-X"):
                self.assertEqual(session.post(self.route(rid), month=JULY)[0], 400)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM amount_decisions").fetchone()[0], 0)
        group = next(g for g in month_overview(self.db, JULY)["groups"] if g["group_id"] == "D-011")
        self.assertEqual(tuple(group["disputed_range"]), (1_925_000, 2_125_000))
        self.assertEqual(group["authority"], "확정 대기")
        active = self.db.execute(
            "SELECT agency_id,rebate_amount_krw FROM effective_rebates WHERE rebate_id='TH-X'"
        ).fetchall()
        self.assertEqual([tuple(r) for r in active], [("D-011", 1_150_000)])
        self.pay("AUG", 50_019)
        self.pay("OTHER", 32_123)
        self.assertEqual(funding.overview(self.db)["reserved"], 1_925_000)
        self.post(STAFF, REVIEW, 400, month=JULY, expected_input=1, amount=1_350_000, reason="금액만 확인")
        self.select_input("D-011", 1_350_000, 400)
        self.select_input("D-037", 1_350_000)
        self.select_input("D-037", 1_350_000, 400)
        self.post(STAFF, "/intake", 200, month=JULY)
        self.assertFalse(has_open_issue(self.db, "TH-X"))
        decision = self.db.execute(
            """SELECT d.decided_by,d.reason,a.previous_agency_id,a.confirmed_agency_id,d.confirmed_amount
               FROM amount_decisions d JOIN amount_agency_decisions a USING(decision_id)
               WHERE d.rebate_id='TH-X'"""
        ).fetchall()
        self.assertEqual([tuple(r) for r in decision],
                         [("staff", "D-037 귀속과 금액을 신청서·계약서와 대조", "D-011", "D-037", 1_350_000)])
        for rid in ("TH-X", "TH-Y"):
            self.post(CASH, self.route(rid), 400, month=JULY)

        aid = submit_approval(self.db, STAFF, JULY, "D-011", "확정된 대상과 금액으로 재상신")
        approval = self.db.execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone()
        snapshot = json.loads(approval["snapshot"])
        self.assertIn("D-037 귀속과 금액을 신청서·계약서와 대조", json.dumps(snapshot, ensure_ascii=False))
        self.assertCountEqual([(r["rebate_id"], r["agency_id"], r["amount"]) for r in snapshot["lines"]],
                              [("TH-X", "D-037", 1_350_000), ("TH-Y", "D-024", 775_000)])
        self.assertEqual((approval["monthly_total"], approval["authority"], approval["new_amount"],
                          snapshot["reapproval_amount"]), (2_125_000, "영업임원", 0, 2_125_000))
        self.post(LEAD, f"/approvals/{aid}/decide", 403, month=JULY, action="approve", reason="팀 재결재 시도")
        self.post(USERS["executive"], f"/approvals/{aid}/decide", 200, month=JULY, action="approve", reason="명세 확인")
        self.assertEqual(funding.overview(self.db)["reserved"], 775_000)
        self.assertEqual(funding.allocate(self.db, CASH, DUE), [self.orders["TH-X"]])
        self.post(CASH, self.route("TH-X"), 400, month=JULY)
        account_path = f"/payments/{self.orders['TH-X']}/account"
        self.post(CASH, account_path, 400, month=JULY, account=OLD_ACCOUNT, evidence="최초 수취계좌 증빙",
                  expected_agency_id="D-011")
        self.post(CASH, self.route("TH-X"), 400, month=JULY)
        self.post(CASH, account_path, 200, month=JULY, account=NEW_ACCOUNT,
                  evidence="확정된 D-037 수취계좌 확인서", expected_agency_id="D-037")
        self.pay("TH-X", 1_350_000, NEW_ACCOUNT)
        self.pay("TH-Y", 775_000)
        self.assertEqual(self.db.execute("SELECT count(*),sum(amount) FROM cash_mock_debits").fetchone()[:],
                         (4, 2_125_000 + 50_019 + 32_123))
        self.assertEqual(self.db.execute("SELECT * FROM approvals WHERE approval_id=?",
                                         (self.initial["approval_id"],)).fetchone(), self.initial)
        self.assertEqual(self.db.execute("SELECT count(*) FROM payment_instructions").fetchone()[0], 4)
        self.assertEqual(self.db.execute("SELECT agency_id,rebate_amount_krw FROM rebates WHERE rebate_id='TH-X'").fetchone()[:],
                         ("D-011", 1_150_000))
        self.assertEqual(self.db.execute("SELECT agency_id,amount_krw FROM payment_instructions WHERE rebate_id='TH-X'").fetchone()[:],
                         ("D-037", 1_350_000))

    def test_original_input_resumes_team_payment_and_other_bank_hold_survives(self):
        iid = self.orders["TH-Y"]
        self.post(CASH, self.route("TH-Y"), 200, month=JULY)
        tid = self.db.execute("SELECT attempt_id FROM mock_payment_attempts WHERE instruction_id=?", (iid,)).fetchone()[0]
        account = f"시험은행 900-{iid:03d}"
        record_reply(self.db, CASH, "TH-Y 실패", tid, 775_000, account, "failure")
        record_reply(self.db, CASH, "TH-Y 지연 성공", tid, 775_000, account, "success")
        self.dispute()
        self.select_input("D-011", 1_150_000)
        self.pay("TH-X", 1_150_000, OLD_ACCOUNT)
        self.post(CASH, self.route("TH-Y"), 400, month=JULY, expected_attempt=tid)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mock_payment_attempts WHERE instruction_id=?", (iid,)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals WHERE period_ym=? AND group_id='D-011'", (JULY,)).fetchone()[0], 1)

    def test_completed_payment_keeps_original_payee_and_debit(self):
        self.pay("TH-X", 1_150_000, OLD_ACCOUNT)
        iid = self.orders["TH-X"]
        query = """SELECT i.agency_id,i.state,t.attempt_id,t.account,r.reply_id,d.amount FROM payment_instructions i
                   JOIN mock_payment_attempts t USING(instruction_id) JOIN mock_bank_replies r USING(attempt_id)
                   JOIN cash_mock_debits d USING(instruction_id,reply_id) WHERE i.instruction_id=?"""
        original = self.db.execute(query, (iid,)).fetchone()
        self.dispute()
        self.select_input("D-037", 1_350_000)
        with closing(open_db(self.path)) as reopened:
            self.assertEqual(reopened.execute(query, (iid,)).fetchone(), original)
            row = next(r for r in month_overview(reopened, JULY)["rows"] if r["rebate_id"] == "TH-X")
            self.assertEqual((row["agency_id"], row["amount"], row["bucket"]), ("D-011", 1_150_000, "paid_amount"))
