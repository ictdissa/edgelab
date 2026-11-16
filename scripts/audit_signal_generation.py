#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import enrich_signal_row
from scripts.run_edge_sanity_checks import build_edge_sanity_warnings, render_report as render_sanity_report

AUDIT_COLUMNS = [
    "signal_id",
    "source_snapshot",
    "category",
    "market_id",
    "outcome",
    "action",
    "signal_action",
    "best_side",
    "buy_edge_raw",
    "sell_edge_raw",
    "buy_edge_adjusted",
    "sell_edge_adjusted",
    "best_adjusted_edge",
    "edge_passes_threshold",
    "inferred_side",
    "reason",
    "fair_probability_present",
    "bid_present",
    "ask_present",
    "mid_present",
    "raw_edge",
    "adjusted_edge",
    "cost_drag",
    "spread",
    "liquidity",
    "uncertainty",
    "data_age_seconds",
    "freshness_status",
    "risk_decision",
    "risk_reasons",
    "final_decision",
    "rejection_reason",
    "side_specific_action_expected",
    "possible_action_bug",
    "paper_candidate",
    "blockers",
]

SUMMARY_COLUMNS = [
    "metric",
    "value",
]


@dataclass(frozen=True)
class AuditSummary:
    total_signals: int
    action_counts: Counter[str]
    reason_counts: Counter[str]
    fair_value_count: int
    executable_bid_ask_count: int
    positive_raw_edge_count: int
    positive_adjusted_edge_count: int
    within_threshold_counts: dict[str, int]
    blocked_missing_data: int
    blocked_action_logic: int
    blocked_risk_manager: int
    blocked_liquidity: int
    blocked_spread: int
    blocked_stale_data: int
    possible_action_bug_count: int
    best_raw_edge: float | None
    best_adjusted_edge: float | None


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit EdgeLab signal action generation")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/signal_generation_audit.md"))
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--sanity-output", type=Path, default=Path("reports/edge_sanity_report.md"))
    parser.add_argument("--sanity-csv-output", type=Path, default=Path("reports/edge_sanity_warnings.csv"))
    args = parser.parse_args()

    settings = Settings()
    rows = load_snapshot_signals(args.snapshot_dir)
    audit_rows = build_audit_rows(rows, settings)
    summary = summarize_audit(audit_rows, settings)
    csv_output = args.csv_output or args.output.with_suffix(".csv")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, audit_rows, AUDIT_COLUMNS)
    args.output.write_text(
        render_report(summary, audit_rows, settings, args.snapshot_dir, csv_output),
        encoding="utf-8",
    )
    sanity_warnings = build_edge_sanity_warnings(rows, settings)
    args.sanity_csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(
        args.sanity_csv_output,
        sanity_warnings,
        [
            "warning_id",
            "source_snapshot",
            "category",
            "market_id",
            "outcome",
            "warning_code",
            "warning",
            "blocks_risk_approval",
            "best_side",
            "best_adjusted_edge",
            "raw_edge",
            "bid",
            "ask",
            "spread",
            "fair_probability",
            "final_decision",
        ],
    )
    args.sanity_output.write_text(
        render_sanity_report(sanity_warnings, args.snapshot_dir, args.sanity_csv_output),
        encoding="utf-8",
    )

    print(f"total_signals={summary.total_signals}")
    print(f"positive_raw_edge={summary.positive_raw_edge_count}")
    print(f"positive_adjusted_edge={summary.positive_adjusted_edge_count}")
    print(f"possible_action_bugs={summary.possible_action_bug_count}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def load_snapshot_signals(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    paths = signal_paths_from_index(snapshot_dir) or sorted(snapshot_dir.glob("iteration_*/signals.csv"))
    for path in paths:
        if not path.exists():
            continue
        for row in read_csv_rows(path):
            row["_source_snapshot"] = str(path)
            rows.append(row)
    return rows


def signal_paths_from_index(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if not index_path.exists():
        return []
    paths: list[Path] = []
    for row in read_csv_rows(index_path):
        value = row.get("signals_path", "")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute() and not path.exists():
            path = snapshot_dir / path
        paths.append(path)
    return paths


def build_audit_rows(rows: list[dict[str, str]], settings: Settings) -> list[dict[str, str]]:
    return [audit_signal(row, settings) for row in rows]


def audit_signal(row: dict[str, str], settings: Settings) -> dict[str, str]:
    row = enrich_signal_row(row, settings)
    fair_probability = parse_optional_float(row.get("fair_probability"))
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    mid = parse_optional_float(row.get("mid"))
    raw = raw_edge(row)
    adjusted = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    cost_drag = raw - adjusted if raw is not None and adjusted is not None else None
    spread = row_spread(row)
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    uncertainty = parse_optional_float(row.get("uncertainty") or row.get("model_uncertainty"))
    freshness = freshness_status(row, settings)
    inferred_side = row.get("best_side", "")
    expected = row.get("edge_passes_threshold") == "true" and inferred_side in {"BUY_YES", "SELL_YES"}
    risk_reasons = row.get("risk_reasons", "")
    possible_bug = (
        expected
        and row.get("signal_action", row.get("action", "")) in {"HOLD", "AVOID"}
        and risk_reasons in {"", "[]"}
    )
    blockers = signal_blockers(row, settings)
    accepted, _reason = paper_candidate_rejection_reason(row, settings)
    reason = row.get("reason") or row.get("rejection_reason") or row.get("risk_reasons") or ""
    return {
        "signal_id": row.get("signal_id", ""),
        "source_snapshot": row.get("_source_snapshot", ""),
        "category": row.get("category", ""),
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "action": row.get("action", ""),
        "signal_action": row.get("signal_action", ""),
        "best_side": row.get("best_side", ""),
        "buy_edge_raw": row.get("buy_edge_raw", ""),
        "sell_edge_raw": row.get("sell_edge_raw", ""),
        "buy_edge_adjusted": row.get("buy_edge_adjusted", ""),
        "sell_edge_adjusted": row.get("sell_edge_adjusted", ""),
        "best_adjusted_edge": row.get("best_adjusted_edge", ""),
        "edge_passes_threshold": row.get("edge_passes_threshold", ""),
        "inferred_side": inferred_side,
        "reason": reason,
        "fair_probability_present": format_bool(fair_probability is not None),
        "bid_present": format_bool(bid is not None),
        "ask_present": format_bool(ask is not None),
        "mid_present": format_bool(mid is not None),
        "raw_edge": format_float(raw),
        "adjusted_edge": format_float(adjusted),
        "cost_drag": format_float(cost_drag),
        "spread": format_float(spread),
        "liquidity": format_float(liquidity),
        "uncertainty": format_float(uncertainty),
        "data_age_seconds": row.get("data_age_seconds", ""),
        "freshness_status": freshness,
        "risk_decision": row.get("risk_decision", ""),
        "risk_reasons": row.get("risk_reasons", ""),
        "final_decision": row.get("final_decision", ""),
        "rejection_reason": row.get("rejection_reason") or row.get("risk_reasons") or "",
        "side_specific_action_expected": format_bool(expected),
        "possible_action_bug": format_bool(possible_bug),
        "paper_candidate": format_bool(accepted),
        "blockers": "; ".join(blockers),
    }


def summarize_audit(rows: list[dict[str, str]], settings: Settings) -> AuditSummary:
    raw_values = [value for row in rows if (value := parse_optional_float(row.get("raw_edge"))) is not None]
    adjusted_values = [value for row in rows if (value := adjusted_value(row)) is not None]
    return AuditSummary(
        total_signals=len(rows),
        action_counts=Counter(row.get("action", "") or "UNKNOWN" for row in rows),
        reason_counts=Counter(
            reason
            for row in rows
            for reason in split_reasons(row.get("rejection_reason") or row.get("reason") or "")
        ),
        fair_value_count=sum(1 for row in rows if row.get("fair_probability_present") == "true"),
        executable_bid_ask_count=sum(
            1 for row in rows if row.get("bid_present") == "true" and row.get("ask_present") == "true"
        ),
        positive_raw_edge_count=sum(
            1
            for row in rows
            if parse_optional_float(row.get("raw_edge")) is not None
            and parse_optional_float(row.get("raw_edge")) > 0
        ),
        positive_adjusted_edge_count=sum(
            1 for row in rows if adjusted_value(row) is not None and adjusted_value(row) > 0
        ),
        within_threshold_counts=within_threshold_counts(rows, settings),
        blocked_missing_data=sum(1 for row in rows if "missing data" in row.get("blockers", "")),
        blocked_action_logic=sum(1 for row in rows if "action logic" in row.get("blockers", "")),
        blocked_risk_manager=sum(1 for row in rows if "risk manager" in row.get("blockers", "")),
        blocked_liquidity=sum(1 for row in rows if "liquidity" in row.get("blockers", "")),
        blocked_spread=sum(1 for row in rows if "spread" in row.get("blockers", "")),
        blocked_stale_data=sum(1 for row in rows if "stale data" in row.get("blockers", "")),
        possible_action_bug_count=sum(1 for row in rows if row.get("possible_action_bug") == "true"),
        best_raw_edge=max(raw_values) if raw_values else None,
        best_adjusted_edge=max(adjusted_values) if adjusted_values else None,
    )


def within_threshold_counts(rows: list[dict[str, str]], settings: Settings) -> dict[str, int]:
    deltas = {
        "within_0_5pct_of_threshold": 0.005,
        "within_1pct_of_threshold": 0.01,
        "within_2pct_of_threshold": 0.02,
        "within_5pct_of_threshold": 0.05,
    }
    output: dict[str, int] = {}
    for label, delta in deltas.items():
        output[label] = sum(
            1
            for row in rows
            if (adjusted := adjusted_value(row)) is not None
            and abs(adjusted - settings.min_adjusted_edge) <= delta
        )
    return output


def paper_candidate_rejection_reason(row: dict[str, str], settings: Settings) -> tuple[bool, str]:
    side = row.get("best_side") or row.get("action", "")
    if side not in {"BUY_YES", "SELL_YES"}:
        return False, "no BUY_YES or SELL_YES best side"
    if not is_risk_approved(row):
        return False, "risk_approved is false"
    adjusted_edge = adjusted_value(row)
    if adjusted_edge is None or adjusted_edge < settings.min_adjusted_edge:
        return False, "adjusted edge below threshold"
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < settings.min_liquidity:
        return False, "liquidity below threshold"
    spread = row_spread(row)
    if spread is None or spread <= 0 or spread > settings.max_spread:
        return False, "spread missing or above threshold"
    if not is_fresh(row, settings):
        return False, "data is not fresh"
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if side == "BUY_YES" and ask is None:
        return False, "BUY_YES missing executable ask"
    if side == "SELL_YES" and bid is None:
        return False, "SELL_YES missing executable bid"
    return True, ""


def side_specific_action_expected(row: dict[str, str], settings: Settings) -> bool:
    inferred_side = inferred_action_side(row)
    if inferred_side not in {"BUY_YES", "SELL_YES"}:
        return False
    adjusted = parse_optional_float(row.get("adjusted_edge"))
    if adjusted is None or adjusted < settings.min_adjusted_edge:
        return False
    if not is_fresh(row, settings):
        return False
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < settings.min_liquidity:
        return False
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if inferred_side == "BUY_YES":
        return ask is not None
    return bid is not None


def signal_blockers(row: dict[str, str], settings: Settings) -> list[str]:
    blockers: list[str] = []
    fair_probability = parse_optional_float(row.get("fair_probability"))
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if fair_probability is None or (bid is None and ask is None):
        blockers.append("missing data")
    if row.get("action") not in {"BUY_YES", "SELL_YES"}:
        blockers.append("action logic")
    if not is_risk_approved(row):
        blockers.append("risk manager")
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < settings.min_liquidity:
        blockers.append("liquidity")
    spread = row_spread(row)
    if spread is None or spread <= 0 or spread > settings.max_spread:
        blockers.append("spread")
    if not is_fresh(row, settings):
        blockers.append("stale data")
    return blockers


def inferred_action_side(row: dict[str, str]) -> str:
    fair_probability = parse_optional_float(row.get("fair_probability"))
    if fair_probability is None:
        return ""
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    candidates: list[tuple[str, float]] = []
    if ask is not None:
        candidates.append(("BUY_YES", fair_probability - ask))
    if bid is not None:
        candidates.append(("SELL_YES", bid - fair_probability))
    if not candidates:
        return ""
    side, edge = max(candidates, key=lambda item: item[1])
    return side if edge > 0 else ""


def raw_edge(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("raw_edge"))
    if explicit is not None:
        return explicit
    fair_probability = parse_optional_float(row.get("fair_probability"))
    if fair_probability is None:
        return None
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    candidates = []
    if ask is not None:
        candidates.append(fair_probability - ask)
    if bid is not None:
        candidates.append(bid - fair_probability)
    return max(candidates) if candidates else None


def adjusted_value(row: dict[str, str]) -> float | None:
    return parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))


