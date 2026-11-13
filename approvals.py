"""Human explanations, group approval versions and their immutable evidence."""

import json
from contextlib import contextmanager
from datetime import date

from payments import create_instructions
from rules import authority_for, month_overview
from workflow_security import require_role


APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS explanations (
    case_id INTEGER PRIMARY KEY, rebate_id TEXT NOT NULL REFERENCES rebates,
    group_id TEXT NOT NULL, question TEXT NOT NULL, required INTEGER NOT NULL CHECK (required IN (0,1)),
    due_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT '요청',
    requested_by TEXT NOT NULL, requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    respondent TEXT, answer TEXT, evidence TEXT, replied_by TEXT, replied_at TEXT,
    resolution TEXT, resolved_by TEXT, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS explanations_rebate ON explanations(rebate_id);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY, period_ym TEXT NOT NULL, group_id TEXT NOT NULL,
    version INTEGER NOT NULL, previous_id INTEGER REFERENCES approvals,
    status TEXT NOT NULL, authority TEXT NOT NULL, monthly_total INTEGER NOT NULL,
    new_amount INTEGER NOT NULL, snapshot TEXT NOT NULL, decision_snapshot TEXT,
    note TEXT NOT NULL, submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_by TEXT, decided_role TEXT, decided_at TEXT, reason TEXT,
    UNIQUE(period_ym,group_id,version)
);
CREATE UNIQUE INDEX IF NOT EXISTS approvals_pending ON approvals(period_ym,group_id) WHERE status='승인 대기';
CREATE TABLE IF NOT EXISTS approval_lines (
    approval_id INTEGER NOT NULL REFERENCES approvals, rebate_id TEXT NOT NULL REFERENCES rebates,
    agency_id TEXT NOT NULL REFERENCES agencies, amount INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('new','paid','approved')), PRIMARY KEY (approval_id,rebate_id)
);
CREATE INDEX IF NOT EXISTS approval_lines_rebate ON approval_lines(rebate_id,approval_id);
"""


def _text(value, label):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label}을(를) 입력하세요.")
    return value


@contextmanager
def approval_write(db):
    """Join the caller's unit of work when a draft and its business record are saved together."""
    if db.in_transaction:
        yield
    else:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield


def request_case(db, actor, rebate_id, question, required=True, due_date=""):
    require_role(actor, "staff", "lead")
    question = _text(question, "소명 요청 내용")
    if due_date:
        try:
            if date.fromisoformat(due_date).isoformat() != due_date:
                raise ValueError
        except ValueError:
            raise ValueError("회신 기한은 YYYY-MM-DD 형식으로 입력하세요.") from None
    with approval_write(db):
        row = db.execute("SELECT period_ym FROM rebates WHERE rebate_id=?", (rebate_id,)).fetchone()
        if not row:
            raise ValueError("정산건을 찾을 수 없습니다.")
        target = next(r for r in month_overview(db, row["period_ym"])["rows"] if r["rebate_id"] == rebate_id)
        if not target["intake_at"] or not target["group_id"] or target["bucket"] in {"paid_amount", "approved_amount"}:
            raise ValueError("접수 후 심사 중인 정산건에서 소명을 요청하세요.")
        return db.execute(
            """INSERT INTO explanations(rebate_id,group_id,question,required,due_date,requested_by)
               VALUES(?,?,?,?,?,?)""",
            (rebate_id, target["group_id"], question, int(required), due_date, actor.user_id),
        ).lastrowid


def reply_case(db, actor, case_id, respondent, answer, evidence=""):
    require_role(actor, "staff", "lead")
    with db:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            """UPDATE explanations SET status='회신 검토', respondent=?,answer=?,evidence=?,
               replied_by=?,replied_at=CURRENT_TIMESTAMP WHERE case_id=? AND status='요청'""",
            (_text(respondent, "회신 주체"), _text(answer, "회신 내용"), (evidence or "").strip(), actor.user_id, case_id),
        )
        if not result.rowcount:
            raise ValueError("답변을 기다리는 소명 요청을 선택하세요.")
    return case_id


