"""Revalidate the financial scope of a group decision at the point of dispatch."""

import json

from rules import authority_for, month_overview


SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_basis_reviews (
    instruction_id INTEGER NOT NULL REFERENCES payment_instructions,
    previous_approval_id INTEGER NOT NULL REFERENCES approvals,
    observed_basis TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reapproval_id INTEGER REFERENCES approvals,
    PRIMARY KEY(instruction_id, previous_approval_id)
);
"""


def _financial_scope(group, lines):
    # A normal transition from review to approved or paid doesn't add another obligation.
    amounts = sorted((r["rebate_id"], r["agency_id"], r["amount"]) for r in lines)
    total = sum(r[2] for r in amounts)
    return dict(group_id=group["group_id"], members=sorted(group["members"]),
                lines=amounts, monthly_total=total, authority=authority_for(total))


def _orders(db, month, group_id=None, *, overview=None):
    orders = db.execute(
        """SELECT i.instruction_id, i.rebate_id, i.agency_id, i.state,
               MAX(i.approval_id,
                   COALESCE((SELECT MAX(t.reapproval_id) FROM correction_tasks t
                             WHERE t.instruction_id=i.instruction_id),0),
                   COALESCE((SELECT MAX(b.reapproval_id) FROM payment_basis_reviews b
                             WHERE b.instruction_id=i.instruction_id),0)) AS effective_approval_id
           FROM payment_instructions i JOIN approvals a USING(approval_id)
           WHERE a.period_ym=? ORDER BY i.instruction_id""", (month,),
    ).fetchall()
    if not orders:
        return []
    view = overview if overview is not None else month_overview(db, month)
    saved = {r["approval_id"]: json.loads(r["decision_snapshot"] or r["snapshot"]) for r in db.execute(
        "SELECT approval_id, decision_snapshot, snapshot FROM approvals WHERE period_ym=? AND status='승인'", (month,)
    )}
    pending = {(r[0], r[1]) for r in db.execute(
        """SELECT b.instruction_id,b.previous_approval_id FROM payment_basis_reviews b
           JOIN approvals a ON a.approval_id=b.previous_approval_id WHERE a.period_ym=? AND b.reapproval_id IS NULL""",
        (month,),
    )}
    rows = {r["rebate_id"]: r for r in view["rows"]}
    scopes = {}
    disputed_ids = {r["rebate_id"] for r in db.execute(
        """SELECT DISTINCT i.rebate_id FROM amount_inputs i JOIN open_intake_issues u
           ON u.rebate_id=i.rebate_id AND u.observed_row=i.observed_row WHERE i.period_ym=?""", (month,),
    )}
    group_disputes = {}
    for rid in sorted(disputed_ids):
        if rid in rows and rows[rid]["state"] not in ("반려", "취소"):
            group_disputes.setdefault(rows[rid]["group_id"], []).append(rid)
    for group in view["groups"]:
        gid = group["group_id"]
        if group_id is None or gid == group_id:
            included = [r for r in view["rows"] if r["group_id"] == gid and
                        r["bucket"] in ("paid_amount", "approved_amount", "review_amount")]
            scopes[gid] = _financial_scope(group, included)
    result = []
    for order in orders:
        row = rows[order["rebate_id"]]
        gid = row["group_id"]
        if gid not in scopes:
            continue
        aid, iid = order["effective_approval_id"], order["instruction_id"]
        basis = saved[aid]
        previous = _financial_scope(basis, basis["lines"])
        current = scopes[gid]
        changed = (previous != current or basis["monthly_total"] != current["monthly_total"]
                   or basis["authority"] != current["authority"])
        outstanding = (iid, aid) in pending
        reason = None
        kind = None
        amount_hold = None
        if order["state"] != "completed" and gid in group_disputes:
            amount_hold = ("그룹 월 누계에 포함할 금액이 미확정입니다. 월 정산 접수에서 "
                           + ", ".join(group_disputes[gid]) + "의 원본 금액을 확인하세요.")
        if order["state"] != "completed" and (changed or outstanding):
            if order["state"] in ("sent", "review"):
                kind = "bank_check"
                action = "기존 시도의 은행 결과부터 확인하세요."
            else:
                kind = "reapproval"
                action = "최신 그룹 명세로 재승인받으세요."
            reason = (f"승인 후 그룹·월의 심사 명세 변경 · {month} · "
                      f"결재 #{aid} {basis['monthly_total']:,}원 → 현재 {current['monthly_total']:,}원 "
                      f"({current['authority']}). {action}")
        result.append(dict(instruction_id=iid, rebate_id=row["rebate_id"], group_id=gid, current_group_id=gid,
                           rebate_amount_krw=row["amount"], effective_approval_id=aid, queue_kind=kind,
                           reason=reason, amount_hold=amount_hold, observed_basis=current, recorded=outstanding))
    return result


def open_orders(db, month, group=None, *, overview=None):
    return [r for r in _orders(db, month, group, overview=overview) if r["reason"]]


def refresh_month(db, month):
    """Latch changed grounds during intake/amount resolution, under the caller's writer lock."""
    if not db.in_transaction:
        raise RuntimeError("월 심사 근거 변경은 업무 트랜잭션 안에서 기록하세요.")
    for line in open_orders(db, month):
        if not line["recorded"]:
            db.execute(
                """INSERT INTO payment_basis_reviews(instruction_id,previous_approval_id,observed_basis)
                   VALUES(?,?,?) ON CONFLICT DO NOTHING""",
                (line["instruction_id"], line["effective_approval_id"],
                 json.dumps(line["observed_basis"], ensure_ascii=False)),
            )


