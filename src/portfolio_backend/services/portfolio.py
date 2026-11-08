from datetime import datetime, time
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps
from zoneinfo import ZoneInfo

from sqlalchemy import select

from portfolio_backend.db import now
from portfolio_backend.domain.ledger import Balance, apply
from portfolio_backend.errors import require
from portfolio_backend.models import (
    Account,
    FXRate,
    Instrument,
    LatestQuote,
    LedgerEvent,
    Outbox,
    Portfolio,
    Position,
    Report,
)
from portfolio_backend.services.common import idempotent, lock_portfolio, owned

CN_TZ = ZoneInfo("Asia/Shanghai")
HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")

# Weekday-only trading sessions (no holiday calendars): a stale mark whose source
# date falls in this window may be shown as an honest close mark instead of a
# missing quote. Set the setting to 0 to keep the strict "stale = missing" rule.
CLOSE_MARK_MAX_AGE_DAYS = 4
# Both daily sessions (A/HK close and US close) are daily recap marks.
DAILY_KINDS = ("daily", "daily_cn", "daily_us")


def market_open(market, at):
    """Whether the instrument's market is in its regular session at ``at``.

    Weekday-only by design: holidays simply count as closed, which makes the
    close-mark relaxation more permissive rather than less.
    """
    if at is None:
        return True
    if market == "CN":
        local = at.astimezone(CN_TZ)
        if local.weekday() >= 5:
            return False
        clock = local.time()
        return time_between(clock, (9, 30), (11, 30)) or time_between(clock, (13, 0), (15, 0))
    if market == "HK":
        local = at.astimezone(HK_TZ)
        if local.weekday() >= 5:
            return False
        clock = local.time()
        return time_between(clock, (9, 30), (12, 0)) or time_between(clock, (13, 0), (16, 0))
    if market == "US":
        local = at.astimezone(US_TZ)
        if local.weekday() >= 5:
            return False
        return time_between(local.time(), (9, 30), (16, 0))
    return True


def time_between(clock, start, end):
    return time(*start) <= clock < time(*end)


def close_mark_state(state, quote, reference_day, max_age_days=CLOSE_MARK_MAX_AGE_DAYS):
    """A stale realtime/delayed mark may represent the last close honestly.

    Keeps the real source timestamp and labels the position ``close_mark``; the
    quote is never relabelled as realtime. Only applies when the mark's source
    day is within ``max_age_days`` of the reference day, so a suspended or
    delisted instrument still shows as missing.
    """
    if state != "stale" or quote is None or max_age_days <= 0:
        return state
    if quote.kind == "nav" or quote.as_of is None:
        return state
    age = (reference_day - quote.as_of.astimezone(CN_TZ).date()).days
    return "close_mark" if 0 <= age <= max_age_days else state


