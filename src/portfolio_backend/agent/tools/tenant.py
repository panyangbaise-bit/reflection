import hashlib
import json
from decimal import Decimal

from sqlalchemy import String, cast, delete, exists, select, text
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.agent.citations import safe_url
from portfolio_backend.db import now
from portfolio_backend.errors import require
from portfolio_backend.models import (
    Account,
    Instrument,
    InstrumentNews,
    LatestQuote,
    LedgerEvent,
    MemoryEvidence,
    Message,
    Position,
    Report,
    TradingDiscipline,
    WatchlistItem,
)
from portfolio_backend.services import portfolio
from portfolio_backend.services.common import lock_portfolio, owned


def page(items, offset, limit):
    return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}


def instrument(session, args):
    row = session.scalar(
        select(Instrument).where(
            Instrument.symbol == args["ticker"], Instrument.market == args.get("market", "CN")
        )
    )
    require(row is not None, "not_found", "标的不在目录中", 404)
    require(row.enabled and row.commercial_display, "instrument_unavailable", "标的不可用", 422)
    return row


def ledger(session, uid, name, args, cfg, report_date=None):
    from portfolio_backend.worker import financial_context

    offset, limit = args.get("offset", 0), args.get("limit", 30)
    if name in {"get_portfolio", "get_return_summary", "get_holding"}:
        data = financial_context(portfolio.snapshot(session, uid, report_date=report_date), cfg)
        if name == "get_return_summary":
            return {k: v for k, v in data.items() if k != "positions"}
        items = data.pop("positions")
        if name == "get_holding":
            items = [
                p for p in items if p["symbol"] == args["ticker"] and p["market"] == args["market"]
            ]
        return data | page(items[offset : offset + limit + 1], offset, limit)
    if args.get("account_id"):
        owned(session, Account, args["account_id"], uid)
    q = (
        select(Position, Instrument)
        .join(Instrument, Position.instrument_id == Instrument.id)
        .where(Position.user_id == uid)
    )
    if args.get("ticker"):
        q = q.where(Instrument.symbol == args["ticker"])
    if args.get("market"):
        q = q.where(Instrument.market == args["market"])
    if args.get("account_id"):
        q = q.where(Position.account_id == args["account_id"])
    if name == "get_trade_history":
        q = (
            q.add_columns(LedgerEvent)
            .join(LedgerEvent, LedgerEvent.position_id == Position.id)
            .where(LedgerEvent.user_id == uid)
            .order_by(LedgerEvent.occurred_at.desc(), LedgerEvent.id.desc())
        )
        rows = session.execute(q.offset(offset).limit(limit + 1)).all()
        items = [
            {
                "symbol": i.symbol,
                "market": i.market,
                "currency": i.currency,
                "kind": e.kind,
                "quantity": str(e.quantity),
                "price": str(e.price),
                "fee": str(e.fee),
                "occurred_at": e.occurred_at.isoformat(),
                "source_id": f"ledger:{e.id}",
            }
            for p, i, e in rows
        ]
    else:
        rows = session.execute(q.order_by(Position.id).offset(offset).limit(limit + 1)).all()
        items = [
            portfolio.position_view(p)
            | {"symbol": i.symbol, "market": i.market, "currency": i.currency}
            for p, i in rows
        ]
    return page(items, offset, limit)


def watchlist(session, uid, name, args):
    if name == "get_watchlist":
        offset, limit = args["offset"], args["limit"]
        rows = session.execute(
            select(WatchlistItem, Instrument)
            .join(Instrument)
            .where(WatchlistItem.user_id == uid)
            .order_by(WatchlistItem.priority, WatchlistItem.id)
            .offset(offset)
            .limit(limit + 1)
        ).all()
        return page(
            [
                {
                    "instrument_id": str(i.id),
                    "ticker": i.symbol,
                    "name": i.name,
                    "market": i.market,
                    "priority": w.priority,
                    "target_price": portfolio.decimal(w.target_price),
                    "note": w.note,
                }
                for w, i in rows
            ],
            offset,
            limit,
        )
    lock_portfolio(session, uid)
    i = instrument(session, args)
    if name == "remove_from_watchlist":
        count = session.execute(
            delete(WatchlistItem).where(
                WatchlistItem.user_id == uid, WatchlistItem.instrument_id == i.id
            )
        ).rowcount
        return {"removed": bool(count)}
    target = Decimal(args["target_price"]) if args.get("target_price") else None
    require(target is None or target > 0, "invalid_target", "目标价须大于 0", 422)
    values = {"priority": args["priority"], "target_price": target, "note": args["note"]}
    session.execute(
        insert(WatchlistItem)
        .values(user_id=uid, instrument_id=i.id, **values)
        .on_conflict_do_update(index_elements=["user_id", "instrument_id"], set_=values)
    )
    return {"saved": True, "ticker": i.symbol, "market": i.market}


