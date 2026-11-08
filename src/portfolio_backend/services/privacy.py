"""User-readable paged export; never includes credentials or cross-tenant control data."""

from sqlalchemy import select

from portfolio_backend.db import now
from portfolio_backend.errors import require
from portfolio_backend.models import Base

EXPORT_SECTIONS = (
    "portfolios",
    "accounts",
    "positions",
    "ledger_events",
    "reports",
    "conversations",
    "messages",
    "consents",
    "preferences",
    "trading_discipline",
    "watchlist_items",
    "memory_evidence",
    "ai_runs",
    "agent_tool_calls",
)


def export_page(session, user_id, section, cursor=None, limit=100):
    require(section in EXPORT_SECTIONS, "invalid_export_section", "未知导出类别", 422)
    table = Base.metadata.tables[section]
    key = table.c.get("id") if "id" in table.c else table.c.user_id
    q = select(table).where(table.c.user_id == user_id).order_by(key)
    if cursor:
        q = q.where(key > cursor)
    rows = session.execute(q.limit(limit + 1)).mappings().all()
    return {
        "schema_version": 1,
        "section": section,
        "generated_at": now(),
        "consistency": "per_page",
        "items": [{k: v for k, v in r.items() if k != "user_id"} for r in rows[:limit]],
        "next_cursor": str(rows[limit - 1][key.name]) if len(rows) > limit else None,
    }
