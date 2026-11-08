import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, timedelta
from uuid import UUID

from celery import Celery
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.errors import DomainError, require
from portfolio_backend.models import (
    TENANT_TABLES,
    AIRun,
    AuthSession,
    Base,
    DeletionRequest,
    Job,
    Message,
    Report,
    User,
)
from portfolio_backend.providers.ai import AIGateway
from portfolio_backend.services import ai, jobs, portfolio
from portfolio_backend.services.common import owned

config = settings()
celery = Celery("portfolio_backend", broker=config.broker_url)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=110,
    task_time_limit=120,
    # Report batches run with their own wall-clock budget (up to ~10 min) and
    # bounded in-task thread concurrency; the visibility timeout must exceed
    # that so a still-running batch is never redelivered.
    broker_transport_options={"visibility_timeout": 1200},
    task_ignore_result=True,
)
logger = logging.getLogger(__name__)

REPORT_TITLES = {
    "daily": "收盘总结",
    "daily_cn": "收盘总结 · A/港股",
    "daily_us": "收盘总结 · 美股",
    "weekly": "每周复盘",
}
REPORT_JOB_KINDS = {"daily", "weekly", "daily_cn", "daily_us"}


def financial_context(data, cfg):
    """One allowlist for interactive and scheduled disclosure; no internal identifiers."""
    fields = {
        "as_of",
        "complete",
        "coverage",
        "market_value_cny",
        "priced_market_value_cny",
        "unrealized_pnl_cny",
        "realized_pnl_by_currency",
        "daily_pnl_cny",
        "history_complete",
    }
    position_fields = {
        "name",
        "symbol",
        "market",
        "currency",
        "quantity",
        "average_cost",
        "opening_cost_estimated",
        "market_value",
        "market_value_cny",
        "unrealized_pnl",
        "price",
        "as_of",
        "quote_status",
        "quote_kind",
        "nav_change",
        "nav_change_pct",
        "nav_previous_as_of",
        "weight_pct",
        "history_complete",
    }
    context = {k: v for k, v in data.items() if k in fields}
    context["positions"] = [
        {k: v for k, v in p.items() if k in position_fields} for p in data["positions"]
    ]
    sources = data.get("market_data_providers")
    if sources is None or not set(sources).issubset(cfg.ai_market_data_providers):
        # Display rights do not imply third-party AI processing/derived-data rights.
        context = {
            k: v for k, v in context.items() if k in {"as_of", "history_complete", "positions"}
        }
        own_fields = {
            "name",
            "symbol",
            "market",
            "currency",
            "quantity",
            "average_cost",
            "opening_cost_estimated",
            "history_complete",
        }
        context["positions"] = [
            {k: v for k, v in p.items() if k in own_fields} for p in context["positions"]
        ]
        context["market_data_withheld"] = True
    return context


def before_transmission(db, claim, cfg, purpose):
    with db.control() as s:
        jobs.locked_job(s, claim)
        require(
            s.get(User, claim["user_id"]).status == "active",
            "account_unavailable",
            "账户不可用",
            403,
        )
        ai.check_consent(s, claim["user_id"], cfg, purpose)


def terminal_failure(session, job, code):
    if job.kind == "answer":
        ai.settle_run(session, job.user_id, UUID(job.payload["run_id"]), error=code)