def news(session, name, args, cfg):
    from portfolio_backend.models import NewsItem

    # Display permission is insufficient for disclosure to an AI supplier.
    q = select(NewsItem).where(
        NewsItem.expires_at > now(), NewsItem.provider.in_(cfg.ai_market_data_providers)
    )
    if name == "search_ticker_news":
        i = instrument(session, args)
        q = q.join(InstrumentNews, InstrumentNews.news_id == NewsItem.id).where(
            InstrumentNews.instrument_id == i.id
        )
    offset, limit = args.get("offset", 0), args.get("limit", 20)
    rows = session.scalars(
        q.order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).offset(offset).limit(limit + 1)
    ).all()
    return page(
        [
            {
                "source_id": f"news:{r.id}",
                "title": r.title,
                "excerpt": r.excerpt,
                "url": r.url if safe_url(r.url) else None,
                "as_of": r.published_at.isoformat(),
                "provider": r.provider,
            }
            for r in rows
        ],
        offset,
        limit,
    ) | {"scope": "collected_unexpired", "empty_is_not_absence": True}


def index_memory_batch(session, uid, cfg, limit=100):
    # Immutable sources are indexed once, in bounded resumable transactions.
    # Mutable discipline is keyed by content version; prior versions remain evidence.
    from portfolio_backend.worker import financial_context

    def unindexed(model, kind):
        return ~exists(
            select(MemoryEvidence.id).where(
                MemoryEvidence.user_id == uid,
                MemoryEvidence.source_type == kind,
                MemoryEvidence.source_id == cast(model.id, String),
            )
        )

    records = []
    more = False
    messages = list(
        session.scalars(
            select(Message)
            .where(Message.user_id == uid, unindexed(Message, "message"))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
    )
    more |= len(messages) == limit
    for r in messages:
        records.append(("message", str(r.id), r.content, r.created_at))
    reports = list(
        session.scalars(
            select(Report)
            .where(Report.user_id == uid, unindexed(Report, "report"))
            .order_by(Report.created_at.desc(), Report.id.desc())
            .limit(limit)
        )
    )
    more |= len(reports) == limit
    for r in reports:
        content = json.dumps(
            {
                "title": r.title,
                "snapshot": financial_context(
                    r.snapshot | {"market_data_providers": ["memory_restricted"]},
                    cfg.model_copy(update={"ai_market_data_providers": set()}),
                ),
            },
            ensure_ascii=False,
        )
        records.append(("report", str(r.id), content, r.created_at))
    d = session.get(TradingDiscipline, uid)
    if d:
        records.append(("discipline", str(uid), d.content, d.updated_at))
    events = list(
        session.scalars(
            select(LedgerEvent)
            .where(LedgerEvent.user_id == uid, unindexed(LedgerEvent, "ledger"))
            .order_by(LedgerEvent.occurred_at.desc(), LedgerEvent.id.desc())
            .limit(limit)
        )
    )
    more |= len(events) == limit
    for r in events:
        p = session.get(Position, r.position_id)
        i = session.get(Instrument, p.instrument_id)
        content = json.dumps(
            {
                "symbol": i.symbol,
                "market": i.market,
                "kind": r.kind,
                "quantity": str(r.quantity),
                "price": str(r.price),
                "fee": str(r.fee),
            },
            ensure_ascii=False,
        )
        records.append(("ledger", str(r.id), content, r.occurred_at))
    if records:
        session.execute(
            insert(MemoryEvidence)
            .values(
                [
                    dict(
                        user_id=uid,
                        source_type=kind,
                        source_id=rid,
                        version=hashlib.sha256(content.encode()).hexdigest(),
                        content=content,
                        as_of=at,
                    )
                    for kind, rid, content, at in records
                ]
            )
            .on_conflict_do_nothing()
        )
    return not more


def memory(session, uid, name, args, cfg, index_complete=True):
    if name == "read_memory":
        r = owned(session, MemoryEvidence, args["evidence_id"], uid)
        start, limit = args["offset"], args["limit"]
        return {
            "source_id": f"memory:{r.id}",
            "content": r.content[start : start + limit],
            "version": r.version,
            "source_type": r.source_type,
            "as_of": r.as_of.isoformat(),
            "next_offset": start + limit if len(r.content) > start + limit else None,
        }
    query = args.get("query", "")
    if name == "get_instrument_timeline":
        i = instrument(session, args)
        query = i.symbol
    q = select(MemoryEvidence).where(MemoryEvidence.user_id == uid)
    if query:
        q = q.where(MemoryEvidence.content.icontains(query, autoescape=True))
    offset, limit = args.get("offset", 0), args.get("limit", 30)
    rows = session.scalars(
        q.order_by(MemoryEvidence.as_of.desc(), MemoryEvidence.id.desc())
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return page(
        [
            {
                "evidence_id": str(r.id),
                "source_id": f"memory:{r.id}",
                "source_type": r.source_type,
                "version": r.version,
                "as_of": r.as_of.isoformat(),
                "excerpt": r.content[:600],
            }
            for r in rows
        ],
        offset,
        limit,
    ) | {
        "retrieval": "keyword",
        "vector_status": "not_configured",
        "index_scope": "all_messages_reports_ledger_and_versioned_discipline",
        "index_complete": index_complete,
        "index_status": "complete" if index_complete else "backfilling_retry_to_continue",
    }


class TenantTools:
    def __init__(
        self, db, uid, cfg, deadline, market_provider=None, user_message="", report_date=None
    ):
        self.db, self.uid, self.cfg, self.deadline = db, uid, cfg, deadline
        self.market_provider = market_provider
        self.user_message = user_message
        # 报告类任务：快照按报告日评估（当日收盘口径），与报告正文保持一致。
        self.report_date = report_date

    def __call__(self, name, args):
        import time

        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "agent_deadline", "已达到本次时间限制", 503)
        if name == "search_web":
            from portfolio_backend.providers.news import DeepSeekWebSearch
            from portfolio_backend.services.public_data import reserve_request

            require(
                self.cfg.ai_disclosure_version != "1",
                "search_disclosure_outdated",
                "联网搜索需要更新 AI 披露版本并重新授权",
                403,
            )
            require(
                self.cfg.deepseek_api_key.get_secret_value(),
                "search_unavailable",
                "联网搜索尚未配置",
                503,
            )
            require(
                reserve_request(self.db, "deepseek-search", self.cfg.search_daily_limit),
                "search_budget_exhausted",
                "今日搜索额度已用完",
                429,
            )
            return DeepSeekWebSearch(self.cfg).search(
                args["query"], timeout=min(remaining, self.cfg.agent_tool_timeout_seconds)
            )
        index_complete = True
        if name in {"search_memory", "get_instrument_timeline"}:
            # Keep each committed batch on timeout so a large first-time index
            # makes progress instead of rolling back and starting over forever.
            budget = time.monotonic() + min(
                5, remaining / 2, self.cfg.agent_tool_timeout_seconds / 2
            )
            index_complete = False
            while time.monotonic() < budget:
                with self.db.tenant(self.uid) as indexing:
                    indexing.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": str(max(1, int((budget - time.monotonic()) * 1000)))},
                    )
                    index_complete = index_memory_batch(indexing, self.uid, self.cfg)
                if index_complete:
                    break
        with self.db.tenant(self.uid) as s:
            s.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {
                    "timeout": str(
                        max(1, int(min(remaining, self.cfg.agent_tool_timeout_seconds) * 1000))
                    )
                },
            )
            if name in {
                "query_portfolio_ledger",
                "get_portfolio",
                "get_return_summary",
                "get_holding",
                "get_trade_history",
            }:
                return ledger(s, self.uid, name, args, self.cfg, report_date=self.report_date)
            if name in {"get_watchlist", "add_to_watchlist", "remove_from_watchlist"}:
                if name != "get_watchlist":
                    from portfolio_backend.agent.access import require_watchlist_intent

                    require_watchlist_intent(name, args, self.user_message)
                return watchlist(s, self.uid, name, args)
            if name in {"search_ticker_news", "get_market_headlines"}:
                return news(s, name, args, self.cfg)
            if name in {"search_memory", "read_memory", "get_instrument_timeline"}:
                return memory(s, self.uid, name, args, self.cfg, index_complete)
            if name == "get_price":
                i = instrument(s, args)
                require(
                    i.provider in self.cfg.ai_market_data_providers,
                    "data_rights_unconfirmed",
                    "该来源未允许 AI 处理",
                    403,
                )
                return portfolio.instrument_view(
                    i, s.get(LatestQuote, i.id), now(), report_date=self.report_date
                )
            if name != "get_market_snapshot":
                i = instrument(s, args)
                args = args | {"asset_class": i.asset_class, "provider_symbol": i.provider_symbol}
        require(
            "akshare:research" in self.cfg.ai_market_data_providers,
            "data_rights_unconfirmed",
            "历史数据来源未允许 AI 处理",
            403,
        )
        from portfolio_backend.providers.research import ResearchMarket

        return (self.market_provider or ResearchMarket(self.cfg)).fetch(
            name, args, timeout=min(remaining, self.cfg.agent_tool_timeout_seconds)
        )
