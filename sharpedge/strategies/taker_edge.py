"""Executable taker edge calculation for YES/outcome contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, FairValue, Market, Signal


@dataclass(frozen=True)
class EdgeCosts:
    slippage: float
    uncertainty: float
    resolution_risk: float
    safety_margin: float

    @property
    def total(self) -> float:
        return self.slippage + self.uncertainty + self.resolution_risk + self.safety_margin


def bps_to_probability(bps: float) -> float:
    return bps / 10_000.0


def costs_from_settings(settings: Settings, fair_value: FairValue) -> EdgeCosts:
    return EdgeCosts(
        slippage=bps_to_probability(settings.default_slippage_bps),
        uncertainty=max(fair_value.uncertainty, bps_to_probability(settings.model_uncertainty_bps)),
        resolution_risk=bps_to_probability(settings.resolution_risk_bps),
        safety_margin=bps_to_probability(settings.safety_margin_bps),
    )


def compute_edge_signal(
    market: Market,
    outcome: str,
    fair_value: FairValue,
    bba: BestBidAsk,
    settings: Settings,
    as_of: datetime | None = None,
) -> Signal:
    """Return a BUY/SELL/HOLD/AVOID signal from executable bid/ask."""

    as_of = as_of or datetime.now(UTC)
    if bba.best_ask is None and bba.best_bid is None:
        return _avoid(market, outcome, fair_value, "missing executable bid/ask", as_of)

    costs = costs_from_settings(settings, fair_value)
    buy_raw = (
        fair_value.fair_probability - bba.best_ask
        if bba.best_ask is not None
        else float("-inf")
    )
    sell_raw = (
        bba.best_bid - fair_value.fair_probability
        if bba.best_bid is not None
        else float("-inf")
    )
    if buy_raw >= sell_raw:
        action = "BUY_YES"
        raw_edge = buy_raw
        available_size = float(bba.best_ask_size or 0.0)
    else:
        action = "SELL_YES"
        raw_edge = sell_raw
        available_size = float(bba.best_bid_size or 0.0)

    adjusted_edge = raw_edge - costs.total
    reasons: list[str] = []
    tradable = market.status.lower() in {"active", "open", "tradable"}
    if not tradable:
        reasons.append(f"market status is {market.status}")
    if available_size < settings.min_liquidity:
        reasons.append("insufficient executable liquidity")
    if adjusted_edge < settings.min_adjusted_edge:
        reasons.append("adjusted edge below threshold")
    if (
        market.start_time
        and market.start_time > as_of
        and (market.start_time - as_of).total_seconds() < settings.min_seconds_to_start
    ):
        reasons.append("event too close to start or already started")

    final_action = action if not reasons else "HOLD"
    confidence = max(0.0, min(1.0, adjusted_edge / max(settings.min_adjusted_edge * 4, 1e-9)))
    return Signal(
        timestamp=as_of,
        market_id=market.market_id,
        event_id=market.event_id,
        outcome=outcome,
        signal_type="external_fair_value_stale_price",
        fair_probability=fair_value.fair_probability,
        polymarket_bid=bba.best_bid,
        polymarket_ask=bba.best_ask,
        raw_edge=raw_edge,
        adjusted_edge=adjusted_edge,
        available_size=available_size,
        confidence=confidence,
        action=final_action,
        reason="; ".join(reasons) if reasons else f"{action} passes edge, liquidity, and timing checks",
    )


def _avoid(
    market: Market, outcome: str, fair_value: FairValue, reason: str, as_of: datetime
) -> Signal:
    return Signal(
        timestamp=as_of,
        market_id=market.market_id,
        event_id=market.event_id,
        outcome=outcome,
        signal_type="external_fair_value_stale_price",
        fair_probability=fair_value.fair_probability,
        polymarket_bid=None,
        polymarket_ask=None,
        raw_edge=0.0,
        adjusted_edge=-1.0,
        available_size=0.0,
        confidence=0.0,
        action="AVOID",
        reason=reason,
    )


def generate_edge_signals(
    markets: list[Market],
    fair_values: list[FairValue],
    best_bid_asks: list[BestBidAsk],
    settings: Settings,
    as_of: datetime | None = None,
) -> list[Signal]:
    as_of = as_of or datetime.now(UTC)
    markets_by_id = {market.market_id: market for market in markets}
    fair_by_key = {(value.market_id, value.outcome): value for value in fair_values}
    token_bba_by_market_outcome: dict[tuple[str, str], BestBidAsk] = {}
    for market in markets:
        for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
            for bba in best_bid_asks:
                if bba.market_id == market.market_id and bba.token_id == token_id:
                    token_bba_by_market_outcome[(market.market_id, outcome)] = bba
                    break

    signals: list[Signal] = []
    for key, fair_value in fair_by_key.items():
        market = markets_by_id.get(fair_value.market_id)
        bba = token_bba_by_market_outcome.get(key)
        if market is None or bba is None:
            continue
        signals.append(
            compute_edge_signal(
                market=market,
                outcome=fair_value.outcome,
                fair_value=fair_value,
                bba=bba,
                settings=settings,
                as_of=as_of,
            )
        )
    return sorted(signals, key=lambda signal: signal.adjusted_edge, reverse=True)
