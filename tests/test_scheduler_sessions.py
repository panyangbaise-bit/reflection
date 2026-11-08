"""Two daily report sessions (CN/HK close, US close), Saturday weekly, id-order stagger."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from portfolio_backend.models import Outbox, Preferences
from portfolio_backend.scheduler import tick

CN = ZoneInfo("Asia/Shanghai")


def enable(db, users, daily=False, weekly=False):
    with db.control() as s:
        for user in users:
            pref = s.get(Preferences, user["id"])
            pref.daily_report, pref.weekly_report = daily, weekly


REPORT_KINDS = ("daily", "daily_cn", "daily_us", "weekly")


def outbox_rows(db):
    with db.control() as s:
        return s.scalars(
            select(Outbox).where(Outbox.kind.in_(REPORT_KINDS)).order_by(Outbox.dedupe_key)
        ).all()


def test_daily_cn_session_opens_at_1630_beijing(db, user, instrument, opening):
    a, b = user(), user()
    iid = instrument()
    opening(a, iid)
    opening(b, iid)
    enable(db, [a, b], daily=True)
    before = datetime(2026, 10, 23, 16, 29, tzinfo=CN)
    assert tick(db, before) == 0
    at_open = datetime(2026, 10, 23, 16, 30, tzinfo=CN)
    assert tick(db, at_open) == 2
    assert tick(db, at_open) == 0
    rows = outbox_rows(db)
    assert {(row.kind, row.payload["period"]) for row in rows} == {("daily_cn", "2026-10-23")}


def test_daily_us_session_follows_new_york_close(db, user, instrument, opening):
    a = user()
    opening(a, instrument())
    enable(db, [a], daily=True)
    # 2026-10-23 is a Friday; 03:00 Beijing = 15:00 ET, before the 16:30 ET window.
    assert tick(db, datetime(2026, 10, 24, 3, 0, tzinfo=CN)) == 0
    # 05:30 Beijing = 17:30 ET Friday: US recap for the New York date is due.
    assert tick(db, datetime(2026, 10, 24, 5, 30, tzinfo=CN)) == 1
    rows = outbox_rows(db)
    assert [(row.kind, row.payload["period"]) for row in rows] == [("daily_us", "2026-10-23")]


def test_weekly_runs_saturday_0900_beijing(db, user, instrument, opening):
    a = user()
    opening(a, instrument())
    enable(db, [a], weekly=True)
    friday_evening = datetime(2026, 10, 23, 18, 0, tzinfo=CN)
    assert tick(db, friday_evening) == 0
    saturday_before = datetime(2026, 10, 24, 8, 59, tzinfo=CN)
    assert tick(db, saturday_before) == 0
    saturday = datetime(2026, 10, 24, 9, 0, tzinfo=CN)
    assert tick(db, saturday) == 1
    rows = outbox_rows(db)
    assert [(row.kind, row.payload["period"]) for row in rows] == [("weekly", "2026-10-24")]


def test_large_ids_are_delayed_in_batches(db, user, instrument, opening):
    users = [user(), user(), user()]
    iid = instrument()
    for user_row in users:
        opening(user_row, iid)
    enable(db, users, daily=True)
    window = datetime(2026, 10, 23, 16, 30, tzinfo=CN)
    # batch_users=1 + 10s interval: each user id gets its own 10s slot.
    assert tick(db, window, batch_users=1, batch_interval=10) == 1
    assert tick(db, window.replace(second=10), batch_users=1, batch_interval=10) == 1
    assert tick(db, window.replace(second=20), batch_users=1, batch_interval=10) == 1
    assert tick(db, window.replace(second=20), batch_users=1, batch_interval=10) == 0
    rows = outbox_rows(db)
    assert len(rows) == 3
    due = sorted(row.payload["due_at"] for row in rows)
    assert due == [
        window.astimezone(UTC).isoformat(),
        (window.replace(second=10)).astimezone(UTC).isoformat(),
        (window.replace(second=20)).astimezone(UTC).isoformat(),
    ]
    with db.control() as s:
        assert (
            s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind.in_(REPORT_KINDS)))
            == 3
        )
