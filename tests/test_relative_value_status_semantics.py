from __future__ import annotations

from scripts.generate_paper_signals import (
    RV_STATUS_INCOMPLETE_PACKAGE,
    RV_STATUS_PRICING_INCOMPLETE,
    build_relative_value_candidates,
)


def test_old_executable_source_status_is_downgraded_when_package_prices_missing() -> None:
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
        markets=[],
        orderbooks=[],
    )

    assert candidates[0]["source_status"] == "EXECUTABLE"
    assert candidates[0]["status"] in {RV_STATUS_PRICING_INCOMPLETE, RV_STATUS_INCOMPLETE_PACKAGE}
    assert candidates[0]["paper_trade_eligible"] == "false"
