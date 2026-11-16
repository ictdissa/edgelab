"""Markdown research report rendering."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from edgelab.data.schemas import FairValue, PredictionMarket, Signal, TradeDecision
from edgelab.matching.event_resolver import EventMatch


def render_research_report(
    markets: list[PredictionMarket],
    matches: list[EventMatch],
    fair_values: list[FairValue],
    signals: list[Signal],
    trades: list[TradeDecision],
) -> str:
    category_counts = Counter(market.category for market in markets)
    rejection_counts = Counter(trade.reason for trade in trades if not trade.approved)
    lines = [
        "# EdgeLab Mock Research Analysis",
        "",
        "## Executive Summary",
        "",
        f"- Markets scanned: {len(markets)}",
        f"- Fair values generated: {len(fair_values)}",
        f"- Signals generated: {len(signals)}",
        f"- Paper trades entered: {sum(1 for trade in trades if trade.approved)}",
        f"- Skipped trades: {sum(1 for trade in trades if not trade.approved)}",
        f"- Generated at: {datetime.now(UTC).isoformat()}",
        "",
        "No live trading was attempted. This run is research/paper only.",
        "Mock data is deterministic plumbing data, not evidence of profitability.",
        "Historical synthetic sample replay as of "
        + (max(s.timestamp for s in signals).isoformat() if signals else "unknown")
        + ". These are not today's tradable events.",
        "No actionable edge found."
        if not any(t.approved for t in trades)
        else "Synthetic paper fills are not live-market evidence.",
        "Synthetic weather values use a demonstration distribution, not a calibrated rainfall forecast.",
        "",
        "## Data Coverage",
        "",
    ]
    lines.extend(
        markdown_table(["category", "count"], [[k, str(v)] for k, v in category_counts.items()])
    )
    lines.extend(["", "## Matching Diagnostics", ""])
    lines.extend(
        markdown_table(
            [
                "market",
                "decision",
                "score",
                "candidate_count",
                "alt_score",
                "rejection_reason",
                "matched_outcomes",
                "reasons",
            ],
            [
                [
                    m.title,
                    m.decision,
                    f"{m.score:.3f}",
                    str(m.candidate_count),
                    "" if m.best_alternative_score is None else f"{m.best_alternative_score:.3f}",
                    m.rejection_reason or "",
                    str(m.matched_outcomes),
                    "; ".join(m.reasons),
                ]
                for m in matches
            ],
        )
    )
    lines.extend(["", "A machine-readable audit is written to `reports/event_match_audit.csv`."])
    lines.extend(["", "## Edge Table", ""])
    if signals:
        lines.extend(
            markdown_table(
                [
                    "market",
                    "category",
                    "fair",
                    "bid",
                    "ask",
                    "raw_edge",
                    "cost_drag",
                    "adjusted",
                    "liquidity",
                    "freshness",
                    "data_age_s",
                    "fair_source",
                    "action",
                    "risk/reason",
                ],
                [
                    [
                        signal.market_id,
                        signal.category,
                        f"{signal.fair_probability:.3f}",
                        "" if signal.prediction_bid is None else f"{signal.prediction_bid:.3f}",
                        "" if signal.prediction_ask is None else f"{signal.prediction_ask:.3f}",
                        f"{signal.raw_edge:.3f}",
                        f"{signal.cost_drag:.3f}",
                        f"{signal.adjusted_edge:.3f}",
                        f"{signal.available_size:.1f}",
                        signal.freshness_status,
                        "" if signal.data_age_seconds is None else f"{signal.data_age_seconds:.0f}",
                        signal.fair_value_source,
                        signal.action,
                        signal.reason,
                    ]
                    for signal in signals
                ],
            )
        )
    else:
        lines.append("No edge signals generated.")
    lines.extend(["", "## Risk Rejections", ""])
    if rejection_counts:
        lines.extend(
            markdown_table(["reason", "count"], [[k, str(v)] for k, v in rejection_counts.items()])
        )
    else:
        lines.append("No skipped paper-trade decisions.")
    lines.extend(["", "## Paper Trades", ""])
    lines.extend(
        markdown_table(
            [
                "market",
                "side",
                "price",
                "size",
                "approved",
                "exec_source",
                "fair",
                "adjusted_edge",
                "reason",
            ],
            [
                [
                    trade.market_id,
                    trade.side,
                    "" if trade.price is None else f"{trade.price:.3f}",
                    f"{trade.size:.1f}",
                    str(trade.approved),
                    trade.execution_price_source,
                    "" if trade.fair_probability is None else f"{trade.fair_probability:.3f}",
                    "" if trade.adjusted_edge is None else f"{trade.adjusted_edge:.3f}",
                    trade.reason,
                ]
                for trade in trades
            ],
        )
    )
    lines.extend(
        [
            "",
            "## CLV Results",
            "",
            "No realized CLV is claimed in the mock research report. CLV requires approved paper "
            "trades plus later fair-value/market snapshots.",
            "",
            "## Cost Decomposition",
            "",
            "Cost drag equals slippage + fees + model uncertainty + resolution risk + safety margin. "
            "A signal is tradable only if raw edge remains positive after this drag and risk checks.",
            "",
            "## Failure Modes",
            "",
            "- Mock anchors are simplified and not evidence of profitability.",
            "- Partial outright fields are rejected rather than treated as exhaustive.",
            "- Apparent edges must survive bid/ask, fees, uncertainty, liquidity, and risk checks.",
            "",
            "## Evidence For Or Against Profitability",
            "",
            "This run is a plumbing and data-quality test. No live-money conclusion should be drawn.",
            "",
            "## Next Experiments",
            "",
            "- Review settlement-equivalent pre-match soccer 1X2 contracts and record coherent odds/books.",
            "- Calibrate uncertainty and execution costs using training events only.",
            "- Register a future evaluation window before held-out collection; distinguish net quote markouts from settled PnL.",
        ]
    )
    return "\n".join(lines)


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output
