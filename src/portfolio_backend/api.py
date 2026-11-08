import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from portfolio_backend import schemas as S
from portfolio_backend.config import Settings, settings
from portfolio_backend.db import Database, now
from portfolio_backend.errors import DomainError, require
from portfolio_backend.models import (
    Account,
    AgentToolCall,
    AIRun,
    AuditLog,
    AuthSession,
    Consent,
    Conversation,
    DeletionRequest,
    Device,
    Instrument,
    Job,
    LatestQuote,
    LedgerEvent,
    Message,
    Outbox,
    Position,
    Preferences,
    Report,
    TradingDiscipline,
    User,
)
from portfolio_backend.services import ai, news, portfolio
from portfolio_backend.services.auth import AuthService, digest
from portfolio_backend.services.catalog import CatalogService
from portfolio_backend.services.common import idempotent, lock_portfolio, owned
from portfolio_backend.services.phone_auth import PhoneAuthService

bearer = HTTPBearer(auto_error=False)
MOBILE = "/api/mobile/v1"
ADMIN = "/api/admin/v1"
REPORT_KINDS = ("daily", "weekly", "daily_cn", "daily_us")
AI_JOB_KINDS = ("answer", "daily", "weekly", "daily_cn", "daily_us")


def result(data, status=200):
    return JSONResponse(
        {"data": jsonable_encoder(data, custom_encoder={Decimal: str})}, status_code=status
    )


def principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    require(credentials and credentials.scheme.lower() == "bearer", "unauthorized", "请先登录", 401)
    p = request.app.state.auth.authenticate(credentials.credentials)
    migration_paths = {
        MOBILE + path
        for path in ["/me", "/me/phone/code", "/me/phone", "/me/phone/carrier", "/auth/logout"]
    }
    if request.url.path not in migration_paths:
        with request.app.state.db.control() as s:
            identity = s.get(User, p.user_id)
            require(
                identity.phone is not None,
                "phone_required",
                "请先验证并绑定手机号",
                403,
            )
            if request.url.path not in {
                MOBILE + path
                for path in [
                    "/me/password/setup",
                    "/me/password/code",
                    "/me/password",
                    "/me/password/carrier",
                ]
            }:
                require(
                    not identity.password_setup_required,
                    "password_setup_required",
                    "请先设置登录密码",
                    403,
                )
    return p


