from __future__ import annotations

from datetime import UTC, datetime

from scripts.update_clv import TradeRow, render_live_clv_report


def test_clv_report_refuses_to_fabricate_without_follow_up_snapshot() -> None:
    trade = TradeRow(
        signal_id="sig1",
        market_id="m1",
        side="BUY_YES",
        price=0.42,
        timestamp=datetime.now(UTC),
    )
    report = render_live_clv_report(
        approved_trade_count=1,
        trades=[trade],
        observations=[],
        snapshot_dbs=[],
    )

    assert "insufficient follow-up data" in report
    assert "refuses to fabricate CLV" in report
    assert "Average CLV" not in report
