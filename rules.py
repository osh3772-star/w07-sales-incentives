"""W07 registered-agency relationships and settlement-month intake totals."""

import json
import re
import sqlite3
from collections import defaultdict
from itertools import combinations


LIMIT_KRW = 2_000_000
IDENTITY_FIELDS = ("representative_name", "address", "bank_account_no")


def effective_agencies(db, as_of):
    """Overlay only changed fields. A YYYY-MM cutoff represents the end of that month."""
    cutoff = as_of + "-31" if len(as_of) == 7 else as_of
    agencies = {row["agency_id"]: dict(row) for row in db.execute("SELECT * FROM agencies ORDER BY agency_id")}
    for change in db.execute(
        """SELECT agency_id, after_json FROM agency_corrections WHERE effective_date <= ?
           ORDER BY effective_date, correction_id""", (cutoff,),
    ):
        agencies[change["agency_id"]].update(json.loads(change["after_json"]))
    return list(agencies.values())


def authority_for(amount):
    return "영업임원" if amount > LIMIT_KRW else "영업지원팀"


def clean_text(value):
    text = " ".join((value or "").split())
    return "" if text.upper() == "NONE" else text


def account_key(value):
    """Compare the bank and number as one key. A partial account is unknown."""
    match = re.fullmatch(r"([^0-9]+?)\s*([0-9][0-9\s-]*)", clean_text(value))
    if not match:
        return None
    bank = clean_text(match[1].strip(" -"))
    number = re.sub(r"[\s-]", "", match[2])
    return (bank, number) if bank else None


def registered_groups(agencies):
    """Build direct evidence first, then the connected components of that graph."""
    keys = {
        agency["agency_id"]: {
            "대표자": clean_text(agency["representative_name"]),
            "주소": clean_text(agency["address"]),
            "정산계좌": account_key(agency["bank_account_no"]),
        }
        for agency in agencies
    }
    neighbors = {agency_id: [] for agency_id in keys}
    edges = []
    ordered_ids = sorted(keys)
    for left, right in combinations(ordered_ids, 2):
        matched = [name for name, value in keys[left].items() if value and value == keys[right][name]]
        if len(matched) >= 2:
            neighbors[left].append(right)
            neighbors[right].append(left)
            edges.append({"left": left, "right": right, "keys": matched})

    membership = {}
    groups = {}
    for first in ordered_ids:
        if first in membership:
            continue
        membership[first] = first
        members = []
        stack = [first]
        while stack:
            agency_id = stack.pop()
            members.append(agency_id)
            for peer in neighbors[agency_id]:
                if peer not in membership:
                    membership[peer] = first
                    stack.append(peer)
        groups[first] = {"group_id": first, "members": sorted(members), "edges": []}

    for edge in edges:
        groups[membership[edge["left"]]]["edges"].append(edge)
    return groups, membership


def _settlement_state(rebate, eligible, reasons):
    """Return an exclusive amount bucket and a human-readable line status."""
    status = rebate["approval_status"]
    if status in {"반려", "취소"}:
        return None, status
    if not eligible:
        return None, "확인 필요"
    approved_by = clean_text(rebate["approver"])
    paid_on = clean_text(rebate["payment_date"])
    if status == "승인" and approved_by:
        # A disputed new copy doesn't erase a previously booked valid payment.
        return ("paid_amount", "원장상 기지급") if paid_on else ("approved_amount", "승인 후 미집행")
    if reasons or status != "미지급" or approved_by or paid_on:
        return None, "확인 필요"
    if rebate["intake_at"]:
        return "review_amount", "접수 완료"
    return "pending_amount", "접수 대기"


