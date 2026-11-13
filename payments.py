"""Local payment instructions and reconciliation of manually entered mock bank replies."""

import uuid

from rules import account_key
from workflow_security import require_role


PAYMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_instructions (
    instruction_id INTEGER PRIMARY KEY,
    approval_id INTEGER NOT NULL REFERENCES approvals(approval_id),
    rebate_id TEXT NOT NULL UNIQUE REFERENCES rebates(rebate_id),
    agency_id TEXT NOT NULL REFERENCES agencies(agency_id),
    amount_krw INTEGER NOT NULL CHECK(amount_krw >= 0),
    state TEXT NOT NULL DEFAULT 'held'
      CHECK(state IN ('held','ready','sent','review','failed','completed')),
    account TEXT, evidence TEXT, verified_by TEXT, verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS mock_payment_attempts (
    attempt_id TEXT PRIMARY KEY,
    instruction_id INTEGER NOT NULL REFERENCES payment_instructions(instruction_id),
    sequence_no INTEGER NOT NULL,
    amount_krw INTEGER NOT NULL, account TEXT NOT NULL, sent_by TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(instruction_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS mock_bank_replies (
    reply_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
    amount_krw INTEGER NOT NULL, account TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('success','failure')),
    outcome TEXT NOT NULL CHECK(outcome IN ('matched','failed','review','unmatched','late')),
    reason TEXT NOT NULL, received_by TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS replies_by_attempt ON mock_bank_replies(attempt_id);
"""


def create_instructions(db, approval_id):
    """Run within the decision's write transaction so an approval and its orders commit together."""
    if not db.in_transaction:
        raise RuntimeError("지급지시 생성에는 결재 트랜잭션이 필요합니다.")
    approval = db.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if approval is None or approval["status"] != "승인":
        raise ValueError("승인된 결재서에서만 지급지시를 생성할 수 있습니다.")
    # A replay of this same decision is harmless. A different approval may
    # reference this obligation for its monthly total but must not approve it again.
    overlap = db.execute(
        """SELECT i.rebate_id FROM approval_lines l JOIN payment_instructions i USING(rebate_id)
           WHERE l.approval_id=? AND l.kind='new' AND i.approval_id<>l.approval_id LIMIT 1""",
        (approval_id,),
    ).fetchone()
    if overlap:
        raise ValueError(f"{overlap['rebate_id']}에는 이미 유효한 지급지시가 있습니다.")
    return db.execute(
        """INSERT INTO payment_instructions(approval_id, rebate_id, agency_id, amount_krw)
           SELECT approval_id, rebate_id, agency_id, amount FROM approval_lines
           WHERE approval_id=? AND kind='new' ON CONFLICT(rebate_id) DO NOTHING""",
        (approval_id,),
    ).rowcount


def _instruction(db, instruction_id):
    row = db.execute(
        """SELECT i.*, a.status AS decision FROM payment_instructions i
           JOIN approvals a USING(approval_id) WHERE instruction_id=?""", (instruction_id,),
    ).fetchone()
    if row is None:
        raise ValueError("지급지시를 찾을 수 없습니다.")
    if row["decision"] != "승인":
        raise ValueError("유효한 승인 명세가 필요합니다.")
    return row


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value.strip().upper() == "NONE":
        raise ValueError(f"{field}을(를) 입력해 주세요.")
    return value.strip()


def confirm_account(db, actor, instruction_id, account, evidence):
    """Freeze the independently confirmed recipient on this instruction before its first send."""
    require_role(actor, "treasury")
    account = _text(account, "수취계좌")
    evidence = _text(evidence, "수취계좌 확인 증빙")
    if not account_key(account):
        raise ValueError("수취계좌에는 은행명과 계좌번호가 모두 필요합니다.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        if order["state"] != "held":
            if order["account"] == account and order["evidence"] == evidence:
                return
            raise ValueError("이미 확인된 수취계좌·증빙은 이 화면에서 변경할 수 없습니다.")
        db.execute(
            """UPDATE payment_instructions SET account=?, evidence=?, verified_by=?,
                      verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), state='ready'
               WHERE instruction_id=?""", (account, evidence, actor.user_id, instruction_id),
        )


def send_mock(db, actor, instruction_id, expected_attempt=None):
    """Record a simulated outgoing attempt locally. Retry must name the confirmed failed attempt."""
    require_role(actor, "treasury")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        if order["state"] == "held" or not order["account"]:
            raise ValueError("수취계좌와 증빙을 확인할 때까지 지급 보류 상태입니다.")
        latest = db.execute(
            """SELECT attempt_id, sequence_no FROM mock_payment_attempts
               WHERE instruction_id=? ORDER BY sequence_no DESC LIMIT 1""", (instruction_id,),
        ).fetchone()
        if order["state"] == "failed":
            if not latest or expected_attempt != latest["attempt_id"]:
                raise ValueError("최근 시도의 실패 회신을 확인한 뒤 해당 시도 번호로 재처리하세요.")
        elif latest:
            # Includes repeat submission after dispatch, review or reconciliation.
            return latest["attempt_id"]
        elif expected_attempt:
            raise ValueError("이 지급지시에는 재처리할 이전 시도가 없습니다.")

        attempt_id = "MOCK-" + uuid.uuid4().hex
        db.execute(
            """INSERT INTO mock_payment_attempts
                      (attempt_id,instruction_id,sequence_no,amount_krw,account,sent_by)
               VALUES (?,?,?,?,?,?)""",
            (attempt_id, instruction_id, latest["sequence_no"] + 1 if latest else 1,
             order["amount_krw"], order["account"], actor.user_id),
        )
        db.execute("UPDATE payment_instructions SET state='sent' WHERE instruction_id=?", (instruction_id,))
        return attempt_id


def _compare_reply(attempt, amount, account, result):
    if attempt is None:
        return "unmatched", None, "지급 시도 번호를 찾을 수 없습니다."
    differences = []
    if amount != attempt["amount_krw"]:
        differences.append("금액")
    if account_key(account) != account_key(attempt["account"]):
        differences.append("수취계좌")
    if differences:
        detail = "·".join(differences) + " 불일치"
    else:
        detail = "금액·수취계좌 일치"
    if attempt["sequence_no"] != attempt["latest_sequence"]:
        return "late", None, f"이전 시도 회신 · {detail}. 해당 시도의 앞선 회신과 대조하세요."
    if attempt["state"] == "completed":
        return "late", None, f"완료된 지시의 추가 회신 · {detail}."
    if attempt["state"] == "failed":
        if result == "success" or differences:
            return "review", "review", f"확정 실패와 상충하는 회신 · {detail}. 재전송을 보류합니다."
        return "late", None, "이미 실패 확인된 시도의 추가 실패 회신입니다."
    if differences:
        return "review", "review", f"{detail}. 기존 시도의 결과를 확인하세요."
    if result == "failure":
        return "failed", "failed", "금액·수취계좌는 일치하며 모의 은행이 실패를 확인했습니다."
    return "matched", "completed", "금액·수취계좌 일치, 모의 은행 성공 확인."


def record_reply(db, actor, reply_id, attempt_id, amount, account, result):
    require_role(actor, "treasury")
    reply_id = _text(reply_id, "모의 회신 번호")
    attempt_id = _text(attempt_id, "모의 지급 시도 번호")
    account = _text(account, "회신 수취계좌")
    amount_text = str(amount)
    if (not amount_text.isascii() or not amount_text.isdecimal() or len(amount_text) > 19
            or int(amount_text) > 2**63 - 1):
        raise ValueError("회신 금액은 0 이상의 원 단위 정수로 입력하세요.")
    amount = int(amount_text)
    if result not in ("success", "failure"):
        raise ValueError("모의 은행 처리결과는 성공 또는 실패여야 합니다.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM mock_bank_replies WHERE reply_id=?", (reply_id,)).fetchone()
        if prior:
            if (prior["attempt_id"], prior["amount_krw"], prior["account"], prior["result"]) != (
                    attempt_id, amount, account, result):
                raise ValueError("같은 회신 번호에 다른 내용이 등록되어 있습니다. 입력값을 확인하세요.")
            return prior["outcome"]
        attempt = db.execute(
            """SELECT t.*, i.state,
                      (SELECT MAX(sequence_no) FROM mock_payment_attempts
                       WHERE instruction_id=t.instruction_id) AS latest_sequence
               FROM mock_payment_attempts t JOIN payment_instructions i USING(instruction_id)
               WHERE attempt_id=?""", (attempt_id,),
        ).fetchone()
        outcome, state, reason = _compare_reply(attempt, amount, account, result)
        db.execute(
            """INSERT INTO mock_bank_replies
                      (reply_id,attempt_id,amount_krw,account,result,outcome,reason,received_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (reply_id, attempt_id, amount, account, result, outcome, reason, actor.user_id),
        )
        if state:
            db.execute("UPDATE payment_instructions SET state=? WHERE instruction_id=?",
                       (state, attempt["instruction_id"]))
        return outcome


def list_payments(db, month):
    """Return one coherent view of the month's instructions, attempts and replies."""
    db.execute("SAVEPOINT payment_view")
    try:
        orders = [
            dict(row, attempts=[], replies=[], latest_attempt=None) for row in db.execute(
                """SELECT i.*, a.period_ym, g.agency_name FROM payment_instructions i
                   JOIN approvals a USING(approval_id) JOIN agencies g USING(agency_id)
                   WHERE a.period_ym=? ORDER BY i.instruction_id DESC""", (month,),
            )
        ]
        by_order = {order["instruction_id"]: order for order in orders}
        for attempt in db.execute(
            """SELECT t.* FROM mock_payment_attempts t JOIN payment_instructions i USING(instruction_id)
               JOIN approvals a USING(approval_id) WHERE a.period_ym=? ORDER BY t.sequence_no""", (month,),
        ):
            order = by_order[attempt["instruction_id"]]
            order["attempts"].append(dict(attempt))
            order["latest_attempt"] = attempt["attempt_id"]
        for reply in db.execute(
            """SELECT r.*, t.instruction_id FROM mock_bank_replies r JOIN mock_payment_attempts t USING(attempt_id)
               JOIN payment_instructions i USING(instruction_id) JOIN approvals a USING(approval_id)
               WHERE a.period_ym=? ORDER BY r.received_at, r.rowid""", (month,),
        ):
            by_order[reply["instruction_id"]]["replies"].append(dict(reply))
        unmatched = [dict(row) for row in db.execute(
            "SELECT * FROM mock_bank_replies WHERE outcome='unmatched' ORDER BY received_at DESC, rowid DESC"
        )]
        return {"orders": orders, "unmatched": unmatched}
    finally:
        db.execute("RELEASE payment_view")