def row_spread(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("spread"))
    if explicit is not None:
        return explicit
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def freshness_status(row: dict[str, str], settings: Settings) -> str:
    explicit = row.get("freshness_status", "")
    if explicit:
        return explicit
    age = parse_optional_float(row.get("data_age_seconds"))
    if age is None:
        return "unknown"
    return "fresh" if age <= settings.max_data_age_seconds else "stale"


def is_fresh(row: dict[str, str], settings: Settings) -> bool:
    return freshness_status(row, settings) == "fresh"


def is_risk_approved(row: dict[str, str]) -> bool:
    return row.get("risk_decision") == "APPROVED" or row.get("risk_approved") in {
        "True",
        "true",
        "1",
    }


def render_report(
    summary: AuditSummary,
    audit_rows: list[dict[str, str]],
    settings: Settings,
    snapshot_dir: Path,
    csv_output: Path,
) -> str:
    side_actionable_hold_avoid = [
        row
        for row in audit_rows
        if row.get("side_specific_action_expected") == "true"
        and row.get("signal_action") in {"HOLD", "AVOID"}
    ]
    lines = [
        "# EdgeLab Signal Generation Audit",
        "",
        "This audit diagnoses whether candidate starvation comes from missing edge, missing data, "
        "thresholds, action generation, or risk gating. It does not change trading behavior.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- csv: `{csv_output}`",
        f"- total signals: {summary.total_signals}",
        f"- signals with fair value: {summary.fair_value_count}",
        f"- signals with valid bid and ask: {summary.executable_bid_ask_count}",
        f"- positive raw-edge signals: {summary.positive_raw_edge_count}",
        f"- positive adjusted-edge signals: {summary.positive_adjusted_edge_count}",
        f"- best raw edge: {format_float(summary.best_raw_edge) or 'n/a'}",
        f"- best adjusted edge: {format_float(summary.best_adjusted_edge) or 'n/a'}",
        f"- side-actionable HOLD/AVOID rows with recorded risk state: {len(side_actionable_hold_avoid)}",
        f"- possible action bugs: {summary.possible_action_bug_count}",
        f"- min adjusted edge threshold: `{settings.min_adjusted_edge}`",
        "",
        "## Direct Answers",
        "",
        f"1. Positive raw-edge signals: {yes_no(summary.positive_raw_edge_count > 0)} "
        f"({summary.positive_raw_edge_count}).",
        f"2. Positive adjusted-edge signals: {yes_no(summary.positive_adjusted_edge_count > 0)} "
        f"({summary.positive_adjusted_edge_count}).",
        "3. HOLD/AVOID signals that look side-actionable by edge math: "
        f"{yes_no(bool(side_actionable_hold_avoid))} ({len(side_actionable_hold_avoid)}). "
        f"Silently hidden without risk reasons: {summary.possible_action_bug_count}.",
        f"4. Diagnosis: {diagnosis(summary)}",
        "",
        "## Action Counts",
        "",
    ]
    lines.extend(markdown_table(["action", "signals"], [[key, str(value)] for key, value in summary.action_counts.most_common()]))
    lines.extend(["", "## Blocked Counts", ""])
    lines.extend(
        markdown_table(
            ["blocker", "signals"],
            [
                ["missing data", str(summary.blocked_missing_data)],
                ["action logic", str(summary.blocked_action_logic)],
                ["risk manager", str(summary.blocked_risk_manager)],
                ["liquidity", str(summary.blocked_liquidity)],
                ["spread", str(summary.blocked_spread)],
                ["stale data", str(summary.blocked_stale_data)],
            ],
        )
    )
    lines.extend(["", "## Adjusted Edge Near Threshold", ""])
    lines.extend(
        markdown_table(
            ["window", "signals"],
            [[key, str(value)] for key, value in summary.within_threshold_counts.items()],
        )
    )
    lines.extend(["", "## Top Reasons", ""])
    if summary.reason_counts:
        lines.extend(
            markdown_table(
                ["reason", "signals"],
                [[key, str(value)] for key, value in summary.reason_counts.most_common(12)],
            )
        )
    else:
        lines.append("No explicit reasons were recorded.")
    lines.extend(["", "## Possible Action Bugs", ""])
    possible = [row for row in audit_rows if row.get("possible_action_bug") == "true"]
    if possible:
        lines.extend(
            markdown_table(
                ["market", "outcome", "action", "inferred", "raw", "adjusted", "blockers"],
                [
                    [
                        row["market_id"],
                        row["outcome"],
                        row["action"],
                        row["inferred_side"],
                        row["raw_edge"],
                        row["adjusted_edge"],
                        row["blockers"],
                    ]
                    for row in possible[:20]
                ],
            )
        )
    else:
        lines.append("No HOLD/AVOID rows met the side-specific actionability test.")
    return "\n".join(lines)


def diagnosis(summary: AuditSummary) -> str:
    if summary.total_signals == 0:
        return "no snapshot signals were available."
    if summary.positive_raw_edge_count == 0 and summary.positive_adjusted_edge_count == 0:
        return "candidate starvation is currently real no-edge evidence."
    if summary.possible_action_bug_count > 0:
        return (
            "candidate starvation is not pure no-edge evidence; side-specific edge math finds "
            "actionable-looking HOLD/AVOID rows, so action-generation and risk gates need review."
        )
    if summary.blocked_risk_manager > 0:
        return "candidate starvation is mainly risk-gating evidence, not a missing-edge result."
    if summary.blocked_missing_data > summary.total_signals / 2:
        return "candidate starvation is mainly missing-data evidence."
    return "candidate starvation appears driven by conservative thresholds and execution-quality gates."


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def split_reasons(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_float(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


def format_bool(value: bool) -> str:
    return "true" if value else "false"


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
