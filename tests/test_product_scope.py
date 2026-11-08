from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from portfolio_backend.agent.citations import resolve
from portfolio_backend.db import now
from portfolio_backend.models import LatestQuote, Message
from portfolio_backend.providers.catalog import normalize_catalog
from portfolio_backend.providers.news import DeepSeekWebSearch
from portfolio_backend.services.ai import settle_run

M = "/api/mobile/v1"


def test_empty_cost_uses_current_quote_and_retries_keep_original_price(
    client, db, user, instrument
):
    u, iid = user(), instrument()
    body = {
        "account_id": str(u["account_id"]),
        "instrument_id": str(iid),
        "quantity": "5",
        "occurred_at": now().isoformat(),
    }
    headers = u["headers"] | {"Idempotency-Key": "cost-from-quote"}
    first = client.post(M + "/opening-positions", headers=headers, json=body)
    assert first.status_code == 201, first.text
    p = first.json()["data"]
    assert Decimal(p["average_cost"]) == Decimal("12")
    assert p["opening_cost_source"] == "fixture" and p["opening_quote_as_of"]
    with db.control() as s:
        s.get(LatestQuote, iid).price = Decimal("13")
    replay = client.post(M + "/opening-positions", headers=headers, json=body)
    assert replay.json() == first.json()
    event = client.get(M + "/ledger-events", headers=u["headers"]).json()["data"]["items"][0]
    assert Decimal(event["price"]) == Decimal("12")


def test_empty_cost_without_valid_quote_requires_manual_cost(client, db, user, instrument):
    u, iid = user(), instrument()
    with db.control() as s:
        s.get(LatestQuote, iid).valid_until = now() - timedelta(seconds=1)
    body = {
        "account_id": str(u["account_id"]),
        "instrument_id": str(iid),
        "quantity": "1",
        "occurred_at": now().isoformat(),
    }
    headers = u["headers"] | {"Idempotency-Key": "no-valid-cost"}
    response = client.post(M + "/opening-positions", headers=headers, json=body)
    assert (
        response.status_code == 422 and response.json()["error"]["code"] == "cost_quote_unavailable"
    )
    body["price"] = "10"
    assert client.post(M + "/opening-positions", headers=headers, json=body).status_code == 201


def test_empty_cost_accepts_freshly_fetched_closing_price_without_current_valuation(
    client, db, user, instrument, monkeypatch
):
    u = user()
    iid = instrument(
        symbol="600519",
        provider="akshare:sina-minute",
        provider_symbol="sh600519",
        freshness_seconds=120,
    )
    with db.control() as s:
        quote = s.get(LatestQuote, iid)
        quote.as_of = now() - timedelta(hours=2)
        quote.fetched_at = quote.as_of
        quote.valid_until = now() - timedelta(hours=1)
    mark_time = now() - timedelta(minutes=30)
    # Keep this deterministic: during the session a stale mark stays missing.
    monkeypatch.setattr("portfolio_backend.services.portfolio.market_open", lambda market, at: True)
    monkeypatch.setattr(
        "portfolio_backend.providers.market.AKShareQuotes._fetch",
        lambda feed, symbol, timeout: [{"as_of": mark_time.isoformat(), "price": "12.50"}],
    )
    body = {
        "account_id": str(u["account_id"]),
        "instrument_id": str(iid),
        "quantity": "1",
        "occurred_at": now().isoformat(),
    }
    headers = u["headers"] | {"Idempotency-Key": "closing-price-cost"}
    response = client.post(M + "/opening-positions", headers=headers, json=body)
    assert response.status_code == 201, response.text
    position = response.json()["data"]
    assert Decimal(position["average_cost"]) == Decimal("12.50")
    assert position["opening_cost_source"] == "akshare:sina-minute"
    assert position["opening_quote_as_of"] == mark_time.isoformat()
    assert (
        client.post(M + "/opening-positions", headers=headers, json=body).json() == response.json()
    )
    snapshot = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert snapshot["positions"][0]["quote_status"] == "stale"
    assert snapshot["market_value_cny"] is None


def test_empty_cost_does_not_reuse_stale_quote_when_refresh_fails(
    client, db, user, instrument, monkeypatch
):
    from portfolio_backend.errors import DomainError

    u = user()
    iid = instrument(symbol="600519", provider="akshare:sina-minute", provider_symbol="sh600519")
    with db.control() as s:
        quote = s.get(LatestQuote, iid)
        quote.as_of = now() - timedelta(hours=2)
        quote.valid_until = now() - timedelta(hours=1)

    def unavailable(*args, **kwargs):
        raise DomainError("market_source_unavailable", "unavailable", 503)

    monkeypatch.setattr("portfolio_backend.providers.market.AKShareQuotes._fetch", unavailable)
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "failed-price-refresh"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(iid),
            "quantity": "1",
            "occurred_at": now().isoformat(),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cost_quote_unavailable"


def test_fund_nav_change_uses_two_dated_navs_not_realtime(client, db, user, instrument):
    u = user()
    iid = instrument(asset_class="fund", quote_kind="nav")
    stamp = now() - timedelta(hours=1)
    with db.control() as s:
        quote = s.get(LatestQuote, iid)
        quote.as_of = stamp
        quote.previous_price = Decimal("10")
        quote.previous_as_of = stamp - timedelta(days=1)
    item = client.get(M + "/instruments?query=510300", headers=u["headers"]).json()["data"][
        "items"
    ][0]
    assert item["quote_status"] == "nav"
    assert Decimal(item["nav_change_pct"]) == Decimal("20")
    assert item["nav_previous_as_of"]
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "nav-cost"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(iid),
            "quantity": "1",
            "occurred_at": now().isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    snapshot = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert snapshot["positions"][0]["quote_kind"] == "nav"
    assert snapshot["market_value_cny"] is None  # NAV is not an intraday market price.


