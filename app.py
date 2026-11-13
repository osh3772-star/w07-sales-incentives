import argparse
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Flask, flash, get_flashed_messages, redirect, render_template, request, session, url_for

import ai_drafts
import approvals as decisions
import payments as settlement
from ai_provider import ProviderError
from rules import month_overview
from store import available_months, open_db, receive_month, sync_sources
from workflow_security import USERS, require_role


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT / "01_참가자_배포용" / "04_원본데이터" / "01_corevia"
SCREENS = {"/": "index", "/cases": "cases", "/approvals": "approvals", "/payments": "payments"}
TEMPLATES = {"index": "intake.html", "cases": "cases.html", "approvals": "approvals.html",
             "payments": "payments.html"}
AI_KINDS = {"cases": ("summary", "question"), "approvals": ("proposal",)}


def create_app(database=None, source_root=SOURCE_ROOT):
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    db_path = Path(database or os.environ.get("W07_DATABASE") or ROOT / "instance" / "w07.sqlite3")
    with closing(open_db(db_path)) as db:
        sync_sources(db, source_root)

    def actor():
        return USERS.get(session.get("user_id"), USERS["staff"])

    def form(name):
        return request.form.get(name, "").strip()

    def page(screen, month, group=None, error=None, status=200, draft_input=None):
        with closing(open_db(db_path)) as db:
            db.execute("BEGIN")
            months = available_months(db)
            if month and month not in months:
                error = error or "원본에 없는 정산월입니다. 목록에서 월을 선택하세요."
                status = 400 if status == 200 else status
            if month not in months:
                month = months[-1] if months else None
            overview = month_overview(db, month) if month else None
            groups = overview["groups"] if overview else []
            if group and group not in {item["group_id"] for item in groups}:
                error = error or "이 정산월에서 찾을 수 없는 그룹입니다."
                status = 400 if status == 200 else status
                group = None
            if not group and groups and screen in ("cases", "approvals"):
                group = next((item["group_id"] for item in groups if len(item["members"]) > 1),
                             groups[0]["group_id"])
            details = {}
            if screen in ("cases", "approvals"):
                details["cases"] = decisions.list_cases(db, month)
                details["ai_drafts"] = ai_drafts.list_drafts(db, month, group) if group else []
            if screen == "approvals":
                details["approvals"] = decisions.list_approvals(db, month)
            if screen == "payments":
                details.update(settlement.list_payments(db, month))
        return render_template(
            TEMPLATES[screen], month=month, months=months, group=group, selected_group=group,
            overview=overview, actor=actor(), users=USERS.values(), csrf_token=session["csrf_token"],
            feedback=" ".join(get_flashed_messages()), error=error, draft_input=draft_input, **details,
        ), status

    def current_screen():
        if request.path == "/identity":
            return SCREENS.get(form("next"), "index")
        return SCREENS.get("/" + request.path.strip("/").split("/")[0], "index")

    @app.before_request
    def protect_forms():
        if request.endpoint == "static":
            return
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        if request.method == "POST":
            if not secrets.compare_digest(session["csrf_token"].encode(), form("csrf_token").encode()):
                return page(current_screen(), form("month"), form("group"),
                            "화면을 새로 열고 다시 시도하세요.", 400)

    def success(message, screen=None, anchor=None):
        flash(message)
        return redirect(url_for(screen or current_screen(), month=form("month") or None,
                                group=form("group") or None, _anchor=anchor), code=303)

    @app.errorhandler(PermissionError)
    def forbidden(error):
        return page(current_screen(), form("month"), form("group"), str(error), 403)

    @app.errorhandler(ValueError)
    def invalid(error):
        return page(current_screen(), form("month"), form("group"), str(error), 400)

    @app.get("/")
    def index():
        return page("index", request.args.get("month"))

    @app.get("/cases")
    def cases():
        return page("cases", request.args.get("month"), request.args.get("group"))

    @app.get("/approvals")
    def approvals():
        return page("approvals", request.args.get("month"), request.args.get("group"))

    @app.get("/payments")
    def payments():
        return page("payments", request.args.get("month"), request.args.get("group"))

    @app.post("/identity")
    def identity():
        selected = USERS.get(form("user_id"))
        if selected is None:
            raise ValueError("목록에서 시연 사용자를 선택하세요.")
        session["user_id"] = selected.user_id
        session["csrf_token"] = secrets.token_urlsafe(32)
        return success(f"{selected.label} 사용자로 전환했습니다.")

    @app.post("/intake")
    def intake():
        require_role(actor(), "staff", "lead")
        try:
            with closing(open_db(db_path)) as db:
                sync_sources(db, source_root)
                result = receive_month(db, form("month"))
        except (OSError, sqlite3.DatabaseError) as exc:
            app.logger.warning("접수 저장 오류: %s", exc)
            return page("index", form("month"), error="원본 또는 DB를 읽을 수 없습니다.", status=503)
        return success(f"{form('month')} 접수 결과 · 신규 {result['added']}건 · 기존 {result['existing']}건"
                       f" · 원본 오류 확인 {result['issues']}건")

    @app.post("/cases/request")
    def request_case():
        with closing(open_db(db_path)) as db:
            number = decisions.request_case(db, actor(), form("rebate_id"), form("question"),
                                            form("required") in ("1", "on"), form("due_date"))
        return success(f"소명 요청 #{number}을 기록했습니다.")

    @app.post("/cases/ai/generate")
    @app.post("/approvals/ai/generate")
    def generate_ai_draft():
        screen = current_screen()
        kind = form("kind")
        if kind not in AI_KINDS[screen]:
            raise ValueError("현재 화면에서 생성할 수 있는 초안 유형을 선택하세요.")
        previous = form("previous_id")
        if previous and not previous.isdecimal():
            raise ValueError("재상신할 결재 버전을 확인하세요.")
        try:
            with closing(open_db(db_path)) as db:
                number = ai_drafts.generate_draft(db, actor(), form("month"), form("group"), kind,
                                                  form("rebate_id") or None,
                                                  previous_id=int(previous) if previous else None)
        except ProviderError as exc:
            return page(screen, form("month"), form("group"), str(exc), 503)
        return success(f"AI 초안 #{number}을 저장했습니다. 원본 근거와 대조한 뒤 채택하세요.",
                       anchor="ai-drafts")

    @app.post("/cases/ai/<int:draft_id>/adopt")
    @app.post("/approvals/ai/<int:draft_id>/adopt")
    def adopt_ai_draft(draft_id):
        screen = current_screen()
        edited = form("edited_text")
        try:
            with closing(open_db(db_path)) as db:
                record = ai_drafts.adopt_draft(db, actor(), draft_id, edited, month=form("month"),
                                               group=form("group"), allowed_kinds=AI_KINDS[screen],
                                               required=form("required") in ("1", "on"), due_date=form("due_date"))
        except ValueError as exc:
            return page(screen, form("month"), form("group"), str(exc), 400,
                        draft_input={"draft_id": draft_id, "text": edited,
                                     "required": form("required") in ("1", "on"), "due_date": form("due_date")})
        if record["case_id"]:
            message = f"검토한 질문을 소명 요청 #{record['case_id']}에 저장했습니다."
        elif record["approval_id"]:
            message = f"검토한 결재안 #{record['approval_id']}을 상신했습니다. 상태는 승인 대기입니다."
        else:
            message = "검토한 요약을 그룹의 월별 근거 기록에 저장했습니다."
        return success(message, anchor="ai-drafts")

    @app.post("/cases/<int:case_id>/reply")
    def reply_case(case_id):
        with closing(open_db(db_path)) as db:
            decisions.reply_case(db, actor(), case_id, form("respondent"), form("answer"), form("evidence"))
        return success("소명 답변을 기록했습니다.")

    @app.post("/cases/<int:case_id>/resolve")
    def resolve_case(case_id):
        with closing(open_db(db_path)) as db:
            decisions.resolve_case(db, actor(), case_id, form("reason"))
        return success("소명 검토 결과를 기록했습니다.")

    @app.post("/approvals/submit")
    def submit_approval():
        previous = form("previous_id")
        if previous and not previous.isdecimal():
            raise ValueError("재상신할 결재 버전을 확인하세요.")
        with closing(open_db(db_path)) as db:
            number = decisions.submit_approval(db, actor(), form("month"), form("group"), form("note"),
                                               int(previous) if previous else None)
        return success(f"결재 #{number}을 상신했습니다.")

    @app.post("/approvals/<int:approval_id>/decide")
    def decide_approval(approval_id):
        with closing(open_db(db_path)) as db:
            decisions.decide_approval(db, actor(), approval_id, form("action"), form("reason"))
        return success(f"결재 #{approval_id}의 결정 기록을 확인하세요.")

    @app.post("/payments/<int:instruction_id>/account")
    def confirm_account(instruction_id):
        with closing(open_db(db_path)) as db:
            settlement.confirm_account(db, actor(), instruction_id, form("account"), form("evidence"))
        return success(f"지급지시 #{instruction_id}의 수취계좌와 확인 근거를 기록했습니다.")

    @app.post("/payments/<int:instruction_id>/send")
    def send_mock(instruction_id):
        with closing(open_db(db_path)) as db:
            attempt = settlement.send_mock(db, actor(), instruction_id, form("expected_attempt") or None)
        return success(f"모의 지급 시도 {attempt}의 처리 상태를 확인하세요.")

    @app.post("/payments/reply")
    def bank_reply():
        with closing(open_db(db_path)) as db:
            result = settlement.record_reply(db, actor(), form("reply_id"), form("attempt_id"),
                                             form("amount"), form("account"), form("result"))
        outcomes = {"matched": "대사 완료", "failed": "지급 실패", "review": "대사 확인 중",
                    "unmatched": "미연결 회신", "late": "사후 회신"}
        return success(f"모의 은행 회신을 기록했습니다. {outcomes.get(result, result)}")

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="W07 판매장려금")
    parser.add_argument("--port", type=int, default=5070)
    args = parser.parse_args()
    create_app().run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
