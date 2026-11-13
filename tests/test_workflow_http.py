import http.cookiejar
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

from test_intake import FormToken, JUNE, running_server


class Session:
    def __init__(self, url):
        self.url = url
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = None
        self.get("/")

    def _open(self, path, data=None):
        try:
            response = self.browser.open(self.url + path, data, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            html = response.read().decode()
            token = FormToken()
            token.feed(html)
            if token.value:
                self.token = token.value
            return response.status, html

    def get(self, path):
        return self._open(path)

    def post(self, path, **fields):
        data = {"csrf_token": self.token, "month": JUNE, **fields}
        return self._open(path, urllib.parse.urlencode(data).encode())


class WorkflowHttpTest(unittest.TestCase):
    def test_real_cases_authority_payment_reconciliation_and_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.sqlite3"
            with running_server(database) as url, closing(sqlite3.connect(database)) as db:
                browser = Session(url)

                def post(path, status=200, **fields):
                    result, body = browser.post(path, **fields)
                    message = body.partition('role="alert">')[2].partition("</div>")[0]
                    self.assertEqual(result, status, f"{path}: {message}")
                    return body

                def sign_in(user):
                    post("/identity", user_id=user, next="/")

                def scalar(sql, params=()):
                    return db.execute(sql, params).fetchone()[0]

                def submit(group):
                    post("/approvals/submit", group=group, note="정산월 원본과 관련자 명세 확인")
                    return scalar("SELECT max(approval_id) FROM approvals WHERE group_id=?", (group,))

                post("/intake")
                post("/cases/request", rebate_id="RB-00120", question="등록·회수계좌 공유 사유 확인",
                     required="1")
                # A required case prevents submission until the recorded response has been reviewed.
                post("/approvals/submit", 400, group="D-011", note="소명 미완료 검증")
                post("/cases/1/reply", respondent="D-024 시연 담당자", answer="공동 회수 약정 확인",
                     evidence="시연용 증빙 A")
                post("/cases/1/resolve", reason="증빙을 검토했고 관련자 합산은 유지")
                normal = submit("D-001")
                grouped = submit("D-011")
                normal_decision = f"/approvals/{normal}/decide"
                group_decision = f"/approvals/{grouped}/decide"
                # A posted role or signer cannot replace the server's signed session identity.
                post(normal_decision, 403, action="approve", reason="권한 검사", role="lead", user_id="lead")
                sign_in("lead")
                post(group_decision, 403, action="approve", reason="초과 권한 검사", role="executive")
                self.assertEqual(scalar("SELECT count(*) FROM payment_instructions"), 0)
                post(normal_decision, action="approve", reason="팀 전결 한도 내 승인")
                sign_in("executive")
                post(group_decision, action="approve", reason="관련자 세 건 합산 및 소명 확인")
                repeat_status, _ = browser.post(group_decision, action="approve", reason="관련자 세 건 합산 및 소명 확인")
                self.assertIn(repeat_status, (200, 400, 409))
                self.assertEqual(db.execute(
                    "SELECT monthly_total, new_amount, decided_by FROM approvals WHERE approval_id=?", (grouped,)
                ).fetchone(), (3_888_097, 3_888_097, "executive"))
                self.assertEqual(db.execute(
                    "SELECT count(*), count(DISTINCT rebate_id), sum(amount_krw) FROM payment_instructions"
                ).fetchone(), (4, 4, 4_877_029))
                order = scalar("SELECT instruction_id FROM payment_instructions WHERE rebate_id='RB-00006'")
                send = f"/payments/{order}/send"
                post(send, 403)
                sign_in("treasury")
                post(send, 400)
                self.assertEqual(scalar("SELECT count(*) FROM mock_payment_attempts"), 0)
                account = "시연은행 900-0001"
                post(f"/payments/{order}/account", account=account, evidence="시연 수취계좌 확인서 A")
                post(send)
                post(send)
                self.assertEqual(scalar("SELECT count(*) FROM mock_payment_attempts"), 1)
                attempt = scalar("SELECT attempt_id FROM mock_payment_attempts")
                post("/payments/reply", reply_id="MOCK-REPLY-1", attempt_id=attempt, amount="988932",
                     account="시연은행 900-0002", result="success")
                self.assertEqual(scalar("SELECT state FROM payment_instructions WHERE instruction_id=?", (order,)), "review")
                fields = dict(reply_id="MOCK-REPLY-2", attempt_id=attempt, amount="988932",
                              account=account, result="success")
                post("/payments/reply", **fields)
                post("/payments/reply", **fields)
                self.assertEqual(scalar("SELECT count(*) FROM mock_bank_replies"), 2)
                self.assertEqual(sorted(db.execute(
                    "SELECT state, count(*) FROM payment_instructions GROUP BY state"
                ).fetchall()), [("completed", 1), ("held", 3)])
                tables = ("approvals", "approval_lines", "explanations", "payment_instructions",
                          "mock_payment_attempts", "mock_bank_replies")
                stored = {name: db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                          for name in tables}

            with running_server(database) as url, closing(sqlite3.connect(database)) as db:
                browser = Session(url)
                for path in ("/cases", "/approvals", "/payments"):
                    status, _ = browser.get(f"{path}?month={JUNE}&group=D-011")
                    self.assertEqual(status, 200)
                for name, records in stored.items():
                    self.assertEqual(db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall(), records)
                self.assertEqual(db.execute("SELECT count(*) FROM payment_instructions").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT state FROM payment_instructions WHERE instruction_id=?", (order,)).fetchone()[0],
                                 "completed")
                self.assertEqual(db.execute("SELECT count(*) FROM mock_bank_replies").fetchone()[0], 2)
