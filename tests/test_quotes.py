from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from redis import Redis
from sqlalchemy import func, select

from portfolio_backend.db import now
from portfolio_backend.models import FXRate, LatestQuote, Outbox

M = "/api/mobile/v1"


def test_missing_quote_never_uses_cost(client, user, instrument, opening, db):
    u, i = user(), instrument()
    opening(u, i)
    with db.control() as s:
        s.delete(s.get(LatestQuote, i))
    data = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert not data["complete"]
    assert data["market_value_cny"] is None
    assert data["positions"][0]["price"] is None
    assert Decimal(data["priced_market_value_cny"]) == 0


def test_nav_is_addable_as_a_snapshot_not_as_realtime(client, user, instrument):
    u = user()
    instrument(asset_class="fund", quote_kind="nav")
    data = client.get(M + "/instruments", headers=u["headers"]).json()["data"]["items"][0]
    assert data["can_add"]
    assert data["quote_kind"] == "nav"
    assert data["unavailable_reason"] is None


def test_stale_quote_does_not_block_opening_snapshot(client, user, instrument, db, monkeypatch):
    monkeypatch.setattr("portfolio_backend.services.portfolio.market_open", lambda market, at: True)
    u, i = user(), instrument()
    with db.control() as s:
        row = s.get(LatestQuote, i)
        row.valid_until = now() - timedelta(seconds=1)
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "stale"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(i),
            "quantity": "1",
            "price": "10",
            "occurred_at": now().isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    data = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert not data["complete"]
    assert data["positions"][0]["quote_status"] == "stale"
    assert Decimal(data["positions"][0]["price"]) == Decimal("12")
    assert data["positions"][0]["market_value"] is None
    assert data["missing"][0]["reason"] == "stale"


def test_missing_quote_is_visible_and_still_recordable(client, user, instrument, db):
    u, i = user(), instrument()
    with db.control() as s:
        s.delete(s.get(LatestQuote, i))
    item = client.get(M + "/instruments?query=510300", headers=u["headers"]).json()["data"][
        "items"
    ][0]
    assert item["can_add"] and item["price"] is None
    assert item["quote_status"] == "missing"
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "no-quote"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(i),
            "quantity": "1",
            "price": "10",
            "occurred_at": now().isoformat(),
        },
    )
    assert response.status_code == 201, response.text


def test_missing_fx_is_explicit(client, user, instrument, opening):
    u = user()
    opening(u, instrument(symbol="AAPL", market="US", currency="USD", asset_class="stock"))
    data = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert data["market_value_cny"] is None
    assert data["missing"][0]["reason"] == "fx_missing"


def test_admin_roles_and_quote_validation(client, user, instrument):
    u, i = user(), instrument()
    assert client.get("/api/admin/v1/overview", headers=u["headers"]).status_code == 403
    body = {
        "instrument_id": str(i),
        "price": "14",
        "provider": "fixture",
        "kind": "realtime",
        "as_of": (now() + timedelta(hours=1)).isoformat(),
        "valid_until": (now() + timedelta(hours=2)).isoformat(),
        "market_state": "open",
    }
    headers = {
        "Authorization": "Bearer test-admin-credential",
        "X-Admin-Reason": "test data publication",
    }
    assert client.put("/api/admin/v1/quotes", json=body, headers=headers).status_code == 422
    headers["Authorization"] = "Bearer test-support-credential"
    assert client.put("/api/admin/v1/quotes", json=body, headers=headers).status_code == 403


