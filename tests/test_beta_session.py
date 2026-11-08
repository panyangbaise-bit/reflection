import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select

from portfolio_backend.models import Account, AuthSession, Instrument, Position, Report
from portfolio_backend.services.auth import AuthService

spec = importlib.util.spec_from_file_location(
    "beta_session", Path(__file__).parents[1] / "scripts/provision-beta-session.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_beta_provisioning_is_empty_scoped_and_revocable(db, cfg, user, client):
    other = user()
    first = module.provision(db, cfg, "simulator-test")
    auth = AuthService(db, cfg)
    owner = auth.authenticate(first["tokens"]["accessToken"])
    with db.control() as s:
        for model in (Position, Instrument, Report):
            assert s.scalar(select(func.count()).select_from(model)) == 0
        assert (
            s.scalar(
                select(func.count()).select_from(Account).where(Account.user_id == owner.user_id)
            )
            == 0
        )
    second = module.provision(db, cfg, "simulator-test")
    assert auth.authenticate(second["tokens"]["accessToken"]).user_id == owner.user_id
    with db.control() as s:
        assert s.get(AuthSession, owner.session_id).revoked
    assert auth.authenticate(other["access_token"])
    headers = {"Authorization": "Bearer " + second["tokens"]["accessToken"]}
    # Provisioning a legacy test session must not bypass the required phone binding.
    assert client.get("/api/mobile/v1/me", headers=headers).json()["data"]["phone_masked"] is None
    assert client.get("/api/mobile/v1/home", headers=headers).status_code == 403
    module.provision(db, cfg, "simulator-test", revoke=True)
    assert client.get("/api/mobile/v1/me", headers=headers).status_code == 401
    assert auth.authenticate(other["access_token"])


def test_beta_provisioning_refuses_production_store_and_invalid_labels(cfg):
    with pytest.raises(ValueError, match="Sandbox"):
        module.provision(None, cfg.model_copy(update={"store_environment": "Production"}), "test")
    for label in ("", "../real-user", "AppleSubject", "a" * 65):
        with pytest.raises(ValueError, match="label"):
            module.provision(None, cfg, label)
