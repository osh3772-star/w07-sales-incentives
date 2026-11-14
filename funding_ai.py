"""Funding alternatives, human decisions, and the resulting departmental work."""

from datetime import date
import json

from ai_provider import ProviderError, generate_text
import funding
from workflow_security import require_role


FUNDING_AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_proposals (
    proposal_id INTEGER PRIMARY KEY,
    instruction_id INTEGER NOT NULL REFERENCES payment_instructions,
    basis_json TEXT NOT NULL, raw_response TEXT NOT NULL, funding_text TEXT NOT NULL, reschedule_text TEXT NOT NULL,
    model TEXT NOT NULL, provider TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    choice TEXT, review_note TEXT, adopted_by TEXT, adopted_at TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN
      ('draft','funding_pending','reschedule_pending','ready_to_move','done','declined','stale'))
);
CREATE UNIQUE INDEX IF NOT EXISTS active_funding_proposal ON funding_proposals(instruction_id)
    WHERE status IN ('funding_pending','reschedule_pending','ready_to_move');
CREATE TABLE IF NOT EXISTS funding_tasks (
    task_id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES funding_proposals,
    kind TEXT NOT NULL CHECK(kind IN ('collection','receipt','negotiation','finalize')),
    owner_role TEXT NOT NULL, due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','waiting','done','cancelled')),
    accepted INTEGER, note TEXT, resolved_by TEXT, resolved_at TEXT,
    UNIQUE(proposal_id,kind)
);
"""


def _note(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 6000:
        raise ValueError("검토 내용과 근거를 1~6,000자로 입력하세요.")
    return value.strip()


def _basis(db, instruction_id, target_amount, new_date, *, active_id=0):
    view = funding.overview(db)
    if not view["baseline"] or view["overcommitted"]:
        raise ValueError("회계에서 기준 잔액과 기존 배정을 먼저 확인하세요.")
    row = next((line for line in view["lines"] if str(line["instruction_id"]) == str(instruction_id)), None)
    if (not row or not row["due_date"] or row["reserved"] or
            row["state"] not in ("held", "ready", "failed") or
            row["queue_state"] in ("reapproval", "bank_check", "account_recheck")):
        raise ValueError("예정일이 있고 미배정인 유효 지급지시를 선택하세요.")
    if db.execute("""SELECT 1 FROM funding_proposals WHERE instruction_id=? AND proposal_id<>?
                     AND status IN ('funding_pending','reschedule_pending','ready_to_move')""",
                  (row["instruction_id"], active_id)).fetchone():
        raise ValueError("이 지급지시에는 이미 진행 중인 자금 대안이 있습니다.")
    raw = str(target_amount)
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 16 or int(raw) <= 0:
        raise ValueError("확보 후보 금액을 양의 원 단위로 입력하세요.")
    try:
        if date.fromisoformat(new_date).isoformat() != new_date or new_date <= row["due_date"]:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("현재 지급예정일 이후의 조정 후보일을 선택하세요.") from None
    target = int(raw)
    old_date = row["due_date"]
    needed = sum(d["needed"] for d in view["days"] if d["due_date"] <= old_date)
    through_new_date = sum(d["needed"] for d in view["days"] if d["due_date"] <= new_date)
    shortage = max(0, needed - view["free"])
    if not shortage:
        raise ValueError("선택한 지급예정일까지 자금 부족액이 없습니다.")
    return {
        "instruction_id": row["instruction_id"], "rebate_id": row["rebate_id"], "agency_id": row["agency_id"],
        "amount": row["amount"], "due_date": old_date, "new_date": new_date,
        "target_amount": target, "shortage": shortage, "confirmed_balance": view["confirmed"],
        "reserved": view["reserved"], "free": view["free"], "old_shortage_after_move":
            max(0, needed - row["amount"] - view["free"]),
        "new_shortage_after_move": max(0, through_new_date - view["free"]),
        "funding_shortage_after_confirmation": max(0, shortage - target),
        "version": view["version"], "schedule_version": row["schedule_version"],
    }


def _options(result, basis):
    if not isinstance(result, dict) or not all(isinstance(result.get(k), str) and result[k].strip()
                                              for k in ("text", "provider", "model", "request_id")):
        raise ProviderError("AI 호출 정보와 응답을 확인할 수 없습니다.")
    raw = result["text"].strip()
    if raw.startswith("```json\n") and raw.endswith("\n```"):
        raw = raw[8:-4]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ProviderError("두 대안의 구조를 읽지 못했습니다. 현재 계획에서 다시 요청하세요.") from None
    if (not isinstance(value, dict) or
            any(value.get(k) != basis[k] for k in ("instruction_id", "target_amount", "new_date")) or
            not all(isinstance(value.get(k), str) and value[k].strip() for k in ("funding", "reschedule"))):
        raise ProviderError("AI 응답의 지급지시·금액·후보일 또는 대안 문안을 확인할 수 없습니다.")
    return value["funding"].strip(), value["reschedule"].strip()


def generate_options(db, actor, instruction_id, target_amount, new_date, *, provider=None):
    require_role(actor, "treasury")
    if db.in_transaction:
        raise RuntimeError("AI 호출 전에 이전 트랜잭션을 완료하세요.")
    with db:
        db.execute("BEGIN")
        basis = _basis(db, instruction_id, target_amount, new_date)
    # No account numbers, bank documents, or personal identifiers leave the application.
    packet = {k: v for k, v in basis.items() if k not in ("version", "schedule_version")}
    result = (provider or generate_text)("funding_options", packet)
    funding_text, reschedule_text = _options(result, basis)
    with funding.write(db):
        return db.execute(
            """INSERT INTO funding_proposals(instruction_id,basis_json,raw_response,funding_text,reschedule_text,
                    provider,model,request_id,created_by) VALUES(?,?,?,?,?,?,?,?,?)""",
            (basis["instruction_id"], json.dumps(basis, ensure_ascii=False), result["text"], funding_text,
             reschedule_text, result["provider"], result["model"], result["request_id"], actor.user_id),
        ).lastrowid


def _proposal(db, proposal_id):
    row = db.execute("SELECT * FROM funding_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("선택한 자금 대안을 찾을 수 없습니다.")
    return row


def adopt_option(db, actor, proposal_id, choice, review_note):
    require_role(actor, "treasury")
    note = _note(review_note)
    if choice not in ("funding", "reschedule"):
        raise ValueError("검토한 대안을 선택하세요.")
    with funding.write(db):
        p = _proposal(db, proposal_id)
        if p["choice"]:
            if (p["choice"], p["review_note"], p["adopted_by"]) == (choice, note, actor.user_id):
                return p["proposal_id"]
            raise ValueError("이미 선택한 안과 다른 요청입니다. 계획의 채택 이력을 확인하세요.")
        basis = json.loads(p["basis_json"])
        if _basis(db, p["instruction_id"], basis["target_amount"], basis["new_date"]) != basis:
            raise ValueError("자금 또는 지급계획이 바뀌었습니다. 최신 계산으로 대안을 다시 요청하세요.")
        if choice == "funding":
            state = "funding_pending"
            work = (("collection", "staff", "open"), ("receipt", "treasury", "open"))
        else:
            state = "reschedule_pending"
            work = (("negotiation", "staff", "open"), ("finalize", "treasury", "waiting"))
        db.execute("""UPDATE funding_proposals SET choice=?,review_note=?,adopted_by=?,
                      adopted_at=CURRENT_TIMESTAMP,status=? WHERE proposal_id=?""",
                   (choice, note, actor.user_id, state, p["proposal_id"]))
        db.executemany("""INSERT INTO funding_tasks(proposal_id,kind,owner_role,status,due_date)
                          VALUES(?,?,?,?,?)""",
                       [(p["proposal_id"], k, owner, status, basis["due_date"]) for k, owner, status in work])
        return p["proposal_id"]


def _close(db, proposal_id, kind, status, actor, note, accepted):
    db.execute("""UPDATE funding_tasks SET status=?,note=?,resolved_by=?,accepted=?,resolved_at=CURRENT_TIMESTAMP
                  WHERE proposal_id=? AND kind=? AND status IN ('open','waiting')""",
               (status, note, actor.user_id, accepted, proposal_id, kind))


def respond_task(db, actor, task_id, accepted, note):
    require_role(actor, "staff", "lead")
    note = _note(note)
    if str(accepted) not in ("0", "1", "False", "True"):
        raise ValueError("협의 결과를 선택하세요.")
    agreed = str(accepted) in ("1", "True")
    with funding.write(db):
        task = db.execute("SELECT * FROM funding_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task or task["owner_role"] != "staff":
            raise ValueError("영업지원 담당 작업을 선택하세요.")
        if (task["status"], task["accepted"], task["note"], task["resolved_by"]) == ("done", agreed, note, actor.user_id):
            return
        if task["status"] != "open":
            raise ValueError("이미 처리한 부서 작업입니다.")
        pid, kind = task["proposal_id"], task["kind"]
        _close(db, pid, kind, "done", actor, note, agreed)
        if not agreed:
            _close(db, pid, "receipt" if kind == "collection" else "finalize",
                   "cancelled", actor, note, False)
            db.execute("UPDATE funding_proposals SET status='declined' WHERE proposal_id=?", (pid,))
        elif kind == "negotiation":
            db.execute("UPDATE funding_tasks SET status='open' WHERE proposal_id=? AND kind='finalize'", (pid,))
            db.execute("UPDATE funding_proposals SET status='ready_to_move' WHERE proposal_id=?", (pid,))


def finalize_reschedule(db, actor, proposal_id, note):
    require_role(actor, "treasury")
    note = _note(note)
    with funding.write(db):
        p = _proposal(db, proposal_id)
        if p["status"] == "done" and p["choice"] == "reschedule":
            prior = db.execute("SELECT note,resolved_by FROM funding_tasks WHERE proposal_id=? AND kind='finalize'",
                               (proposal_id,)).fetchone()
            if tuple(prior) == (note, actor.user_id):
                return
        if p["status"] != "ready_to_move":
            raise ValueError("영업지원의 일정 협의가 완료된 안을 선택하세요.")
        basis = json.loads(p["basis_json"])
        try:
            latest = _basis(db, p["instruction_id"], basis["target_amount"], basis["new_date"],
                            active_id=p["proposal_id"])
        except ValueError:
            latest = None
        if basis == latest:
            funding.schedule(db, actor, p["instruction_id"], basis["new_date"], note,
                             expected_date=basis["due_date"], expected_version=basis["schedule_version"],
                             proposal_id=p["proposal_id"])
            _close(db, proposal_id, "finalize", "done", actor, note, True)
            db.execute("UPDATE funding_proposals SET status='done' WHERE proposal_id=?", (proposal_id,))
            return
        reason = "협의 중 자금 또는 지급지시 근거가 바뀌었습니다. 최신 계획에서 다시 검토하세요."
        _close(db, proposal_id, "finalize", "cancelled", actor, reason, False)
        db.execute("UPDATE funding_proposals SET status='stale' WHERE proposal_id=?", (proposal_id,))
    # The superseded task no longer locks the old plan, including after a reload.
    raise ValueError(reason)


def confirm_funding_receipt(db, actor, proposal_id, bank_reference, amount, received_at, evidence):
    require_role(actor, "treasury")
    with funding.write(db):
        p = _proposal(db, proposal_id)
        if p["choice"] != "funding" or p["status"] not in ("funding_pending", "done"):
            raise ValueError("입금 확인이 필요한 자금 확보안을 선택하세요.")
        if p["status"] == "done" and not db.execute(
            "SELECT 1 FROM cash_receipts WHERE reference=? AND proposal_id=?", (bank_reference.strip(), proposal_id),
        ).fetchone():
            raise ValueError("목표액의 입금 확인이 완료된 안입니다.")
        changed = funding.confirm_receipt(db, actor, bank_reference, amount, received_at, evidence,
                                          proposal_id=p["proposal_id"])
        received = db.execute("SELECT SUM(amount) FROM cash_receipts WHERE proposal_id=?", (proposal_id,)).fetchone()[0]
        if received >= json.loads(p["basis_json"])["target_amount"] and p["status"] != "done":
            _close(db, proposal_id, "receipt", "done", actor, evidence, True)
            _close(db, proposal_id, "collection", "cancelled", actor, "목표액 실입금 확인으로 일정 확인 종료", None)
            db.execute("UPDATE funding_proposals SET status='done' WHERE proposal_id=?", (proposal_id,))
        return changed


def list_proposals(db):
    tasks = {}
    for task in db.execute("SELECT * FROM funding_tasks ORDER BY task_id"):
        tasks.setdefault(task["proposal_id"], []).append(dict(task))
    received = dict(db.execute("SELECT proposal_id,SUM(amount) FROM cash_receipts GROUP BY proposal_id"))
    result = []
    version = None
    for row in db.execute("SELECT * FROM funding_proposals ORDER BY proposal_id DESC"):
        item = dict(row)
        item.update(json.loads(item.pop("basis_json")))
        pid = item["proposal_id"]
        if item["status"] == "draft":
            if version is None:
                version = funding.overview(db)["version"]
            if version != item["version"]:
                item["status"] = "stale"
        item.update(tasks=tasks.get(pid, []), received_amount=received.get(pid, 0))
        result.append(item)
    return result
