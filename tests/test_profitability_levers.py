from __future__ import annotations

from scripts.analyze_profitability_levers import build_group_rows, profitability_status


def test_profitability_levers_do_not_claim_profitability_on_small_sample() -> None:
    rows = [
        {
            "paper_trade_id": "pt1",
            "side": "BUY_YES",
            "horizon": "5m",
            "clv": "0.01",
            "spread_at_entry": "0.002",
            "adjusted_edge_at_entry": "0.03",
            "liquidity_at_entry": "50",
            "fair_value_source": "unit",
        }
    ]

    groups = build_group_rows(rows)
    overall = next(row for row in groups if row["group_type"] == "overall")

    assert overall["recommendation"] == "COLLECT_MORE"
    assert "No profitability claim" in profitability_status(1, 0.01)


def test_profitability_levers_block_non_positive_large_sample() -> None:
    rows = [
        {
            "paper_trade_id": f"pt{index}",
            "side": "SELL_YES",
            "horizon": "5m",
            "clv": "-0.001",
            "spread_at_entry": "0.002",
            "adjusted_edge_at_entry": "0.03",
            "liquidity_at_entry": "500",
            "fair_value_source": "unit",
        }
        for index in range(20)
    ]

    groups = build_group_rows(rows)
    overall = next(row for row in groups if row["group_type"] == "overall")

    assert overall["recommendation"] == "DO_NOT_SCALE"
    assert "not positive" in profitability_status(20, -0.001)
