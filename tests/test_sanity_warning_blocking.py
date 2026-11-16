from __future__ import annotations

from edgelab.config.settings import Settings
from scripts.generate_paper_signals import leg_sanity_warnings


def test_missing_bid_ask_is_blocking_sanity_warning() -> None:
    warnings = leg_sanity_warnings(
        token_id="token",
        bba_present=True,
        bid=None,
        ask=None,
        spread=None,
        entry_price=None,
        size_available=20,
        age_seconds=1,
        settings=Settings(),
    )

    assert any(warning["code"] == "MISSING_BID_ASK" and warning["blocks_paper"] for warning in warnings)


def test_low_liquidity_warning_is_diagnostic_only() -> None:
    warnings = leg_sanity_warnings(
        token_id="token",
        bba_present=True,
        bid=0.40,
        ask=0.41,
        spread=0.01,
        entry_price=0.41,
        size_available=9,
        age_seconds=1,
        settings=Settings(MIN_LIQUIDITY=10),
    )

    assert any(warning["code"] == "LOW_LIQUIDITY_DIAGNOSTIC" and not warning["blocks_paper"] for warning in warnings)
