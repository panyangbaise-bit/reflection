import hashlib
import re
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import InstrumentNews, NewsItem, Position
from portfolio_backend.providers.news import DeepSeekSearch, NewsReference, WallStreetCN
from portfolio_backend.services.public_data import reserve_request


def relevant(item, instrument):
    text = item.title + " " + item.excerpt
    name = instrument.name.strip()
    if len(name) >= 2 and name.casefold() in text.casefold():
        return True
    symbol = instrument.symbol
    if len(symbol) == 1:
        return "$" + symbol in text
    return bool(
        re.search(r"(?<![\w])" + re.escape(symbol) + r"(?![\w])", text, flags=re.IGNORECASE)
    )


def fresh(items, at):
    return [item for item in items if at - timedelta(hours=48) <= item.published_at <= at]


def encode(items):
    return [asdict(item) | {"published_at": item.published_at.isoformat()} for item in items]


def decode(items):
    return [
        NewsReference(**(item | {"published_at": datetime.fromisoformat(item["published_at"])}))
        for item in (items or [])
    ]


class NewsCollector:
    def __init__(self, db, cfg, shared, primary=None, fallback=None):
        self.db, self.cfg, self.shared = db, cfg, shared
        self.primary, self.fallback = primary or WallStreetCN(), fallback or DeepSeekSearch(cfg)

    def _safe(self, key, ttl, loader):
        try:
            return fresh(decode(self.shared.get(key, ttl, lambda: encode(loader()))), now())
        except DomainError as exc:
            if exc.code in {"data_fetch_pending", "data_cache_unavailable"}:
                raise
            return []

    def collect(self, instrument):
        latest = self._safe("wallstreetcn:latest", self.cfg.news_poll_seconds, self.primary.latest)
        matches = [item for item in latest if relevant(item, instrument)]
        query = instrument.name[:60] + " " + instrument.symbol
        if not matches:

            def primary_search():
                if not reserve_request(self.db, "wallstreetcn-search", 1000):
                    return []
                return self.primary.search(query)

            searched = self._safe("wallstreetcn:search:" + query, 3600, primary_search)
            matches = [item for item in searched if relevant(item, instrument)]
        if not matches and self.cfg.deepseek_api_key.get_secret_value():
            # Public instrument identity only: no account IDs, weights, quantities or private questions.
            after = (now() - timedelta(days=2)).date().isoformat()
            search_query = f"{query} after:{after}"[:128]

            def paid_search():
                if not reserve_request(self.db, "deepseek-search", self.cfg.search_daily_limit):
                    return []
                return self.fallback.search(search_query)

            searched = self._safe("deepseek-search:" + search_query, 21600, paid_search)
            matches = [item for item in searched if relevant(item, instrument)]
        return self.save(instrument.id, matches)

    def save(self, instrument_id, items):
        count = 0
        with self.db.control() as s:
            for item in fresh(items, now()):
                hashed = hashlib.sha256(item.url.encode()).hexdigest()
                values = dict(
                    url_hash=hashed,
                    url=item.url,
                    provider=item.provider,
                    source=item.source,
                    title=item.title,
                    excerpt=item.excerpt,
                    published_at=item.published_at,
                    expires_at=min(
                        now() + timedelta(hours=24), item.published_at + timedelta(hours=48)
                    ),
                )
                # Same URL gets one shared record. Do not overwrite the publisher excerpt with fallback text.
                nid = s.scalar(
                    insert(NewsItem)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["url_hash"])
                    .returning(NewsItem.id)
                )
                if nid is None:
                    nid = s.scalar(select(NewsItem.id).where(NewsItem.url_hash == hashed))
                s.execute(
                    insert(InstrumentNews)
                    .values(instrument_id=instrument_id, news_id=nid)
                    .on_conflict_do_nothing(index_elements=["instrument_id", "news_id"])
                )
                count += 1
        return count


def for_user(session, user_id, cfg, limit=3):
    query = select(NewsItem).where(
        NewsItem.expires_at > now(),
        exists(
            select(InstrumentNews.news_id)
            .join(Position, Position.instrument_id == InstrumentNews.instrument_id)
            .where(
                InstrumentNews.news_id == NewsItem.id,
                Position.user_id == user_id,
                Position.quantity > 0,
            )
        ),
    )
    if cfg.environment == "production":
        query = query.where(NewsItem.provider.in_(cfg.news_display_providers))
    items = session.scalars(
        query.order_by(NewsItem.created_at.desc(), NewsItem.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": str(item.id),
            "title": item.title,
            "summary": item.excerpt,
            "source": item.source,
            "url": item.url,
            "published_at": item.published_at.isoformat(),
            "created_at": item.created_at.isoformat(),
            "provider": item.provider,
            "kind": "news",
            "usage_scope": "production" if cfg.environment == "production" else "development",
        }
        for item in items
    ]


def expire(db):
    with db.control() as s:
        return s.execute(delete(NewsItem).where(NewsItem.expires_at <= now())).rowcount
