"""AI-assisted writing. Only explicit adoption creates or changes business records."""

import json
import re

from approvals import (approval_write, correction_flags, current_memos, list_approvals, list_cases,
                       reapproval_tasks, request_case, submit_approval)
from rules import authority_for, effective_agencies, month_overview, registered_groups
from workflow_security import require_role


AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_drafts (
    draft_id INTEGER PRIMARY KEY,
    period_ym TEXT NOT NULL, group_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('summary','question','proposal')),
    rebate_id TEXT REFERENCES rebates, previous_id INTEGER REFERENCES approvals,
    evidence_json TEXT NOT NULL, original_text TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, request_id TEXT,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    adopted_text TEXT, adopted_by TEXT, adopted_at TEXT,
    case_id INTEGER UNIQUE REFERENCES explanations, approval_id INTEGER UNIQUE REFERENCES approvals
);
CREATE INDEX IF NOT EXISTS ai_drafts_group ON ai_drafts(period_ym,group_id,draft_id);
"""


def _masker(db, cases):
    # Only derived keys/amounts leave the app. Narrative answers need masking too.
    protected = set()
    for agency in db.execute("SELECT agency_name,representative_name,address,business_reg_no,bank_account_no FROM agencies"):
        protected.update(agency)
    for correction in db.execute("SELECT before_json,after_json FROM agency_corrections"):
        for values in correction:
            protected.update(json.loads(values).values())
    protected.update(row[0] for row in db.execute("SELECT DISTINCT bank_account_no FROM deposits"))
    protected.update(case["respondent"] for case in cases)
    literals = [re.escape(value) for value in sorted(filter(None, protected), key=len, reverse=True)
                if value.strip().upper() != "NONE"]
    names = re.compile("|".join(literals)) if literals else None
    # Account/phone/business identifiers, e-mail addresses, and links not present in the master.
    private = re.compile(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w.-]+\.[\w-]+|"
                         r"(?:[A-Za-z가-힣0-9·]+[시군구읍면동리로길]\s+){1,6}\d+(?:-\d+)?"
                         r"(?:\s*\d+(?:동|호))*|"
                         r"(?:담당자|성명|이름)\s*[:=]\s*[가-힣]{2,5}|"
                         r"(?<!\d)\d(?:[\s.()-]*\d){6,}(?!\d)", re.IGNORECASE)

    def mask(text):
        text = text or ""
        if names:
            text = names.sub("[비공개]", text)
        return private.sub("[비공개]", text)
    return mask


def _applicable_corrections(db, month, members, *, since=0):
    notices = []
    for row in db.execute(
        """SELECT c.correction_id,c.agency_id,c.effective_date,i.changed_json,
                  i.before_json AS previous_groups,i.after_json AS current_groups
           FROM agency_corrections c JOIN correction_impacts i
                ON i.correction_id=c.correction_id AND i.period_ym=?
           WHERE c.effective_date<=? AND c.correction_id>? ORDER BY c.correction_id""",
        (month, month + "-31", since),
    ):
        impacted = {row["agency_id"]}
        for groups in (row["previous_groups"], row["current_groups"]):
            if groups:
                for group in json.loads(groups):
                    impacted.update(group["members"])
        if impacted.intersection(members):
            notices.append({"correction_id": row["correction_id"], "agency_id": row["agency_id"],
                            "effective_date": row["effective_date"],
                            "changed_fields": sorted(json.loads(row["changed_json"]))})
    return notices


def _scope(db, month, group_id, kind, rebate_id, previous_id):
    if kind not in ("summary", "question", "proposal"):
        raise ValueError("지원하는 초안 종류를 선택하세요.")
    if kind != "question" and rebate_id:
        raise ValueError("소명 질문만 개별 정산건을 지정할 수 있습니다.")
    if kind != "proposal" and previous_id is not None:
        raise ValueError("결재안 재상신에만 이전 결재를 지정할 수 있습니다.")
    data = month_overview(db, month)
    group = next((item for item in data["groups"] if item["group_id"] == group_id), None)
    if not group:
        raise ValueError("해당 정산월에서 관련자 그룹을 찾을 수 없습니다.")
    rows = [row for row in data["rows"] if row["group_id"] == group_id]
    tasks = reapproval_tasks(db, month)
    if kind == "question":
        target = next((r for r in rows if r["rebate_id"] == rebate_id), None)
        if (not target or target["bucket"] == "paid_amount" or
                (rebate_id not in tasks and (not target["intake_at"] or target["bucket"] == "approved_amount"))):
            raise ValueError("그룹에서 소명을 요청할 접수 건을 선택하세요.")
    history = [doc for doc in list_approvals(db, month) if group_id in doc["visible_groups"]]
    if previous_id is not None:
        if not any(doc["approval_id"] == previous_id and group_id in doc["resubmittable_groups"] for doc in history):
            raise ValueError("재상신할 수 있는 결재 버전을 선택하세요.")
    cases = [case for case in list_cases(db, month) if case["agency_id"] in group["members"]]
    mask = _masker(db, cases)
    lines = []
    reopened = 0
    issue_ids = {issue["rebate_id"] for issue in data["issues"]}
    for row in rows:
        reopen = (previous_id is not None and previous_id == row["approval_id"] and row["state"] == "반려"
                  and row["intake_at"] and row["rebate_id"] not in issue_ids)
        if reopen:
            reopened += row["amount"]
        lines.append({"agency_id": row["agency_id"], "rebate_id": row["rebate_id"],
                      "amount_krw": row["amount"], "state": row["state"], "included": row["included"] or bool(reopen),
                      "notes": row["notes"]})
    memos = current_memos(db, month, group_id)
    context = {
        "period_ym": month, "group_id": group_id, "members": group["members"], "edges": group["edges"],
        "monthly_total_krw": group["total"] + reopened, "new_amount_krw": group["review_amount"] + reopened,
        "paid_krw": group["paid_amount"], "approved_unpaid_krw": group["approved_amount"],
        "pending_krw": group["pending_amount"], "authority": authority_for(group["total"] + reopened),
        "lines": lines, "target_rebate_id": rebate_id, "previous_approval_id": previous_id,
        "approval_history": [{"id": doc["approval_id"], "version": doc["version"], "status": doc["status"]}
                             for doc in history],
        "cases": [{"case_id": c["case_id"], "rebate_id": c["rebate_id"], "status": c["status"],
                   "required": bool(c["required"]), "question": mask(c["question"]),
                   "answer": mask(c["answer"]), "resolution": mask(c["resolution"]),
                   "has_evidence": bool(c["evidence"])} for c in cases],
        "review_memos": [{"id": m["draft_id"], "text": mask(m["text"])} for m in memos],
    }
    notices = _applicable_corrections(db, month, group["members"])
    if notices:
        context["registration_changes"] = notices
    if notices or tasks.intersection(r["rebate_id"] for r in rows):
        reconsidered = [r for r in rows if r["rebate_id"] in tasks and r["bucket"] == "approved_amount"]
        context["reapproval_rebate_ids"] = [r["rebate_id"] for r in reconsidered]
        context["reapproval_amount_krw"] = sum(r["amount"] for r in reconsidered)
        context["review_required_approval_ids"] = [d["approval_id"] for d in history if d["review_required"]]
    return context


def _serialized(context):
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def generate_draft(db, actor, month, group, kind, rebate_id=None, *, provider=None, previous_id=None):
    require_role(actor, "staff", "lead")
    if db.in_transaction:
        raise RuntimeError("초안 생성은 기존 DB 트랜잭션 밖에서 실행해야 합니다.")
    with db:
        db.execute("BEGIN")
        evidence = _scope(db, month, group, kind, rebate_id or None, previous_id)
        evidence_json = _serialized(evidence)
        revision = db.execute("SELECT COALESCE(MAX(correction_id),0) FROM agency_corrections").fetchone()[0]
    if provider is None:
        from ai_provider import generate_text
        provider = generate_text
    result = provider(kind, evidence)
    if not isinstance(result, dict) or not all(isinstance(result.get(k), str) and result[k].strip()
                                               for k in ("text", "provider", "model")):
        raise ValueError("모델 초안이나 호출 출처가 올바르지 않아 저장하지 못했습니다.")
    with approval_write(db):
        draft_id = db.execute(
            """INSERT INTO ai_drafts(period_ym,group_id,kind,rebate_id,previous_id,evidence_json,
                      original_text,provider,model,request_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (month, group, kind, rebate_id or None, previous_id, evidence_json, result["text"],
             result["provider"], result["model"], result.get("request_id"), actor.user_id),
        ).lastrowid
        # The provider runs outside the write lock. A late result retains the input revision it actually used.
        for change in _applicable_corrections(db, month, evidence["members"], since=revision):
            db.execute("""INSERT INTO correction_flags(correction_id,target_kind,target_id,period_ym)
                          VALUES(?,'draft',?,?)""", (change["correction_id"], draft_id, month))
        return draft_id


