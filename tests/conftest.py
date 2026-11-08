from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text

from portfolio_backend.api import create_app
from portfolio_backend.config import Settings
from portfolio_backend.db import Database, now
from portfolio_backend.models import (
    Account,
    Base,
    Instrument,
    LatestQuote,
    Portfolio,
    Preferences,
    User,
)
from portfolio_backend.services.auth import digest, issue_tokens


@pytest.fixture(scope="session")
def cfg():
    return Settings(
        environment="test",
        catalog_live_search_enabled=False,
        database_url="postgresql+psycopg://pa_app:local-app-only@localhost:55432/portfolio_test",
        control_database_url="postgresql+psycopg://pa_control:local-control-only@localhost:55432/portfolio_test",
        migration_database_url="postgresql+psycopg://postgres:local-owner-only@localhost:55432/portfolio_test",
        database_statement_timeout_ms=60000,
        cache_url="redis://localhost:56379/15",
        broker_url="redis://localhost:56380/15",
        ai_gateway_url="https://test-provider.invalid/answer",
        ai_provider_id="test-provider",
        ai_disclosure_version="1",
        admin_credentials={
            digest("test-admin-credential"): {"name": "test-operator", "role": "owner"},
            digest("test-support-credential"): {"name": "support", "role": "support"},
        },
    )


@pytest.fixture
def db(cfg):
    assert cfg.migration_database_url.endswith("/portfolio_test")
    owner = create_engine(cfg.migration_database_url)
    with owner.begin() as connection:
        connection.execute(text("TRUNCATE " + ",".join(Base.metadata.tables) + " CASCADE"))
    owner.dispose()
    cache = Redis.from_url(cfg.cache_url)
    cache.flushdb()
    cache.close()
    database = Database(cfg)
    database.verify_roles()
    yield database
    database.close()


@pytest.fixture
def client(db, cfg):
    with TestClient(create_app(cfg, database=db)) as client:
        yield client


@pytest.fixture
def user(db, cfg):
    def create():
        with db.control() as s:
            user = User(
                apple_subject=str(uuid4()),
                phone="139" + str(uuid4().int % 100000000).zfill(8),
                store_environment="Sandbox",
            )
            s.add(user)
            s.flush()
            root = Portfolio(user_id=user.id)
            s.add(root)
            s.flush()
            account = Account(user_id=user.id, portfolio_id=root.id, name="测试账户")
            s.add_all([account, Preferences(user_id=user.id)])
            tokens = issue_tokens(s, user.id, cfg)
            s.flush()
            return {
                "id": user.id,
                "account_id": account.id,
                **tokens,
                "headers": {"Authorization": "Bearer " + tokens["access_token"]},
            }

    return create


@pytest.fixture
def instrument(db):
    def create(**overrides):
        data = dict(
            symbol="510300",
            name="沪深300 ETF",
            market="CN",
            asset_class="etf",
            currency="CNY",
            provider="fixture",
            quote_kind="realtime",
            enabled=True,
            commercial_display=True,
            freshness_seconds=3600,
        )
        data.update(overrides)
        with db.control() as s:
            row = Instrument(**data)
            s.add(row)
            s.flush()
            s.add(
                LatestQuote(
                    instrument_id=row.id,
                    price="12",
                    provider=row.provider,
                    kind=row.quote_kind,
                    as_of=now(),
                    valid_until=now() + timedelta(hours=1),
                )
            )
            return row.id

    return create


@pytest.fixture
def opening(client):
    def create(user, instrument_id, **overrides):
        payload = {
            "account_id": str(user["account_id"]),
            "instrument_id": str(instrument_id),
            "quantity": "100",
            "price": "10",
            "occurred_at": now().isoformat(),
        }
        payload.update(overrides)
        response = client.post(
            "/api/mobile/v1/opening-positions",
            json=payload,
            headers=user["headers"] | {"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return create
