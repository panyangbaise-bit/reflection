from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import AuthSession, DeletionRequest, Job, Position, User
from portfolio_backend.services import jobs
from portfolio_backend.services.auth import AppleIdentityVerifier, digest
from portfolio_backend.worker import execute

M = "/api/mobile/v1"


def test_refresh_replay_revokes_family(client, user):
    u = user()
    response = client.post(M + "/auth/refresh", json={"refresh_token": u["refresh_token"]})
    assert response.status_code == 200
    next_token = response.json()["data"]["access_token"]
    assert client.get(M + "/me", headers=u["headers"]).status_code == 401
    assert (
        client.get(M + "/me", headers={"Authorization": "Bearer " + next_token}).status_code == 200
    )
    response = client.post(M + "/auth/refresh", json={"refresh_token": u["refresh_token"]})
    assert response.status_code == 401
    assert (
        client.get(M + "/me", headers={"Authorization": "Bearer " + next_token}).status_code == 401
    )


def test_apple_nonce_and_signature_validation(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = AppleIdentityVerifier("com.portfolioagent.ios")

    class Key:
        pass

    parsed = Key()
    parsed.key = key.public_key()
    monkeypatch.setattr(verifier.keys, "get_signing_key_from_jwt", lambda _: parsed)
    claims = {
        "iss": "https://appleid.apple.com",
        "sub": "apple-user",
        "aud": "com.portfolioagent.ios",
        "iat": now(),
        "exp": now() + timedelta(minutes=5),
        "nonce": digest("nonce"),
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert verifier.verify(token, digest("nonce")) == "apple-user"
    with pytest.raises(DomainError):
        verifier.verify(token, digest("wrong-nonce"))
    with pytest.raises(DomainError):
        verifier.verify(
            jwt.encode(claims | {"aud": "wrong-app"}, key, algorithm="RS256"), digest("nonce")
        )


def test_deletion_revokes_immediately_then_erases(client, db, cfg, user, instrument, opening):
    u = user()
    with db.control() as s:
        row = s.get(User, u["id"])
        row.phone, row.password_hash = "13800138000", "test-hash"
    opening(u, instrument())
    response = client.post(
        M + "/me/deletion-requests", headers=u["headers"], json={"confirmation": "DELETE"}
    )
    assert response.status_code == 202, response.text
    receipt = response.json()["data"]["receipt"]
    assert client.get(M + "/me", headers=u["headers"]).status_code == 401
    with db.tenant(u["id"]) as s:
        assert s.scalars(select(Position)).all() == []
    jobs.materialize_outbox(db)
    with db.control() as s:
        job = s.scalar(select(Job).where(Job.kind == "delete_account"))
        job_id = job.id
    assert execute(db, job_id, cfg) == "succeeded"
    with db.control() as s:
        assert s.scalars(select(Position)).all() == []
        assert s.scalars(select(AuthSession)).all() == []
        deleted = s.get(User, u["id"])
        assert deleted.status == "deleted"
        assert deleted.phone is None and deleted.password_hash is None
        assert s.scalar(select(DeletionRequest)).status == "completed"
    status = client.post(M + "/auth/deletion-status", json={"refresh_token": receipt})
    assert status.json()["data"]["status"] == "completed"


def test_refresh_does_not_bypass_deletion_reauthentication(client, db, user):
    u = user()
    with db.control() as s:
        row = s.scalar(select(AuthSession).where(AuthSession.user_id == u["id"]))
        row.authenticated_at = now() - timedelta(hours=1)
    token = client.post(M + "/auth/refresh", json={"refresh_token": u["refresh_token"]}).json()[
        "data"
    ]["access_token"]
    response = client.post(
        M + "/me/deletion-requests",
        headers={"Authorization": "Bearer " + token},
        json={"confirmation": "DELETE"},
    )
    assert response.status_code == 403


def test_no_development_login_or_removed_store_routes(client, user):
    assert client.post(M + "/auth/dev").status_code == 404
    u = user()
    assert (
        client.post(
            M + "/subscriptions/apple/sync",
            headers=u["headers"],
            json={"signed_transaction": "unverified"},
        ).status_code
        == 404
    )
    assert client.get(M + "/me/entitlements", headers=u["headers"]).status_code == 404
