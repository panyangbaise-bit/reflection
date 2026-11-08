from datetime import timedelta

from portfolio_backend.db import now
from portfolio_backend.models import Report


def test_overview_chart_uses_only_own_complete_daily_snapshots(client, db, user):
    a, b = user(), user()
    at = now()
    with db.control() as s:
        for owner, kind, offset, complete, value in [
            (a, "daily", 4, True, "101.1234567890"),
            (a, "daily", 3, False, "99"),
            (a, "weekly", 2, True, "9999"),
            (a, "daily", 1, True, "110"),
            (b, "daily", 1, True, "777"),
        ]:
            stamp = at - timedelta(days=offset)
            s.add(
                Report(
                    user_id=owner["id"],
                    kind=kind,
                    period=stamp.date().isoformat(),
                    title="报告",
                    summary="摘要",
                    created_at=stamp,
                    snapshot={
                        "as_of": stamp.isoformat(),
                        "complete": complete,
                        "market_value_cny": value,
                    },
                )
            )
    data = client.get("/api/mobile/v1/home", headers=a["headers"]).json()["data"]
    assert [p["market_value_cny"] for p in data["asset_history"]] == ["101.1234567890", "110"]
    # Asset change between the two most recent complete daily marks.
    assert data["snapshot"]["yesterday_pnl_cny"] == "8.8765432110"
    assert data["snapshot"]["daily_pnl_cny"] is None
    assert data["snapshot"]["cumulative_pnl_cny"] is None
    other = client.get("/api/mobile/v1/home", headers=b["headers"]).json()["data"]
    assert [p["market_value_cny"] for p in other["asset_history"]] == ["777"]
