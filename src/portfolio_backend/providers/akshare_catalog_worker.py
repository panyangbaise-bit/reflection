"""Process-isolated AKShare symbol-list fetch. Never imported by the mobile API."""

import contextlib
import io
import json
import sys

from portfolio_backend.providers.catalog import normalize_catalog


def main():
    kind = sys.argv[1]
    if kind == "us-stock":
        from portfolio_backend.providers.us_directory import fetch_us_directory

        print(json.dumps(fetch_us_directory(), ensure_ascii=False))
        return
    # Progress output may use stderr; stdout must remain JSON only.
    with contextlib.redirect_stdout(io.StringIO()):
        import akshare as ak

        if kind == "cn-stock":
            frame = ak.stock_zh_a_spot()
        elif kind == "cn-etf":
            frame = ak.fund_etf_category_sina(symbol="ETF基金")
        elif kind == "cn-lof":
            frame = ak.fund_etf_category_sina(symbol="LOF基金")
        elif kind == "cn-closed-fund":
            frame = ak.fund_etf_category_sina(symbol="封闭式基金")
        elif kind == "hk-stock":
            frame = ak.stock_hk_spot()
        elif kind == "cn-fund":
            frame = ak.fund_name_em().rename(columns={"基金代码": "代码", "基金简称": "名称"})
        else:
            raise ValueError("unsupported_catalog_kind")
        records = frame.to_dict("records")
    rows = normalize_catalog(kind, records)
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
