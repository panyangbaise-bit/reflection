"""Seed only the isolated development stack. Credentials never enter stdout."""

import json
import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update

from portfolio_backend.config import settings
from portfolio_backend.db import Database, now
from portfolio_backend.models import (
    Account,
    AuthSession,
    Consent,
    Entitlement,
    Instrument,
    LatestQuote,
    Portfolio,
    Preferences,
    Report,
    User,
)
from portfolio_backend.schemas import OpeningPosition
from portfolio_backend.services import portfolio
from portfolio_backend.services.auth import issue_tokens

cfg = settings()
if not cfg.simulator_ai or cfg.environment != "development" or cfg.store_environment != "Sandbox":
    raise SystemExit("Requires isolated development simulator configuration")
db = Database(cfg)
with db.control() as s:
    u = s.scalar(select(User).where(User.apple_subject == "local-simulator-fixture"))
    if u is None:
        u = User(apple_subject="local-simulator-fixture", store_environment="Sandbox")
        s.add(u)
        s.flush()
        root = Portfolio(user_id=u.id)
        s.add(root)
        s.flush()
        s.add_all(
            [
                Account(user_id=u.id, portfolio_id=root.id, name="本机合成联调账户"),
                Preferences(user_id=u.id),
                Entitlement(
                    user_id=u.id, expires_at=now() + timedelta(days=30), source="local-fixture"
                ),
            ]
        )
        for purpose in ("interactive_ai", "scheduled_ai"):
            s.add(
                Consent(
                    user_id=u.id,
                    purpose=purpose,
                    provider="local-simulator",
                    disclosure_version=cfg.ai_disclosure_version,
                    granted=True,
                )
            )
    uid = u.id
    # This isolated synthetic user is seeded with the current test disclosure.
    s.execute(
        update(Consent)
        .where(Consent.user_id == uid, Consent.provider == "local-simulator")
        .values(disclosure_version=cfg.ai_disclosure_version, granted=True)
    )
    s.execute(update(AuthSession).where(AuthSession.user_id == uid).values(revoked=True))
    tokens = issue_tokens(s, uid, cfg)
    i = s.scalar(select(Instrument).where(Instrument.symbol == "LOCAL300"))
    if i is None:
        i = Instrument(
            symbol="LOCAL300",
            name="本机合成 ETF",
            market="CN",
            asset_class="etf",
            currency="CNY",
            provider="local-fixture",
            quote_kind="realtime",
            enabled=True,
            commercial_display=True,
            freshness_seconds=3600,
        )
        s.add(i)
        s.flush()
    iid = i.id
    quote = s.get(LatestQuote, iid)
    if quote is None:
        quote = LatestQuote(instrument_id=iid)
        s.add(quote)
    quote.price, quote.provider, quote.kind = "4.2", "local-fixture", "realtime"
    quote.as_of, quote.valid_until, quote.market_state = now(), now() + timedelta(hours=1), "open"
    nav = s.scalar(select(Instrument).where(Instrument.symbol == "SIMNAV"))
    if nav is None:
        nav = Instrument(
            symbol="SIMNAV",
            name="本机合成净值基金",
            market="CN",
            asset_class="fund",
            currency="CNY",
            provider="local-fixture",
            quote_kind="nav",
            enabled=True,
            commercial_display=True,
            freshness_seconds=604800,
        )
        s.add(nav)
        s.flush()
    nav_quote = s.get(LatestQuote, nav.id)
    if nav_quote is None:
        nav_quote = LatestQuote(instrument_id=nav.id)
        s.add(nav_quote)
    nav_quote.price, nav_quote.previous_price = "1.05", "1.00"
    nav_quote.provider, nav_quote.kind, nav_quote.market_state = "local-fixture", "nav", "closed"
    nav_quote.as_of, nav_quote.previous_as_of = (
        now() - timedelta(hours=1),
        now() - timedelta(days=1),
    )
    nav_quote.valid_until = nav_quote.as_of + timedelta(days=7)
with db.tenant(uid) as s:
    account = s.scalar(select(Account).where(Account.user_id == uid))
    from portfolio_backend.models import Position

    if not s.scalar(select(Position).where(Position.user_id == uid)):
        portfolio.open_position(
            s,
            uid,
            OpeningPosition(
                account_id=account.id,
                instrument_id=iid,
                quantity="100",
                price="4",
                occurred_at=now(),
            ),
            str(uuid4()),
        )
    data = portfolio.snapshot(s, uid)
    if not s.scalar(select(Report).where(Report.user_id == uid)):
        s.add(
            Report(
                user_id=uid,
                kind="daily",
                period=now().date().isoformat(),
                title="本机合成复盘",
                summary="用于模拟器功能走查的合成持仓和报价，不代表真实资产。",
                snapshot=data,
            )
        )
# Compose extracts this file using docker cp; API/worker never receive host paths.

fd = os.open("/tmp/simulator-session.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(
        {
            "origin": "http://127.0.0.1:8000",
            "tokens": {
                "accessToken": tokens["access_token"],
                "refreshToken": tokens["refresh_token"],
            },
        },
        f,
    )
db.close()
print("Local synthetic account ready. Session saved privately in /tmp/simulator-session.json.")
