from datetime import timedelta

import pytest
from redis import Redis
from sqlalchemy import select

from portfolio_backend.models import CatalogSyncState
from portfolio_backend.providers.catalog import normalize_catalog
from portfolio_backend.providers.instrument_search import normalize_suggestions
from portfolio_backend.providers.us_directory import parse_directory
from portfolio_backend.services.catalog import KINDS, CatalogService


def us_rows():
    return parse_directory(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "QQQ|Invesco QQQ Trust|G|N|N|100|Y|N\n"
        "QQQM|Invesco Nasdaq 100 ETF|G|N|N|100|Y|N\n"
        "DUOL|Duolingo Inc|Q|N|N|100|N|N\n"
        "ZTEST|Test issue|Q|Y|N|100|N|N\n"
        "File Creation Time: 1103202612:00|||||||\n",
        "nasdaqlisted.txt",
    )


def test_official_us_directory_excludes_test_issues_and_classifies_etfs():
    rows = us_rows()
    assert [r["symbol"] for r in rows] == ["QQQ", "QQQM", "DUOL"]
    assert rows[0]["asset_class"] == "etf" and rows[2]["asset_class"] == "stock"
    assert rows[0]["provider_symbol"] == "QQQ" and rows[0]["quote_kind"] == "close"
    other = parse_directory(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "SPY|SPDR S&P 500|P|SPY|Y|100|N|SPY\n"
        "BRK.B|Berkshire Hathaway|N|BRK.B|N|100|N|BRK/B\n"
        "File Creation Time: 1103202612:00|||||||\n",
        "otherlisted.txt",
    )
    assert other[0]["asset_class"] == "etf" and other[1]["symbol"] == "BRK.B"
    with pytest.raises(ValueError):
        parse_directory("<html>temporarily unavailable</html>", "nasdaqlisted.txt")
    with pytest.raises(ValueError):
        parse_directory("Symbol|Security Name|ETF|Test Issue\nQQQ|QQQ|Y|N\n", "nasdaqlisted.txt")


