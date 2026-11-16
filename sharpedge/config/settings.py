"""Application settings with fail-closed live trading controls."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment and optional .env files."""

    model_config = SettingsConfigDict(
        env_file=("sharpedge/config/example.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    polymarket_api_base: str = Field(
        "https://data-api.polymarket.com", alias="POLYMARKET_API_BASE"
    )
    polymarket_clob_base: str = Field(
        "https://clob.polymarket.com", alias="POLYMARKET_CLOB_BASE"
    )
    polymarket_gamma_base: str = Field(
        "https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_BASE"
    )
    polymarket_ws_url: str = Field(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        alias="POLYMARKET_WS_URL",
    )

    odds_api_key: str | None = Field(None, alias="ODDS_API_KEY")
    odds_api_base: str | None = Field(
        "https://api.the-odds-api.com/v4", alias="ODDS_API_BASE"
    )
    odds_api_io_key: str | None = Field(None, alias="ODDS_API_IO_KEY")
    odds_api_io_base: str = Field(
        "https://api.odds-api.io/v3", alias="ODDS_API_IO_BASE"
    )
    odds_api_io_bookmakers: str = Field(
        "Pinnacle,Bet365,Betfair,Unibet", alias="ODDS_API_IO_BOOKMAKERS"
    )
    odds_api_io_max_requests_per_hour: int = Field(
        90, ge=1, le=90, alias="ODDS_API_IO_MAX_REQUESTS_PER_HOUR"
    )
    odds_api_io_rate_limit_state: Path = Field(
        Path(".sharpedge_cache/odds_api_io_rate_limit.json"),
        alias="ODDS_API_IO_RATE_LIMIT_STATE",
    )
    odds_api_io_cache_dir: Path = Field(
        Path("data/raw/cache/odds_api_io"),
        alias="ODDS_API_IO_CACHE_DIR",
    )

    database_url: str = Field("sqlite:///sharpedge.db", alias="DATABASE_URL")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    mode: Literal["mock", "research", "paper", "live"] = Field(
        "research", alias="SHARPEDGE_MODE"
    )

    enable_live_trading: bool = Field(False, alias="ENABLE_LIVE_TRADING")
    polymarket_private_key: str | None = Field(None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_api_key: str | None = Field(None, alias="POLYMARKET_API_KEY")
    polymarket_api_secret: str | None = Field(None, alias="POLYMARKET_API_SECRET")
    polymarket_api_passphrase: str | None = Field(
        None, alias="POLYMARKET_API_PASSPHRASE"
    )
    polymarket_funder: str | None = Field(None, alias="POLYMARKET_FUNDER")

    max_position_per_market: float = Field(100.0, alias="MAX_POSITION_PER_MARKET")
    max_position_per_event: float = Field(250.0, alias="MAX_POSITION_PER_EVENT")
    max_total_unresolved_exposure: float = Field(
        1_000.0, alias="MAX_TOTAL_UNRESOLVED_EXPOSURE"
    )
    max_daily_loss: float = Field(100.0, alias="MAX_DAILY_LOSS")
    max_trade_size: float = Field(25.0, alias="MAX_TRADE_SIZE")
    min_adjusted_edge: float = Field(0.015, alias="MIN_ADJUSTED_EDGE")
    min_liquidity: float = Field(5.0, alias="MIN_LIQUIDITY")

    default_slippage_bps: float = Field(20.0, alias="DEFAULT_SLIPPAGE_BPS")
    model_uncertainty_bps: float = Field(75.0, alias="MODEL_UNCERTAINTY_BPS")
    resolution_risk_bps: float = Field(25.0, alias="RESOLUTION_RISK_BPS")
    safety_margin_bps: float = Field(50.0, alias="SAFETY_MARGIN_BPS")

    max_orderbook_age_seconds: int = Field(120, alias="MAX_ORDERBOOK_AGE_SECONDS")
    max_odds_age_seconds: int = Field(300, alias="MAX_ODDS_AGE_SECONDS")
    min_seconds_to_start: int = Field(300, alias="MIN_SECONDS_TO_START")

    @property
    def local_database_path(self) -> Path:
        """Return the local database path for sqlite/duckdb-style URLs."""

        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///"))
        if self.database_url.startswith("duckdb:///"):
            return Path(self.database_url.removeprefix("duckdb:///"))
        return Path(self.database_url)

    @property
    def trading_cost_bps(self) -> float:
        return (
            self.default_slippage_bps
            + self.model_uncertainty_bps
            + self.resolution_risk_bps
            + self.safety_margin_bps
        )

    @property
    def required_live_credentials_present(self) -> bool:
        required = [
            self.polymarket_private_key,
            self.polymarket_api_key,
            self.polymarket_api_secret,
            self.polymarket_api_passphrase,
        ]
        return all(bool(value) for value in required)

    @model_validator(mode="after")
    def fail_closed_for_live_trading(self) -> "Settings":
        if self.mode == "live" and not self.enable_live_trading:
            raise ValueError("SHARPEDGE_MODE=live requires ENABLE_LIVE_TRADING=true")
        if self.enable_live_trading and not self.required_live_credentials_present:
            raise ValueError(
                "ENABLE_LIVE_TRADING=true requires Polymarket private key and API credentials"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings loader."""

    return Settings()
