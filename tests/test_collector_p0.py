"""P0 collector optimization unit tests: no Docker, no network, no real akshare."""

from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from portfolio_backend.collector import effective_news_poll, effective_quote_poll
from portfolio_backend.config import Settings
from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.providers.market import AKShareQuotes, market_session
from portfolio_backend.providers.market_worker import PoolFetcher

SHANGHAI = ZoneInfo("Asia/Shanghai")


def asset(provider="akshare:etf", market="CN", symbol="510300", asset_class="etf"):
    return SimpleNamespace(
        provider=provider,
        market=market,
        symbol=symbol,
        provider_symbol=None,
        freshness_seconds=120,
        asset_class=asset_class,
    )


@pytest.mark.parametrize(
    ("stamp", "open_"),
    [
        ("2026-11-02T09:29:59", False),  # Monday, before open
        ("2026-11-02T09:30:00", True),  # Monday, open
        ("2026-11-02T11:29:59", True),
        ("2026-11-02T11:30:00", False),  # lunch break
        ("2026-11-02T12:59:59", False),
        ("2026-11-02T13:00:00", True),  # afternoon open
        ("2026-11-02T14:59:59", True),
        ("2026-11-02T15:00:00", False),  # close
        ("2026-11-02T23:00:00", False),  # night
        ("2026-11-07T10:00:00", False),  # Saturday
        ("2026-11-08T10:00:00", False),  # Sunday
    ],
)
def test_market_session(stamp, open_):
    at = datetime.fromisoformat(stamp).replace(tzinfo=SHANGHAI)
    assert market_session(at) is open_


def test_market_session_converts_to_shanghai_time():
    # 2026-11-02 02:00 UTC == 10:00 Asia/Shanghai on a Monday: open.
    assert market_session(datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)) is True
    # 2026-11-02 18:00 UTC == 02:00 Asia/Shanghai on a Tuesday: closed.
    assert market_session(datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)) is False


def test_effective_polls_track_session(monkeypatch):
    cfg = Settings(environment="test")
    monkeypatch.setattr("portfolio_backend.collector.market_session", lambda at=None: True)
    assert effective_quote_poll(cfg) == cfg.quote_poll_seconds == 120
    assert effective_news_poll(cfg) == cfg.news_poll_seconds == 300
    monkeypatch.setattr("portfolio_backend.collector.market_session", lambda at=None: False)
    assert effective_quote_poll(cfg) == cfg.off_hours_quote_poll_seconds == 600
    assert effective_news_poll(cfg) == cfg.off_hours_news_poll_seconds == 900


def test_p0_config_defaults():
    cfg = Settings(environment="test")
    assert cfg.data_max_instruments_per_tick == 200
    assert cfg.data_fetch_workers == 2
    assert cfg.off_hours_quote_poll_seconds == 600
    assert cfg.off_hours_news_poll_seconds == 900


def test_batch_normalize_filters_rows_by_symbol():
    at = now()
    rows = [
        {"symbol": "518880", "price": "6.1", "as_of": at.isoformat()},
        {"symbol": "510300", "price": "4.2", "as_of": at.isoformat()},
        {"symbol": "159915", "price": "2.3", "as_of": at.isoformat()},
    ]
    mark = AKShareQuotes().normalize(asset(symbol="510300"), rows, at)
    assert mark.price == Decimal("4.2") and mark.as_of == at
    with pytest.raises(DomainError) as e:
        AKShareQuotes().normalize(asset(symbol="999999"), rows, at)
    assert e.value.code == "quote_missing"


class _FakeExecutor:
    """ProcessPoolExecutor stand-in that never spawns a real process."""

    def __init__(self, future=None, submit_error=None):
        self.future = future
        self.submit_error = submit_error
        self.shutdown_calls = 0

    def submit(self, *args, **kwargs):
        if self.submit_error is not None:
            raise self.submit_error
        return self.future

    def shutdown(self, wait=False, cancel_futures=False):
        self.shutdown_calls += 1


