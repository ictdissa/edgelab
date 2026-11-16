from __future__ import annotations

from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, FairValue, Market
from sharpedge.execution.paper_trader import PaperTrader
from sharpedge.execution.risk_manager import RiskManager
from sharpedge.strategies.taker_edge import compute_edge_signal


def _market() -> Market:
    return Market(
        market_id="m1",
        event_id="e1",
        sport="Soccer",
        league="Premier League",
        title="Arsenal vs Chelsea 1X2 match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["ars", "draw", "che"],
        start_time=datetime(2026, 6, 1, tzinfo=UTC),
        resolution_source="official league result",
        status="active",
    )


def test_paper_trader_buys_at_ask_not_mid() -> None:
    settings = Settings(
        MIN_ADJUSTED_EDGE=0.01,
        DEFAULT_SLIPPAGE_BPS=1,
        MODEL_UNCERTAINTY_BPS=1,
        RESOLUTION_RISK_BPS=1,
        SAFETY_MARGIN_BPS=1,
        MIN_LIQUIDITY=1,
    )
    as_of = datetime(2026, 5, 20, tzinfo=UTC)
    market = _market()
    fair = FairValue(
        event_id="e1",
        market_id="m1",
        outcome="Arsenal",
        fair_probability=0.62,
        source="test",
        timestamp=as_of,
        uncertainty=0.001,
    )
    bba = BestBidAsk(
        market_id="m1",
        token_id="ars",
        timestamp=as_of,
        best_bid=0.50,
        best_bid_size=100,
        best_ask=0.55,
        best_ask_size=100,
        mid=0.525,
        spread=0.05,
    )
    signal = compute_edge_signal(market, "Arsenal", fair, bba, settings, as_of)
    decision = PaperTrader(settings, RiskManager(settings)).process_signal(
        signal,
        market,
        bba,
        proposed_size=5,
        odds_timestamp=as_of,
        as_of=as_of,
    )
    assert decision.executed
    assert decision.trade is not None
    assert decision.trade.paper_or_live == "paper"
    assert decision.trade.price == 0.55


def test_paper_trader_sells_at_bid_not_mid() -> None:
    settings = Settings(
        MIN_ADJUSTED_EDGE=0.01,
        DEFAULT_SLIPPAGE_BPS=1,
        MODEL_UNCERTAINTY_BPS=1,
        RESOLUTION_RISK_BPS=1,
        SAFETY_MARGIN_BPS=1,
        MIN_LIQUIDITY=1,
    )
    as_of = datetime(2026, 5, 20, tzinfo=UTC)
    market = _market()
    fair = FairValue(
        event_id="e1",
        market_id="m1",
        outcome="Arsenal",
        fair_probability=0.40,
        source="test",
        timestamp=as_of,
        uncertainty=0.001,
    )
    bba = BestBidAsk(
        market_id="m1",
        token_id="ars",
        timestamp=as_of,
        best_bid=0.50,
        best_bid_size=100,
        best_ask=0.55,
        best_ask_size=100,
        mid=0.525,
        spread=0.05,
    )
    signal = compute_edge_signal(market, "Arsenal", fair, bba, settings, as_of)
    decision = PaperTrader(settings, RiskManager(settings)).process_signal(
        signal,
        market,
        bba,
        proposed_size=5,
        odds_timestamp=as_of,
        as_of=as_of,
    )
    assert decision.executed
    assert decision.trade is not None
    assert decision.trade.paper_or_live == "paper"
    assert decision.trade.price == 0.50
