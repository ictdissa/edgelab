#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import FINAL_PAPER_CANDIDATE, enrich_signal_row

FUNNEL_COLUMNS = [
    "scope",
    "total_signals",
    "valid_data",
    "has_fair_value",
    "has_executable_bid_ask",
    "fresh_data",
    "liquidity_above_threshold",
    "spread_below_threshold",
    "positive_raw_edge",
    "positive_adjusted_edge",
    "edge_passes_threshold",
    "buy_sell_action",
    "risk_evaluated",
    "risk_approved",
    "paper_candidate",
    "biggest_rejection_reason",
    "biggest_rejection_count",
    "closest_score",
    "raw_edge_p10",
    "raw_edge_p25",
    "raw_edge_p50",
    "raw_edge_p75",
    "raw_edge_p90",
    "adjusted_edge_p10",
    "adjusted_edge_p25",
    "adjusted_edge_p50",
    "adjusted_edge_p75",
    "adjusted_edge_p90",
    "spread_p10",
    "spread_p25",
    "spread_p50",
    "spread_p75",
    "spread_p90",
    "liquidity_p10",
    "liquidity_p25",
    "liquidity_p50",
    "liquidity_p75",
    "liquidity_p90",
    "data_age_seconds_p10",
    "data_age_seconds_p25",
    "data_age_seconds_p50",
    "data_age_seconds_p75",
    "data_age_seconds_p90",
    "uncertainty_p10",
    "uncertainty_p25",
    "uncertainty_p50",
    "uncertainty_p75",
    "uncertainty_p90",
    "rejection_reasons",
]


