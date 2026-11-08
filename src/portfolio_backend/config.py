from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PA_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://pa_app:local-app-only@localhost:55432/portfolio"
    control_database_url: str = (
        "postgresql+psycopg://pa_control:local-control-only@localhost:55432/portfolio"
    )
    migration_database_url: str = (
        "postgresql+psycopg://postgres:local-owner-only@localhost:55432/portfolio"
    )
    cache_url: str = "redis://localhost:56379/0"
    auth_cache_url: str = ""
    broker_url: str = "redis://localhost:56380/0"
    apple_bundle_id: str = "com.portfolioagent.ios"
    store_environment: Literal["Sandbox", "Production"] = "Sandbox"
    ai_gateway_url: str = ""
    ai_gateway_key: SecretStr = SecretStr("")
    ai_provider_id: str = "deepseek"
    ai_disclosure_version: str = "2"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-flash"
    ai_max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    # Per-call output cap for the ReAct loop. DeepSeek reasoning tokens count
    # against this budget; 2048 is too small for thinking-enabled answers.
    agent_max_output_tokens: int = Field(default=8192, ge=1024, le=128000)
    ai_max_input_chars: int = Field(default=60000, ge=1000, le=200000)
    agent_trace_retention_days: int = Field(default=30, ge=1, le=365)
    agent_trace_capture_content: bool = False
    simulator_ai: bool = False
    agent_max_rounds: int = Field(default=12, ge=1, le=20)
    agent_timeout_seconds: int = Field(default=95, ge=1, le=100)
    agent_tool_timeout_seconds: int = Field(default=15, ge=1, le=30)
    agent_max_tokens: int = Field(default=64000, ge=1024, le=128000)
    agent_result_max_chars: int = Field(default=12000, ge=1000, le=32000)
    agent_identical_call_limit: int = Field(default=3, ge=2, le=5)
    agent_thinking: bool = True
    # Separate rights from mobile display: quote/FX data and derived values sent to the model.
    ai_market_data_providers: set[str] = set()
    data_collection_enabled: bool = False
    # The catalog is built from AKShare's public symbol lists; no per-symbol admin entry.
    catalog_auto_discover: bool = True
    catalog_live_search_enabled: bool = True
    catalog_sync_hours: int = Field(default=24, ge=1, le=720)
    # Deployment-level confirmation for AKShare display, not a legal conclusion.
    akshare_display_confirmed: bool = True
    # Per-provider confirmation for the particular paid-app usage, not an automatic licence.
    commercial_data_providers: set[str] = set()
    news_display_providers: set[str] = set()
    quote_poll_seconds: int = Field(default=120, ge=60, le=3600)
    # A stale realtime/delayed mark may be shown as the latest close (status
    # close_mark) while the market is closed, within this many days. 0 disables.
    close_mark_max_age_days: int = Field(default=4, ge=0, le=10)
    # Outside CN trading sessions (Mon-Fri 09:30-11:30 / 13:00-15:00 Asia/Shanghai)
    # the collector throttles quotes and news to these slower cadences.
    off_hours_quote_poll_seconds: int = Field(default=600, ge=60, le=3600)
    news_poll_seconds: int = Field(default=300, ge=120, le=86400)
    off_hours_news_poll_seconds: int = Field(default=900, ge=120, le=86400)
    fx_poll_seconds: int = Field(default=21600, ge=1800, le=86400)
    search_daily_limit: int = Field(default=100, ge=0, le=10000)
    data_max_instruments_per_tick: int = Field(default=200, ge=1, le=500)
    # Resident fetch worker processes for the AKShare pool; akshare is imported
    # inside the workers, never in the caller. Sequential collection uses one worker;
    # the rest serve concurrent API refreshes and absorb worker crashes.
    data_fetch_workers: int = Field(default=2, ge=1, le=8)
    admin_credentials: dict[str, dict[str, str]] = {}
    access_seconds: int = 900
    refresh_days: int = 30
    test_phone_login_enabled: bool = False
    aliyun_access_key_id: SecretStr = SecretStr("")
    aliyun_access_key_secret: SecretStr = SecretStr("")
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_enabled: bool = False
    aliyun_carrier_enabled: bool = False
    phone_auth_pepper: SecretStr = SecretStr("")
    carrier_daily_limit: int = Field(default=1000, ge=1, le=100000)
    sms_daily_limit: int = Field(default=100, ge=1, le=100000)
    job_lease_seconds: int = 180
    max_job_attempts: int = 3
    # Dispatcher work per 2s cycle; larger keeps up with the 18:00 outbox burst.
    dispatch_batch_limit: int = Field(default=500, ge=1, le=2000)
    # Scheduled report delivery is staggered by user-id order: every group of
    # this many users starts `report_dispatch_batch_interval_seconds` later.
    report_dispatch_batch_users: int = Field(default=50, ge=1, le=2000)
    report_dispatch_batch_interval_seconds: int = Field(default=15, ge=1, le=3600)
    # Plan A report pipeline: deterministic facts are generated per batch (no AI);
    # AI summaries are separate jobs for eligible users only, running with bounded
    # in-task thread concurrency so one process serves several summaries.
    report_batch_size: int = Field(default=500, ge=1, le=2000)
    report_ai_batch_size: int = Field(default=8, ge=1, le=50)
    report_ai_concurrency: int = Field(default=4, ge=1, le=16)
    report_ai_budget_seconds: int = Field(default=480, ge=60, le=3600)
    report_job_lease_seconds: int = Field(default=900, ge=180, le=7200)
    report_ai_enabled: bool = True
    # Deterministic per-user sampling for AI summaries (1.0 = every user, as before).
    report_ai_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    database_pool_size: int = Field(default=10, ge=1, le=50)
    control_database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_statement_timeout_ms: int = Field(default=10000, ge=1000, le=120000)

    @model_validator(mode="after")
    def secure_production(self):
        if self.test_phone_login_enabled and self.store_environment != "Sandbox":
            raise ValueError("Test phone login is restricted to isolated Sandbox deployments")
        if self.aliyun_sms_enabled or self.aliyun_carrier_enabled:
            if self.environment == "production" and not self.auth_cache_url:
                raise ValueError("Production phone auth requires a dedicated noeviction Redis URL")
            if (
                not self.aliyun_access_key_id.get_secret_value()
                or not self.aliyun_access_key_secret.get_secret_value()
            ):
                raise ValueError("Phone authentication requires Aliyun credentials")
            if len(self.phone_auth_pepper.get_secret_value()) < 32:
                raise ValueError(
                    "Phone authentication requires a random pepper of at least 32 characters"
                )
        if self.aliyun_sms_enabled and not (
            self.aliyun_sms_sign_name and self.aliyun_sms_template_code
        ):
            raise ValueError("SMS requires an approved sign name and template code")
        if self.agent_trace_capture_content and (
            not self.ai_disclosure_version.isdecimal() or int(self.ai_disclosure_version) < 3
        ):
            raise ValueError("Full trace capture requires disclosure version 3 or newer")
        if self.simulator_ai and (
            self.environment != "development"
            or self.store_environment != "Sandbox"
            or self.ai_provider_id != "local-simulator"
            or self.deepseek_api_key.get_secret_value()
            or self.ai_gateway_url
        ):
            raise ValueError(
                "Simulator AI requires isolated development Sandbox, local-simulator provider and no external AI credentials"
            )
        if self.deepseek_api_key.get_secret_value():
            if self.ai_provider_id != "deepseek" or self.ai_gateway_url:
                raise ValueError("DeepSeek credentials require provider=deepseek and no AI gateway")
        if self.environment == "production":
            for url in (self.database_url, self.control_database_url):
                if "local-" in url or "postgres:postgres@" in url:
                    raise ValueError("Production requires separately provisioned database roles")
            if self.ai_gateway_url and not self.ai_gateway_url.startswith("https://"):
                raise ValueError("Production AI gateway must use HTTPS")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
