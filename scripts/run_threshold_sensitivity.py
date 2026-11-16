#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import FINAL_PAPER_CANDIDATE, enrich_signal_row

MIN_ADJUSTED_EDGES = [0.005, 0.01, 0.02, 0.03, 0.05]
MAX_SPREADS = [0.01, 0.02, 0.05, 0.10]
MIN_LIQUIDITIES = [1.0, 5.0, 10.0, 25.0, 50.0]
MAX_DATA_AGES = [30.0, 60.0, 300.0, 900.0]

SENSITIVITY_COLUMNS = [
    "min_adjusted_edge",
    "max_spread",
    "min_liquidity",
    "max_data_age_seconds",
    "candidates",
    "average_raw_edge",
    "average_adjusted_edge",
    "median_spread",
    "median_liquidity",
    "average_shadow_clv",
    "shadow_observations",
    "warning",
]


@dataclass(frozen=True)
class Gate:
    min_adjusted_edge: float
    max_spread: float
    min_liquidity: float
    max_data_age_seconds: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Run diagnostic threshold sensitivity")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/threshold_sensitivity.md"))
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--shadow-clv", type=Path, default=Path("reports/shadow_clv_observations.csv"))
    args = parser.parse_args()

    settings = Settings()
    rows = [enrich_signal_row(row, settings) for row in load_snapshot_signals(args.snapshot_dir)]
    shadow_rows = read_csv_rows(args.shadow_clv)
    output_rows = run_sensitivity(rows, shadow_rows)
    csv_output = args.csv_output or args.output.with_suffix(".csv")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, output_rows, SENSITIVITY_COLUMNS)
    args.output.write_text(
        render_report(rows, output_rows, args.snapshot_dir, csv_output, args.shadow_clv),
        encoding="utf-8",
    )

    max_candidates = max((int(row["candidates"]) for row in output_rows), default=0)
    print(f"signals_analyzed={len(rows)}")
    print(f"threshold_settings={len(output_rows)}")
    print(f"max_candidates={max_candidates}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def load_snapshot_signals(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    paths = signal_paths_from_index(snapshot_dir) or sorted(snapshot_dir.glob("iteration_*/signals.csv"))
    for path in paths:
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


def run_sensitivity(
    rows: list[dict[str, str]],
    shadow_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    output = []
    shadow_index = index_shadow_clv(shadow_rows)
    for min_edge in MIN_ADJUSTED_EDGES:
        for max_spread in MAX_SPREADS:
            for min_liquidity in MIN_LIQUIDITIES:
                for max_age in MAX_DATA_AGES:
                    gate = Gate(min_edge, max_spread, min_liquidity, max_age)
                    candidates = [row for row in rows if passes_gate(row, gate)]
                    shadow_values = candidate_shadow_values(candidates, shadow_index)
                    output.append(sensitivity_row(gate, candidates, shadow_values))
    return output


def passes_gate(row: dict[str, str], gate: Gate) -> bool:
    side = row.get("best_side", "")
    if side not in {"BUY_YES", "SELL_YES"}:
        return False
    if not is_risk_approved(row):
        return False
    adjusted_edge = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    if adjusted_edge is None or adjusted_edge < gate.min_adjusted_edge:
        return False
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < gate.min_liquidity:
        return False
    spread = row_spread(row)
    if spread is None or spread <= 0 or spread > gate.max_spread:
        return False
    age = parse_optional_float(row.get("data_age_seconds"))
    if age is None or age > gate.max_data_age_seconds:
        return False
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if side == "BUY_YES" and ask is None:
        return False
    if side == "SELL_YES" and bid is None:
        return False
    return row.get("final_decision") == FINAL_PAPER_CANDIDATE


def sensitivity_row(
    gate: Gate,
    candidates: list[dict[str, str]],
    shadow_values: list[float],
) -> dict[str, str]:
    raw_edges = [value for row in candidates if (value := raw_edge(row)) is not None]
    adjusted_edges = [
        value
        for row in candidates
        if (value := parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge")))
        is not None
    ]
    spreads = [value for row in candidates if (value := row_spread(row)) is not None]
    liquidity_values = [
        value
        for row in candidates
        if (value := parse_optional_float(row.get("liquidity") or row.get("available_size")))
        is not None
    ]
    warning = "no_candidates"
    if candidates:
        warning = "none; executable bid/ask required, midpoint fills excluded"
    return {
        "min_adjusted_edge": format_float(gate.min_adjusted_edge),
        "max_spread": format_float(gate.max_spread),
        "min_liquidity": format_float(gate.min_liquidity),
        "max_data_age_seconds": format_float(gate.max_data_age_seconds),
        "candidates": str(len(candidates)),
        "average_raw_edge": format_float(average(raw_edges)),
        "average_adjusted_edge": format_float(average(adjusted_edges)),
        "median_spread": format_float(median_or_none(spreads)),
        "median_liquidity": format_float(median_or_none(liquidity_values)),
        "average_shadow_clv": format_float(average(shadow_values)),
        "shadow_observations": str(len(shadow_values)),
        "warning": warning,
    }


def index_shadow_clv(rows: list[dict[str, str]]) -> dict[tuple[str, str, str], list[float]]:
    output: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        if row.get("status") != "observed":
            continue
        value = parse_optional_float(row.get("shadow_clv"))
        if value is None:
            continue
        key = (row.get("market_id", ""), row.get("outcome", ""), row.get("shadow_side", ""))
        output.setdefault(key, []).append(value)
    return output


def candidate_shadow_values(
    candidates: list[dict[str, str]],
    shadow_index: dict[tuple[str, str, str], list[float]],
) -> list[float]:
    values: list[float] = []
    for row in candidates:
        key = (row.get("market_id", ""), row.get("outcome", ""), row.get("best_side", ""))
        values.extend(shadow_index.get(key, []))
    return values


def render_report(
    rows: list[dict[str, str]],
    output_rows: list[dict[str, str]],
    snapshot_dir: Path,
    csv_output: Path,
    shadow_clv_path: Path,
) -> str:
    max_candidates = max((int(row["candidates"]) for row in output_rows), default=0)
    productive_rows = [row for row in output_rows if int(row["candidates"]) > 0]
    lines = [
        "# EdgeLab Threshold Sensitivity",
        "",
        "This is diagnostic only. It does not update production thresholds, force trades, or "
        "claim profitability.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- signals analyzed: {len(rows)}",
        f"- threshold settings tested: {len(output_rows)}",
        f"- maximum candidates under any setting: {max_candidates}",
        f"- csv: `{csv_output}`",
        f"- shadow CLV input: `{shadow_clv_path}`",
        "",
    ]
    if max_candidates == 0:
        lines.extend(
            [
                "No tested threshold setting produced candidates. Because action and risk gates remain "
                "in force, this is evidence that starvation is not solved by simply loosening edge, "
                "spread, liquidity, or freshness thresholds.",
                "",
            ]
        )
    else:
        lines.extend(["## Top Candidate-Producing Settings", ""])
        top_rows = sorted(productive_rows, key=lambda row: int(row["candidates"]), reverse=True)[:12]
        lines.extend(
            markdown_table(
                [
                    "min edge",
                    "max spread",
                    "min liq",
                    "max age",
                    "candidates",
                    "avg edge",
                    "median spread",
                    "median liq",
                    "avg shadow clv",
                    "warning",
                ],
                [
                    [
                        row["min_adjusted_edge"],
                        row["max_spread"],
                        row["min_liquidity"],
                        row["max_data_age_seconds"],
                        row["candidates"],
                        row["average_adjusted_edge"],
                        row["median_spread"],
                        row["median_liquidity"],
                        row["average_shadow_clv"],
                        row["warning"],
                    ]
                    for row in top_rows
                ],
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Threshold changes are not justified by this diagnostic alone. Any future threshold change "
            "needs enough snapshots, paper fills at bid/ask, and tradable CLV evidence.",
        ]
    )
    return "\n".join(lines)


def is_risk_approved(row: dict[str, str]) -> bool:
    return row.get("risk_decision") == "APPROVED" or row.get("risk_approved") in {
        "True",
        "true",
        "1",
    }


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


def row_spread(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("spread"))
    if explicit is not None:
        return explicit
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


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


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def format_float(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
