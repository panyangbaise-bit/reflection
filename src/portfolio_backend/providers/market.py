import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from portfolio_backend.errors import DomainError

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")


def market_session(at: datetime | None = None) -> bool:
    """CN market open state: Mon-Fri 09:30-11:30 and 13:00-15:00 Asia/Shanghai.

    Weekday-only by design: the collector throttles off-hours polling without a
    holiday-calendar dependency, so holidays simply poll at the off-hours cadence.
    """
    local = (at or datetime.now(CN_TZ)).astimezone(CN_TZ)
    if local.weekday() >= 5:
        return False
    clock = local.time()
    return time(9, 30) <= clock < time(11, 30) or time(13, 0) <= clock < time(15, 0)


@dataclass(frozen=True)
class MarketMark:
    price: Decimal
    as_of: datetime
    kind: str
    provider: str
    valid_until: datetime
    previous_price: Decimal | None = None
    previous_as_of: datetime | None = None


@dataclass(frozen=True)
class FXMark:
    currency: str
    rate: Decimal
    as_of: datetime
    valid_until: datetime
    provider: str


class AKShareFX:
    """PBOC central parities for CNY conversion. Weekend/holiday safe validity window."""

    provider = "akshare:fx"

    def __init__(self, fetch=None):
        self.fetch = fetch or AKShareQuotes._fetch

    def load(self):
        rows = self.fetch("fx", "all", timeout=80)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DomainError("invalid_quote", "汇率数据格式已变化", 503)
        marks = []
        for row in rows:
            try:
                currency = str(row.get("currency", ""))
                if currency not in {"USD", "HKD"}:
                    continue
                # 央行中间价按每 100 外币报价，换算为每 1 外币兑人民币。
                rate = Decimal(str(row["rate"])) / Decimal(100)
                stamp = datetime.fromisoformat(str(row["as_of"])[:10])
                stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                if not rate.is_finite() or rate <= 0:
                    continue
                marks.append(
                    FXMark(
                        currency,
                        rate,
                        stamp,
                        stamp + timedelta(days=3),
                        self.provider,
                    )
                )
            except (InvalidOperation, ValueError, KeyError, TypeError):
                continue
        return marks


