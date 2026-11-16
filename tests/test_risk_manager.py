from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, Market, Signal
from sharpedge.execution.paper_trader import PaperTrader
from sharpedge.execution.risk_manager import RiskManager
from edgelab.config.settings import Settings as EdgeLabSettings
from edgelab.data.schemas import PredictionMarket, Signal as EdgeLabSignal
from edgelab.execution.risk_manager import RiskManager as EdgeLabRiskManager


def _market() -> Market:
    return Market(
        market_id="m1",
        event_id="e1",
        sport="Soccer",
        league="Premier League",
        title="Arsenal vs Chelsea 1X2",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["home", "draw", "away"],
        start_time=datetime(2026, 6, 2, tzinfo=UTC),
        end_time=datetime(2026, 6, 2, 2, tzinfo=UTC),
        resolution_source="official",
        status="active",
        raw_metadata={},
    )


def _signal() -> Signal:
    return Signal(
        timestamp=datetime(2026, 5, 16, tzinfo=UTC),
        market_id="m1",
        event_id="e1",
        outcome="Arsenal",
        signal_type="test",
        fair_probability=0.55,
        polymarket_bid=0.40,
        polymarket_ask=0.42,
        raw_edge=0.13,
        adjusted_edge=0.08,
        available_size=25,
        confidence=0.8,
        action="BUY_YES",
        reason="test",
    )


def test_risk_rejects_live_when_disabled() -> None:
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    settings = Settings(ENABLE_LIVE_TRADING=False)
    decision = RiskManager(settings).check_signal(
        signal=_signal(),
        market=_market(),
        proposed_size=5,
        orderbook_timestamp=as_of,
        odds_timestamp=as_of,
        live=True,
        as_of=as_of,
    )
    assert not decision.approved
    assert "live trading disabled" in decision.reasons


def test_risk_rejects_stale_data() -> None:
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    settings = Settings(MAX_ORDERBOOK_AGE_SECONDS=60, MAX_ODDS_AGE_SECONDS=60)
    decision = RiskManager(settings).check_signal(
        signal=_signal(),
        market=_market(),
        proposed_size=5,
        orderbook_timestamp=as_of - timedelta(minutes=5),
        odds_timestamp=as_of - timedelta(minutes=5),
        live=False,
        as_of=as_of,
    )
    assert not decision.approved
    assert "order book is stale" in decision.reasons
    assert "external odds are stale" in decision.reasons


def test_paper_trader_buys_at_ask_never_mid() -> None:
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    settings = Settings(MIN_ADJUSTED_EDGE=0.01, MAX_TRADE_SIZE=10)
    bba = BestBidAsk(
        market_id="m1",
        token_id="home",
        timestamp=as_of,
        best_bid=0.40,
        best_bid_size=25,
        best_ask=0.42,
        best_ask_size=25,
        mid=0.41,
        spread=0.02,
    )
    trader = PaperTrader(settings, RiskManager(settings))
    decision = trader.process_signal(
        signal=_signal(),
        market=_market(),
        bba=bba,
        proposed_size=5,
        odds_timestamp=as_of,
        as_of=as_of,
    )
    assert decision.executed
    assert decision.trade is not None
    assert decision.trade.price == 0.42
    assert decision.trade.price != 0.41


def test_edgelab_risk_rejects_review_mapping_and_live() -> None:
    as_of = datetime(2026, 5, 18, tzinfo=UTC)
    settings = EdgeLabSettings(MIN_LIQUIDITY=1)
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="sports_1x2",
        title="Arsenal vs Chelsea",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="official",
    )
    signal = EdgeLabSignal(
        signal_id="s",
        timestamp=as_of,
        market_id="m",
        event_id="e",
        outcome="Yes",
        category="sports_1x2",
        fair_probability=0.6,
        prediction_bid=0.5,
        prediction_ask=0.55,
        raw_edge=0.05,
        cost_drag=0.01,
        adjusted_edge=0.04,
        available_size=10,
        confidence=0.5,
        action="BUY_YES",
        reason="test",
        fair_value_source="mock",
    )
    decision = EdgeLabRiskManager(settings).check(
        signal,
        market,
        proposed_size=5,
        data_timestamp=as_of,
        live=True,
        match_decision="REVIEW",
        as_of=as_of,
    )
    assert not decision.approved
    assert "live trading disabled by default" in decision.reasons
    assert "event mapping is REVIEW" in decision.reasons
