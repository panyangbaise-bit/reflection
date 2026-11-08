"""Normalize AKShare symbol lists into the shared instrument catalog.

The lists are public symbol/name directories. They do not prove that a quote is
current or that display rights are confirmed; both remain separate checks.
"""

import math
import re
from typing import Any

CN_CODE = re.compile(r"^(sh|sz|bj)(\d{6})$")
US_CODE = re.compile(r"^(105|106|107)\.([A-Z0-9][A-Z0-9.-]{0,19})$")
HK_CODE = re.compile(r"^(\d{1,5})$")

KINDS = {"cn-stock", "cn-etf", "cn-lof", "cn-closed-fund", "cn-fund", "hk-stock", "us-stock"}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def normalize_catalog(kind: str, records: list[dict]) -> list[dict]:
    if kind not in KINDS:
        raise ValueError("unsupported_catalog_kind")
    rows: dict[tuple[str, str, str], dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = next(
            (
                value
                for value in (
                    text(record.get("名称")),
                    text(record.get("中文名称")),
                    text(record.get("英文名称")),
                )
                if value
            ),
            "",
        )
        code = text(record.get("代码"))
        if code.endswith(".0") and code[:-2].isdigit():
            code = code[:-2]
        if not name or not code:
            continue
        if kind == "cn-fund":
            if not re.fullmatch(r"\d{6}", code):
                continue
            symbol = provider_symbol = code
            market, currency, asset_class = "CN", "CNY", "fund"
            provider, quote_kind, freshness = "akshare:fund-nav", "nav", 604800
        elif kind in {"cn-stock", "cn-etf", "cn-lof", "cn-closed-fund"}:
            match = CN_CODE.fullmatch(code.lower())
            if not match:
                continue
            symbol, provider_symbol = match.group(2), match.group(0)
            market, currency = "CN", "CNY"
            asset_class = {"cn-stock": "stock", "cn-etf": "etf"}.get(kind, "listed_fund")
            provider, quote_kind, freshness = "akshare:sina-minute", "realtime", 120
        elif kind == "us-stock":
            match = US_CODE.fullmatch(code.upper())
            if not match:
                continue
            symbol, provider_symbol = match.group(2), match.group(0)
            market, currency, asset_class = "US", "USD", "stock"
            provider, quote_kind, freshness = "akshare:us-minute", "delayed", 1800
        else:
            match = HK_CODE.fullmatch(code)
            if not match:
                continue
            symbol = match.group(1).zfill(5)
            provider_symbol = symbol
            market, currency = "HK", "HKD"
            asset_class = "stock"
            provider, quote_kind, freshness = "akshare:hk-spot", "delayed", 1800
            if text(record.get("英文名称")) and text(record.get("中文名称")):
                # Keep both names searchable without a second alias column.
                name = name + " " + text(record.get("英文名称"))
        key = (market, symbol, asset_class)
        if key in rows:
            continue
        rows[key] = {
            "symbol": symbol,
            "name": name[:160],
            "market": market,
            "asset_class": asset_class,
            "currency": currency,
            "provider": provider,
            "provider_symbol": provider_symbol,
            "quote_kind": quote_kind,
            "freshness_seconds": freshness,
        }
    return list(rows.values())


def score(row: dict, query: str) -> int | None:
    q = query.strip()
    if not q:
        return None
    upper = q.upper()
    symbol = str(row["symbol"]).upper()
    provider_symbol = str(row.get("provider_symbol") or "").upper()
    name = str(row["name"])
    if symbol == upper or provider_symbol == upper:
        return 0
    if symbol.startswith(upper) or provider_symbol.startswith(upper):
        return 1
    if name == q:
        return 2
    if name.lower().startswith(q.lower()):
        return 3
    if q.lower() in name.lower() or upper in provider_symbol:
        return 4
    return None


def match_catalog(rows: list[dict], query: str, limit: int = 30) -> list[dict]:
    """Pure ranking helper used by tests and any future local list cache."""
    scored = []
    seen = set()
    for row in rows:
        value = score(row, query)
        key = (row["market"], row["symbol"], row["asset_class"])
        if value is None or key in seen:
            continue
        seen.add(key)
        scored.append((value, row["market"], row["symbol"], row))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:limit]]
