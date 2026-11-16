"""Typed data contracts used across collection, research, and execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PriceLevel(StrictModel):
    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)


class Market(StrictModel):
    market_id: str
    event_id: str
    sport: str
    league: str | None = None
    title: str
    description: str | None = None
    outcomes: list[str]
    token_ids: list[str]
    start_time: datetime | None = None
    end_time: datetime | None = None
    resolution_source: str | None = None
    status: str = "unknown"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("start_time", "end_time")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value else None

    @model_validator(mode="after")
    def outcomes_and_tokens_match_when_possible(self) -> "Market":
        if self.token_ids and len(self.outcomes) != len(self.token_ids):
            raise ValueError("outcomes and token_ids must have equal length")
        return self


class OrderBook(StrictModel):
    market_id: str
    token_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    def best_bid_ask(self) -> "BestBidAsk":
        bid = max(self.bids, key=lambda level: level.price, default=None)
        ask = min(self.asks, key=lambda level: level.price, default=None)
        best_bid = bid.price if bid else None
        best_ask = ask.price if ask else None
        mid = None
        spread = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
            spread = max(0.0, best_ask - best_bid)
        return BestBidAsk(
            market_id=self.market_id,
            token_id=self.token_id,
            timestamp=self.timestamp,
            best_bid=best_bid,
            best_bid_size=bid.size if bid else None,
            best_ask=best_ask,
            best_ask_size=ask.size if ask else None,
            mid=mid,
            spread=spread,
        )


class BestBidAsk(StrictModel):
    market_id: str
    token_id: str
    timestamp: datetime
    best_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    best_bid_size: float | None = Field(default=None, ge=0.0)
    best_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    best_ask_size: float | None = Field(default=None, ge=0.0)
    mid: float | None = Field(default=None, ge=0.0, le=1.0)
    spread: float | None = Field(default=None, ge=0.0)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ExternalOdds(StrictModel):
    event_id: str
    sport: str
    book: str
    market_type: str
    outcome: str
    decimal_odds: float = Field(gt=1.0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class FairValue(StrictModel):
    event_id: str
    market_id: str
    outcome: str
    fair_probability: float = Field(ge=0.0, le=1.0)
    source: str
    timestamp: datetime = Field(default_factory=utc_now)
    uncertainty: float = Field(ge=0.0, le=1.0)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


SignalAction = Literal["BUY_YES", "SELL_YES", "QUOTE_BID", "QUOTE_ASK", "HOLD", "AVOID"]


class Signal(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    market_id: str
    event_id: str | None = None
    outcome: str
    signal_type: str
    fair_probability: float = Field(ge=0.0, le=1.0)
    polymarket_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    polymarket_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    raw_edge: float
    adjusted_edge: float
    available_size: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    action: SignalAction
    reason: str

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class Trade(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    market_id: str
    event_id: str | None = None
    outcome: str
    side: Literal["BUY", "SELL"]
    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(gt=0.0)
    fees: float = Field(default=0.0, ge=0.0)
    slippage: float = Field(default=0.0, ge=0.0)
    signal_id: str | None = None
    paper_or_live: Literal["paper", "live"] = "paper"
    notes: str | None = None

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "BUY" else -self.size

    @property
    def cash_flow(self) -> float:
        if self.side == "BUY":
            return -(self.price * self.size + self.fees + self.slippage)
        return self.price * self.size - self.fees - self.slippage


class Resolution(StrictModel):
    market_id: str
    outcome: str
    resolved_outcome: str
    payout: float = Field(ge=0.0, le=1.0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RiskState(StrictModel):
    positions_by_market: dict[str, float] = Field(default_factory=dict)
    positions_by_event: dict[str, float] = Field(default_factory=dict)
    total_unresolved_exposure: float = 0.0
    daily_pnl: float = 0.0


class RiskDecision(StrictModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    max_approved_size: float = 0.0


class CrossMarketInconsistency(StrictModel):
    inconsistency_type: str
    involved_markets: list[str]
    implied_edge: float
    executable_legs: list[str] = Field(default_factory=list)
    liquidity_available: float = 0.0
    executable_after_costs: bool = False
    theoretical_only: bool = True
    reason: str


class PaperDecision(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    signal: Signal
    executed: bool
    trade: Trade | None = None
    reasons: list[str] = Field(default_factory=list)


class CLVObservation(StrictModel):
    market_id: str
    outcome: str
    signal_timestamp: datetime
    horizon: str
    entry_price: float
    fair_probability_at_horizon: float
    clv: float
    sport: str | None = None
    signal_type: str | None = None
    liquidity_bucket: str | None = None
    adjusted_edge_bucket: str | None = None


class MarketMakerQuote(StrictModel):
    market_id: str
    outcome: str
    bid: float | None = None
    ask: float | None = None
    quote: bool
    reason: str
    expected_value: float
