from __future__ import annotations

from datetime import UTC, datetime

import pytest

from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.research.backtest import assert_no_lookahead, point_in_time_filter
from sharpedge.config.settings import Settings
from sharpedge.data.schemas import ExternalOdds, Market, OrderBook, PriceLevel
from sharpedge.research.backtest import Backtester, select_point_in_time_inputs


def test_point_in_time_selection_excludes_future_odds_and_books() -> None:
    t0 = datetime(2026, 5, 16, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 16, 1, 0, tzinfo=UTC)
    books = [
        OrderBook(market_id="m1", token_id="a", timestamp=t0, bids=[PriceLevel(price=0.4, size=10)], asks=[PriceLevel(price=0.45, size=10)]),
        OrderBook(market_id="m1", token_id="a", timestamp=t1, bids=[PriceLevel(price=0.9, size=10)], asks=[PriceLevel(price=0.95, size=10)]),
    ]
    odds = [
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="A", decimal_odds=2.0, timestamp=t0),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="B", decimal_odds=2.0, timestamp=t0),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="A", decimal_odds=1.05, timestamp=t1),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="B", decimal_odds=12.0, timestamp=t1),
    ]
    selected_books, selected_odds = select_point_in_time_inputs(t0, books, odds)
    assert len(selected_books) == 1
    assert selected_books[0].best_bid_ask().best_bid == 0.4
    assert len(selected_odds) == 2
    assert all(row.timestamp <= t0 for row in selected_odds)


def test_backtest_does_not_use_future_fair_value_for_entry() -> None:
    t0 = datetime(2026, 5, 16, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 16, 1, 0, tzinfo=UTC)
    market = Market(
        market_id="m1",
        event_id="e1",
        sport="MMA",
        league="UFC",
        title="A vs B moneyline",
        outcomes=["A", "B"],
        token_ids=["a", "b"],
        start_time=datetime(2026, 6, 1, tzinfo=UTC),
        end_time=datetime(2026, 6, 1, 3, tzinfo=UTC),
        resolution_source="official",
        status="active",
        raw_metadata={},
    )
    books = [
        OrderBook(market_id="m1", token_id="a", timestamp=t0, bids=[PriceLevel(price=0.49, size=20)], asks=[PriceLevel(price=0.51, size=20)]),
        OrderBook(market_id="m1", token_id="b", timestamp=t0, bids=[PriceLevel(price=0.47, size=20)], asks=[PriceLevel(price=0.50, size=20)]),
    ]
    odds = [
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="A", decimal_odds=2.0, timestamp=t0),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="B", decimal_odds=2.0, timestamp=t0),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="A", decimal_odds=1.10, timestamp=t1),
        ExternalOdds(event_id="e1", sport="MMA", book="Sharp", market_type="moneyline", outcome="B", decimal_odds=8.0, timestamp=t1),
    ]
    settings = Settings(MIN_ADJUSTED_EDGE=0.01, MAX_TRADE_SIZE=5)
    with pytest.warns(DeprecationWarning, match="SharpEdge pricing is deprecated"):
        result = Backtester(settings).run([market], books, odds, start=t0, end=t0)
    assert result.signals_evaluated == 2
    assert sum(1 for decision in result.decisions if decision.executed) == 0


def test_edgelab_point_in_time_filter_excludes_future_rows() -> None:
    provider = MockProvider()
    as_of = datetime(2026, 5, 17, tzinfo=UTC)
    anchors, books = point_in_time_filter(as_of, provider.anchors(), provider.orderbooks())
    assert anchors == []
    assert books == []


def test_edgelab_assert_no_lookahead_raises_on_future_rows() -> None:
    provider = MockProvider()
    as_of = datetime(2026, 5, 17, tzinfo=UTC)
    with pytest.raises(ValueError, match="Lookahead data detected"):
        assert_no_lookahead(as_of, provider.anchors(), provider.orderbooks())
