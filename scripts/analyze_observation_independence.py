#!/usr/bin/env python
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    default_experiment_dir,
    load_snapshot_signals,
    markdown_table,
    read_csv_rows,
    write_csv,
)

TABLE_COLUMNS = ["metric", "group", "value", "interpretation"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze markout observation independence")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("reports/observation_independence_report.md"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/observation_independence_table.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, report = build_independence_report(args.experiment_dir, args.csv_output)
    write_csv(args.csv_output, rows, TABLE_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"raw_markout_rows={metric_value(rows, 'raw_markout_rows')}")
    print(f"unique_paper_fills={metric_value(rows, 'unique_paper_fills')}")
    print(f"unique_markets={metric_value(rows, 'unique_markets')}")
    print(f"report={args.output}")


def build_independence_report(experiment_dir: Path, csv_output: Path | None = None) -> tuple[list[dict[str, object]], str]:
    reports_dir = experiment_dir / "reports"
    snapshot_dir = experiment_dir / "snapshots"
    markouts = read_csv_rows(reports_dir / "markout_observations.csv")
    ledger = read_csv_rows(experiment_dir / "paper_trades" / "ledger.csv")
    signals = load_snapshot_signals(snapshot_dir)
    index = read_csv_rows(snapshot_dir / "snapshot_index.csv")
    observed = [row for row in markouts if row.get("status") == "observed"]
    signal_ids = {row.get("signal_id", "") for row in markouts if row.get("signal_id")}
    paper_fill_ids = {row.get("paper_trade_id", "") for row in ledger if row.get("paper_trade_id")}
    unique_markets = {row.get("market_id", "") for row in markouts if row.get("market_id")}
    unique_tokens = {row.get("token_id", "") for row in markouts if row.get("token_id")}
    if not unique_tokens:
        unique_tokens = {row.get("token_id", "") for row in signals if row.get("token_id")}
    snapshot_pairs = {
        (row.get("timestamp", ""), row.get("later_timestamp", ""))
        for row in observed
        if row.get("timestamp") and row.get("later_timestamp")
    }
    by_fill = Counter(row.get("signal_id", "") for row in observed if row.get("signal_id"))
    by_market = Counter(row.get("market_id", "") for row in observed if row.get("market_id"))
    by_token = Counter(row.get("token_id", "") for row in observed if row.get("token_id"))
    by_category = Counter(row.get("category", "") or "missing" for row in observed)
    by_source = Counter(row.get("fair_value_source", "") or "missing" for row in observed)
    by_horizon_fill: dict[str, set[str]] = defaultdict(set)
    for row in observed:
        if row.get("signal_id"):
            by_horizon_fill[row.get("horizon", "")].add(row["signal_id"])

    rows: list[dict[str, object]] = []
    add(rows, "raw_markout_rows", "overall", len(markouts), "All attempted markout rows, including missing follow-ups.")
    add(rows, "observed_markout_rows", "overall", len(observed), "Rows with conservative executable future bid/ask marks.")
    add(rows, "unique_experiment_runs", "overall", 1 if experiment_dir.exists() else 0, "One falsification campaign directory.")
    add(rows, "unique_snapshots", "overall", len(index) or len({row.get("_iteration", "") for row in signals}), "Snapshot iterations on disk.")
    add(rows, "unique_markets", "overall", len(unique_markets), "Distinct market IDs in markout rows.")
    add(rows, "unique_tokens", "overall", len([token for token in unique_tokens if token]), "Distinct token IDs when available.")
    add(rows, "unique_signal_rows", "overall", len(signal_ids), "Distinct signal IDs in markout rows.")
    add(rows, "unique_paper_fills", "overall", len(paper_fill_ids), "Distinct rows in the paper-fill ledger.")
    add(rows, "unique_snapshot_pairs", "overall", len(snapshot_pairs), "Distinct entry/follow-up timestamp pairs.")
    add(rows, "effective_sample_unique_fill", "overall", len(by_fill), "Conservative ESS by signal/fill ID.")
    add(rows, "effective_sample_market", "overall", len(by_market), "Conservative ESS by market.")
    add(rows, "effective_sample_token", "overall", len(by_token), "Conservative ESS by token; zero means token IDs are unavailable.")
    add(rows, "effective_sample_snapshot_pair", "overall", len(snapshot_pairs), "Conservative ESS by snapshot pair.")
    add(rows, "effective_sample_category_source", "overall", len({(row.get("category", ""), row.get("fair_value_source", "")) for row in observed}), "Conservative ESS by category/source bucket.")
    for horizon, fills in sorted(by_horizon_fill.items()):
        add(rows, "unique_paper_fills_by_horizon", horizon, len(fills), "Same fill can appear at multiple horizons.")
    for market, count in by_market.most_common(10):
        add(rows, "observations_per_market", market, count, "Market concentration; high concentration reduces independent evidence.")
    for token, count in by_token.most_common(10):
        add(rows, "observations_per_token", token, count, "Token concentration; blank if token IDs are absent.")
    for category, count in by_category.most_common():
        add(rows, "concentration_by_category", category, count, "Observed rows by category.")
    for source, count in by_source.most_common():
        add(rows, "concentration_by_fair_value_source", source, count, "Observed rows by fair-value source.")
    report = render_report(rows, by_fill, by_market, by_category, by_source, csv_output)
    return rows, report


def add(rows: list[dict[str, object]], metric: str, group: str, value: object, interpretation: str) -> None:
    rows.append({"metric": metric, "group": group, "value": value, "interpretation": interpretation})


def metric_value(rows: list[dict[str, object]], metric: str) -> object:
    return next((row["value"] for row in rows if row["metric"] == metric and row["group"] == "overall"), "")


def render_report(
    rows: list[dict[str, object]],
    by_fill: Counter[str],
    by_market: Counter[str],
    by_category: Counter[str],
    by_source: Counter[str],
    csv_output: Path | None,
) -> str:
    overview = [row for row in rows if row["group"] == "overall"]
    repeated = Counter(by_fill.values())
    lines = [
        "# Observation Independence Report",
        "",
        "Repeated multi-horizon markouts are not independent evidence. This report reports raw rows and conservative effective sample-size proxies.",
        "",
        "## Overview",
        "",
    ]
    lines.extend(markdown_table(["metric", "value", "interpretation"], [[row["metric"], row["value"], row["interpretation"]] for row in overview]))
    lines.extend(["", "## Repeated Observations Per Fill", ""])
    lines.extend(markdown_table(["observations_per_fill", "fills"], repeated.most_common()))
    lines.extend(["", "## Top Market Concentration", ""])
    lines.extend(markdown_table(["market_id", "observations"], by_market.most_common(10)))
    lines.extend(["", "## Category Concentration", ""])
    lines.extend(markdown_table(["category", "observations"], by_category.most_common()))
    lines.extend(["", "## Fair-Value Source Concentration", ""])
    lines.extend(markdown_table(["fair_value_source", "observations"], by_source.most_common()))
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Use unique paper fills, markets, tokens, and snapshot pairs as conservative sample-size bounds. "
            "Do not present raw multi-horizon rows as independent observations.",
            "",
            f"- table csv: `{csv_output}`",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
