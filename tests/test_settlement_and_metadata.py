"""Economic equivalence and point-in-time market universe regression tests."""

from datetime import UTC, datetime, timedelta
import json
import pytest

from edgelab.config.settings import Settings
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, PriceLevel
from edgelab.matching.settlement import settlement_equivalent, apply_settlement_registry
from edgelab.research.backtest import MarketObservation, replay, market_universe_as_of
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.strategies.taker_edge import generate_signals

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def spec():
    return dict(
        review_status="reviewed",
        period="regulation_time",
        includes_overtime=False,
        draw_is_outcome=True,
        cancellation_rule="synthetic-identical-v1",
        evidence_reference="unit-test reviewed rules",
        reviewed_at=NOW.isoformat(),
    )


def test_matching_names_do_not_prove_settlement_equivalence():
    assert settlement_equivalent(spec(), spec(), NOW)[0]
    for change in (
        {"period": "qualification"},
        {"includes_overtime": True},
        {"draw_is_outcome": False},
        {"cancellation_rule": "different"},
        {"reviewed_at": (NOW + timedelta(seconds=1)).isoformat()},
    ):
        assert not settlement_equivalent(spec(), {**spec(), **change}, NOW)[0]
    assert not settlement_equivalent(None, None, NOW)[0]


def test_unreviewed_sports_anchor_is_avoid_even_with_large_edge():
    market = PredictionMarket(
        market_id="m",
        event_id="e",
        category="sports_1x2",
        title="Arsenal vs Chelsea match winner",
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["a", "d", "c"],
        start_time=NOW + timedelta(hours=2),
        resolution_source="official",
    )
    anchors = [
        ExternalAnchor(
            source_id=o,
            event_id="e",
            source="book",
            category="sports_1x2",
            outcome=o,
            decimal_odds=odds,
            timestamp=NOW,
            available_at=NOW,
        )
        for o, odds in [("Arsenal", 2), ("Draw", 3), ("Chelsea", 4)]
    ]
    book = OrderBook(
        market_id="m",
        token_id="a",
        timestamp=NOW,
        available_at=NOW,
        bids=[PriceLevel(price=0.1, size=100)],
        asks=[PriceLevel(price=0.12, size=100)],
    )
    settings = Settings(_env_file=None, MIN_LIQUIDITY=1)
    values = SportsOddsModel(settings, NOW).compute([market], anchors, [book])
    result = generate_signals([market], values, [book.best_bid_ask()], settings, NOW)
    assert all(s.action == "AVOID" for s in result)
    market.raw_metadata["settlement_spec"] = spec()
    for a in anchors:
        a.metadata["settlement_spec"] = spec()
    values = SportsOddsModel(settings, NOW).compute([market], anchors, [book])
    result = generate_signals([market], values, [book.best_bid_ask()], settings, NOW)
    assert next(s for s in result if s.outcome == "Arsenal").action == "BUY_YES"


def test_future_market_metadata_cannot_change_earlier_decisions():
    m = PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="BTC above $110 at expiry",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        end_time=NOW + timedelta(days=30),
        resolution_source="index",
    )
    a = ExternalAnchor(
        source_id="a",
        market_id="m",
        event_id="e",
        source="mock_spot",
        category="crypto_threshold",
        outcome="Yes",
        timestamp=NOW,
        available_at=NOW,
        metadata={"spot": 100, "threshold": 110, "volatility": 0.5},
    )
    b = OrderBook(
        market_id="m",
        token_id="yes",
        timestamp=NOW,
        available_at=NOW,
        bids=[PriceLevel(price=0.1, size=100)],
        asks=[PriceLevel(price=0.12, size=100)],
    )
    history = [MarketObservation(m, NOW, NOW)]
    future = m.model_copy(update={"status": "resolved", "title": "changed future title"})
    extended = history + [
        MarketObservation(future, NOW + timedelta(days=1), NOW + timedelta(days=1))
    ]
    settings = Settings(_env_file=None, MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10)
    first = replay([future], [a], [b], settings, [NOW], market_history=history)
    second = replay([future], [a], [b], settings, [NOW], market_history=extended)
    assert first.signals == second.signals and first.decisions == second.decisions
    assert any(d.approved for d in first.decisions)
    assert market_universe_as_of([m], extended, NOW + timedelta(days=1))["m"].status == "resolved"


def test_registry_is_scoped_by_event_book_and_market(tmp_path):
    m = PredictionMarket(
        market_id="m",
        event_id="pm",
        category="sports_1x2",
        title="Arsenal vs Chelsea",
        outcomes=["Yes", "No"],
        token_ids=["y", "n"],
    )
    a = ExternalAnchor(
        source_id="a",
        event_id="external",
        source="bookA",
        category="sports_1x2",
        outcome="Arsenal",
        decimal_odds=2,
        timestamp=NOW,
    )
    b = a.model_copy(update={"source_id": "b", "source": "bookB"})
    path = tmp_path / "reviews.json"
    path.write_text(
        json.dumps(
            [
                dict(
                    market_id="m",
                    external_event_id="external",
                    book="bookA",
                    prediction=spec(),
                    external=spec(),
                )
            ]
        )
    )
    markets, anchors = apply_settlement_registry([m], [a, b], path)
    assert markets[0].raw_metadata["settlement_spec"]["review_status"] == "reviewed"
    assert anchors[0].market_id == "m" and anchors[0].metadata.get("settlement_spec")
    assert not anchors[1].metadata.get("settlement_spec")
    assert not m.raw_metadata, "registry enrichment must not mutate prior metadata snapshots"


def test_same_vintage_conflict_is_not_order_dependent():
    m = PredictionMarket(
        market_id="m",
        event_id="pm",
        category="sports_1x2",
        title="Arsenal vs Chelsea",
        outcomes=["Yes", "No"],
        token_ids=["y", "n"],
    )
    different = m.model_copy(update={"status": "resolved"})
    for history in (
        [MarketObservation(m, NOW, NOW), MarketObservation(different, NOW, NOW)],
        [MarketObservation(different, NOW, NOW), MarketObservation(m, NOW, NOW)],
    ):
        with pytest.raises(ValueError, match="conflicting"):
            market_universe_as_of([], history, NOW)


def test_future_history_cannot_remove_a_known_earlier_fallback():
    m = PredictionMarket(
        market_id="m",
        event_id="e",
        category="sports_1x2",
        title="Arsenal vs Chelsea",
        outcomes=["Yes", "No"],
        token_ids=["y", "n"],
        raw_metadata={"collected_at": NOW.isoformat()},
    )
    old = MarketObservation(m, NOW, NOW)
    future = m.model_copy(update={"status": "resolved"})
    later = MarketObservation(future, NOW + timedelta(days=1), NOW + timedelta(days=1))
    assert market_universe_as_of([m], [later], NOW) == market_universe_as_of([m], [], NOW)
    m.status = "resolved"
    assert old.market.status == "active", "later mutation must not alter archived metadata"
