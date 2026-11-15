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


def _reply_differences(attempt, amount, account):
    differences = []
    if amount != attempt["amount_krw"]:
        differences.append("금액")
    if account_key(account) != account_key(attempt["account"]):
        differences.append("수취계좌")
    return differences


def _reviewed_discrepancy(db, attempt_id, amount, account):
    # A fresh report ID can repeat evidence already considered by treasury.
    reports = db.execute(
        """SELECT r.reply_id,r.account FROM bank_review_discrepancies d
           JOIN bank_result_reviews b USING(review_id) JOIN mock_bank_replies r USING(reply_id)
           WHERE b.decision IS NOT NULL AND r.attempt_id=? AND r.amount_krw=? AND r.result='success'
           ORDER BY b.review_id DESC""", (attempt_id, amount),
    )
    key = account_key(account) or account
    return next((r["reply_id"] for r in reports if (account_key(r["account"]) or r["account"]) == key), None)


def _bank_task(db, instruction_id, reply_id, kind):
    db.execute(
        """INSERT INTO bank_result_reviews(instruction_id,opened_by_reply,review_kind) VALUES(?,?,?)
           ON CONFLICT(instruction_id) WHERE decision IS NULL DO UPDATE SET review_kind='outcome'
           WHERE excluded.review_kind='outcome'""", (instruction_id, reply_id, kind),
    )
    return _open_bank_review(db, instruction_id)["review_id"]


def migrate_completed_discrepancies(db):
    """Connect previously recorded late mismatches to the decision that saw them or to an open task."""
    if "review_kind" not in {r["name"] for r in db.execute("PRAGMA table_info(bank_result_reviews)")}:
        db.execute("""ALTER TABLE bank_result_reviews ADD COLUMN review_kind TEXT NOT NULL DEFAULT 'outcome'
                      CHECK (review_kind IN ('outcome','discrepancy'))""")
    db.execute("""CREATE TABLE IF NOT EXISTS bank_review_discrepancies (
                    reply_id TEXT PRIMARY KEY REFERENCES mock_bank_replies,
                    review_id INTEGER NOT NULL REFERENCES bank_result_reviews)""")
    rows = db.execute(
        """WITH transitions(instruction_id, version, complete) AS (
               SELECT t.instruction_id,r.rowid,1 FROM mock_bank_replies r
               JOIN mock_payment_attempts t USING(attempt_id) WHERE r.outcome='matched'
               UNION ALL
               SELECT instruction_id,through_reply,decision='completed' FROM bank_result_reviews
               WHERE decision IS NOT NULL
           )
           SELECT r.reply_id,r.attempt_id,r.amount_krw AS reported_amount,r.account AS reported_account,
                  r.rowid AS version,t.instruction_id,t.amount_krw,t.account
           FROM mock_bank_replies r JOIN mock_payment_attempts t USING(attempt_id)
           JOIN payment_instructions i USING(instruction_id)
           WHERE i.state='completed' AND r.result='success' AND r.outcome IN ('late','review')
             AND (SELECT complete FROM transitions WHERE instruction_id=i.instruction_id AND version<r.rowid
                  ORDER BY version DESC LIMIT 1)
           ORDER BY r.rowid"""
    ).fetchall()
    for row in rows:
        amount, account = row["reported_amount"], row["reported_account"]
        if not _reply_differences(row, amount, account):
            continue
        # A past decision already covers replies through its saved input version.
        decision = db.execute("""SELECT review_id FROM bank_result_reviews
                                 WHERE instruction_id=? AND through_reply>=? ORDER BY review_id LIMIT 1""",
                              (row["instruction_id"], row["version"])).fetchone()
        if decision:
            review_id = decision["review_id"]
        elif _reviewed_discrepancy(db, row["attempt_id"], amount, account):
            continue
        else:
            review_id = _bank_task(db, row["instruction_id"], row["reply_id"], "discrepancy")
        db.execute("INSERT OR IGNORE INTO bank_review_discrepancies(reply_id,review_id) VALUES(?,?)",
                   (row["reply_id"], review_id))


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