def resolve_case(db, actor, case_id, reason):
    require_role(actor, "staff", "lead")
    with db:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            """UPDATE explanations SET status='해소', resolution=?,resolved_by=?,resolved_at=CURRENT_TIMESTAMP
               WHERE case_id=? AND status='회신 검토'""",
            (_text(reason, "검토 결과"), actor.user_id, case_id),
        )
        if not result.rowcount:
            raise ValueError("회신이 기록된 소명건을 검토하세요.")
    return case_id


def list_cases(db, month):
    return [dict(row) for row in db.execute(
        """SELECT e.*,r.agency_id,r.period_ym FROM explanations e JOIN rebates r USING(rebate_id)
           WHERE r.period_ym=? ORDER BY case_id DESC""", (month,),
    )]


def list_approvals(db, month):
    result = []
    seen = set()
    for saved in db.execute("SELECT * FROM approvals WHERE period_ym=? ORDER BY approval_id DESC", (month,)):
        row = dict(saved)
        row["snapshot"] = json.loads(row["snapshot"])
        row["decision_snapshot"] = json.loads(row["decision_snapshot"]) if row["decision_snapshot"] else None
        keys = {line["rebate_id"] for line in row["snapshot"]["lines"] if line["kind"] == "new"}
        row["can_resubmit"] = row["status"] in ("반려", "보완 요청", "승인 대기") and not keys.intersection(seen)
        seen.update(keys)
        result.append(row)
    return result


def _evidence(db, month, group_id, reopen_id=None, *, require_resolved=True):
    overview = month_overview(db, month)
    group = next((g for g in overview["groups"] if g["group_id"] == group_id), None)
    if group is None:
        raise ValueError("해당 귀속월의 관련자 그룹을 찾을 수 없습니다.")
    members = group["members"]
    cases = [c for c in list_cases(db, month) if c["agency_id"] in members]
    if any(r["bucket"] == "pending_amount" for r in overview["rows"] if r["group_id"] == group_id):
        raise ValueError("같은 그룹·월에 미접수 정산건이 있습니다. 월 접수 후 합산을 다시 확인하세요.")
    kinds = {"review_amount": "new", "paid_amount": "paid", "approved_amount": "approved"}
    source_issues = {i["rebate_id"] for i in overview["issues"]}
    lines = []
    for row in overview["rows"]:
        if row["group_id"] != group_id:
            continue
        kind = kinds.get(row["bucket"])
        if (reopen_id is not None and row["approval_id"] == reopen_id and row["state"] == "반려"
                and row["intake_at"] and row["rebate_id"] not in source_issues):
            kind = "new"
        if kind:
            lines.append({
                "rebate_id": row["rebate_id"], "agency_id": row["agency_id"], "amount": row["amount"],
                "kind": kind, "notes": row["notes"], "source_status": row["source_status"],
                "source_approver": row["source_approver"], "payment_date": row["payment_date"],
                "intake_at": row["intake_at"],
            })
    new_lines = [line for line in lines if line["kind"] == "new"]
    if not new_lines:
        raise ValueError("이번 결재에 포함할 신규 접수 건이 없습니다.")
    included_ids = {line["rebate_id"] for line in lines}
    if require_resolved and any(c["rebate_id"] in included_ids and c["required"] and c["status"] != "해소"
                                for c in cases):
        raise ValueError("필수 소명의 답변과 해소를 완료하고 상신하세요.")
    total = sum(line["amount"] for line in lines)
    master = [dict(row) for row in db.execute(
        "SELECT * FROM agencies ORDER BY agency_id"
    ) if row["agency_id"] in members]
    evidence = {
        "period_ym": month, "group_id": group_id, "members": members, "edges": group["edges"],
        "master": master, "lines": lines, "cases": cases,
        "monthly_total": total, "new_amount": sum(line["amount"] for line in new_lines),
        "authority": authority_for(total),
    }
    memos = [dict(row) for row in db.execute(
        """SELECT draft_id, adopted_text AS text, adopted_by AS reviewer, adopted_at AS reviewed_at
           FROM ai_drafts WHERE period_ym=? AND group_id=? AND kind='summary' AND adopted_at IS NOT NULL
           ORDER BY draft_id""", (month, group_id),
    )]
    # An unchanged pre-AI approval compares equal to its original stored evidence.
    if memos:
        evidence["review_memos"] = memos
    return evidence


