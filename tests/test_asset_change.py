"""Asset change and honest close-mark relaxation (home valuation after hours)."""

from datetime import timedelta
from decimal import Decimal

from portfolio_backend.db import now
from portfolio_backend.models import LatestQuote, Report
from portfolio_backend.services import portfolio


def stale_quote(db, instrument_id, age=timedelta(days=1)):
    with db.control() as s:
        row = s.get(LatestQuote, instrument_id)
        row.as_of = now() - age
        row.fetched_at = row.as_of
        row.valid_until = now() - timedelta(seconds=1)
    return instrument_id


def test_live_close_marks_show_latest_close_when_market_closed(
    db, cfg, user, instrument, opening, monkeypatch
):
    monkeypatch.setattr(portfolio, "market_open", lambda market, at: False)
    u = user()
    i = instrument()
    opening(u, i, quantity="100", price="10")
    stale_quote(db, i)
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"])
    assert data["complete"]
    assert data["positions"][0]["quote_status"] == "close_mark"
    assert Decimal(data["positions"][0]["price"]) == 12
    assert Decimal(data["market_value_cny"]) == 1200


def test_live_stale_mark_stays_missing_during_session(
    db, cfg, user, instrument, opening, monkeypatch
):
    monkeypatch.setattr(portfolio, "market_open", lambda market, at: True)
    u = user()
    i = instrument()
    opening(u, i)
    stale_quote(db, i)
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"])
    assert not data["complete"]
    assert data["positions"][0]["quote_status"] == "stale"
    assert data["positions"][0]["market_value"] is None


def test_close_mark_ignores_marks_older_than_window(
    db, cfg, user, instrument, opening, monkeypatch
):
    monkeypatch.setattr(portfolio, "market_open", lambda market, at: False)
    u = user()
    i = instrument()
    opening(u, i)
    stale_quote(db, i, age=timedelta(days=10))
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"])
    assert not data["complete"]
    assert data["positions"][0]["quote_status"] == "stale"


def test_close_mark_relaxation_can_be_disabled(db, cfg, user, instrument, opening, monkeypatch):
    monkeypatch.setattr(portfolio, "market_open", lambda market, at: False)
    u = user()
    i = instrument()
    opening(u, i)
    stale_quote(db, i)
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"], close_marks=False)
    assert not data["complete"] and data["positions"][0]["quote_status"] == "stale"


def test_weekend_report_uses_fridays_close_as_close_mark(
    db, cfg, user, instrument, opening, monkeypatch
):
    u = user()
    i = instrument()
    opening(u, i)
    with db.control() as s:
        row = s.get(LatestQuote, i)
        row.as_of = now() - timedelta(days=1)
        row.valid_until = now() - timedelta(seconds=1)
        report_day = (now() - timedelta(days=1)).astimezone(portfolio.CN_TZ).date() + timedelta(
            days=1
        )
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"], report_date=report_day)
    assert data["complete"]
    assert data["positions"][0]["quote_status"] == "close_mark"


def test_asset_change_comes_from_complete_daily_marks(db, user, instrument, opening):
    u = user()
    i = instrument()
    opening(u, i, quantity="100", price="10")
    at = now()
    with db.control() as s:
        for offset, complete, value in [(3, False, "1"), (2, True, "1000"), (1, True, "1100")]:
            stamp = at - timedelta(days=offset)
            s.add(
                Report(
                    user_id=u["id"],
                    kind="daily",
                    period=stamp.date().isoformat(),
                    title="报告",
                    summary="摘要",
                    created_at=stamp,
                    snapshot={
                        "as_of": stamp.isoformat(),
                        "complete": complete,
                        "market_value_cny": value,
                    },
                )
            )
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"])
    # Current priced value 1200 vs the latest complete mark 1100; yesterday 1000→1100.
    assert Decimal(data["daily_pnl_cny"]) == Decimal("100")
    assert Decimal(data["yesterday_pnl_cny"]) == Decimal("100")


def test_asset_change_needs_two_complete_marks(db, user, instrument, opening):
    u = user()
    i = instrument()
    opening(u, i)
    at = now()
    with db.control() as s:
        s.add(
            Report(
                user_id=u["id"],
                kind="daily",
                period=at.date().isoformat(),
                title="报告",
                summary="摘要",
                created_at=at - timedelta(days=1),
                snapshot={
                    "as_of": (at - timedelta(days=1)).isoformat(),
                    "complete": True,
                    "market_value_cny": "1000",
                },
            )
        )
    with db.tenant(u["id"]) as s:
        data = portfolio.snapshot(s, u["id"])
    assert data["yesterday_pnl_cny"] is None
    assert Decimal(data["daily_pnl_cny"]) == Decimal("200")


def test_home_chart_appends_current_mark(client, db, user, instrument, opening):
    u = user()
    i = instrument()
    opening(u, i, quantity="100", price="10")
    at = now()
    with db.control() as s:
        s.add(
            Report(
                user_id=u["id"],
                kind="daily",
                period=(at - timedelta(days=1)).date().isoformat(),
                title="报告",
                summary="摘要",
                created_at=at - timedelta(days=1),
                snapshot={
                    "as_of": (at - timedelta(days=1)).isoformat(),
                    "complete": True,
                    "market_value_cny": "1000",
                },
            )
        )
    data = client.get("/api/mobile/v1/home", headers=u["headers"]).json()["data"]
    history = data["asset_history"]
    assert [Decimal(point["market_value_cny"]) for point in history] == [
        Decimal("1000"),
        Decimal("1200"),
    ]
    assert data["snapshot"]["complete"]
