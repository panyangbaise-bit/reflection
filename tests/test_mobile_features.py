from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from portfolio_backend.db import now
from portfolio_backend.models import Conversation, Message, Report

M = "/api/mobile/v1"


def test_classification_and_layout_conflicts(client, user, instrument, opening):
    u, other = user(), user()
    p = opening(u, instrument())
    body = {"position_type": "satellite", "expected_version": p["version"]}
    path = M + f"/positions/{p['id']}/classification"
    assert client.patch(path, headers=other["headers"], json=body).status_code == 404
    r = client.patch(path, headers=u["headers"], json=body)
    assert r.status_code == 200
    assert r.json()["data"]["position_type"] == "satellite"
    assert client.patch(path, headers=u["headers"], json=body).status_code == 409
    snapshot = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert snapshot["positions"][0]["position_type"] == "satellite"
    layout = {"overview_order": ["holdings", "latest", "discipline"], "expected_version": 0}
    assert client.put(M + "/me/layout", headers=u["headers"], json=layout).status_code == 200
    assert client.put(M + "/me/layout", headers=u["headers"], json=layout).status_code == 409
    assert (
        client.get(M + "/me/preferences", headers=u["headers"]).json()["data"]["overview_order"]
        == layout["overview_order"]
    )
    assert (
        client.get(M + "/me/preferences", headers=other["headers"]).json()["data"]["overview_order"]
        is None
    )


def test_discipline_stale_editor_never_overwrites(client, user):
    u = user()
    first = client.put(
        M + "/me/discipline",
        headers=u["headers"],
        json={"content": "A", "expected_updated_at": None},
    )
    assert first.status_code == 200
    stamp = first.json()["data"]["updated_at"]
    assert (
        client.put(
            M + "/me/discipline",
            headers=u["headers"],
            json={"content": "B", "expected_updated_at": stamp},
        ).status_code
        == 200
    )
    assert (
        client.put(
            M + "/me/discipline",
            headers=u["headers"],
            json={"content": "C", "expected_updated_at": stamp},
        ).status_code
        == 409
    )
    assert client.get(M + "/me/discipline", headers=u["headers"]).json()["data"]["content"] == "B"


def test_conversation_idempotency_and_pagination(client, user, db):
    u = user()
    headers = u["headers"] | {"Idempotency-Key": "new-conversation-key"}
    first = client.post(M + "/conversations", headers=headers, json={"title": "Q"})
    second = client.post(M + "/conversations", headers=headers, json={"title": "Q"})
    assert first.json() == second.json()
    with db.tenant(u["id"]) as s:
        conversation = s.scalar(select(Conversation))
        cid = conversation.id
        for n in range(7):
            s.add(Message(user_id=u["id"], conversation_id=cid, role="user", content=f"message{n}"))
    messages, cursor = [], None
    while True:
        data = client.get(
            M + f"/conversations/{cid}/messages",
            headers=u["headers"],
            params={"paged": "true", "limit": 2, **({"cursor": cursor} if cursor else {})},
        ).json()["data"]
        messages += data["items"]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(messages) == len({m["id"] for m in messages}) == 7
    assert (
        client.get(M + f"/conversations/{cid}/active-run", headers=u["headers"]).json()["data"][
            "run_id"
        ]
        is None
    )


def test_ledger_filters_do_not_expose_other_accounts(client, user, instrument, opening):
    u, other = user(), user()
    p = opening(u, instrument())
    assert (
        client.get(
            M + "/ledger-events", headers=other["headers"], params={"position_id": p["id"]}
        ).status_code
        == 404
    )
    items = client.get(
        M + "/ledger-events", headers=u["headers"], params={"account_id": str(u["account_id"])}
    ).json()["data"]["items"]
    assert len(items) == 1 and items[0]["kind"] == "opening_snapshot"


def test_ledger_pages_in_event_order_even_when_timestamps_tie(client, user, instrument, opening):
    from datetime import datetime

    u, other = user(), user()
    at = now() - timedelta(days=1)
    p = opening(u, instrument(), occurred_at=(at - timedelta(days=1)).isoformat())
    for n in range(4):
        response = client.post(
            M + "/ledger-events",
            headers=u["headers"] | {"Idempotency-Key": str(uuid4())},
            json={
                "position_id": p["id"],
                "expected_version": p["version"] + n,
                "kind": "buy",
                "quantity": "1",
                "price": "10",
                "fee": "0",
                "occurred_at": at.isoformat(),
            },
        )
        assert response.status_code == 201, response.text
    seen, cursor = [], None
    while True:
        page = client.get(
            M + "/ledger-events",
            headers=u["headers"],
            params={
                "limit": 2,
                **({"cursor": cursor} if cursor else {}),
            },
        ).json()["data"]
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len({row["id"] for row in seen}) == 5
    stamps = [(datetime.fromisoformat(row["occurred_at"]), row["id"]) for row in seen]
    assert stamps == sorted(stamps, reverse=True)
    assert seen[-1]["kind"] == "opening_snapshot"
    assert (
        client.get(
            M + "/ledger-events", headers=other["headers"], params={"cursor": seen[0]["id"]}
        ).status_code
        == 404
    )


