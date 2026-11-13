import csv
import http.cookiejar
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from html.parser import HTMLParser
from pathlib import Path

from app import ROOT, SOURCE_ROOT
from rules import month_overview, registered_groups
from store import open_db, receive_month, sync_sources


JUNE = "2026-06"
LEDGER = Path("sales/sales_rebate_payment_cv.csv")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.db_path = self.path / "intake.sqlite3"
        self.db = open_db(self.db_path)
        self.addCleanup(self.db.close)
        sync_sources(self.db, SOURCE_ROOT)

    def test_real_month_and_transitive_evidence(self):
        preview = month_overview(self.db, JUNE)
        self.assertEqual(preview["stats"]["pending_count"], 40)
        self.assertEqual(preview["stats"]["monthly_total"], 0)
        self.assertEqual(receive_month(self.db, JUNE)["added"], 40)
        data = month_overview(self.db, JUNE)
        self.assertEqual(data["stats"]["received_count"], 40)
        self.assertEqual(data["stats"]["unpaid_amount"], 37_159_812)
        self.assertEqual(data["stats"]["monthly_total"], 37_159_812)
        group = next(g for g in data["groups"] if "D-024" in g["members"])
        self.assertEqual(set(group["members"]), {"D-011", "D-024", "D-037"})
        self.assertEqual({frozenset((e["left"], e["right"])) for e in group["edges"]},
                         {frozenset(("D-011", "D-024")), frozenset(("D-011", "D-037"))})
        self.assertEqual((group["total"], group["authority"]), (3_888_097, "영업임원"))
        self.assertEqual(sum(r["authority"] == "영업지원팀" for r in data["rows"]), 37)
        self.assertTrue(next(r for r in data["rows"] if r["agency_id"] == "D-024")["needs_check"])

    def test_concurrent_double_submit_preserves_receipts(self):
        def submit(_):
            with closing(open_db(self.db_path)) as other:
                return receive_month(other, JUNE)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sorted((r["added"], r["existing"]) for r in results), [(0, 40), (40, 0)])
        before = self.db.execute("SELECT rebate_id, intake_at FROM rebates ORDER BY rebate_id").fetchall()
        sync_sources(self.db, SOURCE_ROOT)
        self.assertEqual(receive_month(self.db, JUNE)["existing"], 40)
        self.assertEqual(before, self.db.execute("SELECT rebate_id, intake_at FROM rebates ORDER BY rebate_id").fetchall())

    def test_bank_name_missing_keys_and_spacing(self):
        def agency(identifier, representative, address, account):
            return {"agency_id": identifier, "representative_name": representative,
                    "address": address, "bank_account_no": account}

        groups, membership = registered_groups([
            agency("A", "김   대표", "NONE", "국민 012-345"),
            agency("B", " 김 대표 ", "", "국민 012345"),
            agency("C", "김 대표", "NONE", "신한 012345"),
        ])
        self.assertEqual(membership, {"A": "A", "B": "A", "C": "C"})
        self.assertEqual(set(groups["A"]["edges"][0]["keys"]), {"대표자", "정산계좌"})

    def copied_sources(self):
        source = self.path / "source"
        for name in (Path("master/m_agency_cv.csv"), LEDGER, Path("financials/fin_collection_history_cv.csv")):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE_ROOT / name, target)
        return source

    def append_row(self, source, rebate_id, month, amount, status, payment_date="NONE"):
        with (source / LEDGER).open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle, delimiter="|", lineterminator="\n").writerow([
                rebate_id, "D-001", month, 10_000_000, amount, status,
                "NONE" if status == "미지급" else "영업지원팀", payment_date,
            ])

    def test_month_membership_statuses_and_exact_limit(self):
        source = self.copied_sources()
        month = "2026-07"
        self.append_row(source, "TEST-1", month, 500_000, "승인", "2026-08-05")
        self.append_row(source, "TEST-2", month, 800_000, "승인")
        self.append_row(source, "TEST-3", month, 700_000, "미지급")
        self.append_row(source, "TEST-4", month, 990_000, "반려")
        self.append_row(source, "TEST-5", month, 990_000, "취소")
        sync_sources(self.db, source)
        self.assertEqual(receive_month(self.db, month)["added"], 1)
        group = month_overview(self.db, month)["groups"][0]
        self.assertEqual((group["paid_amount"], group["approved_amount"], group["review_amount"]),
                         (500_000, 800_000, 700_000))
        self.assertEqual((group["total"], group["authority"]), (2_000_000, "영업지원팀"))
        self.append_row(source, "TEST-6", month, 1, "미지급")
        sync_sources(self.db, source)
        self.assertEqual(receive_month(self.db, month)["added"], 1)
        group = month_overview(self.db, month)["groups"][0]
        self.assertEqual((group["total"], group["authority"]), (2_000_001, "영업임원"))

    def test_conflicting_rebate_id_opens_one_issue_and_preserves_amount(self):
        source = self.copied_sources()
        self.append_row(source, "RB-00006", JUNE, 1, "미지급")
        sync_sources(self.db, source)
        sync_sources(self.db, source)
        result = receive_month(self.db, JUNE)
        self.assertEqual((result["added"], result["issues"]), (39, 1))
        original = self.db.execute("SELECT rebate_amount_krw, intake_at FROM rebates WHERE rebate_id='RB-00006'").fetchone()
        self.assertEqual(tuple(original), (988_932, None))
        self.assertEqual(month_overview(self.db, JUNE)["stats"]["monthly_total"], 37_159_812 - 988_932)

    def test_invalid_record_rolls_back_entire_source_refresh(self):
        source = self.copied_sources()
        self.append_row(source, "TEST-VALID", JUNE, 10, "미지급")
        self.append_row(source, "TEST-INVALID", JUNE, "원", "미지급")
        with self.assertRaises(ValueError):
            sync_sources(self.db, source)
        count = self.db.execute("SELECT COUNT(*) FROM rebates").fetchone()[0]
        self.assertEqual(count, 240)


