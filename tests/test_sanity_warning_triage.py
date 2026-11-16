from __future__ import annotations

from scripts.triage_sanity_warnings import build_triage


def test_sanity_warning_triage_marks_suspicious_edges_as_diagnostic() -> None:
    rows = [
        {
            "warning_code": "SUSPICIOUS_ADJUSTED_EDGE",
            "warning": "adjusted_edge > 0.25",
            "blocks_risk_approval": "false",
            "category": "crypto",
            "market_id": "m1",
            "best_adjusted_edge": "0.92",
        }
    ]

    triage = build_triage(rows)

    assert triage[0]["severity"] == "LOW"
    assert triage[0]["blocking_count"] == "0"
