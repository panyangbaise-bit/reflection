"""AKShare adapter: historical closes are never labelled realtime."""

import contextlib
import io
import json
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


def collect(ak, name, args):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    market, ticker = args.get("market", "CN"), args.get("ticker", "")
    asset = args.get("asset_class", "stock")
    if name == "get_kline":
        if market == "CN" and asset == "fund":
            frame = ak.fund_open_fund_info_em(symbol=ticker, indicator="单位净值走势", period="1年")
            rows = (
                frame.sort_values("净值日期")[["净值日期", "单位净值"]]
                .rename(columns={"净值日期": "date", "单位净值": "close"})
                .to_dict("records")
            )
            points = []
            for row in rows:
                day = datetime.fromisoformat(str(row["date"])).date()
                value = Decimal(str(row["close"]))
                if not value.is_finite() or value <= 0 or day > today:
                    raise ValueError("invalid_nav")
                if day >= today - timedelta(days=args["days"]):
                    points.append({"date": day.isoformat(), "close": str(value)})
            return {"ticker": ticker, "market": market, "kind": "historical_nav", "points": points}
        params = dict(
            symbol=ticker,
            period="daily",
            start_date=(today - timedelta(days=args["days"])).strftime("%Y%m%d"),
            end_date=today.strftime("%Y%m%d"),
            adjust="",
        )
        if not (
            (market == "HK" and asset == "stock")
            or (market == "CN" and asset in {"stock", "etf", "listed_fund"})
        ):
            return {"error": "unsupported_market_or_asset"}
        source = "sina"
        try:
            if market == "HK":
                if not re.fullmatch(r"\d{5}", ticker):
                    return {"error": "invalid_provider_symbol"}
                frame = ak.stock_hk_daily(symbol=ticker, adjust="")
            else:
                if not re.fullmatch(r"\d{6}", ticker):
                    return {"error": "invalid_provider_symbol"}
                symbol = args.get("provider_symbol") or ""
                if not re.fullmatch(r"(?:sh|sz|bj)" + ticker, symbol):
                    prefix = (
                        "bj"
                        if ticker.startswith(("4", "8", "92"))
                        else ("sh" if ticker.startswith(("5", "6", "9")) else "sz")
                    )
                    symbol = prefix + ticker
                if asset == "stock":
                    frame = ak.stock_zh_a_daily(
                        symbol=symbol,
                        start_date=params["start_date"],
                        end_date=params["end_date"],
                        adjust="",
                    )
                else:
                    frame = ak.fund_etf_hist_sina(symbol=symbol)
            if frame.empty:
                raise ValueError("empty_sina_history")
        except Exception:
            source = "eastmoney"
            frame = (
                ak.stock_hk_hist
                if market == "HK"
                else (ak.stock_zh_a_hist if asset == "stock" else ak.fund_etf_hist_em)
            )(**params)
        rows = (
            frame.rename(
                columns={
                    "日期": "date",
                    "开盘": "open",
                    "最高": "high",
                    "最低": "low",
                    "收盘": "close",
                    "成交量": "volume",
                }
            )[["date", "open", "high", "low", "close", "volume"]]
            .tail(366)
            .to_dict("records")
        )
        rows = [
            r
            for r in rows
            if datetime.fromisoformat(str(r["date"])).date() >= today - timedelta(days=args["days"])
        ]
        corrected_rounding = False
        for r in rows:
            prices = {}
            for field in ("open", "high", "low", "close"):
                value = Decimal(str(r[field]))
                if not value.is_finite() or value <= 0:
                    raise ValueError("invalid_price")
                prices[field] = value
            # Sina sometimes returns float noise such as low=393.8, close=393.79999.
            # Clamp only sub-ppm discrepancies; reject materially inconsistent data.
            tolerance = max(prices["high"] * Decimal("0.0000001"), Decimal("0.00000001"))
            lower, upper = (
                min(prices["open"], prices["close"]),
                max(prices["open"], prices["close"]),
            )
            if prices["low"] > lower:
                if prices["low"] - lower > tolerance:
                    raise ValueError("invalid_ohlc")
                r["low"], corrected_rounding = str(lower), True
            if prices["high"] < upper:
                if upper - prices["high"] > tolerance:
                    raise ValueError("invalid_ohlc")
                r["high"], corrected_rounding = str(upper), True
            if datetime.fromisoformat(str(r["date"])).date() > today:
                raise ValueError("future_price")
        return {
            "ticker": ticker,
            "market": market,
            "kind": "historical_daily",
            "adjust": "none",
            "volume_unit": "share" if source == "sina" or market == "HK" else "lot",
            "source": source,
            "rounding_corrected": corrected_rounding,
            "points": [{k: str(v) for k, v in r.items()} for r in rows],
        }
    if name == "get_financials" and market == "CN" and asset == "stock":
        frame = ak.stock_financial_abstract_ths(symbol=ticker, indicator="按报告期")
    elif name == "get_financials" and market == "HK" and asset == "stock":
        frame = ak.stock_financial_hk_report_em(stock=ticker, symbol="利润表", indicator="年度")
    elif name == "get_fund_info" and market == "CN" and asset in {"fund", "etf"}:
        frame = ak.fund_overview_em(symbol=ticker)
    elif name == "get_market_snapshot" and market == "CN":
        frame = ak.stock_zh_index_daily(symbol="sh000001").tail(2)
    elif name == "get_market_snapshot" and market == "HK":
        frame = ak.stock_hk_index_daily_sina(symbol="HSI").tail(2)
    else:
        return {"error": "unsupported_market_or_asset"}
    return {
        "kind": "historical_reference",
        "rows": [
            {str(k): None if str(v) in {"nan", "NaT", "None"} else str(v) for k, v in r.items()}
            for r in frame.tail(20).to_dict("records")
        ],
    }


def main():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import akshare as ak

        data = collect(ak, sys.argv[1], json.loads(sys.argv[2]))
    from portfolio_backend.db import now

    print(
        json.dumps(
            data | {"provider": "akshare:research", "fetched_at": now().isoformat()},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
