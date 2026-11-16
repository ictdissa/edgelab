"""Pre-trade checks for funding, exposure, contract identity and data freshness."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, RiskDecision, Signal
from edgelab.matching.market_classifier import is_fixture_winner_contract
from edgelab.matching.settlement import settlement_equivalent

LOGGER = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.positions_by_market: dict[str, float] = {}
        self.positions_by_event: dict[str, float] = {}
        self.total_exposure = 0.0
        self.daily_pnl = 0.0
        self.daily_loss_latched = False
        self.inventory_by_token: dict[str, float] = {}

    def check(
        self,
        signal: Signal,
        market: PredictionMarket,
        proposed_size: float,
        data_timestamp: datetime,
        live: bool = False,
        match_decision: str | None = None,
        as_of: datetime | None = None,
    ) -> RiskDecision:
        as_of = as_of or datetime.now(UTC)
        as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        reasons: list[str] = []
        if live or self.settings.enable_live_trading:
            reasons.append("live trading disabled by default")
        match_decision = match_decision or signal.match_decision
        if match_decision != "MATCH":
            reasons.append(f"event mapping is {match_decision}")
        if signal.action not in {"BUY_YES", "SELL_YES"}:
            reasons.append(f"signal action is {signal.action}")
        if signal.adjusted_edge < self.settings.min_adjusted_edge:
            reasons.append("adjusted edge below threshold")
        price = signal.prediction_ask if signal.action == "BUY_YES" else signal.prediction_bid
        if price is not None and signal.action in {"BUY_YES", "SELL_YES"}:
            raw = (
                signal.fair_probability - price
                if signal.action == "BUY_YES"
                else price - signal.fair_probability
            )
            drag = (
                self.settings.default_slippage_bps
                + self.settings.resolution_risk_bps
                + self.settings.safety_margin_bps
            ) / 10000
            drag += (
                max(signal.uncertainty, self.settings.model_uncertainty_bps / 10000)
                + price * self.settings.fee_bps / 10000
            )
            if raw - drag < self.settings.min_adjusted_edge:
                reasons.append("recomputed net edge below threshold")
        if market.category in {"unknown", "politics_election"}:
            reasons.append("unsupported execution category")
        if market.category in {"sports_1x2", "sports_moneyline"} and not (
            is_fixture_winner_contract(market.title, market.description, market.outcomes)
        ):
            reasons.append("unsupported fixture payoff")
        if signal.event_id != market.event_id or signal.category != market.category:
            reasons.append("event/category identity mismatch")
        if signal.risk_event_id != market.risk_event_id:
            reasons.append("canonical risk event identity mismatch")
        if (
            market.category.startswith("sports")
            and market.raw_metadata.get("synthetic_data") is not True
        ):
            if not market.risk_event_id:
                reasons.append("canonical fixture risk identity unverified")
            spec = market.raw_metadata.get("settlement_spec")
            if (
                market.category != "sports_1x2"
                or not signal.settlement_verified
                or not settlement_equivalent(spec, spec, as_of)[0]
            ):
                reasons.append("settlement equivalence unverified")
        if signal.available_size < self.settings.min_liquidity:
            reasons.append("insufficient liquidity")
        if proposed_size > self.settings.max_trade_size:
            reasons.append("trade size exceeds max_trade_size")
        if not math.isfinite(proposed_size) or proposed_size <= 0:
            reasons.append("trade size must be finite and positive")
        if proposed_size > signal.available_size:
            reasons.append("trade size exceeds displayed depth")
        if market.status != "active" or (market.end_time and market.end_time <= as_of):
            reasons.append("market inactive or expired")
        if (
            market.category.startswith("sports")
            and market.start_time
            and market.start_time <= as_of
        ):
            reasons.append("in-play sports execution is not validated")
        if signal.token_id != market.token_for(signal.outcome) or not signal.token_id:
            reasons.append("missing or mismatched outcome token")
        if (
            signal.action == "SELL_YES"
            and self.inventory_by_token.get(signal.token_id or "", 0) < proposed_size
        ):
            reasons.append("insufficient inventory; naked sales disabled")
        if signal.prediction_bid is None or signal.prediction_ask is None:
            reasons.append("missing executable bid/ask")
        elif not 0 <= signal.prediction_ask - signal.prediction_bid <= self.settings.max_spread:
            reasons.append("crossed or wide spread")
        delta = proposed_size if signal.action == "BUY_YES" else -proposed_size
        reducing = signal.action == "SELL_YES" and 0 < proposed_size <= self.inventory_by_token.get(
            signal.token_id or "", 0
        )
        if (
            not reducing
            and self.positions_by_market.get(market.market_id, 0.0) + delta
            > self.settings.max_position_per_market
        ):
            reasons.append("max position per market exceeded")
        if (
            not reducing
            and self.positions_by_event.get(market.risk_event_id or market.event_id, 0.0) + delta
            > self.settings.max_position_per_event
        ):
            reasons.append("max position per event exceeded")
        if (
            not reducing
            and self.total_exposure + delta > self.settings.max_total_unresolved_exposure
        ):
            reasons.append("max total unresolved exposure exceeded")
        if not reducing and (
            self.daily_loss_latched or self.daily_pnl <= -abs(self.settings.max_daily_loss)
        ):
            reasons.append("max daily loss reached")
        source_timestamps = [
            timestamp
            for timestamp in (
                data_timestamp,
                signal.fair_value_timestamp,
                signal.orderbook_timestamp,
                signal.fair_value_available_at,
                signal.orderbook_available_at,
            )
            if timestamp is not None
        ]
        if signal.fair_value_timestamp is None or signal.orderbook_timestamp is None:
            reasons.append("missing data freshness timestamps")
        elif any(timestamp > as_of for timestamp in source_timestamps):
            reasons.append("future data timestamp detected")
        else:
            oldest = min(source_timestamps)
            age = (as_of - oldest).total_seconds()
            if age > self.settings.max_data_age_seconds:
                reasons.append("stale data")
        if (
            signal.orderbook_timestamp
            and (as_of - signal.orderbook_timestamp).total_seconds()
            > self.settings.max_orderbook_age_seconds
        ):
            reasons.append("order book exceeds strategy freshness limit")
        if (
            signal.fair_value_timestamp
            and (as_of - signal.fair_value_timestamp).total_seconds()
            > self.settings.max_odds_age_seconds
        ):
            reasons.append("fair anchor exceeds strategy freshness limit")
        if (
            signal.fair_value_timestamp
            and signal.orderbook_timestamp
            and abs((signal.fair_value_timestamp - signal.orderbook_timestamp).total_seconds())
            > self.settings.max_cross_source_skew_seconds
        ):
            reasons.append("cross-source timestamp skew exceeds limit")
        if signal.freshness_status in {"stale", "future_data"}:
            reasons.append(f"signal freshness is {signal.freshness_status}")
        if not market.resolution_source:
            reasons.append("unclear resolution rules")
        decision = RiskDecision(approved=not reasons, reasons=reasons)
        if not decision.approved:
            LOGGER.info(
                "risk_rejected market_id=%s signal_id=%s reasons=%s",
                market.market_id,
                signal.signal_id,
                "|".join(reasons),
            )
        return decision

    def reserve(self, market: PredictionMarket, size: float) -> None:
        self.positions_by_market[market.market_id] = (
            self.positions_by_market.get(market.market_id, 0.0) + size
        )
        event = market.risk_event_id or market.event_id
        self.positions_by_event[event] = self.positions_by_event.get(event, 0.0) + size
        self.total_exposure += size
