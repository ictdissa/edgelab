"""Typed data contracts for EdgeLab research."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


MarketCategory = Literal[
    "sports_moneyline",
    "sports_1x2",
    "sports_total",
    "sports_exact_score",
    "sports_outright",
    "crypto_threshold",
    "weather_threshold",
    "politics_election",
    "exhaustive_multi_outcome",
    "implication_market",
    "unknown",
]


class ThresholdPayoff(StrictModel):
    asset: str
    threshold: float = Field(gt=0)
    direction: Literal["above", "below"]
    expiry: datetime
    kind: Literal["terminal", "barrier"] = "terminal"
    unit: str = "USD"

    @field_validator("expiry")
    @classmethod
    def normalize_expiry(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class PredictionMarket(StrictModel):
    market_id: str
    event_id: str
    risk_event_id: str | None = Field(default=None, min_length=1)
    category: MarketCategory
    title: str
    description: str | None = None
    outcomes: list[str]
    token_ids: list[str]
    start_time: datetime | None = None
    end_time: datetime | None = None
    resolution_source: str | None = None
    status: str = "active"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    payoff: ThresholdPayoff | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value else None

    @model_validator(mode="after")
    def outcomes_and_tokens_align(self) -> "PredictionMarket":
        if self.token_ids and len(self.outcomes) != len(self.token_ids):
            raise ValueError("outcomes and token_ids must have the same length when tokens exist")
        return self

    def token_for(self, outcome: str) -> str | None:
        return next(
            (
                token
                for name, token in zip(self.outcomes, self.token_ids, strict=False)
                if name.casefold() == outcome.casefold()
            ),
            None,
        )


class PriceLevel(StrictModel):
    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)


class OrderBook(StrictModel):
    market_id: str
    token_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    available_at: datetime | None = None
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)

    @field_validator("timestamp", "available_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    def best_bid_ask(self) -> "BestBidAsk":
        bid = max(self.bids, key=lambda level: level.price, default=None)
        ask = min(self.asks, key=lambda level: level.price, default=None)
        best_bid = bid.price if bid else None
        best_ask = ask.price if ask else None
        mid = None if best_bid is None or best_ask is None else (best_bid + best_ask) / 2
        spread = None if best_bid is None or best_ask is None else max(0.0, best_ask - best_bid)
        return BestBidAsk(
            market_id=self.market_id,
            token_id=self.token_id,
            timestamp=self.timestamp,
            available_at=self.available_at,
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
    available_at: datetime | None = None
    best_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    best_bid_size: float | None = Field(default=None, ge=0.0)
    best_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    best_ask_size: float | None = Field(default=None, ge=0.0)
    mid: float | None = Field(default=None, ge=0.0, le=1.0)
    spread: float | None = Field(default=None, ge=0.0)

    @field_validator("timestamp", "available_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class ExternalAnchor(StrictModel):
    source_id: str
    event_id: str
    source: str
    category: MarketCategory
    outcome: str
    decimal_odds: float | None = Field(default=None, gt=1.0)
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    market_id: str | None = None
    available_at: datetime | None = None

    @field_validator("timestamp", "available_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class FairValue(StrictModel):
    event_id: str
    market_id: str
    outcome: str
    fair_probability: float = Field(ge=0.0, le=1.0)
    source: str
    timestamp: datetime = Field(default_factory=utc_now)
    uncertainty: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    computed_at: datetime | None = None
    available_at: datetime | None = None

    @field_validator("timestamp", "computed_at", "available_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


SignalAction = Literal[
    "BUY_YES", "SELL_YES", "QUOTE_BID", "QUOTE_ASK", "HOLD", "AVOID", "RELATIVE_VALUE"
]


class Signal(StrictModel):
    signal_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    market_id: str
    event_id: str
    risk_event_id: str | None = Field(default=None, min_length=1)
    outcome: str
    category: MarketCategory
    fair_probability: float = Field(ge=0.0, le=1.0)
    prediction_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    prediction_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    raw_edge: float
    cost_drag: float
    adjusted_edge: float
    buy_edge_raw: float | None = None
    sell_edge_raw: float | None = None
    buy_edge_adjusted: float | None = None
    sell_edge_adjusted: float | None = None
    best_side: Literal["BUY_YES", "SELL_YES", "NONE"] = "NONE"
    best_adjusted_edge: float | None = None
    edge_passes_threshold: bool = False
    available_size: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    action: SignalAction
    signal_action: SignalAction = "HOLD"
    reason: str
    fair_value_source: str
    token_id: str | None = None
    match_decision: Literal["MATCH", "REVIEW", "REJECT"] = "REVIEW"
    settlement_verified: bool = False
    fair_value_available_at: datetime | None = None
    orderbook_available_at: datetime | None = None
    market_status: str = "unknown"
    resolution_source: str | None = None
    market_end_time: datetime | None = None
    uncertainty: float = Field(default=0.0, ge=0, le=1)
    book_bid_size: float | None = Field(default=None, ge=0)
    book_ask_size: float | None = Field(default=None, ge=0)
    fair_value_provider: str | None = None
    fair_value_timestamp: datetime | None = None
    orderbook_timestamp: datetime | None = None
    data_age_seconds: float | None = Field(default=None, ge=0.0)
    freshness_status: Literal["fresh", "stale", "future_data", "unknown"] = "unknown"
    match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_approved: bool | None = None
    risk_decision: Literal["APPROVED", "REJECTED", "NOT_EVALUATED"] = "NOT_EVALUATED"
    risk_reasons: list[str] | str = ""
    final_decision: Literal[
        "PAPER_CANDIDATE",
        "WATCHLIST_ONLY",
        "REJECTED_BY_RISK",
        "NO_EDGE",
        "RELATIVE_VALUE_MULTI_LEG",
        "THEORETICAL_ONLY",
        "DATA_INVALID",
    ] = "WATCHLIST_ONLY"

    @field_validator(
        "timestamp",
        "fair_value_timestamp",
        "orderbook_timestamp",
        "market_end_time",
        "fair_value_available_at",
        "orderbook_available_at",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value else None


TradeSide = Literal["BUY_YES", "SELL_YES", "SKIP"]


class TradeDecision(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    signal_id: str
    market_id: str
    event_id: str
    risk_event_id: str | None = Field(default=None, min_length=1)
    outcome: str
    side: TradeSide
    price: float | None = Field(default=None, ge=0, le=1)
    size: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    slippage: float = Field(default=0.0, ge=0)
    token_id: str | None = None
    category: MarketCategory = "unknown"
    cash_change: float = 0.0
    realized_pnl: float = 0.0
    net_entry_price: float | None = None
    fee_bps_at_entry: float | None = Field(default=None, ge=0)
    slippage_bps_at_entry: float | None = Field(default=None, ge=0)
    paper_or_live: Literal["paper", "live"] = "paper"
    approved: bool
    reason: str
    fair_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    prediction_bid: float | None = Field(default=None, ge=0.0, le=1.0)
    prediction_ask: float | None = Field(default=None, ge=0.0, le=1.0)
    adjusted_edge: float | None = None
    available_size: float | None = Field(default=None, ge=0.0)
    execution_price_source: Literal["ask", "bid", "none"] = "none"
    clv_5m: float | None = None
    clv_30m: float | None = None
    clv_1h: float | None = None
    clv_pre_event: float | None = None
    fair_value_source: str | None = None
    fair_value_provider: str | None = None
    fair_value_timestamp: datetime | None = None
    orderbook_timestamp: datetime | None = None
    data_age_seconds: float | None = Field(default=None, ge=0.0)
    match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_clv_5m: float | None = None
    expected_clv_30m: float | None = None
    expected_clv_1h: float | None = None
    expected_clv_pre_event: float | None = None

    @field_validator("timestamp", "fair_value_timestamp", "orderbook_timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value else None


class RiskDecision(StrictModel):
    approved: bool
    reasons: list[str]