@dataclass(frozen=True)
class FunnelStats:
    scope: str
    rows: list[dict[str, str]]
    total_signals: int
    valid_data: int
    has_fair_value: int
    has_executable_bid_ask: int
    fresh_data: int
    liquidity_above_threshold: int
    spread_below_threshold: int
    positive_raw_edge: int
    positive_adjusted_edge: int
    edge_passes_threshold: int
    buy_sell_action: int
    risk_evaluated: int
    risk_approved: int
    paper_candidate: int
    rejection_reasons: Counter[str]
    first_failure_reasons: Counter[str]
    distributions: dict[str, dict[str, float | None]]

    @property
    def biggest_rejection_reason(self) -> str:
        if self.first_failure_reasons:
            return self.first_failure_reasons.most_common(1)[0][0]
        if self.rejection_reasons:
            return self.rejection_reasons.most_common(1)[0][0]
        return ""

    @property
    def biggest_rejection_count(self) -> int:
        if self.first_failure_reasons:
            return self.first_failure_reasons.most_common(1)[0][1]
        if self.rejection_reasons:
            return self.rejection_reasons.most_common(1)[0][1]
        return 0

    @property
    def closest_score(self) -> float:
        if self.total_signals == 0:
            return 0.0
        weighted = (
            self.has_fair_value
            + self.valid_data
            + self.has_executable_bid_ask
            + self.fresh_data
            + self.liquidity_above_threshold
            + self.spread_below_threshold
            + self.positive_raw_edge
            + self.positive_adjusted_edge
            + self.edge_passes_threshold
            + 2 * self.buy_sell_action
            + 2 * self.risk_evaluated
            + 4 * self.risk_approved
            + 8 * self.paper_candidate
        )
        return weighted / (27 * self.total_signals)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose EdgeLab signal-to-paper funnel")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument(
        "--extra-snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional snapshot directories to include in funnel diagnostics.",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/signal_funnel.md"))
    parser.add_argument("--csv-output", type=Path)
    args = parser.parse_args()

    settings = Settings()
    snapshot_dirs = [args.snapshot_dir, *args.extra_snapshot_dir]
    rows = [enrich_signal_row(row, settings) for row in load_snapshot_signals_from_dirs(snapshot_dirs)]
    stats = build_all_stats(rows, settings)
    csv_output = args.csv_output or args.output.with_suffix(".csv")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, [stats_to_row(item) for item in stats], FUNNEL_COLUMNS)
    args.output.write_text(render_report(stats, settings, args.snapshot_dir, csv_output), encoding="utf-8")

    overall = next(item for item in stats if item.scope == "overall")
    print(f"signals_analyzed={overall.total_signals}")
    print(f"paper_candidates={overall.paper_candidate}")
    print(f"biggest_rejection_reason={overall.biggest_rejection_reason}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def load_snapshot_signals(snapshot_dir: Path) -> list[dict[str, str]]:
    return load_snapshot_signals_from_dirs([snapshot_dir])


def load_snapshot_signals_from_dirs(snapshot_dirs: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for snapshot_dir in snapshot_dirs:
        paths = signal_paths_from_index(snapshot_dir) or sorted(snapshot_dir.glob("iteration_*/signals.csv"))
        for path in paths:
            if not path.exists():
                continue
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
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


def build_all_stats(rows: list[dict[str, str]], settings: Settings) -> list[FunnelStats]:
    categories = sorted({row.get("category", "unknown") or "unknown" for row in rows})
    stats = [build_stats("overall", rows, settings)]
    for category in categories:
        category_rows = [row for row in rows if (row.get("category", "") or "unknown") == category]
        stats.append(build_stats(category, category_rows, settings))
    return stats


def build_stats(scope: str, rows: list[dict[str, str]], settings: Settings) -> FunnelStats:
    first_failure_reasons: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    for row in rows:
        accepted, reason = paper_candidate_rejection_reason(row, settings)
        if not accepted:
            first_failure_reasons[reason] += 1
        for rejection_reason in split_reasons(
            row.get("rejection_reason") or row.get("risk_reasons") or row.get("reason") or ""
        ):
            rejection_reasons[rejection_reason] += 1

    return FunnelStats(
        scope=scope,
        rows=rows,
        total_signals=len(rows),
        valid_data=sum(1 for row in rows if row.get("final_decision") != "DATA_INVALID"),
        has_fair_value=sum(1 for row in rows if has_fair_value(row)),
        has_executable_bid_ask=sum(1 for row in rows if has_executable_quote(row)),
        fresh_data=sum(1 for row in rows if is_fresh(row, settings)),
        liquidity_above_threshold=sum(1 for row in rows if has_liquidity(row, settings)),
        spread_below_threshold=sum(1 for row in rows if spread_is_acceptable(row, settings)),
        positive_raw_edge=sum(1 for row in rows if raw_edge(row) is not None and raw_edge(row) > 0),
        positive_adjusted_edge=sum(1 for row in rows if positive_adjusted_edge(row)),
        edge_passes_threshold=sum(1 for row in rows if row.get("edge_passes_threshold") == "true"),
        buy_sell_action=sum(1 for row in rows if row.get("best_side") in {"BUY_YES", "SELL_YES"}),
        risk_evaluated=sum(1 for row in rows if row.get("risk_decision") in {"APPROVED", "REJECTED"}),
        risk_approved=sum(1 for row in rows if is_risk_approved(row)),
        paper_candidate=sum(1 for row in rows if row.get("final_decision") == FINAL_PAPER_CANDIDATE),
        rejection_reasons=rejection_reasons,
        first_failure_reasons=first_failure_reasons,
        distributions=build_distributions(rows),
    )


def paper_candidate_rejection_reason(row: dict[str, str], settings: Settings) -> tuple[bool, str]:
    side = row.get("best_side", "")
    if side not in {"BUY_YES", "SELL_YES"}:
        return False, "no BUY_YES or SELL_YES best side"
    if row.get("edge_passes_threshold") != "true":
        return False, "edge does not pass threshold"
    if not is_risk_approved(row):
        return False, "risk_approved is false"
    adjusted_edge = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    if adjusted_edge is None or adjusted_edge < settings.min_adjusted_edge:
        return False, "adjusted edge below threshold"
    if not has_liquidity(row, settings):
        return False, "liquidity below threshold"
    if not spread_is_acceptable(row, settings):
        return False, "spread missing or above threshold"
    if not is_fresh(row, settings):
        return False, "data is not fresh"
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if side == "BUY_YES" and ask is None:
        return False, "BUY_YES missing executable ask"
    if side == "SELL_YES" and bid is None:
        return False, "SELL_YES missing executable bid"
    if row.get("final_decision") != FINAL_PAPER_CANDIDATE:
        return False, row.get("final_decision") or "not PAPER_CANDIDATE"
    return True, ""


def has_fair_value(row: dict[str, str]) -> bool:
    return parse_optional_float(row.get("fair_probability")) is not None


def has_executable_quote(row: dict[str, str]) -> bool:
    return (
        parse_optional_float(row.get("bid") or row.get("prediction_bid")) is not None
        or parse_optional_float(row.get("ask") or row.get("prediction_ask")) is not None
    )


def is_fresh(row: dict[str, str], settings: Settings) -> bool:
    age = parse_optional_float(row.get("data_age_seconds"))
    return age is not None and age <= settings.max_data_age_seconds


def has_liquidity(row: dict[str, str], settings: Settings) -> bool:
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    return liquidity is not None and liquidity >= settings.min_liquidity


def spread_is_acceptable(row: dict[str, str], settings: Settings) -> bool:
    spread = row_spread(row)
    return spread is not None and 0 < spread <= settings.max_spread


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


def positive_adjusted_edge(row: dict[str, str]) -> bool:
    value = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    return value is not None and value > 0


def row_spread(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("spread"))
    if explicit is not None:
        return explicit
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def build_distributions(rows: list[dict[str, str]]) -> dict[str, dict[str, float | None]]:
    values = {
        "raw_edge": [value for row in rows if (value := raw_edge(row)) is not None],
        "adjusted_edge": [
            value
            for row in rows
            if (value := parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge")))
            is not None
        ],
        "spread": [value for row in rows if (value := row_spread(row)) is not None],
        "liquidity": [
            value
            for row in rows
            if (value := parse_optional_float(row.get("liquidity") or row.get("available_size")))
            is not None
        ],
        "data_age_seconds": [
            value
            for row in rows
            if (value := parse_optional_float(row.get("data_age_seconds"))) is not None
        ],
        "uncertainty": [
            value
            for row in rows
            if (value := parse_optional_float(row.get("uncertainty") or row.get("model_uncertainty")))
            is not None
        ],
    }
    return {name: percentiles(series) for name, series in values.items()}


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
    ordered = sorted(values)
    return {
        "p10": percentile(ordered, 0.10),
        "p25": percentile(ordered, 0.25),
        "p50": median(ordered),
        "p75": percentile(ordered, 0.75),
        "p90": percentile(ordered, 0.90),
    }


def percentile(ordered: list[float], quantile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = quantile * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def stats_to_row(stats: FunnelStats) -> dict[str, str]:
    row = {
        "scope": stats.scope,
        "total_signals": str(stats.total_signals),
        "valid_data": str(stats.valid_data),
        "has_fair_value": str(stats.has_fair_value),
        "has_executable_bid_ask": str(stats.has_executable_bid_ask),
        "fresh_data": str(stats.fresh_data),
        "liquidity_above_threshold": str(stats.liquidity_above_threshold),
        "spread_below_threshold": str(stats.spread_below_threshold),
        "positive_raw_edge": str(stats.positive_raw_edge),
        "positive_adjusted_edge": str(stats.positive_adjusted_edge),
        "edge_passes_threshold": str(stats.edge_passes_threshold),
        "buy_sell_action": str(stats.buy_sell_action),
        "risk_evaluated": str(stats.risk_evaluated),
        "risk_approved": str(stats.risk_approved),
        "paper_candidate": str(stats.paper_candidate),
        "biggest_rejection_reason": stats.biggest_rejection_reason,
        "biggest_rejection_count": str(stats.biggest_rejection_count),
        "closest_score": format_float(stats.closest_score),
        "rejection_reasons": json.dumps(dict(stats.rejection_reasons.most_common(12)), sort_keys=True),
    }
    for name in (
        "raw_edge",
        "adjusted_edge",
        "spread",
        "liquidity",
        "data_age_seconds",
        "uncertainty",
    ):
        for suffix in ("p10", "p25", "p50", "p75", "p90"):
            row[f"{name}_{suffix}"] = format_float(stats.distributions[name][suffix])
    return row


def render_report(
    stats: list[FunnelStats],
    settings: Settings,
    snapshot_dir: Path,
    csv_output: Path,
) -> str:
    overall = next(item for item in stats if item.scope == "overall")
    categories = [item for item in stats if item.scope != "overall"]
    closest = max(categories, key=lambda item: item.closest_score, default=None)
    threshold_conclusion = threshold_assessment(overall)
    lines = [
        "# EdgeLab Signal Funnel Diagnostics",
        "",
        "This report diagnoses why research signals do or do not become strict paper-trading "
        "candidates. It does not change thresholds, place trades, or approve live trading.",
        "",
        "## Configuration",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- csv: `{csv_output}`",
        f"- min adjusted edge: `{settings.min_adjusted_edge}`",
        f"- min liquidity: `{settings.min_liquidity}`",
        f"- max spread: `{settings.max_spread}`",
        f"- max data age seconds: `{settings.max_data_age_seconds}`",
        "",
        "## Funnel Summary",
        "",
    ]
    lines.extend(
        markdown_table(
            [
                "scope",
                "signals",
                "valid",
                "fair",
                "quote",
                "fresh",
                "liquid",
                "spread ok",
                "raw edge+",
                "adj edge+",
                "threshold",
                "buy/sell",
                "risk eval",
                "risk ok",
                "candidates",
            ],
            [
                [
                    item.scope,
                    str(item.total_signals),
                    str(item.valid_data),
                    str(item.has_fair_value),
                    str(item.has_executable_bid_ask),
                    str(item.fresh_data),
                    str(item.liquidity_above_threshold),
                    str(item.spread_below_threshold),
                    str(item.positive_raw_edge),
                    str(item.positive_adjusted_edge),
                    str(item.edge_passes_threshold),
                    str(item.buy_sell_action),
                    str(item.risk_evaluated),
                    str(item.risk_approved),
                    str(item.paper_candidate),
                ]
                for item in stats
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Why Zero Paper Candidates",
            "",
            zero_candidate_explanation(overall),
            "",
            "## Binding Filters",
            "",
        ]
    )
    if overall.first_failure_reasons:
        lines.extend(
            markdown_table(
                ["first binding filter", "signals"],
                [
                    [reason, str(count)]
                    for reason, count in overall.first_failure_reasons.most_common(10)
                ],
            )
        )
    else:
        lines.append("No rejected signals were available.")
    lines.extend(["", "## Category Closest To Tradability", ""])
    if closest is None or closest.total_signals == 0:
        lines.append("No category had snapshot signals.")
    else:
        lines.append(
            f"`{closest.scope}` is closest by the diagnostic score "
            f"({closest.closest_score:.3f}). It has {closest.positive_adjusted_edge} "
            f"positive adjusted-edge rows and {closest.risk_approved} risk-approved rows."
        )
    lines.extend(["", "## Threshold Assessment", "", threshold_conclusion, ""])
    hidden = positive_edge_hidden_rows(overall.rows)
    lines.extend(
        [
            "## Positive Edge Hidden By HOLD/AVOID",
            "",
            (
                "No positive adjusted edges are silently hidden as HOLD/AVOID without risk reasons."
                if not hidden
                else f"{len(hidden)} positive adjusted-edge rows are labeled HOLD/AVOID without risk reasons."
            ),
            "",
        ]
    )
    lines.extend(["## Distributions", ""])
    lines.extend(render_distribution_section(overall))
    lines.extend(["", "## Recorded Rejection Reasons", ""])
    if overall.rejection_reasons:
        lines.extend(
            markdown_table(
                ["recorded reason", "signals"],
                [[reason, str(count)] for reason, count in overall.rejection_reasons.most_common(12)],
            )
        )
    else:
        lines.append("No explicit rejection reasons were recorded.")
    return "\n".join(lines)


def zero_candidate_explanation(overall: FunnelStats) -> str:
    if overall.paper_candidate > 0:
        return f"{overall.paper_candidate} paper candidates are present."
    if overall.total_signals == 0:
        return "No signals were found in the snapshot directory."
    if overall.buy_sell_action == 0:
        return (
            "There are zero paper candidates because no signal reached a strict BUY_YES or "
            "SELL_YES action. Research-only and AVOID/HOLD rows are intentionally blocked."
        )
    if overall.risk_approved == 0:
        return "There are zero paper candidates because no BUY_YES/SELL_YES signal passed risk."
    return (
        "There are zero paper candidates because surviving signals failed one or more execution "
        "gates such as edge, liquidity, spread, freshness, or executable side-specific quotes."
    )


def threshold_assessment(overall: FunnelStats) -> str:
    if overall.paper_candidate > 0:
        return "Configured thresholds are producing candidates; this report does not judge profitability."
    if overall.buy_sell_action == 0 or overall.risk_approved == 0:
        return (
            "The current starvation is not primarily evidence that thresholds are too strict. "
            "The action/risk gates bind before the edge, liquidity, spread, and freshness gates."
        )
    if overall.positive_adjusted_edge == 0:
        return "Signals look genuinely weak under the current fair-value model: adjusted edge is non-positive."
    return (
        "Some signals have positive adjusted edge, but execution-quality gates still block paper "
        "candidates. Threshold sensitivity should be treated as diagnostic only."
    )


def positive_edge_hidden_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if positive_adjusted_edge(row)
        and row.get("signal_action", row.get("action", "")) in {"HOLD", "AVOID"}
        and row.get("risk_reasons") in {"", "[]"}
    ]


def render_distribution_section(stats: FunnelStats) -> list[str]:
    rows = []
    for name, values in stats.distributions.items():
        rows.append(
            [
                name,
                format_float(values["p10"]),
                format_float(values["p25"]),
                format_float(values["p50"]),
                format_float(values["p75"]),
                format_float(values["p90"]),
            ]
        )
    return markdown_table(["metric", "p10", "p25", "p50", "p75", "p90"], rows)


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


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
