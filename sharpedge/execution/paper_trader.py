"""Realistic paper trading at executable bid/ask."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, Market, PaperDecision, Signal, Trade
from sharpedge.data.storage import Storage
from sharpedge.execution.risk_manager import RiskManager


class PaperTrader:
    def __init__(
        self,
        settings: Settings,
        risk_manager: RiskManager,
        storage: Storage | None = None,
    ) -> None:
        self.settings = settings
        self.risk_manager = risk_manager
        self.storage = storage
        self.positions: dict[tuple[str, str], float] = defaultdict(float)
        self.cash: float = 0.0
        self.trades: list[Trade] = []
        self.decisions: list[PaperDecision] = []

    def process_signal(
        self,
        signal: Signal,
        market: Market,
        bba: BestBidAsk,
        proposed_size: float | None = None,
        odds_timestamp: datetime | None = None,
        as_of: datetime | None = None,
    ) -> PaperDecision:
        as_of = as_of or datetime.now(UTC)
        proposed_size = proposed_size or min(
            self.settings.max_trade_size, signal.available_size
        )
        odds_timestamp = odds_timestamp or signal.timestamp
        decision = self.risk_manager.check_signal(
            signal=signal,
            market=market,
            proposed_size=proposed_size,
            orderbook_timestamp=bba.timestamp,
            odds_timestamp=odds_timestamp,
            live=False,
            resolution_rules_clear=bool(market.resolution_source),
            as_of=as_of,
        )
        if not decision.approved:
            paper_decision = PaperDecision(
                timestamp=as_of,
                signal=signal,
                executed=False,
                reasons=decision.reasons,
            )
            self._record_decision(paper_decision)
            return paper_decision

        size = min(decision.max_approved_size, signal.available_size)
        if signal.action == "BUY_YES":
            if bba.best_ask is None:
                return self._skip(as_of, signal, "missing ask")
            price = bba.best_ask
            side = "BUY"
        elif signal.action == "SELL_YES":
            if bba.best_bid is None:
                return self._skip(as_of, signal, "missing bid")
            price = bba.best_bid
            side = "SELL"
        else:
            return self._skip(as_of, signal, f"non-executable action {signal.action}")

        slippage = (self.settings.default_slippage_bps / 10_000.0) * size
        fees = 0.0
        trade = Trade(
            timestamp=as_of,
            market_id=signal.market_id,
            event_id=signal.event_id,
            outcome=signal.outcome,
            side=side,
            price=price,
            size=size,
            fees=fees,
            slippage=slippage,
            signal_id=None,
            paper_or_live="paper",
            notes=f"paper fill at executable {'ask' if side == 'BUY' else 'bid'}",
        )
        self._apply_trade(market, trade, signal.action)
        paper_decision = PaperDecision(
            timestamp=as_of,
            signal=signal,
            executed=True,
            trade=trade,
            reasons=[],
        )
        self._record_decision(paper_decision)
        return paper_decision

    def mark_to_market(self, latest_bbas: dict[tuple[str, str], BestBidAsk]) -> float:
        unrealized = 0.0
        for key, position in self.positions.items():
            bba = latest_bbas.get(key)
            if not bba:
                continue
            if position > 0 and bba.best_bid is not None:
                unrealized += position * bba.best_bid
            elif position < 0 and bba.best_ask is not None:
                unrealized += abs(position) * (1.0 - bba.best_ask)
        return self.cash + unrealized

    def realized_pnl(self) -> float:
        return self.cash

    def _apply_trade(self, market: Market, trade: Trade, action: str) -> None:
        key = (trade.market_id, trade.outcome)
        self.positions[key] += trade.signed_size
        self.cash += trade.cash_flow
        self.trades.append(trade)
        self.risk_manager.apply_trade(market, action, trade.size, cash_pnl=trade.cash_flow)
        if self.storage:
            self.storage.insert_trade(trade)

    def _skip(self, as_of: datetime, signal: Signal, reason: str) -> PaperDecision:
        decision = PaperDecision(
            timestamp=as_of,
            signal=signal,
            executed=False,
            reasons=[reason],
        )
        self._record_decision(decision)
        return decision

    def _record_decision(self, decision: PaperDecision) -> None:
        self.decisions.append(decision)
        if self.storage:
            self.storage.insert_decision(
                signal=decision.signal,
                executed=decision.executed,
                reasons=decision.reasons,
                trade=decision.trade,
            )
