from __future__ import annotations

from edgelab.research.clv import CLVRecord, render_clv_report, summarize_clv


def test_clv_buy_and_sell_direction() -> None:
    records = [
        CLVRecord("buy", 0.40, 0.45, "BUY_YES"),
        CLVRecord("sell", 0.60, 0.55, "SELL_YES"),
    ]
    summary = summarize_clv(records)
    assert round(summary["average_clv"], 4) == 0.05


def test_empty_clv_report_does_not_claim_performance() -> None:
    report = render_clv_report([], approved_trade_count=0)
    assert "No CLV is reported yet" in report
    assert "avoids implying profitability" in report
