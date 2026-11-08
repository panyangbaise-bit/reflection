import hashlib
import json
from uuid import UUID

from sqlalchemy import select, text

from portfolio_backend.errors import require
from portfolio_backend.models import Idempotency, Portfolio


def lock_portfolio(session, user_id):
    row = session.scalar(select(Portfolio).where(Portfolio.user_id == user_id).with_for_update())
    require(row is not None, "not_found", "组合不存在", 404)
    return row


def owned(session, model, row_id, user_id, lock=False):
    query = select(model).where(model.id == row_id, model.user_id == user_id)
    row = session.scalar(query.with_for_update() if lock else query)
    require(row is not None, "not_found", "记录不存在", 404)
    return row


def idempotent(session, user_id: UUID, scope, key, payload, action):
    require(key and 1 <= len(key) <= 128, "idempotency_required", "需要 Idempotency-Key", 422)
    # Database lock covers absent keys too; uniqueness alone cannot serialize first attempts.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{user_id}:{scope}:{key}"},
    )
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    existing = session.scalar(
        select(Idempotency).where(
            Idempotency.user_id == user_id, Idempotency.scope == scope, Idempotency.key == key
        )
    )
    if existing:
        require(
            existing.request_hash == request_hash,
            "idempotency_conflict",
            "同一幂等键不能用于不同请求",
        )
        return existing.response
    result = action()
    session.add(
        Idempotency(
            user_id=user_id, scope=scope, key=key, request_hash=request_hash, response=result
        )
    )
    return result
