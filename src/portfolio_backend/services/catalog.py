"""Build the instrument catalog from AKShare symbol lists.

Mobile requests only read PostgreSQL. The optional ``market-data`` worker owns
the upstream fetch and runs in the memory-bounded collector container.
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
import unicodedata
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import CatalogSyncState, Instrument
from portfolio_backend.providers.instrument_search import fetch_suggestions
from portfolio_backend.services.public_data import SharedFetch

logger = logging.getLogger(__name__)

KINDS = ("cn-stock", "cn-etf", "hk-stock", "cn-fund", "us-stock", "cn-lof", "cn-closed-fund")
LOCK_KEY = "catalog:v1:sync-lock"
LAST_SYNC_KEY = "catalog:v1:last-sync"
SYNC_REQUEST_KEY = "catalog:v1:sync-requested"
RETRY_SECONDS = 300
FAILED_KEY = "catalog:v1:failed:"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_term(query):
    value = unicodedata.normalize("NFKC", query).strip()
    market = None
    suffix = re.fullmatch(r"(.+)\.(SH|SZ|BJ|HK|US|SS)", value, re.IGNORECASE)
    prefix = re.fullmatch(r"(SH|SZ|BJ|HK|US)([0-9]{1,6})", value, re.IGNORECASE)
    if suffix:
        value, exchange = suffix.groups()
        market = {"HK": "HK", "US": "US"}.get(exchange.upper(), "CN")
    elif prefix:
        exchange, value = prefix.groups()
        market = {"HK": "HK", "US": "US"}.get(exchange.upper(), "CN")
    return value, market


class CatalogService:
    def __init__(self, config, database, cache):
        self.config = config
        self.db = database
        self.cache = cache

    def _fetch(self, kind):
        timeout = 120
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "portfolio_backend.providers.akshare_catalog_worker",
                    kind,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "catalog_worker_failed kind=%s code=%s stderr=%s",
                kind,
                exc.returncode,
                (exc.stderr or "")[-300:],
            )
            raise DomainError("catalog_source_unavailable", "行情目录源暂不可用", 503) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise DomainError("catalog_source_unavailable", "行情目录源暂不可用", 503) from exc
        try:
            rows = json.loads(result.stdout)
        except ValueError as exc:
            raise DomainError("catalog_invalid", "行情目录数据格式已变化", 503) from exc
        if not isinstance(rows, list) or not rows:
            raise DomainError("catalog_empty", "行情目录源没有返回标的", 503)
        return rows

    def _upsert(self, rows):
        values = [
            row
            | {
                "enabled": True,
                "commercial_display": self.config.akshare_display_confirmed
                or row["provider"] in self.config.commercial_data_providers,
            }
            for row in rows
        ]
        with self.db.control() as session:
            for start in range(0, len(values), 500):
                statement = insert(Instrument).values(values[start : start + 500])
                statement = statement.on_conflict_do_update(
                    index_elements=["market", "symbol", "asset_class"],
                    set_={
                        "name": statement.excluded.name,
                        "provider": statement.excluded.provider,
                        "provider_symbol": statement.excluded.provider_symbol,
                        "quote_kind": statement.excluded.quote_kind,
                        "freshness_seconds": statement.excluded.freshness_seconds,
                        "enabled": or_(Instrument.enabled, statement.excluded.enabled),
                        "commercial_display": statement.excluded.commercial_display,
                    },
                    # Never overwrite an operator's manual provider namespace.
                    where=Instrument.provider.like("akshare:%"),
                )
                session.execute(statement)
        return len(values)

    @staticmethod
    def last_key(kind):
        return LAST_SYNC_KEY + ":" + kind

    def states(self):
        with self.db.control() as session:
            rows = session.scalars(select(CatalogSyncState)).all()
            session.expunge_all()
            return {row.kind: row for row in rows}

    def record_attempt(self, kind, count=0, error=None):
        stamp = now()
        with self.db.control() as session:
            values = {"kind": kind, "last_attempt": stamp, "last_error": error}
            updates = {"last_attempt": stamp, "last_error": error}
            if not error:
                updates.update(last_success=stamp, row_count=count)
            values.update(last_success=stamp if not error else None, row_count=count)
            session.execute(
                insert(CatalogSyncState)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["kind"],
                    set_=updates,
                )
            )

    def sync(self, kinds=KINDS):
        token = str(uuid4())
        try:
            acquired = self.cache.set(LOCK_KEY, token, nx=True, ex=120 * len(kinds) + 60)
        except RedisError:
            acquired = True
        if not acquired:
            return {"status": "busy"}
        counts = {}
        stamp = str(int(time.time()))
        try:
            for kind in kinds:
                try:
                    counts[kind] = self._upsert(self._fetch(kind))
                    self.record_attempt(kind, counts[kind])
                    if counts[kind]:
                        try:
                            self.cache.set(self.last_key(kind), stamp)
                            self.cache.delete(FAILED_KEY + kind)
                        except RedisError:
                            pass
                except DomainError as exc:
                    counts[kind] = 0
                    self.record_attempt(kind, error=exc.code)
                    try:
                        self.cache.set(FAILED_KEY + kind, stamp, ex=RETRY_SECONDS)
                    except RedisError:
                        pass
                    logger.warning("catalog_fetch_failed kind=%s code=%s", kind, exc.code)
            counts["total"] = sum(counts.get(kind, 0) for kind in kinds)
            counts["status"] = "ok" if all(counts.get(kind, 0) > 0 for kind in kinds) else "partial"
            if counts["total"]:
                try:
                    self.cache.set(LAST_SYNC_KEY, stamp)
                except RedisError:
                    pass
            return counts
        finally:
            try:
                self.cache.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) end",
                    1,
                    LOCK_KEY,
                    token,
                )
            except RedisError:
                pass

    def sync_if_due(self):
        now_ts = time.time()
        due = []
        states = self.states()
        for kind in KINDS:
            state = states.get(kind)
            if (
                state
                and state.last_error
                and now_ts - state.last_attempt.timestamp() < RETRY_SECONDS
            ):
                continue
            last = state.last_success.timestamp() if state and state.last_success else 0
            if not last or now_ts - last >= self.config.catalog_sync_hours * 3600:
                due.append(kind)
        if not due:
            return None
        return self.sync(tuple(due))

    def request_sync(self, *, force=True):
        try:
            self.cache.set(SYNC_REQUEST_KEY, "1" if force else "due", ex=3600, nx=not force)
        except RedisError:
            pass

    def consume_request(self):
        try:
            request = self.cache.get(SYNC_REQUEST_KEY)
            if not request:
                return None
        except RedisError:
            return None
        try:
            self.cache.delete(SYNC_REQUEST_KEY)
        except RedisError:
            pass
        return self.sync_if_due() if request in (b"due", "due") else self.sync()

    def count(self):
        with self.db.control() as session:
            return session.scalar(select(func.count()).select_from(Instrument)) or 0

    def search(self, query, limit=30):
        text, market = search_term(query)
        variants = {text}
        if text.isdigit() and len(text) < 5:
            variants.add(text.zfill(5))
        rank_cases = []
        conditions = []
        for value in sorted(variants):
            escaped = escape_like(value)
            rank_cases.extend(
                [
                    (func.lower(Instrument.symbol) == value.lower(), 0),
                    (func.lower(Instrument.provider_symbol) == value.lower(), 0),
                    (Instrument.symbol.ilike(escaped + "%"), 1),
                    (Instrument.provider_symbol.ilike(escaped + "%"), 1),
                    (Instrument.name == value, 2),
                    (Instrument.name.ilike(escaped + "%"), 3),
                ]
            )
            conditions.extend(
                [
                    Instrument.name.ilike("%" + escaped + "%"),
                    Instrument.search_aliases.ilike("%" + escaped + "%"),
                    Instrument.symbol.ilike("%" + escaped + "%"),
                    Instrument.provider_symbol.ilike("%" + escaped + "%"),
                ]
            )
        rank = case(*rank_cases, else_=4)
        statement = select(Instrument).where(or_(*conditions))
        if market:
            statement = statement.where(Instrument.market == market)
        with self.db.control() as session:
            rows = session.scalars(
                statement.order_by(rank, Instrument.market, Instrument.symbol).limit(limit)
            ).all()
            session.expunge_all()
            return rows

    def discover(self, query):
        if not self.config.catalog_live_search_enabled:
            return
        term, market = search_term(query)
        if len(term) < 2:
            return

        def fetch():
            # Public lookup budget across all app users; cached results do not spend it.
            key = "catalog:v2:lookup-rate:" + str(int(time.time()) // 60)
            count = self.cache.incr(key)
            self.cache.expire(key, 120)
            if count > 60:
                return []
            return fetch_suggestions(term)

        try:
            rows = (
                SharedFetch(self.cache).get("instrument-search:" + term.casefold(), 900, fetch)
                or []
            )
            with self.db.control() as session:
                for row in rows:
                    if market and row["market"] != market:
                        continue
                    values = row | {
                        "enabled": True,
                        "commercial_display": self.config.akshare_display_confirmed
                        or row["provider"] in self.config.commercial_data_providers,
                    }
                    statement = insert(Instrument).values(**values)
                    # Keep full-directory identity, quote feed and existing user references.
                    # Only augment search aliases on conflict; never overwrite manual rows.
                    session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["market", "symbol", "asset_class"],
                            set_={"search_aliases": statement.excluded.search_aliases},
                            where=Instrument.provider.like("akshare:%"),
                        )
                    )
        except Exception:
            # Directory search remains available when optional discovery is unreachable.
            logger.warning("catalog_live_lookup_unavailable")

    def ready_or_request(self, query=""):
        if not self.config.catalog_auto_discover:
            return False
        count = self.count()
        if count:
            states = self.states()
            if query and not all(states.get(kind) and states[kind].last_success for kind in KINDS):
                self.request_sync(force=False)
            # An upstream outage never invalidates the last durable directory.
            return True
        if query:
            self.request_sync(force=False)
        raise DomainError(
            "catalog_unavailable",
            "行情目录正在初始化，请稍后重试；需要运行数据采集进程",
            503,
        )


def main():
    parser = argparse.ArgumentParser(description="Sync the AKShare instrument catalog")
    parser.add_argument(
        "--kind",
        action="append",
        choices=KINDS,
        help="limit to specific list (repeatable)",
    )
    args = parser.parse_args()
    config = settings()
    db = Database(config)
    cache = Redis.from_url(config.cache_url)
    db.verify_roles()
    service = CatalogService(config, db, cache)
    try:
        print(service.sync(tuple(args.kind) if args.kind else KINDS), flush=True)
    finally:
        db.close()
        cache.close()


if __name__ == "__main__":
    main()
