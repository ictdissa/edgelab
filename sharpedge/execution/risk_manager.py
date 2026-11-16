"""Strict pre-trade risk checks for paper and live trading."""

from __future__ import annotations

from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import Market, RiskDecision, RiskState, Signal


class RiskManager:
    """Fail-closed risk engine shared by paper and live execution paths."""

    def __init__(self, settings: Settings, state: RiskState | None = None) -> None:
        self.settings = settings
        self.state = state or RiskState()

    def check_signal(
        self,
        signal: Signal,
        market: Market,
        proposed_size: float,
        orderbook_timestamp: datetime,
        odds_timestamp: datetime,
        live: bool = False,
        resolution_rules_clear: bool = True,
        as_of: datetime | None = None,
    ) -> RiskDecision:
        as_of = as_of or datetime.now(UTC)
        reasons: list[str] = []
        if live and not self.settings.enable_live_trading:
            reasons.append("live trading disabled")
        if live and not self.settings.required_live_credentials_present:
            reasons.append("live credentials missing")
        if signal.action not in {"BUY_YES", "SELL_YES"}:
            reasons.append(f"signal action {signal.action} is not executable")
        if signal.adjusted_edge < self.settings.min_adjusted_edge:
            reasons.append("adjusted edge below minimum")
        if signal.available_size < self.settings.min_liquidity:
            reasons.append("liquidity below minimum")
        if proposed_size <= 0:
            reasons.append("proposed size must be positive")
        if proposed_size > self.settings.max_trade_size:
            reasons.append("proposed size exceeds max trade size")
        if not resolution_rules_clear:
            reasons.append("market resolution rules unclear")
        if market.status.lower() not in {"active", "open", "tradable"}:
            reasons.append(f"market status {market.status} is not tradable")
        if (
            market.start_time
            and market.start_time > as_of
            and (market.start_time - as_of).total_seconds() < self.settings.min_seconds_to_start
        ):
            reasons.append("event too close to start or already started")
        if (as_of - orderbook_timestamp).total_seconds() > self.settings.max_orderbook_age_seconds:
            reasons.append("order book is stale")
        if (as_of - odds_timestamp).total_seconds() > self.settings.max_odds_age_seconds:
            reasons.append("external odds are stale")
        if self.state.daily_pnl <= -abs(self.settings.max_daily_loss):
            reasons.append("max daily loss reached")

        signed_delta = proposed_size if signal.action == "BUY_YES" else -proposed_size
        current_market_position = self.state.positions_by_market.get(market.market_id, 0.0)
        current_event_position = self.state.positions_by_event.get(market.event_id, 0.0)
        market_after = abs(current_market_position + signed_delta)
        event_after = abs(current_event_position + signed_delta)
        if market_after > self.settings.max_position_per_market:
            reasons.append("max position per market exceeded")
        if event_after > self.settings.max_position_per_event:
            reasons.append("max position per event exceeded")
        if (
            self.state.total_unresolved_exposure + proposed_size
            > self.settings.max_total_unresolved_exposure
        ):
            reasons.append("max total unresolved exposure exceeded")

        max_approved_size = min(
            proposed_size,
            self.settings.max_trade_size,
            signal.available_size,
            max(0.0, self.settings.max_position_per_market - abs(current_market_position)),
            max(0.0, self.settings.max_position_per_event - abs(current_event_position)),
            max(0.0, self.settings.max_total_unresolved_exposure - self.state.total_unresolved_exposure),
        )
        return RiskDecision(
            approved=not reasons,
            reasons=reasons,
            max_approved_size=max_approved_size if not reasons else 0.0,
        )

    def apply_trade(self, market: Market, action: str, size: float, cash_pnl: float = 0.0) -> None:
        signed = size if action == "BUY_YES" else -size
        self.state.positions_by_market[market.market_id] = (
            self.state.positions_by_market.get(market.market_id, 0.0) + signed
        )
        self.state.positions_by_event[market.event_id] = (
            self.state.positions_by_event.get(market.event_id, 0.0) + signed
        )
        self.state.total_unresolved_exposure += abs(size)
        self.state.daily_pnl += cash_pnl
