import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from redis import Redis
from sqlalchemy import select

from portfolio_backend.collector import collect_news, publish_mark, tick
from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import (
    ExternalBudget,
    Instrument,
    LatestQuote,
    NewsItem,
    User,
)
from portfolio_backend.providers.market import AKShareQuotes
from portfolio_backend.providers.news import DeepSeekSearch, NewsReference, WallStreetCN
from portfolio_backend.services.news import NewsCollector, for_user, relevant
from portfolio_backend.services.public_data import SharedFetch, reserve_request


def asset(provider="akshare:etf", market="CN", symbol="510300", asset_class="etf"):
    return SimpleNamespace(
        provider=provider,
        market=market,
        symbol=symbol,
        provider_symbol=None,
        freshness_seconds=120,
        asset_class=asset_class,
    )


def test_quotes_use_source_time_and_delay_not_fetch_time():
    at = now()
    old = at - timedelta(days=2)
    q = AKShareQuotes()
    mark = q.normalize(
        asset(), [{"symbol": "510300", "price": "4.1234", "as_of": old.isoformat()}], at
    )
    assert mark.as_of == old and mark.valid_until < at
    hk = asset("akshare:hk-minute", "HK", "00700", "stock")
    assert q.normalize(hk, [{"price": "400", "as_of": at.isoformat()}], at).kind == "delayed"
    us = asset("akshare:us-daily", "US", "QQQ", "etf")
    close = q.normalize(us, [{"price": "500", "as_of": old.isoformat()}], at)
    assert close.kind == "close" and close.as_of == old and close.valid_until < at
    for value in ["nan", "-1", "0", "inf", "1000000000001"]:
        with pytest.raises(DomainError):
            q.normalize(
                asset(), [{"symbol": "510300", "price": value, "as_of": at.isoformat()}], at
            )
    with pytest.raises(DomainError):
        q.normalize(asset(), [{"symbol": "510300", "price": "4.2"}], at)
    with pytest.raises(DomainError):
        q.normalize(
            asset(),
            [{"symbol": "510300", "price": "4.2", "as_of": (at + timedelta(days=1)).isoformat()}],
            at,
        )


def test_hk_spot_quote_is_delayed_and_zero_pads_symbols():
    at = now()
    hk = asset("akshare:hk-spot", "HK", "00700", "stock")
    hk.provider_symbol = "00700"
    rows = [
        {"symbol": "1", "price": "64.25", "as_of": "2026/10/23 15:27:07"},
        {"symbol": "700", "price": "400.5", "as_of": "2026/10/23 15:27:07"},
    ]
    mark = AKShareQuotes().normalize(hk, rows, at)
    assert mark.kind == "delayed"
    assert mark.price == Decimal("400.5")
    assert mark.as_of.isoformat().startswith("2026-10-23T15:27:07")
    assert AKShareQuotes().cache_key(hk) == "akshare:hk-spot"


def test_provider_namespace_is_explicit():
    q = AKShareQuotes(fetch=lambda feed, symbol: [{"feed": feed, "symbol": symbol}])
    with pytest.raises(DomainError):
        q.load(asset("akshare:cn-minute", "HK", "00700", "stock"))
    with pytest.raises(DomainError):
        q.load(asset("akshare:us-minute", "US", "AAPL", "stock"))
    us = asset("akshare:us-minute", "US", "AAPL", "stock")
    us.provider_symbol = "105.AAPL"
    assert q.load(us)[0]["symbol"] == "105.AAPL"
    assert q.cache_key(asset(symbol="510300")) == q.cache_key(asset(symbol="518880"))


def test_news_parse_free_only_and_no_full_article():
    data = {
        "data": {
            "items": [
                {
                    "resource": {
                        "title": "<b>腾讯</b>财报",
                        "uri": "https://wallstreetcn.com/articles/123",
                        "content_short": "短摘要" * 50,
                        "content": "PRIVATE FULL ARTICLE",
                        "display_time": now().timestamp(),
                    }
                },
                {
                    "resource": {
                        "title": "付费",
                        "uri": "https://wallstreetcn.com/articles/124",
                        "is_priced": True,
                        "display_time": now().timestamp(),
                    }
                },
            ]
        }
    }
    items = WallStreetCN.parse(data)
    assert len(items) == 1 and items[0].title == "腾讯财报" and len(items[0].excerpt) == 50
    assert "FULL" not in str(items)


def test_deepseek_search_never_sends_private_context_or_fetches_body(cfg):
    cfg = cfg.model_copy(update={"deepseek_api_key": SecretStr("test")})

    def respond(request):
        assert str(request.url) == DeepSeekSearch.endpoint
        payload = json.loads(request.content)
        assert payload == {"query": "腾讯 财报", "count": 5, "need_content": False}
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "腾讯财报",
                        "url": "https://example.com/article",
                        "snippet": "摘要",
                        "summary": "do not store",
                        "content": "FULL BODY",
                        "date": now().isoformat(),
                        "site_name": "source",
                    }
                ]
            },
        )

    items = DeepSeekSearch(cfg, httpx.MockTransport(respond)).search("腾讯 财报")
    assert len(items) == 1 and items[0].excerpt == "摘要" and "FULL BODY" not in str(items)


