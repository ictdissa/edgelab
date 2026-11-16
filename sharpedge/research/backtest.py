"""Point-in-time backtester with explicit no-lookahead guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, ExternalOdds, Market, OrderBook, PaperDecision
from sharpedge.execution.paper_trader import PaperTrader
from sharpedge.execution.risk_manager import RiskManager
from sharpedge.research.fair_value_model import FairValueModel
from sharpedge.strategies.taker_edge import generate_edge_signals


@dataclass
class BacktestResult:
    decisions: list[PaperDecision] = field(default_factory=list)
    final_cash_pnl: float = 0.0
    final_mark_to_market_pnl: float = 0.0
    signals_evaluated: int = 0


def select_point_in_time_inputs(
    as_of: datetime,
    orderbooks: list[OrderBook],
    odds: list[ExternalOdds],
) -> tuple[list[OrderBook], list[ExternalOdds]]:
    """Select only data observable at or before as_of."""

    selected_books = [book for book in orderbooks if book.timestamp <= as_of]
    selected_odds = [row for row in odds if row.timestamp <= as_of]
    if any(book.timestamp > as_of for book in selected_books):
        raise AssertionError("future order book leaked into point-in-time selection")
    if any(row.timestamp > as_of for row in selected_odds):
        raise AssertionError("future odds leaked into point-in-time selection")
    latest_books: dict[tuple[str, str], OrderBook] = {}
    for book in selected_books:
        key = (book.market_id, book.token_id)
        current = latest_books.get(key)
        if current is None or book.timestamp > current.timestamp:
            latest_books[key] = book
    latest_odds: dict[tuple[str, str, str, str], ExternalOdds] = {}
    for row in selected_odds:
        key = (row.event_id, row.book, row.market_type, row.outcome)
        current = latest_odds.get(key)
        if current is None or row.timestamp > current.timestamp:
            latest_odds[key] = row
    return list(latest_books.values()), list(latest_odds.values())


class Backtester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        markets: list[Market],
        orderbooks: list[OrderBook],
        odds: list[ExternalOdds],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> BacktestResult:
        timestamps = sorted({book.timestamp for book in orderbooks} | {row.timestamp for row in odds})
        if start:
            timestamps = [ts for ts in timestamps if ts >= start]
        if end:
            timestamps = [ts for ts in timestamps if ts <= end]

        risk_manager = RiskManager(self.settings)
        trader = PaperTrader(self.settings, risk_manager)
        result = BacktestResult()
        markets_by_id = {market.market_id: market for market in markets}
        outcome_by_token = {
            (market.market_id, token_id): outcome
            for market in markets
            for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False)
        }

        for as_of in timestamps:
            as_of = as_of.astimezone(UTC)
            pit_books, pit_odds = select_point_in_time_inputs(as_of, orderbooks, odds)
            if not pit_books or not pit_odds:
                continue
            model = FairValueModel(
                base_uncertainty_bps=self.settings.model_uncertainty_bps,
                now=as_of,
            )
            fair_values = model.compute_fair_values(
                markets=markets,
                odds=pit_odds,
                orderbooks=pit_books,
                as_of=as_of,
            )
            bbas = [book.best_bid_ask() for book in pit_books]
            signals = generate_edge_signals(markets, fair_values, bbas, self.settings, as_of=as_of)
            result.signals_evaluated += len(signals)
            bba_by_key: dict[tuple[str, str], BestBidAsk] = {}
            for bba in bbas:
                outcome = outcome_by_token.get((bba.market_id, bba.token_id))
                if outcome:
                    bba_by_key[(bba.market_id, outcome)] = bba
            latest_odds_ts = max((row.timestamp for row in pit_odds), default=as_of)
            for signal in signals:
                if signal.action not in {"BUY_YES", "SELL_YES"}:
                    continue
                market = markets_by_id.get(signal.market_id)
                bba = bba_by_key.get((signal.market_id, signal.outcome))
                if not market or not bba:
                    continue
                decision = trader.process_signal(
                    signal=signal,
                    market=market,
                    bba=bba,
                    proposed_size=min(signal.available_size, self.settings.max_trade_size),
                    odds_timestamp=latest_odds_ts,
                    as_of=as_of,
                )
                result.decisions.append(decision)

        latest_bbas = {
            (book.market_id, outcome_by_token.get((book.market_id, book.token_id), book.token_id)): book.best_bid_ask()
            for book in orderbooks
        }
        result.final_cash_pnl = trader.realized_pnl()
        result.final_mark_to_market_pnl = trader.mark_to_market(latest_bbas)
        return result
