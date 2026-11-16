from __future__ import annotations

from scripts.analyze_clv_robustness import build_robustness_rows, render_report


def test_clv_robustness_requires_minimum_sample() -> None:
    rows = [{"clv": "0.10", "side": "BUY_YES", "horizon": "5m", "fair_value_source": "crypto"}]

    robustness = build_robustness_rows(rows)
    overall = next(row for row in robustness if row["group_type"] == "overall")

    assert overall["verdict"] == "INSUFFICIENT_SAMPLE"


def test_clv_robustness_rejects_repeated_unidentified_sample() -> None:
    rows = [
        {"clv": "0.02", "side": "BUY_YES", "horizon": "5m", "fair_value_source": "crypto"}
        for _ in range(20)
    ]

    robustness = build_robustness_rows(rows)
    overall = next(row for row in robustness if row["group_type"] == "overall")

    assert overall["verdict"] == "INSUFFICIENT_SAMPLE"
    assert overall["bootstrap_ci_low"] == "n/a"
    assert overall["independent_blocks"] == "1"


def test_clv_robustness_report_does_not_claim_profitability_without_data(tmp_path) -> None:
    report = render_report([], [], tmp_path / "ledger.csv", tmp_path / "clv.csv", tmp_path / "robust.csv")

    assert "No profitability claim is possible" in report
    assert "does not change thresholds" in report
