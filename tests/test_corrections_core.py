import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from corrections import agency_versions, apply_correction, complete_post_review, list_corrections, list_tasks
from rules import effective_agencies, month_overview
from store import open_db, receive_month, sync_sources
from workflow_security import USERS


SOURCE = Path(__file__).resolve().parents[1] / "01_참가자_배포용/04_원본데이터/01_corevia"
JUNE = "2026-06"


class RegistrationCorrections(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "settlement.sqlite"
        self.db = open_db(self.path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE)
        receive_month(self.db, JUNE)
        self.initial = self.master("2026-06-30")
        source = self.db.execute("SELECT * FROM agencies WHERE agency_id='D-011'").fetchone()
        self.join = {key: source[key] for key in ("representative_name", "address")}

    def master(self, at):
        return next(a for a in effective_agencies(self.db, at) if a["agency_id"] == "D-001")

    def change(self, values, day, **kw):
        return apply_correction(self.db, USERS["staff"], "D-001", values, day, "약정 정정", "시험용 증빙", **kw)

    def test_month_end_range_field_patches_and_backdated_split(self):
        # Register a July address change first, then enter the two-field June merger retroactively.
        self.change({"address": "7월 적용 주소"}, "2026-07-01")
        change_id = self.change(self.join, "2026-06-02", expected_version=1)
        self.assertEqual(self.master("2026-06-01"), self.initial)
        june = self.master(JUNE)
        later = self.master("2026-07")
        self.assertEqual((june["address"], june["representative_name"]),
                         (self.join["address"], self.join["representative_name"]))
        self.assertEqual((later["address"], later["representative_name"]), ("7월 적용 주소", self.join["representative_name"]))
        record = next(i for i in list_corrections(self.db, JUNE, "D-011") if i["correction_id"] == change_id)
        self.assertEqual((record["effective_date"], record["created_by"], record["reason"], record["evidence"]),
                         ("2026-06-02", "staff", "약정 정정", "시험용 증빙"))
        self.assertEqual(record["before_values"], {k: self.initial[k] for k in self.join})
        self.assertEqual(record["after_values"], self.join)
        month, = record["impacts"]
        self.assertEqual(sorted(g["total"] for g in month["before"]), [988_932, 3_888_097])
        self.assertEqual([(g["members"], g["total"], g["authority"]) for g in month["after"]],
                         [(["D-001", "D-011", "D-024", "D-037"], 4_877_029, "영업임원")])
        split_id = self.change({"address": self.initial["address"]}, "2026-06-15")
        split = next(i for i in list_corrections(self.db, JUNE, "D-011") if i["correction_id"] == split_id)
        self.assertEqual(sorted(g["total"] for g in split["impacts"][0]["after"]), [988_932, 3_888_097])
        # A still earlier one-field patch is hidden by the June address version, without spurious June impacts.
        earlier = self.change({"address": "5월 적용 주소"}, "2026-05-10")
        self.assertEqual([i["period_ym"] for i in list_corrections(self.db, JUNE)[0]["impacts"]], ["2026-05"])
        self.assertEqual(self.master("2026-07")["address"], "7월 적용 주소")
        self.assertEqual(agency_versions(self.db)["D-001"], earlier)
        self.assertEqual(self.db.execute("SELECT address FROM agencies WHERE agency_id='D-001'").fetchone()[0],
                         self.initial["address"])
        sync_sources(self.db, SOURCE)
        with closing(open_db(self.path)) as reopened:
            self.assertEqual(agency_versions(reopened)["D-001"], earlier)
            self.assertEqual(list_corrections(reopened, JUNE), list_corrections(self.db, JUNE))

    def test_source_paid_tasks_and_treasury_only_review(self):
        change_id = self.change(self.join, "2026-05-31")
        tasks = list_tasks(self.db, "2026-05", "D-001")
        self.assertEqual({t["agency_id"] for t in tasks}, {"D-001", "D-011", "D-024", "D-037"})
        self.assertTrue(all(t["kind"] == "post_review" and t["instruction_id"] is None for t in tasks))
        task = tasks[0]
        with self.assertRaises(PermissionError):
            complete_post_review(self.db, USERS["lead"], task["task_id"], "검토 확인")
        complete_post_review(self.db, USERS["treasury"], task["task_id"], "기지급 원장 증빙 확인")
        self.assertEqual([(t["status"], t["resolved_by"]) for t in list_tasks(self.db, "2026-05")
                          if t["task_id"] == task["task_id"]], [("done", "treasury")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM payment_instructions").fetchone()[0], 0)
        self.assertTrue(change_id)

    def test_invalid_changes_rollback_and_optimistic_version(self):
        with self.assertRaises(PermissionError):
            apply_correction(self.db, USERS["treasury"], "D-001", self.join, "2026-06-01", "확인")
        with self.assertRaisesRegex(ValueError, "증빙"):
            apply_correction(self.db, USERS["staff"], "D-001", self.join, "2026-06-01", "확인", "")
        with self.assertRaises(ValueError):
            self.change({"bank_account_no": "1234"}, "2026-06-01")
        self.change({"address": "새 주소"}, "2026-06-01", expected_version=0)
        with self.assertRaisesRegex(ValueError, "이력"):
            self.change(self.join, "2026-06-01", expected_version=0)
        with self.assertRaisesRegex(ValueError, "동일"):
            self.change({"address": "새 주소"}, "2026-06-01", expected_version=1)
        self.db.execute("""CREATE TRIGGER impact_failure BEFORE INSERT ON correction_impacts
                           BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.change(self.join, "2026-06-01")
        self.assertEqual(len(list_corrections(self.db, JUNE)), 1)
        self.assertEqual(self.master(JUNE)["address"], "새 주소")
        self.assertEqual(json.loads(self.db.execute("SELECT after_json FROM agency_corrections").fetchone()[0]),
                         {"address": "새 주소"})
        self.assertEqual(month_overview(self.db, JUNE)["stats"]["monthly_total"], 37_159_812)

    def test_same_day_sequence_and_scheduled_account_only_change(self):
        self.change({"bank_account_no": "모의은행 400-002"}, "2026-09-01", expected_version=0)
        self.change({"bank_account_no": "모의은행 400-003"}, "2026-09-01", expected_version=1)
        self.assertEqual(self.master("2026-08")["bank_account_no"], self.initial["bank_account_no"])
        self.assertEqual(self.master("2026-09-01")["bank_account_no"], "모의은행 400-003")
        latest, first = list_corrections(self.db, JUNE, "D-001")
        self.assertEqual(latest["before_values"], first["after_values"])
        self.assertEqual(latest["impacts"], [])
        self.assertEqual(first["tasks"], [])
        self.change({"bank_account_no": "모의은행 400-001"}, "2026-06-01", expected_version=2)
        june = list_corrections(self.db, JUNE, "D-001")[0]["impacts"][0]
        self.assertEqual(june["before"][0]["group_id"], "D-001")
        self.assertEqual(june["after"][0]["members"], ["D-001"])
        self.assertEqual(june["after"][0]["total"], 988_932)
        self.assertEqual(self.master("2026-09")["bank_account_no"], "모의은행 400-003")