class FormToken(HTMLParser):
    value = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "csrf_token":
            self.value = fields.get("value")


@contextmanager
def running_server(db_path):
    environment = dict(os.environ, W07_DATABASE=str(db_path), PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-B", "-u", str(ROOT / "app.py"), "--port", "0"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", cwd=ROOT,
    )
    try:
        for line in process.stdout:
            found = re.search(r"http://127\.0\.0\.1:\d+", line)
            if found:
                yield found.group()
                break
        else:
            raise RuntimeError(f"서버가 실행되지 않았습니다 (종료 코드 {process.poll()}).")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        process.stdout.close()


class ServerRestartTest(unittest.TestCase):
    def test_intake_page_and_restart_on_same_database(self):
        receipt_query = "SELECT rebate_id, intake_at FROM rebates WHERE intake_at IS NOT NULL ORDER BY rebate_id"
        receipts = None
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "http.sqlite3"
            for new_count, existing_count in [(40, 0), (0, 40)]:
                with running_server(db_path) as url:
                    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                    with browser.open(f"{url}/?month={JUNE}", timeout=10) as response:
                        page = response.read().decode()
                    self.assertIn("37,159,812", page)
                    self.assertIn("3,888,097", page)
                    if receipts is not None:
                        with closing(sqlite3.connect(db_path)) as db:
                            self.assertEqual(receipts, db.execute(receipt_query).fetchall())
                    token = FormToken()
                    token.feed(page)
                    self.assertTrue(token.value)
                    form = urllib.parse.urlencode({"month": JUNE, "csrf_token": token.value}).encode()
                    with browser.open(f"{url}/intake", form, timeout=10) as response:
                        result_page = response.read().decode()
                    self.assertIn(f"신규 {new_count}건 · 기존 {existing_count}건", result_page)
                    with closing(sqlite3.connect(db_path)) as db:
                        current = db.execute(receipt_query).fetchall()
                    self.assertEqual(len(current), 40)
                    if receipts is not None:
                        self.assertEqual(current, receipts)
                    receipts = current


if __name__ == "__main__":
    unittest.main()
