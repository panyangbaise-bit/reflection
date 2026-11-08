from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import func, select

from portfolio_backend.errors import DomainError
from portfolio_backend.models import CatalogSyncState, Instrument
from portfolio_backend.providers.catalog import match_catalog, normalize_catalog
from portfolio_backend.services.catalog import CatalogService


def catalog_row(symbol="600519", name="贵州茅台", provider="akshare:sina-minute"):
    return {
        "symbol": symbol,
        "name": name,
        "market": "CN",
        "asset_class": "stock",
        "currency": "CNY",
        "provider": provider,
        "provider_symbol": "sh" + symbol,
        "quote_kind": "realtime",
        "freshness_seconds": 120,
    }


def test_normalize_cn_stock_and_etf_lists():
    stocks = normalize_catalog(
        "cn-stock",
        [
            {"代码": "sh600519", "名称": "贵州茅台"},
            {"代码": "sz000001", "名称": "平安银行"},
            {"代码": "bad", "名称": "无效"},
            {"代码": "sh600000", "名称": ""},
        ],
    )
    assert [row["symbol"] for row in stocks] == ["600519", "000001"]
    assert stocks[0]["provider_symbol"] == "sh600519"
    assert stocks[0]["provider"] == "akshare:sina-minute"
    assert stocks[0]["quote_kind"] == "realtime"
    assert stocks[0]["asset_class"] == "stock"

    etfs = normalize_catalog("cn-etf", [{"代码": "sz159998", "名称": "计算机ETF天弘"}])
    assert etfs[0]["symbol"] == "159998"
    assert etfs[0]["asset_class"] == "etf"
    assert etfs[0]["provider"] == "akshare:sina-minute"


def test_normalize_hk_codes_and_delayed_feed():
    rows = normalize_catalog(
        "hk-stock",
        [{"代码": 1.0, "中文名称": "长和", "英文名称": "CKH HOLDINGS"}],
    )
    assert rows[0]["symbol"] == "00001"
    assert rows[0]["name"] == "长和 CKH HOLDINGS"
    assert rows[0]["market"] == "HK"
    assert rows[0]["currency"] == "HKD"
    assert rows[0]["provider"] == "akshare:hk-spot"
    assert rows[0]["quote_kind"] == "delayed"


def test_catalog_match_ranking_prefers_exact_code():
    rows = [
        {
            "market": "CN",
            "symbol": "600519",
            "asset_class": "stock",
            "name": "贵州茅台",
            "provider_symbol": "sh600519",
        },
        {
            "market": "CN",
            "symbol": "600518",
            "asset_class": "stock",
            "name": "康美药业",
            "provider_symbol": "sh600518",
        },
        {
            "market": "CN",
            "symbol": "600519",
            "asset_class": "etf",
            "name": "示例ETF",
            "provider_symbol": "sh600519",
        },
    ]
    matched = match_catalog(rows, "600519", limit=3)
    assert matched[0]["symbol"] == "600519"
    assert match_catalog(rows, "茅台")[0]["symbol"] == "600519"


def test_catalog_sync_is_idempotent_and_preserves_manual_rows(db, cfg):
    with db.control() as s:
        s.add(
            Instrument(
                symbol="600519",
                name="手工配置名称",
                market="CN",
                asset_class="stock",
                currency="CNY",
                provider="fixture",
                quote_kind="realtime",
                enabled=True,
                commercial_display=True,
            )
        )
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    rows = [catalog_row("600519", "贵州茅台"), catalog_row("000001", "平安银行")]
    service._fetch = lambda kind: rows
    first = service.sync(kinds=("cn-stock",))
    second = service.sync(kinds=("cn-stock",))
    assert first["cn-stock"] == second["cn-stock"] == 2
    with db.control() as s:
        assert s.scalar(select(func.count()).select_from(Instrument)) == 2
        manual = s.scalar(select(Instrument).where(Instrument.symbol == "600519"))
        assert manual.provider == "fixture" and manual.name == "手工配置名称"
        auto = s.scalar(select(Instrument).where(Instrument.symbol == "000001"))
        assert auto.provider == "akshare:sina-minute"
        assert auto.enabled and auto.commercial_display
    assert [row.symbol for row in service.search("000001")] == ["000001"]

    revoked = CatalogService(cfg.model_copy(update={"akshare_display_confirmed": False}), db, cache)
    revoked._fetch = lambda kind: rows
    revoked.sync(kinds=("cn-stock",))
    with db.control() as s:
        auto = s.scalar(select(Instrument).where(Instrument.symbol == "000001"))
        manual = s.scalar(select(Instrument).where(Instrument.symbol == "600519"))
        assert not auto.commercial_display and manual.commercial_display
    cache.close()


def test_catalog_sync_continues_when_one_source_fails(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)

    def fetch(kind):
        if kind == "cn-etf":
            raise DomainError("catalog_source_unavailable", "bad", 503)
        return [catalog_row()]

    service._fetch = fetch
    result = service.sync(kinds=("cn-stock", "cn-etf"))
    assert result["cn-stock"] == 1 and result["cn-etf"] == 0
    assert result["total"] == 1
    cache.close()


