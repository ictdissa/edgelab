from __future__ import annotations

from datetime import UTC, datetime

from edgelab.config.settings import Settings
from edgelab.data.schemas import OrderBook, PredictionMarket, PriceLevel
from edgelab.strategies.relative_value_graph import (
    RV_RELATION_EXHAUSTIVE,
    RV_RELATION_IMPLICATION,
    RV_STATUS_EXECUTABLE,
    RV_STATUS_INSUFFICIENT_LIQUIDITY,
    RV_STATUS_MISSING_BOOK,
    RV_STATUS_NO_VIOLATION,
    RV_STATUS_THEORETICAL_ONLY,
    find_relative_value_opportunities,
)

NOW = datetime(2026, 5, 19, tzinfo=UTC)


def test_exhaustive_outcomes_near_one_produce_no_violation() -> None:
    market = _market(
        "group-winner",
        "group",
        "exhaustive_multi_outcome",
        "Which club wins Group A?",
        ["Alpha", "Bravo", "Charlie"],
        ["alpha", "bravo", "charlie"],
    )
    books = [
        _book("group-winner", "alpha", 0.32, 0.34),
        _book("group-winner", "bravo", 0.32, 0.34),
        _book("group-winner", "charlie", 0.33, 0.35),
    ]

    rows = find_relative_value_opportunities([market], books, _settings())
    exhaustive = [row for row in rows if row.relation_type == RV_RELATION_EXHAUSTIVE]

    assert exhaustive
    assert exhaustive[0].status == RV_STATUS_NO_VIOLATION
    assert exhaustive[0].theoretical_violation == 0.0


def test_child_probability_greater_than_parent_creates_theoretical_violation() -> None:
    child, parent = _spain_implication_markets()
    rows = find_relative_value_opportunities(
        [child, parent],
        [
            _book(child.market_id, "child-yes", 0.58, 0.60),
            _book(parent.market_id, "parent-yes", 0.48, 0.50),
        ],
        _settings(),
    )

    implication = _first_implication(rows)

    assert implication.theoretical_violation > 0
    assert implication.child_market == child.market_id
    assert implication.parent_market == parent.market_id


def test_missing_bid_or_ask_marks_theoretical_only_or_missing_book() -> None:
    child, parent = _spain_implication_markets()
    rows = find_relative_value_opportunities(
        [child, parent],
        [
            _book(child.market_id, "child-yes", 0.58, 0.60),
            OrderBook(
                market_id=parent.market_id,
                token_id="parent-yes",
                timestamp=NOW,
                bids=[PriceLevel(price=0.48, size=50)],
                asks=[],
            ),
        ],
        _settings(),
    )

    implication = _first_implication(rows)

    assert implication.status in {RV_STATUS_THEORETICAL_ONLY, RV_STATUS_MISSING_BOOK}
    assert implication.status != RV_STATUS_EXECUTABLE


def test_insufficient_liquidity_is_not_executable() -> None:
    child, parent = _spain_implication_markets()
    rows = find_relative_value_opportunities(
        [child, parent],
        [
            _book(child.market_id, "child-yes", 0.80, 0.82, size=2),
            _book(parent.market_id, "parent-yes", 0.40, 0.42, size=2),
        ],
        _settings(min_liquidity=10),
    )

    implication = _first_implication(rows)

    assert implication.executable_violation > 0
    assert implication.status == RV_STATUS_INSUFFICIENT_LIQUIDITY


def test_executable_violation_requires_all_legs_to_have_tradable_bid_ask() -> None:
    child, parent = _spain_implication_markets()
    missing_leg_rows = find_relative_value_opportunities(
        [child, parent],
        [
            _book(child.market_id, "child-yes", 0.80, 0.82),
            OrderBook(
                market_id=parent.market_id,
                token_id="parent-yes",
                timestamp=NOW,
                bids=[PriceLevel(price=0.40, size=100)],
                asks=[],
            ),
        ],
        _settings(min_liquidity=10),
    )

    executable_rows = find_relative_value_opportunities(
        [child, parent],
        [
            _book(child.market_id, "child-yes", 0.80, 0.82),
            _book(parent.market_id, "parent-yes", 0.40, 0.42),
        ],
        _settings(min_liquidity=10),
    )

    assert _first_implication(missing_leg_rows).status != RV_STATUS_EXECUTABLE
    assert _first_implication(executable_rows).status == RV_STATUS_EXECUTABLE


def _spain_implication_markets() -> tuple[PredictionMarket, PredictionMarket]:
    child = _market(
        "spain-wins-world-cup",
        "world-cup",
        "sports_outright",
        "Will Spain win the 2026 FIFA World Cup?",
        ["Yes", "No"],
        ["child-yes", "child-no"],
    )
    parent = _market(
        "spain-reaches-final",
        "world-cup",
        "sports_outright",
        "Will Spain reach the 2026 FIFA World Cup final?",
        ["Yes", "No"],
        ["parent-yes", "parent-no"],
    )
    return child, parent


def _first_implication(rows):
    return next(row for row in rows if row.relation_type == RV_RELATION_IMPLICATION)


def _market(
    market_id: str,
    event_id: str,
    category: str,
    title: str,
    outcomes: list[str],
    token_ids: list[str],
) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        event_id=event_id,
        category=category,
        title=title,
        outcomes=outcomes,
        token_ids=token_ids,
        resolution_source="test fixture",
    )


def _book(
    market_id: str,
    token_id: str,
    bid: float,
    ask: float,
    size: float = 100,
) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=NOW,
        bids=[PriceLevel(price=bid, size=size)],
        asks=[PriceLevel(price=ask, size=size)],
    )


def _settings(min_liquidity: float = 1) -> Settings:
    return Settings(
        MIN_LIQUIDITY=min_liquidity,
        DEFAULT_SLIPPAGE_BPS=25,
        FEE_BPS=20,
        RESOLUTION_RISK_BPS=30,
        SAFETY_MARGIN_BPS=50,
    )