class _FakeFuture:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def result(self, timeout=None):
        if self.error is not None:
            raise self.error
        return self.value

    def cancel(self):
        return False


@pytest.mark.parametrize("submit_error", [OSError("boom"), BrokenProcessPool("gone")])
def test_pool_submit_failure_maps_to_market_source_unavailable(submit_error):
    fetcher = PoolFetcher(workers=1)
    fetcher._create = lambda: _FakeExecutor(submit_error=submit_error)
    with pytest.raises(DomainError) as e:
        fetcher.fetch("etf", "", timeout=5)
    assert e.value.code == "market_source_unavailable" and e.value.status == 503
    fetcher.close()


@pytest.mark.parametrize(
    "error",
    [FutureTimeout(), BrokenProcessPool("gone"), RuntimeError("worker died")],
)
def test_pool_worker_failure_maps_to_market_source_unavailable(error):
    fetcher = PoolFetcher(workers=1)
    fetcher._create = lambda: _FakeExecutor(future=_FakeFuture(error=error))
    with pytest.raises(DomainError) as e:
        fetcher.fetch("etf", "", timeout=5)
    assert e.value.code == "market_source_unavailable" and e.value.status == 503
    fetcher.close()


def test_pool_recovers_once_after_crash():
    makers = iter(
        [
            lambda: _FakeExecutor(submit_error=BrokenProcessPool("gone")),
            lambda: _FakeExecutor(future=_FakeFuture(value=[{"price": "1.2"}])),
        ]
    )
    fetcher = PoolFetcher(workers=1)
    fetcher._create = lambda: next(makers)()
    assert fetcher.fetch("etf", "") == [{"price": "1.2"}]
    fetcher.close()


def test_pool_passes_through_domain_errors():
    fetcher = PoolFetcher(workers=1)
    fetcher._create = lambda: _FakeExecutor(
        future=_FakeFuture(error=DomainError("invalid_quote", "行情数据格式已变化", 503))
    )
    with pytest.raises(DomainError) as e:
        fetcher.fetch("etf", "")
    assert e.value.code == "invalid_quote"
    fetcher.close()


def test_fetch_falls_back_to_subprocess_when_pool_unavailable(monkeypatch):
    def broken_pool(feed, symbol, timeout):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr("portfolio_backend.providers.market_worker.pool_fetch", broken_pool)
    monkeypatch.setattr(
        "portfolio_backend.providers.market.AKShareQuotes._subprocess_fetch",
        lambda feed, symbol, timeout: [{"price": "3.3", "as_of": "2026-11-02T10:00:00+08:00"}],
    )
    rows = AKShareQuotes._fetch("etf", "510300", 50)
    assert rows == [{"price": "3.3", "as_of": "2026-11-02T10:00:00+08:00"}]


def test_fetch_falls_back_to_subprocess_when_pool_broken(monkeypatch):
    from portfolio_backend.providers.market_worker import PoolBroken

    def broken_pool(feed, symbol, timeout):
        raise PoolBroken("market_source_unavailable", "免费行情源暂不可用", 503)

    fallbacks = []
    monkeypatch.setattr("portfolio_backend.providers.market_worker.pool_fetch", broken_pool)
    monkeypatch.setattr(
        "portfolio_backend.providers.market.AKShareQuotes._subprocess_fetch",
        lambda feed, symbol, timeout: fallbacks.append((feed, symbol)) or [{"price": "1.0"}],
    )
    rows = AKShareQuotes._fetch("hk-spot", "", 50)
    assert rows == [{"price": "1.0"}] and fallbacks == [("hk-spot", "")]


def test_fetch_keeps_domain_error_from_pool(monkeypatch):
    def broken_pool(feed, symbol, timeout):
        raise DomainError("market_source_unavailable", "免费行情源暂不可用", 503)

    monkeypatch.setattr("portfolio_backend.providers.market_worker.pool_fetch", broken_pool)
    with pytest.raises(DomainError) as e:
        AKShareQuotes._fetch("etf", "510300")
    assert e.value.code == "market_source_unavailable"
