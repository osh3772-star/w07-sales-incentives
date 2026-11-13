import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from ai_drafts import adopt_draft, generate_draft, list_drafts
from approvals import decide_approval, list_approvals, list_cases, reply_case, resolve_case, submit_approval
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


MONTH, GROUP, TARGET = "2026-06", "D-011", "RB-00120"
STAFF, LEAD = USERS["staff"], USERS["lead"]
SOURCE = Path(__file__).resolve().parents[1] / "01_참가자_배포용/04_원본데이터/01_corevia"


def mock_writer(kind, context):
    return {"provider": "test-stub", "model": "mock", "request_id": "mock-" + kind,
            "text": f"모의 초안 {kind} · {context['monthly_total_krw']}원"}


class AIDraftsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "workflow.sqlite3"
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE)
        receive_month(self.db, MONTH)

    def generate(self, kind, rebate=None, provider=mock_writer):
        return generate_draft(self.db, STAFF, MONTH, GROUP, kind, rebate, provider=provider)

    def test_human_edits_saved_to_three_business_records(self):
        question = self.generate("question", TARGET)
        self.assertFalse(list_cases(self.db, MONTH))
        opened = adopt_draft(self.db, STAFF, question, "회수 약정 사본을 보내주세요.", required=True)
        reply_case(self.db, STAFF, opened["case_id"], "시연 응답자", "공동 회수 약정에 따라 입금했습니다.", "회신 문서 A")
        resolve_case(self.db, LEAD, opened["case_id"], "공동 회수 증빙 확인")
        summary = self.generate("summary")
        adopt_draft(self.db, LEAD, summary, "검토된 그룹 근거와 공동 회수 약정")
        proposal = self.generate("proposal")
        created = adopt_draft(self.db, STAFF, proposal, "6월 3,888,097원 임원 심사 요청")
        with closing(open_db(self.path)) as reopened:
            drafts = list_drafts(reopened, MONTH, GROUP)
            self.assertEqual([d["draft_id"] for d in drafts], [proposal, summary, question])
            self.assertTrue(all(d["provider"] == "test-stub" and d["adopted_at"] for d in drafts))
            self.assertEqual(list_cases(reopened, MONTH)[0]["question"], opened["adopted_text"])
            [approval] = list_approvals(reopened, MONTH)
            self.assertEqual((approval["approval_id"], approval["note"], approval["status"]),
                             (created["approval_id"], created["adopted_text"], "승인 대기"))
            self.assertEqual(approval["snapshot"]["review_memos"][0]["text"], drafts[1]["adopted_text"])
            self.assertNotEqual(drafts[1]["adopted_text"], drafts[1]["original_text"])
            self.assertEqual(approval["snapshot"]["review_memos"][0]["reviewer"], LEAD.user_id)
            self.assertEqual(reopened.execute("SELECT count(*) FROM payment_instructions").fetchone()[0], 0)

    def test_untrusted_narrative_and_masked_evidence(self):
        row = self.db.execute("SELECT * FROM agencies WHERE agency_id='D-024'").fetchone()
        cid = adopt_draft(self.db, STAFF, self.generate("question", TARGET), "사유 확인")["case_id"]
        narrative = "공동 회수 약정 확인. " + " ".join(row[k] for k in (
            "representative_name", "address", "business_reg_no", "bank_account_no"))
        respondent = "별도회신주체"
        reply_case(self.db, STAFF, cid, respondent,
                   narrative + " " + respondent + " 010-1234-5678 sales@example.com", "원본 증빙 업체정보")
        captured = []

        def writer(kind, packet):
            self.assertFalse(self.db.in_transaction)
            captured.append(packet)
            return mock_writer(kind, packet)

        self.generate("summary", provider=writer)
        [packet] = captured
        text = json.dumps(packet, ensure_ascii=False)
        self.assertIn("공동 회수 약정 확인", text)
        self.assertIn("DP-00013", text)
        self.assertEqual(packet["edges"][0]["keys"], ["대표자", "주소"])
        self.assertEqual(packet["monthly_total_krw"], 3_888_097)
        for name in ("address", "representative_name", "business_reg_no", "bank_account_no"):
            self.assertNotIn(row[name], text)
        for private in (respondent, "010-1234-5678", "sales@example.com", "원본 증빙 업체정보"):
            self.assertNotIn(private, text)

    def test_stale_and_failed_requests_leave_no_partial_records(self):
        refused = self.generate("proposal")
        self.generate("summary")
        adopt_draft(self.db, STAFF, self.generate("question", TARGET), "근거 확인", required=True)
        with self.assertRaisesRegex(ValueError, "바뀌었"):
            adopt_draft(self.db, STAFF, refused, "기존 초안 검토")
        newest = self.generate("proposal")
        with self.assertRaisesRegex(ValueError, "필수 소명"):
            adopt_draft(self.db, STAFF, newest, "필수 소명 대기")
        with self.assertRaises(ValueError):
            self.generate("question", "RB-00006")
        with self.assertRaises(PermissionError):
            adopt_draft(self.db, USERS["treasury"], newest, "상신")
        with self.assertRaises(ValueError):
            adopt_draft(self.db, STAFF, newest, "상신", allowed_kinds=("summary",))
        with self.assertRaises(ValueError):
            adopt_draft(self.db, STAFF, newest, "상신", group="D-001")
        self.assertTrue(all(d["adopted_at"] is None for d in list_drafts(self.db, MONTH, GROUP) if d["kind"] == "proposal"))
        self.assertFalse(list_approvals(self.db, MONTH))
        before = len(list_drafts(self.db, MONTH, GROUP))
        def offline(kind, evidence):
            raise ConnectionError("모의 연결 거부")
        with self.assertRaises(ConnectionError):
            self.generate("summary", provider=offline)
        self.assertEqual(len(list_drafts(self.db, MONTH, GROUP)), before)

    def test_double_adopt_and_atomic_rollback(self):
        draft_id = self.generate("question", TARGET)
        def adopt(_):
            with closing(open_db(self.path)) as conn:
                return adopt_draft(conn, STAFF, draft_id, "대리점에 증빙 요청")["case_id"]
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(adopt, range(2))
        self.assertEqual(first, second)
        self.assertEqual(len(list_cases(self.db, MONTH)), 1)
        other = self.generate("question", TARGET)
        self.db.execute("""CREATE TRIGGER adoption_failure BEFORE UPDATE ON ai_drafts
                           BEGIN SELECT RAISE(ABORT, '테스트 저장 실패'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            adopt_draft(self.db, STAFF, other, "실패 회차")
        self.assertEqual(len(list_cases(self.db, MONTH)), 1)

    def test_v2_pending_snapshot_still_valid(self):
        pending = submit_approval(self.db, STAFF, MONTH, "D-001", "기존 결재")
        self.assertNotIn("review_memos", list_approvals(self.db, MONTH)[0]["snapshot"])
        self.db.execute("DROP TABLE ai_drafts")
        self.db.execute("PRAGMA user_version=2")
        with closing(open_db(self.path)) as migrated:
            decide_approval(migrated, LEAD, pending, "approve", "이관 후 결정")
            self.assertEqual(list_approvals(migrated, MONTH)[0]["status"], "승인")
