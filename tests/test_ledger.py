from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from portfolio_backend.db import now
from portfolio_backend.domain.ledger import Balance, apply
from portfolio_backend.errors import DomainError
from portfolio_backend.models import Account, LedgerEvent, Position

M = "/api/mobile/v1"


def test_average_cost_and_realized():
    balance = apply(Balance(), "opening_snapshot", Decimal("100"), Decimal("10"))
    balance = apply(balance, "sell", Decimal("50"), Decimal("12"))
    assert balance == Balance(Decimal("50"), Decimal("500"), Decimal("100"))
    balance = apply(balance, "sell", Decimal("50"), Decimal("12"), Decimal("1"))
    assert balance == Balance(Decimal(0), Decimal(0), Decimal("199"))


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0"])
def test_invalid_amount(amount):
    with pytest.raises(DomainError):
        apply(Balance(), "buy", Decimal(amount), Decimal(1))


def test_opening_trade_snapshot(client, user, instrument, opening):
    u, i = user(), instrument()
    p = opening(u, i)
    payload = {
        "position_id": p["id"],
        "kind": "sell",
        "quantity": "50",
        "price": "12",
        "expected_version": 1,
        "occurred_at": now().isoformat(),
    }
    res = client.post(
        M + "/ledger-events", json=payload, headers=u["headers"] | {"Idempotency-Key": "sell-1"}
    )
    assert res.status_code == 201, res.text
    row = res.json()["data"]
    assert Decimal(row["average_cost"]) == 10
    assert Decimal(row["realized_pnl"]) == 100
    snapshot = client.get(M + "/portfolio/snapshot", headers=u["headers"]).json()["data"]
    assert snapshot["complete"]
    assert Decimal(snapshot["market_value_cny"]) == 600
    assert Decimal(snapshot["unrealized_pnl_cny"]) == 100
    assert snapshot["history_complete"] is False
    assert snapshot["daily_pnl_cny"] is None
    assert Decimal(snapshot["cumulative_pnl_cny"]) == 200
    ledger = client.get(M + "/ledger-events", headers=u["headers"]).json()["data"]["items"]
    assert {r["kind"] for r in ledger} == {"opening_snapshot", "sell"}
    assert all(isinstance(r["price"], str) for r in ledger)


def test_concurrent_idempotency_and_sell(client, user, instrument, opening, db):
    u = user()
    p = opening(u, instrument())
    payload = {
        "position_id": p["id"],
        "kind": "sell",
        "quantity": "70",
        "price": "12",
        "expected_version": 1,
        "occurred_at": now().isoformat(),
    }

    def request(key):
        return client.post(
            M + "/ledger-events", json=payload, headers=u["headers"] | {"Idempotency-Key": key}
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(request, ["identical"] * 10))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert len({r.text for r in responses}) == 1
    assert request("new-key").json()["error"]["code"] == "version_conflict"
    payload["quantity"] = "40"
    assert request("identical").json()["error"]["code"] == "idempotency_conflict"
    payload["expected_version"] = 2
    assert request("too-much").json()["error"]["code"] == "excessive_sale"
    with db.tenant(u["id"]) as s:
        assert s.scalar(select(func.count()).select_from(LedgerEvent)) == 2


def test_two_distinct_sells_cannot_overdraw(client, user, instrument, opening):
    u = user()
    p = opening(u, instrument())
    payload = {
        "position_id": p["id"],
        "kind": "sell",
        "quantity": "70",
        "price": "12",
        "expected_version": 1,
        "occurred_at": now().isoformat(),
    }

    def request(_):
        return client.post(
            M + "/ledger-events",
            json=payload,
            headers=u["headers"] | {"Idempotency-Key": str(uuid4())},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(request, range(2))) == [201, 409]


def test_tenant_isolation_even_without_query_filter(client, user, instrument, opening, db):
    a, b = user(), user()
    p = opening(a, instrument())
    with db.tenant(b["id"]) as s:
        assert s.scalars(select(Position)).all() == []
        assert len(s.scalars(select(Account)).all()) == 1
    with db.engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM positions")).all() == []
    res = client.post(
        M + "/ledger-events",
        headers=b["headers"] | {"Idempotency-Key": "steal"},
        json={
            "position_id": p["id"],
            "kind": "sell",
            "quantity": "10",
            "price": "10",
            "expected_version": 1,
            "occurred_at": now().isoformat(),
        },
    )
    assert res.status_code == 404
    with pytest.raises(DBAPIError), db.tenant(b["id"]) as s:
        s.execute(text("UPDATE ledger_events SET price=1"))
    with pytest.raises(DBAPIError), db.tenant(b["id"]) as s:
        s.add(
            Position(
                user_id=b["id"],
                account_id=a["account_id"],
                instrument_id=uuid4(),
                quantity=1,
                cost_pool=1,
                realized_pnl=0,
                version=1,
                last_occurred_at=now(),
            )
        )
        s.flush()


def test_bad_dates_and_decimal_input(client, user, instrument, opening):
    u, i = user(), instrument()
    response = client.post(
        M + "/opening-positions",
        headers=u["headers"] | {"Idempotency-Key": "bad"},
        json={
            "account_id": str(u["account_id"]),
            "instrument_id": str(i),
            "quantity": "NaN",
            "price": "1",
            "occurred_at": "2020-01-01T00:00:00",
        },
    )
    assert response.status_code == 422
    assert "NaN" not in response.text


def test_direct_opening_creates_one_default_account_concurrently(client, user, instrument, db):
    u = user()
    with db.tenant(u["id"]) as s:
        s.delete(s.get(Account, u["account_id"]))
    instruments = [instrument(symbol=f"DIRECT{i}") for i in range(3)]

    def create(i):
        return client.post(
            M + "/opening-positions",
            headers=u["headers"] | {"Idempotency-Key": f"direct-{i}"},
            json={
                "instrument_id": str(instruments[i]),
                "quantity": "2",
                "price": "10",
                "occurred_at": now().isoformat(),
            },
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(create, range(3)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    account_ids = {r.json()["data"]["account_id"] for r in results}
    assert len(account_ids) == 1
    with db.tenant(u["id"]) as s:
        accounts = list(s.scalars(select(Account)))
        assert len(accounts) == 1
        assert accounts[0].name == "默认持仓"
        assert str(accounts[0].id) in account_ids


def test_direct_opening_reuses_existing_account_and_is_idempotent(client, user, instrument):
    u, other = user(), user()
    payload = {
        "instrument_id": str(instrument()),
        "quantity": "2",
        "price": "10",
        "occurred_at": now().isoformat(),
    }
    headers = u["headers"] | {"Idempotency-Key": "direct"}
    first = client.post(M + "/opening-positions", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["data"]["account_id"] == str(u["account_id"])
    repeated = client.post(M + "/opening-positions", headers=headers, json=payload)
    assert repeated.json() == first.json()
    foreign = client.post(
        M + "/opening-positions",
        headers=other["headers"] | {"Idempotency-Key": "foreign"},
        json=payload | {"account_id": str(u["account_id"])},
    )
    assert foreign.status_code == 404