class AKShareQuotes:
    """Use source timestamps. Fetch time must never rejuvenate an old/undated quote."""

    providers = {
        "akshare:cn-minute",
        "akshare:etf",
        "akshare:hk-minute",
        "akshare:hk-spot",
        "akshare:us-minute",
        "akshare:us-daily",
        "akshare:sina-minute",
        "akshare:fund-nav",
    }

    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(feed, symbol, timeout=90):
        # Resident pool: akshare is imported once per long-lived worker instead
        # of once per call. When the pool itself cannot run (broken spawn, OOM
        # kills), fall back to the one-shot subprocess so quotes keep flowing.
        try:
            from portfolio_backend.providers.market_worker import PoolBroken, pool_fetch

            return pool_fetch(feed, symbol, timeout)
        except PoolBroken as exc:
            logger.warning("fetch_pool_broken falling_back_to_subprocess error=%s", exc)
            return AKShareQuotes._subprocess_fetch(feed, symbol, timeout)
        except DomainError:
            raise
        except Exception as exc:
            logger.warning("fetch_pool_unavailable falling_back_to_subprocess error=%s", exc)
            return AKShareQuotes._subprocess_fetch(feed, symbol, timeout)

    @staticmethod
    def _subprocess_fetch(feed, symbol, timeout=90):
        """One-shot subprocess fallback; kept for rollback and offline probing."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "portfolio_backend.providers.akshare_worker", feed, symbol],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise DomainError("market_source_unavailable", "免费行情源暂不可用", 503) from exc

    def cache_key(self, instrument):
        # One upstream list serves every ETF/HK symbol; never key it by one symbol.
        if instrument.provider in {"akshare:etf", "akshare:hk-spot"}:
            return instrument.provider
        return instrument.provider + ":" + (instrument.provider_symbol or instrument.symbol)

    def load(self, instrument):
        if instrument.provider not in self.providers:
            raise DomainError("unsupported_provider", "行情接口尚不支持", 422)
        feed = instrument.provider.split(":")[1]
        symbol = instrument.provider_symbol or instrument.symbol
        valid = (
            (
                feed == "sina-minute"
                and instrument.market == "CN"
                and instrument.asset_class in {"stock", "etf", "listed_fund", "gold"}
                and re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol)
            )
            or (
                feed == "cn-minute"
                and instrument.market == "CN"
                and instrument.asset_class == "stock"
                and re.fullmatch(r"\d{6}", symbol)
            )
            or (
                feed == "etf"
                and instrument.market == "CN"
                and instrument.asset_class in {"etf", "gold"}
            )
            or (
                feed == "hk-minute" and instrument.market == "HK" and re.fullmatch(r"\d{5}", symbol)
            )
            or (feed == "hk-spot" and instrument.market == "HK" and re.fullmatch(r"\d{5}", symbol))
            or (
                feed == "fund-nav"
                and instrument.market == "CN"
                and instrument.asset_class == "fund"
                and re.fullmatch(r"\d{6}", symbol)
            )
            or (
                feed == "us-minute"
                and instrument.market == "US"
                and re.fullmatch(r"\d{3}\.[A-Z0-9.-]{1,20}", symbol)
            )
            or (
                feed == "us-daily"
                and instrument.market == "US"
                and re.fullmatch(r"[A-Z0-9][A-Z0-9.$^/=-]{0,31}", symbol)
            )
        )
        if not valid:
            raise DomainError("invalid_provider_symbol", "行情命名空间不匹配", 422)
        if feed == "hk-spot":
            return self.fetch(feed, "")
        return self.fetch(feed, symbol)

    def normalize(self, instrument, rows, at):
        feed = instrument.provider.split(":")[1]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DomainError("invalid_quote", "行情数据格式已变化", 503)

        def same_symbol(row):
            value = str(row.get("symbol", ""))
            return value.zfill(5) if feed == "hk-spot" else value

        rows = [
            r for r in rows if feed not in {"etf", "hk-spot"} or same_symbol(r) == instrument.symbol
        ]
        if not rows:
            raise DomainError("quote_missing", "未取得标的价格", 503)
        row = rows[-1]
        try:
            price = Decimal(str(row["price"]))
            stamp = datetime.fromisoformat(str(row["as_of"]).replace("/", "-"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(
                    tzinfo=ZoneInfo(
                        "America/New_York" if instrument.market == "US" else "Asia/Shanghai"
                    )
                )
            if not price.is_finite() or not (0 < price <= Decimal("1e12")) or stamp > at:
                raise ValueError("invalid_mark")
        except (InvalidOperation, ValueError, KeyError, TypeError) as exc:
            raise DomainError("invalid_quote", "行情价格或来源时间不完整", 503) from exc
        # Documented delayed HK/US feeds are never promoted to realtime by polling faster.
        kind = (
            "close"
            if feed == "us-daily"
            else "nav"
            if feed == "fund-nav"
            else ("delayed" if feed in {"hk-minute", "hk-spot", "us-minute"} else "realtime")
        )
        previous_price = previous_as_of = None
        if feed == "fund-nav" and len(rows) >= 2:
            try:
                prior = rows[-2]
                previous_price = Decimal(str(prior["price"]))
                previous_as_of = datetime.fromisoformat(str(prior["as_of"]).replace("/", "-"))
                if previous_as_of.tzinfo is None:
                    previous_as_of = previous_as_of.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                if not previous_price.is_finite() or previous_price <= 0 or previous_as_of >= stamp:
                    raise ValueError("invalid_prior_nav")
            except (InvalidOperation, ValueError, KeyError, TypeError):
                previous_price = previous_as_of = None
        return MarketMark(
            price,
            stamp,
            kind,
            instrument.provider,
            stamp + timedelta(seconds=instrument.freshness_seconds),
            previous_price,
            previous_as_of,
        )
