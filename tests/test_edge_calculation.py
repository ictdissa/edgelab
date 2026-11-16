from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, FairValue, Market
from sharpedge.strategies.taker_edge import compute_edge_signal
from edgelab.config.settings import Settings as EdgeLabSettings
from edgelab.data.schemas import BestBidAsk as EdgeLabBBA
from edgelab.data.schemas import FairValue as EdgeLabFairValue
from edgelab.data.schemas import PredictionMarket
from edgelab.strategies.taker_edge import compute_edge_signal as edgelab_compute_edge_signal


def _market() -> Market:
    return Market(
        market_id="m1",
        event_id="e1",
        sport="MMA",
        league="UFC",
        title="Fighter A vs Fighter B moneyline",
        outcomes=["Fighter A", "Fighter B"],
        token_ids=["a", "b"],
        start_time=datetime(2026, 6, 1, tzinfo=UTC),
        end_time=datetime(2026, 6, 1, 3, tzinfo=UTC),
        resolution_source="official",
        status="active",
        raw_metadata={},
    )


def test_buy_edge_uses_ask_and_adjusts_for_costs() -> None:
    settings = Settings(
        MIN_ADJUSTED_EDGE=0.01,
        DEFAULT_SLIPPAGE_BPS=20,
        MODEL_UNCERTAINTY_BPS=50,
        RESOLUTION_RISK_BPS=25,
        SAFETY_MARGIN_BPS=25,
    )
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    fair = FairValue(
        event_id="e1",
        market_id="m1",
        outcome="Fighter A",
        fair_probability=0.60,
        source="test",
        timestamp=as_of,
        uncertainty=0.006,
    )
    bba = BestBidAsk(
        market_id="m1",
        token_id="a",
        timestamp=as_of,
        best_bid=0.52,
        best_bid_size=100,
        best_ask=0.55,
        best_ask_size=100,
        mid=0.535,
        spread=0.03,
    )
    signal = compute_edge_signal(_market(), "Fighter A", fair, bba, settings, as_of)
    assert signal.action == "BUY_YES"
    assert math.isclose(signal.raw_edge, 0.05)
    assert signal.adjusted_edge > settings.min_adjusted_edge


def test_missing_orderbook_returns_avoid() -> None:
    settings = Settings()
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    fair = FairValue(
        event_id="e1",
        market_id="m1",
        outcome="Fighter A",
        fair_probability=0.60,
        source="test",
        timestamp=as_of,
        uncertainty=0.01,
    )
    bba = BestBidAsk(
        market_id="m1",
        token_id="a",
        timestamp=as_of,
        best_bid=None,
        best_bid_size=None,
        best_ask=None,
        best_ask_size=None,
        mid=None,
        spread=None,
    )
    signal = compute_edge_signal(_market(), "Fighter A", fair, bba, settings, as_of)
    assert signal.action == "AVOID"
    assert "missing executable" in signal.reason


def test_edgelab_adjusted_edge_uses_executable_ask() -> None:
    settings = EdgeLabSettings(MIN_ADJUSTED_EDGE=0.005, MIN_LIQUIDITY=1)
    as_of = datetime(2026, 5, 18, tzinfo=UTC)
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="Will BTC be above 100k?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="index",
    )
    fair = EdgeLabFairValue(
        event_id="e",
        market_id="m",
        outcome="Yes",
        fair_probability=0.70,
        source="test",
        timestamp=as_of,
        uncertainty=0.005,
    )
    bba = EdgeLabBBA(
        market_id="m",
        token_id="yes",
        timestamp=as_of,
        best_bid=0.62,
        best_bid_size=50,
        best_ask=0.64,
        best_ask_size=50,
        mid=0.63,
        spread=0.02,
    )
    signal = edgelab_compute_edge_signal(market, fair, bba, settings, as_of)
    assert signal.action == "BUY_YES"
    assert signal.raw_edge == pytest.approx(0.06)
    assert signal.freshness_status == "fresh"
    assert signal.data_age_seconds == pytest.approx(0.0)


def test_edgelab_stale_live_data_outputs_avoid() -> None:
    settings = EdgeLabSettings(MAX_DATA_AGE_SECONDS=60, MIN_LIQUIDITY=1)
    as_of = datetime(2026, 5, 18, tzinfo=UTC)
    old = datetime(2026, 5, 17, 23, 55, tzinfo=UTC)
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="Will BTC be above 100k?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="index",
    )
    fair = EdgeLabFairValue(
        event_id="e",
        market_id="m",
        outcome="Yes",
        fair_probability=0.70,
        source="test",
        timestamp=old,
        uncertainty=0.005,
    )
    bba = EdgeLabBBA(
        market_id="m",
        token_id="yes",
        timestamp=old,
        best_bid=0.62,
        best_bid_size=50,
        best_ask=0.64,
        best_ask_size=50,
        mid=0.63,
        spread=0.02,
    )

    signal = edgelab_compute_edge_signal(market, fair, bba, settings, as_of)

    assert signal.action == "AVOID"
    assert signal.freshness_status == "stale"


def test_edgelab_missing_one_side_of_book_outputs_avoid() -> None:
    settings = EdgeLabSettings(MIN_LIQUIDITY=1)
    as_of = datetime(2026, 5, 18, tzinfo=UTC)
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="Will BTC be above 100k?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="index",
    )
    fair = EdgeLabFairValue(
        event_id="e",
        market_id="m",
        outcome="Yes",
        fair_probability=0.70,
        source="test",
        timestamp=as_of,
        uncertainty=0.005,
    )
    bba = EdgeLabBBA(
        market_id="m",
        token_id="yes",
        timestamp=as_of,
        best_bid=0.62,
        best_bid_size=50,
        best_ask=None,
        best_ask_size=None,
        mid=None,
        spread=None,
    )

    signal = edgelab_compute_edge_signal(market, fair, bba, settings, as_of)

    assert signal.action == "AVOID"
    assert "missing executable bid/ask" in signal.reason