def test_empty_catalog_requests_sync_instead_of_silent_empty(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    with pytest.raises(DomainError) as error:
        service.ready_or_request("600519")
    assert error.value.code == "catalog_unavailable"
    assert cache.get("catalog:v1:sync-requested") == b"due"
    cache.close()


def test_search_zero_pads_short_hk_codes(db, cfg):
    with db.control() as s:
        s.add(
            Instrument(
                id=uuid4(),
                symbol="00700",
                name="腾讯控股 TENCENT",
                market="HK",
                asset_class="stock",
                currency="HKD",
                provider="akshare:hk-spot",
                provider_symbol="00700",
                quote_kind="delayed",
                enabled=True,
                commercial_display=True,
                freshness_seconds=1800,
                created_at=datetime.now(timezone.utc),
            )
        )
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    assert [row.symbol for row in service.search("700")] == ["00700"]
    assert [row.symbol for row in service.search("腾讯")] == ["00700"]
    cache.close()


def test_admin_catalog_sync_queues_a_request(client, cfg):
    response = client.post(
        "/api/admin/v1/catalog/sync",
        headers={
            "Authorization": "Bearer test-admin-credential",
            "X-Admin-Reason": "refresh catalog for test",
        },
    )
    assert response.status_code == 202, response.text
    cache = Redis.from_url(cfg.cache_url)
    assert cache.get("catalog:v1:sync-requested") == b"1"
    cache.close()


def test_nan_names_are_ignored_in_hk_rows():
    rows = normalize_catalog(
        "hk-stock",
        [{"代码": 700, "中文名称": float("nan"), "英文名称": "TENCENT"}],
    )
    assert rows[0]["symbol"] == "00700"
    assert rows[0]["name"] == "TENCENT"


def test_sync_if_due_only_refreshes_stale_lists(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    calls = []

    def fetch(kind):
        calls.append(kind)
        return [catalog_row()]

    service._fetch = fetch
    service.sync(kinds=("cn-stock",))
    assert calls == ["cn-stock"]
    calls.clear()
    service.sync_if_due()
    assert calls == ["cn-etf", "hk-stock", "cn-fund", "us-stock", "cn-lof", "cn-closed-fund"]
    calls.clear()
    assert service.sync_if_due() is None
    assert calls == []
    cache.close()


def test_us_catalog_retains_source_namespace_and_searchable_symbol():
    rows = normalize_catalog(
        "us-stock",
        [
            {"代码": "105.DUOL", "名称": "Duolingo"},
            {"代码": "106.BRK.B", "名称": "Berkshire Hathaway"},
            {"代码": "DUOL", "名称": "Ambiguous"},
            {"代码": "999.DUOL", "名称": "Unknown exchange"},
            {"代码": "105.DUOL", "名称": "Duolingo"},
        ],
    )
    assert len(rows) == 2
    item = match_catalog(rows, "duol")[0]
    assert item["symbol"] == "DUOL"
    assert item["provider_symbol"] == "105.DUOL"
    assert item["market"] == "US"
    assert item["currency"] == "USD"
    assert item["provider"] == "akshare:us-minute"
    assert item["quote_kind"] == "delayed"
    assert match_catalog(rows, "Duolingo")[0] == item


@pytest.mark.parametrize("row_count", [1, 5000])
def test_partial_catalog_keeps_search_available(db, cfg, row_count):
    from portfolio_backend.services.catalog import KINDS

    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service.count = lambda: row_count
    for kind in KINDS:
        if kind != "us-stock":
            cache.set(service.last_key(kind), "1")
    assert service.ready_or_request("DUOL") is True
    assert cache.get("catalog:v1:sync-requested") == b"due"
    cache.set(service.last_key("us-stock"), "1")
    assert service.ready_or_request("UNKNOWN") is True
    cache.close()


def test_failed_source_waits_before_retry_and_keeps_existing_rows(db, cfg):
    from portfolio_backend.services.catalog import FAILED_KEY, KINDS

    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service._upsert(normalize_catalog("us-stock", [{"代码": "105.DUOL", "名称": "Duolingo"}]))
    calls = []

    def failed(kind):
        calls.append(kind)
        raise DomainError("catalog_source_unavailable", "bad", 503)

    service._fetch = failed
    for kind in KINDS:
        if kind != "us-stock":
            service.record_attempt(kind, count=1)
    assert service.sync_if_due()["status"] == "partial"
    assert calls == ["us-stock"]
    assert service.search("duol")[0].symbol == "DUOL"
    service.request_sync(force=False)
    assert service.consume_request() is None
    assert calls == ["us-stock"]
    cache.delete(FAILED_KEY + "us-stock")
    with db.control() as session:
        state = session.get(CatalogSyncState, "us-stock")
        state.last_attempt -= timedelta(seconds=301)
    service._fetch = lambda kind: normalize_catalog(
        "us-stock", [{"代码": "105.DUOL", "名称": "Duolingo"}]
    )
    assert service.sync_if_due()["status"] == "ok"
    assert cache.get(service.last_key("us-stock"))
    assert cache.get(FAILED_KEY + "us-stock") is None
    cache.close()


def test_admin_sync_is_not_downgraded_by_search_request(db, cfg):
    cache = Redis.from_url(cfg.cache_url)
    service = CatalogService(cfg, db, cache)
    service.request_sync()
    service.request_sync(force=False)
    service.sync = lambda: {"forced": True}
    service.sync_if_due = lambda: {"forced": False}
    assert service.consume_request() == {"forced": True}
    cache.close()


def test_api_does_not_block_search_when_one_market_is_missing(client, user):
    identity = user()
    service = client.app.state.catalog
    service.count = lambda: 5000
    response = client.get("/api/mobile/v1/instruments?query=DUOL", headers=identity["headers"])
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    service._upsert(normalize_catalog("us-stock", [{"代码": "105.DUOL", "名称": "Duolingo"}]))
    response = client.get("/api/mobile/v1/instruments?query=DUOL", headers=identity["headers"])
    assert response.status_code == 200
    assert "DUOL" in response.text