def test_revoked_display_rights_hide_quotes_and_valuation(client, db, user, instrument, opening):
    from portfolio_backend.models import Instrument

    u = user()
    iid = instrument()
    opening(u, iid)
    with db.control() as s:
        s.get(Instrument, iid).commercial_display = False
    catalog = client.get("/api/mobile/v1/instruments", headers=u["headers"]).json()["data"][
        "items"
    ][0]
    assert not catalog["can_add"] and catalog["price"] is None
    snapshot = client.get("/api/mobile/v1/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert not snapshot["complete"] and snapshot["market_value_cny"] is None
    assert snapshot["positions"][0]["price"] is None


def test_fresh_nav_mark_counts_as_complete_snapshot(client, user, instrument, opening):
    u = user()
    i = instrument(asset_class="fund", quote_kind="nav", freshness_seconds=604800)
    opening(u, i)
    data = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    # NAV counts toward coverage but stays out of intraday market totals (product rule).
    assert data["complete"]
    assert data["coverage"] == {"total": 1, "priced": 1}
    assert data["positions"][0]["quote_status"] == "nav"
    assert data["positions"][0]["market_value"] is None
    assert data["market_value_cny"] is None


def test_stale_same_day_mark_counts_as_close_mark_for_reports(
    client, user, instrument, opening, db, monkeypatch
):
    from portfolio_backend.services import portfolio

    # Deterministic: treat the session as open so the live view stays strict.
    monkeypatch.setattr(portfolio, "market_open", lambda market, at: True)
    u = user()
    i = instrument()
    opening(u, i)
    with db.control() as s:
        row = s.get(LatestQuote, i)
        row.valid_until = now() - timedelta(seconds=1)
        report_day = now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        # Live view stays strict: stale means incomplete.
        live = portfolio.snapshot(s, u["id"])
        assert not live["complete"] and live["positions"][0]["quote_status"] == "stale"
        # Report view accepts the report day's last mark as the close mark.
        report = portfolio.snapshot(s, u["id"], report_date=report_day)
    assert report["complete"]
    assert report["coverage"] == {"total": 1, "priced": 1}
    assert report["positions"][0]["quote_status"] == "close_mark"
    assert Decimal(report["positions"][0]["market_value"]) > 0


def test_akshare_fx_normalizes_per_hundred_parity():
    from portfolio_backend.providers.market import AKShareFX

    loader = AKShareFX(
        fetch=lambda feed, symbol, timeout: [
            {"currency": "USD", "rate": "671.56", "as_of": "2026-11-06"},
            {"currency": "HKD", "rate": "85.601", "as_of": "2026-11-06"},
            {"currency": "EUR", "rate": "700.00", "as_of": "2026-11-06"},
        ]
    )
    marks = {m.currency: m for m in loader.load()}
    assert set(marks) == {"USD", "HKD"}
    assert marks["USD"].rate == Decimal("6.7156")
    assert marks["HKD"].rate == Decimal("0.85601")
    assert marks["USD"].provider == "akshare:fx"


def test_fx_refresh_upserts_and_honors_poll_window(db, cfg):
    from portfolio_backend.collector import refresh_fx
    from portfolio_backend.providers.market import FXMark

    cache = Redis.from_url(cfg.cache_url)
    cache.delete("public-data:fx-fetched")
    marks = [
        FXMark(
            "USD",
            Decimal("7.10"),
            now() - timedelta(hours=6),
            now() + timedelta(days=3),
            "akshare:fx",
        ),
        FXMark(
            "HKD",
            Decimal("0.91"),
            now() - timedelta(hours=6),
            now() + timedelta(days=3),
            "akshare:fx",
        ),
    ]

    class Loader:
        def __init__(self, rows):
            self.rows = rows

        def load(self):
            return self.rows

    try:
        assert refresh_fx(db, cfg, cache, loader=Loader(marks)) == 2
        with db.control() as s:
            rates = {r.currency: r for r in s.scalars(select(FXRate))}
            assert rates["USD"].rate == Decimal("7.10")
            assert rates["HKD"].rate == Decimal("0.91")
            assert rates["USD"].provider == "akshare:fx"
        # Within the poll window the second call is a no-op; an older same-provider mark never overwrites.
        assert refresh_fx(db, cfg, cache, loader=Loader(marks)) == 0
        old = [
            FXMark(
                "USD",
                Decimal("6.00"),
                now() - timedelta(days=30),
                now() + timedelta(days=3),
                "akshare:fx",
            )
        ]
        cache.delete("public-data:fx-fetched")
        refresh_fx(db, cfg, cache, loader=Loader(old))
        with db.control() as s:
            assert s.scalar(select(FXRate).where(FXRate.currency == "USD")).rate == Decimal("7.10")
        # A different provider replaces the row even with an older as_of (manual test data hand-off).
        switched = [
            FXMark(
                "USD",
                Decimal("7.20"),
                now() - timedelta(days=30),
                now() + timedelta(days=3),
                "akshare:fx-v2",
            )
        ]
        cache.delete("public-data:fx-fetched")
        refresh_fx(db, cfg, cache, loader=Loader(switched))
        with db.control() as s:
            row = s.scalar(select(FXRate).where(FXRate.currency == "USD"))
            assert row.rate == Decimal("7.20") and row.provider == "akshare:fx-v2"
    finally:
        cache.delete("public-data:fx-fetched")
        with db.control() as s:
            from sqlalchemy import delete

            s.execute(delete(FXRate).where(FXRate.currency.in_(["USD", "HKD"])))


def test_admin_report_replay_clears_dedupe_chain(client, user, instrument, opening, db):
    u = user()
    opening(u, instrument())
    today = now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    headers = {"Authorization": "Bearer test-admin-credential", "X-Admin-Reason": "replay test"}
    url = f"/api/admin/v1/users/{u['id']}/reports"
    assert (
        client.post(url, json={"kind": "daily", "period": today}, headers=headers).status_code
        == 202
    )
    key = f"report:{u['id']}:daily:{today}"

    def count_outbox():
        with db.control() as s:
            return s.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.dedupe_key == key)
            )

    assert count_outbox() == 1
    # Replay clears the chain and requeues once; plain requeue stays deduplicated.
    assert (
        client.post(
            url, json={"kind": "daily", "period": today, "replay": True}, headers=headers
        ).status_code
        == 202
    )
    assert count_outbox() == 1
    # Admin user list exposes today's report state for the trigger UI.
    listing = client.get(
        "/api/admin/v1/users", headers={"Authorization": "Bearer test-admin-credential"}
    ).json()["data"]["items"]
    assert any(item["id"] == str(u["id"]) and item["today"] == today for item in listing)
