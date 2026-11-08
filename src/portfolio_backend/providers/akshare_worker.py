"""Process-isolated optional AKShare dependency. Not imported by the mobile API.

Entry points:
- CLI: ``python -m portfolio_backend.providers.akshare_worker <feed> <symbol>``
  (one-shot subprocess, kept for rollback and manual probing);
- ``fetch_rows``: the same logic as a plain function, used by the resident
  fetch pool so each long-lived worker imports akshare exactly once.
"""

import contextlib
import io
import json
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# Imported once per process, lazily: the collector/API parent must never pay for it.
_AKSHARE = None


def _ak():
    global _AKSHARE
    if _AKSHARE is None:
        # Library progress/stdout must not corrupt the machine-readable result.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import akshare as ak
        _AKSHARE = ak
    return _AKSHARE


def warmup():
    """Import akshare inside the calling process (resident fetch workers only)."""
    _ak()


def fetch_rows(feed, symbol):
    """Return JSON-friendly dict rows for one feed. ``symbol`` is ignored by full-table feeds."""
    # Upstream progress bars must never reach the worker's stdio.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return _fetch_rows(_ak(), feed, symbol)


def _fetch_rows(ak, feed, symbol):
    if feed == "sina-minute":
        frame = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        result = (
            frame.tail(1)[["day", "close"]]
            .rename(columns={"day": "as_of", "close": "price"})
            .to_dict("records")
        )
    elif feed == "cn-minute":
        frame = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
        result = (
            frame.tail(1)[["时间", "收盘"]]
            .rename(columns={"时间": "as_of", "收盘": "price"})
            .to_dict("records")
        )
    elif feed == "etf":
        frame = ak.fund_etf_spot_em()
        result = (
            frame[["代码", "最新价", "更新时间"]]
            .rename(columns={"代码": "symbol", "最新价": "price", "更新时间": "as_of"})
            .to_dict("records")
        )
    elif feed == "hk-minute":
        frame = ak.stock_hk_hist_min_em(symbol=symbol, period="1", adjust="")
        result = (
            frame.tail(1)[["时间", "收盘"]]
            .rename(columns={"时间": "as_of", "收盘": "price"})
            .to_dict("records")
        )
    elif feed == "hk-spot":
        frame = ak.stock_hk_spot()
        codes = frame["代码"].astype(str).str.replace(r"\.0$", "", regex=True)
        frame["代码"] = codes.str.zfill(5)
        result = (
            frame[["代码", "最新价", "日期时间"]]
            .rename(columns={"代码": "symbol", "最新价": "price", "日期时间": "as_of"})
            .to_dict("records")
        )
    elif feed == "us-minute":
        frame = ak.stock_us_hist_min_em(symbol=symbol)
        result = (
            frame.tail(1)[["时间", "收盘"]]
            .rename(columns={"时间": "as_of", "收盘": "price"})
            .to_dict("records")
        )
    elif feed == "us-daily":
        frame = ak.stock_us_daily(symbol=symbol, adjust="")
        result = []
        current = datetime.now(ZoneInfo("America/New_York"))
        for _, row in frame.tail(3).iterrows():
            stamp = datetime.combine(row["date"], time(16), ZoneInfo("America/New_York"))
            if stamp <= current:
                result.append({"as_of": stamp.isoformat(), "price": row["close"]})
    elif feed == "fund-nav":
        frame = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势", period="1月")
        result = (
            frame.sort_values("净值日期")
            .tail(2)[["净值日期", "单位净值"]]
            .rename(columns={"净值日期": "as_of", "单位净值": "price"})
            .to_dict("records")
        )
    elif feed == "fx":
        # PBOC central parity (央行中间价) for USD/HKD; per-currency history call.
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        result = []
        for currency, name in (("USD", "美元"), ("HKD", "港币")):
            try:
                frame = ak.currency_boc_sina(symbol=name, start_date=start, end_date=today)
                frame = frame[frame["央行中间价"].notna()]
                if frame.empty:
                    continue
                row = frame.sort_values("日期").tail(1).iloc[0]
                result.append(
                    {"currency": currency, "rate": row["央行中间价"], "as_of": str(row["日期"])}
                )
            except Exception:
                continue
    else:
        raise ValueError("unsupported_feed")
    return [{k: str(v) for k, v in row.items()} for row in result]


def main():
    feed, symbol = sys.argv[1:3]
    print(json.dumps(fetch_rows(feed, symbol), ensure_ascii=False))


if __name__ == "__main__":
    main()
