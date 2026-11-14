import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import funding
import funding_ai as advice
from ai_provider import ProviderError
from approvals import decide_approval, submit_approval
from store import create_funding_database, open_db, receive_month, sync_sources
from workflow_security import USERS


SOURCE = Path(__file__).resolve().parents[1] / "01_참가자_배포용/04_원본데이터/01_corevia"
DUE, LATER, MONTH = "2026-07-10", "2026-07-13", "2026-06"
CASH, STAFF, EXEC = (USERS[k] for k in ("treasury", "staff", "executive"))


def fixture_model(kind, packet):
    assert kind == "funding_options"
    words = {k: packet[k] for k in ("instruction_id", "target_amount", "new_date")}
    words.update(funding="시험 회수 계획과 입금 확인", reschedule="시험 일정 협의와 신구 날짜 재계산")
    return dict(text=json.dumps(words, ensure_ascii=False), provider="모의 제공자", model="fixture",
                request_id=uuid4().hex)


class FundingAdviceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "planning.sqlite3"
        create_funding_database(self.path)
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE)
        receive_month(self.db, MONTH)
        approval = submit_approval(self.db, STAFF, MONTH, "D-011", "시험용 결재")
        decide_approval(self.db, EXEC, approval, "approve", "시험 승인")
        funding.set_balance(self.db, CASH, 3_000_000, "2026-07-08", "시험 자금일보", "예시 원화계좌")
        for line in funding.overview(self.db)["lines"]:
            funding.schedule(self.db, CASH, line["instruction_id"], DUE, "시험 예정일")
        funding.allocate(self.db, CASH, DUE)
        self.iid = next(l["instruction_id"] for l in funding.overview(self.db)["lines"] if l["agency_id"] == "D-037")

    def generate(self, provider=fixture_model):
        return advice.generate_options(self.db, CASH, self.iid, 900000, LATER, provider=provider)

    def test_move_after_staff_confirmation_no_cash_and_repeat_safe(self):
        pid = self.generate()
        proposal, = advice.list_proposals(self.db)
        self.assertEqual((proposal["shortage"], proposal["old_shortage_after_move"], proposal["new_shortage_after_move"]),
                         (888_097, 0, 888_097))
        with self.assertRaises(PermissionError):
            advice.adopt_option(self.db, STAFF, pid, "reschedule", "담당자 권한 검사")
        advice.adopt_option(self.db, CASH, pid, "reschedule", "협의 후 13일로 변경")
        advice.adopt_option(self.db, CASH, pid, "reschedule", "협의 후 13일로 변경")
        with self.assertRaises(ValueError):
            funding.schedule(self.db, CASH, self.iid, LATER, "직접 변경 시도")
        with self.assertRaises(ValueError):
            advice.finalize_reschedule(self.db, CASH, pid, "협의 전")
        self.assertEqual(funding.overview(self.db)["days"][0]["due_date"], DUE)
        tasks = advice.list_proposals(self.db)[0]["tasks"]
        self.assertEqual([t["owner_role"] for t in tasks], ["staff", "treasury"])
        with self.assertRaises(PermissionError):
            advice.respond_task(self.db, CASH, tasks[0]["task_id"], True, "권한 검사")
        for _ in range(2):
            advice.respond_task(self.db, STAFF, tasks[0]["task_id"], True, "시험 협의서 확인")
        for _ in range(2):
            advice.finalize_reschedule(self.db, CASH, pid, "협의 증빙 확인")
        with closing(open_db(self.path)) as db:
            view = funding.overview(db)
            self.assertEqual([(d["due_date"],d["total"],d["shortage"]) for d in view["days"]],
                             [(DUE, 2_562_561, 0), (LATER, 1_325_536, 888_097)])
            self.assertEqual(view["free"], 437_439)
            self.assertEqual(view["receipts"], [])
            p, = advice.list_proposals(db)
            self.assertEqual(p["status"], "done")
            self.assertEqual([t["status"] for t in p["tasks"]], ["done", "done"])

    def test_confirm_partial_receipts_atomically_and_only_once(self):
        pid = self.generate()
        advice.adopt_option(self.db, CASH, pid, "funding", "검토한 추가 자금 목표")
        p, = advice.list_proposals(self.db)
        self.assertEqual(p["received_amount"], 0)
        with self.assertRaises(ValueError):
            advice.adopt_option(self.db, CASH, pid, "reschedule", "다른 선택")
        advice.respond_task(self.db, STAFF, p["tasks"][0]["task_id"], True, "시연 회수예정")
        self.assertEqual(funding.overview(self.db)["days"][0]["shortage"], 888_097)
        def confirm(_):
            with closing(open_db(self.path)) as db:
                return advice.confirm_funding_receipt(db, CASH, pid, "TX1", 500000, "2026-07-09", "시험 증빙 1")
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(confirm, range(2))), [False, True])
        self.assertEqual(funding.overview(self.db)["days"][0]["shortage"], 388_097)
        self.assertEqual(advice.list_proposals(self.db)[0]["status"], "funding_pending")
        with self.assertRaises(ValueError):
            advice.confirm_funding_receipt(self.db, CASH, pid, "TX1", 900000, "2026-07-09", "다른 증빙")
        for _ in range(2):
            advice.confirm_funding_receipt(self.db, CASH, pid, "TX2", 400000, "2026-07-10", "시험 증빙 2")
        self.assertEqual(funding.allocate(self.db, CASH, DUE), [self.iid])
        view = funding.overview(self.db)
        self.assertEqual((view["confirmed"],view["reserved"],view["free"],len(view["receipts"])),
                         (3_900_000, 3_888_097, 11_903, 2))
        self.assertEqual(advice.list_proposals(self.db)[0]["received_amount"], 900000)

    def test_stale_basis_untrusted_provider_role_and_adoption_rollback(self):
        with self.assertRaises(PermissionError):
            advice.generate_options(self.db, STAFF, self.iid, 900000, LATER, provider=fixture_model)
        def invalid_provider(kind, packet):
            result = fixture_model(kind, packet)
            raw = json.loads(result["text"])
            raw["target_amount"] = 100
            return dict(result, text=json.dumps(raw))
        with self.assertRaises(ProviderError):
            self.generate(invalid_provider)
        self.assertEqual(advice.list_proposals(self.db), [])
        pid = self.generate()
        self.db.execute("""CREATE TRIGGER fail_task BEFORE INSERT ON funding_tasks WHEN NEW.kind='receipt'
                           BEGIN SELECT RAISE(ABORT,'모의 저장 실패'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            advice.adopt_option(self.db, CASH, pid, "funding", "원자적 저장 검증")
        p, = advice.list_proposals(self.db)
        self.assertIsNone(p["choice"])
        self.assertEqual(p["tasks"], [])
        funding.confirm_receipt(self.db, CASH, "SMALL", 1, "2026-07-09", "시험 현금 근거 변경")
        with self.assertRaisesRegex(ValueError, "바뀌"):
            advice.adopt_option(self.db, CASH, pid, "reschedule", "오래된 근거")

    def test_new_evidence_after_negotiation_retires_proposal_without_moving_due_date(self):
        pid = self.generate()
        advice.adopt_option(self.db, CASH, pid, "reschedule", "마감 전 조정")
        task = advice.list_proposals(self.db)[0]["tasks"][0]["task_id"]
        advice.respond_task(self.db, STAFF, task, 1, "일정 재협의 완료")
        funding.confirm_receipt(self.db, CASH, "NEW-EVIDENCE", 500000, "2026-07-09", "계획 후 새 확인금")
        with self.assertRaisesRegex(ValueError, "근거가 바뀌"):
            advice.finalize_reschedule(self.db, CASH, pid, "기존 협의 완료")
        p, = advice.list_proposals(self.db)
        self.assertEqual(p["status"], "stale")
        self.assertEqual([t["status"] for t in p["tasks"]], ["done", "cancelled"])
        self.assertEqual(funding.overview(self.db)["days"][0]["due_date"], DUE)
        self.assertNotEqual(self.generate(), pid)
