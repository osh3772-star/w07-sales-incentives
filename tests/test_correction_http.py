import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from test_intake import JUNE, running_server
from test_workflow_http import Session


class CorrectionHttpTest(unittest.TestCase):
    def post(self, url, expected=200, **fields):
        status, html = self.browser.post(url, **{"group": "D-001", **fields})
        self.assertEqual(status, expected, html.partition('role="alert">')[2][:250])
        return html

    def user(self, name):
        self.post("/identity", user_id=name, next="/")

    def screen(self, path, group="D-001"):
        status, body = self.browser.get(f"/{path}?month={JUNE}&group={group}")
        self.assertEqual(status, 200)
        return body

    def test_team_approval_through_correction_reapproval_and_bank_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "review.sqlite3"
            with running_server(database) as url, closing(sqlite3.connect(database)) as db:
                self.browser = Session(url)
                self.post("/intake")
                self.post("/approvals/submit", note="6월 단독 그룹 검토")
                self.user("lead")
                self.post("/approvals/1/decide", action="approve", reason="988,932원 전결")
                original = db.execute("SELECT * FROM approvals WHERE approval_id=1").fetchone()
                account = "시험은행 123-0001"
                self.user("treasury")
                self.post("/payments/1/account", account=account, evidence="시험 계좌확인서")
                self.assertIn('action="/payments/1/send"', self.screen("payments"))
                rep, address = db.execute(
                    "SELECT representative_name,address FROM agencies WHERE agency_id='D-011'"
                ).fetchone()
                changes = dict(agency_id="D-001", effective_date="2026-06-15", expected_version="0",
                               representative_name=rep, address=address, reason="모의 대표자 및 주소 정정",
                               evidence="시험 변경확인서")
                self.post("/cases/correct", 403, **changes)
                self.user("staff")
                history = self.post("/cases/correct", **changes)
                self.assertIn("4,877,029원", history)
                self.assertIn("3,888,097원", history)
                self.assertIn("988,932원", history)
                self.assertIn("모의 대표자 및 주소 정정", history)
                self.post("/cases/correct", 400, **changes)
                self.assertEqual(db.execute("SELECT count(*) FROM agency_corrections").fetchone()[0], 1)
                self.user("treasury")
                queue = self.screen("payments")
                self.assertIn("정정 지급 보류", queue)
                self.assertNotIn('action="/payments/1/send"', queue)
                self.post("/payments/1/send", 400)
                self.assertEqual(db.execute("SELECT count(*) FROM mock_payment_attempts").fetchone()[0], 0)

            with running_server(database) as url, closing(sqlite3.connect(database)) as db:
                self.browser = Session(url)
                group_page = self.screen("approvals", "D-011")
                self.assertIn("영업임원 재상신", group_page)
                self.assertIn("승인 근거 재검토 필요", group_page)
                self.post("/approvals/submit", note="재승인 건 및 신규 3건 함께 검토")
                total, new, evidence = db.execute(
                    "SELECT monthly_total,new_amount,snapshot FROM approvals WHERE approval_id=2"
                ).fetchone()
                self.assertEqual((total, new, json.loads(evidence)["reapproval_amount"]),
                                 (4_877_029, 3_888_097, 988_932))
                self.user("lead")
                self.post("/approvals/2/decide", 403, action="approve", reason="권한 경계 검증")
                self.user("executive")
                self.post("/approvals/2/decide", action="approve", reason="정정 근거와 누계 확인")
                self.assertEqual(original, db.execute("SELECT * FROM approvals WHERE approval_id=1").fetchone())
                self.assertEqual(db.execute(
                    "SELECT count(*),count(DISTINCT rebate_id),sum(amount_krw) FROM payment_instructions"
                ).fetchone(), (4, 4, 4_877_029))
                self.user("treasury")
                self.assertIn('action="/payments/1/send"', self.screen("payments"))
                self.post("/payments/1/send")
                attempt, = db.execute("SELECT attempt_id FROM mock_payment_attempts").fetchone()
                self.user("staff")
                self.post("/cases/correct", agency_id="D-001", expected_version="1", effective_date="2026-06-16",
                          representative_name="시험 분리 대표", reason="전송 후 다시 분리", evidence="시험 변경통보")
                self.user("treasury")
                queue = self.screen("payments")
                self.assertIn(f"시도 {attempt}의 금액", queue)
                self.assertNotIn('action="/payments/1/send"', queue)
                self.post("/payments/1/send", 400)
                reply = dict(reply_id="시험-001", attempt_id=attempt, amount="988932", account=account, result="success")
                self.post("/payments/reply", **reply)
                self.post("/payments/reply", **reply)
                task_id, = db.execute(
                    "SELECT task_id FROM correction_tasks WHERE instruction_id=1 AND kind='post_review'"
                ).fetchone()
                self.assertIn("사후 검토 대기", self.screen("payments"))
                self.user("staff")
                self.post(f"/payments/tasks/{task_id}/complete", 403, note="권한 검사")
                self.user("treasury")
                self.post(f"/payments/tasks/{task_id}/complete", note="기존 지시와 모의 성공 회신 확인")
                self.assertIn("기존 지시와 모의 성공 회신 확인", self.screen("payments"))
                self.assertEqual(db.execute("SELECT state FROM payment_instructions WHERE instruction_id=1").fetchone(),
                                 ("completed",))
                for table in ("mock_payment_attempts", "mock_bank_replies"):
                    self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
