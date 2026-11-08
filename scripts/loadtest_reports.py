"""Report-pipeline load test: seed users, run a scheduled session, drain jobs.

Runs the real scheduler/materialiser/worker code in-process against the database
selected by the standard PA_* settings. AI calls are replaced by a stub that
sleeps ``--ai-delay`` seconds, so capacity can be measured without external
API cost; the stub still runs inside the real report_ai thread pool.

Usage:
    python scripts/loadtest_reports.py seed --users 100 --positions 5
    python scripts/loadtest_reports.py run --session cn --at 2026-11-09T16:30:00+08:00
    python scripts/loadtest_reports.py run --session us --ai-delay 5 --workers 2
"""

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.models import (
    Account,
    AgentToolCall,
    AIRun,
    Consent,
    Conversation,
    Instrument,
    Job,
    LatestQuote,
    Message,
    Outbox,
    Portfolio,
    Position,
    Preferences,
    Report,
    User,
)
from portfolio_backend.providers.ai import AIAnswer
from portfolio_backend.scheduler import tick
from portfolio_backend.services import jobs
from portfolio_backend.worker import execute

CN = ZoneInfo("Asia/Shanghai")
US = ZoneInfo("America/New_York")
SESSIONS = {
    "cn": ("daily_cn", CN, 16, 30),
    "us": ("daily_us", US, 16, 30),
    "weekly": ("weekly", CN, 9, 0),
}