def recent_complete_daily_values(session, user_id, limit=8):
    """Market values of the most recent complete daily snapshots, newest first."""
    rows = (
        session.execute(
            select(Report.snapshot)
            .where(Report.user_id == user_id, Report.kind.in_(DAILY_KINDS))
            .order_by(Report.created_at.desc(), Report.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    values = []
    for snapshot in rows:
        if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
            continue
        amount = snapshot.get("market_value_cny")
        if amount is None:
            continue
        try:
            values.append(Decimal(str(amount)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return values


def decimal(value):
    return format(value, "f") if value is not None else None


def financial_precision(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 50
            return fn(*args, **kwargs)

    return wrapped


def quote_status(instrument, quote, at):
    if not instrument.commercial_display:
        return "display_rights_unconfirmed"
    if not instrument.enabled:
        return "instrument_disabled"
    if quote is None:
        return "missing"
    if quote.provider != instrument.provider or quote.kind != instrument.quote_kind:
        return "provider_mismatch"
    if quote.as_of > at or quote.valid_until <= at:
        return "stale"
    if quote.kind == "nav":
        return "nav"
    return "current"


def report_state(state, quote, report_day):
    """收盘后报告：当日最后一条成交即收盘价，保留真实时间戳与状态标签。"""
    if (
        report_day is not None
        and state == "stale"
        and quote is not None
        and quote.as_of is not None
        and quote.as_of.astimezone(ZoneInfo("Asia/Shanghai")).date() == report_day
    ):
        return "close_mark"
    return state


def add_admission(instrument):
    """A catalog instrument can be recorded as an existing holding.

    Quotes still gate valuation, not the user's ability to record what they own.
    Stale, delayed, close and NAV marks are displayed with their real timestamp
    and are never relabelled as realtime.
    """
    if not instrument.enabled:
        return instrument.unsupported_reason or "instrument_disabled"
    if not instrument.commercial_display:
        return "display_rights_unconfirmed"
    return None


def instrument_view(instrument, quote, at, report_date=None):
    reason = add_admission(instrument)
    return {
        "id": str(instrument.id),
        "symbol": instrument.symbol,
        "name": instrument.name,
        "market": instrument.market,
        "asset_class": instrument.asset_class,
        "currency": instrument.currency,
        "quote_kind": instrument.quote_kind,
        "provider": instrument.provider,
        "can_add": reason is None,
        "unavailable_reason": reason,
        "quote_status": report_state(quote_status(instrument, quote, at), quote, report_date),
        "price": decimal(quote.price)
        if quote and instrument.commercial_display and instrument.enabled
        else None,
        "as_of": quote.as_of.isoformat()
        if quote and instrument.commercial_display and instrument.enabled
        else None,
    } | nav_change(instrument, quote, at)


@financial_precision
def nav_change(instrument, quote, at):
    valid = (
        quote
        and instrument.enabled
        and instrument.commercial_display
        and quote.provider == instrument.provider
        and quote.kind == instrument.quote_kind == "nav"
        and quote.as_of <= at
        and quote.previous_price is not None
        and quote.previous_price > 0
        and quote.previous_as_of is not None
        and quote.previous_as_of < quote.as_of
    )
    if not valid:
        return {"nav_change": None, "nav_change_pct": None, "nav_previous_as_of": None}
    return {
        "nav_change": decimal(quote.price - quote.previous_price),
        "nav_change_pct": decimal(
            (quote.price - quote.previous_price) / quote.previous_price * 100
        ),
        "nav_previous_as_of": quote.previous_as_of.isoformat(),
    }


@financial_precision
def position_view(p):
    return {
        "id": str(p.id),
        "account_id": str(p.account_id),
        "instrument_id": str(p.instrument_id),
        "position_type": p.position_type,
        "quantity": decimal(p.quantity),
        "cost_pool": decimal(p.cost_pool),
        "average_cost": decimal(p.cost_pool / p.quantity) if p.quantity else None,
        "realized_pnl": decimal(p.realized_pnl),
        "version": p.version,
        "last_occurred_at": p.last_occurred_at.isoformat(),
        "history_complete": p.history_complete,
        "opening_cost_source": p.opening_cost_source,
        "opening_cost_estimated": p.opening_cost_source is not None,
        "opening_quote_as_of": p.opening_quote_as_of.isoformat() if p.opening_quote_as_of else None,
    }


def open_position(session, user_id, body, key, refreshed_after=None):
    def action():
        portfolio = lock_portfolio(session, user_id)
        # The portfolio lock serializes first-position requests so only one
        # implicit account is created. Existing account/ledger history is retained.
        if body.account_id is not None:
            account = owned(session, Account, body.account_id, user_id)
        else:
            account = session.scalar(
                select(Account)
                .where(Account.user_id == user_id)
                .order_by(Account.created_at, Account.id)
                .limit(1)
            )
            if account is None:
                account = Account(user_id=user_id, portfolio_id=portfolio.id, name="默认持仓")
                session.add(account)
                session.flush()
        instrument = session.get(Instrument, body.instrument_id)
        require(instrument is not None, "not_found", "标的不存在", 404)
        reason = add_admission(instrument)
        require(reason is None, "instrument_unavailable", f"暂不能新增持仓：{reason}", 422)
        require(body.occurred_at <= now(), "invalid_date", "记录日期不能在未来", 422)
        existing = session.scalar(
            select(Position).where(
                Position.user_id == user_id,
                Position.account_id == account.id,
                Position.instrument_id == body.instrument_id,
            )
        )
        require(existing is None, "duplicate_position", "已有此标的的持仓，请记录买卖")
        price = body.price
        quote = None
        if price is None:
            quote = session.get(LatestQuote, body.instrument_id)
            state = quote_status(instrument, quote, now())
            # A newly fetched closing/delayed mark is a reference cost, never a
            # current valuation. Do not silently reuse an old cached stale mark.
            refreshed_stale = bool(
                state == "stale"
                and refreshed_after is not None
                and quote.fetched_at >= refreshed_after
                and quote.as_of <= now()
                and quote.provider == instrument.provider
                and quote.kind == instrument.quote_kind
            )
            require(
                state in {"current", "nav"} or refreshed_stale,
                "cost_quote_unavailable",
                "未能取得可用报价，请手动填写单位成本",
                422,
            )
            price = quote.price
        balance = apply(Balance(), "opening_snapshot", body.quantity, price)
        p = Position(
            user_id=user_id,
            account_id=account.id,
            instrument_id=body.instrument_id,
            position_type=body.position_type,
            quantity=balance.quantity,
            cost_pool=balance.cost_pool,
            realized_pnl=balance.realized,
            version=1,
            last_occurred_at=body.occurred_at,
            history_complete=False,
            opening_cost_source=quote.provider if quote else None,
            opening_quote_as_of=quote.as_of if quote else None,
        )
        session.add(p)
        session.flush()
        session.add(
            LedgerEvent(
                user_id=user_id,
                position_id=p.id,
                position_version=1,
                kind="opening_snapshot",
                quantity=body.quantity,
                price=price,
                fee=0,
                occurred_at=body.occurred_at,
            )
        )
        portfolio.version += 1
        session.add(
            Outbox(
                user_id=user_id,
                kind="portfolio_changed",
                dedupe_key=f"position:{p.id}:1",
                payload={"position_id": str(p.id)},
            )
        )
        return position_view(p)

    return idempotent(session, user_id, "opening", key, body.model_dump(mode="json"), action)


def trade(session, user_id, body, key):
    def action():
        portfolio = lock_portfolio(session, user_id)
        p = owned(session, Position, body.position_id, user_id, lock=True)
        require(p.version == body.expected_version, "version_conflict", "持仓已更新，请刷新后重试")
        require(
            p.last_occurred_at <= body.occurred_at <= now(),
            "invalid_date",
            "日期不能早于最近记录或晚于现在",
            422,
        )
        balance = apply(
            Balance(p.quantity, p.cost_pool, p.realized_pnl),
            body.kind,
            body.quantity,
            body.price,
            body.fee,
        )
        p.quantity, p.cost_pool, p.realized_pnl = (
            balance.quantity,
            balance.cost_pool,
            balance.realized,
        )
        p.version += 1
        p.last_occurred_at = body.occurred_at
        portfolio.version += 1
        session.add(
            LedgerEvent(
                user_id=user_id,
                position_id=p.id,
                position_version=p.version,
                kind=body.kind,
                quantity=body.quantity,
                price=body.price,
                fee=body.fee,
                occurred_at=body.occurred_at,
            )
        )
        session.add(
            Outbox(
                user_id=user_id,
                kind="portfolio_changed",
                dedupe_key=f"position:{p.id}:{p.version}",
                payload={"position_id": str(p.id)},
            )
        )
        return position_view(p)

    return idempotent(session, user_id, "trade", key, body.model_dump(mode="json"), action)


@financial_precision
def snapshot(
    session,
    user_id,
    at=None,
    report_date=None,
    close_marks=True,
    close_mark_max_age_days=CLOSE_MARK_MAX_AGE_DAYS,
):
    """Current valuation. Stale realtime/delayed marks are shown as honest close
    marks (real timestamp kept, never relabelled realtime) when the market is
    closed, or for closed-market reports whose period is the last close's day
    or a later non-trading day. Missing/FX gaps are still never filled with cost
    or zero, and arithmetic stays exact."""
    at = at or now()
    report_day = report_date if report_date is not None else None
    portfolio = session.scalar(
        select(Portfolio).where(Portfolio.user_id == user_id).with_for_update(read=True)
    )
    require(portfolio is not None, "not_found", "组合不存在", 404)
    rows = session.execute(
        select(Position, Instrument, LatestQuote)
        .join(Instrument, Position.instrument_id == Instrument.id)
        .outerjoin(LatestQuote, LatestQuote.instrument_id == Instrument.id)
        .where(Position.user_id == user_id)
        .order_by(Position.id)
    ).all()
    rates = {r.currency: r for r in session.scalars(select(FXRate))}
    positions, missing, values = [], [], []
    partial_value = Decimal(0)
    data_providers = set()
    unrealized_cny = Decimal(0)
    realized_by_currency = {}
    for p, instrument, quote in rows:
        item = (
            position_view(p)
            | {
                "name": instrument.name,
                "symbol": instrument.symbol,
                "market": instrument.market,
                "asset_class": instrument.asset_class,
                "currency": instrument.currency,
                "quote_kind": instrument.quote_kind,
            }
            | nav_change(instrument, quote, at)
        )
        realized_by_currency[instrument.currency] = (
            realized_by_currency.get(instrument.currency, Decimal(0)) + p.realized_pnl
        )
        if p.quantity == 0:
            item.update(
                market_value="0",
                market_value_cny="0",
                unrealized_pnl="0",
                quote_status="not_needed",
                price=None,
                as_of=None,
            )
            positions.append(item)
            continue
        state = quote_status(instrument, quote, at)
        if report_day is not None:
            state = report_state(state, quote, report_day)
            state = close_mark_state(state, quote, report_day, close_mark_max_age_days)
        elif close_marks and not market_open(instrument.market, at):
            state = close_mark_state(
                state, quote, at.astimezone(CN_TZ).date(), close_mark_max_age_days
            )
        fx = rates.get(instrument.currency)
        data_providers.add(instrument.provider)
        if instrument.currency != "CNY":
            data_providers.add(fx.provider if fx else "unknown_fx")
        rate = (
            Decimal(1)
            if instrument.currency == "CNY"
            else (fx.rate if fx and fx.as_of <= at < fx.valid_until else None)
        )
        # Preserve stale marks for display, but exclude them from current totals.
        item.update(
            quote_status=state,
            price=decimal(quote.price)
            if quote and instrument.commercial_display and instrument.enabled
            else None,
            as_of=quote.as_of.isoformat()
            if quote and instrument.commercial_display and instrument.enabled
            else None,
            market_value=None,
            market_value_cny=None,
            unrealized_pnl=None,
        )
        if state in {"current", "close_mark"}:
            value = p.quantity * quote.price
            unrealized = value - p.cost_pool
            item.update(market_value=decimal(value), unrealized_pnl=decimal(unrealized))
            if rate is not None:
                converted = value * rate
                item["market_value_cny"] = decimal(converted)
                partial_value += converted
                unrealized_cny += unrealized * rate
                values.append((item, converted))
        # NAV（净值型）计入报告覆盖，但按产品口径不并入盘中市值合计。
        priced_states = {"current", "close_mark", "nav"}
        if state not in priced_states:
            missing.append({"position_id": str(p.id), "reason": state})
        elif state in {"current", "close_mark"} and rate is None:
            missing.append({"position_id": str(p.id), "reason": "fx_missing"})
        positions.append(item)
    complete = not missing
    for item, value in values:
        item["weight_pct"] = (
            decimal(value / partial_value * 100) if complete and partial_value else None
        )
    active_count = sum(1 for p, _, _ in rows if p.quantity > 0)
    # Asset change is anchored on real report marks, never a fabricated series.
    # newest-first complete daily snapshots: [0] is the latest closed book.
    history_values = recent_complete_daily_values(session, user_id)
    daily_pnl = None
    if complete and values and history_values:
        daily_pnl = partial_value - history_values[0]
    yesterday_pnl = history_values[0] - history_values[1] if len(history_values) >= 2 else None
    cumulative_pnl = None
    if complete and active_count > 0:
        realized_cny = Decimal(0)
        realized_ok = True
        for currency, amount in realized_by_currency.items():
            if currency == "CNY":
                realized_cny += amount
                continue
            fx = rates.get(currency)
            if fx is None or not (fx.as_of <= at < fx.valid_until):
                realized_ok = False
                break
            realized_cny += amount * fx.rate
        if realized_ok:
            cumulative_pnl = unrealized_cny + realized_cny
    return {
        "portfolio_id": str(portfolio.id),
        "version": portfolio.version,
        "as_of": at.isoformat(),
        "valuation_method": "moving_average_v1",
        "complete": complete,
        "coverage": {"total": active_count, "priced": active_count - len(missing)},
        "missing": missing,
        "market_value_cny": decimal(partial_value) if complete and values else None,
        "priced_market_value_cny": decimal(partial_value),
        "unrealized_pnl_cny": decimal(unrealized_cny) if complete else None,
        "realized_pnl_by_currency": {k: decimal(v) for k, v in realized_by_currency.items()},
        "historical_realized_pnl_cny": None,
        "daily_pnl_cny": decimal(daily_pnl),
        "yesterday_pnl_cny": decimal(yesterday_pnl),
        "cumulative_pnl_cny": decimal(cumulative_pnl),
        "history_complete": all(p.history_complete for p, _, _ in rows),
        "positions": positions,
        "market_data_providers": sorted(data_providers),
    }


def asset_history(session, user_id):
    """Actual complete daily-report marks, not a return curve or fabricated daily series."""
    snapshots = session.execute(
        select(
            Report.snapshot["complete"],
            Report.snapshot["as_of"],
            Report.snapshot["market_value_cny"],
        )
        .where(Report.user_id == user_id, Report.kind.in_(DAILY_KINDS))
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(90)
    )
    points = {}
    for complete, as_of, amount in snapshots:
        if complete is not True:
            continue
        try:
            stamp = datetime.fromisoformat(as_of)
            value = Decimal(amount)
            if stamp.tzinfo is None or not value.is_finite() or value < 0:
                continue
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        points.setdefault(stamp, {"as_of": stamp.isoformat(), "market_value_cny": decimal(value)})
    return [points[stamp] for stamp in sorted(points)]