def test_same_code_keeps_separate_on_exchange_and_off_exchange_funds(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service._upsert(normalize_catalog("cn-lof", [{"代码": "sz161725", "名称": "白酒基金LOF"}]))
    service._upsert(normalize_catalog("cn-fund", [{"代码": "161725", "名称": "招商白酒LOF A"}]))
    service._upsert(
        normalize_catalog("cn-fund", [{"代码": "000198", "名称": "余额宝", "基金类型": "货币型"}])
    )
    assert {row.asset_class for row in service.search("161725")} == {"fund", "listed_fund"}
    assert service.search("余额宝")[0].symbol == "000198"
    cache.close()


def test_exact_lowercase_and_market_suffix_search(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service._upsert(us_rows())
    service._upsert(normalize_catalog("hk-stock", [{"代码": "700", "名称": "腾讯"}]))
    for query in ("qqq", " QQQ.US ", "ＱＱＱ"):
        assert service.search(query)[0].symbol == "QQQ"
    for query in ("700.HK", "HK00700", "00700"):
        assert service.search(query)[0].symbol == "00700"
    assert not service.search("QQQ.HK")
    assert not service.search("%_")
    cache.close()


def test_sync_status_survives_cache_eviction_and_failed_refresh(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service._fetch = lambda kind: us_rows()
    assert service.sync()["status"] == "ok"
    cache.flushdb()
    assert service.sync_if_due() is None
    assert service.ready_or_request("missing")
    service.record_attempt("us-stock", error="catalog_source_unavailable")
    states = service.states()
    assert states["us-stock"].last_success and states["us-stock"].row_count == 3
    assert service.search("QQQ")[0].symbol == "QQQ"
    with db.control() as session:
        for row in session.scalars(select(CatalogSyncState)):
            row.last_success -= timedelta(days=2)
    calls = []
    service._fetch = lambda kind: calls.append(kind) or us_rows()
    service.sync_if_due()
    assert set(calls) == set(KINDS) - {"us-stock"}
    cache.close()


def test_live_search_enriches_alias_without_changing_existing_identity(
    client, cfg, user, monkeypatch
):
    identity = user()
    service = client.app.state.catalog
    service.config = cfg.model_copy(update={"catalog_live_search_enabled": True})
    service._upsert(us_rows())
    existing = service.search("QQQ")[0]
    calls = []
    payload = {
        "QuotationCodeTable": {
            "Data": [
                {
                    "Code": "QQQ",
                    "Name": "纳斯达克100ETF",
                    "Classify": "UsStock",
                    "MktNum": "105",
                    "TypeUS": "5",
                    "PinYin": "NSDK100ETF",
                },
                {"Code": "BTC", "Name": "Not supported", "Classify": "Crypto", "MktNum": "999"},
            ]
        }
    }

    def fetch(query):
        calls.append(query)
        return normalize_suggestions(payload)

    monkeypatch.setattr("portfolio_backend.services.catalog.fetch_suggestions", fetch)
    for _ in range(2):
        response = client.get(
            "/api/mobile/v1/instruments",
            params={"query": "纳斯达克100ETF"},
            headers=identity["headers"],
        )
        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["id"] == str(existing.id) and item["symbol"] == "QQQ"
        assert item["asset_class"] == "etf" and item["can_add"]
    assert calls == ["纳斯达克100ETF"]
    assert not service.search("BTC")

    def unavailable(query):
        raise TimeoutError()

    monkeypatch.setattr("portfolio_backend.services.catalog.fetch_suggestions", unavailable)
    response = client.get(
        "/api/mobile/v1/instruments?query=not-listed", headers=identity["headers"]
    )
    assert response.status_code == 200 and response.json()["data"]["items"] == []
    assert service.search("QQQ")[0].id == existing.id


def test_suggestion_markets_and_fund_venues_are_validated():
    rows = normalize_suggestions(
        {
            "QuotationCodeTable": {
                "Data": [
                    {"Code": "600519", "Name": "茅台", "Classify": "AStock", "MktNum": "1"},
                    {"Code": "00700", "Name": "腾讯", "Classify": "HK", "MktNum": "116"},
                    {"Code": "161725", "Name": "白酒LOF", "Classify": "Fund", "MktNum": "0"},
                    {"Code": "161725", "Name": "白酒基金A", "Classify": "OTCFUND", "MktNum": "150"},
                    {
                        "Code": "AAPL",
                        "Name": "苹果",
                        "Classify": "UsStock",
                        "MktNum": "105",
                        "TypeUS": "1",
                    },
                    {"Code": "garbage", "Name": "bad", "Classify": "AStock", "MktNum": "1"},
                ]
            }
        }
    )
    assert [(r["market"], r["asset_class"]) for r in rows] == [
        ("CN", "stock"),
        ("HK", "stock"),
        ("CN", "listed_fund"),
        ("CN", "fund"),
        ("US", "stock"),
    ]


def test_all_market_results_can_create_holdings_without_live_quote(client, user):
    identity = user()
    service = client.app.state.catalog
    batches = [us_rows()]
    for kind, code in [
        ("cn-stock", "sh600519"),
        ("hk-stock", "00700"),
        ("cn-etf", "sh510300"),
        ("cn-lof", "sz161725"),
        ("cn-fund", "000198"),
    ]:
        batches.append(normalize_catalog(kind, [{"代码": code, "名称": kind}]))
    for batch in batches:
        service._upsert(batch)
    from portfolio_backend.db import now

    for query in ["600519", "00700", "QQQ", "DUOL", "510300", "161725", "000198"]:
        result = client.get(
            "/api/mobile/v1/instruments", params={"query": query}, headers=identity["headers"]
        )
        item = result.json()["data"]["items"][0]
        assert item["can_add"]
        response = client.post(
            "/api/mobile/v1/opening-positions",
            headers=identity["headers"] | {"Idempotency-Key": query},
            json={
                "account_id": str(identity["account_id"]),
                "instrument_id": item["id"],
                "quantity": "1",
                "price": "10",
                "occurred_at": now().isoformat(),
            },
        )
        assert response.status_code == 201, response.text
