"""Explicit, billable/live integration test. Routine runs skip the model calls."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import SOURCE_ROOT, create_app
from ai_drafts import adopt_draft, generate_draft, list_drafts
from approvals import decide_approval, submit_approval
from corrections import apply_correction
from store import open_db, receive_month, sync_sources
from test_intake import FormToken, JUNE
from workflow_security import USERS


@unittest.skipUnless(os.environ.get("W07_TEST_LIVE_AI") == "1", "실제 모델 호출은 명시적으로 실행")
class LiveDraftTest(unittest.TestCase):
    def test_real_adopted_summary_and_new_proposal_after_registration_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "corrected-ai.sqlite3"
            with closing(open_db(path)) as db:
                sync_sources(db, SOURCE_ROOT)
                receive_month(db, JUNE)
                group = "D-001"
                memo = generate_draft(db, USERS["staff"], JUNE, group, "summary")
                original = list_drafts(db, JUNE, group)[0]
                edit = original["original_text"] + "\n담당자 검토 기록."
                adopt_draft(db, USERS["staff"], memo, edit)
                decision = submit_approval(db, USERS["staff"], JUNE, group, "단독 그룹 확인")
                decide_approval(db, USERS["lead"], decision, "approve", "팀 전결")
                values = dict(db.execute(
                    "SELECT representative_name,address FROM agencies WHERE agency_id='D-011'"
                ).fetchone())
                change = apply_correction(db, USERS["staff"], group, values, "2026-06-15",
                                          "시험 대표자·주소 정정", "시험 변경확인서")
                proposal = generate_draft(db, USERS["staff"], JUNE, group, "proposal")
            with closing(open_db(path)) as db:
                fresh, preserved = list_drafts(db, JUNE, group)
                self.assertEqual((preserved["original_text"], preserved["adopted_text"], preserved["correction_ids"]),
                                 (original["original_text"], edit, [change]))
                self.assertEqual((fresh["draft_id"], fresh["review_required"]), (proposal, False))
                packet = json.loads(fresh["evidence_json"])
                self.assertEqual((packet["monthly_total_krw"], packet["new_amount_krw"], packet["reapproval_amount_krw"]),
                                 (4_877_029, 3_888_097, 988_932))
                self.assertFalse(packet["review_memos"])
                self.assertIn("영업임원", fresh["original_text"])
                for amount in (4_877_029, 3_888_097, 988_932):
                    self.assertIn(str(amount), fresh["original_text"].replace(",", ""))
                for item in (preserved, fresh):
                    self.assertTrue(item["request_id"])
                    print("LIVE correction", item["kind"], item["model"], item["request_id"])

    def test_real_web_generation_edit_adoption_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ai-live.sqlite3"
            self.app = create_app(path, SOURCE_ROOT)
            self.client = self.app.test_client()
            token = FormToken()
            token.feed(self.client.get("/").text)
            self.csrf = token.value
            self.assertTrue(self.csrf)

            def post(route, **fields):
                values = {"csrf_token": self.csrf, "month": JUNE, "group": "D-011", **fields}
                reply = self.client.post(route, data=values, follow_redirects=True)
                self.assertEqual(reply.status_code, 200, reply.text.partition('role="alert">')[2][:300])
                return reply.text

            def generate(db, screen, kind, **fields):
                page = post(f"/{screen}/ai/generate", kind=kind, **fields)
                row = db.execute("SELECT * FROM ai_drafts WHERE kind=? ORDER BY draft_id DESC LIMIT 1",
                                 (kind,)).fetchone()
                self.assertIsNotNone(row)
                self.assertGreater(len(row["original_text"].strip()), 10)
                self.assertTrue(row["provider"] and row["model"] and row["request_id"])
                self.assertIsNone(row["adopted_text"])
                self.assertIn(row["model"], page)
                return row

            post("/intake")
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                question = generate(db, "cases", "question", rebate_id="RB-00120")
                revised_question = question["original_text"] + "\nD-024의 3월·4월 회수 건을 구분해 답변해 주세요."
                adopt_question = f"/cases/ai/{question['draft_id']}/adopt"
                post(adopt_question, edited_text=revised_question, required="1")
                post(adopt_question, edited_text=revised_question, required="1")
                case, = db.execute("SELECT * FROM explanations").fetchall()
                self.assertEqual(case["question"], revised_question)
                post(f"/cases/{case['case_id']}/reply", respondent="검증용 대리점 담당자",
                     answer="검증용 회신입니다. 공동 수금 약정으로 두 회수 건에 같은 계좌를 사용했습니다.",
                     evidence="검증용 증빙 A")
                post(f"/cases/{case['case_id']}/resolve", reason="검증용 회신 기록 확인. 그룹 합산 유지.")

                summary = generate(db, "cases", "summary")
                context = json.loads(summary["evidence_json"])
                self.assertEqual(context["monthly_total_krw"], 3_888_097)
                self.assertEqual(len(context["edges"]), 2)
                self.assertTrue(any("공동 수금 약정" in (item["answer"] or "") for item in context["cases"]))
                self.assertIn("DP-00013", summary["evidence_json"])
                revised_summary = summary["original_text"] + "\n검토 메모: 회수계좌와 지급 수취계좌는 별도로 확인한다."
                post(f"/cases/ai/{summary['draft_id']}/adopt", edited_text=revised_summary)

                proposal = generate(db, "approvals", "proposal")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)
                revised_proposal = proposal["original_text"] + "\n담당자 검토 완료. 영업임원에게 상신합니다."
                post(f"/approvals/ai/{proposal['draft_id']}/adopt", edited_text=revised_proposal)
                approval, = db.execute("SELECT * FROM approvals").fetchall()
                self.assertEqual((approval["note"], approval["status"], approval["monthly_total"], approval["new_amount"]),
                                 (revised_proposal, "승인 대기", 3_888_097, 3_888_097))
                memo, = json.loads(approval["snapshot"])["review_memos"]
                self.assertEqual((memo["draft_id"], memo["text"]), (summary["draft_id"], revised_summary))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 0)
                drafts = [dict(row) for row in db.execute(
                    "SELECT draft_id,kind,model,provider,request_id,original_text,adopted_text,adopted_by FROM ai_drafts ORDER BY draft_id"
                )]
                self.assertEqual(len(drafts), 3)
                for draft in drafts:
                    self.assertNotEqual(draft["original_text"], draft["adopted_text"])
                    self.assertEqual(draft["adopted_by"], "staff")
                    print("LIVE", draft["kind"], draft["provider"], draft["model"], draft["request_id"],
                          f"{len(draft['original_text'])}자", draft["original_text"], flush=True)

            # Re-open using a new Flask application and a fresh session.
            client = create_app(path, SOURCE_ROOT).test_client()
            for screen, original in (("cases", revised_question), ("approvals", revised_proposal)):
                page = client.get(f"/{screen}?month={JUNE}&group=D-011")
                self.assertEqual(page.status_code, 200)
                self.assertIn(original.splitlines()[-1], page.text)
