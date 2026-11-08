from datetime import timedelta

from sqlalchemy import delete

from portfolio_backend.db import now
from portfolio_backend.models import AgentToolCall


def purge(db, days=30, at=None):
    with db.control() as s:
        return s.execute(
            delete(AgentToolCall).where(
                AgentToolCall.created_at < (at or now()) - timedelta(days=days)
            )
        ).rowcount
