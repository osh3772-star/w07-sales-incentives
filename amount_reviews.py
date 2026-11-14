"""Review different amounts supplied for one rebate while retaining the source rows."""

from rules import effective_agencies, registered_groups
from workflow_security import require_role


SCHEMA = """
CREATE TABLE IF NOT EXISTS amount_decisions (
    decision_id INTEGER PRIMARY KEY,
    rebate_id TEXT NOT NULL REFERENCES rebates,
    previous_amount INTEGER NOT NULL,
    confirmed_amount INTEGER NOT NULL CHECK(confirmed_amount >= 0),
    reason TEXT NOT NULL, decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS amount_decisions_rebate ON amount_decisions(rebate_id,decision_id);
CREATE TABLE IF NOT EXISTS reviewed_amount_inputs (
    rebate_id TEXT NOT NULL REFERENCES rebates, observed_row TEXT NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES amount_decisions,
    PRIMARY KEY (rebate_id,observed_row)
);
CREATE VIEW IF NOT EXISTS open_intake_issues AS
SELECT i.* FROM intake_issues i WHERE NOT EXISTS (
    SELECT 1 FROM reviewed_amount_inputs a WHERE a.rebate_id=i.rebate_id AND a.observed_row=i.observed_row
    AND i.reason='같은 정산건 ID의 원본 내용이 다릅니다.'
);
CREATE VIEW IF NOT EXISTS effective_rebates AS
SELECT r.rebate_id, r.agency_id, r.period_ym, r.sales_performance_krw,
       COALESCE(d.confirmed_amount,r.rebate_amount_krw) AS rebate_amount_krw,
       r.rebate_amount_krw AS source_amount_krw, r.approval_status, r.approver, r.payment_date, r.intake_at
FROM rebates r LEFT JOIN amount_decisions d ON d.decision_id=(
    SELECT MAX(sub.decision_id) FROM amount_decisions sub WHERE sub.rebate_id=r.rebate_id
);
"""

