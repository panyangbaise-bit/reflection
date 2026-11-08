"""Explicit local fixture creation. Never called by API startup or migrations."""

import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.models import Account, Instrument, LatestQuote, Portfolio, Preferences, User
from portfolio_backend.services.auth import issue_tokens


def main():
    cfg = settings()
    if cfg.environment != "development":
        raise SystemExit("Sample data can only be created in development")
    db = Database(cfg)
    db.verify_roles()
    with db.control() as s:
        user = s.scalar(select(User).where(User.apple_subject == "local-development-only"))
        if user is None:
            user = User(apple_subject="local-development-only", store_environment="Sandbox")
            s.add(user)
            s.flush()
            root = Portfolio(user_id=user.id)
            s.add(root)
            s.flush()
            s.add_all(
                [
                    Account(user_id=user.id, portfolio_id=root.id, name="本地样例账户"),
                    Preferences(user_id=user.id),
                ]
            )
        sample = s.scalar(select(Instrument).where(Instrument.provider == "local-fixture"))
        if sample is None:
            sample = Instrument(
                # Deliberately not a real AKShare symbol: the auto catalog may already
                # own 510300, and the sample quote must not masquerade as market data.
                symbol="LOCAL300",
                name="样例 · 本地合成数据",
                market="CN",
                asset_class="etf",
                currency="CNY",
                provider="local-fixture",
                quote_kind="realtime",
                enabled=True,
                commercial_display=True,
                freshness_seconds=3600,
            )
            s.add(sample)
            s.flush()
        quote = s.get(LatestQuote, sample.id)
        if quote is None:
            quote = LatestQuote(instrument_id=sample.id)
            s.add(quote)
        quote.price, quote.provider, quote.kind = "4.2", "local-fixture", "realtime"
        quote.as_of, quote.valid_until, quote.market_state = (
            now(),
            now() + timedelta(hours=1),
            "open",
        )
        tokens = issue_tokens(s, user.id, cfg)
        tokens["sample_instrument_id"] = str(sample.id)
    directory = Path("secrets")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "development-session.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(tokens, handle, indent=2)
    db.close()
    print(f"Local sample session saved to {path}; token expires in {cfg.access_seconds} seconds.")
