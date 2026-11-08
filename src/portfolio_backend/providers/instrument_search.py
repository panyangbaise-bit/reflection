"""Bounded, optional symbol discovery; never used as a price source."""

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from portfolio_backend.providers.catalog import normalize_catalog


def normalize_suggestions(payload):
    items = payload.get("QuotationCodeTable", {}).get("Data") or []
    if not isinstance(items, list):
        raise ValueError("invalid_search_response")
    result = []
    for item in items[:30]:
        if not isinstance(item, dict):
            continue
        code, name = str(item.get("Code", "")), str(item.get("Name", ""))
        kind, market_id = item.get("Classify"), str(item.get("MktNum", ""))
        source = {"代码": code, "名称": name}
        rows = []
        if kind == "UsStock" and market_id in {"105", "106", "107"}:
            rows = normalize_catalog("us-stock", [source | {"代码": market_id + "." + code}])
            if rows:
                rows[0].update(
                    provider="akshare:us-daily",
                    provider_symbol=code,
                    quote_kind="close",
                    freshness_seconds=345600,
                )
                if str(item.get("TypeUS")) == "5":
                    rows[0]["asset_class"] = "etf"
        elif kind == "HK" and market_id == "116":
            rows = normalize_catalog("hk-stock", [source])
            if rows and ("ETF" in name.upper() or str(item.get("SecurityType")) == "27"):
                rows[0]["asset_class"] = "etf"
        elif kind == "OTCFUND" and market_id == "150":
            rows = normalize_catalog("cn-fund", [source])
        elif kind in {"AStock", "Fund"} and re.fullmatch(r"\d{6}", code):
            prefix = {"1": "sh", "0": "sz"}.get(market_id)
            if kind == "AStock" and code.startswith(("4", "8", "92")):
                prefix = "bj"
            if prefix:
                category = (
                    "cn-stock"
                    if kind == "AStock"
                    else ("cn-etf" if "ETF" in name.upper() else "cn-lof")
                )
                rows = normalize_catalog(category, [source | {"代码": prefix + code}])
        for row in rows:
            row["search_aliases"] = (name + " " + str(item.get("PinYin") or ""))[:500]
            result.append(row)
    return result


def fetch_suggestions(query):
    params = urlencode({"input": query, "type": "14", "count": 20})
    request = Request(
        "https://searchapi.eastmoney.com/api/suggest/get?" + params,
        headers={"User-Agent": "PortfolioCatalog/1.0"},
    )
    with urlopen(request, timeout=2.5) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("oversize_search_response")
    return normalize_suggestions(json.loads(data))
