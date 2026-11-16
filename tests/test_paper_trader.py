from __future__ import annotations

from datetime import UTC, datetime

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, Signal
from edgelab.execution.paper_trader import PaperTrader


def test_paper_trader_fills_at_ask_never_mid() -> None:
    settings = Settings(MIN_ADJUSTED_EDGE=0.0, MIN_LIQUIDITY=1.0)
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="Will BTC be above 100k?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="index",
    )
    signal = Signal(
        signal_id="s",
        timestamp=datetime.now(UTC),
        market_id="m",
        event_id="e",
        outcome="Yes",
        category="crypto_threshold",
        fair_probability=0.60,
        prediction_bid=0.50,
        prediction_ask=0.55,
        raw_edge=0.05,
        cost_drag=0.01,
        adjusted_edge=0.04,
        available_size=10,
        confidence=0.5,
        action="BUY_YES",
        reason="test",
        fair_value_source="mock",
        token_id="yes", match_decision="MATCH",
        fair_value_timestamp=datetime.now(UTC), orderbook_timestamp=datetime.now(UTC),
    )
    trades = PaperTrader(settings).execute([signal], {"m": market})
    assert trades[0].approved
    assert trades[0].price == 0.55
    assert trades[0].execution_price_source == "ask"
    assert trades[0].fair_probability == 0.60