def operator(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    require(credentials is not None, "unauthorized", "需要管理员身份", 401)
    token_hash = digest(credentials.credentials)
    for expected, identity in request.app.state.config.admin_credentials.items():
        if secrets.compare_digest(token_hash, expected):
            require(
                identity.get("role") in {"owner", "operations", "support"},
                "forbidden",
                "无效管理员角色",
                403,
            )
            return identity
    raise DomainError("forbidden", "管理员凭据无效", 403)


def admin_permission(identity, *roles):
    require(identity["role"] in {"owner", *roles}, "forbidden", "没有此操作权限", 403)


def audit(session, identity, action, reason, target=None, details=None):
    session.add(
        AuditLog(
            actor=identity["name"],
            action=action,
            reason=reason,
            target_id=str(target) if target else None,
            details=details or {},
        )
    )


def create_app(config: Settings | None = None, database=None, identity_verifier=None):
    config = config or settings()
    db = database or Database(config)
    auth = AuthService(db, config, identity_verifier)
    cache = Redis.from_url(config.cache_url, socket_connect_timeout=1, socket_timeout=1)

    auth_cache = (
        Redis.from_url(config.auth_cache_url, socket_connect_timeout=1, socket_timeout=1)
        if config.auth_cache_url
        else cache
    )

    @asynccontextmanager
    async def lifespan(app):
        db.verify_roles()
        yield
        cache.close()
        if auth_cache is not cache:
            auth_cache.close()
        if database is None:
            db.close()

    app = FastAPI(
        title="Portfolio Agent Mobile API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if config.environment != "production" else None,
    )
    app.state.db, app.state.config, app.state.auth = db, config, auth
    phone_auth = PhoneAuthService(db, config, auth_cache)
    app.state.phone_auth = phone_auth
    catalog = CatalogService(config, db, cache)
    app.state.catalog = catalog

    @app.middleware("http")
    async def request_context(request, call_next):
        request_id = str(uuid4())
        # Fixed window enforced atomically in shared Redis. No financial/identity values in keys.
        if request.url.path.startswith(MOBILE + "/auth/"):
            key = "auth-rate:" + digest(request.client.host if request.client else "unknown")
            try:
                count = await asyncio.to_thread(
                    auth_cache.eval,
                    "local n=redis.call('INCR',KEYS[1]); "
                    "if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
                    1,
                    key,
                )
                if count > 60:
                    return JSONResponse(
                        {"error": {"code": "rate_limited", "message": "请稍后重试"}},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
            except RedisError:
                return JSONResponse(
                    {"error": {"code": "auth_unavailable", "message": "登录暂不可用"}},
                    status_code=503,
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        headers = {"Retry-After": "5"} if exc.status in {429, 503} else None
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Never echo tokens, questions or financial inputs back into validation diagnostics.
        fields = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        return JSONResponse(
            {"error": {"code": "invalid_request", "fields": fields}}, status_code=422
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(
            {"error": {"code": "conflict", "message": "记录冲突，请刷新后重试"}}, status_code=409
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with db.control() as session:
            session.execute(text("SELECT 1"))
        cache.ping()
        if auth_cache is not cache:
            auth_cache.ping()
        return {"status": "ready"}

    def client_ip(request):
        # Never trust client-supplied X-Forwarded-For. Configure trusted proxy IPs in uvicorn.
        return request.client.host if request.client else "unknown"

    @app.post(MOBILE + "/auth/test-phone/login")
    def test_phone_login(body: S.PhoneCode):
        return result(phone_auth.test_phone_login(body.phone))

    @app.post(MOBILE + "/auth/sms/code")
    def sms_code(body: S.PhoneCode, request: Request):
        return result(phone_auth.send_code(body.phone, client_ip(request)), 201)

    @app.post(MOBILE + "/auth/sms/login")
    def sms_login(body: S.PhoneLogin):
        return result(phone_auth.sms_login(body.challenge_id, body.code))

    @app.post(MOBILE + "/auth/password/login")
    def password_login(body: S.PasswordLogin, request: Request):
        return result(phone_auth.password_login(body.phone, body.password, client_ip(request)))

    @app.post(MOBILE + "/me/password/setup")
    def setup_password(body: S.PasswordSetup, p=Depends(principal)):
        return result(phone_auth.setup_password(p, body.password))

    @app.post(MOBILE + "/auth/phone/recovery-code")
    def recovery_code(body: S.PhoneRecovery, request: Request):
        return result(
            phone_auth.recovery_code(body.phone, body.password, body.new_phone, client_ip(request)),
            201,
        )

    @app.post(MOBILE + "/auth/phone/change")
    def change_phone(body: S.PhoneLogin):
        return result(phone_auth.change_phone(body.challenge_id, body.code))

    @app.post(MOBILE + "/me/phone/old-code")
    def old_phone_code(request: Request, p=Depends(principal)):
        return result(
            phone_auth.send_code(
                phone_auth.account_phone(p), client_ip(request), "phone-change-old", p.user_id
            ),
            201,
        )

    @app.post(MOBILE + "/me/phone/change-code")
    def change_phone_code(body: S.PhoneChangeCode, request: Request, p=Depends(principal)):
        return result(
            phone_auth.sms_change_code(
                p, body.challenge_id, body.code, body.new_phone, client_ip(request)
            ),
            201,
        )

    @app.post(MOBILE + "/auth/carrier/login")
    def carrier_login(body: S.CarrierLogin, request: Request):
        return result(phone_auth.carrier_login(body.access_token, client_ip(request)))

    @app.post(MOBILE + "/me/phone/carrier")
    def carrier_bind(body: S.CarrierLogin, request: Request, p=Depends(principal)):
        return result(phone_auth.carrier_bind(p, body.access_token, client_ip(request)))

    @app.post(MOBILE + "/me/password/carrier")
    def carrier_password(body: S.CarrierPassword, request: Request, p=Depends(principal)):
        return result(
            phone_auth.carrier_verify_account(
                p, body.access_token, client_ip(request), body.password
            )
        )

    @app.post(MOBILE + "/me/reauth/carrier")
    def carrier_reauth(body: S.CarrierLogin, request: Request, p=Depends(principal)):
        return result(phone_auth.carrier_verify_account(p, body.access_token, client_ip(request)))

    @app.post(MOBILE + "/auth/carrier/change")
    def carrier_change(body: S.CarrierPhoneChange, request: Request):
        return result(
            phone_auth.carrier_change_phone(
                body.phone, body.password, body.access_token, client_ip(request)
            )
        )

    @app.post(MOBILE + "/me/phone/code")
    def bind_code(body: S.PhoneCode, request: Request, p=Depends(principal)):
        with db.control() as s:
            require(
                s.get(User, p.user_id).phone is None, "phone_already_bound", "已绑定手机号", 409
            )
        return result(phone_auth.send_code(body.phone, client_ip(request), "bind", p.user_id), 201)

    @app.post(MOBILE + "/me/phone")
    def bind_phone(body: S.PhoneProof, p=Depends(principal)):
        return result(phone_auth.bind_phone(p, body.challenge_id, body.code))

    @app.post(MOBILE + "/me/password/code")
    def password_code(request: Request, p=Depends(principal)):
        return result(
            phone_auth.send_code(
                phone_auth.account_phone(p), client_ip(request), "password", p.user_id
            ),
            201,
        )

    @app.post(MOBILE + "/me/password")
    def set_password(body: S.PasswordSet, p=Depends(principal)):
        return result(phone_auth.set_password(p, body.challenge_id, body.code, body.password))

    @app.post(MOBILE + "/me/reauth/code")
    def reauth_code(request: Request, p=Depends(principal)):
        return result(
            phone_auth.send_code(
                phone_auth.account_phone(p), client_ip(request), "reauth", p.user_id
            ),
            201,
        )

    @app.post(MOBILE + "/me/reauth")
    def reauth(body: S.PhoneProof, p=Depends(principal)):
        return result(phone_auth.reauthenticate(p, body.challenge_id, body.code))

    @app.post(MOBILE + "/auth/refresh")
    def refresh(body: S.Refresh):
        return result(auth.refresh(body.refresh_token))

    @app.post(MOBILE + "/auth/logout")
    def logout(p=Depends(principal)):
        auth.logout(p)
        # Shared devices are rebound only through an authenticated registration request.
        with db.tenant(p.user_id) as s:
            s.execute(update(Device).where(Device.user_id == p.user_id).values(enabled=False))
        return result({"logged_out": True})

    @app.get(MOBILE + "/me")
    def me(p=Depends(principal)):
        with db.control() as s:
            user = s.get(User, p.user_id)
            return result(
                {
                    "id": p.user_id,
                    "app_account_token": p.user_id,
                    "store_environment": config.store_environment,
                    "phone_masked": user.phone[:3] + "****" + user.phone[-4:]
                    if user.phone
                    else None,
                    "has_password": user.password_hash is not None,
                    "password_setup_required": user.password_setup_required,
                }
            )

    @app.get(MOBILE + "/accounts")
    def accounts(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            rows = s.scalars(
                select(Account).where(Account.user_id == p.user_id).order_by(Account.id)
            )
            return result(
                [{"id": r.id, "name": r.name, "portfolio_id": r.portfolio_id} for r in rows]
            )

    @app.post(MOBILE + "/accounts", status_code=201)
    def add_account(
        body: S.AccountCreate, key: str = Header(alias="Idempotency-Key"), p=Depends(principal)
    ):
        with db.tenant(p.user_id) as s:

            def action():
                root = lock_portfolio(s, p.user_id)
                row = Account(user_id=p.user_id, portfolio_id=root.id, name=body.name)
                s.add(row)
                s.flush()
                return {"id": str(row.id), "name": row.name, "portfolio_id": str(root.id)}

            data = idempotent(s, p.user_id, "account", key, body.model_dump(), action)
        return result(data, 201)

    @app.get(MOBILE + "/instruments")
    def instruments(
        query: str = Query(default="", max_length=80),
        cursor: UUID | None = None,
        limit: int = Query(30, ge=1, le=100),
        p=Depends(principal),
    ):
        query = query.strip()
        if query:
            rows = catalog.search(query, limit)
            # Exact tickers stay local and fast. Name/pinyin searches may need
            # cross-market aliases even when a local substring already matches.
            from portfolio_backend.services.catalog import search_term

            term, _ = search_term(query)
            if not any(row.symbol.casefold() == term.casefold() for row in rows):
                catalog.discover(query)
                rows = catalog.search(query, limit)
            if not rows:
                catalog.ready_or_request(query)
                return result({"items": [], "next_cursor": None})
            ids = [row.id for row in rows]
            with db.control() as s:
                marks = {
                    quote.instrument_id: quote
                    for quote in s.scalars(
                        select(LatestQuote).where(LatestQuote.instrument_id.in_(ids))
                    )
                }
            return result(
                {
                    "items": [
                        portfolio.instrument_view(row, marks.get(row.id), now()) for row in rows
                    ],
                    "next_cursor": None,
                }
            )
        with db.tenant(p.user_id) as s:
            q = select(Instrument, LatestQuote).outerjoin(LatestQuote).order_by(Instrument.id)
            if cursor:
                q = q.where(Instrument.id > cursor)
            rows = s.execute(q.limit(limit + 1)).all()
            return result(
                {
                    "items": [
                        portfolio.instrument_view(i, mark, now()) for i, mark in rows[:limit]
                    ],
                    "next_cursor": str(rows[limit - 1][0].id) if len(rows) > limit else None,
                }
            )

    @app.post(MOBILE + "/opening-positions", status_code=201)
    def opening(
        body: S.OpeningPosition, key: str = Header(alias="Idempotency-Key"), p=Depends(principal)
    ):
        refreshed_after = None
        if body.price is None:
            # Fetch only public instrument identity, outside the user transaction.
            # A prior idempotent result is still returned even if the refresh fails.
            from functools import partial

            from portfolio_backend.collector import publish_mark
            from portfolio_backend.providers.market import AKShareQuotes
            from portfolio_backend.services.public_data import SharedFetch

            with db.control() as s:
                instrument = s.get(Instrument, body.instrument_id)
                quote = s.get(LatestQuote, body.instrument_id)
                refresh = (
                    instrument
                    and portfolio.add_admission(instrument) is None
                    and portfolio.quote_status(instrument, quote, now()) not in {"current", "nav"}
                )
                if instrument:
                    s.expunge(instrument)
            if refresh:
                try:
                    refresh_started = now()
                    provider = AKShareQuotes(fetch=partial(AKShareQuotes._fetch, timeout=30))
                    if instrument.provider in provider.providers:
                        rows = SharedFetch(cache).get(
                            provider.cache_key(instrument),
                            config.quote_poll_seconds,
                            lambda: provider.load(instrument),
                        )
                        if rows:
                            publish_mark(
                                db, instrument, provider.normalize(instrument, rows, now()), config
                            )
                            refreshed_after = refresh_started
                except DomainError:
                    pass
        with db.tenant(p.user_id) as s:
            data = portfolio.open_position(s, p.user_id, body, key, refreshed_after=refreshed_after)
        return result(data, 201)

    @app.post(MOBILE + "/ledger-events", status_code=201)
    def trade(body: S.Trade, key: str = Header(alias="Idempotency-Key"), p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            data = portfolio.trade(s, p.user_id, body, key)
        return result(data, 201)

    @app.get(MOBILE + "/ledger-events")
    def ledger(
        cursor: UUID | None = None,
        limit: int = Query(50, ge=1, le=100),
        position_id: UUID | None = None,
        account_id: UUID | None = None,
        p=Depends(principal),
    ):
        with db.tenant(p.user_id) as s:
            q = (
                select(LedgerEvent)
                .where(LedgerEvent.user_id == p.user_id)
                .order_by(LedgerEvent.occurred_at.desc(), LedgerEvent.id.desc())
            )
            if position_id:
                owned(s, Position, position_id, p.user_id)
                q = q.where(LedgerEvent.position_id == position_id)
            if account_id:
                owned(s, Account, account_id, p.user_id)
                q = q.join(Position, Position.id == LedgerEvent.position_id).where(
                    Position.account_id == account_id
                )
            if cursor:
                anchor = owned(s, LedgerEvent, cursor, p.user_id)
                q = q.where(
                    tuple_(LedgerEvent.occurred_at, LedgerEvent.id)
                    < tuple_(anchor.occurred_at, anchor.id)
                )
            rows = s.scalars(q.limit(limit + 1)).all()
            return result(
                {
                    "items": [
                        {
                            column.name: getattr(r, column.name)
                            for column in LedgerEvent.__table__.columns
                            if column.name != "user_id"
                        }
                        for r in rows[:limit]
                    ],
                    "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
                }
            )

    @app.get(MOBILE + "/instruments/{instrument_id}/kline")
    def instrument_kline(
        instrument_id: UUID, days: int = Query(90, ge=5, le=365), p=Depends(principal)
    ):
        from portfolio_backend.providers.research import ResearchMarket

        with db.control() as s:
            item = s.get(Instrument, instrument_id)
            require(item is not None, "not_found", "标的不存在", 404)
            require(
                portfolio.add_admission(item) is None, "instrument_unavailable", "标的不可用", 422
            )
            args = {
                "ticker": item.symbol,
                "market": item.market,
                "asset_class": item.asset_class,
                "provider_symbol": item.provider_symbol,
                "days": days,
            }
            local_fixture = config.simulator_ai and item.provider == "local-fixture"
        if local_fixture:
            if args["asset_class"] == "fund":
                points = [
                    {"date": (now() - timedelta(days=n)).date().isoformat(), "close": price}
                    for n, price in [(3, "1.0"), (2, "1.02"), (1, "1.01"), (0, "1.05")]
                ]
            else:
                points = [
                    {
                        "date": (now() - timedelta(days=n)).date().isoformat(),
                        "open": op,
                        "high": high,
                        "low": low,
                        "close": close,
                    }
                    for n, op, high, low, close in [
                        (3, "4.0", "4.1", "3.9", "4.05"),
                        (2, "4.05", "4.2", "4.0", "4.15"),
                        (1, "4.15", "4.2", "4.0", "4.05"),
                        (0, "4.05", "4.3", "4.0", "4.2"),
                    ]
                ]
            return result(
                {
                    "title": "本机合成历史净值"
                    if args["asset_class"] == "fund"
                    else "本机合成历史日线",
                    "ticker": args["ticker"],
                    "market": args["market"],
                    "points": points,
                    "annotations": [],
                    "trendlines": [],
                    "provider": "local-fixture",
                    "kind": "historical_nav"
                    if args["asset_class"] == "fund"
                    else "historical_daily",
                }
            )
        data = ResearchMarket(config).fetch("get_kline", args, timeout=30)
        require(not data.get("error"), "chart_unavailable", "暂无法取得历史行情", 503)
        return result(
            data
            | {
                "title": "历史净值" if args["asset_class"] == "fund" else "历史日线",
                "annotations": [],
                "trendlines": [],
            }
        )

    @app.get(MOBILE + "/watchlist")
    def get_watchlist(
        offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), p=Depends(principal)
    ):
        from portfolio_backend.agent.tools.tenant import watchlist

        with db.tenant(p.user_id) as s:
            return result(
                watchlist(s, p.user_id, "get_watchlist", {"offset": offset, "limit": limit})
            )

    @app.put(MOBILE + "/watchlist")
    def save_watchlist(body: S.WatchlistInput, p=Depends(principal)):
        from portfolio_backend.models import WatchlistItem

        with db.tenant(p.user_id) as s:
            item = s.get(Instrument, body.instrument_id)
            require(item is not None, "not_found", "标的不存在", 404)
            require(
                portfolio.add_admission(item) is None, "instrument_unavailable", "标的不可用", 422
            )
            lock_portfolio(s, p.user_id)
            values = {
                "priority": body.priority,
                "target_price": body.target_price,
                "note": body.note,
            }
            s.execute(
                pg_insert(WatchlistItem)
                .values(user_id=p.user_id, instrument_id=item.id, **values)
                .on_conflict_do_update(index_elements=["user_id", "instrument_id"], set_=values)
            )
            return result({"saved": True})

    @app.delete(MOBILE + "/watchlist/{instrument_id}")
    def delete_watchlist(instrument_id: UUID, p=Depends(principal)):
        from portfolio_backend.models import WatchlistItem

        with db.tenant(p.user_id) as s:
            lock_portfolio(s, p.user_id)
            count = s.execute(
                __import__("sqlalchemy")
                .delete(WatchlistItem)
                .where(
                    WatchlistItem.user_id == p.user_id, WatchlistItem.instrument_id == instrument_id
                )
            ).rowcount
            return result({"removed": bool(count)})

    def _close_marks():
        # Display-only relaxation: the latest close may stand in for a missing
        # live mark outside market hours; timestamps stay real and status is
        # ``close_mark``. A zero age disables the relaxation.
        return {
            "close_marks": config.close_mark_max_age_days > 0,
            "close_mark_max_age_days": config.close_mark_max_age_days,
        }

    @app.get(MOBILE + "/portfolio/snapshot")
    def snapshot(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            return result(portfolio.snapshot(s, p.user_id, **_close_marks()))

    @app.get(MOBILE + "/positions")
    def positions(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            return result(portfolio.snapshot(s, p.user_id, **_close_marks())["positions"])

    @app.patch(MOBILE + "/positions/{position_id}/classification")
    def classify(position_id: UUID, body: S.PositionTypeInput, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            root = lock_portfolio(s, p.user_id)
            position = owned(s, Position, position_id, p.user_id, lock=True)
            require(
                position.version == body.expected_version,
                "version_conflict",
                "持仓已更新，请刷新后重试",
            )
            position.position_type = body.position_type
            position.version += 1
            root.version += 1
            data = portfolio.position_view(position)
        return result(data)

    @app.get(MOBILE + "/home")
    def home(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            report = s.scalar(
                select(Report)
                .where(Report.user_id == p.user_id)
                .order_by(Report.created_at.desc(), Report.id.desc())
                .limit(1)
            )
            snapshot = portfolio.snapshot(s, p.user_id, **_close_marks())
            history = portfolio.asset_history(s, p.user_id)
            # Render today's point on the home chart even before tonight's report
            # exists; report history itself never includes fabricated values.
            if snapshot.get("complete") and snapshot.get("market_value_cny"):
                today = snapshot["as_of"][:10]
                if not history or history[-1]["as_of"][:10] != today:
                    history = [
                        *history,
                        {
                            "as_of": snapshot["as_of"],
                            "market_value_cny": snapshot["market_value_cny"],
                        },
                    ]
            return result(
                {
                    "snapshot": snapshot,
                    "asset_history": history,
                    "latest_content": report_view(report) if report else None,
                    "news": news.for_user(s, p.user_id, config),
                }
            )

    @app.get(MOBILE + "/feed")
    def feed(
        cursor: UUID | None = None, limit: int = Query(20, ge=1, le=100), p=Depends(principal)
    ):
        with db.tenant(p.user_id) as s:
            q = select(Report).where(Report.user_id == p.user_id)
            if cursor:
                after = owned(s, Report, cursor, p.user_id)
                from sqlalchemy import tuple_

                q = q.where(tuple_(Report.created_at, Report.id) < (after.created_at, after.id))
            rows = s.scalars(
                q.order_by(Report.created_at.desc(), Report.id.desc()).limit(limit + 1)
            ).all()
            return result(
                {
                    "items": [report_view(r) for r in rows[:limit]],
                    "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
                }
            )

    @app.get(MOBILE + "/reports/{report_id}")
    def report(report_id: UUID, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            r = owned(s, Report, report_id, p.user_id)
            return result(
                report_view(r)
                | {
                    "snapshot": r.snapshot,
                    "remaining_free": max(0, 3 - r.free_used - r.free_reserved),
                }
            )

    @app.post(MOBILE + "/reports/{report_id}/read")
    def read_report(report_id: UUID, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            owned(s, Report, report_id, p.user_id, lock=True).read_at = now()
        return result({"read": True})

    @app.post(MOBILE + "/reports/{report_id}/questions", status_code=202)
    def report_question(
        report_id: UUID,
        body: S.Question,
        key: str = Header(alias="Idempotency-Key"),
        p=Depends(principal),
    ):
        with db.tenant(p.user_id) as s:
            data = ai.enqueue_question(s, p.user_id, body, key, config, report_id=report_id)
        return result(data, 202)

    @app.get(MOBILE + "/conversations")
    def conversations(
        paged: bool = False,
        cursor: UUID | None = None,
        limit: int = Query(30, ge=1, le=100),
        report_id: UUID | None = None,
        p=Depends(principal),
    ):
        from sqlalchemy import tuple_

        with db.tenant(p.user_id) as s:
            q = select(Conversation).where(Conversation.user_id == p.user_id)
            if report_id:
                owned(s, Report, report_id, p.user_id)
                q = q.where(Conversation.report_id == report_id)
            if cursor:
                c = owned(s, Conversation, cursor, p.user_id)
                q = q.where(tuple_(Conversation.created_at, Conversation.id) < (c.created_at, c.id))
            rows = s.scalars(
                q.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(
                    (limit + 1) if paged else 100
                )
            ).all()
            items = (
                [
                    {"id": r.id, "title": r.title, "report_id": r.report_id}
                    for r in rows[:limit]
                    if paged
                ]
                if paged
                else [{"id": r.id, "title": r.title, "report_id": r.report_id} for r in rows]
            )
            return result(
                {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None}
                if paged
                else items
            )

    @app.post(MOBILE + "/conversations", status_code=201)
    def create_conversation(
        body: S.ConversationCreate,
        key: str | None = Header(default=None, alias="Idempotency-Key"),
        p=Depends(principal),
    ):
        with db.tenant(p.user_id) as s:

            def create():
                row = Conversation(user_id=p.user_id, title=body.title)
                s.add(row)
                s.flush()
                return {"id": str(row.id), "title": row.title, "report_id": None}

            data = (
                idempotent(s, p.user_id, "conversation", key, body.model_dump(), create)
                if key
                else create()
            )
        return result(data, 201)

    @app.get(MOBILE + "/conversations/{conversation_id}/messages")
    def messages(
        conversation_id: UUID,
        paged: bool = False,
        cursor: UUID | None = None,
        limit: int = Query(30, ge=1, le=100),
        p=Depends(principal),
    ):
        from sqlalchemy import tuple_

        with db.tenant(p.user_id) as s:
            owned(s, Conversation, conversation_id, p.user_id)
            q = select(Message).where(
                Message.user_id == p.user_id, Message.conversation_id == conversation_id
            )
            if cursor:
                m = owned(s, Message, cursor, p.user_id)
                require(m.conversation_id == conversation_id, "not_found", "消息不存在", 404)
                q = q.where(tuple_(Message.created_at, Message.id) < (m.created_at, m.id))
            q = (
                q.order_by(Message.created_at.desc(), Message.id.desc())
                if paged
                else q.order_by(Message.created_at, Message.id)
            )
            rows = s.scalars(q.limit(limit + 1 if paged else 500)).all()
            items = [
                {
                    "id": r.id,
                    "role": r.role,
                    "content": r.content,
                    "created_at": r.created_at,
                    "charts": r.charts,
                }
                for r in (rows[:limit] if paged else rows)
            ]
            return result(
                {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None}
                if paged
                else items
            )

    @app.get(MOBILE + "/conversations/{conversation_id}/active-run")
    def active_run(conversation_id: UUID, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            owned(s, Conversation, conversation_id, p.user_id)
            r = s.scalar(
                select(AIRun)
                .where(AIRun.user_id == p.user_id, AIRun.conversation_id == conversation_id)
                .order_by(AIRun.created_at.desc(), AIRun.id.desc())
                .limit(1)
            )
            return result({"run_id": str(r.id) if r else None, "status": r.status if r else None})

    @app.post(MOBILE + "/conversations/{conversation_id}/messages", status_code=202)
    def ask(
        conversation_id: UUID,
        body: S.Question,
        key: str = Header(alias="Idempotency-Key"),
        p=Depends(principal),
    ):
        with db.tenant(p.user_id) as s:
            data = ai.enqueue_question(
                s, p.user_id, body, key, config, conversation_id=conversation_id
            )
        return result(data, 202)

    def run_data(run_id, p):
        with db.tenant(p.user_id) as s:
            r = owned(s, AIRun, run_id, p.user_id)
            return {
                "id": str(r.id),
                "status": r.status,
                "answer": r.answer,
                "error_code": r.error_code,
                "charts": r.usage.get("charts", []),
            }

    @app.get(MOBILE + "/runs/{run_id}")
    def run(run_id: UUID, p=Depends(principal)):
        return result(run_data(run_id, p))

    @app.get(MOBILE + "/runs/{run_id}/events")
    def run_events(run_id: UUID, request: Request, p=Depends(principal)):
        run_data(run_id, p)
        token = request.headers["authorization"].split(" ", 1)[1]

        async def events():
            last = None
            for _ in range(60):
                if await request.is_disconnected():
                    return
                # Recheck session/deletion on every poll. No DB connection held during sleep.
                try:
                    current = await asyncio.to_thread(auth.authenticate, token)
                    data = await asyncio.to_thread(run_data, run_id, current)
                except DomainError:
                    return
                payload = json.dumps(data, ensure_ascii=False)
                if payload != last:
                    yield f"event: state\ndata: {payload}\n\n"
                    last = payload
                else:
                    yield ": keep-alive\n\n"
                if data["status"] in {"succeeded", "failed"}:
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get(MOBILE + "/me/export")
    def export_data(
        section: str = "positions",
        cursor: UUID | None = None,
        limit: int = Query(100, ge=1, le=200),
        p=Depends(principal),
    ):
        from portfolio_backend.services.privacy import export_page

        with db.tenant(p.user_id) as s:
            return result(export_page(s, p.user_id, section, cursor, limit))

    @app.get(MOBILE + "/me/privacy")
    def privacy(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            rows = s.scalars(select(Consent).where(Consent.user_id == p.user_id))
            return result(
                {
                    "ai_provider": config.ai_provider_id or None,
                    "disclosure_version": config.ai_disclosure_version,
                    "trace_capture_content": config.agent_trace_capture_content,
                    "consents": [
                        {
                            "purpose": r.purpose,
                            "granted": r.granted,
                            "provider": r.provider,
                            "disclosure_version": r.disclosure_version,
                        }
                        for r in rows
                    ],
                }
            )

    @app.put(MOBILE + "/me/consents/{purpose}")
    def consent(purpose: str, body: S.ConsentInput, p=Depends(principal)):
        require(
            purpose in {"interactive_ai", "scheduled_ai"}, "invalid_purpose", "未知授权用途", 422
        )
        if body.granted:
            ai.require_provider(config)
            require(
                body.provider == config.ai_provider_id
                and body.disclosure_version == config.ai_disclosure_version,
                "disclosure_changed",
                "请阅读当前服务的数据使用说明",
                409,
            )
        with db.tenant(p.user_id) as s:
            lock_portfolio(s, p.user_id)
            row = s.scalar(
                select(Consent).where(Consent.user_id == p.user_id, Consent.purpose == purpose)
            )
            if row is None:
                row = Consent(user_id=p.user_id, purpose=purpose)
                s.add(row)
            row.provider, row.disclosure_version = body.provider, body.disclosure_version
            row.granted, row.updated_at = body.granted, now()
        return result({"purpose": purpose, "granted": body.granted})

    @app.get(MOBILE + "/me/preferences")
    def get_preferences(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            row = s.get(Preferences, p.user_id)
            return result(
                {
                    c.name: getattr(row, c.name)
                    for c in Preferences.__table__.columns
                    if c.name != "user_id"
                }
            )

    @app.patch(MOBILE + "/me/preferences")
    def preferences(body: S.PreferencesInput, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            row = s.scalar(
                select(Preferences).where(Preferences.user_id == p.user_id).with_for_update()
            )
            for field, value in body.model_dump(exclude_none=True).items():
                setattr(row, field, value)
        return result({"saved": True})

    @app.put(MOBILE + "/me/layout")
    def save_layout(body: S.LayoutInput, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            row = s.scalar(
                select(Preferences).where(Preferences.user_id == p.user_id).with_for_update()
            )
            require(
                row.layout_version == body.expected_version,
                "version_conflict",
                "其他设备已调整总览，请刷新后重试",
            )
            row.overview_order = body.overview_order
            row.layout_version += 1
            return result(
                {"overview_order": row.overview_order, "layout_version": row.layout_version}
            )

    @app.get(MOBILE + "/me/discipline")
    def get_discipline(p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            row = s.get(TradingDiscipline, p.user_id)
            if row is None:
                return result({"content": "", "updated_at": None})
            return result({"content": row.content, "updated_at": row.updated_at})

    @app.put(MOBILE + "/me/discipline")
    def save_discipline(body: S.DisciplineInput, p=Depends(principal)):
        saved_at = now()
        with db.tenant(p.user_id) as s:
            lock_portfolio(s, p.user_id)
            if "expected_updated_at" in body.model_fields_set:
                existing = s.get(TradingDiscipline, p.user_id)
                require(
                    (existing.updated_at if existing else None) == body.expected_updated_at,
                    "version_conflict",
                    "交易纪律已在其他设备更新，请重新打开编辑页后合并内容",
                )
            statement = pg_insert(TradingDiscipline).values(
                user_id=p.user_id, content=body.content, updated_at=saved_at
            )
            s.execute(
                statement.on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={"content": body.content, "updated_at": saved_at},
                )
            )
        return result({"saved": True, "updated_at": saved_at})

    @app.put(MOBILE + "/me/devices/{device_id}")
    def register_device(device_id: UUID, body: S.DeviceInput, p=Depends(principal)):
        with db.control() as s:
            # Control role permits rebinding a token without disclosing its prior owner.
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "device:" + digest(body.token.lower())},
            )
            row = s.scalar(select(Device).where(Device.token_hash == digest(body.token.lower())))
            if row is None:
                collision = s.get(Device, device_id)
                require(
                    collision is None or collision.user_id == p.user_id,
                    "not_found",
                    "设备不存在",
                    404,
                )
                row = collision or Device(id=device_id, user_id=p.user_id)
                s.add(row)
            row.user_id, row.token, row.token_hash = (
                p.user_id,
                body.token.lower(),
                digest(body.token.lower()),
            )
            row.environment, row.enabled = body.environment, True
        return result({"id": row.id, "registered": True})

    @app.delete(MOBILE + "/me/devices/{device_id}")
    def remove_device(device_id: UUID, p=Depends(principal)):
        with db.tenant(p.user_id) as s:
            s.delete(owned(s, Device, device_id, p.user_id))
        return result({"removed": True})

    @app.post(MOBILE + "/me/deletion-requests", status_code=202)
    def delete_account(body: S.DeletionInput, p=Depends(principal)):
        require(
            p.authenticated_at > now() - timedelta(minutes=5),
            "reauthentication_required",
            "请先验证手机号后删除账户",
            403,
        )
        receipt = secrets.token_urlsafe(32)
        with db.control() as s:
            user = s.scalar(select(User).where(User.id == p.user_id).with_for_update())
            user.status = "deleting"
            s.execute(
                update(AuthSession).where(AuthSession.user_id == p.user_id).values(revoked=True)
            )
            s.execute(update(Device).where(Device.user_id == p.user_id).values(enabled=False))
            row = DeletionRequest(user_id=p.user_id, receipt_hash=digest(receipt))
            s.add(row)
            s.add(
                Outbox(
                    user_id=p.user_id,
                    kind="delete_account",
                    dedupe_key=f"delete:{p.user_id}",
                    payload={},
                )
            )
        return result(
            {
                "status": "queued",
                "receipt": receipt,
                "deletion_notice": "删除账户将清除账户与个人数据，操作完成后不可恢复。",
            },
            202,
        )

    @app.post(MOBILE + "/auth/deletion-status")
    def deletion_status(body: S.Refresh):
        with db.control() as s:
            row = s.scalar(
                select(DeletionRequest).where(
                    DeletionRequest.receipt_hash == digest(body.refresh_token)
                )
            )
            require(row is not None, "not_found", "删除请求不存在", 404)
            return result({"status": row.status, "completed_at": row.completed_at})

    @app.get(ADMIN + "/overview")
    def overview(identity=Depends(operator)):
        with db.control() as s:
            counts = {
                status: count
                for status, count in s.execute(
                    select(Job.status, func.count()).group_by(Job.status)
                )
            }
            users = s.scalar(select(func.count()).select_from(User).where(User.status == "active"))
            instruments = s.scalar(select(func.count()).select_from(Instrument))
            audit(s, identity, "overview.read", "运维概览")
            try:
                last_sync = cache.get("catalog:v1:last-sync")
            except RedisError:
                last_sync = None
            return result(
                {
                    "active_users": users,
                    "jobs": counts,
                    "instruments": instruments,
                    "catalog_last_sync": int(last_sync) if last_sync else None,
                    "version": "0.1.0",
                }
            )

    @app.post(ADMIN + "/catalog/sync", status_code=202)
    def catalog_sync(
        reason: str = Header(min_length=5, max_length=500, alias="X-Admin-Reason"),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            audit(s, identity, "catalog.sync", reason)
        catalog.request_sync()
        return result({"queued": True}, 202)

    @app.get(ADMIN + "/jobs")
    def jobs(identity=Depends(operator)):
        admin_permission(identity, "operations")
        with db.control() as s:
            rows = s.scalars(select(Job).order_by(Job.created_at.desc()).limit(100))
            audit(s, identity, "jobs.read", "任务运行状态")
            return result(
                [
                    {
                        "id": r.id,
                        "kind": r.kind,
                        "status": r.status,
                        "attempt": r.attempt,
                        "error_code": r.error_code,
                        "due_at": r.due_at,
                    }
                    for r in rows
                ]
            )

    @app.get(ADMIN + "/trace-users")
    def admin_trace_users(
        query: str = Query("", max_length=100),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            q = (
                select(User.id, User.apple_subject, func.count(Job.id), func.max(Job.created_at))
                .join(Job, Job.user_id == User.id)
                .where(Job.kind.in_(AI_JOB_KINDS))
                .group_by(User.id, User.apple_subject)
            )
            if query:
                # Never expose or search Apple subject identifiers; Sandbox labels are explicit.
                from sqlalchemy import String, cast, or_

                q = q.where(
                    or_(
                        cast(User.id, String).ilike("%" + query + "%"),
                        User.apple_subject.like("operator-beta:%" + query + "%"),
                    )
                )
            rows = s.execute(
                q.order_by(func.max(Job.created_at).desc(), User.id).offset(offset).limit(limit + 1)
            ).all()
            audit(s, identity, "trace_users.read", "AI 轨迹用户筛选")
            return result(
                {
                    "items": [
                        {
                            "user_id": row[0],
                            "label": row[1].removeprefix("operator-beta:")
                            if row[1].startswith("operator-beta:")
                            else None,
                            "run_count": row[2],
                            "last_run_at": row[3],
                        }
                        for row in rows[:limit]
                    ],
                    "next_offset": offset + limit if len(rows) > limit else None,
                }
            )

    @app.get(ADMIN + "/runs")
    def admin_runs(
        user_id: UUID | None = None,
        status: str | None = None,
        triggered_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            q = select(Job).where(Job.kind.in_(AI_JOB_KINDS))
            if user_id:
                q = q.where(Job.user_id == user_id)
            if status:
                q = q.where(Job.status == status)
            if triggered_by:
                q = q.where(Job.kind == triggered_by)
            if since:
                require(since.tzinfo is not None, "invalid_date", "时间需要时区", 422)
                q = q.where(Job.created_at >= since)
            if until:
                require(until.tzinfo is not None, "invalid_date", "时间需要时区", 422)
                q = q.where(Job.created_at <= until)
            rows = s.scalars(
                q.order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit + 1)
            ).all()
            audit(s, identity, "runs.read", "AI 任务运行排查", user_id)
            items = []
            for r in rows[:limit]:
                run = s.get(AIRun, UUID(r.payload["run_id"])) if r.payload.get("run_id") else None
                usage = {
                    k: v
                    for k, v in (run.usage if run else {}).items()
                    if k in {"model", "input_tokens", "output_tokens", "degraded_reason"}
                }
                items.append(
                    {
                        "id": r.id,
                        "run_id": run.id if run else None,
                        "user_id": r.user_id,
                        "triggered_by": r.kind,
                        "status": r.status,
                        "created_at": r.created_at,
                        "attempt": r.attempt,
                        "error_code": r.error_code,
                        "usage": usage,
                    }
                )
            return result(
                {"items": items, "next_offset": offset + limit if len(rows) > limit else None}
            )

    @app.get(ADMIN + "/runs/{run_id}/tool-calls")
    def admin_tool_calls(
        run_id: UUID,
        tool_name: str | None = None,
        status: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            from sqlalchemy import or_

            q = select(AgentToolCall).where(
                or_(AgentToolCall.job_id == run_id, AgentToolCall.run_id == run_id)
            )
            if tool_name:
                q = q.where(AgentToolCall.tool_name == tool_name)
            if status:
                q = q.where(AgentToolCall.status == status)
            rows = s.scalars(
                q.order_by(AgentToolCall.created_at, AgentToolCall.id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            audit(s, identity, "tool_calls.read", "工具调用轨迹排查", run_id)
            return result(
                {
                    "items": [
                        {
                            c.name: getattr(r, c.name)
                            for c in AgentToolCall.__table__.columns
                            if c.name != "user_id"
                        }
                        for r in rows[:limit]
                    ],
                    "next_offset": offset + limit if len(rows) > limit else None,
                }
            )

    @app.post(ADMIN + "/instruments", status_code=201)
    def add_instrument(
        body: S.InstrumentInput,
        reason: str = Header(min_length=5, max_length=500, alias="X-Admin-Reason"),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            row = Instrument(**body.model_dump())
            s.add(row)
            s.flush()
            audit(s, identity, "instrument.create", reason, row.id)
            data = {"id": row.id}
        return result(data, 201)

    @app.put(ADMIN + "/quotes")
    def publish_quote(
        body: S.QuoteInput,
        reason: str = Header(min_length=5, max_length=500, alias="X-Admin-Reason"),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            instrument = s.get(Instrument, body.instrument_id)
            require(instrument, "not_found", "标的不存在", 404)
            require(
                body.provider == instrument.provider and body.kind == instrument.quote_kind,
                "provider_mismatch",
                "报价来源或类型与目录不匹配",
                422,
            )
            require(
                body.as_of <= now()
                and body.valid_until > now()
                and body.valid_until
                <= body.as_of + timedelta(seconds=instrument.freshness_seconds),
                "invalid_quote_time",
                "报价时间或有效期不正确",
                422,
            )
            s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "quote:" + str(instrument.id)},
            )
            row = s.get(LatestQuote, instrument.id)
            require(row is None or body.as_of >= row.as_of, "old_quote", "不能用旧行情覆盖新行情")
            if row is None:
                row = LatestQuote(instrument_id=instrument.id)
                s.add(row)
            for key, value in body.model_dump(exclude={"instrument_id"}).items():
                setattr(row, key, value)
            row.fetched_at = now()
            audit(s, identity, "quote.publish", reason, instrument.id)
        return result({"saved": True})

    @app.post(ADMIN + "/users/{user_id}/reports", status_code=202)
    def generate_report(
        user_id: UUID,
        body: S.ReportRequest,
        reason: str = Header(min_length=5, max_length=500, alias="X-Admin-Reason"),
        identity=Depends(operator),
    ):
        admin_permission(identity, "operations")
        with db.control() as s:
            user = s.get(User, user_id)
            require(user and user.status == "active", "not_found", "用户不存在", 404)
            require(
                body.period
                == now()
                .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
                .date()
                .isoformat(),
                "historical_report_unavailable",
                "历史快照回放尚未开放",
                422,
            )
            from sqlalchemy.dialects.postgresql import insert

            key = f"report:{user_id}:{body.kind}:{body.period}"
            if body.replay:
                # 重跑：清空整条幂等链（报告→追问→轨迹→任务→Outbox），随后重新入队。
                report_ids = select(Report.id).where(
                    Report.user_id == user_id,
                    Report.kind == body.kind,
                    Report.period == body.period,
                )
                job_ids = select(Job.id).where(Job.user_id == user_id, Job.dedupe_key == key)
                conversation_ids = select(Conversation.id).where(
                    Conversation.user_id == user_id, Conversation.report_id.in_(report_ids)
                )
                s.execute(delete(Message).where(Message.conversation_id.in_(conversation_ids)))
                s.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
                s.execute(delete(AIRun).where(AIRun.report_id.in_(report_ids)))
                s.execute(delete(Report).where(Report.id.in_(report_ids)))
                s.execute(delete(AgentToolCall).where(AgentToolCall.job_id.in_(job_ids)))
                s.execute(delete(Job).where(Job.id.in_(job_ids)))
                s.execute(delete(Outbox).where(Outbox.dedupe_key == key))
                audit(
                    s,
                    identity,
                    "report.replay",
                    reason,
                    user_id,
                    {"kind": body.kind, "period": body.period},
                )
            s.execute(
                insert(Outbox)
                .values(
                    user_id=user_id, kind=body.kind, dedupe_key=key, payload={"period": body.period}
                )
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )
            audit(
                s,
                identity,
                "report.request",
                reason,
                user_id,
                {"kind": body.kind, "period": body.period, "replay": body.replay},
            )
        return result({"queued": True}, 202)

    @app.get(ADMIN + "/users")
    def admin_users(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        identity=Depends(operator),
    ):
        with db.control() as s:
            users = s.scalars(
                select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
            ).all()
            prefs = {
                p.user_id: p
                for p in s.scalars(
                    select(Preferences).where(Preferences.user_id.in_([u.id for u in users]))
                )
            }
            today_by_user = {
                u.id: now()
                .astimezone(
                    __import__("zoneinfo").ZoneInfo(
                        (prefs.get(u.id).timezone if prefs.get(u.id) else None) or "Asia/Shanghai"
                    )
                )
                .date()
                .isoformat()
                for u in users
            }
            report_rows = s.execute(
                select(Report.user_id, Report.kind, Report.period).where(
                    Report.user_id.in_([u.id for u in users]),
                    Report.kind.in_(REPORT_KINDS),
                )
            ).all()
            have = {(r.user_id, r.kind): r.period for r in report_rows}
            items = []
            for u in users:
                today = today_by_user[u.id]
                items.append(
                    {
                        "id": str(u.id),
                        "phone_masked": u.phone[:3] + "****" + u.phone[-4:] if u.phone else None,
                        "created_at": u.created_at.isoformat(),
                        "has_password": u.password_hash is not None,
                        "today": today,
                        # Both daily sessions count as "today's daily recap".
                        "daily_today": any(
                            have.get((u.id, kind)) == today
                            for kind in ("daily", "daily_cn", "daily_us")
                        ),
                        "weekly_today": have.get((u.id, "weekly")) == today,
                    }
                )
            audit(s, identity, "users.read", "管理后台用户列表", None)
            return result({"items": items})

    @app.get(ADMIN + "/users/{user_id}/reports")
    def admin_reports(
        user_id: UUID,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        identity=Depends(operator),
    ):
        with db.control() as s:
            user = s.get(User, user_id)
            require(user is not None, "not_found", "用户不存在", 404)
            rows = s.scalars(
                select(Report)
                .where(Report.user_id == user_id)
                .order_by(Report.created_at.desc())
                .offset(offset)
                .limit(limit + 1)
            ).all()
            items = []
            for r in rows[:limit]:
                snapshot = r.snapshot or {}
                analysis = snapshot.get("analysis") or {}
                items.append(
                    {
                        "id": str(r.id),
                        "kind": r.kind,
                        "period": r.period,
                        "title": r.title,
                        "summary": r.summary,
                        "complete": snapshot.get("complete"),
                        "coverage": snapshot.get("coverage"),
                        "market_value_cny": snapshot.get("market_value_cny"),
                        "analysis_kind": analysis.get("kind"),
                        "analysis_status": analysis.get("status"),
                        "created_at": r.created_at.isoformat(),
                        "read": r.read_at is not None,
                    }
                )
            audit(s, identity, "report.read", "管理后台查看报告列表", user_id)
            return result(
                {
                    "items": items,
                    "next_offset": offset + limit if len(rows) > limit else None,
                }
            )

    @app.get(ADMIN + "/users/{user_id}/reports/{report_id}")
    def admin_report_detail(user_id: UUID, report_id: UUID, identity=Depends(operator)):
        with db.control() as s:
            report = s.get(Report, report_id)
            require(
                report is not None and report.user_id == user_id,
                "not_found",
                "报告不存在",
                404,
            )
            audit(s, identity, "report.read", "管理后台查看报告详情", report_id)
            return result(
                {
                    "id": str(report.id),
                    "kind": report.kind,
                    "period": report.period,
                    "title": report.title,
                    "summary": report.summary,
                    "snapshot": report.snapshot,
                    "created_at": report.created_at.isoformat(),
                    "read": report.read_at is not None,
                }
            )

    return app


def report_view(row):
    return {
        "id": str(row.id),
        "kind": row.kind,
        "period": row.period,
        "title": row.title,
        "summary": row.summary,
        "created_at": row.created_at.isoformat(),
        "read": row.read_at is not None,
        "analysis_kind": row.snapshot.get("analysis", {}).get("kind", "facts"),
        "ai_provider": row.snapshot.get("analysis", {}).get("provider"),
    }


def main():
    import uvicorn

    uvicorn.run("portfolio_backend.api:create_app", factory=True, host="127.0.0.1", port=8000)
