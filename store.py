"""Read Corevia's source snapshot and persist monthly intake in SQLite."""

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from ai_drafts import AI_SCHEMA
from approvals import APPROVAL_SCHEMA
from payments import PAYMENT_SCHEMA


AGENCY_FIELDS = (
    "agency_id", "agency_name", "representative_name", "business_reg_no",
    "address", "bank_account_no", "rebate_eligible", "registered_date",
)
DEPOSIT_FIELDS = (
    "deposit_id", "agency_id", "deposit_date", "amount_krw",
    "bank_account_no", "matched_ar_id",
)
REBATE_FIELDS = (
    "rebate_id", "agency_id", "period_ym", "sales_performance_krw",
    "rebate_amount_krw", "approval_status", "approver", "payment_date",
)
MISSING = ("", "NONE")

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS agencies (
    agency_id TEXT PRIMARY KEY, agency_name TEXT NOT NULL,
    representative_name TEXT NOT NULL, business_reg_no TEXT NOT NULL,
    address TEXT NOT NULL, bank_account_no TEXT NOT NULL,
    rebate_eligible INTEGER NOT NULL CHECK (rebate_eligible IN (0, 1)),
    registered_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deposits (
    deposit_id TEXT PRIMARY KEY, agency_id TEXT NOT NULL REFERENCES agencies,
    deposit_date TEXT NOT NULL, amount_krw INTEGER NOT NULL,
    bank_account_no TEXT NOT NULL, matched_ar_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rebates (
    rebate_id TEXT PRIMARY KEY, agency_id TEXT NOT NULL, period_ym TEXT NOT NULL,
    sales_performance_krw INTEGER NOT NULL, rebate_amount_krw INTEGER NOT NULL,
    approval_status TEXT NOT NULL, approver TEXT NOT NULL, payment_date TEXT NOT NULL,
    intake_at TEXT
);
CREATE INDEX IF NOT EXISTS rebates_month ON rebates(period_ym);
CREATE TABLE IF NOT EXISTS intake_issues (
    rebate_id TEXT NOT NULL, period_ym TEXT NOT NULL, reason TEXT NOT NULL,
    observed_row TEXT NOT NULL,
    PRIMARY KEY (rebate_id, period_ym, reason, observed_row)
);
CREATE INDEX IF NOT EXISTS intake_issues_month ON intake_issues(period_ym);
PRAGMA user_version = 1;
COMMIT;
"""


def open_db(db_path):
    """Open one connection per request. Multiple processes can share the file."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise ValueError("더 최신 버전의 업무 DB입니다. 앱 버전을 확인하세요.")
        if version == 0:
            db.executescript(SCHEMA)
        if version < 2:
            db.executescript("BEGIN IMMEDIATE;\n" + APPROVAL_SCHEMA + "\n" + PAYMENT_SCHEMA
                             + "\nPRAGMA user_version = 2;\nCOMMIT;")
        if version < 3:
            db.executescript("BEGIN IMMEDIATE;\n" + AI_SCHEMA + "\nPRAGMA user_version = 3;\nCOMMIT;")
    except Exception:
        db.close()
        raise
    return db


def _parse_row(row, fields):
    if None in row or any(row[key] is None for key in fields):
        raise ValueError("행의 열 개수가 머리글과 다릅니다.")
    values = {name: row[name] for name in fields}
    if values[fields[0]].strip() in MISSING:
        raise ValueError("원본 식별자가 없습니다.")
    for key, value in values.items():
        if key.endswith("_krw"):
            if not value.isascii() or not value.isdecimal() or int(value) > 2**63 - 1:
                raise ValueError(f"{key} 값은 0 이상의 원 단위 정수여야 합니다.")
            values[key] = int(value)
        elif key == "rebate_eligible":
            if value not in ("True", "False"):
                raise ValueError("장려금 적격 값은 True 또는 False여야 합니다.")
            values[key] = int(value == "True")
        elif key == "period_ym":
            if len(value) != 7 or date.fromisoformat(value + "-01").isoformat()[:7] != value:
                raise ValueError("귀속월 형식은 YYYY-MM입니다.")
        elif key.endswith("_date") and value not in MISSING:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError(f"{key} 날짜 형식은 YYYY-MM-DD입니다.")
    return values


def _rows(path, fields):
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter="|")
            if not set(fields).issubset(reader.fieldnames or ()):
                raise ValueError(f"{path.name}의 필수 열을 확인해 주세요.")
            for row in reader:
                try:
                    yield _parse_row(row, fields)
                except ValueError as error:
                    raise ValueError(f"{path.name} {reader.line_num}행: {error}") from error
    except (OSError, UnicodeError, csv.Error) as error:
        raise ValueError(f"{path.name} 원본을 읽을 수 없습니다.") from error


def _insert_source(db, table, fields, row):
    # Table and field names come exclusively from the fixed schema above.
    key = fields[0]
    columns = ", ".join(fields)
    existing = db.execute(
        f"SELECT {columns} FROM {table} WHERE {key} = ?", (row[key],)
    ).fetchone()
    if existing is None:
        placeholders = ", ".join("?" for _ in fields)
        db.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            [row[field] for field in fields],
        )
    return existing