MIXED_APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS amount_approval_checks (
    decision_id INTEGER PRIMARY KEY REFERENCES amount_decisions,
    evidence TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS source_amount_conflicts AS
SELECT i.rebate_id, i.period_ym, i.observed_row, r.sales_performance_krw AS original_sales,
       json_extract(i.observed_row,'$.sales_performance_krw') AS reported_sales,
       json_extract(i.observed_row,'$.rebate_amount_krw') AS amount,
       r.approval_status AS original_status, json_extract(i.observed_row,'$.approval_status') AS reported_status,
       r.approver AS original_approver, json_extract(i.observed_row,'$.approver') AS reported_approver,
       r.payment_date AS original_date, json_extract(i.observed_row,'$.payment_date') AS reported_date
FROM intake_issues i JOIN rebates r USING(rebate_id)
WHERE i.reason='같은 정산건 ID의 원본 내용이 다릅니다.'
  AND i.period_ym=r.period_ym AND json_extract(i.observed_row,'$.period_ym')=r.period_ym
  AND json_extract(i.observed_row,'$.agency_id')=r.agency_id
  AND json_extract(i.observed_row,'$.rebate_amount_krw')<>r.rebate_amount_krw;
DROP VIEW IF EXISTS amount_inputs;
CREATE VIEW amount_inputs AS
SELECT *, (original_status<>reported_status OR original_approver<>reported_approver) AS approval_changed
FROM source_amount_conflicts WHERE original_date=reported_date;
"""

AGENCY_REVIEW_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS amount_agency_decisions (
    decision_id INTEGER PRIMARY KEY REFERENCES amount_decisions,
    previous_agency_id TEXT NOT NULL REFERENCES agencies,
    confirmed_agency_id TEXT NOT NULL REFERENCES agencies
)""",
    "DROP VIEW IF EXISTS amount_inputs",
    "DROP VIEW IF EXISTS source_amount_conflicts",
    "DROP VIEW IF EXISTS effective_rebates",
    """CREATE VIEW effective_rebates AS
SELECT r.rebate_id, COALESCE((
           SELECT a.confirmed_agency_id FROM amount_agency_decisions a
           JOIN amount_decisions d USING(decision_id)
           WHERE d.rebate_id=r.rebate_id ORDER BY a.decision_id DESC LIMIT 1
       ), r.agency_id) AS agency_id,
       r.agency_id AS source_agency_id, r.period_ym, r.sales_performance_krw,
       COALESCE(d.confirmed_amount,r.rebate_amount_krw) AS rebate_amount_krw,
       r.rebate_amount_krw AS source_amount_krw, r.approval_status, r.approver, r.payment_date, r.intake_at
FROM rebates r LEFT JOIN amount_decisions d ON d.decision_id=(
    SELECT MAX(sub.decision_id) FROM amount_decisions sub WHERE sub.rebate_id=r.rebate_id
)""",
    """CREATE VIEW source_amount_conflicts AS
SELECT i.rebate_id, i.period_ym, i.observed_row,
       r.agency_id AS source_agency_id, json_extract(i.observed_row,'$.agency_id') AS reported_agency_id,
       r.sales_performance_krw AS original_sales,
       json_extract(i.observed_row,'$.sales_performance_krw') AS reported_sales,
       json_extract(i.observed_row,'$.rebate_amount_krw') AS amount,
       r.approval_status AS original_status, json_extract(i.observed_row,'$.approval_status') AS reported_status,
       r.approver AS original_approver, json_extract(i.observed_row,'$.approver') AS reported_approver,
       r.payment_date AS original_date, json_extract(i.observed_row,'$.payment_date') AS reported_date
FROM intake_issues i JOIN rebates r USING(rebate_id)
WHERE i.reason='같은 정산건 ID의 원본 내용이 다릅니다.'
  AND i.period_ym=r.period_ym AND json_extract(i.observed_row,'$.period_ym')=r.period_ym
  AND (json_extract(i.observed_row,'$.agency_id')<>r.agency_id
       OR json_extract(i.observed_row,'$.rebate_amount_krw')<>r.rebate_amount_krw)""",
    """CREATE VIEW amount_inputs AS
SELECT *, (original_status<>reported_status OR original_approver<>reported_approver) AS approval_changed
FROM source_amount_conflicts WHERE original_date=reported_date""",
)


def has_open_issue(db, rebate_id):
    return db.execute("SELECT 1 FROM open_intake_issues WHERE rebate_id=? LIMIT 1", (rebate_id,)).fetchone() is not None


def _review_inputs(db, month, rebate_id=None):
    rows = db.execute(
        """SELECT i.*, r.source_amount_krw, r.rebate_amount_krw, r.agency_id AS effective_agency_id, a.decision_id,
                  CASE WHEN p.state='completed' THEN p.amount_krw
                       WHEN r.approval_status='승인' AND r.payment_date NOT IN ('NONE','') THEN r.source_amount_krw END AS paid
           FROM amount_inputs i JOIN effective_rebates r USING(rebate_id)
           LEFT JOIN reviewed_amount_inputs a USING(rebate_id,observed_row)
           LEFT JOIN payment_instructions p USING(rebate_id)
           WHERE i.period_ym=? AND (? IS NULL OR i.rebate_id=?)
           ORDER BY i.rebate_id, i.reported_agency_id, i.amount, i.observed_row""", (month, rebate_id, rebate_id),
    ).fetchall()
    if not any(r["decision_id"] is None and r["reported_agency_id"] != r["effective_agency_id"] for r in rows):
        return rows
    agencies = effective_agencies(db, month)
    _, groups = registered_groups(agencies)
    eligible = {a["agency_id"] for a in agencies if a["rebate_eligible"]}
    return [r for r in rows if r["decision_id"] is not None or r["reported_agency_id"] == r["effective_agency_id"]
            or (r["reported_agency_id"] in eligible
                and groups[r["reported_agency_id"]] == groups.get(r["effective_agency_id"]))]


