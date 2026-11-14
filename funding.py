"""KRW reservations and confirmed-bank evidence for the separate payment-plan database."""

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from itertools import groupby

from workflow_security import require_role


FUNDING_SCHEMA = """
CREATE TABLE cash_balances (
    balance_id INTEGER PRIMARY KEY, as_of TEXT NOT NULL UNIQUE, scope TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount>=0), restricted INTEGER NOT NULL CHECK(restricted>=0),
    external INTEGER NOT NULL CHECK(external>=0), evidence TEXT NOT NULL, confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE cash_receipts (
    reference TEXT PRIMARY KEY NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
    received_on TEXT NOT NULL, evidence TEXT NOT NULL, proposal_id INTEGER,
    confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE cash_plans (
    instruction_id INTEGER PRIMARY KEY REFERENCES payment_instructions, due_date TEXT NOT NULL,
    schedule_version INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved IN (0,1))
);
CREATE TABLE cash_plan_events (
    event_id INTEGER PRIMARY KEY, instruction_id INTEGER NOT NULL REFERENCES payment_instructions,
    action TEXT NOT NULL, previous_date TEXT, due_date TEXT NOT NULL, amount INTEGER NOT NULL,
    reason TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE cash_mock_debits (
    instruction_id INTEGER PRIMARY KEY REFERENCES payment_instructions,
    reply_id TEXT NOT NULL UNIQUE REFERENCES mock_bank_replies, amount INTEGER NOT NULL,
    paid_on TEXT NOT NULL, balance_id INTEGER NOT NULL REFERENCES cash_balances
);
"""


