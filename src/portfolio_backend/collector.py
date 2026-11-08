"""Shared low-cost acquisition worker. Mobile reads never trigger upstream network requests."""

import argparse
import logging
import time
from dataclasses import asdict
from uuid import UUID

from redis import Redis
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import (
    FXRate,
    Instrument,
    LatestQuote,
    Position,
    User,
    WatchlistItem,
)
from portfolio_backend.providers import market_worker
from portfolio_backend.providers.market import AKShareFX, AKShareQuotes, market_session
from portfolio_backend.services.catalog import CatalogService
from portfolio_backend.services.news import NewsCollector, expire
from portfolio_backend.services.public_data import SharedFetch

logger = logging.getLogger(__name__)


def display_confirmed(provider, cfg):
    if provider in cfg.commercial_data_providers:
        return True
    return cfg.akshare_display_confirmed and provider.startswith("akshare:")


def effective_quote_poll(cfg, at=None):
    """Quote poll/TTL window: session cadence during CN trading hours, slower otherwise."""
    return cfg.quote_poll_seconds if market_session(at) else cfg.off_hours_quote_poll_seconds


def effective_news_poll(cfg, at=None):
    return cfg.news_poll_seconds if market_session(at) else cfg.off_hours_news_poll_seconds


def publish_mark(db, instrument, mark, cfg):
    if mark.kind != instrument.quote_kind:
        raise DomainError("quote_kind_mismatch", "延迟行情不能冒充实时行情", 422)
    if cfg.environment == "production" and not (
        instrument.commercial_display and display_confirmed(mark.provider, cfg)
    ):
        raise DomainError("data_rights_unconfirmed", "此数据源的商业用途尚未确认", 422)
    with db.control() as s:
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "quote:" + str(instrument.id)},
        )
        current = s.get(Instrument, instrument.id)
        if not current or current.provider != mark.provider or current.quote_kind != mark.kind:
            raise DomainError("quote_provider_changed", "数据源设置已变更", 409)
        values = asdict(mark) | {
            "instrument_id": instrument.id,
            "fetched_at": now(),
            "market_state": "unknown",
        }
        query = insert(LatestQuote).values(**values)
        s.execute(
            query.on_conflict_do_update(
                index_elements=["instrument_id"],
                set_={k: v for k, v in values.items() if k != "instrument_id"},
                where=LatestQuote.as_of <= mark.as_of,
            )
        )


def refresh_fx(db, cfg, cache, loader=None):
    """Upsert PBOC parities at most once per fx_poll_seconds; failure keeps the last rates."""
    if not cache.set("public-data:fx-fetched", "1", ex=cfg.fx_poll_seconds, nx=True):
        return 0
    loader = loader or AKShareFX()
    marks = loader.load()
    updates = 0
    with db.control() as s:
        for mark in marks:
            values = {
                "currency": mark.currency,
                "rate": mark.rate,
                "as_of": mark.as_of,
                "valid_until": mark.valid_until,
                "provider": mark.provider,
            }
            query = insert(FXRate).values(**values)
            s.execute(
                query.on_conflict_do_update(
                    index_elements=["currency"],
                    set_={k: v for k, v in values.items() if k != "currency"},
                    # 新来源（如从手工注入切回 akshare:fx）允许替换旧行；同一来源不回退旧价。
                    where=or_(FXRate.as_of <= mark.as_of, FXRate.provider != mark.provider),
                )
            )
            updates += 1
    return updates


