"""SSH/operator-only Sandbox account provisioning. Creates no asset or market fixtures."""

import argparse
import json
import os
import re
from pathlib import Path

from sqlalchemy import select, text, update

from portfolio_backend.config import settings
from portfolio_backend.db import Database
from portfolio_backend.models import AuthSession, Portfolio, Preferences, User
from portfolio_backend.services.auth import issue_tokens

BETA_ORIGIN = "https://api.example.com"


def provision(db, cfg, account, revoke=False):
    if cfg.store_environment != "Sandbox":
        raise ValueError("Operator beta accounts are only allowed in the Sandbox deployment")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", account):
        raise ValueError("Use a lowercase beta account label, 3-64 characters")
    subject = "operator-beta:" + account
    with db.control() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": subject},
        )
        user = session.scalar(select(User).where(User.apple_subject == subject))
        if user is None:
            if revoke:
                return None
            user = User(apple_subject=subject, store_environment="Sandbox")
            session.add(user)
            session.flush()
            session.add_all([Portfolio(user_id=user.id), Preferences(user_id=user.id)])
        if user.status != "active" or user.store_environment != "Sandbox":
            raise ValueError("Beta account is not active in Sandbox")
        session.execute(
            update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True)
        )
        if revoke:
            return None
        tokens = issue_tokens(session, user.id, cfg)
        return {
            "origin": BETA_ORIGIN,
            "tokens": {
                "accessToken": tokens["access_token"],
                "refreshToken": tokens["refresh_token"],
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    if not args.revoke and args.output is None:
        parser.error("--output is required when provisioning")
    cfg = settings()
    db = Database(cfg)
    db.verify_roles()
    try:
        if args.revoke:
            provision(db, cfg, args.account, revoke=True)
            print("Beta account sessions revoked; account data retained.")
            return
        # Refuse existing files/symlinks, and never print credentials.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(provision(db, cfg, args.account), handle)
        except Exception:
            args.output.unlink(missing_ok=True)
            raise
        print("Ordinary Sandbox user session saved privately; no fixtures or Pro grant created.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
