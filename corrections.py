"""Effective-dated registration changes and their settlement review obligations."""

import json
from dataclasses import dataclass
from datetime import date

from rules import IDENTITY_FIELDS, account_key, authority_for, effective_agencies, month_overview, registered_groups
from workflow_security import require_role


CORRECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agency_corrections (
    correction_id INTEGER PRIMARY KEY,
    agency_id TEXT NOT NULL REFERENCES agencies,
    effective_date TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL,
    reason TEXT NOT NULL, evidence TEXT NOT NULL, created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS correction_by_date ON agency_corrections(effective_date,correction_id);
CREATE TABLE IF NOT EXISTS correction_impacts (
    correction_id INTEGER NOT NULL REFERENCES agency_corrections, period_ym TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL, changed_json TEXT NOT NULL,
    PRIMARY KEY(correction_id,period_ym)
);
CREATE TABLE IF NOT EXISTS correction_flags (
    correction_id INTEGER NOT NULL REFERENCES agency_corrections,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('approval','draft')),
    target_id INTEGER NOT NULL, period_ym TEXT NOT NULL,
    PRIMARY KEY(correction_id,target_kind,target_id)
);
CREATE INDEX IF NOT EXISTS correction_target ON correction_flags(target_kind,target_id);
CREATE TABLE IF NOT EXISTS correction_tasks (
    task_id INTEGER PRIMARY KEY, correction_id INTEGER NOT NULL REFERENCES agency_corrections,
    period_ym TEXT NOT NULL, group_id TEXT NOT NULL, rebate_id TEXT NOT NULL REFERENCES rebates,
    instruction_id INTEGER REFERENCES payment_instructions, attempt_id TEXT REFERENCES mock_payment_attempts,
    kind TEXT NOT NULL
        CHECK(kind IN ('reapproval','bank_check','post_review')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_by TEXT, resolved_at TEXT, resolution TEXT, reapproval_id INTEGER REFERENCES approvals
);
CREATE UNIQUE INDEX IF NOT EXISTS correction_task_open ON correction_tasks(correction_id,rebate_id,kind)
    WHERE status='open';
CREATE INDEX IF NOT EXISTS correction_task_for_rebate ON correction_tasks(rebate_id,status,kind);
CREATE TABLE IF NOT EXISTS payment_account_rechecks (
    task_id INTEGER PRIMARY KEY REFERENCES correction_tasks,
    instruction_id INTEGER NOT NULL REFERENCES payment_instructions,
    previous_account TEXT, previous_evidence TEXT, previous_verified_by TEXT, previous_verified_at TEXT,
    account TEXT NOT NULL, evidence TEXT NOT NULL, verified_by TEXT NOT NULL, verified_at TEXT NOT NULL
);
"""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def agency_versions(db):
    return dict(db.execute(
        """SELECT a.agency_id, COALESCE(MAX(c.correction_id),0)
           FROM agencies a LEFT JOIN agency_corrections c USING(agency_id) GROUP BY a.agency_id"""
    ))


@dataclass
class _MonthState:
    values: dict
    groups: dict
    membership: dict
    overview: dict


def _registered_state(db, month, agency_id):
    master = effective_agencies(db, month)
    groups, membership = registered_groups(master)
    agency = next(row for row in master if row["agency_id"] == agency_id)
    return _MonthState({field: agency[field] for field in IDENTITY_FIELDS}, groups, membership, month_overview(db, month))


def _summaries(state, affected_ids):
    amounts = {g["group_id"]: g for g in state.overview["groups"]}
    applied_groups = {r["group_id"] for r in state.overview["rows"]
                      if r["bucket"] in ("paid_amount", "approved_amount", "review_amount")}
    result = []
    for group_id in sorted({state.membership[agency_id] for agency_id in affected_ids}):
        group = state.groups[group_id]
        # Include groups without eligible obligations so splits and merges remain explainable.
        amount = amounts.get(group_id, {})
        total = amount.get("total", 0)
        # Projections can include not-yet-received lines. Snapshot the authority for the applied total.
        result.append(dict(group, total=total, authority=authority_for(total) if group_id in applied_groups else "—",
                           projected_total=amount.get("projected_total", 0)))
    return result


def _flag(db, change_id, month, kind, target_id):
    db.execute("INSERT OR IGNORE INTO correction_flags VALUES(?,?,?,?)", (change_id,kind,target_id,month))


def _record_month(db, change_id, month, agency_id, before, after):
    changed = {key: value for key, value in after.values.items() if value != before.values[key]}
    if not changed:
        return
    old_group = before.groups[before.membership[agency_id]]
    new_group = after.groups[after.membership[agency_id]]
    affected = set(old_group["members"]) | set(new_group["members"])
    old_groups, new_groups = _summaries(before, affected), _summaries(after, affected)
    db.execute("INSERT INTO correction_impacts VALUES(?,?,?,?,?)",
               (change_id, month, _json(old_groups), _json(new_groups), _json(changed)))
    for approval in db.execute("SELECT approval_id,status,snapshot FROM approvals WHERE period_ym=?", (month,)):
        if not affected.intersection(json.loads(approval["snapshot"])["members"]):
            continue
        _flag(db, change_id, month, "approval", approval["approval_id"])
        if approval["status"] == "승인 대기":
            db.execute("UPDATE approvals SET status='정정 재검토' WHERE approval_id=?", (approval["approval_id"],))

    for draft in db.execute("SELECT draft_id,evidence_json FROM ai_drafts WHERE period_ym=?", (month,)):
        basis = json.loads(draft["evidence_json"])
        # Even an unchanged edge can now require a new explanation: the model
        # receives the field name and effective date of the applicable correction.
        if affected.intersection(basis["members"]):
            _flag(db, change_id, month, "draft", draft["draft_id"])

    orders = {r["rebate_id"]: r for r in db.execute(
        """SELECT i.*, (SELECT attempt_id FROM mock_payment_attempts a WHERE a.instruction_id=i.instruction_id
                        ORDER BY sequence_no DESC LIMIT 1) AS attempt_id
           FROM payment_instructions i JOIN rebates r USING(rebate_id) WHERE r.period_ym=?""",
        (month,),
    )}
    for row in after.overview["rows"]:
        if row["agency_id"] not in affected:
            continue
        order = orders.get(row["rebate_id"])
        if order:
            state = order["state"]
            if state == "completed":
                kind = "post_review"
            elif state in ("sent","review"):
                kind = "bank_check"
            else:
                kind = "reapproval"
        elif row["bucket"] == "paid_amount":
            kind = "post_review"
        elif row["bucket"] == "approved_amount":
            kind = "reapproval"
        else:
            continue
        db.execute(
            """INSERT INTO correction_tasks(correction_id,period_ym,group_id,rebate_id,instruction_id,attempt_id,kind)
               VALUES(?,?,?,?,?,?,?)""",
            (change_id, month, row["group_id"], row["rebate_id"], order["instruction_id"] if order else None,
             order["attempt_id"] if order else None, kind),
        )


def apply_correction(db, actor, agency_id, changes, effective_date, reason, evidence="", *, expected_version=None):
    """Add one field patch and derive all consequences under the same exclusive writer lock."""
    require_role(actor, "staff", "lead")
    if not isinstance(changes, dict) or not changes or set(changes) - set(IDENTITY_FIELDS):
        raise ValueError("정정할 대표자·주소·등록계좌를 선택하세요.")
    if not reason.strip():
        raise ValueError("정정 사유를 입력하세요.")
    if not evidence.strip():
        raise ValueError("정정을 확인한 증빙 참조를 입력하세요.")
    try:
        if date.fromisoformat(effective_date).isoformat() != effective_date:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("정정 적용일은 YYYY-MM-DD 형식으로 입력하세요.") from None
    with db:
        db.execute("BEGIN IMMEDIATE")
        versions = agency_versions(db)
        if agency_id not in versions:
            raise ValueError("등록된 대리점을 선택하세요.")
        if expected_version is not None and str(versions[agency_id]) != str(expected_version):
            raise ValueError("등록정보 이력이 바뀌었습니다. 새로 조회한 후 정정하세요.")
        current = next(a for a in effective_agencies(db, effective_date) if a["agency_id"] == agency_id)
        after_values = {}
        for field, value in changes.items():
            value = " ".join(str(value or "").split())
            if not value:
                raise ValueError("변경값을 입력하세요. 미확인 값은 NONE으로 기록하세요.")
            if field == "bank_account_no" and value != "NONE" and not account_key(value):
                raise ValueError("등록계좌에 은행명과 계좌번호를 함께 입력하세요.")
            if current[field] != value:
                after_values[field] = value
        if not after_values:
            raise ValueError("적용일 기준 등록정보와 동일합니다.")
        before_values = {k: current[k] for k in after_values}
        months = [m[0] for m in db.execute(
            "SELECT DISTINCT period_ym FROM rebates WHERE period_ym>=? ORDER BY period_ym", (effective_date[:7],),
        )]
        before = {month: _registered_state(db, month, agency_id) for month in months}
        change_id = db.execute(
            """INSERT INTO agency_corrections
               (agency_id,effective_date,before_json,after_json,reason,evidence,created_by)
               VALUES(?,?,?,?,?,?,?)""",
            (agency_id,effective_date,_json(before_values),_json(after_values),reason.strip(),evidence.strip(),actor.user_id),
        ).lastrowid
        for month in months:
            _record_month(db, change_id, month, agency_id, before[month], _registered_state(db, month, agency_id))
        return change_id


def list_tasks(db, month, group=None):
    rows = db.execute(
        """SELECT t.*,r.agency_id,r.rebate_amount_krw FROM correction_tasks t JOIN rebates r USING(rebate_id)
           WHERE t.period_ym=? ORDER BY t.task_id DESC""", (month,),
    ).fetchall()
    membership = registered_groups(effective_agencies(db, month))[1] if rows else {}
    return [dict(r, current_group_id=membership[r["agency_id"]]) for r in rows
            if not group or group == membership[r["agency_id"]]]


def list_corrections(db, month, group=None):
    groups = registered_groups(effective_agencies(db, month))[0]
    members = set(groups.get(group, {}).get("members", ()))
    items = []
    for row in db.execute("SELECT * FROM agency_corrections ORDER BY correction_id DESC"):
        record = dict(row)
        record["before_values"] = json.loads(record.pop("before_json"))
        record["after_values"] = json.loads(record.pop("after_json"))
        impacts = db.execute(
            "SELECT * FROM correction_impacts WHERE correction_id=? ORDER BY period_ym", (row["correction_id"],)
        )
        record["impacts"] = [
            {"period_ym": i["period_ym"], "before": json.loads(i["before_json"]), "after": json.loads(i["after_json"]),
             "changed_fields": sorted(json.loads(i["changed_json"]))}
            for i in impacts
        ]
        participates = any(i["period_ym"] == month and any(g["group_id"] == group for g in i["before"] + i["after"])
                           for i in record["impacts"])
        if not group or row["agency_id"] in members or participates:
            record["flags"] = [dict(flag) for flag in db.execute(
                """SELECT f.target_kind,f.target_id,f.period_ym,COALESCE(a.group_id,d.group_id) AS group_id,
                          d.kind AS draft_kind
                   FROM correction_flags f
                   LEFT JOIN approvals a ON f.target_kind='approval' AND a.approval_id=f.target_id
                   LEFT JOIN ai_drafts d ON f.target_kind='draft' AND d.draft_id=f.target_id
                   WHERE f.correction_id=? ORDER BY f.period_ym,f.target_kind,f.target_id""", (row["correction_id"],)
            )]
            record["tasks"] = [dict(task) for task in db.execute(
                """SELECT task_id,kind,status,period_ym,group_id,rebate_id,instruction_id,attempt_id,reapproval_id
                   FROM correction_tasks WHERE correction_id=? ORDER BY period_ym,task_id""", (row["correction_id"],)
            )]
            items.append(record)
    return items


def complete_post_review(db, actor, task_id, note):
    require_role(actor, "treasury")
    if not note.strip():
        raise ValueError("사후 검토 결과를 입력하세요.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE correction_tasks SET status='done', resolved_by=?,resolved_at=CURRENT_TIMESTAMP,resolution=?
               WHERE task_id=? AND kind='post_review' AND status='open'""", (actor.user_id, note.strip(), task_id),
        )
        if not changed.rowcount:
            raise ValueError("미처리 사후 검토 작업을 선택하세요.")
