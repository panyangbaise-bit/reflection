import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import jwt
from sqlalchemy import select, text, update

from portfolio_backend.config import Settings
from portfolio_backend.db import Database, now
from portfolio_backend.errors import DomainError, require
from portfolio_backend.models import (
    AuthSession,
    LoginChallenge,
    Portfolio,
    Preferences,
    User,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID
    authenticated_at: object


class AppleIdentityVerifier:
    def __init__(self, bundle_id):
        self.bundle_id = bundle_id
        self.keys = jwt.PyJWKClient("https://appleid.apple.com/auth/keys", timeout=5)

    def verify(self, token, nonce_hash):
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.bundle_id,
                issuer="https://appleid.apple.com",
                options={"require": ["exp", "iat", "sub", "nonce"]},
            )
            require(
                secrets.compare_digest(claims["nonce"], nonce_hash),
                "invalid_identity",
                "Apple 登录校验失败",
                401,
            )
            return claims["sub"]
        except jwt.PyJWTError as exc:
            raise DomainError("invalid_identity", "Apple 登录校验失败", 401) from exc


def issue_tokens(session, user_id, config: Settings, family_id=None, authenticated_at=None):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    row = AuthSession(
        user_id=user_id,
        family_id=family_id or uuid4(),
        access_hash=digest(access),
        refresh_hash=digest(refresh),
        access_expires_at=now() + timedelta(seconds=config.access_seconds),
        expires_at=now() + timedelta(days=config.refresh_days),
        authenticated_at=authenticated_at or now(),
    )
    session.add(row)
    session.flush()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": config.access_seconds,
        "user_id": str(user_id),
        "app_account_token": str(user_id),
    }


class AuthService:
    def __init__(self, db: Database, config: Settings, verifier=None):
        self.db, self.config = db, config
        self.verifier = verifier or AppleIdentityVerifier(config.apple_bundle_id)

    def challenge(self):
        nonce = secrets.token_urlsafe(32)
        with self.db.control() as s:
            row = LoginChallenge(nonce_hash=digest(nonce), expires_at=now() + timedelta(minutes=5))
            s.add(row)
            s.flush()
            return {
                "challenge_id": str(row.id),
                "nonce": nonce,
                "apple_request_nonce": row.nonce_hash,
                "expires_in": 300,
            }

    def login(self, challenge_id, identity_token):
        with self.db.control() as s:
            challenge = s.get(LoginChallenge, challenge_id)
            require(
                challenge and not challenge.consumed and challenge.expires_at > now(),
                "invalid_challenge",
                "登录请求已失效",
                401,
            )
            nonce_hash = challenge.nonce_hash
        # Remote key lookup never holds a database connection/lock.
        subject = self.verifier.verify(identity_token, nonce_hash)
        with self.db.control() as s:
            challenge = s.scalar(
                select(LoginChallenge).where(LoginChallenge.id == challenge_id).with_for_update()
            )
            require(
                challenge and not challenge.consumed and challenge.expires_at > now(),
                "invalid_challenge",
                "登录请求已失效",
                401,
            )
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "identity:" + subject},
            )
            user = s.scalar(select(User).where(User.apple_subject == subject))
            if user is None:
                user = User(apple_subject=subject, store_environment=self.config.store_environment)
                s.add(user)
                s.flush()
                s.add_all([Portfolio(user_id=user.id), Preferences(user_id=user.id)])
            require(user.status == "active", "account_unavailable", "账户不可用", 403)
            challenge.consumed = True
            return issue_tokens(s, user.id, self.config)

    def authenticate(self, token) -> Principal:
        with self.db.control() as s:
            row = s.scalar(
                select(AuthSession)
                .join(User)
                .where(
                    AuthSession.access_hash == digest(token),
                    AuthSession.revoked.is_(False),
                    AuthSession.access_expires_at > now(),
                    User.status == "active",
                )
            )
            require(row is not None, "unauthorized", "请重新登录", 401)
            return Principal(row.user_id, row.id, row.authenticated_at)

    def refresh(self, token):
        replay = False
        with self.db.control() as s:
            family = s.scalar(
                select(AuthSession.family_id).where(AuthSession.refresh_hash == digest(token))
            )
            require(family is not None, "unauthorized", "请重新登录", 401)
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "session-family:" + str(family)},
            )
            row = s.scalar(
                select(AuthSession)
                .where(AuthSession.refresh_hash == digest(token))
                .with_for_update()
            )
            require(row and row.expires_at > now(), "unauthorized", "请重新登录", 401)
            if row.revoked:
                # Commit family revocation even though the request is rejected.
                s.execute(
                    update(AuthSession)
                    .where(AuthSession.family_id == row.family_id)
                    .values(revoked=True)
                )
                replay = True
            else:
                user = s.get(User, row.user_id)
                require(user.status == "active", "account_unavailable", "账户不可用", 403)
                row.revoked = True
                result = issue_tokens(
                    s, row.user_id, self.config, row.family_id, row.authenticated_at
                )
        require(not replay, "refresh_reused", "登录凭据已被使用，请重新登录", 401)
        return result

    def logout(self, principal):
        with self.db.control() as s:
            row = s.get(AuthSession, principal.session_id)
            require(row is not None, "unauthorized", "请重新登录", 401)
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "session-family:" + str(row.family_id)},
            )
            s.execute(
                update(AuthSession)
                .where(AuthSession.family_id == row.family_id)
                .values(revoked=True)
            )
