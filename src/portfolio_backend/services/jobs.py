from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.db import now
from portfolio_backend.errors import require
from portfolio_backend.models import Job, Outbox, User

QUEUES = {
    "answer": "interactive_ai",
    # Legacy per-user report kinds still drain through the reports queue; new
    # work is dispatched as report_facts/report_ai batches (plan A).
    "daily": "reports",
    "weekly": "reports",
    # Two daily sessions: A/HK close (16:30 Asia/Shanghai) and US close (16:30 ET).
    "daily_cn": "reports",
    "daily_us": "reports",
    "delete_account": "privacy",
    # Plan A: deterministic facts batches and AI summary batches share a queue.
    "report_facts": "reports",
    "report_ai": "reports",
}

REPORT_KINDS = {"daily", "weekly", "daily_cn", "daily_us"}
# Batch jobs legitimately outlive the interactive 180s lease (AI summaries run
# with bounded in-task thread concurrency), so they get a longer lease.
REPORT_LEASE_KINDS = {"report_facts", "report_ai"}


def _due_at(payload):
    """Parse the staggered due time stored by the scheduler; None when absent."""
    value = (payload or {}).get("due_at")
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def lease_seconds(kind, config):
    if kind in REPORT_LEASE_KINDS:
        return config.report_job_lease_seconds
    return config.job_lease_seconds


def materialize_outbox(db, limit=100, report_batch_size=500):
    """Transactionally turn domain outbox rows into recoverable job records.

    Report rows (daily/weekly) are grouped into bounded ``report_facts`` batch
    jobs so a few tasks generate thousands of fact baselines; other kinds keep
    one job per outbox row. Every outbox row is still marked dispatched in the
    same transaction, and report batches dedupe on their first outbox id.
    """
    # Staggered sessions store a due time on the outbox row; it must pass
    # before the row is materialised so later user-id batches start later.
    due_now = now().astimezone(UTC).isoformat()
    with db.control() as s:
        events = s.scalars(
            select(Outbox)
            .where(
                Outbox.dispatched_at.is_(None),
                or_(
                    Outbox.payload["due_at"].as_string().is_(None),
                    Outbox.payload["due_at"].as_string() <= due_now,
                ),
            )
            .order_by(Outbox.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        groups: dict[tuple[str, str], list[Outbox]] = {}
        for event in events:
            if event.kind in REPORT_KINDS:
                period = str((event.payload or {}).get("period") or "")
                groups.setdefault((event.kind, period), []).append(event)
            elif event.kind in QUEUES:
                s.execute(
                    insert(Job)
                    .values(
                        user_id=event.user_id,
                        kind=event.kind,
                        dedupe_key=event.dedupe_key,
                        payload=event.payload,
                    )
                    .on_conflict_do_nothing(index_elements=["dedupe_key"])
                )
            # Portfolio-change events invalidate nothing: snapshots read the authoritative DB.
            event.dispatched_at = now()
        for (kind, period), group in groups.items():
            for index in range(0, len(group), report_batch_size):
                chunk = group[index : index + report_batch_size]
                due_at = _due_at(chunk[0].payload) or now()
                s.execute(
                    insert(Job)
                    .values(
                        user_id=chunk[0].user_id,
                        kind="report_facts",
                        dedupe_key=f"report_facts:{kind}:{period}:{chunk[0].id}",
                        due_at=due_at,
                        payload={
                            "kind": kind,
                            "period": period,
                            "users": [str(event.user_id) for event in chunk],
                        },
                    )
                    .on_conflict_do_nothing(index_elements=["dedupe_key"])
                )
        return len(events)


def dispatch(db, send, limit=100, report_batch_size=500):
    materialize_outbox(db, limit, report_batch_size)
    with db.control() as s:
        rows = s.scalars(
            select(Job)
            .where(
                Job.status == "queued",
                Job.due_at <= now(),
                or_(
                    Job.last_dispatched_at.is_(None),
                    Job.last_dispatched_at < now() - timedelta(seconds=60),
                ),
            )
            .order_by(Job.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            # Broker acceptance before marking; a crash duplicates a message, not the side effect.
            send(str(row.id), QUEUES[row.kind], row.kind)
            row.last_dispatched_at = now()
        return len(rows)


def claim(db, job_id: UUID, config):
    with db.control() as s:
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status != "queued" or job.due_at > now():
            return None
        user = s.get(User, job.user_id)
        if user.status != "active" and job.kind != "delete_account":
            job.status = "cancelled"
            return None
        job.status, job.fence = "running", uuid4()
        job.attempt += 1
        job.lease_until = now() + timedelta(seconds=lease_seconds(job.kind, config))
        return {
            "id": job.id,
            "user_id": job.user_id,
            "kind": job.kind,
            "payload": job.payload,
            "fence": job.fence,
            "attempt": job.attempt,
        }


def locked_job(session, claimed):
    job = session.scalar(select(Job).where(Job.id == claimed["id"]).with_for_update())
    require(
        job
        and job.status == "running"
        and job.fence == claimed["fence"]
        and job.lease_until > now(),
        "stale_worker",
        "任务租约已失效",
    )
    return job


def recover(db, config, terminal_callback):
    with db.control() as s:
        candidates = s.execute(
            select(Job.id, Job.user_id)
            .where(Job.status == "running", Job.lease_until < now())
            .limit(100)
        ).all()
    recovered = 0
    for job_id, user_id in candidates:
        with db.control() as s:
            # Completion and deletion use the same order: user -> job -> private records.
            user = s.scalar(select(User).where(User.id == user_id).with_for_update())
            job = s.scalar(select(Job).where(Job.id == job_id).with_for_update(skip_locked=True))
            if not job or job.status != "running" or job.lease_until >= now():
                continue
            if user.status == "deleted":
                job.status, job.fence = "cancelled", None
                continue
            if job.attempt >= config.max_job_attempts:
                terminal_callback(s, job, "worker_lost")
                job.status, job.error_code = "failed", "worker_lost"
            else:
                job.status = "queued"
                job.due_at = now() + timedelta(seconds=min(60, 2**job.attempt))
                job.last_dispatched_at = None
            job.fence = None
            recovered += 1
    return recovered