def test_fund_catalog_and_live_nav_fallback(client, db, user, instrument, monkeypatch):
    rows = normalize_catalog(
        "cn-fund",
        [
            {"代码": "001234", "名称": "基金", "基金类型": "混合型"},
            {"代码": "009999", "名称": "货币基金", "基金类型": "货币型"},
        ],
    )
    assert len(rows) == 2 and all(row["provider"] == "akshare:fund-nav" for row in rows)
    u = user()
    iid = instrument(
        symbol="001234",
        provider="akshare:fund-nav",
        provider_symbol="001234",
        asset_class="fund",
        quote_kind="nav",
        freshness_seconds=604800,
    )
    with db.control() as s:
        s.delete(s.get(LatestQuote, iid))
    today = now().astimezone(ZoneInfo("Asia/Shanghai")).date()
    calls = []

    def fetch(feed, symbol, timeout=50):
        calls.append((feed, symbol))
        return [
            {"as_of": str(today - timedelta(days=1)), "price": "1.2"},
            {"as_of": str(today), "price": "1.5"},
        ]

    monkeypatch.setattr("portfolio_backend.providers.market.AKShareQuotes._fetch", fetch)
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "fresh-nav"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(iid),
            "quantity": "1",
            "occurred_at": now().isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    assert calls == [("fund-nav", "001234")]
    assert Decimal(response.json()["data"]["average_cost"]) == Decimal("1.5")
    item = client.get(M + "/instruments?query=001234", headers=u["headers"]).json()["data"][
        "items"
    ][0]
    assert Decimal(item["nav_change_pct"]) == Decimal("25")


def test_watchlist_owner_and_instrument_id(client, user, instrument):
    a, b = user(), user()
    iid = instrument()
    body = {"instrument_id": str(iid), "priority": 2, "target_price": "9.5", "note": "个人观察"}
    assert client.put(M + "/watchlist", headers=a["headers"], json=body).status_code == 200
    assert client.put(M + "/watchlist", headers=a["headers"], json=body).status_code == 200
    data = client.get(M + "/watchlist", headers=a["headers"]).json()["data"]["items"]
    assert len(data) == 1 and data[0]["instrument_id"] == str(iid) and data[0]["note"] == "个人观察"
    assert client.get(M + "/watchlist", headers=b["headers"]).json()["data"]["items"] == []
    assert (
        client.delete(M + "/watchlist/" + str(iid), headers=b["headers"]).json()["data"]["removed"]
        is False
    )
    assert (
        client.delete(M + "/watchlist/" + str(iid), headers=a["headers"]).json()["data"]["removed"]
        is True
    )


def test_deepseek_search_registers_only_safe_sources(cfg):
    cfg = cfg.model_copy(update={"deepseek_api_key": SecretStr("test")})

    def respond(request):
        assert request.url.path == "/v1/tools/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "公开文章",
                        "url": "https://example.com/article",
                        "snippet": "摘要",
                        "date": None,
                    },
                    {"title": "内网", "url": "https://127.0.0.1/private", "snippet": "private"},
                ]
            },
        )

    data = DeepSeekWebSearch(cfg, httpx.MockTransport(respond)).search("基金公告")
    assert len(data["items"]) == 1 and data["items"][0]["published_at"] is None
    source = data["items"][0]
    assert "https://example.com/article" in resolve(
        "[[cite:" + source["source_id"] + "]]", {source["source_id"]: source}
    )


def test_web_search_requires_updated_disclosure_and_respects_budget(db, user, cfg, monkeypatch):
    import time

    import pytest

    from portfolio_backend.agent.tools.tenant import TenantTools
    from portfolio_backend.errors import DomainError

    uid = user()["id"]
    old = cfg.model_copy(update={"deepseek_api_key": SecretStr("test"), "search_daily_limit": 1})
    tool = TenantTools(db, uid, old, time.monotonic() + 30)
    with pytest.raises(DomainError) as exc:
        tool("search_web", {"query": "基金公告"})
    assert exc.value.code == "search_disclosure_outdated"
    monkeypatch.setattr(
        DeepSeekWebSearch,
        "search",
        lambda self, query, timeout: {"items": [], "query_received": query},
    )
    fresh = old.model_copy(update={"ai_disclosure_version": "2"})
    tool = TenantTools(db, uid, fresh, time.monotonic() + 30)
    assert tool("search_web", {"query": "基金公告"})["items"] == []
    with pytest.raises(DomainError) as exc:
        tool("search_web", {"query": "第二次搜索"})
    assert exc.value.code == "search_budget_exhausted"


def test_chart_payload_survives_message_history(client, db, user):
    from portfolio_backend.models import AIRun, Conversation

    u = user()
    chart = {
        "points": [{"date": "2026-10-20", "close": "3.14"}],
        "annotations": [{"index": 0, "text": "记录"}],
    }
    with db.tenant(u["id"]) as s:
        conversation = Conversation(user_id=u["id"], title="图表")
        s.add(conversation)
        s.flush()
        run = AIRun(user_id=u["id"], conversation_id=conversation.id, status="queued")
        s.add(run)
        s.flush()
        rid, cid = run.id, conversation.id
        settle_run(s, u["id"], rid, answer="图表 [[cite:source]]", usage={"charts": [chart]})
    response = client.get(M + f"/conversations/{cid}/messages?paged=true", headers=u["headers"])
    assert response.json()["data"]["items"][0]["charts"] == [chart]
    with db.tenant(u["id"]) as s:
        assert s.scalar(select(Message).where(Message.conversation_id == cid)).charts == [chart]
