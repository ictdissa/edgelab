from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.generate_paper_signals import (
    RV_STATUS_PAPER_ELIGIBLE,
    RV_STATUS_INCOMPLETE_PACKAGE,
    RV_STATUS_PRICING_INCOMPLETE,
    build_candidates,
    build_relative_value_candidates,
)
from edgelab.config.settings import Settings
from edgelab.data.schemas import OrderBook, PredictionMarket, PriceLevel

NOW = datetime(2026, 5, 20, tzinfo=UTC)


def test_relative_value_executable_converts_to_multileg_candidate() -> None:
    rows = [
        {
            "relation_type": "implication",
            "status": "EXECUTABLE",
            "required_legs": "SELL child:Yes at bid; BUY parent:Yes at ask",
            "involved_outcomes": "child:Yes | parent:Yes",
            "executable_violation": "0.03",
            "theoretical_violation": "0.05",
            "available_liquidity": "25",
            "max_trade_size": "10",
        }
    ]

    markets = [
        _market("child", ["Yes", "No"], ["child-yes", "child-no"]),
        _market("parent", ["Yes", "No"], ["parent-yes", "parent-no"]),
    ]
    books = [
        _book("child", "child-yes", 0.60, 0.62),
        _book("parent", "parent-yes", 0.40, 0.42),
    ]

    candidates = build_relative_value_candidates(
        rows,
        snapshot_timestamp=NOW.isoformat(),
        markets=markets,
        orderbooks=books,
        settings=Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.10),
    )

    assert candidates[0]["final_decision"] == "RELATIVE_VALUE_MULTI_LEG"
    assert candidates[0]["status"] == RV_STATUS_PAPER_ELIGIBLE
    assert candidates[0]["leg_count"] == "2"
    legs = json.loads(candidates[0]["legs_json"])
    assert len(legs) == 2
    assert all(leg["entry_price"] for leg in legs)


def test_relative_value_missing_leg_bid_ask_is_not_executable_package() -> None:
    rows = [
        {
            "relation_type": "implication",
            "status": "EXECUTABLE",
            "required_legs": "SELL child:Yes at bid; BUY parent:Yes at ask",
            "involved_outcomes": "child:Yes | parent:Yes",
            "executable_violation": "0.03",
            "theoretical_violation": "0.05",
        }
    ]
    candidates = build_relative_value_candidates(
        rows,
        snapshot_timestamp=NOW.isoformat(),
        markets=[_market("child", ["Yes", "No"], ["child-yes", "child-no"])],
        orderbooks=[_book("child", "child-yes", 0.60, 0.62)],
        settings=Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.10),
    )

    assert candidates[0]["status"] in {RV_STATUS_PRICING_INCOMPLETE, RV_STATUS_INCOMPLETE_PACKAGE}
    assert candidates[0]["paper_trade_eligible"] == "false"


def test_relative_value_rows_do_not_enter_single_leg_paper_candidates() -> None:
    rows = [
        {
            "category": "relative_value",
            "market_id": "child",
            "outcome": "child:Yes | parent:Yes",
            "action": "RELATIVE_VALUE_EXECUTABLE",
            "adjusted_edge": "0.03",
            "liquidity": "25",
            "data_age_seconds": "1",
            "risk_decision": "APPROVED",
        }
    ]

    candidates, _rejections, rejected = build_candidates(
        rows,
        Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.08),
        None,
    )

    assert candidates == []
    assert rejected == []


def test_static_report_relative_value_candidate_is_not_paper_eligible() -> None:
    rows = [
        {
            "relation_type": "implication",
            "status": "EXECUTABLE",
            "required_legs": "SELL child:Yes at bid; BUY parent:Yes at ask",
            "involved_outcomes": "child:Yes | parent:Yes",
            "executable_violation": "0.03",
            "theoretical_violation": "0.05",
            "relative_value_source": "static_report",
        }
    ]
    markets = [
        _market("child", ["Yes", "No"], ["child-yes", "child-no"]),
        _market("parent", ["Yes", "No"], ["parent-yes", "parent-no"]),
    ]
    books = [
        _book("child", "child-yes", 0.60, 0.62),
        _book("parent", "parent-yes", 0.40, 0.42),
    ]

    candidates = build_relative_value_candidates(
        rows,
        snapshot_timestamp=NOW.isoformat(),
        markets=markets,
        orderbooks=books,
        settings=Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.10),
    )

    assert candidates[0]["relative_value_source"] == "static_report"
    assert candidates[0]["empirical_evidence_eligible"] == "false"
    assert candidates[0]["paper_trade_eligible"] == "false"
    assert "requires live_snapshot" in candidates[0]["paper_trade_blocker"]


def _market(market_id: str, outcomes: list[str], token_ids: list[str]) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        event_id=market_id,
        category="sports_outright",
        title=market_id,
        outcomes=outcomes,
        token_ids=token_ids,
    )


def _book(market_id: str, token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=NOW,
        bids=[PriceLevel(price=bid, size=20)],
        asks=[PriceLevel(price=ask, size=20)],
    )
