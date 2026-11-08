import importlib.util
import stat
from pathlib import Path

import pytest

from portfolio_backend.config import Settings

spec = importlib.util.spec_from_file_location("beta_prepare", Path("deploy/beta/prepare.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_beta_secrets_are_private_separate_and_never_overwritten(tmp_path):
    directory = tmp_path / "beta"
    module.prepare(directory, "api-test.example.com", "admin-test.example.com")
    files = {p.name: p.read_text() for p in directory.iterdir()}
    runtime = Settings(_env_file=directory / "runtime.env")
    assert runtime.environment == "production" and runtime.store_environment == "Sandbox"
    assert runtime.database_pool_size == runtime.control_database_pool_size == 2
    assert runtime.database_max_overflow == 0
    owner = files["postgres.env"].split("POSTGRES_PASSWORD=", 1)[1].splitlines()[0]
    assert owner not in files["runtime.env"] and owner in files["migration.env"]
    assert "local-" not in files["runtime.env"]
    assert not runtime.deepseek_api_key.get_secret_value() and not runtime.data_collection_enabled
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
    with pytest.raises(FileExistsError):
        module.prepare(directory, "api-test.example.com", "admin-test.example.com")
    assert files == {p.name: p.read_text() for p in directory.iterdir()}


@pytest.mark.parametrize("domain", ["https://api.example.com", "evil.com\nKEY=value", "a/b"])
def test_beta_config_rejects_injected_or_non_hostname_values(tmp_path, domain):
    with pytest.raises(ValueError):
        module.prepare(tmp_path / "beta", domain, "admin-test.example.com")
    assert not (tmp_path / "beta").exists()


@pytest.mark.parametrize(
    "admin_domain", ["https://admin.example.com", "evil.com\nKEY=value", "a/b"]
)
def test_beta_config_rejects_injected_or_non_hostname_admin_domains(tmp_path, admin_domain):
    with pytest.raises(ValueError):
        module.prepare(tmp_path / "beta", "api-test.example.com", admin_domain)
    assert not (tmp_path / "beta").exists()
