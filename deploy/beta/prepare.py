"""Generate fresh beta-only credentials locally on the server. Never overwrite existing secrets."""

import argparse
import os
import re
import secrets
from pathlib import Path


def prepare(directory, domain, admin_domain):
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain
    ):
        raise ValueError("Use a DNS hostname, without https:// or a path")
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", admin_domain
    ):
        raise ValueError("Use a DNS hostname for --admin-domain, without https:// or a path")
    # Exclusive directory creation prevents partial or accidental credential rotation.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    owner, app, control = (secrets.token_hex(24) for _ in range(3))
    files = {
        "compose.env": f"BETA_DOMAIN={domain}\nADMIN_DOMAIN={admin_domain}\n",
        "postgres.env": (
            "POSTGRES_USER=postgres\nPOSTGRES_DB=portfolio\n"
            f"POSTGRES_PASSWORD={owner}\nAPP_DB_PASSWORD={app}\nCONTROL_DB_PASSWORD={control}\n"
        ),
        "migration.env": (
            f"PA_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:{owner}@postgres:5432/portfolio\n"
        ),
        "runtime.env": (
            "PA_ENVIRONMENT=production\nPA_STORE_ENVIRONMENT=Sandbox\n"
            f"PA_DATABASE_URL=postgresql+psycopg://pa_app:{app}@postgres:5432/portfolio\n"
            f"PA_CONTROL_DATABASE_URL=postgresql+psycopg://pa_control:{control}@postgres:5432/portfolio\n"
            "PA_DATABASE_POOL_SIZE=2\nPA_CONTROL_DATABASE_POOL_SIZE=2\nPA_DATABASE_MAX_OVERFLOW=0\n"
            "PA_CACHE_URL=redis://cache:6379/0\nPA_AUTH_CACHE_URL=redis://auth-cache:6379/0\nPA_BROKER_URL=redis://broker:6379/0\n"
            "PA_APPLE_BUNDLE_ID=com.portfolioagent.ios\n"
            "PA_DEEPSEEK_API_KEY=\nPA_AI_PROVIDER_ID=deepseek\nPA_DEEPSEEK_MODEL=deepseek-flash\n"
            "PA_AI_MAX_OUTPUT_TOKENS=1024\nPA_AI_MARKET_DATA_PROVIDERS=[]\n"
            "PA_ADMIN_CREDENTIALS={}\nPA_DATA_COLLECTION_ENABLED=false\n"
            "PA_DATA_MAX_INSTRUMENTS_PER_TICK=5\nPA_QUOTE_POLL_SECONDS=120\n"
            "PA_NEWS_POLL_SECONDS=300\nPA_SEARCH_DAILY_LIMIT=20\n"
            "PA_COMMERCIAL_DATA_PROVIDERS=[]\nPA_NEWS_DISPLAY_PROVIDERS=[]\n"
        ),
    }
    for name, value in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
    print(f"Created four private configuration files in {directory}; no secrets printed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="api.example.com")
    parser.add_argument("--admin-domain", default="admin.example.com")
    parser.add_argument("--directory", type=Path, default=Path("secrets/beta"))
    args = parser.parse_args()
    prepare(args.directory, args.domain, args.admin_domain)