def _stored_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def submit_approval(db, actor, month, group_id, note, previous_id=None):
    require_role(actor, "staff", "lead")
    note = _text(note, "상신 사유")
    with approval_write(db):
        history = [a for a in list_approvals(db, month) if a["group_id"] == group_id]
        pending = next((a for a in history if a["status"] == "승인 대기"), None)
        if pending and pending["approval_id"] != previous_id:
            raise ValueError("이미 승인 대기 중인 결재안이 있습니다. 변경된 근거는 해당 결재안에서 재상신하세요.")
        previous = next((a for a in history if a["approval_id"] == previous_id), None)
        if previous_id is not None and (not previous or not previous["can_resubmit"]):
            raise ValueError("현재 그룹·월의 재상신 가능한 결재 버전을 선택하세요.")
        if not previous and any(a["status"] == "보완 요청" and a["can_resubmit"] for a in history):
            raise ValueError("보완 요청을 받은 결재안에서 새 버전으로 재상신하세요.")
        evidence = _evidence(db, month, group_id, previous_id)
        if pending:
            if pending["snapshot"] == evidence:
                raise ValueError("상신 근거가 기존 승인 대기 버전과 동일합니다.")
            db.execute("UPDATE approvals SET status='재상신' WHERE approval_id=?", (previous_id,))
        version = history[0]["version"] + 1 if history else 1
        approval_id = db.execute(
            """INSERT INTO approvals(period_ym,group_id,version,previous_id,status,authority,
               monthly_total,new_amount,snapshot,note,submitted_by) VALUES(?,?,?,?,'승인 대기',?,?,?,?,?,?)""",
            (month, group_id, version, previous_id, evidence["authority"], evidence["monthly_total"],
             evidence["new_amount"], _stored_json(evidence), note, actor.user_id),
        ).lastrowid
        db.executemany(
            "INSERT INTO approval_lines(approval_id,rebate_id,agency_id,amount,kind) VALUES(?,?,?,?,?)",
            [(approval_id, line["rebate_id"], line["agency_id"], line["amount"], line["kind"])
             for line in evidence["lines"]],
        )
        return approval_id


def decide_approval(db, actor, approval_id, action, reason):
    states = {"approve": "승인", "reject": "반려", "supplement": "보완 요청"}
    if action not in states:
        raise ValueError("승인·반려·보완 요청 중 하나를 선택하세요.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not saved:
            raise ValueError("결재안을 찾을 수 없습니다.")
        require_role(actor, "executive" if saved["authority"] == "영업임원" else "lead")
        reason = _text(reason, "결정 사유")
        if saved["status"] != "승인 대기":
            if saved["status"] == states[action] and saved["decided_by"] == actor.user_id and saved["reason"] == reason:
                return approval_id
            raise ValueError("이미 처리되었거나 대체된 결재안입니다.")
        evidence = _evidence(db, saved["period_ym"], saved["group_id"], require_resolved=action == "approve")
        require_role(actor, "executive" if evidence["authority"] == "영업임원" else "lead")
        submitted = json.loads(saved["snapshot"])
        if action != "approve":
            # The reviewer may respond to an outstanding case but the financial basis must remain current.
            submitted["cases"] = evidence["cases"]
        if evidence != submitted:
            raise ValueError("상신 이후 근거 또는 월 누계가 바뀌었습니다. 영업지원이 최신 근거로 재상신해야 합니다.")
        db.execute(
            """UPDATE approvals SET status=?, reason=?,decision_snapshot=?,decided_by=?,decided_role=?,
               decided_at=CURRENT_TIMESTAMP WHERE approval_id=?""",
            (states[action], reason, _stored_json(evidence), actor.user_id, actor.role, approval_id),
        )
        if action == "approve":
            create_instructions(db, approval_id)
        return approval_id
