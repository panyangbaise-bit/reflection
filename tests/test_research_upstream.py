from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from portfolio_backend.db import now
from portfolio_backend.providers.research_worker import collect


class Frame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def rename(self, columns):
        return Frame(
            [{columns.get(key, key): value for key, value in row.items()} for row in self.rows]
        )

    def __getitem__(self, columns):
        return Frame([{column: row[column] for column in columns} for row in self.rows])

    def tail(self, count):
        return Frame(self.rows[-count:])

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


@pytest.mark.parametrize(
    "market,asset,ticker,provider_symbol,method,expected_symbol",
    [
        ("CN", "stock", "600519", "sh600519", "stock_zh_a_daily", "sh600519"),
        ("CN", "etf", "510300", "sh510300", "fund_etf_hist_sina", "sh510300"),
        ("HK", "stock", "00700", "00700", "stock_hk_daily", "00700"),
    ],
)
def test_sina_daily_history_preserves_provider_and_filters_time(
    market, asset, ticker, provider_symbol, method, expected_symbol
):
    today = now().astimezone(ZoneInfo("Asia/Shanghai")).date()
    rows = [
        {
            "date": str(today - timedelta(days=31)),
            "open": "2",
            "high": "3",
            "low": "1",
            "close": "2",
            "volume": "100",
        },
        {
            "date": str(today - timedelta(days=1)),
            "open": "2",
            "high": "3",
            "low": "1",
            "close": "2.5",
            "volume": "100",
        },
    ]

    class Fake:
        def __getattr__(self, attr):
            assert attr == method

            def fetch(**kwargs):
                assert kwargs["symbol"] == expected_symbol
                return Frame(rows)

            return fetch

    result = collect(
        Fake(),
        "get_kline",
        {
            "ticker": ticker,
            "market": market,
            "asset_class": asset,
            "provider_symbol": provider_symbol,
            "days": 30,
        },
    )
    assert result["source"] == "sina" and result["volume_unit"] == "share"
    assert len(result["points"]) == 1 and result["points"][0]["close"] == "2.5"


def test_history_falls_back_to_eastmoney_if_sina_unavailable():
    today = now().astimezone(ZoneInfo("Asia/Shanghai")).date()

    class Fake:
        def stock_zh_a_daily(self, **kwargs):
            raise OSError("unavailable")

        def stock_zh_a_hist(self, **kwargs):
            assert kwargs["symbol"] == "600519"
            return Frame(
                [
                    {
                        "日期": str(today),
                        "开盘": "2",
                        "最高": "3",
                        "最低": "1",
                        "收盘": "2.5",
                        "成交量": "100",
                    }
                ]
            )

    result = collect(
        Fake(),
        "get_kline",
        {"ticker": "600519", "market": "CN", "asset_class": "stock", "days": 30},
    )
    assert result["source"] == "eastmoney" and result["volume_unit"] == "lot"
    assert len(result["points"]) == 1


def test_tiny_upstream_rounding_error_is_corrected_but_real_invalid_prices_fail():
    today = now().astimezone(ZoneInfo("Asia/Shanghai")).date()
    row = {
        "date": str(today),
        "open": "412",
        "high": "413.4",
        "low": "393.8",
        "close": "393.79999",
        "volume": "100",
    }

    class Fake:
        def stock_hk_daily(self, **kwargs):
            return Frame([row])

    args = {"ticker": "00700", "market": "HK", "asset_class": "stock", "days": 30}
    result = collect(Fake(), "get_kline", args)
    assert result["rounding_corrected"] is True
    assert result["points"][0]["low"] == "393.79999"
    row["close"] = "380"
    with pytest.raises(ValueError, match="invalid_ohlc"):
        collect(Fake(), "get_kline", args)