def tick(db, cfg, cache, quotes=None):
    """One quote pass: a bounded, cursor-fair batch of held/watchlist instruments."""
    if not cfg.data_collection_enabled:
        return {"status": "disabled"}
    shared, quotes = SharedFetch(cache), quotes or AKShareQuotes()
    # Stable cursor makes bounded batches fair even if some symbols repeatedly fail.
    cursor_key = "public-data:instrument-cursor"
    cursor = cache.get(cursor_key)
    with db.control() as s:
        # Poll only what a user actually holds. The catalog may contain thousands
        # of searchable symbols; refreshing all of them would delay real holdings.
        held = (
            select(Position.instrument_id)
            .where(Position.quantity > 0)
            .union(select(WatchlistItem.instrument_id))
        )
        base = select(Instrument).where(Instrument.id.in_(held))
        query = base.where(Instrument.id > UUID(cursor.decode())) if cursor else base
        instruments = s.scalars(
            query.order_by(Instrument.id).limit(cfg.data_max_instruments_per_tick)
        ).all()
        if not instruments and cursor:
            instruments = s.scalars(
                base.order_by(Instrument.id).limit(cfg.data_max_instruments_per_tick)
            ).all()
        # Values detached before external I/O; no transaction/connection is held across slow sources.
        s.expunge_all()
    counts = {
        "quote_updates": 0,
        "news_matches": 0,
        "source_errors": 0,
        "instruments": len(instruments),
        "fx_updates": 0,
    }
    poll = effective_quote_poll(cfg)
    for instrument in instruments:
        if instrument.provider in quotes.providers:
            try:
                data = shared.get(
                    quotes.cache_key(instrument),
                    poll,
                    lambda: quotes.load(instrument),
                )
                if data:
                    mark = quotes.normalize(instrument, data, now())
                    publish_mark(db, instrument, mark, cfg)
                    counts["quote_updates"] += 1
            except DomainError as exc:
                counts["source_errors"] += 1
                logger.warning(
                    "quote_collection_failed provider=%s code=%s", instrument.provider, exc.code
                )
        cache.set(cursor_key, str(instrument.id), ex=86400)
    expire(db)
    return counts


def collect_news(db, cfg, cache):
    """One news pass over every active holding; runs on the news cadence."""
    if not cfg.data_collection_enabled:
        return {"status": "disabled"}
    shared = SharedFetch(cache)
    with db.control() as s:
        # Only positive positions of active users are news-monitored (never the watchlist).
        news_targets = set(
            s.scalars(
                select(Position.instrument_id)
                .join(User)
                .where(Position.quantity > 0, User.status == "active")
            )
        )
        instruments = (
            s.scalars(select(Instrument).where(Instrument.id.in_(news_targets))).all()
            if news_targets
            else []
        )
        s.expunge_all()
    news_cfg = cfg.model_copy(update={"news_poll_seconds": effective_news_poll(cfg)})
    news = NewsCollector(db, news_cfg, shared)
    counts = {"news_matches": 0, "source_errors": 0, "instruments": len(instruments)}
    for instrument in instruments:
        try:
            counts["news_matches"] += news.collect(instrument)
        except DomainError:
            counts["source_errors"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    cfg = settings()
    db, cache = Database(cfg), Redis.from_url(cfg.cache_url)
    db.verify_roles()
    catalog = CatalogService(cfg, db, cache)
    next_catalog = 0.0
    # Independent rhythms: quotes, news and FX each run on their own cadence.
    next_quote = next_news = next_fx = 0.0
    try:
        while True:
            if cfg.catalog_auto_discover:
                try:
                    requested = catalog.consume_request()
                    if requested:
                        print({"catalog": requested}, flush=True)
                    elif time.monotonic() >= next_catalog:
                        result = catalog.sync_if_due()
                        if result:
                            print({"catalog": result}, flush=True)
                        next_catalog = time.monotonic() + (
                            3600 if result and result.get("total") else 300
                        )
                except Exception:
                    logger.error("catalog_sync_failed")
                    next_catalog = time.monotonic() + 3600
            if time.monotonic() >= next_quote:
                try:
                    print(tick(db, cfg, cache), flush=True)
                except Exception:
                    logger.error("data_collection_failed")
                next_quote = time.monotonic() + effective_quote_poll(cfg)
            if time.monotonic() >= next_news:
                try:
                    print(collect_news(db, cfg, cache), flush=True)
                except Exception:
                    logger.error("news_collection_failed")
                next_news = time.monotonic() + effective_news_poll(cfg)
            if time.monotonic() >= next_fx:
                try:
                    print({"fx_updates": refresh_fx(db, cfg, cache)}, flush=True)
                except DomainError as exc:
                    logger.warning("fx_collection_failed code=%s", exc.code)
                except Exception:
                    logger.error("fx_collection_failed")
                next_fx = time.monotonic() + cfg.fx_poll_seconds
            if args.once:
                return
            # Wake every 30s (catalog request latency) or at the earliest deadline.
            delay = (
                min(
                    next_quote,
                    next_news,
                    next_fx,
                    next_catalog if cfg.catalog_auto_discover else float("inf"),
                )
                - time.monotonic()
            )
            time.sleep(max(0.0, min(delay, 30.0)))
    finally:
        db.close()
        cache.close()
        market_worker.close_default_pool()


if __name__ == "__main__":
    main()