def annotate_orders(db, month, orders):
    by_id = {r["instruction_id"]: r for r in _orders(db, month)} if orders else {}
    for order in orders:
        current = by_id[order["instruction_id"]]
        order["effective_approval_id"] = current["effective_approval_id"]
        order["monthly_hold"] = current["reason"]
        order["group_amount_hold"] = current["amount_hold"]
        if not (current["reason"] or current["amount_hold"]):
            continue
        if order["queue_state"] in ("sent", "review"):
            order["queue_state"] = "bank_check"
        elif order["queue_state"] in ("held", "ready", "failed"):
            order["queue_state"] = "source_review" if current["amount_hold"] else "basis_review"


def check_dispatch(db, instruction_id):
    month = db.execute(
        """SELECT a.period_ym FROM payment_instructions i JOIN approvals a USING(approval_id)
           WHERE i.instruction_id=?""", (instruction_id,),
    ).fetchone()[0]
    for line in _orders(db, month):
        if line["instruction_id"] == instruction_id:
            if line["amount_hold"] or line["reason"]:
                raise ValueError(line["amount_hold"] or line["reason"])
            return


def authorize(db, actor, approval_id, lines):
    """Apply only the unsent items the approver explicitly reconsidered, in the same decision transaction."""
    import funding

    for line in lines:
        previous_id = line.get("basis_previous_approval_id")
        if previous_id is None:
            continue
        order = db.execute("SELECT * FROM payment_instructions WHERE rebate_id=?", (line["rebate_id"],)).fetchone()
        if order["state"] not in ("held", "ready", "failed"):
            raise ValueError("기존 지급 시도의 결과를 확인한 뒤 최신 근거로 재상신하세요.")
        iid = order["instruction_id"]
        updated = db.execute(
            """UPDATE payment_basis_reviews SET reapproval_id=? WHERE instruction_id=?
               AND previous_approval_id=? AND reapproval_id IS NULL""", (approval_id, iid, previous_id),
        ).rowcount
        if updated != 1:
            raise ValueError("재승인 근거가 바뀌었습니다. 최신 명세로 다시 상신하세요.")
        if order["amount_krw"] != line["amount"]:
            funding.invalidate_amount_reservation(db, actor, iid)
            db.execute("UPDATE payment_instructions SET amount_krw=? WHERE instruction_id=?", (line["amount"], iid))