def list_drafts(db, month, group):
    flags = correction_flags(db, month, "draft")
    groups, _ = registered_groups(effective_agencies(db, month))
    members = set(groups.get(group, {}).get("members", ()))
    result = []
    for row in db.execute("SELECT * FROM ai_drafts WHERE period_ym=? ORDER BY draft_id DESC", (month,)):
        ids = flags.get(row["draft_id"], [])
        if row["group_id"] == group or (ids and members.intersection(json.loads(row["evidence_json"])["members"])):
            result.append(dict(row, review_required=bool(ids), correction_ids=ids))
    return result


def adopt_draft(db, actor, draft_id, edited_text, *, required=False, due_date="",
                month=None, group=None, allowed_kinds=None):
    require_role(actor, "staff", "lead")
    text = (edited_text or "").strip()
    if not text:
        raise ValueError("채택할 내용을 입력하세요.")
    with approval_write(db):
        row = db.execute("SELECT * FROM ai_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if (row is None or (month is not None and row["period_ym"] != month)
                or (group is not None and row["group_id"] != group)
                or (allowed_kinds is not None and row["kind"] not in allowed_kinds)):
            raise ValueError("선택한 업무에서 이 초안을 찾을 수 없습니다.")
        if row["adopted_at"]:
            if row["adopted_text"] != text or row["adopted_by"] != actor.user_id:
                raise ValueError("이미 채택된 초안입니다. 채택 기록을 확인하세요.")
            if row["case_id"] is not None:
                case = db.execute("SELECT required,due_date FROM explanations WHERE case_id=?",
                                  (row["case_id"],)).fetchone()
                if (case["required"], case["due_date"]) != (bool(required), due_date):
                    raise ValueError("다른 조건으로 이미 채택된 소명 질문입니다. 요청 이력을 확인하세요.")
            return dict(row)
        if db.execute("SELECT 1 FROM correction_flags WHERE target_kind='draft' AND target_id=?",
                      (draft_id,)).fetchone():
            raise ValueError("등록정보 정정으로 업무 근거가 바뀌었습니다. 재검토 후 최신 근거로 초안을 생성하세요.")
        latest = _scope(db, row["period_ym"], row["group_id"], row["kind"], row["rebate_id"], row["previous_id"])
        if _serialized(latest) != row["evidence_json"]:
            raise ValueError("생성 이후 업무 근거가 바뀌었습니다. 최신 근거로 초안을 다시 생성하세요.")
        case_id = approval_id = None
        if row["kind"] == "question":
            case_id = request_case(db, actor, row["rebate_id"], text, required, due_date)
        elif row["kind"] == "proposal":
            approval_id = submit_approval(db, actor, row["period_ym"], row["group_id"], text, row["previous_id"])
        db.execute(
            """UPDATE ai_drafts SET adopted_text=?,adopted_by=?,adopted_at=CURRENT_TIMESTAMP,
                      case_id=?,approval_id=? WHERE draft_id=?""",
            (text, actor.user_id, case_id, approval_id, draft_id),
        )
        return dict(db.execute("SELECT * FROM ai_drafts WHERE draft_id=?", (draft_id,)).fetchone())
