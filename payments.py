"""Local payment instructions and reconciliation of manually entered mock bank replies."""

import uuid

import funding
import monthly_basis
import amount_reviews

from rules import account_key, month_overview
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

BANK_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_result_reviews (
    review_id INTEGER PRIMARY KEY, instruction_id INTEGER NOT NULL REFERENCES payment_instructions,
    opened_by_reply TEXT NOT NULL UNIQUE REFERENCES mock_bank_replies,
    decision TEXT CHECK(decision IN ('completed','retry')), evidence TEXT, reviewed_by TEXT, reviewed_at TEXT,
    through_reply INTEGER, through_sequence INTEGER, success_reply TEXT REFERENCES mock_bank_replies
);
CREATE UNIQUE INDEX IF NOT EXISTS open_bank_review ON bank_result_reviews(instruction_id) WHERE decision IS NULL;
CREATE TABLE IF NOT EXISTS reversed_mock_debits (
    review_id INTEGER PRIMARY KEY REFERENCES bank_result_reviews, instruction_id INTEGER NOT NULL,
    reply_id TEXT NOT NULL, amount INTEGER NOT NULL, paid_on TEXT NOT NULL, balance_id INTEGER NOT NULL
);
INSERT OR IGNORE INTO bank_result_reviews(instruction_id,opened_by_reply)
SELECT t.instruction_id,r.reply_id FROM mock_payment_attempts t JOIN mock_bank_replies r USING(attempt_id)
WHERE EXISTS (SELECT 1 FROM mock_bank_replies earlier WHERE earlier.attempt_id=r.attempt_id
              AND earlier.rowid<r.rowid AND earlier.result<>r.result)
  AND NOT EXISTS (SELECT 1 FROM bank_result_reviews b WHERE b.instruction_id=t.instruction_id)
ORDER BY r.rowid;
UPDATE payment_instructions SET state='review' WHERE state!='completed' AND instruction_id IN (
    SELECT instruction_id FROM bank_result_reviews WHERE decision IS NULL
);
"""


def _open_bank_review(db, instruction_id):
    return db.execute("""SELECT * FROM bank_result_reviews
                         WHERE instruction_id=? AND decision IS NULL""", (instruction_id,)).fetchone()


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
    blocked = db.execute(
        """SELECT l.rebate_id FROM approval_lines l JOIN correction_tasks t USING(rebate_id)
           WHERE l.approval_id=? AND l.kind='new' AND t.status='open'
           AND NOT EXISTS (SELECT 1 FROM payment_instructions i
                           WHERE i.rebate_id=l.rebate_id AND i.approval_id=l.approval_id)
           LIMIT 1""", (approval_id,),
    ).fetchone()
    if blocked:
        raise ValueError(f"{blocked['rebate_id']}의 정정 후속 작업을 먼저 처리하세요.")
    return db.execute(
        """INSERT INTO payment_instructions(approval_id, rebate_id, agency_id, amount_krw)
           SELECT approval_id, rebate_id, agency_id, amount FROM approval_lines
           WHERE approval_id=? AND kind='new' ON CONFLICT(rebate_id) DO NOTHING""",
        (approval_id,),
    ).rowcount


def authorize_corrected_orders(db, approval_id, task_ids):
    """Link the fresh decision to held obligations; keep the original instructions and evidence."""
    if not db.in_transaction:
        raise RuntimeError("재승인은 결재 결정과 함께 저장해야 합니다.")
    for task_id in task_ids:
        task = db.execute(
            """SELECT t.status, t.kind, t.reapproval_id, i.state, i.amount_krw, l.amount
               FROM correction_tasks t
               JOIN approval_lines l ON l.rebate_id=t.rebate_id AND l.kind='approved'
               JOIN approvals a ON a.approval_id=l.approval_id AND a.period_ym=t.period_ym
               LEFT JOIN payment_instructions i ON i.instruction_id=t.instruction_id
               WHERE t.task_id=? AND a.approval_id=? AND a.status='승인'""",
            (task_id, approval_id),
        ).fetchone()
        if not task or task["kind"] != "reapproval":
            raise ValueError("재승인할 지급 건과 승인 명세가 일치하지 않습니다.")
        if task["status"] == "done" and task["reapproval_id"] == approval_id:
            continue
        if (task["status"] != "open" or task["state"] not in (None, "held", "ready", "failed")
                or (task["state"] and task["amount_krw"] != task["amount"])):
            raise ValueError("지급 상태가 바뀌었습니다. 기존 지급지시의 처리 결과를 확인하세요.")
        db.execute(
            """UPDATE correction_tasks SET status='done', reapproval_id=?,
                      resolved_by=(SELECT decided_by FROM approvals WHERE approval_id=?),
                      resolved_at=CURRENT_TIMESTAMP, resolution='정정 근거 재승인'
               WHERE task_id=?""", (approval_id, approval_id, task_id),
        )


def _check_dispatch_allowed(db, instruction_id):
    tasks = {row["kind"] for row in db.execute(
        "SELECT kind FROM correction_tasks WHERE instruction_id=? AND status='open'", (instruction_id,),
    )}
    if "bank_check" in tasks:
        raise ValueError("이미 전송한 지급지시입니다. 재전송을 보류하고 기존 시도의 은행 결과부터 확인하세요.")
    if "reapproval" in tasks:
        raise ValueError("등록정보 정정으로 지급 보류 상태입니다. 최신 근거로 재승인을 받아야 합니다.")


_ACCOUNT_RECHECKS_DUE = """
    SELECT t.task_id, t.instruction_id FROM correction_tasks t
    JOIN payment_instructions i USING(instruction_id)
    JOIN agency_corrections c USING(correction_id)
    JOIN correction_impacts p ON p.correction_id=t.correction_id AND p.period_ym=t.period_ym
    LEFT JOIN payment_account_rechecks h USING(task_id)
    WHERE i.state IN ('held','ready','failed') AND t.reapproval_id IS NOT NULL
      AND i.agency_id=c.agency_id AND json_type(p.changed_json,'$.bank_account_no') IS NOT NULL
      AND h.task_id IS NULL