def test_budget_is_atomic_durable_and_resets_by_shanghai_day(db):
    at = datetime(2026, 10, 24, 15, 59, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: reserve_request(db, "deepseek-search", 7, at), range(20)))
    assert sum(results) == 7
    with db.control() as s:
        assert s.get(ExternalBudget, ("deepseek-search", "2026-10-24")).reserved_calls == 7
    assert reserve_request(db, "deepseek-search", 7, at + timedelta(minutes=2))
    assert not reserve_request(db, "disabled", 0, at)


def test_shared_fetch_singleflight_and_negative_cache(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    shared = SharedFetch(cache)
    started, release = Event(), Event()
    calls = []

    def loader():
        calls.append(1)
        started.set()
        release.wait(3)
        return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(shared.get, "same-symbol", 300, loader)
        assert started.wait(1)
        with pytest.raises(DomainError) as error:
            shared.get("same-symbol", 300, loader)
        assert error.value.code == "data_fetch_pending"
        release.set()
        assert first.result() == []
    assert shared.get("same-symbol", 300, loader) == [] and len(calls) == 1
    cache.close()


def test_free_news_first_then_budgeted_cached_fallback(db, cfg, instrument):
    iid = instrument(name="腾讯", symbol="00700", market="HK", currency="HKD")
    with db.control() as s:
        item = s.get(Instrument, iid)
        s.expunge(item)
    stamp = now()
    ref = NewsReference(
        "https://wallstreetcn.com/articles/1",
        "腾讯财报",
        "摘要",
        "华尔街见闻",
        "wallstreetcn",
        stamp,
    )

    class Primary:
        calls = 0

        def latest(self):
            self.calls += 1
            return [ref]

        def search(self, query):
            self.calls += 1
            return []

    class Fallback:
        calls = 0

        def search(self, query):
            self.calls += 1
            return [
                NewsReference(
                    "https://example.com/2", "腾讯财报", "摘要", "Example", "deepseek-search", stamp
                )
            ]

    cfg = cfg.model_copy(update={"deepseek_api_key": SecretStr("test"), "search_daily_limit": 1})
    cache = Redis.from_url(cfg.cache_url)
    p, f = Primary(), Fallback()
    collector = NewsCollector(db, cfg, SharedFetch(cache), p, f)
    assert collector.collect(item) == 1 and collector.collect(item) == 1
    assert p.calls == 1 and f.calls == 0
    cache.flushdb()
    p.latest = lambda: []
    assert collector.collect(item) == 1 and collector.collect(item) == 1
    assert f.calls == 1
    with db.control() as s:
        assert (
            s.get(
                ExternalBudget,
                (
                    "deepseek-search",
                    now()
                    .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
                    .date()
                    .isoformat(),
                ),
            ).reserved_calls
            == 1
        )
        assert len(s.scalars(select(NewsItem)).all()) == 2
    cache.close()


def test_news_tenant_matching_and_production_gate(client, db, cfg, user, instrument, opening):
    a, b = user(), user()
    iid = instrument(name="腾讯", symbol="00700", market="HK", currency="HKD")
    opening(a, iid)
    ref = NewsReference(
        "https://wallstreetcn.com/articles/1",
        "腾讯财报",
        "摘要",
        "华尔街见闻",
        "wallstreetcn",
        now(),
    )
    NewsCollector(db, cfg, None).save(iid, [ref])
    assert len(client.get("/api/mobile/v1/home", headers=a["headers"]).json()["data"]["news"]) == 1
    assert client.get("/api/mobile/v1/home", headers=b["headers"]).json()["data"]["news"] == []
    with db.tenant(a["id"]) as s:
        assert for_user(s, a["id"], cfg.model_copy(update={"environment": "production"})) == []
        assert (
            len(
                for_user(
                    s,
                    a["id"],
                    cfg.model_copy(
                        update={
                            "environment": "production",
                            "news_display_providers": {"wallstreetcn"},
                        }
                    ),
                )
            )
            == 1
        )


def test_quote_collection_does_not_grant_rights_or_relabel_delay(db, cfg, instrument):
    iid = instrument(provider="akshare:etf", commercial_display=False)
    with db.control() as s:
        item = s.get(Instrument, iid)
        s.expunge(item)
    q = AKShareQuotes()
    mark = q.normalize(
        item, [{"symbol": item.symbol, "price": "4.2", "as_of": now().isoformat()}], now()
    )
    with pytest.raises(DomainError) as e:
        publish_mark(db, item, mark, cfg.model_copy(update={"environment": "production"}))
    assert e.value.code == "data_rights_unconfirmed"
    publish_mark(db, item, mark, cfg)
    with db.control() as s:
        assert not s.get(Instrument, iid).commercial_display
        assert s.get(LatestQuote, iid).as_of == mark.as_of
    item.quote_kind = "delayed"
    with pytest.raises(DomainError):
        publish_mark(db, item, mark, cfg)


def test_ambiguous_short_ticker_does_not_match_arbitrary_prose():
    i = SimpleNamespace(name="Agilent", symbol="A")
    ref = NewsReference(
        "https://example.com", "A new story", "", "Example", "deepseek-search", now()
    )
    assert not relevant(ref, i)


def test_collector_batches_share_quotes_and_only_collect_active_holdings(
    db, cfg, user, instrument, opening, monkeypatch
):
    users = [user() for _ in range(4)]
    symbols = ["510300", "518880", "510500", "159915"]
    ids = [instrument(symbol=symbol, provider="akshare:etf") for symbol in symbols]
    for owner, iid in zip(users, ids):
        opening(owner, iid)
    with db.control() as s:
        s.get(User, users[3]["id"]).status = "deleting"
    stamp = now().isoformat()
    loads, collected = [], []

    def fetch(feed, symbol):
        loads.append(feed)
        return [{"symbol": code, "price": "4.5", "as_of": stamp} for code in symbols]

    def collect(self, item):
        collected.append(item.id)
        return 0

    monkeypatch.setattr(NewsCollector, "collect", collect)
    cfg = cfg.model_copy(
        update={"data_collection_enabled": True, "data_max_instruments_per_tick": 2}
    )
    with Redis.from_url(cfg.cache_url) as cache:
        first = tick(db, cfg, cache, AKShareQuotes(fetch))
        second = tick(db, cfg, cache, AKShareQuotes(fetch))
        news = collect_news(db, cfg, cache)
    assert first["instruments"] == second["instruments"] == 2
    assert first["source_errors"] == second["source_errors"] == 0
    assert first["quote_updates"] + second["quote_updates"] == 4
    assert loads == ["etf"]
    # News runs on its own cadence over active holdings, never deleted accounts.
    assert news["instruments"] == 3 and set(collected) == set(ids[:3])
    with db.control() as s:
        assert {str(quote.price.normalize()) for quote in s.scalars(select(LatestQuote))} == {"4.5"}


def test_collector_ignores_catalog_symbols_without_positions(db, cfg, user, instrument, opening):
    held = instrument(symbol="510300", provider="akshare:etf")
    instrument(symbol="510500", provider="akshare:etf")
    opening(user(), held)
    stamp = now().isoformat()
    loads = []

    def fetch(feed, symbol):
        loads.append(feed)
        return [{"symbol": "510300", "price": "4.5", "as_of": stamp}]

    cfg = cfg.model_copy(update={"data_collection_enabled": True})
    with Redis.from_url(cfg.cache_url) as cache:
        counts = tick(db, cfg, cache, AKShareQuotes(fetch))
    assert counts["instruments"] == 1 and counts["quote_updates"] == 1
    assert loads == ["etf"]


@pytest.mark.parametrize("malformed", [None, {}, "unexpected"])
def test_malformed_sources_fail_with_domain_error(cfg, malformed):
    with pytest.raises(DomainError):
        WallStreetCN.parse({"data": {"items": malformed}})
    with pytest.raises(DomainError):
        AKShareQuotes().normalize(asset(), malformed, now())
    search = DeepSeekSearch(
        cfg.model_copy(update={"deepseek_api_key": SecretStr("test")}),
        httpx.MockTransport(lambda _: httpx.Response(200, json={"results": malformed})),
    )
    with pytest.raises(DomainError):
        search.search("腾讯 财报")


def test_pending_free_fetch_does_not_start_paid_fallback(db, cfg):
    class Pending:
        def get(self, *args):
            raise DomainError("data_fetch_pending", "pending", 503)

    class Fallback:
        def search(self, query):
            pytest.fail("A pending free request must not trigger paid search")

    cfg = cfg.model_copy(update={"deepseek_api_key": SecretStr("test")})
    with pytest.raises(DomainError) as error:
        NewsCollector(db, cfg, Pending(), fallback=Fallback()).collect(asset())
    assert error.value.code == "data_fetch_pending"
    with db.control() as s:
        assert s.scalars(select(ExternalBudget)).all() == []


def test_production_akshare_display_confirmation_gate(db, cfg, instrument):
    iid = instrument(provider="akshare:etf", commercial_display=True)
    with db.control() as s:
        item = s.get(Instrument, iid)
        s.expunge(item)
    mark = AKShareQuotes().normalize(
        item, [{"symbol": item.symbol, "price": "4.2", "as_of": now().isoformat()}], now()
    )
    confirmed = cfg.model_copy(
        update={"environment": "production", "akshare_display_confirmed": True}
    )
    publish_mark(db, item, mark, confirmed)
    rejected = cfg.model_copy(
        update={
            "environment": "production",
            "akshare_display_confirmed": False,
            "commercial_data_providers": set(),
        }
    )
    with pytest.raises(DomainError) as error:
        publish_mark(db, item, mark, rejected)
    assert error.value.code == "data_rights_unconfirmed"