def _record_issue(db, row, reason, period):
    db.execute(
        """INSERT INTO intake_issues(rebate_id, period_ym, reason, observed_row)
           VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (row["rebate_id"], period, reason, json.dumps(row, ensure_ascii=False, sort_keys=True)),
    )


def _rebate_issue(row, eligibility):
    if row["agency_id"] not in eligibility:
        return "대리점 마스터에 없는 대리점입니다."
    if not eligibility[row["agency_id"]]:
        return "장려금 지급 비적격 대리점입니다."
    status = row["approval_status"]
    if status == "미지급" and row["approver"] in MISSING and row["payment_date"] in MISSING:
        return None
    if status == "승인" and row["approver"] not in MISSING:
        return None
    if status in ("반려", "취소") and row["payment_date"] in MISSING:
        return None
    return "원본 승인 상태·승인자·지급일을 확인해 주세요."


def sync_sources(db, source_root):
    """Append new source IDs atomically. Conflicting rebate versions need review."""
    root = Path(source_root)
    reference_files = (
        ("agencies", "master/m_agency_cv.csv", AGENCY_FIELDS),
        ("deposits", "financials/fin_collection_history_cv.csv", DEPOSIT_FIELDS),
    )
    with db:
        db.execute("BEGIN IMMEDIATE")
        for table, filename, fields in reference_files:
            for row in _rows(root / filename, fields):
                try:
                    existing = _insert_source(db, table, fields, row)
                except sqlite3.IntegrityError as error:
                    raise ValueError(f"{filename}의 {row[fields[0]]} 참조를 확인해 주세요.") from error
                if existing is not None and dict(existing) != row:
                    raise ValueError(f"{filename}의 기존 ID {row[fields[0]]}와 내용이 다릅니다.")
        eligibility = dict(db.execute("SELECT agency_id, rebate_eligible FROM agencies"))
        for row in _rows(root / "sales/sales_rebate_payment_cv.csv", REBATE_FIELDS):
            existing = _insert_source(db, "rebates", REBATE_FIELDS, row)
            if existing is not None and dict(existing) != row:
                reason = "같은 정산건 ID의 원본 내용이 다릅니다."
                for period in {row["period_ym"], existing["period_ym"]}:
                    _record_issue(db, row, reason, period)
                continue
            reason = _rebate_issue(row, eligibility)
            if reason:
                _record_issue(db, row, reason, row["period_ym"])


def available_months(db):
    return [
        row[0] for row in db.execute(
            "SELECT period_ym FROM rebates UNION SELECT period_ym FROM intake_issues ORDER BY period_ym"
        )
    ]


def receive_month(db, period_ym):
    """Receive only qualified, unresolved-payment requests; return repeat-safe counts."""
    # A conflict can concern two different months. Any open issue blocks this ID.
    eligible = """
        period_ym = ? AND approval_status = '미지급'
        AND approver IN ('NONE', '') AND payment_date IN ('NONE', '')
        AND agency_id IN (SELECT agency_id FROM agencies WHERE rebate_eligible = 1)
        AND NOT EXISTS (SELECT 1 FROM intake_issues WHERE intake_issues.rebate_id = rebates.rebate_id)
    """
    with db:
        db.execute("BEGIN IMMEDIATE")
        if period_ym not in available_months(db):
            raise ValueError("원본에 있는 정산월을 선택해 주세요.")
        existing = db.execute(
            f"SELECT COUNT(*) FROM rebates WHERE {eligible} AND intake_at IS NOT NULL", (period_ym,)
        ).fetchone()[0]
        added = db.execute(
            f"""UPDATE rebates SET intake_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE {eligible} AND intake_at IS NULL""",
            (period_ym,),
        ).rowcount
        issues = db.execute(
            "SELECT COUNT(DISTINCT rebate_id) FROM intake_issues WHERE period_ym = ?", (period_ym,)
        ).fetchone()[0]
    return {"added": added, "existing": existing, "issues": issues}