"""


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
    """Verify the recipient, retaining every earlier verification when registration changes."""
    require_role(actor, "treasury")
    account = _text(account, "수취계좌")
    evidence = _text(evidence, "수취계좌 확인 증빙")
    if not account_key(account):
        raise ValueError("수취계좌에는 은행명과 계좌번호가 모두 필요합니다.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        _check_dispatch_allowed(db, instruction_id)
        checks = db.execute(_ACCOUNT_RECHECKS_DUE + " AND t.instruction_id=?", (instruction_id,)).fetchall()
        if order["state"] != "held" and not checks:
            if order["account"] == account and order["evidence"] == evidence:
                return
            raise ValueError("이미 확인된 수취계좌·증빙은 이 화면에서 변경할 수 없습니다.")
        for check in checks:
            db.execute(
                """INSERT INTO payment_account_rechecks
                   (task_id,instruction_id,previous_account,previous_evidence,previous_verified_by,
                    previous_verified_at,account,evidence,verified_by,verified_at)
                   SELECT ?,instruction_id,account,evidence,verified_by,verified_at,?,?,?,
                          strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   FROM payment_instructions WHERE instruction_id=?""",
                (check["task_id"], account, evidence, actor.user_id, instruction_id),
            )
        db.execute(
            """UPDATE payment_instructions SET account=?, evidence=?, verified_by=?,
                      verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                      state=CASE WHEN state='held' THEN 'ready' ELSE state END
               WHERE instruction_id=?""", (account, evidence, actor.user_id, instruction_id),
        )


def send_mock(db, actor, instruction_id, expected_attempt=None):
    """Record a simulated outgoing attempt locally. Retry must name the confirmed failed attempt."""
    require_role(actor, "treasury")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        if _open_bank_review(db, instruction_id):
            raise ValueError("은행 회신이 상충합니다. 회계가 모든 시도의 결과를 확인할 때까지 전송할 수 없습니다.")
        if amount_reviews.has_open_issue(db, order["rebate_id"]):
            raise ValueError("정산 원본 불일치로 지급 보류 상태입니다. 월 정산 접수에서 입력값과 근거를 확인하세요.")
        _check_dispatch_allowed(db, instruction_id)
        monthly_basis.check_dispatch(db, instruction_id)
        if db.execute(_ACCOUNT_RECHECKS_DUE + " AND t.instruction_id=? LIMIT 1", (instruction_id,)).fetchone():
            raise ValueError("등록계좌가 정정되었습니다. 회계에서 수취계좌와 증빙을 다시 확인해야 합니다.")
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

        funding.check_dispatch(db, instruction_id)
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


def _conflicting_reply(db, attempt, result):
    # A treasury decision covers exactly the evidence it saw. A later report for
    # any reviewed attempt can reopen the inquiry. A new attempt starts independently.
    reviewed = db.execute(
        """SELECT b.decision, success.attempt_id AS paid_attempt FROM bank_result_reviews b
           LEFT JOIN mock_bank_replies success ON success.reply_id=b.success_reply
           WHERE b.instruction_id=? AND b.decision IS NOT NULL AND b.through_sequence>=?
           ORDER BY b.review_id DESC LIMIT 1""", (attempt["instruction_id"], attempt["sequence_no"]),
    ).fetchone()
    if reviewed:
        confirmed_paid = reviewed["decision"] == "completed" and reviewed["paid_attempt"] == attempt["attempt_id"]
        return confirmed_paid != (result == "success")
    return bool(db.execute("SELECT 1 FROM mock_bank_replies WHERE attempt_id=? AND result<>? LIMIT 1",
                           (attempt["attempt_id"], result)).fetchone() or
                (attempt["sequence_no"] < attempt["latest_sequence"] and result == "success"))


def resolve_bank_review(db, actor, instruction_id, decision, evidence, reply_version, success_reply=""):
    """Record treasury's conclusion for all attempts, serialized with incoming bank replies."""
    require_role(actor, "treasury")
    if decision not in ("completed", "retry"):
        raise ValueError("지급 완료 또는 모든 시도 미지급 확인을 선택하세요.")
    evidence = _text(evidence, "모든 전송 시도의 은행 조회 결과와 확인 증빙")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        review = _open_bank_review(db, instruction_id)
        if not review:
            raise ValueError("회계 확인을 기다리는 상충 회신이 없습니다.")
        replies = db.execute(
            """SELECT r.*, r.rowid AS version,t.amount_krw AS sent_amount,t.account AS sent_account
               FROM mock_payment_attempts t JOIN mock_bank_replies r USING(attempt_id)
               WHERE t.instruction_id=? ORDER BY r.rowid""", (instruction_id,),
        ).fetchall()
        version = replies[-1]["version"]
        if str(version) != str(reply_version):
            raise ValueError("은행 회신이 바뀌었습니다. 모든 시도와 최신 회신을 다시 확인하세요.")
        if decision == "completed":
            proof = next((r for r in replies if r["reply_id"] == success_reply), None)
            if not proof or proof["result"] != "success" or proof["amount_krw"] != proof["sent_amount"] or (
                    account_key(proof["account"]) != account_key(proof["sent_account"])):
                raise ValueError("기존 시도의 금액과 수취계좌가 일치하는 성공 회신을 선택하세요.")
            if proof["amount_krw"] != order["amount_krw"]:
                raise ValueError("선택한 과거 시도의 지급액과 현재 승인 전액이 다릅니다. 은행 증빙과 승인 금액을 확인하세요.")
        else:
            success_reply = None
        db.execute(
            """UPDATE bank_result_reviews SET decision=?, evidence=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP,
                      through_reply=?, through_sequence=(SELECT MAX(sequence_no) FROM mock_payment_attempts
                      WHERE instruction_id=?), success_reply=? WHERE review_id=?""",
            (decision, evidence, actor.user_id, version, instruction_id, success_reply, review["review_id"]),
        )
        state = "completed" if decision == "completed" else "failed"
        if order["state"] == "completed" and state == "failed":
            funding.reverse_mock_debit(db, actor, instruction_id, review["review_id"])
            db.execute("UPDATE payment_instructions SET state='review' WHERE instruction_id=?", (instruction_id,))
            funding.reconcile_mock(db, actor, instruction_id, review["opened_by_reply"], "review")
        if order["state"] != "completed" and state == "completed":
            funding.reconcile_mock(db, actor, instruction_id, success_reply, "completed")
        db.execute("UPDATE payment_instructions SET state=? WHERE instruction_id=?", (state, instruction_id))
        # A registration correction may have been attached to any of these attempts.
        for attempt in db.execute("SELECT * FROM mock_payment_attempts WHERE instruction_id=?", (instruction_id,)):
            _record_correction_followup(db, attempt, state, f"회계 확인 #{review['review_id']}", actor)