def prepare_answer(db, claim, cfg):
    uid, run_id = claim["user_id"], UUID(claim["payload"]["run_id"])
    with db.tenant(uid) as s:
        ai.require_provider(cfg)
        ai.check_consent(s, uid, cfg)
        run = owned(s, AIRun, run_id, uid)
        report = owned(s, Report, run.report_id, uid) if run.report_id else None
        data = report.snapshot if report else portfolio.snapshot(s, uid)
        history = s.scalars(
            select(Message)
            .where(Message.user_id == uid, Message.conversation_id == run.conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(12)
        ).all()
        return {
            "request_id": str(run_id),
            "purpose": "interactive_ai",
            "provider": cfg.ai_provider_id,
            "context": financial_context(data, cfg),
            "messages": [{"role": m.role, "content": m.content} for m in reversed(history)],
            "instructions": "解释给定持仓与复盘事实。缺失数据须明确说明，不虚构行情、收益或来源。不提供买卖、择时、仓位配置等投资决策或个性化推荐。",
        }


def build_report(db, claim, cfg=None):
    period = claim["payload"].get("period")
    report_date = None
    if period:
        try:
            report_date = date.fromisoformat(str(period)[:10])
        except ValueError:
            report_date = None
    with db.tenant(claim["user_id"]) as session:
        data = portfolio.snapshot(
            session,
            claim["user_id"],
            report_date=report_date,
            close_mark_max_age_days=(
                cfg.close_mark_max_age_days
                if cfg is not None
                else portfolio.CLOSE_MARK_MAX_AGE_DAYS
            ),
        )
    # v0 facts baseline: never label this deterministic summary as an AI-generated analysis.
    status = "估值完整" if data["complete"] else "部分行情或汇率缺失"
    title = REPORT_TITLES.get(claim["kind"], "收盘总结")
    summary = f"{status}，已覆盖 {data['coverage']['priced']}/{data['coverage']['total']} 项持仓。"
    summary += "本报告记录生成时的组合快照；尚无可核实的期间收益，不展示推算的涨跌。"
    return {"snapshot": data, "title": title, "summary": summary}


def enrich_report(db, claim, cfg, report, gateway):
    # A report still delivers its factual baseline when optional AI is off/unavailable.
    report["snapshot"]["analysis"] = {"kind": "facts", "status": "not_authorized"}
    try:
        ai.require_provider(cfg)
        before_transmission(db, claim, cfg, "scheduled_ai")
        request = {
            "request_id": str(claim["id"]),
            "purpose": "scheduled_ai",
            "provider": cfg.ai_provider_id,
            "context": financial_context(report["snapshot"], cfg),
            "messages": [{"role": "user", "content": "生成一段不超过 500 字的组合复盘摘要。"}],
            "instructions": "解释组合快照中的配置与集中度。仅有生成时快照，没有期间收益、新闻或历史价格。"
            "不得推算日/周涨跌，不得声称所有市场已收盘。行情不完整须放在首句。"
            "不提供买卖、择时、仓位配置等投资决策或个性化推荐。",
        }
        if gateway is None and (cfg.simulator_ai or cfg.deepseek_api_key.get_secret_value()):
            from portfolio_backend.agent.runtime import answer as react_answer

            answer = react_answer(
                db, claim, cfg, request, lambda: before_transmission(db, claim, cfg, "scheduled_ai")
            )
        else:
            answer = (gateway or AIGateway(cfg)).answer(request)
        if answer.degraded_reason:
            report["snapshot"]["analysis"]["status"] = answer.degraded_reason
            return None
        report["snapshot"]["analysis"] = {
            "kind": "ai",
            "status": "generated",
            "provider": cfg.ai_provider_id,
            "disclosure_version": cfg.ai_disclosure_version,
            **answer.model_dump(exclude={"text"}),
        }
        return answer.text
    except DomainError as exc:
        if exc.code in {"account_unavailable", "stale_worker"}:
            raise
        report["snapshot"]["analysis"]["status"] = exc.code
        return None


def _ai_eligible(db, cfg, user_id):
    """Deterministic per-user sampling; keeps scheduled AI cost bounded at scale."""
    if not cfg.report_ai_enabled or cfg.report_ai_sample_ratio <= 0:
        return False
    if cfg.report_ai_sample_ratio < 1.0:
        digest = hashlib.sha256(str(user_id).encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") / 2**32
        if bucket >= cfg.report_ai_sample_ratio:
            return False
    return True


def report_facts(db, claim, cfg):
    """Plan A phase 1: deterministic fact baselines for a bounded user batch.

    One job builds up to ``report_batch_size`` fact reports with no LLM call, so
    the 18:00 sweep needs a few tasks instead of one per user. Missing reports
    are created idempotently; users eligible for an AI summary are queued into
    ``report_ai`` batch jobs (phase 2).
    """
    payload = claim["payload"]
    kind, period = payload["kind"], payload["period"]
    user_ids = [UUID(value) for value in payload.get("users", [])]
    candidates = []
    created = 0
    for user_id in user_ids:
        with db.control() as s:
            user = s.get(User, user_id)
            if user is None or user.status != "active":
                continue
            existing = s.scalar(
                select(Report).where(
                    Report.user_id == user_id,
                    Report.kind == kind,
                    Report.period == period,
                )
            )
            analysis_kind = (
                ((existing.snapshot or {}).get("analysis") or {}).get("kind")
                if existing is not None
                else None
            )
        if analysis_kind == "ai":
            continue
        if existing is None:
            data = build_report(
                db,
                {**claim, "user_id": user_id, "payload": {**payload, "period": period}},
                cfg,
            )
            with db.control() as s:
                user = s.get(User, user_id)
                if user is None or user.status != "active":
                    continue
                duplicate = s.scalar(
                    select(Report).where(
                        Report.user_id == user_id,
                        Report.kind == kind,
                        Report.period == period,
                    )
                )
                if duplicate is None:
                    s.add(Report(user_id=user_id, kind=kind, period=period, **data))
                    created += 1
        if _ai_eligible(db, cfg, user_id):
            candidates.append(user_id)
    queued = _enqueue_report_ai(db, claim, cfg, candidates)
    logger.info(
        "report_facts_done kind=%s period=%s users=%s created=%s ai_queued=%s",
        kind,
        period,
        len(user_ids),
        created,
        queued,
    )
    return created


def _enqueue_report_ai(db, claim, cfg, user_ids):
    """Queue AI summary batches; dedupe on the facts job id makes retries safe."""
    if not user_ids:
        return 0
    payload = claim["payload"]
    batch_size = cfg.report_ai_batch_size
    queued = 0
    with db.control() as s:
        for index in range(0, len(user_ids), batch_size):
            chunk = user_ids[index : index + batch_size]
            inserted = s.scalar(
                insert(Job)
                .values(
                    user_id=chunk[0],
                    kind="report_ai",
                    dedupe_key=f"report_ai:{claim['id']}:{index // batch_size}",
                    payload={
                        "kind": payload["kind"],
                        "period": payload["period"],
                        "users": [str(user_id) for user_id in chunk],
                    },
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(Job.id)
            )
            queued += int(inserted is not None)
    return queued


def _record_ai_result(db, claim, cfg, user_id, data, summary_text):
    with db.control() as s:
        jobs.locked_job(s, claim)
        user = s.get(User, user_id)
        if user is None or user.status != "active":
            return "inactive"
        report = s.scalar(
            select(Report)
            .where(
                Report.user_id == user_id,
                Report.kind == claim["payload"]["kind"],
                Report.period == claim["payload"]["period"],
            )
            .with_for_update()
        )
        if report is None:
            return "missing"
        if summary_text:
            try:
                ai.check_consent(s, user_id, cfg, "scheduled_ai")
                data["summary"] = summary_text
            except DomainError:
                data["snapshot"]["analysis"] = {"kind": "facts", "status": "consent_revoked"}
        report.snapshot = data["snapshot"]
        report.summary = data["summary"]
        return "done"


def _enrich_user(db, claim, cfg, gateway, user_id):
    """One AI summary for one user; safe to run inside a worker thread."""
    kind, period = claim["payload"]["kind"], claim["payload"]["period"]
    with db.tenant(user_id) as s:
        report = s.scalar(
            select(Report).where(
                Report.user_id == user_id, Report.kind == kind, Report.period == period
            )
        )
        if report is None:
            return "missing"
        if ((report.snapshot or {}).get("analysis") or {}).get("kind") == "ai":
            return "already"
        data = {
            "snapshot": report.snapshot,
            "title": report.title,
            "summary": report.summary,
        }
    # Report summaries keep the daily/weekly prompt; the batch job provides the fence.
    per_user = {
        **claim,
        "user_id": user_id,
        "kind": kind,
        "payload": {**claim["payload"], "period": period},
    }
    summary_text = enrich_report(db, per_user, cfg, data, gateway)
    return _record_ai_result(db, claim, cfg, user_id, data, summary_text)


def report_ai(db, claim, cfg, gateway=None):
    """Plan A phase 2: bounded in-task thread concurrency for AI summaries.

    AI calls are network-bound (GIL released), so a few threads per process give
    the throughput of several prefork children at a fraction of the memory.
    Per-user failures degrade to the fact baseline; only fence/account errors
    fail the batch for retry.
    """
    user_ids = [UUID(value) for value in claim["payload"].get("users", [])]
    if not user_ids:
        return 0
    workers = max(1, min(cfg.report_ai_concurrency, len(user_ids)))
    budget_deadline = time.monotonic() + cfg.report_ai_budget_seconds
    results = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report-ai") as pool:
        futures = {
            pool.submit(_enrich_user, db, claim, cfg, gateway, user_id): user_id
            for user_id in user_ids
        }
        try:
            for future in as_completed(
                futures, timeout=max(1.0, budget_deadline - time.monotonic())
            ):
                user_id = futures[future]
                try:
                    results[user_id] = future.result()
                except DomainError as exc:
                    # A stale fence invalidates the whole batch; anything else
                    # (account revoked mid-flight, provider failure) degrades to
                    # the fact baseline for that user only.
                    if exc.code == "stale_worker":
                        raise
                    results[user_id] = exc.code
                    logger.warning("report_ai_user_failed code=%s", exc.code)
                except Exception:
                    results[user_id] = "worker_error"
                    logger.warning("report_ai_user_failed code=worker_error")
        except FutureTimeout:
            logger.warning("report_ai_budget_exhausted users=%s", len(user_ids))
            for future in futures:
                future.cancel()
    logger.info(
        "report_ai_done kind=%s period=%s users=%s results=%s",
        claim["payload"]["kind"],
        claim["payload"]["period"],
        len(user_ids),
        results,
    )
    return len(results)


def erase_private_data(session, user_id, current_job_id):
    # All private records currently live in PostgreSQL; future external stores must add erasure steps.
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in TENANT_TABLES:
            session.execute(delete(table).where(table.c.user_id == user_id))
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    session.execute(
        update(Job)
        .where(Job.user_id == user_id, Job.id != current_job_id)
        .values(status="cancelled", payload={}, fence=None)
    )
    request = session.scalar(select(DeletionRequest).where(DeletionRequest.user_id == user_id))
    request.status, request.completed_at = "completed", now()
    user = session.get(User, user_id)
    user.apple_subject, user.status = f"deleted:{user_id}", "deleted"
    user.phone, user.password_hash = None, None


def execute(db, job_id, cfg, gateway=None):
    claimed = jobs.claim(db, job_id, cfg)
    if claimed is None:
        return "ignored"
    try:
        if claimed["kind"] == "report_facts":
            report_facts(db, claimed, cfg)
            with db.control() as s:
                job = jobs.locked_job(s, claimed)
                job.status, job.lease_until = "succeeded", None
            return "succeeded"
        if claimed["kind"] == "report_ai":
            report_ai(db, claimed, cfg, gateway)
            with db.control() as s:
                job = jobs.locked_job(s, claimed)
                job.status, job.lease_until = "succeeded", None
            return "succeeded"
        answer = report_data = report_summary = None
        if claimed["kind"] == "answer":
            payload = prepare_answer(db, claimed, cfg)
            # Check current account + consent again immediately before external transmission.
            before_transmission(db, claimed, cfg, "interactive_ai")
            if gateway is None and (cfg.simulator_ai or cfg.deepseek_api_key.get_secret_value()):
                from portfolio_backend.agent.runtime import answer as react_answer

                answer = react_answer(
                    db,
                    claimed,
                    cfg,
                    payload,
                    lambda: before_transmission(db, claimed, cfg, "interactive_ai"),
                )
            else:
                answer = (gateway or AIGateway(cfg)).answer(payload)
        elif claimed["kind"] in REPORT_JOB_KINDS:
            report_data = build_report(db, claimed, cfg)
            report_summary = enrich_report(db, claimed, cfg, report_data, gateway)
        elif claimed["kind"] != "delete_account":
            raise DomainError("unsupported_job", "未知任务")
        # Short transaction: fencing validation, side effects and success are atomic.
        with db.control() as s:
            user = s.scalar(select(User).where(User.id == claimed["user_id"]).with_for_update())
            job = jobs.locked_job(s, claimed)
            require(
                user.status == "active" or claimed["kind"] == "delete_account",
                "account_unavailable",
                "账户不可用",
                403,
            )
            if answer:
                ai.check_consent(s, user.id, cfg)
                ai.settle_run(
                    s,
                    user.id,
                    UUID(job.payload["run_id"]),
                    answer=answer.text,
                    error=answer.degraded_reason,
                    usage=answer.model_dump(exclude={"text"}),
                )
            elif report_data:
                if report_summary:
                    try:
                        ai.check_consent(s, user.id, cfg, "scheduled_ai")
                        report_data["summary"] = report_summary
                    except DomainError:
                        report_data["snapshot"]["analysis"] = {
                            "kind": "facts",
                            "status": "consent_revoked",
                        }
                existing = s.scalar(
                    select(Report).where(
                        Report.user_id == user.id,
                        Report.kind == job.kind,
                        Report.period == job.payload["period"],
                    )
                )
                if existing is None:
                    s.add(
                        Report(
                            user_id=user.id,
                            kind=job.kind,
                            period=job.payload["period"],
                            **report_data,
                        )
                    )
            else:
                erase_private_data(s, user.id, job.id)
            job.status, job.lease_until = "succeeded", None
        return "succeeded"
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else "worker_error"
        with db.control() as s:
            user = s.scalar(select(User).where(User.id == claimed["user_id"]).with_for_update())
            job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job or job.fence != claimed["fence"] or job.status != "running":
                return "stale"
            if user.status == "deleted":
                job.status = "cancelled"
                return "cancelled"
            retryable = code in {"ai_provider_failed", "worker_error"}
            if retryable and job.attempt < cfg.max_job_attempts:
                job.status, job.last_dispatched_at = "queued", None
                job.due_at = now() + timedelta(seconds=2**job.attempt)
            else:
                terminal_failure(s, job, code)
                job.status = "failed"
            job.error_code, job.fence = code, None
        # Log identifiers/codes only, never exception payloads containing financial inputs.
        logger.warning("job_failed job_id=%s code=%s", job_id, code)
        return "failed"


@celery.task(name="portfolio.execute")
def execute_task(job_id):
    db = Database(config)
    try:
        return execute(db, UUID(job_id), config)
    finally:
        db.close()


def _task_options(cfg, kind):
    # Report batches run with their own wall-clock budget; the interactive
    # defaults (soft 110s / hard 120s) would kill a bounded multi-user batch.
    if kind in jobs.REPORT_LEASE_KINDS:
        return {
            "soft_time_limit": cfg.report_ai_budget_seconds + 90,
            "time_limit": cfg.report_ai_budget_seconds + 120,
        }
    return {}


def dispatch_once(db, cfg):
    jobs.recover(db, cfg, terminal_failure)
    return jobs.dispatch(
        db,
        lambda job_id, queue, kind: celery.send_task(
            "portfolio.execute", args=[job_id], queue=queue, **_task_options(cfg, kind)
        ),
        limit=cfg.dispatch_batch_limit,
        report_batch_size=cfg.report_batch_size,
    )


def dispatch_main():
    db = Database(config)
    db.verify_roles()
    try:
        while True:
            try:
                dispatch_once(db, config)
            except Exception:
                logger.error("dispatcher_failed")
            time.sleep(2)
    finally:
        db.close()
