from sqlalchemy import func, select

from portfolio_backend.config import Settings
from portfolio_backend.errors import require
from portfolio_backend.models import (
    AIRun,
    Consent,
    Conversation,
    Message,
    Outbox,
    Report,
)
from portfolio_backend.services.common import idempotent, lock_portfolio, owned


def check_consent(session, user_id, config: Settings, purpose="interactive_ai"):
    row = session.scalar(
        select(Consent).where(Consent.user_id == user_id, Consent.purpose == purpose)
    )
    require(
        row
        and row.granted
        and row.provider == config.ai_provider_id
        and row.disclosure_version == config.ai_disclosure_version,
        "ai_consent_required",
        "请先确认当前 AI 服务的数据使用说明",
        403,
    )


def require_provider(config):
    require(
        (config.simulator_ai or config.ai_gateway_url or config.deepseek_api_key.get_secret_value())
        and config.ai_provider_id,
        "ai_unavailable",
        "AI 服务尚未配置",
        503,
    )


def enqueue_question(session, user_id, body, key, config, conversation_id=None, report_id=None):
    def action():
        lock_portfolio(session, user_id)
        require_provider(config)
        check_consent(session, user_id, config)
        report = owned(session, Report, report_id, user_id, lock=True) if report_id else None
        if report:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.user_id == user_id, Conversation.report_id == report.id
                )
            )
            if conversation is None:
                conversation = Conversation(
                    user_id=user_id, report_id=report.id, title=report.title[:100]
                )
                session.add(conversation)
                session.flush()
            # Every account has the same per-report free follow-up quota.
            require(
                report.free_used + report.free_reserved < 3,
                "quota_exhausted",
                "这份报告的免费追问已用完",
                403,
            )
        else:
            conversation = owned(session, Conversation, conversation_id, user_id, lock=True)
            # A report conversation must use the report route and its quota.
            require(
                conversation.report_id is None, "report_route_required", "请从报告发起追问", 422
            )
        active = session.scalar(
            select(func.count())
            .select_from(AIRun)
            .where(AIRun.user_id == user_id, AIRun.status.in_(["queued", "running"]))
        )
        require(active < 2, "too_many_runs", "请等待当前回答完成", 429)
        same_conversation = session.scalar(
            select(AIRun.id)
            .where(
                AIRun.user_id == user_id,
                AIRun.conversation_id == conversation.id,
                AIRun.status.in_(["queued", "running"]),
            )
            .limit(1)
        )
        require(same_conversation is None, "conversation_busy", "当前对话正在回答", 409)
        use_free = report is not None
        if use_free:
            report.free_reserved += 1
        run = AIRun(
            user_id=user_id,
            conversation_id=conversation.id,
            report_id=report.id if report else None,
            used_free_credit=use_free,
        )
        session.add(run)
        session.flush()
        session.add(
            Message(
                user_id=user_id, conversation_id=conversation.id, role="user", content=body.content
            )
        )
        session.add(
            Outbox(
                user_id=user_id,
                kind="answer",
                dedupe_key=f"answer:{run.id}",
                payload={"run_id": str(run.id)},
            )
        )
        return {
            "run_id": str(run.id),
            "conversation_id": str(conversation.id),
            "status": "queued",
            "remaining_free": (3 - report.free_used - report.free_reserved if use_free else None),
        }

    scope = f"question:{report_id or conversation_id}"
    return idempotent(session, user_id, scope, key, body.model_dump(mode="json"), action)


def settle_run(session, user_id, run_id, answer=None, error=None, usage=None):
    lock_portfolio(session, user_id)
    run = owned(session, AIRun, run_id, user_id, lock=True)
    if run.status in {"succeeded", "failed"}:
        return
    if run.used_free_credit:
        report = owned(session, Report, run.report_id, user_id, lock=True)
        report.free_reserved -= 1
        if error is None:
            report.free_used += 1
    run.status = "failed" if error else "succeeded"
    run.answer, run.error_code, run.usage = answer, error, usage or {}
    if answer:
        session.add(
            Message(
                user_id=user_id,
                conversation_id=run.conversation_id,
                role="assistant",
                content=answer,
                charts=(usage or {}).get("charts", []),
            )
        )