def confirm_account(db, actor, instruction_id, account, evidence, expected_agency_id=None):
    """Verify the recipient, retaining every earlier verification when registration changes."""
    require_role(actor, "treasury")
    account = _text(account, "수취계좌")
    evidence = _text(evidence, "수취계좌 확인 증빙")
    if not account_key(account):
        raise ValueError("수취계좌에는 은행명과 계좌번호가 모두 필요합니다.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _instruction(db, instruction_id)
        changed_payee = db.execute(
            "SELECT 1 FROM payment_payee_changes WHERE instruction_id=? LIMIT 1", (instruction_id,),
        ).fetchone()
        if (expected_agency_id or changed_payee) and expected_agency_id != order["agency_id"]:
            raise ValueError("지급 대리점이 바뀌었습니다. 현재 대리점의 수취계좌 증빙을 다시 확인하세요.")
        _check_dispatch_allowed(db, instruction_id)
        checks = db.execute(_ACCOUNT_RECHECKS_DUE + " AND t.instruction_id=?", (instruction_id,)).fetchall()
        if order["state"] != "held" and not (order["state"] == "failed" and not order["account"]) and not checks:
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
                      (attempt_id,instruction_id,sequence_no,amount_krw,account,sent_by,payee_agency_id)
               VALUES (?,?,?,?,?,?,?)""",
            (attempt_id, instruction_id, latest["sequence_no"] + 1 if latest else 1,
             order["amount_krw"], order["account"], actor.user_id, order["agency_id"]),
        )
        db.execute("UPDATE payment_instructions SET state='sent' WHERE instruction_id=?", (instruction_id,))
        return attempt_id


def _compare_reply(attempt, amount, account, result):
    if attempt is None:
        return "unmatched", None, "지급 시도 번호를 찾을 수 없습니다."
    differences = _reply_differences(attempt, amount, account)
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
        review = db.execute(
            "SELECT * FROM bank_result_reviews WHERE instruction_id=? ORDER BY review_id DESC LIMIT 1",
            (instruction_id,),
        ).fetchone()
        if not review:
            raise ValueError("회계 확인을 기다리는 상충 회신이 없습니다.")
        replies = db.execute(
            """SELECT r.*, r.rowid AS version,t.amount_krw AS sent_amount,t.account AS sent_account,t.payee_agency_id
               FROM mock_payment_attempts t JOIN mock_bank_replies r USING(attempt_id)
               WHERE t.instruction_id=? ORDER BY r.rowid""", (instruction_id,),
        ).fetchall()
        version = replies[-1]["version"]
        if str(version) != str(reply_version):
            raise ValueError("은행 회신이 바뀌었습니다. 모든 시도와 최신 회신을 다시 확인하세요.")
        if decision == "retry":
            success_reply = None
        state = "completed" if decision == "completed" else "failed"
        if review["decision"] is not None:
            previous = (review["decision"], review["success_reply"], review["evidence"], review["reviewed_by"],
                        review["through_reply"])
            if order["state"] == state and previous == (decision, success_reply, evidence, actor.user_id, version):
                return
            raise ValueError("이미 확정한 회계 결정입니다. 최신 은행 회신과 확인 이력을 조회하세요.")
        if review["review_kind"] == "discrepancy" and decision == "retry":
            raise ValueError("완료 후 금액·수취계좌 불일치입니다. 기존 지급 증빙을 확인하고 지급 완료 근거를 기록하세요.")
        if decision == "completed":
            proof = next((r for r in replies if r["reply_id"] == success_reply), None)
            if not proof or proof["result"] != "success" or proof["amount_krw"] != proof["sent_amount"] or (
                    account_key(proof["account"]) != account_key(proof["sent_account"])):
                raise ValueError("기존 시도의 금액과 수취계좌가 일치하는 성공 회신을 선택하세요.")
            if proof["amount_krw"] != order["amount_krw"]:
                raise ValueError("선택한 과거 시도의 지급액과 현재 승인 전액이 다릅니다. 은행 증빙과 승인 금액을 확인하세요.")
            if proof["payee_agency_id"] != order["agency_id"]:
                raise ValueError("선택한 지급 시도의 수취 대리점과 현재 승인된 대리점이 다릅니다.")
        db.execute(
            """UPDATE bank_result_reviews SET decision=?, evidence=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP,
                      through_reply=?, through_sequence=(SELECT MAX(sequence_no) FROM mock_payment_attempts
                      WHERE instruction_id=?), success_reply=? WHERE review_id=?""",
            (decision, evidence, actor.user_id, version, instruction_id, success_reply, review["review_id"]),
        )
        if order["state"] == "completed" and state == "failed":
            funding.reverse_mock_debit(db, actor, instruction_id, review["review_id"])
            db.execute("UPDATE payment_instructions SET state='review' WHERE instruction_id=?", (instruction_id,))
            funding.reconcile_mock(db, actor, instruction_id, review["opened_by_reply"], "review")
        if state == "completed":
            if order["state"] == "completed":
                funding.set_debit_evidence(db, instruction_id, review["review_id"], success_reply)
            else:
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
            """SELECT t.*, i.state, i.amount_krw AS current_amount,i.agency_id AS current_agency,
                      (SELECT MAX(sequence_no) FROM mock_payment_attempts
                       WHERE instruction_id=t.instruction_id) AS latest_sequence
               FROM mock_payment_attempts t JOIN payment_instructions i USING(instruction_id)
               WHERE attempt_id=?""", (attempt_id,),
        ).fetchone()
        outcome, state, reason = _compare_reply(attempt, amount, account, result)
        review_kind = None
        late_mismatch = False
        if attempt is not None:
            open_review = _open_bank_review(db, attempt["instruction_id"])
            differences = _reply_differences(attempt, amount, account)
            late_mismatch = attempt["state"] == "completed" and result == "success" and bool(differences)
            reviewed_copy = late_mismatch and _reviewed_discrepancy(db, attempt_id, amount, account)
            conflict = _conflicting_reply(db, attempt, result) or (
                result == "success" and (attempt["amount_krw"] != attempt["current_amount"]
                                         or attempt["payee_agency_id"] != attempt["current_agency"]))
            if reviewed_copy and not open_review and not conflict:
                reason = f"회계가 확인한 불일치 회신 {reviewed_copy}와 같은 내용입니다."
            elif conflict:
                review_kind = "outcome"
            elif late_mismatch:
                review_kind = "discrepancy"
            elif open_review:
                review_kind = open_review["review_kind"]
            if review_kind:
                outcome = "review"
                state = None if attempt["state"] == "completed" else "review"
                comparison = "·".join(differences) + " 불일치" if differences else "금액·수취계좌 일치"
                reason = f"{comparison} · 회계가 은행 회신과 지급 근거를 재검토해야 합니다."
        db.execute(
            """INSERT INTO mock_bank_replies
                      (reply_id,attempt_id,amount_krw,account,result,outcome,reason,received_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (reply_id, attempt_id, amount, account, result, outcome, reason, actor.user_id),
        )
        if review_kind:
            review_id = _bank_task(db, attempt["instruction_id"], reply_id, review_kind)
            if late_mismatch:
                db.execute("INSERT INTO bank_review_discrepancies(reply_id,review_id) VALUES(?,?)",
                           (reply_id, review_id))
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
            dict(row, attempts=[], replies=[], latest_attempt=None, queue_state=row["state"], bank_reviews=[], debit=None,
                 bank_conflict=False, bank_review_kind=None, reply_version=0,
                 correction_tasks=[], account_rechecks=[], payee_changes=[],
                 post_review=False, effective_approval_id=row["approval_id"])
            for row in db.execute(
                """SELECT i.*, a.period_ym, g.agency_name FROM payment_instructions i
                   JOIN approvals a USING(approval_id) JOIN agencies g USING(agency_id)
                   WHERE a.period_ym=? ORDER BY i.instruction_id DESC""", (month,),
            )
        ]
        by_order = {order["instruction_id"]: order for order in orders}
        for order in orders:
            if order["state"] == "failed" and not order["account"]:
                order["queue_state"] = "held"
        for change in db.execute(
            """SELECT c.* FROM payment_payee_changes c JOIN approvals a ON a.approval_id=c.reapproval_id
               WHERE a.period_ym=? ORDER BY c.reapproval_id""", (month,),
        ):
            by_order[change["instruction_id"]]["payee_changes"].append(dict(change))
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
            """SELECT r.*, r.rowid AS version,t.instruction_id,t.amount_krw AS sent_amount,t.account AS sent_account,
                      t.payee_agency_id
               FROM mock_bank_replies r JOIN mock_payment_attempts t USING(attempt_id)
               JOIN payment_instructions i USING(instruction_id) JOIN approvals a USING(approval_id)
               WHERE a.period_ym=? ORDER BY r.rowid""", (month,),
        ):
            order = by_order[reply["instruction_id"]]
            order["reply_version"] = reply["version"]
            order["replies"].append(dict(reply, confirmed_success=reply["result"] == "success" and
                                         reply["amount_krw"] == reply["sent_amount"] and
                                         reply["amount_krw"] == order["amount_krw"] and
                                         reply["payee_agency_id"] == order["agency_id"] and
                                         account_key(reply["account"]) == account_key(reply["sent_account"])))
        mismatch_ids = {}
        for mismatch in db.execute(
            """SELECT d.* FROM bank_review_discrepancies d JOIN bank_result_reviews b USING(review_id)
               JOIN mock_bank_replies r USING(reply_id) JOIN payment_instructions i USING(instruction_id)
               JOIN approvals a USING(approval_id) WHERE a.period_ym=? ORDER BY r.rowid""", (month,),
        ):
            mismatch_ids.setdefault(mismatch["review_id"], []).append(mismatch["reply_id"])
        for review in db.execute(
            """SELECT b.*, h.previous_reply_id FROM bank_result_reviews b
               LEFT JOIN debit_evidence_changes h USING(review_id) JOIN payment_instructions i USING(instruction_id)
               JOIN approvals a USING(approval_id) WHERE a.period_ym=? ORDER BY review_id""", (month,),
        ):
            order = by_order[review["instruction_id"]]
            order["bank_reviews"].append(dict(review, mismatched_reply_ids=mismatch_ids.get(review["review_id"], [])))
            if review["decision"] is None:
                order["bank_conflict"] = True
                order["bank_review_kind"] = review["review_kind"]
                order["queue_state"] = "bank_check"
        if funding.enabled(db):
            for debit in db.execute(
                """SELECT d.instruction_id,d.amount,d.paid_on,d.reply_id,t.sequence_no
                   FROM cash_mock_debits d JOIN mock_bank_replies r USING(reply_id)
                   JOIN mock_payment_attempts t ON t.attempt_id=r.attempt_id
                   JOIN payment_instructions i ON i.instruction_id=d.instruction_id
                   JOIN approvals a USING(approval_id) WHERE a.period_ym=?""", (month,),
            ):
                by_order[debit["instruction_id"]]["debit"] = dict(debit)
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
