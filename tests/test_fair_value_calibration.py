from __future__ import annotations

from scripts.analyze_fair_value_calibration import build_calibration_rows, join_settlements


def test_fair_value_calibration_computes_brier_and_bias() -> None:
    signals = [
        {
            "market_id": "m1",
            "outcome": "Yes",
            "category": "crypto",
            "fair_value": "0.80",
            "mid": "0.70",
            "fair_value_source": "unit",
        }
    ]
    joined = join_settlements(signals, {("m1", "Yes"): 1.0})

    rows = build_calibration_rows(joined)
    overall = next(row for row in rows if row["group_type"] == "overall")

    assert overall["fair_brier_score"] == "0.04"
    assert overall["market_brier_score"] == "0.09"
    assert overall["bias"] == "-0.2"
    assert overall["status"] == "INSUFFICIENT_SETTLED_SAMPLE"
