"""Strict schemas: user identity and arbitrary SQL/URLs are never model parameters."""

from typing import Literal
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Args):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=30, ge=1, le=100)


class Ledger(Page):
    ticker: str | None = Field(default=None, max_length=32)
    market: Literal["CN", "HK", "US"] | None = None
    account_id: UUID | None = None


class Instrument(Args):
    ticker: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.:-]+$")
    market: Literal["CN", "HK", "US"] = "CN"


class Kline(Instrument):
    days: int = Field(default=90, ge=5, le=365)


class Market(Args):
    market: Literal["CN", "HK"] = "CN"


class Watch(Instrument):
    priority: int = Field(default=3, ge=1, le=5)
    target_price: str | None = Field(default=None, pattern=r"^\d{1,12}(\.\d{1,12})?$")
    note: str = Field(default="", max_length=1000)


class Search(Page):
    query: str = Field(default="", max_length=200)


class WebSearch(Args):
    query: str = Field(min_length=1, max_length=128)


class Read(Args):
    evidence_id: UUID
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=4000, ge=1, le=12000)


class Annotation(Args):
    index: int = Field(ge=0, le=365)
    text: str = Field(min_length=1, max_length=200)


class Trendline(Args):
    start_index: int = Field(ge=0, le=365)
    end_index: int = Field(ge=0, le=365)
    text: str = Field(min_length=1, max_length=200)


class Chart(Args):
    source_tool_call_id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="历史行情", max_length=100)
    annotations: list[Annotation] = Field(default_factory=list, max_length=20)
    trendlines: list[Trendline] = Field(default_factory=list, max_length=10)


SPECS = {
    "search_web": (
        WebSearch,
        "使用 DeepSeek 搜索公开网页。搜索词将发送给搜索服务；只检索公开资料，不传入账户数量、成本或其他私人信息。",
    ),
    "query_portfolio_ledger": (Ledger, "分页查询当前用户持仓账本，可包含已清仓；成本不是现价。"),
    "get_portfolio": (Page, "分页查询当前组合及估值缺失情况。"),
    "get_return_summary": (Args, "收益汇总；缺少历史价格或汇率时返回 null，原币分开。"),
    "get_holding": (Instrument, "按代码和市场查询本人持仓，可能分布在多个账户。"),
    "get_trade_history": (Ledger, "分页查询成交和初始快照，包含费用及时间。"),
    "get_price": (Instrument, "取得缓存报价与真实时点、类型及有效状态。"),
    "get_kline": (
        Kline,
        "取得目录内 CN/HK 股票/ETF 历史日线或 CN 基金历史净值，返回本轮 call_id 供图表引用。",
    ),
    "get_financials": (Instrument, "查询公开财报摘要及数据缺失状态。"),
    "get_fund_info": (Instrument, "查询公开基金/ETF资料，不能当作实时行情。"),
    "get_market_snapshot": (Market, "获取市场指数最近历史收盘概览。"),
    "search_ticker_news": (
        Instrument,
        "查询已采集且未过期的标的新闻，返回来源 ID；无结果不代表没有新闻。",
    ),
    "get_market_headlines": (Page, "分页查询已采集的近期市场新闻。"),
    "get_watchlist": (Page, "分页查看本人监察表。"),
    "add_to_watchlist": (Watch, "仅当用户明确要求时添加/更新监察表，不下单。"),
    "remove_from_watchlist": (Instrument, "仅当用户明确要求时删除监察表标的。"),
    "search_memory": (Search, "关键词检索本人历史对话、报告、纪律和操作证据，非向量检索。"),
    "read_memory": (Read, "凭证据 ID 分页读取原文及版本/时间，不能跨用户。"),
    "get_instrument_timeline": (
        Instrument,
        "查询标的相关历史证据时间线，结果可用 read_memory 回查。",
    ),
    "annotate_kline": (Chart, "仅引用本轮成功 get_kline 的数据生成图表载荷，不能自行传入价格。"),
}
WRITES = {"add_to_watchlist", "remove_from_watchlist"}


def build_tools(executor, read_only=False):
    tools = []
    for name, (schema, description) in SPECS.items():
        if read_only and name in WRITES:
            continue

        def invoke(_name=name, **kwargs):
            return executor(_name, kwargs)

        tools.append(
            StructuredTool.from_function(
                invoke, name=name, description=description, args_schema=schema
            )
        )
    return tools
