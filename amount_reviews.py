"""Review different amounts supplied for one rebate while retaining the source rows."""

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
CREATE VIEW IF NOT EXISTS amount_inputs AS
SELECT i.rebate_id, i.period_ym, i.observed_row, r.sales_performance_krw AS original_sales,
       json_extract(i.observed_row,'$.sales_performance_krw') AS reported_sales,
       json_extract(i.observed_row,'$.rebate_amount_krw') AS amount
FROM intake_issues i JOIN rebates r USING(rebate_id)
WHERE i.reason='같은 정산건 ID의 원본 내용이 다릅니다.'
  AND i.period_ym=r.period_ym AND json_extract(i.observed_row,'$.period_ym')=r.period_ym
  AND json_extract(i.observed_row,'$.agency_id')=r.agency_id
  AND json_extract(i.observed_row,'$.approval_status')=r.approval_status
  AND json_extract(i.observed_row,'$.approver')=r.approver
  AND json_extract(i.observed_row,'$.payment_date')=r.payment_date
  AND json_extract(i.observed_row,'$.rebate_amount_krw')<>r.rebate_amount_krw;
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


def has_open_issue(db, rebate_id):
    return db.execute("SELECT 1 FROM open_intake_issues WHERE rebate_id=? LIMIT 1", (rebate_id,)).fetchone() is not None


def list_reviews(db, month):
    result = {}
    for row in db.execute(
        """SELECT i.*, r.source_amount_krw, r.rebate_amount_krw, a.decision_id,
                  CASE WHEN p.state='completed' THEN p.amount_krw
                       WHEN r.approval_status='승인' AND r.payment_date NOT IN ('NONE','') THEN r.source_amount_krw END AS paid
           FROM amount_inputs i JOIN effective_rebates r USING(rebate_id)
           LEFT JOIN reviewed_amount_inputs a USING(rebate_id,observed_row)
           LEFT JOIN payment_instructions p USING(rebate_id)
           WHERE i.period_ym=? ORDER BY i.rebate_id, i.amount""", (month,),
    ):
        review = result.setdefault(row["rebate_id"], {
            "rebate_id": row["rebate_id"], "original": row["source_amount_krw"],
            "effective": row["rebate_amount_krw"], "paid": row["paid"], "inputs": [], "decisions": [], "open": False,
        })
        review["inputs"].append({"amount": row["amount"], "decision_id": row["decision_id"],
                                 "original_sales": row["original_sales"], "reported_sales": row["reported_sales"]})
        if row["decision_id"] is None:
            review["open"] = True
    for row in db.execute(
        """SELECT d.* FROM amount_decisions d JOIN rebates r USING(rebate_id)
           WHERE r.period_ym=? ORDER BY d.decision_id DESC""", (month,),
    ):
        result[row["rebate_id"]]["decisions"].append(dict(row))
    return list(result.values())


def resolve_amount(db, actor, rebate_id, amount, reason, expected_input):
    require_role(actor, "staff", "lead")
    value = str(amount)
    if not value.isascii() or not value.isdecimal() or len(value) > 19 or int(value) > 2**63 - 1:
        raise ValueError("확정할 금액을 원 단위 정수로 입력하세요.")
    if not isinstance(reason, str) or not reason.strip() or reason.strip().upper() == "NONE":
        raise ValueError("확정 금액을 판단한 근거를 입력하세요.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT i.observed_row, a.decision_id FROM amount_inputs i
               LEFT JOIN reviewed_amount_inputs a USING(rebate_id,observed_row)
               WHERE i.rebate_id=?""", (rebate_id,),
        ).fetchall()
        pending = [r["observed_row"] for r in rows if r["decision_id"] is None]
        if not pending or len(rows) != expected_input:
            raise ValueError("금액 확인 대상이 바뀌었습니다. 최신 입력과 판단 이력을 다시 확인하세요.")
        current = db.execute(
            "SELECT rebate_amount_krw,period_ym FROM effective_rebates WHERE rebate_id=?", (rebate_id,),
        ).fetchone()
        decision = db.execute(
            """INSERT INTO amount_decisions(rebate_id,previous_amount,confirmed_amount,reason,decided_by)
               VALUES(?,?,?,?,?)""", (rebate_id, current["rebate_amount_krw"], int(value), reason.strip(), actor.user_id),
        ).lastrowid
        db.executemany(
            "INSERT INTO reviewed_amount_inputs(rebate_id,observed_row,decision_id) VALUES(?,?,?)",
            [(rebate_id, source, decision) for source in pending],
        )
        from monthly_basis import refresh_month
        refresh_month(db, current["period_ym"])
        return decision