def list_reviews(db, month):
    result = {}
    for row in _review_inputs(db, month):
        review = result.setdefault(row["rebate_id"], {
            "rebate_id": row["rebate_id"], "original": row["source_amount_krw"],
            "effective": row["rebate_amount_krw"], "paid": row["paid"], "inputs": [], "decisions": [], "open": False,
            "source_agency_id": row["source_agency_id"], "effective_agency_id": row["effective_agency_id"],
            "candidate_agencies": {row["effective_agency_id"]},
        })
        review["inputs"].append({key: row[key] for key in (
            "amount", "decision_id", "original_sales", "reported_sales", "approval_changed",
            "original_status", "original_approver", "reported_status", "reported_approver", "reported_agency_id",
        )})
        if row["decision_id"] is None:
            review["open"] = True
            review["candidate_agencies"].add(row["reported_agency_id"])
            if row["approval_changed"]:
                review["needs_approval_check"] = True
    for row in db.execute(
        """SELECT d.*, c.evidence AS approval_evidence, a.previous_agency_id, a.confirmed_agency_id
           FROM amount_decisions d LEFT JOIN amount_agency_decisions a USING(decision_id)
           LEFT JOIN amount_approval_checks c USING(decision_id) JOIN rebates r USING(rebate_id)
           WHERE r.period_ym=? ORDER BY d.decision_id DESC""", (month,),
    ):
        result[row["rebate_id"]]["decisions"].append(dict(row))
    return list(result.values())


def resolve_amount(db, actor, rebate_id, amount, reason, expected_input, approval_evidence="", confirmed_agency_id=""):
    require_role(actor, "staff", "lead")
    value = str(amount)
    if not value.isascii() or not value.isdecimal() or len(value) > 19 or int(value) > 2**63 - 1:
        raise ValueError("확정할 금액을 원 단위 정수로 입력하세요.")
    if not isinstance(reason, str) or not reason.strip() or reason.strip().upper() == "NONE":
        raise ValueError("확정 금액을 판단한 근거를 입력하세요.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM effective_rebates WHERE rebate_id=?", (rebate_id,)).fetchone()
        rows = _review_inputs(db, current["period_ym"], rebate_id) if current else []
        pending = [r for r in rows if r["decision_id"] is None]
        if not pending or len(rows) != expected_input:
            raise ValueError("금액 확인 대상이 바뀌었습니다. 최신 입력과 판단 이력을 다시 확인하세요.")
        check_approval = any(r["approval_changed"] for r in pending)
        if check_approval and (not isinstance(approval_evidence, str)
                               or approval_evidence.strip().upper() in ("", "NONE")):
            raise ValueError("추가 입력의 승인 표기를 실제 결재 기록과 대조한 근거를 입력하세요.")
        check_agency = any(r["reported_agency_id"] != current["agency_id"] for r in pending)
        if check_agency:
            candidates = {(r["reported_agency_id"], r["amount"]) for r in pending}
            candidates.add((current["agency_id"], current["rebate_amount_krw"]))
            if (confirmed_agency_id, int(value)) not in candidates:
                raise ValueError("현재 심사 명세 또는 추가 입력에 함께 기재된 대리점과 금액을 선택하세요.")
        elif confirmed_agency_id and confirmed_agency_id != current["agency_id"]:
            raise ValueError("이 정산건의 확인 대상 대리점과 일치하지 않습니다.")
        decision = db.execute(
            """INSERT INTO amount_decisions(rebate_id,previous_amount,confirmed_amount,reason,decided_by)
               VALUES(?,?,?,?,?)""", (rebate_id, current["rebate_amount_krw"], int(value), reason.strip(), actor.user_id),
        ).lastrowid
        db.executemany(
            "INSERT INTO reviewed_amount_inputs(rebate_id,observed_row,decision_id) VALUES(?,?,?)",
            [(rebate_id, r["observed_row"], decision) for r in pending],
        )
        if check_approval:
            db.execute("INSERT INTO amount_approval_checks(decision_id,evidence) VALUES(?,?)",
                       (decision, approval_evidence.strip()))
        if check_agency:
            db.execute("""INSERT INTO amount_agency_decisions(decision_id,previous_agency_id,confirmed_agency_id)
                          VALUES(?,?,?)""", (decision, current["agency_id"], confirmed_agency_id))
        from monthly_basis import refresh_month
        refresh_month(db, current["period_ym"])
        return decision