def month_overview(db: sqlite3.Connection, month: str) -> dict:
    # Schema is shared by source refresh and intake. All reads for one page must
    # see the same committed revision, even as another request receives a month.
    db.execute("SAVEPOINT overview_snapshot")
    try:
        agencies = effective_agencies(db, month)
        # The most recent application for a rebate controls its review state.
        # Reference lines on later applications do not change an earlier decision.
        applications = {
            row["rebate_id"]: row for row in db.execute(
                """SELECT l.rebate_id, a.approval_id, a.status, p.state AS payment_state,
                          p.amount_krw AS payment_amount
                   FROM approvals a JOIN approval_lines l USING (approval_id)
                   LEFT JOIN payment_instructions p ON p.rebate_id=l.rebate_id
                   WHERE a.period_ym=? AND l.kind='new' ORDER BY a.approval_id""",
                (month,),
            )
        }
        rebates = db.execute(
            "SELECT * FROM effective_rebates WHERE period_ym = ? ORDER BY agency_id, rebate_id", (month,)
        ).fetchall()
        deposits = db.execute(
            """SELECT agency_id, deposit_id, bank_account_no FROM deposits
               WHERE substr(deposit_date, 1, 7) <= ? ORDER BY agency_id, deposit_id""",
            (month,),
        ).fetchall()
        issues = [
            dict(row) for row in db.execute(
                """SELECT DISTINCT rebate_id, reason FROM open_intake_issues
                   WHERE period_ym = ? ORDER BY rebate_id, reason""",
                (month,),
            )
        ]
    finally:
        db.execute("RELEASE overview_snapshot")

    registry = {agency["agency_id"]: agency for agency in agencies}
    group_definitions, membership = registered_groups(agencies)
    issues_by_id = defaultdict(list)
    for issue in issues:
        issues_by_id[issue["rebate_id"]].append(issue["reason"])

    different_accounts = defaultdict(list)
    for deposit in deposits:
        agency = registry.get(deposit["agency_id"])
        registered = account_key(agency["bank_account_no"]) if agency else None
        observed = account_key(deposit["bank_account_no"])
        if registered and observed and registered != observed:
            different_accounts[deposit["agency_id"]].append(deposit["deposit_id"])

    rows = []
    groups = {}
    unpaid_count = 0
    unpaid_amount = 0
    pending_count = 0
    received_count = 0
    for rebate in rebates:
        agency_id = rebate["agency_id"]
        agency = registry.get(agency_id)
        eligible = bool(agency and agency["rebate_eligible"])
        reasons = issues_by_id[rebate["rebate_id"]]
        bucket, state = _settlement_state(rebate, eligible, reasons)
        application = applications.get(rebate["rebate_id"])
        if application:
            decision = application["status"]
            if decision == "승인":
                if application["payment_state"] == "completed":
                    bucket, state = "paid_amount", "모의 대사 완료"
                else:
                    bucket, state = "approved_amount", "승인 후 미집행"
            elif decision == "반려":
                bucket, state = None, "반려"
            elif bucket == "review_amount":
                state = decision
        amount = rebate["rebate_amount_krw"]
        if bucket == "paid_amount":
            amount = rebate["source_amount_krw"]
            if application and application["payment_state"] == "completed":
                amount = application["payment_amount"]
        group_id = membership.get(agency_id)
        if group_id and group_id not in groups:
            groups[group_id] = {
                **group_definitions[group_id],
                "row_count": 0,
                "included_count": 0,
                "paid_amount": 0,
                "approved_amount": 0,
                "review_amount": 0,
                "pending_amount": 0,
            }
        if group_id:
            group = groups[group_id]
            group["row_count"] += 1
            if bucket:
                group[bucket] += amount
                group["included_count"] += 1

        if eligible and rebate["approval_status"] == "미지급":
            unpaid_count += 1
            unpaid_amount += rebate["source_amount_krw"]
        if bucket == "pending_amount":
            pending_count += 1
        if rebate["intake_at"]:
            received_count += 1

        notes = list(reasons)
        evidence_ids = different_accounts[agency_id]
        if evidence_ids:
            notes.append("등록·회수계좌 불일치 · " + ", ".join(evidence_ids))
        rows.append({
            "rebate_id": rebate["rebate_id"], "agency_id": agency_id,
            "agency_name": agency["agency_name"] if agency else "등록 확인 필요",
            "amount": amount, "source_status": rebate["approval_status"],
            "source_approver": rebate["approver"], "payment_date": rebate["payment_date"],
            "intake_at": rebate["intake_at"], "state": state, "group_id": group_id,
            "approval_id": application["approval_id"] if application else None,
            "bucket": bucket,
            "included": bucket is not None, "needs_check": bool(notes) or state == "확인 필요",
            "notes": notes,
        })

    for group in groups.values():
        group["total"] = group["paid_amount"] + group["approved_amount"] + group["review_amount"]
        group["projected_total"] = group["total"] + group["pending_amount"]
        group["authority"] = authority_for(group["projected_total"]) if group["included_count"] else "—"
    for row in rows:
        group = groups.get(row["group_id"])
        row["group_total"] = group["projected_total"] if group else 0
        row["group_applied_total"] = group["total"] if group else 0
        row["authority"] = group["authority"] if row["included"] else "—"

    active_groups = [group for group in groups.values() if group["included_count"]]
    stats = {
        "source_count": len(rows), "unpaid_count": unpaid_count, "unpaid_amount": unpaid_amount,
        "received_count": received_count, "pending_count": pending_count,
        "monthly_total": sum(group["total"] for group in active_groups),
        "projected_total": sum(group["projected_total"] for group in active_groups),
        "review_amount": sum(group["review_amount"] for group in active_groups),
        "group_count": len(active_groups),
        "executive_group_count": sum(group["authority"] == "영업임원" for group in active_groups),
        "issue_count": len(issues), "check_count": sum(row["needs_check"] for row in rows),
    }
    return {"month": month, "rows": rows, "groups": list(groups.values()), "stats": stats, "issues": issues}
