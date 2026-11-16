from __future__ import annotations

from datetime import UTC, datetime

from edgelab.config.settings import Settings
from edgelab.data.schemas import OrderBook, PredictionMarket, PriceLevel
from scripts.generate_paper_signals import RV_STATUS_PAPER_ELIGIBLE, build_relative_value_candidates

NOW = datetime(2026, 5, 20, tzinfo=UTC)


def test_all_priced_buy_sell_legs_become_paper_eligible_package() -> None:
    candidates = build_relative_value_candidates(
        [
            {
                "relation_type": "implication",
                "status": "EXECUTABLE",
                "required_legs": "SELL child:Yes at bid; BUY parent:Yes at ask",
                "involved_outcomes": "child:Yes | parent:Yes",
                "executable_violation": "0.03",
                "theoretical_violation": "0.05",
            }
        ],
        snapshot_timestamp=NOW.isoformat(),
        markets=[
            _market("child", "child-yes"),
            _market("parent", "parent-yes"),
        ],
        orderbooks=[
            _book("child", "child-yes", 0.60, 0.62),
            _book("parent", "parent-yes", 0.40, 0.42),
        ],
        settings=Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.10),
    )

    row = candidates[0]

    assert row["status"] == RV_STATUS_PAPER_ELIGIBLE
    assert row["package_entry_cost"] == "-0.18"
    assert row["paper_trade_eligible"] == "true"


def _market(market_id: str, yes_token: str) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        event_id=market_id,
        category="sports_outright",
        title=market_id,
        outcomes=["Yes", "No"],
        token_ids=[yes_token, f"{yes_token}-no"],
    )


def _book(market_id: str, token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=NOW,
        bids=[PriceLevel(price=bid, size=20)],
        asks=[PriceLevel(price=ask, size=20)],
    )
