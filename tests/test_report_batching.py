"""Plan A: batched fact reports + threaded AI summaries (no Docker-free shortcuts)."""

import threading
import time
from uuid import UUID

from sqlalchemy import func, select

from portfolio_backend.models import Job, Outbox, Report
from portfolio_backend.providers.ai import AIAnswer
from portfolio_backend.services import jobs
from portfolio_backend.worker import execute


def queue_report(db, user_ids, kind="daily", period="2026-10-24"):
    with db.control() as s:
        for user_id in user_ids:
            s.add(
                Outbox(
                    user_id=user_id,
                    kind=kind,
                    dedupe_key=f"report:{user_id}:{kind}:{period}",
                    payload={"period": period},
                )
            )
    jobs.materialize_outbox(db)


def facts_jobs(db):
    with db.control() as s:
        return s.scalars(
            select(Job).where(Job.kind == "report_facts").order_by(Job.created_at, Job.id)
        ).all()


def ai_jobs(db):
    with db.control() as s:
        return s.scalars(
            select(Job).where(Job.kind == "report_ai").order_by(Job.created_at, Job.id)
        ).all()


class Gateway:
    def __init__(self, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def answer(self, payload):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                from portfolio_backend.errors import DomainError

                raise DomainError("ai_provider_failed", "unavailable", 503)
            return AIAnswer(text="持仓集中于单一 ETF。", model="test-model")
        finally:
            with self._lock:
                self.active -= 1


def grant_scheduled_consent(db, cfg, user_ids):
    from portfolio_backend.models import Consent

    with db.control() as s:
        for user_id in user_ids:
            s.add(
                Consent(
                    user_id=user_id,
                    purpose="scheduled_ai",
                    provider=cfg.ai_provider_id,
                    disclosure_version=cfg.ai_disclosure_version,
                    granted=True,
                )
            )


def test_report_outbox_rows_are_batched_into_facts_jobs(db, user):
    users = [user() for _ in range(5)]
    with db.control() as s:
        for u in users:
            s.add(
                Outbox(
                    user_id=u["id"],
                    kind="daily",
                    dedupe_key=f"report:{u['id']}:daily:2026-10-24",
                    payload={"period": "2026-10-24"},
                )
            )
    assert jobs.materialize_outbox(db, report_batch_size=2) == 5
    batches = facts_jobs(db)
    assert len(batches) == 3
    sizes = [len(job.payload["users"]) for job in batches]
    assert sizes == [2, 2, 1]
    assert all(batch.kind == "report_facts" for batch in batches)
    with db.control() as s:
        assert (
            s.scalar(select(func.count()).select_from(Outbox).where(Outbox.dispatched_at.is_(None)))
            == 0
        )
        assert s.scalar(select(func.count()).select_from(Job)) == 3


def test_repeated_materialize_does_not_duplicate_batches(db, user):
    u = user()
    queue_report(db, [u["id"]])
    jobs.materialize_outbox(db)
    jobs.materialize_outbox(db)
    with db.control() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_facts_batch_creates_reports_and_skips_ai_when_disabled(db, cfg, user, instrument, opening):
    disabled = cfg.model_copy(update={"report_ai_enabled": False})
    users = [user(), user()]
    iid = instrument()
    for u in users:
        opening(u, iid)
    queue_report(db, [u["id"] for u in users])
    batch = facts_jobs(db)[0]
    assert execute(db, batch.id, disabled) == "succeeded"
    with db.control() as s:
        reports = s.scalars(select(Report)).all()
        assert len(reports) == 2
        assert all(report.summary and "尚无可核实" in report.summary for report in reports)
    assert ai_jobs(db) == []


def test_facts_and_ai_phases_create_ai_summary(db, cfg, user, instrument, opening):
    u = user()
    opening(u, instrument())
    grant_scheduled_consent(db, cfg, [u["id"]])
    queue_report(db, [u["id"]])
    facts = facts_jobs(db)[0]
    assert execute(db, facts.id, cfg) == "succeeded"
    with db.control() as s:
        report = s.scalar(select(Report))
        assert "analysis" not in (report.snapshot or {})
    ai_batch = ai_jobs(db)[0]
    gateway = Gateway()
    assert execute(db, ai_batch.id, cfg, gateway) == "succeeded"
    assert gateway.calls == 1
    with db.control() as s:
        report = s.scalar(select(Report))
        assert report.snapshot["analysis"]["kind"] == "ai"
        assert report.snapshot["analysis"]["status"] == "generated"
        assert report.summary == "持仓集中于单一 ETF。"


def test_ai_batch_uses_threads_and_degrades_per_user(db, cfg, user, instrument, opening):
    users = [user() for _ in range(4)]
    iid = instrument()
    for u in users:
        opening(u, iid)
    grant_scheduled_consent(db, cfg, [u["id"] for u in users])
    queue_report(db, [u["id"] for u in users])
    facts = facts_jobs(db)[0]
    assert execute(db, facts.id, cfg) == "succeeded"
    ai_batch = ai_jobs(db)[0]
    assert len(ai_batch.payload["users"]) == 4
    gateway = Gateway(delay=0.2)
    started = time.monotonic()
    assert execute(db, ai_batch.id, cfg, gateway) == "succeeded"
    elapsed = time.monotonic() - started
    # Four summaries at the default concurrency=4 overlap instead of serialising.
    assert gateway.max_active > 1
    assert elapsed < 0.75


def test_ai_batch_failure_keeps_fact_baseline(db, cfg, user, instrument, opening):
    users = [user() for _ in range(2)]
    iid = instrument()
    for u in users:
        opening(u, iid)
    grant_scheduled_consent(db, cfg, [u["id"] for u in users])
    queue_report(db, [u["id"] for u in users])
    facts = facts_jobs(db)[0]
    assert execute(db, facts.id, cfg) == "succeeded"
    ai_batch = ai_jobs(db)[0]
    gateway = Gateway(fail=True)
    assert execute(db, ai_batch.id, cfg, gateway) == "succeeded"
    with db.control() as s:
        reports = s.scalars(select(Report)).all()
        assert len(reports) == 2
        for report in reports:
            assert report.snapshot["analysis"] == {
                "kind": "facts",
                "status": "ai_provider_failed",
            }
            assert "尚无可核实" in report.summary


def test_sampling_ratio_bounds_ai_candidates(db, cfg, user, instrument, opening):
    # Ratio 0 keeps every report factual; ratio 1 emits an AI batch.
    off = cfg.model_copy(update={"report_ai_sample_ratio": 0.0})
    u = user()
    opening(u, instrument())
    queue_report(db, [u["id"]])
    facts = facts_jobs(db)[0]
    assert execute(db, facts.id, off) == "succeeded"
    assert ai_jobs(db) == []


def test_ai_batch_payload_users_are_real_uuids(db, cfg, user, instrument, opening):
    u = user()
    opening(u, instrument())
    queue_report(db, [u["id"]])
    facts = facts_jobs(db)[0]
    execute(db, facts.id, cfg)
    [UUID(value) for value in ai_jobs(db)[0].payload["users"]]