def test_report_read_snapshot_and_free_quota(client, user, db, cfg):
    from portfolio_backend.models import Consent
    from portfolio_backend.services import portfolio

    u = user()
    with db.tenant(u["id"]) as s:
        r = Report(
            user_id=u["id"],
            kind="daily",
            period="2026-10-26",
            title="R",
            summary="facts",
            snapshot=portfolio.snapshot(s, u["id"]),
        )
        s.add(r)
        s.add(
            Consent(
                user_id=u["id"],
                purpose="interactive_ai",
                provider=cfg.ai_provider_id,
                disclosure_version=cfg.ai_disclosure_version,
                granted=True,
            )
        )
        s.flush()
        rid = r.id
    path = M + f"/reports/{rid}"
    assert client.get(path, headers=u["headers"]).json()["data"]["remaining_free"] == 3
    assert client.post(path + "/read", headers=u["headers"]).status_code == 200
    assert client.get(path, headers=u["headers"]).json()["data"]["read"]
    headers = u["headers"] | {"Idempotency-Key": str(uuid4())}
    a = client.post(path + "/questions", headers=headers, json={"content": "解释"})
    b = client.post(path + "/questions", headers=headers, json={"content": "解释"})
    assert a.status_code == 202 and a.json() == b.json()
    assert client.get(path, headers=u["headers"]).json()["data"]["remaining_free"] == 2


def test_simulator_provider_cannot_run_in_production(cfg):
    from portfolio_backend.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="test", simulator_ai=True, ai_provider_id="local-simulator")


def test_export_is_paged_private_and_has_no_auth_tokens(client, user, instrument, opening):
    u, other = user(), user()
    opening(u, instrument())
    response = client.get(M + "/me/export?section=positions", headers=u["headers"])
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["schema_version"] == 1 and data["items"][0]["quantity"].startswith("100")
    assert (
        client.get(M + "/me/export?section=positions", headers=other["headers"]).json()["data"][
            "items"
        ]
        == []
    )
    assert (
        client.get(M + "/me/export?section=auth_sessions", headers=u["headers"]).status_code == 422
    )
    assert client.get(M + "/me/export").status_code == 401


def test_trace_retention_and_account_erasure(client, db, cfg, user, instrument):
    from portfolio_backend.models import AgentToolCall, Job, MemoryEvidence, WatchlistItem
    from portfolio_backend.services import jobs
    from portfolio_backend.services.trace_retention import purge
    from portfolio_backend.worker import execute

    u, other = user(), user()
    iid = instrument()
    at = now()
    with db.control() as s:
        for owner in (u, other):
            job = Job(user_id=owner["id"], kind="daily", dedupe_key=str(uuid4()), payload={})
            s.add(job)
            s.flush()
            for age in (31, 1):
                s.add(
                    AgentToolCall(
                        user_id=owner["id"],
                        job_id=job.id,
                        round=1,
                        tool_name="get_portfolio",
                        call_id=str(uuid4()),
                        triggered_by="daily",
                        status="succeeded",
                        created_at=at - timedelta(days=age),
                    )
                )
            s.add(WatchlistItem(user_id=owner["id"], instrument_id=iid))
            s.add(
                MemoryEvidence(
                    user_id=owner["id"],
                    source_type="test",
                    source_id="1",
                    version="v1",
                    content="private",
                    as_of=at,
                )
            )
    assert purge(db, days=30, at=at) == 2
    assert purge(db, days=30, at=at) == 0
    response = client.post(
        M + "/me/deletion-requests", headers=u["headers"], json={"confirmation": "DELETE"}
    )
    assert response.status_code == 202
    jobs.materialize_outbox(db)
    with db.control() as s:
        jid = s.scalar(select(Job.id).where(Job.kind == "delete_account", Job.user_id == u["id"]))
    assert execute(db, jid, cfg) == "succeeded"
    with db.control() as s:
        for model in (AgentToolCall, WatchlistItem, MemoryEvidence):
            assert s.scalars(select(model).where(model.user_id == u["id"])).all() == []
            assert len(s.scalars(select(model).where(model.user_id == other["id"])).all()) == 1