def enabled(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cash_balances'").fetchone() is not None


# On upgrade, old failures may already have released a reservation before a conflicting reply.
BANK_REVIEW_RESERVATIONS_SQL = """
INSERT INTO cash_plan_events(instruction_id,action,previous_date,due_date,amount,reason,actor)
SELECT p.instruction_id,'reserve',p.due_date,p.due_date,i.amount_krw,'기존 은행 회신 상충 재확인','system'
FROM cash_plans p JOIN payment_instructions i USING(instruction_id)
JOIN bank_result_reviews b USING(instruction_id)
WHERE p.reserved=0 AND b.decision IS NULL AND i.state='review';
UPDATE cash_plans SET reserved=1 WHERE reserved=0 AND instruction_id IN (
    SELECT i.instruction_id FROM payment_instructions i JOIN bank_result_reviews b USING(instruction_id)
    WHERE b.decision IS NULL AND i.state='review'
);
"""


@contextmanager
def write(db):
    if db.in_transaction:
        yield
    else:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield


def _today():
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def _date(value, label, *, past=False):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{label}을 YYYY-MM-DD 형식으로 입력하세요.") from None
    if past and value > _today():
        raise ValueError(f"{label}에 미래 일자를 입력할 수 없습니다.")
    return value


def _money(value, label, *, positive=False):
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        raise ValueError(f"{label}에 원 단위 정수를 입력하세요.")
    amount = int(text)
    if amount > 2**63 - 1 or (positive and amount == 0):
        raise ValueError(f"{label}의 범위를 확인하세요.")
    return amount


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value.strip().upper() == "NONE":
        raise ValueError(f"{label}을 입력하세요.")
    return value.strip()


def _balance(db):
    return db.execute("SELECT * FROM cash_balances ORDER BY as_of DESC LIMIT 1").fetchone()


def set_balance(db, actor, amount, as_of, evidence, scope, restricted=0, external=0):
    require_role(actor, "treasury")
    as_of = _date(as_of, "기준일", past=True)
    values = (_money(amount, "잔액"), _money(restricted, "사용제한액"), _money(external, "타 용도 배정액"),
              _text(evidence, "잔액 대사 증빙"), _text(scope, "재원 범위"))
    with write(db):
        old = db.execute("SELECT amount,restricted,external,evidence,scope FROM cash_balances WHERE as_of=?",
                         (as_of,)).fetchone()
        if old:
            if tuple(old) != values:
                raise ValueError("같은 기준일에 다른 잔액 확인 기록이 있습니다.")
            return False
        current = _balance(db)
        if current and (current["as_of"] >= as_of or current["scope"] != values[-1]):
            raise ValueError("같은 재원 범위에서 최근 기준일 이후의 마감 잔액을 확인하세요.")
        db.execute("""INSERT INTO cash_balances(as_of,amount,restricted,external,evidence,scope,confirmed_by)
                      VALUES(?,?,?,?,?,?,?)""", (as_of, *values, actor.user_id))
        return True


def confirm_receipt(db, actor, reference, amount, received_on, evidence, *, proposal_id=None):
    require_role(actor, "treasury")
    reference = _text(reference, "은행 입금 거래번호")
    data = (_money(amount, "실입금액", positive=True), _date(received_on, "실입금일", past=True),
            _text(evidence, "입금 거래 증빙"), proposal_id)
    with write(db):
        prior = db.execute("SELECT amount,received_on,evidence,proposal_id FROM cash_receipts WHERE reference=?",
                           (reference,)).fetchone()
        if prior:
            if tuple(prior) != data:
                raise ValueError("같은 은행 입금 거래번호에 다른 금액·날짜·증빙 또는 확보안이 등록됐습니다.")
            return False
        baseline = _balance(db)
        if not baseline or received_on <= baseline["as_of"]:
            raise ValueError("대사한 기준 잔액의 마감일 이후에 실제 입금된 거래만 추가할 수 있습니다.")
        db.execute("""INSERT INTO cash_receipts(reference,amount,received_on,evidence,proposal_id,confirmed_by)
                      VALUES(?,?,?,?,?,?)""", (reference, *data, actor.user_id))
        return True


def _lines(db):
    # Reuse the payments screen's effective approval and correction queue so a
    # reapproval cannot introduce another obligation or clear a dispatch hold.
    from payments import list_payments

    plans = {p["instruction_id"]: p for p in db.execute(
        """SELECT p.*, e.reservation_version FROM cash_plans p LEFT JOIN (
               SELECT instruction_id, MAX(event_id) AS reservation_version FROM cash_plan_events
               WHERE action='reserve' GROUP BY instruction_id
           ) e USING(instruction_id)"""
    )}
    months = db.execute("""SELECT DISTINCT a.period_ym FROM payment_instructions i
                           JOIN approvals a USING(approval_id) ORDER BY a.period_ym""").fetchall()
    lines = []
    for (month,) in months:
        for order in list_payments(db, month)["orders"]:
            if order["state"] == "completed":
                continue
            iid = order["instruction_id"]
            plan = plans.get(iid)
            lines.append(dict(instruction_id=iid, rebate_id=order["rebate_id"], agency_id=order["agency_id"],
                              agency_name=order["agency_name"], period_ym=month, state=order["state"],
                              queue_state=order["queue_state"], approval_id=order["effective_approval_id"],
                              amount=order["amount_krw"], due_date=plan["due_date"] if plan else None,
                              schedule_version=plan["schedule_version"] if plan else 0,
                              reservation_version=plan["reservation_version"] if plan and plan["reserved"] else None,
                              allocated=bool(plan and plan["reserved"]),
                              reserved=order["amount_krw"] if plan and plan["reserved"] else 0))
    return sorted(lines, key=lambda r: (r["due_date"] or "9999-12-31", r["instruction_id"]))


def overview(db):
    db.execute("SAVEPOINT cash_view")
    try:
        baseline = _balance(db)
        receipts = [dict(r) for r in db.execute("SELECT * FROM cash_receipts ORDER BY received_on DESC,reference")]
        lines = _lines(db)
        confirmed = restricted = external = 0
        if baseline:
            confirmed = baseline["amount"] + sum(r["amount"] for r in receipts if r["received_on"] > baseline["as_of"])
            # A new bank closing balance incorporates debits through that date. A
            # debit recorded against the current snapshot must still be deducted.
            debits = db.execute("""SELECT amount FROM cash_mock_debits WHERE paid_on>? OR balance_id=?""",
                                (baseline["as_of"], baseline["balance_id"]))
            confirmed -= sum(r[0] for r in debits)
            restricted, external = baseline["restricted"], baseline["external"]
        reserved = sum(r["reserved"] for r in lines)
        balance_after_reservations = confirmed - restricted - external - reserved
        free, overcommitted = max(0, balance_after_reservations), max(0, -balance_after_reservations)
        days, cumulative = [], 0
        today = _today()
        for line in lines:
            line["funds_ready"] = bool(baseline and not overcommitted and line["allocated"]
                                       and line["due_date"] <= today)
        scheduled = [r for r in lines if r["due_date"]]
        for due_date, group in groupby(scheduled, key=lambda r: r["due_date"]):
            daily = list(group)
            total = sum(r["amount"] for r in daily)
            assigned = sum(r["reserved"] for r in daily)
            need = total - assigned
            cumulative += need
            days.append(dict(due_date=due_date, total=total, reserved=assigned, needed=need,
                             shortage=max(0, cumulative-free)))
        revision = db.execute("SELECT COALESCE(MAX(event_id),0) FROM cash_plan_events").fetchone()[0]
        version_data = [baseline["balance_id"] if baseline else None, len(receipts), confirmed, revision, lines]
        version = hashlib.sha256(json.dumps(version_data, sort_keys=True).encode()).hexdigest()
        return dict(baseline=dict(baseline) if baseline else None, confirmed=confirmed, reserved=reserved,
                    restricted=restricted, external=external, free=free, overcommitted=overcommitted,
                    days=days, lines=lines, receipts=receipts, version=version)
    finally:
        db.execute("RELEASE cash_view")


def _line(db, instruction_id):
    return next((r for r in _lines(db) if r["instruction_id"] == instruction_id), None)


def _event(db, actor, line, action, reason, due_date):
    db.execute("""INSERT INTO cash_plan_events(instruction_id,action,previous_date,due_date,amount,reason,actor)
                  VALUES(?,?,?,?,?,?,?)""", (line["instruction_id"], action, line["due_date"], due_date,
                                             line["amount"], reason, actor.user_id))


def schedule(db, actor, instruction_id, due_date, reason, *, expected_date=None, expected_version=None, proposal_id=None):
    require_role(actor, "treasury")
    due_date = _date(due_date, "지급예정일")
    reason = _text(reason, "일정 지정·변경 사유")
    with write(db):
        line = _line(db, instruction_id)
        if not line or line["state"] not in ("held", "ready", "failed"):
            raise ValueError("전송 전 또는 실패가 확정된 미지급 지시만 일정을 변경할 수 있습니다.")
        if line["due_date"] == due_date:
            return False
        if ((expected_date is not None and expected_date != line["due_date"]) or
                (expected_version is not None and str(expected_version) != str(line["schedule_version"]))):
            raise ValueError("예정일이 바뀌었습니다. 최신 계획을 확인하세요.")
        pending = db.execute("""SELECT proposal_id FROM funding_proposals WHERE instruction_id=?
                                AND status IN ('reschedule_pending','ready_to_move')""", (instruction_id,)).fetchone()
        if pending and pending[0] != proposal_id:
            raise ValueError("진행 중인 지급일 조정 작업에서 일정을 확정하세요.")
        db.execute("""INSERT INTO cash_plans(instruction_id,due_date,schedule_version) VALUES(?,?,1)
                      ON CONFLICT(instruction_id) DO UPDATE SET due_date=excluded.due_date,
                                                               schedule_version=schedule_version+1""",
                   (instruction_id, due_date))
        _event(db, actor, line, "schedule", reason, due_date)
        return True


def allocate(db, actor, due_date):
    require_role(actor, "treasury")
    due_date = _date(due_date, "배정 대상일")
    with write(db):
        view = overview(db)
        if not view["baseline"] or view["overcommitted"]:
            raise ValueError("자금 확인 필요: 기준 잔액, 사용제한액과 기존 배정을 대조하세요.")
        free = view["free"]
        allocated = []
        for line in view["lines"]:
            if not line["due_date"] or line["due_date"] > due_date:
                continue
            if line["allocated"] or line["queue_state"] not in ("held", "ready", "failed", "account_recheck"):
                continue
            if line["amount"] > free:
                break
            db.execute("UPDATE cash_plans SET reserved=1 WHERE instruction_id=?", (line["instruction_id"],))
            _event(db, actor, line, "reserve", "예정일 순 전액 배정", line["due_date"])
            allocated.append(line["instruction_id"])
            free -= line["amount"]
        return allocated


def release(db, actor, instruction_id, reason, *, expected_reservation=None):
    require_role(actor, "treasury")
    reason = _text(reason, "배정 해제 사유")
    with write(db):
        line = _line(db, instruction_id)
        if not line or line["state"] not in ("held", "ready", "failed"):
            raise ValueError("기존 송금 결과가 미확인되었거나 지급 완료된 지시의 배정을 해제할 수 없습니다.")
        if not line["allocated"]:
            return False
        if expected_reservation is not None and str(expected_reservation) != str(line["reservation_version"]):
            raise ValueError("기존 배정이 해제되고 다시 배정됐습니다. 최신 배정 기록을 확인하세요.")
        db.execute("UPDATE cash_plans SET reserved=0 WHERE instruction_id=?", (instruction_id,))
        _event(db, actor, line, "release", reason, line["due_date"])
        return True


def check_dispatch(db, instruction_id):
    if enabled(db):
        view = overview(db)
        if not any(r["instruction_id"] == instruction_id and r["funds_ready"] for r in view["lines"]):
            raise ValueError("대사한 회사 잔액, 전액 자금 배정 및 지급예정일 도래를 확인하세요.")


def invalidate_amount_reservation(db, actor, instruction_id, *, payee_changed=False):
    """Release the previously reserved sum inside the reapproval transaction."""
    if not enabled(db):
        return
    if not db.in_transaction:
        raise RuntimeError("금액 변경과 기존 배정 해제는 함께 기록해야 합니다.")
    plan = db.execute(
        """SELECT p.instruction_id, p.due_date, i.amount_krw AS amount FROM cash_plans p
           JOIN payment_instructions i USING(instruction_id)
           WHERE p.instruction_id=? AND p.reserved=1""", (instruction_id,),
    ).fetchone()
    if plan:
        db.execute("UPDATE cash_plans SET reserved=0 WHERE instruction_id=?", (instruction_id,))
        reason = "수취 대리점 변경으로 기존 배정 해제" if payee_changed else "재승인 금액 변경으로 기존 배정 해제"
        _event(db, actor, plan, "release", reason, plan["due_date"])


def set_debit_evidence(db, instruction_id, review_id, success_reply):
    """Relink an existing debit in the same transaction as its treasury decision."""
    if not db.in_transaction:
        raise RuntimeError("모의 출금 근거와 회계 결정은 함께 저장해야 합니다.")
    if not enabled(db):
        return
    debit = db.execute("SELECT reply_id FROM cash_mock_debits WHERE instruction_id=?",
                       (instruction_id,)).fetchone()
    if debit is None:
        raise ValueError("지급 완료 지시의 기존 모의 출금 기록을 확인하세요.")
    if debit["reply_id"] == success_reply:
        return
    db.execute("INSERT INTO debit_evidence_changes(review_id,previous_reply_id) VALUES(?,?)",
               (review_id, debit["reply_id"]))
    db.execute("UPDATE cash_mock_debits SET reply_id=? WHERE instruction_id=?",
               (success_reply, instruction_id))


def migrate_debit_evidence(db):
    db.execute("""CREATE TABLE IF NOT EXISTS debit_evidence_changes (
                      review_id INTEGER PRIMARY KEY REFERENCES bank_result_reviews,
                      previous_reply_id TEXT NOT NULL REFERENCES mock_bank_replies
                  )""")
    if not enabled(db):
        return
    # A nonpayment decision retires that debit. Only replay decisions for the active one.
    reviews = db.execute(
        """SELECT b.instruction_id, b.review_id, b.success_reply
           FROM bank_result_reviews b JOIN cash_mock_debits d USING(instruction_id)
           WHERE b.decision='completed' AND b.review_id > COALESCE((
               SELECT MAX(old.review_id) FROM bank_result_reviews old
               WHERE old.instruction_id=b.instruction_id AND old.decision='retry'
           ),0) ORDER BY b.review_id"""
    ).fetchall()
    for review in reviews:
        set_debit_evidence(db, review["instruction_id"], review["review_id"], review["success_reply"])


def reverse_mock_debit(db, actor, instruction_id, review_id):
    """Archive the one active simulated debit on an explicit treasury nonpayment decision."""
    require_role(actor, "treasury")
    if not db.in_transaction:
        raise RuntimeError("회계 결과 결정과 모의 출금 정정은 함께 저장해야 합니다.")
    if enabled(db):
        db.execute(
            """INSERT INTO reversed_mock_debits(review_id,instruction_id,reply_id,amount,paid_on,balance_id)
               SELECT ?, instruction_id, reply_id, amount, paid_on, balance_id FROM cash_mock_debits
               WHERE instruction_id=?""", (review_id, instruction_id),
        )
        db.execute("DELETE FROM cash_mock_debits WHERE instruction_id=?", (instruction_id,))


def reconcile_mock(db, actor, instruction_id, reply_id, state):
    """Called before changing the order state, inside the bank-reply write transaction."""
    if state not in ("completed", "review") or not enabled(db):
        return
    line = _line(db, instruction_id)
    if not line or not line["due_date"]:
        raise ValueError("은행 회신을 대조할 원 지급계획이 없습니다.")
    if state == "review":
        # A late, contradictory reply may arrive after a confirmed failure and release.
        if not line["allocated"]:
            db.execute("UPDATE cash_plans SET reserved=1 WHERE instruction_id=?", (instruction_id,))
            _event(db, actor, line, "reserve", f"회신 {reply_id} 결과 재확인", line["due_date"])
        return
    db.execute("""INSERT INTO cash_mock_debits(instruction_id,reply_id,amount,paid_on,balance_id)
                  VALUES(?,?,?,?,?)""", (instruction_id, reply_id, line["amount"], _today(), _balance(db)["balance_id"]))
    db.execute("UPDATE cash_plans SET reserved=0 WHERE instruction_id=?", (instruction_id,))
    _event(db, actor, line, "consume", f"모의 출금 대사 {reply_id}", line["due_date"])
