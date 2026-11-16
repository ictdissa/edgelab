"""Paper trader using executable bid/ask, never midpoint fills."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, Signal, TradeDecision
from edgelab.execution.risk_manager import RiskManager
from edgelab.execution.portfolio import PaperPortfolio
from edgelab.research.ledger_io import ledger_lock

LOGGER = logging.getLogger(__name__)


class PaperTrader:
    def __init__(
        self,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
        portfolio_path: Path | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.risk = risk or RiskManager(self.settings)
        self.portfolio = PaperPortfolio(self.settings.paper_starting_cash, portfolio_path)

    def execute(
        self,
        signals: Iterable[Signal],
        markets: dict[str, PredictionMarket],
        as_of: datetime | None = None,
    ) -> list[TradeDecision]:
        decisions: list[TradeDecision] = []
        for signal in signals:
            self.portfolio.connection.execute("BEGIN IMMEDIATE")
            try:
                decision = self._execute_one(signal, markets, as_of)
                if not self.portfolio.seen(signal.signal_id):
                    self.portfolio.record(decision)
                self.portfolio.connection.execute(
                    "INSERT INTO attempts(payload) VALUES (?)", (decision.model_dump_json(),)
                )
                self.portfolio.connection.execute("COMMIT")
            except Exception:
                self.portfolio.connection.execute("ROLLBACK")
                raise
            decisions.append(decision)
        return decisions

    def _execute_one(
        self, signal: Signal, markets: dict[str, PredictionMarket], as_of: datetime | None
    ) -> TradeDecision:
        now = as_of or datetime.now(UTC)
        self.portfolio.begin_day(now)
        if self.portfolio.seen(signal.signal_id):
            return _skip(signal, "duplicate signal already processed", now)
        if signal.token_id and self.portfolio.is_settled(signal.token_id):
            return _skip(signal, "settled token is terminal; further trading prohibited", now)
        previous_quote = self.portfolio.quote_time(signal.token_id or "")
        if (
            previous_quote
            and signal.orderbook_timestamp
            and signal.orderbook_timestamp < previous_quote
        ):
            return _skip(signal, "out-of-order book older than latest portfolio mark", now)
        market = markets.get(signal.market_id)
        if signal.action in {"BUY_YES", "SELL_YES"} and market is not None:
            if signal.risk_event_id != market.risk_event_id:
                return _skip(signal, "canonical risk event identity mismatch", now)
            try:
                # Bind the whole verified universe before checking new risk, so legacy
                # holdings in another wrapper cannot escape this fixture's exposure.
                for known in markets.values():
                    if known.risk_event_id:
                        self.portfolio.bind_risk_event(known.event_id, known.risk_event_id)
            except ValueError as exc:
                return _skip(signal, str(exc), now)
            if not market.risk_event_id and self.portfolio.risk_key(market.event_id) is not None:
                return _skip(signal, "established canonical fixture risk identity missing", now)
        self.risk.positions_by_market, self.risk.positions_by_event, self.risk.total_exposure = (
            self.portfolio.exposures()
        )
        self.risk.inventory_by_token[signal.token_id or ""] = self.portfolio.inventory(
            signal.token_id or ""
        )
        if (
            signal.token_id
            and signal.prediction_bid is not None
            and signal.prediction_ask is not None
            and signal.prediction_bid <= signal.prediction_ask
            and signal.orderbook_timestamp
            and 0
            <= (now - signal.orderbook_timestamp).total_seconds()
            <= self.settings.max_data_age_seconds
            and markets.get(signal.market_id)
            and markets[signal.market_id].token_for(signal.outcome) == signal.token_id
        ):
            self.portfolio.mark(
                signal.token_id, signal.prediction_bid, signal.orderbook_timestamp, now
            )
        self.risk.daily_pnl = self.portfolio.daily_pnl(now)
        self.risk.daily_loss_latched = self.portfolio.loss_stopped(
            now, self.settings.max_daily_loss
        )
        if signal.action == "BUY_YES" and self.portfolio.stale_positions(
            now, min(self.settings.max_data_age_seconds, self.settings.max_orderbook_age_seconds)
        ):
            return _skip(signal, "open portfolio contains stale marks; new exposure blocked", now)
        market = markets.get(signal.market_id)
        if market is None:
            LOGGER.warning("paper_skip missing_market market_id=%s", signal.market_id)
            return _skip(signal, "missing market metadata", now)
        if signal.risk_approved is False:
            return _skip(signal, f"risk_approved=false: {signal.risk_reasons}", now)
        proposed = min(self.settings.max_trade_size, signal.available_size)
        if signal.action == "SELL_YES" and self.portfolio.inventory(signal.token_id or "") > 0:
            proposed = min(proposed, self.portfolio.inventory(signal.token_id or ""))
        if signal.token_id and signal.orderbook_timestamp:
            remaining = signal.available_size - self.portfolio.consumed(
                signal.token_id, signal.orderbook_timestamp, signal.action
            )
            proposed = min(proposed, max(0.0, remaining))
            if proposed <= 0:
                return _skip(signal, "displayed quote liquidity already consumed", now)
        data_timestamp = (
            signal.orderbook_timestamp or signal.fair_value_timestamp or signal.timestamp
        )
        risk = self.risk.check(signal, market, proposed, data_timestamp, live=False, as_of=as_of)
        if not risk.approved:
            return _skip(signal, "; ".join(risk.reasons), now)
        price = signal.prediction_ask if signal.action == "BUY_YES" else signal.prediction_bid
        if price is None:
            return _skip(signal, "missing executable price", now)
        if (
            signal.action == "SELL_YES"
            and self.portfolio.inventory(signal.token_id or "") < proposed
        ):
            return _skip(signal, "insufficient inventory: naked SELL_YES is not supported", now)
        fee = proposed * price * self.settings.fee_bps / 10_000.0
        slippage = proposed * self.settings.default_slippage_bps / 10_000.0
        if signal.action == "BUY_YES" and proposed * price + fee + slippage > self.portfolio.cash:
            return _skip(signal, "insufficient paper cash", now)
        decision = TradeDecision(
            timestamp=now,
            signal_id=signal.signal_id,
            market_id=signal.market_id,
            event_id=signal.event_id,
            risk_event_id=market.risk_event_id,
            outcome=signal.outcome,
            side="BUY_YES" if signal.action == "BUY_YES" else "SELL_YES",
            price=price,
            size=proposed,
            fees=fee,
            slippage=slippage,
            fee_bps_at_entry=self.settings.fee_bps,
            slippage_bps_at_entry=self.settings.default_slippage_bps,
            token_id=signal.token_id,
            category=signal.category,
            approved=True,
            reason="paper fill at ask" if signal.action == "BUY_YES" else "paper fill at bid",
            fair_probability=signal.fair_probability,
            prediction_bid=signal.prediction_bid,
            prediction_ask=signal.prediction_ask,
            adjusted_edge=signal.adjusted_edge,
            available_size=signal.available_size,
            execution_price_source="ask" if signal.action == "BUY_YES" else "bid",
            fair_value_source=signal.fair_value_source,
            fair_value_provider=signal.fair_value_provider,
            fair_value_timestamp=signal.fair_value_timestamp,
            orderbook_timestamp=signal.orderbook_timestamp,
            data_age_seconds=signal.data_age_seconds,
            match_score=signal.match_score,
        )
        self.portfolio.fill(decision, signal.prediction_bid or 0.0)
        self.portfolio.loss_stopped(now, self.settings.max_daily_loss)
        LOGGER.info(
            "paper_fill signal_id=%s side=%s price=%.4f size=%.2f",
            signal.signal_id,
            decision.side,
            price,
            proposed,
        )
        return decision


def _skip(signal: Signal, reason: str, as_of: datetime | None = None) -> TradeDecision:
    return TradeDecision(
        timestamp=as_of or signal.timestamp,
        signal_id=signal.signal_id,
        market_id=signal.market_id,
        event_id=signal.event_id,
        risk_event_id=signal.risk_event_id,
        outcome=signal.outcome,
        token_id=signal.token_id,
        category=signal.category,
        side="SKIP",
        price=None,
        size=0.0,
        approved=False,
        reason=reason,
        fair_probability=signal.fair_probability,
        prediction_bid=signal.prediction_bid,
        prediction_ask=signal.prediction_ask,
        adjusted_edge=signal.adjusted_edge,
        available_size=signal.available_size,
        execution_price_source="none",
        fair_value_source=signal.fair_value_source,
        fair_value_provider=signal.fair_value_provider,
        fair_value_timestamp=signal.fair_value_timestamp,
        orderbook_timestamp=signal.orderbook_timestamp,
        data_age_seconds=signal.data_age_seconds,
        match_score=signal.match_score,
    )


def write_ledger(path: Path, rows: list[TradeDecision], append: bool = True) -> None:
    with ledger_lock(path):
        _write_ledger_locked(path, rows, append)


def _write_ledger_locked(path: Path, rows: list[TradeDecision], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(TradeDecision.model_fields)
    existing: list[dict[str, str]] = []
    if append and path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != columns:
                raise ValueError("Ledger schema mismatch; use a new path or explicit migration")
            existing = list(reader)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".csv.tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(existing)
        seen = {row["signal_id"] for row in existing}
        for row in rows:
            if row.signal_id not in seen:
                writer.writerow(row.model_dump(mode="json"))
                seen.add(row.signal_id)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