class StubGateway:
    """Deterministic stand-in for the model: network wait becomes a sleep."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def answer(self, payload):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return AIAnswer(text="负载测试摘要：组合以核心持仓为主。", model="loadtest-stub")
        finally:
            with self.lock:
                self.active -= 1


def seed(db, cfg, users, positions, clear=False):
    with db.control() as s:
        if clear:
            for table in (
                Message,
                Conversation,
                AgentToolCall,
                AIRun,
                Job,
                Report,
                Outbox,
                Position,
                Account,
                Portfolio,
                Preferences,
                Consent,
                User,
                LatestQuote,
                Instrument,
            ):
                s.execute(delete(table))
        instruments = []
        for index in range(positions):
            instrument = Instrument(
                symbol=f"{600000 + index}",
                name=f"负载测试标的 {index + 1}",
                market="CN",
                asset_class="stock",
                currency="CNY",
                provider="fixture",
                quote_kind="realtime",
                enabled=True,
                commercial_display=True,
                freshness_seconds=3600,
            )
            s.add(instrument)
            s.flush()
            s.add(
                LatestQuote(
                    instrument_id=instrument.id,
                    price="10",
                    provider="fixture",
                    kind="realtime",
                    as_of=now(),
                    valid_until=now() + timedelta(hours=1),
                )
            )
            instruments.append(instrument.id)
        for index in range(users):
            user = User(
                apple_subject=f"loadtest-{index}",
                phone=f"139{index:08d}",
                store_environment="Sandbox",
            )
            s.add(user)
            s.flush()
            root = Portfolio(user_id=user.id)
            s.add(root)
            s.flush()
            account = Account(user_id=user.id, portfolio_id=root.id, name="负载测试账户")
            s.add(account)
            s.add(
                Preferences(
                    user_id=user.id,
                    daily_report=True,
                    weekly_report=True,
                    timezone="Asia/Shanghai",
                )
            )
            s.add(
                Consent(
                    user_id=user.id,
                    purpose="scheduled_ai",
                    provider=cfg.ai_provider_id,
                    disclosure_version=cfg.ai_disclosure_version,
                    granted=True,
                )
            )
            s.flush()
            for instrument_id in instruments:
                s.add(
                    Position(
                        user_id=user.id,
                        account_id=account.id,
                        instrument_id=instrument_id,
                        quantity=100,
                        cost_pool=1000,
                        realized_pnl=0,
                        version=1,
                        last_occurred_at=now(),
                        history_complete=False,
                    )
                )
        s.flush()
        return users, positions


def run_session(db, cfg, kind, at, workers, ai_delay, report_batch_size, tz):
    gateway = StubGateway(ai_delay)
    period = at.astimezone(tz).date().isoformat()
    started = time.monotonic()
    scheduled = tick(db, at)
    schedule_seconds = round(time.monotonic() - started, 3)
    with db.control() as s:
        pending = s.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(
                Outbox.kind == kind,
                Outbox.dispatched_at.is_(None),
                Outbox.payload["period"].as_string() == period,
            )
        )
    executed = {"report_facts": 0, "report_ai": 0}
    first_report_at = None
    drained = False
    timed_out = False
    hard_deadline = time.monotonic() + 1800
    while not drained:
        jobs.materialize_outbox(db, limit=1000, report_batch_size=report_batch_size)
        with db.control() as s:
            rows = s.scalars(
                select(jobs.Job)
                .where(jobs.Job.status == "queued", jobs.Job.due_at <= now())
                .order_by(jobs.Job.due_at)
                .limit(200)
            ).all()
            ids = [(row.id, row.kind) for row in rows]
        if not ids:
            with db.control() as s:
                remaining = s.scalar(
                    select(func.count()).select_from(jobs.Job).where(jobs.Job.status == "queued")
                )
                outbox_left = s.scalar(
                    select(func.count()).select_from(Outbox).where(Outbox.dispatched_at.is_(None))
                )
            if remaining == 0 and outbox_left == 0:
                drained = True
            elif time.monotonic() > hard_deadline:
                timed_out = True
                break
            else:
                time.sleep(0.2)
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda item: _run_one(db, cfg, item[0], gateway), ids))
        for _, executed_kind in ids:
            if executed_kind in executed:
                executed[executed_kind] += 1
        if first_report_at is None:
            with db.control() as s:
                if s.scalar(select(func.count()).select_from(Report)):
                    first_report_at = time.monotonic() - started
    total_seconds = time.monotonic() - started
    with db.control() as s:
        reports = s.scalar(select(func.count()).select_from(Report).where(Report.kind == kind))
    return {
        "session": kind,
        "period": period,
        "outbox_pending_at_start": pending,
        "schedule_seconds": schedule_seconds,
        "scheduled_outbox_rows": scheduled,
        "facts_jobs": executed["report_facts"],
        "ai_jobs": executed["report_ai"],
        "ai_calls": gateway.calls,
        "ai_max_concurrency": gateway.max_active,
        "reports_created": reports,
        "first_report_seconds": round(first_report_at, 2) if first_report_at else None,
        "total_seconds": round(total_seconds, 2),
        "workers": workers,
        "ai_delay": ai_delay,
        "timed_out": timed_out,
    }


def _run_one(db, cfg, job_id, gateway):
    try:
        return execute(db, job_id, cfg, gateway)
    except Exception as exc:  # noqa: BLE001 - load test reports failures explicitly
        return f"error:{type(exc).__name__}:{exc}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed")
    seed_cmd.add_argument("--users", type=int, default=100)
    seed_cmd.add_argument("--positions", type=int, default=5)
    seed_cmd.add_argument("--clear", action="store_true")
    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--session", choices=sorted(SESSIONS), default="cn")
    run_cmd.add_argument("--at", default=None, help="ISO timestamp; default session window now")
    run_cmd.add_argument("--workers", type=int, default=2)
    run_cmd.add_argument("--ai-delay", type=float, default=0.0)
    run_cmd.add_argument("--report-batch-size", type=int, default=500)
    args = parser.parse_args()

    cfg = settings()
    db = Database(cfg)
    try:
        if args.command == "seed":
            users, positions = seed(db, cfg, args.users, args.positions, clear=args.clear)
            print(json.dumps({"seeded_users": users, "positions": positions}, ensure_ascii=False))
            return
        kind, tz, hour, minute = SESSIONS[args.session]
        if args.at:
            at = datetime.fromisoformat(args.at)
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
        else:
            # Default to yesterday's window so every staggered due time is already
            # past and the drain loop does not wait on real staging delays.
            local = now().astimezone(tz) - timedelta(days=1)
            at = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz).replace(
                hour=hour, minute=minute
            ) + timedelta(minutes=5)
        result = run_session(
            db, cfg, kind, at, args.workers, args.ai_delay, args.report_batch_size, tz
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
