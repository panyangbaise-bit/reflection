from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from portfolio_backend.domain.instruments import resolve_cn_symbol

Quantity = Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=10, le=Decimal("1e12"))]
Price = Annotated[Decimal, Field(gt=0, max_digits=26, decimal_places=12, le=Decimal("1e12"))]
Fee = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=8, le=Decimal("1e12"))]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def decimal_wire_format(cls, value, info):
        if info.field_name in {"quantity", "price", "fee", "rate"} and isinstance(
            value, (float, int)
        ):
            raise ValueError("Financial values must be decimal strings")
        return value


class AppleLogin(Input):
    challenge_id: UUID
    identity_token: str = Field(min_length=1, max_length=10000)


class Refresh(Input):
    refresh_token: str = Field(min_length=20, max_length=256)


class PhoneCode(Input):
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")
    agreements_accepted: Literal[True]


class PhoneProof(Input):
    challenge_id: UUID
    code: str = Field(pattern=r"^[0-9]{6}$")


class PhoneLogin(PhoneProof):
    agreements_accepted: Literal[True]


class PhoneRecovery(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")
    password: str = Field(min_length=10, max_length=128)
    new_phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")
    agreements_accepted: Literal[True]


class PhoneChangeCode(PhoneProof):
    new_phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")


class PasswordSet(PhoneProof):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=10, max_length=128)


class PasswordSetup(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=10, max_length=128)


class PasswordLogin(PasswordSetup):
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")
    agreements_accepted: Literal[True]


class CarrierLogin(Input):
    access_token: str = Field(min_length=20, max_length=10000)
    agreements_accepted: Literal[True]


class CarrierPassword(PasswordSetup):
    access_token: str = Field(min_length=20, max_length=10000)
    agreements_accepted: Literal[True]


class CarrierPhoneChange(PasswordLogin):
    access_token: str = Field(min_length=20, max_length=10000)


class AccountCreate(Input):
    name: str = Field(min_length=1, max_length=100)


class OpeningPosition(Input):
    position_type: Literal["core", "satellite"] = "core"
    account_id: UUID | None = None
    instrument_id: UUID
    quantity: Quantity
    price: Price | None = None
    occurred_at: AwareDatetime


class Trade(Input):
    position_id: UUID
    kind: Literal["buy", "sell"]
    quantity: Quantity
    price: Price
    fee: Fee = Decimal("0")
    expected_version: int = Field(ge=1)
    occurred_at: AwareDatetime


class ConsentInput(Input):
    granted: bool
    provider: str = Field(min_length=1, max_length=64)
    disclosure_version: str = Field(min_length=1, max_length=32)


class PreferencesInput(Input):
    daily_report: bool | None = None
    weekly_report: bool | None = None
    report_push: bool | None = None
    news_push: bool | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, v):
        if v is not None:
            try:
                ZoneInfo(v)
            except (ValueError, KeyError) as exc:
                raise ValueError("Unknown timezone") from exc
        return v


class DisciplineInput(Input):
    expected_updated_at: AwareDatetime | None = None
    content: str = Field(max_length=2000)


class Question(Input):
    content: str = Field(min_length=1, max_length=4000)


class ConversationCreate(Input):
    title: str = Field(default="新的对话", min_length=1, max_length=100)


class WatchlistInput(Input):
    instrument_id: UUID
    priority: int = Field(default=3, ge=1, le=5)
    target_price: Price | None = None
    note: str = Field(default="", max_length=1000)


class QuoteInput(Input):
    instrument_id: UUID
    price: Price
    kind: Literal["realtime", "delayed", "close", "nav"]
    provider: str = Field(min_length=1, max_length=64)
    as_of: AwareDatetime
    valid_until: AwareDatetime
    market_state: Literal["open", "closed", "halted"]
    previous_price: Price | None = None
    previous_as_of: AwareDatetime | None = None

    @model_validator(mode="after")
    def previous_nav_valid(self):
        if (self.previous_price is None) != (self.previous_as_of is None):
            raise ValueError("previous price and timestamp must be supplied together")
        if self.previous_price is not None and (
            self.kind != "nav" or self.previous_as_of >= self.as_of
        ):
            raise ValueError("previous NAV must precede current NAV")
        return self


class InstrumentInput(Input):
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    market: Literal["CN", "HK", "US"]
    asset_class: Literal["stock", "etf", "fund", "listed_fund", "gold"]
    currency: Literal["CNY", "USD", "HKD"]
    provider: str = Field(min_length=1, max_length=64)
    provider_symbol: str | None = Field(default=None, max_length=64)
    quote_kind: Literal["realtime", "delayed", "close", "nav"]
    enabled: bool = False
    commercial_display: bool = False
    freshness_seconds: int = Field(default=120, ge=1, le=604800)
    unsupported_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_symbol(self):
        if self.market == "CN":
            kind = "etf" if self.asset_class == "gold" else self.asset_class
            _, self.symbol = resolve_cn_symbol(f"{kind}:{self.symbol}")
        return self


class ReportRequest(Input):
    kind: Literal["daily", "weekly"]
    period: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    replay: bool = False

    @field_validator("period")
    @classmethod
    def real_date(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v


class DeviceInput(Input):
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,200}$")
    environment: Literal["sandbox", "production"]


class DeletionInput(Input):
    confirmation: Literal["DELETE"]


class AdminAction(Input):
    reason: str = Field(min_length=5, max_length=500)


class LayoutInput(Input):
    expected_version: int = Field(ge=0)
    overview_order: list[Literal["discipline", "latest", "holdings"]]

    @field_validator("overview_order")
    @classmethod
    def complete_order(cls, value):
        if len(value) != 3 or len(set(value)) != 3:
            raise ValueError("All three blocks must occur once")
        return value


class PositionTypeInput(Input):
    expected_version: int = Field(ge=1)
    position_type: Literal["core", "satellite"]
