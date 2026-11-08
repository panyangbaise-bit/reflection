"""US exchange listings from Nasdaq Trader's two official symbol directories.

Directory identity is independent of quote availability. ETF flags come from
the source; no Eastmoney exchange ID is guessed from a ticker.
"""

import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
FILES = ("nasdaqlisted.txt", "otherlisted.txt")


def parse_directory(payload: str, filename: str) -> list[dict]:
    symbol_key = "Symbol" if filename == FILES[0] else "ACT Symbol"
    reader = csv.DictReader(io.StringIO(payload.lstrip("\ufeff")), delimiter="|")
    if not {symbol_key, "Security Name", "ETF", "Test Issue"}.issubset(reader.fieldnames or []):
        raise ValueError("invalid_us_directory_header")
    if "File Creation Time:" not in payload:
        raise ValueError("incomplete_us_directory")
    result = {}
    for item in reader:
        symbol = (item.get(symbol_key) or "").strip()
        name = (item.get("Security Name") or "").strip()
        if item.get("Test Issue") != "N" or not name:
            continue
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.$^/=-]{0,31}", symbol):
            continue
        asset = "etf" if item.get("ETF") == "Y" else "stock"
        result[symbol, asset] = {
            "symbol": symbol,
            "name": name[:160],
            "market": "US",
            "asset_class": asset,
            "currency": "USD",
            "provider": "akshare:us-daily",
            "provider_symbol": symbol,
            "quote_kind": "close",
            "freshness_seconds": 345600,
        }
    if not result:
        raise ValueError("empty_us_directory")
    return list(result.values())


def fetch_directory(filename: str) -> list[dict]:
    request = Request(BASE + filename, headers={"User-Agent": "PortfolioCatalog/1.0"})
    with urlopen(request, timeout=25) as response:
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError("oversize_us_directory")
    rows = parse_directory(data.decode("utf-8"), filename)
    if len(rows) < 1000:
        raise ValueError("truncated_us_directory")
    return rows


def fetch_us_directory() -> list[dict]:
    # Both files must succeed. Failed refreshes leave the previous DB snapshot intact.
    with ThreadPoolExecutor(max_workers=2) as pool:
        parts = list(pool.map(fetch_directory, FILES))
    unique = {(r["market"], r["symbol"], r["asset_class"]): r for part in parts for r in part}
    return list(unique.values())
