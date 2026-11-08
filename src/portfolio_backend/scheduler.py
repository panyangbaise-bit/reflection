"""Scheduled recaps: two daily sessions (CN/HK close, US close) and a Saturday weekly.

Users are always scheduled in ascending user-id order. Each batch of
``report_dispatch_batch_users`` ids is delayed by
``report_dispatch_batch_interval_seconds`` relative to the session window, so
larger user ids start later. The delay is stored on the outbox row and honoured
when the dispatcher materialises jobs.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.models import Outbox, Position, Preferences, User

CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Session:
    kind: str
    tz: ZoneInfo
    hour: int
    minute: int
    weekday: int | None = None  # None = every weekday; 5 = Saturday


# Market-close recaps: A/HK shares finish at 15:00/16:00 Asia/Shanghai, US at
# 16:00 America/New_York; the weekly recap starts Saturday 09:00 Asia/Shanghai.
SESSIONS = (
    Session("daily_cn", CN_TZ, 16, 30),
    Session("daily_us", US_TZ, 16, 30),
    Session("weekly", CN_TZ, 9, 0, weekday=5),
)


def due_session(session, at):
    """Window start and report period for this session, or None before it opens."""
    local = at.astimezone(session.tz)
    if session.weekday is not None and local.weekday() != session.weekday:
        return None
    window = datetime.combine(
        local.date(), clock_time(session.hour, session.minute), tzinfo=session.tz
    )
    if at < window:
        return None
    return window, local.date().isoformat()


def tick(db, at=None, batch_users=None, batch_interval=None):
    cfg = settings()
    at = at or now()
    batch_users = batch_users or cfg.report_dispatch_batch_users
    batch_interval = batch_interval or cfg.report_dispatch_batch_interval_seconds
    count = 0
    with db.control() as s:
        if not s.scalar(text("SELECT pg_try_advisory_xact_lock(741258963)")):
            return 0
        for session in SESSIONS:
            due = due_session(session, at)
            if due is None:
                continue
            window, period = due
            count += _schedule(s, session, window, period, at, batch_users, batch_interval)
    return count


def _schedule(s, session, window, period, at, batch_users, batch_interval):
    """Write outbox rows in ascending user-id order with staggered due times."""
    flag = Preferences.weekly_report if session.kind == "weekly" else Preferences.daily_report
    after = None
    index = 0
    inserted_count = 0
    while True:
        query = (
            select(Preferences.user_id)
            .join(User)
            .where(
                User.status == "active",
                flag,
                exists(
                    select(Position.id).where(Position.user_id == User.id, Position.quantity > 0)
                ),
            )
        )
        if after:
            query = query.where(Preferences.user_id > after)
        rows = s.scalars(query.order_by(Preferences.user_id).limit(500)).all()
        if not rows:
            break
        for user_id in rows:
            # Ascending id order: later batches (larger ids) start later.
            due_at = window + timedelta(seconds=(index // batch_users) * batch_interval)
            index += 1
            if due_at > at:
                # Ordered by id, so every remaining user is at least as late.
                return inserted_count
            key = f"report:{user_id}:{session.kind}:{period}"
            inserted = s.scalar(
                insert(Outbox)
                .values(
                    user_id=user_id,
                    kind=session.kind,
                    dedupe_key=key,
                    payload={"period": period, "due_at": due_at.astimezone(UTC).isoformat()},
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
                .returning(Outbox.id)
            )
            inserted_count += int(inserted is not None)
        after = rows[-1]
    return inserted_count


def main():
    cfg = settings()
    db = Database(cfg)
    db.verify_roles()
    try:
        next_purge = 0.0
        while True:
            tick(db)
            if time.monotonic() >= next_purge:
                from portfolio_backend.services.trace_retention import purge

                purge(db, cfg.agent_trace_retention_days)
                next_purge = time.monotonic() + 3600
            time.sleep(30)
    finally:
        db.close()