def _record_correction_followup(db, attempt, state, reply_id, actor):
    # Keep a resolved bank inquiry and its follow-up as separate auditable tasks.
    tasks = db.execute(
        """SELECT * FROM correction_tasks WHERE attempt_id=? AND (
             (kind IN ('bank_check','reapproval') AND (status='open' OR reapproval_id IS NOT NULL))
             OR (kind='post_review' AND ?='failed'))
           ORDER BY task_id""", (attempt["attempt_id"], state),
    ).fetchall()
    if not tasks:
        return
    next_kind = {"completed": "post_review", "failed": "reapproval", "review": "bank_check"}[state]
    moving = [task for task in tasks if task["kind"] != next_kind]
    if not moving:
        return
    # The original correction might have been followed by a second merge or split.
    row = moving[0]
    group_id = next(line["group_id"] for line in month_overview(db, row["period_ym"])["rows"]
                    if line["rebate_id"] == row["rebate_id"])
    for task in moving:
        if task["status"] == "open":
            db.execute(
                """UPDATE correction_tasks SET status='done', resolved_by=?, resolved_at=CURRENT_TIMESTAMP,
                          resolution=? WHERE task_id=?""",
                (actor.user_id, f"모의 은행 회신 {reply_id}", task["task_id"]),
            )
        db.execute(
            """INSERT INTO correction_tasks(correction_id,period_ym,group_id,rebate_id,instruction_id,attempt_id,kind)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(correction_id,rebate_id,kind) WHERE status='open' DO NOTHING""",
            (task["correction_id"], task["period_ym"], group_id, task["rebate_id"],
             attempt["instruction_id"], attempt["attempt_id"], next_kind),
        )


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
            """SELECT t.*, i.state, i.amount_krw AS current_amount,
                      (SELECT MAX(sequence_no) FROM mock_payment_attempts
                       WHERE instruction_id=t.instruction_id) AS latest_sequence
               FROM mock_payment_attempts t JOIN payment_instructions i USING(instruction_id)
               WHERE attempt_id=?""", (attempt_id,),
        ).fetchone()
        outcome, state, reason = _compare_reply(attempt, amount, account, result)
        needs_review = attempt is not None and (
            _open_bank_review(db, attempt["instruction_id"]) or _conflicting_reply(db, attempt, result)
            or (result == "success" and attempt["amount_krw"] != attempt["current_amount"]))
        if needs_review:
            matches = amount == attempt["amount_krw"] and account_key(account) == account_key(attempt["account"])
            comparison = "금액·수취계좌 일치" if matches else "금액·수취계좌 불일치"
            outcome = "review"
            state = None if attempt["state"] == "completed" else "review"
            reason = f"{comparison} · 상충 회신 보류. 회계가 모든 시도의 결과를 확인해야 합니다."
        db.execute(
            """INSERT INTO mock_bank_replies
                      (reply_id,attempt_id,amount_krw,account,result,outcome,reason,received_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (reply_id, attempt_id, amount, account, result, outcome, reason, actor.user_id),
        )
        if needs_review:
            db.execute("""INSERT INTO bank_result_reviews(instruction_id,opened_by_reply) VALUES(?,?)
                          ON CONFLICT(instruction_id) WHERE decision IS NULL DO NOTHING""",
                       (attempt["instruction_id"], reply_id))
        if state:
            funding.reconcile_mock(db, actor, attempt["instruction_id"], reply_id, state)
            db.execute("UPDATE payment_instructions SET state=? WHERE instruction_id=?",
                       (state, attempt["instruction_id"]))
            _record_correction_followup(db, attempt, state, reply_id, actor)
        return outcome


def list_payments(db, month):
    """Return one coherent view of the month's instructions, attempts and replies."""
    db.execute("SAVEPOINT payment_view")
    try:
        orders = [
            dict(row, attempts=[], replies=[], latest_attempt=None, queue_state=row["state"], bank_reviews=[],
                 bank_conflict=False, reply_version=0,
                 correction_tasks=[], account_rechecks=[], post_review=False, effective_approval_id=row["approval_id"])
            for row in db.execute(
                """SELECT i.*, a.period_ym, g.agency_name FROM payment_instructions i
                   JOIN approvals a USING(approval_id) JOIN agencies g USING(agency_id)
                   WHERE a.period_ym=? ORDER BY i.instruction_id DESC""", (month,),
            )
        ]
        by_order = {order["instruction_id"]: order for order in orders}
        for due in db.execute(_ACCOUNT_RECHECKS_DUE + " AND t.period_ym=?", (month,)):
            by_order[due["instruction_id"]]["queue_state"] = "account_recheck"
        for check in db.execute(
            """SELECT h.* FROM payment_account_rechecks h JOIN correction_tasks t USING(task_id)
               WHERE t.period_ym=? ORDER BY h.task_id""", (month,),
        ):
            by_order[check["instruction_id"]]["account_rechecks"].append(dict(check))
        for task in db.execute(
            """SELECT * FROM correction_tasks WHERE period_ym=? AND instruction_id IS NOT NULL
               ORDER BY task_id""", (month,),
        ):
            order = by_order[task["instruction_id"]]
            order["correction_tasks"].append(dict(task))
            if task["status"] == "open":
                if task["kind"] == "post_review":
                    order["post_review"] = True
                elif task["kind"] == "bank_check" or order["queue_state"] != "bank_check":
                    order["queue_state"] = task["kind"]
            if task["reapproval_id"] is not None:
                order["effective_approval_id"] = max(order["effective_approval_id"], task["reapproval_id"])
        for attempt in db.execute(
            """SELECT t.* FROM mock_payment_attempts t JOIN payment_instructions i USING(instruction_id)
               JOIN approvals a USING(approval_id) WHERE a.period_ym=? ORDER BY t.sequence_no""", (month,),
        ):
            order = by_order[attempt["instruction_id"]]
            order["attempts"].append(dict(attempt))
            order["latest_attempt"] = attempt["attempt_id"]
        for reply in db.execute(
            """SELECT r.*, r.rowid AS version,t.instruction_id, t.amount_krw AS sent_amount,t.account AS sent_account
               FROM mock_bank_replies r JOIN mock_payment_attempts t USING(attempt_id)
               JOIN payment_instructions i USING(instruction_id) JOIN approvals a USING(approval_id)
               WHERE a.period_ym=? ORDER BY r.rowid""", (month,),
        ):
            order = by_order[reply["instruction_id"]]
            order["reply_version"] = reply["version"]
            order["replies"].append(dict(reply, confirmed_success=reply["result"] == "success" and
                                         reply["amount_krw"] == reply["sent_amount"] and
                                         reply["amount_krw"] == order["amount_krw"] and
                                         account_key(reply["account"]) == account_key(reply["sent_account"])))
        for review in db.execute(
            """SELECT b.* FROM bank_result_reviews b JOIN payment_instructions i USING(instruction_id)
               JOIN approvals a USING(approval_id) WHERE a.period_ym=? ORDER BY review_id""", (month,),
        ):
            order = by_order[review["instruction_id"]]
            order["bank_reviews"].append(dict(review))
            if review["decision"] is None:
                order["bank_conflict"] = True
                order["queue_state"] = "bank_check"
        unmatched = [dict(row) for row in db.execute(
            "SELECT * FROM mock_bank_replies WHERE outcome='unmatched' ORDER BY received_at DESC, rowid DESC"
        )]
        monthly_basis.annotate_orders(db, month, orders)
        unresolved = {row[0] for row in db.execute(
            "SELECT DISTINCT rebate_id FROM open_intake_issues WHERE period_ym=?", (month,)
        )}
        for order in orders:
            order["source_hold"] = order["rebate_id"] in unresolved
            if order["source_hold"] and order["queue_state"] in ("held", "ready", "failed"):
                order["queue_state"] = "source_review"
        return {"orders": orders, "unmatched": unmatched}
    finally:
        db.execute("RELEASE payment_view")
