"""Executable taker edge calculation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from edgelab.config.settings import Settings
from edgelab.data.schemas import BestBidAsk, FairValue, PredictionMarket, Signal, SignalAction
from edgelab.fair_value.uncertainty import bps
from edgelab.matching.market_classifier import is_fixture_winner_contract

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EdgeCosts:
    slippage: float
    fees: float
    uncertainty: float
    resolution_risk: float
    safety_margin: float

    @property
    def total(self) -> float:
        return (
            self.slippage + self.fees + self.uncertainty + self.resolution_risk + self.safety_margin
        )


def costs_from_settings(settings: Settings, fair_value: FairValue) -> EdgeCosts:
    return EdgeCosts(
        slippage=bps(settings.default_slippage_bps),
        fees=bps(settings.fee_bps),
        uncertainty=max(fair_value.uncertainty, bps(settings.model_uncertainty_bps)),
        resolution_risk=bps(settings.resolution_risk_bps),
        safety_margin=bps(settings.safety_margin_bps),
    )


def compute_edge_signal(
    market: PredictionMarket,
    fair_value: FairValue,
    bba: BestBidAsk,
    settings: Settings,
    as_of: datetime | None = None,
) -> Signal:
    as_of = as_of or datetime.now(UTC)
    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    costs = costs_from_settings(settings, fair_value)
    expected_token = market.token_for(fair_value.outcome)
    buy_raw = fair_value.fair_probability - bba.best_ask if bba.best_ask is not None else -1.0
    sell_raw = bba.best_bid - fair_value.fair_probability if bba.best_bid is not None else -1.0
    buy_cost = costs.total - costs.fees + (bba.best_ask or 0) * costs.fees
    sell_cost = costs.total - costs.fees + (bba.best_bid or 0) * costs.fees
    side: Literal["BUY_YES", "SELL_YES"]
    if buy_raw - buy_cost >= sell_raw - sell_cost:
        side = "BUY_YES"
        raw_edge = buy_raw
        available_size = float(bba.best_ask_size or 0.0)
    else:
        side = "SELL_YES"
        raw_edge = sell_raw
        available_size = float(bba.best_bid_size or 0.0)
    buy_edge_adjusted = buy_raw - buy_cost if bba.best_ask is not None else None
    sell_edge_adjusted = sell_raw - sell_cost if bba.best_bid is not None else None
    cost_drag = buy_cost if side == "BUY_YES" else sell_cost
    adjusted = raw_edge - cost_drag
    best_side: Literal["BUY_YES", "SELL_YES", "NONE"] = side if adjusted > 0 else "NONE"
    best_adjusted_edge = adjusted if adjusted != float("-inf") else None
    edge_passes_threshold = (
        best_adjusted_edge is not None and best_adjusted_edge >= settings.min_adjusted_edge
    )
    reasons: list[str] = []
    invalid_contract = (
        expected_token is None
        or bba.token_id != expected_token
        or bba.market_id != market.market_id
        or fair_value.event_id != market.event_id
    )
    if invalid_contract:
        reasons.append("contract/token identity mismatch")
    unsupported_payoff = market.category in {"sports_1x2", "sports_moneyline"} and not (
        is_fixture_winner_contract(market.title, market.description, market.outcomes)
    )
    if unsupported_payoff:
        reasons.append("unsupported fixture payoff")
    if fair_value.metadata.get("model_validated") is False:
        reasons.append("volatility model uncalibrated; watchlist only")
    unverified_settlement = (
        market.category.startswith("sports")
        and market.raw_metadata.get("synthetic_data") is not True
        and fair_value.metadata.get("settlement_verified") is not True
    )
    if unverified_settlement:
        reasons.append("settlement equivalence unverified; watchlist only")
    if bba.best_bid is not None and bba.best_ask is not None and bba.best_bid > bba.best_ask:
        reasons.append("crossed book")
    if market.end_time and market.end_time <= as_of:
        reasons.append("market expired")
    if market.category.startswith("sports") and market.start_time and market.start_time <= as_of:
        reasons.append("in-play sports execution is not validated")
    source_timestamps = [
        fair_value.timestamp,
        fair_value.available_at or fair_value.timestamp,
        bba.timestamp,
        bba.available_at or bba.timestamp,
    ]
    has_future_data = any(timestamp > as_of for timestamp in source_timestamps)
    oldest_timestamp = min(source_timestamps)
    data_age_seconds = max(0.0, (as_of - oldest_timestamp).total_seconds())
    fair_age = (as_of - min(source_timestamps[:2])).total_seconds()
    book_age = (as_of - min(source_timestamps[2:])).total_seconds()
    stale_data = (
        fair_age > min(settings.max_data_age_seconds, settings.max_odds_age_seconds)
        or book_age > min(settings.max_data_age_seconds, settings.max_orderbook_age_seconds)
        or abs((fair_value.timestamp - bba.timestamp).total_seconds())
        > settings.max_cross_source_skew_seconds
    )
    if has_future_data:
        reasons.append("future data timestamp detected")
    elif stale_data:
        reasons.append(
            "stale fair-value or order-book data, or excessive cross-source timestamp skew"
        )
    if market.status.lower() != "active":
        reasons.append(f"market status={market.status}")
    if bba.best_bid is None or bba.best_ask is None:
        reasons.append("missing executable bid/ask")
    if bba.spread is not None and bba.spread > settings.max_spread:
        reasons.append("spread too wide")
    if available_size < settings.min_liquidity:
        reasons.append("insufficient liquidity")
    if adjusted < settings.min_adjusted_edge:
        reasons.append("adjusted edge below threshold")
    if market.resolution_source in {None, ""}:
        reasons.append("unclear resolution source")
    action: SignalAction = side if edge_passes_threshold and not reasons else "HOLD"
    if (
        fair_value.metadata.get("model_validated") is False
        or unsupported_payoff
        or unverified_settlement
        or invalid_contract
        or has_future_data
        or stale_data
        or bba.best_bid is None
        or bba.best_ask is None
    ):
        action = "AVOID"
    freshness_status: Literal["fresh", "stale", "future_data", "unknown"] = (
        "future_data" if has_future_data else "stale" if stale_data else "fresh"
    )
    if reasons:
        LOGGER.info(
            "signal_rejected market_id=%s action=%s adjusted_edge=%.6f reasons=%s",
            market.market_id,
            action,
            adjusted,
            "|".join(reasons),
        )
    risk_decision: Literal["REJECTED", "NOT_EVALUATED"] = "REJECTED" if reasons else "NOT_EVALUATED"
    final_decision: Literal["NO_EDGE", "DATA_INVALID", "REJECTED_BY_RISK", "WATCHLIST_ONLY"]
    if not edge_passes_threshold and not reasons:
        final_decision = "NO_EDGE"
    elif has_future_data or stale_data or bba.best_bid is None or bba.best_ask is None:
        final_decision = "DATA_INVALID"
    elif reasons:
        final_decision = "REJECTED_BY_RISK"
    else:
        final_decision = "WATCHLIST_ONLY"
    return Signal(
        signal_id=f"{market.market_id}:{fair_value.outcome}:{as_of.isoformat()}",
        timestamp=as_of,
        market_id=market.market_id,
        event_id=market.event_id,
        risk_event_id=market.risk_event_id,
        outcome=fair_value.outcome,
        category=market.category,
        fair_probability=fair_value.fair_probability,
        prediction_bid=bba.best_bid,
        prediction_ask=bba.best_ask,
        raw_edge=raw_edge,
        cost_drag=cost_drag,
        adjusted_edge=adjusted,
        buy_edge_raw=None if buy_raw == float("-inf") else buy_raw,
        sell_edge_raw=None if sell_raw == float("-inf") else sell_raw,
        buy_edge_adjusted=buy_edge_adjusted,
        sell_edge_adjusted=sell_edge_adjusted,
        best_side=best_side,
        best_adjusted_edge=best_adjusted_edge,
        edge_passes_threshold=edge_passes_threshold,
        available_size=available_size,
        confidence=max(0.0, min(1.0, adjusted / max(settings.min_adjusted_edge * 4, 1e-9))),
        action=action,
        signal_action=action,
        reason="; ".join(reasons) if reasons else f"{side} passes edge and liquidity checks",
        fair_value_source=fair_value.source,
        token_id=expected_token,
        match_decision=fair_value.metadata.get("match_decision", "REVIEW"),
        settlement_verified=fair_value.metadata.get("settlement_verified", False),
        fair_value_available_at=fair_value.available_at,
        orderbook_available_at=bba.available_at,
        market_status=market.status,
        resolution_source=market.resolution_source,
        market_end_time=market.end_time,
        uncertainty=fair_value.uncertainty,
        book_bid_size=bba.best_bid_size,
        book_ask_size=bba.best_ask_size,
        fair_value_provider=fair_value.metadata.get("provider"),
        fair_value_timestamp=fair_value.timestamp,
        orderbook_timestamp=bba.timestamp,
        data_age_seconds=data_age_seconds,
        freshness_status=freshness_status,
        risk_decision=risk_decision,
        risk_reasons=reasons,
        final_decision=final_decision,
    )


def generate_signals(
    markets: list[PredictionMarket],
    fair_values: list[FairValue],
    bbas: list[BestBidAsk],
    settings: Settings,
    as_of: datetime | None = None,
) -> list[Signal]:
    markets_by_id = {market.market_id: market for market in markets}
    now = as_of or datetime.now(UTC)
    signals: list[Signal] = []
    for fair in fair_values:
        market = markets_by_id.get(fair.market_id)
        token = market.token_for(fair.outcome) if market else None
        eligible = [
            b
            for b in bbas
            if b.market_id == fair.market_id
            and b.token_id == token
            and b.timestamp <= now
            and (b.available_at or b.timestamp) <= now
        ]
        bba = max(
            eligible, key=lambda b: (b.timestamp, b.available_at or b.timestamp), default=None
        )
        if bba and any(
            (b.timestamp, b.available_at) == (bba.timestamp, bba.available_at) and b != bba
            for b in eligible
        ):
            LOGGER.warning(
                "conflicting_same_vintage_books market=%s token=%s", fair.market_id, token
            )
            bba = None
        if market and bba is None:
            bba = BestBidAsk(market_id=market.market_id, token_id=token or "missing", timestamp=now)
        if market and bba:
            signals.append(compute_edge_signal(market, fair, bba, settings, as_of=as_of))
        else:
            LOGGER.warning(
                "missing_market_or_book fair_market_id=%s has_market=%s has_bba=%s",
                fair.market_id,
                bool(market),
                bool(bba),
            )
    return sorted(signals, key=lambda signal: signal.adjusted_edge, reverse=True)
