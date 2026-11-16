"""Runtime settings with fail-closed trading defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated configuration for data collection and paper-only research."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        allow_inf_nan=False,
    )

    polymarket_gamma_base: str = Field(
        "https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_BASE"
    )
    polymarket_clob_base: str = Field("https://clob.polymarket.com", alias="POLYMARKET_CLOB_BASE")
    database_url: str = Field("sqlite:///data/edgelab.sqlite", alias="DATABASE_URL")
    cache_dir: Path = Field(Path("data/raw/cache/edgelab"), alias="EDGELAB_CACHE_DIR")
    live_data_dir: Path = Field(Path("data/live"), alias="EDGELAB_LIVE_DATA_DIR")

    odds_provider: str | None = Field(None, alias="ODDS_PROVIDER")
    odds_api_key: str | None = Field(None, alias="ODDS_API_KEY")
    odds_api_base: str = Field("https://api.the-odds-api.com/v4", alias="ODDS_API_BASE")
    odds_api_sport_keys: str = Field("soccer_epl", alias="ODDS_API_SPORT_KEYS")
    odds_api_io_key: str | None = Field(None, alias="ODDS_API_IO_KEY")
    odds_api_io_base: str = Field("https://api.odds-api.io/v3", alias="ODDS_API_IO_BASE")
    odds_api_io_bookmakers: str = Field("Bet365,Unibet", alias="ODDS_API_IO_BOOKMAKERS")
    the_odds_api_key: str | None = Field(None, alias="THE_ODDS_API_KEY")
    the_odds_api_base: str | None = Field(None, alias="THE_ODDS_API_BASE")
    the_odds_api_sport_keys: str | None = Field(None, alias="THE_ODDS_API_SPORT_KEYS")
    the_odds_api_regions: str = Field("us,uk,eu", alias="THE_ODDS_API_REGIONS")
    odds_provider_event_limit: int = Field(20, alias="ODDS_PROVIDER_EVENT_LIMIT")
    crypto_api_base: str = Field("https://api.coingecko.com/api/v3", alias="CRYPTO_API_BASE")
    weather_api_base: str = Field("https://api.open-meteo.com/v1", alias="WEATHER_API_BASE")
    weather_api_key: str | None = Field(None, alias="WEATHER_API_KEY")
    polling_api_key: str | None = Field(None, alias="POLLING_API_KEY")
    provider_rate_limit_per_minute: int = Field(30, alias="PROVIDER_RATE_LIMIT_PER_MINUTE")
    odds_rate_limit_per_minute: int = Field(10, alias="ODDS_RATE_LIMIT_PER_MINUTE")
    polymarket_rate_limit_per_minute: int = Field(75, alias="POLYMARKET_RATE_LIMIT_PER_MINUTE")
    crypto_rate_limit_per_minute: int = Field(30, alias="CRYPTO_RATE_LIMIT_PER_MINUTE")
    weather_rate_limit_per_minute: int = Field(30, alias="WEATHER_RATE_LIMIT_PER_MINUTE")

    enable_live_trading: bool = Field(False, alias="ENABLE_LIVE_TRADING")
    max_position_per_market: float = Field(250.0, alias="MAX_POSITION_PER_MARKET")
    max_position_per_event: float = Field(500.0, alias="MAX_POSITION_PER_EVENT")
    max_total_unresolved_exposure: float = Field(2000.0, alias="MAX_TOTAL_UNRESOLVED_EXPOSURE")
    max_daily_loss: float = Field(250.0, alias="MAX_DAILY_LOSS")
    max_trade_size: float = Field(100.0, alias="MAX_TRADE_SIZE")
    min_liquidity: float = Field(10.0, alias="MIN_LIQUIDITY")
    max_spread: float = Field(0.08, alias="MAX_SPREAD")
    min_adjusted_edge: float = Field(0.015, alias="MIN_ADJUSTED_EDGE")
    default_slippage_bps: float = Field(25.0, alias="DEFAULT_SLIPPAGE_BPS")
    fee_bps: float = Field(20.0, alias="FEE_BPS")
    model_uncertainty_bps: float = Field(75.0, alias="MODEL_UNCERTAINTY_BPS")
    resolution_risk_bps: float = Field(30.0, alias="RESOLUTION_RISK_BPS")
    safety_margin_bps: float = Field(50.0, alias="SAFETY_MARGIN_BPS")
    max_data_age_seconds: int = Field(900, alias="MAX_DATA_AGE_SECONDS")
    max_orderbook_age_seconds: int = Field(30, gt=0, alias="MAX_ORDERBOOK_AGE_SECONDS")
    max_odds_age_seconds: int = Field(180, gt=0, alias="MAX_ODDS_AGE_SECONDS")
    max_cross_source_skew_seconds: int = Field(60, gt=0, alias="MAX_CROSS_SOURCE_SKEW_SECONDS")
    settlement_registry_file: Path | None = Field(None, alias="SETTLEMENT_REGISTRY_FILE")
    paper_starting_cash: float = Field(10000.0, gt=0, alias="PAPER_STARTING_CASH")
    odds_api_io_rate_limit_state: Path = Field(
        Path(".sharpedge_cache/odds_api_io_rate_limit.json"), alias="ODDS_API_IO_RATE_LIMIT_STATE"
    )
    odds_api_io_max_requests_per_hour: int = Field(
        90, ge=1, le=90, alias="ODDS_API_IO_MAX_REQUESTS_PER_HOUR"
    )

    @property
    def sqlite_path(self) -> Path:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///"))
        return Path("data/edgelab.sqlite")

    @model_validator(mode="after")
    def live_trading_requires_explicit_setup(self) -> "Settings":
        if self.enable_live_trading:
            raise ValueError(
                "Live trading is intentionally disabled in EdgeLab v0.1. "
                "Add a credential-aware live-trading config before enabling."
            )
        for name in (
            "max_position_per_market",
            "max_position_per_event",
            "max_total_unresolved_exposure",
            "max_daily_loss",
            "max_trade_size",
            "min_liquidity",
            "max_spread",
            "max_data_age_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "min_adjusted_edge",
            "default_slippage_bps",
            "fee_bps",
            "model_uncertainty_bps",
            "resolution_risk_bps",
            "safety_margin_bps",
        ):
            value = getattr(self, name)
            if not 0 <= value < float("inf"):
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in (
            "odds_rate_limit_per_minute",
            "provider_rate_limit_per_minute",
            "crypto_rate_limit_per_minute",
            "weather_rate_limit_per_minute",
            "polymarket_rate_limit_per_minute",
            "odds_provider_event_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        return self
