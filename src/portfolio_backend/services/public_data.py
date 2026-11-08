"""Shared public-data cache and durable request budgets; never keyed by a user's holdings."""

import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert

from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import ExternalBudget


class SharedFetch:
    def __init__(self, cache):
        self.cache = cache

    def get(self, key, ttl, loader):
        key = "public-data:v1:" + hashlib.sha256(key.encode()).hexdigest()
        token = str(uuid4())
        try:
            data = self.cache.get(key)
            if data:
                return json.loads(data)
            if self.cache.get(key + ":error"):
                return None
            if not self.cache.set(key + ":lock", token, nx=True, ex=90):
                raise DomainError("data_fetch_pending", "共享数据正在更新", 503)
        except (RedisError, ValueError) as exc:
            raise DomainError("data_cache_unavailable", "共享缓存暂不可用", 503) from exc
        try:
            # Recheck after taking the lock in case another process filled it just before release.
            data = self.cache.get(key)
            if data:
                return json.loads(data)
            value = loader()
            self.cache.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
            return value
        except Exception:
            self.cache.set(key + ":error", "1", ex=60)
            raise
        finally:
            self.cache.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end",
                1,
                key + ":lock",
                token,
            )


def reserve_request(db, provider, limit, at=None):
    """Reserve before calling. Unknown outcomes count; no optimistic refunds on timeout."""
    if limit <= 0:
        return False
    day = (at or now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with db.control() as s:
        query = insert(ExternalBudget).values(provider=provider, day=day, reserved_calls=1)
        used = s.scalar(
            query.on_conflict_do_update(
                index_elements=["provider", "day"],
                set_={"reserved_calls": ExternalBudget.reserved_calls + 1},
                where=ExternalBudget.reserved_calls < limit,
            ).returning(ExternalBudget.reserved_calls)
        )
        return used is not None
