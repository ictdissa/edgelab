#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

HORIZONS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
}

OBSERVATION_COLUMNS = [
    "signal_id",
    "timestamp",
    "market_id",
    "token_id",
    "outcome",
    "category",
    "side",
    "final_decision",
    "evidence_type",
    "fair_value_source",
    "horizon",
    "entry_price",
    "entry_price_source",
    "later_timestamp",
    "later_bid",
    "later_ask",
    "later_mid",
    "later_fair_probability",
    "later_value",
    "later_value_source",
    "markout",
    "markout_type",
    "shadow_value",
    "shadow_value_source",
    "shadow_markout",
    "status",
    "edge_bucket",
    "liquidity_bucket",
    "spread_bucket",
    "quote_age_bucket",
]

SUMMARY_COLUMNS = [
    "group_type",
    "group",
    "horizon",
    "observations",
    "average_markout",
    "median_markout",
    "win_rate",
]


@dataclass(frozen=True)
class SignalRow:
    row: dict[str, str]
    timestamp: datetime
    market_id: str
    outcome: str

    @property
    def mid_or_fair(self) -> tuple[float | None, str]:
        mid = parse_optional_float(self.row.get("mid"))
        if mid is not None:
            return mid, "mid"
        fair = parse_optional_float(self.row.get("fair_probability"))
        if fair is not None:
            return fair, "fair_probability"
        return None, ""

    def conservative_exit(self, side: str) -> tuple[float | None, str]:
        if side == "BUY_YES":
            return parse_optional_float(
                self.row.get("bid") or self.row.get("prediction_bid")
            ), "FUTURE_BID"
        if side == "SELL_YES":
            return parse_optional_float(
                self.row.get("ask") or self.row.get("prediction_ask")
            ), "FUTURE_ASK"
        return None, ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multi-horizon markout analysis for accepted and rejected signals"
    )
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument(
        "--extra-snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional snapshot directories for follow-up marks.",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/markout_report.md"))
    parser.add_argument(
        "--observations-output", type=Path, default=Path("reports/markout_observations.csv")
    )
    parser.add_argument("--summary-output", type=Path, default=Path("reports/markout_summary.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot_dirs = [args.snapshot_dir, *args.extra_snapshot_dir]
    signals = load_signals(snapshot_dirs)
    observations = build_markout_observations(signals)
    summary_rows = build_summary_rows(observations)
    args.observations_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.observations_output, observations, OBSERVATION_COLUMNS)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.summary_output, summary_rows, SUMMARY_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_report(
            signals, observations, summary_rows, args.observations_output, args.summary_output
        ),
        encoding="utf-8",
    )
    print(f"signals={len(signals)}")
    print(f"markout_observations={sum(1 for row in observations if row['status'] == 'observed')}")
    print(f"report={args.output}")


def load_signals(snapshot_dirs: list[Path]) -> list[SignalRow]:
    rows: list[SignalRow] = []
    seen_paths: set[Path] = set()
    for snapshot_dir in snapshot_dirs:
        for path in snapshot_signal_paths(snapshot_dir):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            for row in read_csv_rows(path):
                timestamp = parse_time(row.get("timestamp"))
                if timestamp is None or not row.get("market_id"):
                    continue
                rows.append(
                    SignalRow(
                        row={**row, "_source_snapshot": str(path)},
                        timestamp=timestamp,
                        market_id=row.get("market_id", ""),
                        outcome=row.get("outcome", ""),
                    )
                )
    return sorted(rows, key=lambda item: item.timestamp)


def snapshot_signal_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        paths = []
        for row in read_csv_rows(index_path):
            value = row.get("signals_path", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() and not path.exists():
                path = snapshot_dir / path
            if path.exists():
                paths.append(path)
        if paths:
            return paths
    return sorted(snapshot_dir.glob("iteration_*/signals.csv"))


def build_markout_observations(signals: list[SignalRow]) -> list[dict[str, str]]:
    by_key: dict[tuple[str, str], list[SignalRow]] = defaultdict(list)
    for signal in signals:
        by_key[(signal.market_id, signal.outcome)].append(signal)
        by_key[(signal.market_id, "")].append(signal)
    for rows in by_key.values():
        rows.sort(key=lambda item: item.timestamp)

    observations: list[dict[str, str]] = []
    for signal in signals:
        side = signal_side(signal.row)
        entry_price, entry_source = entry_reference(signal.row, side)
        evidence_type = classify_evidence_type(signal.row, side, entry_source)
        for horizon, delta in HORIZONS.items():
            observations.append(
                observe_signal(
                    signal, side, entry_price, entry_source, evidence_type, horizon, delta, by_key
                )
            )
    return observations


def observe_signal(
    signal: SignalRow,
    side: str,
    entry_price: float | None,
    entry_source: str,
    evidence_type: str,
    horizon: str,
    delta: timedelta,
    by_key: dict[tuple[str, str], list[SignalRow]],
) -> dict[str, str]:
    target = signal.timestamp + delta
    follow_up = first_follow_up(by_key.get((signal.market_id, signal.outcome), []), target)
    if (
        follow_up
        and signal.row.get("token_id")
        and follow_up.row.get("token_id") != signal.row["token_id"]
    ):
        follow_up = None
    later_value, later_source = follow_up.conservative_exit(side) if follow_up else (None, "")
    shadow_value, shadow_source = follow_up.mid_or_fair if follow_up else (None, "")
    markout = compute_markout(side, entry_price, later_value)
    shadow_markout = compute_markout(side, entry_price, shadow_value)
    status, markout_type = markout_status_and_type(
        side=side,
        entry_price=entry_price,
        entry_source=entry_source,
        follow_up=follow_up,
        markout=markout,
        shadow_markout=shadow_markout,
    )
    if status == "observed" and follow_up:
        depth = parse_optional_float(
            follow_up.row.get("book_bid_size" if side == "BUY_YES" else "book_ask_size")
        )
        size = parse_optional_float(signal.row.get("size") or signal.row.get("available_size"))
        if depth is None or size is None or depth < size:
            status, markout_type = "quote_observed", "quote_only_no_depth"
    return {
        "signal_id": signal.row.get("signal_id", ""),
        "timestamp": signal.timestamp.isoformat(),
        "market_id": signal.market_id,
        "token_id": signal.row.get("token_id", ""),
        "outcome": signal.outcome,
        "category": signal.row.get("category", ""),
        "side": side,
        "final_decision": signal.row.get("final_decision", ""),
        "evidence_type": evidence_type,
        "fair_value_source": signal.row.get("fair_value_source", ""),
        "horizon": horizon,
        "entry_price": format_optional(entry_price),
        "entry_price_source": entry_source,
        "later_timestamp": "" if follow_up is None else follow_up.timestamp.isoformat(),
        "later_bid": format_optional(
            None
            if follow_up is None
            else parse_optional_float(
                follow_up.row.get("bid") or follow_up.row.get("prediction_bid")
            )
        ),
        "later_ask": format_optional(
            None
            if follow_up is None
            else parse_optional_float(
                follow_up.row.get("ask") or follow_up.row.get("prediction_ask")
            )
        ),
        "later_mid": format_optional(
            None if follow_up is None else parse_optional_float(follow_up.row.get("mid"))
        ),
        "later_fair_probability": format_optional(
            None
            if follow_up is None
            else parse_optional_float(follow_up.row.get("fair_probability"))
        ),
        "later_value": format_optional(later_value),
        "later_value_source": later_source,
        "markout": format_optional(markout),
        "markout_type": markout_type,
        "shadow_value": format_optional(shadow_value),
        "shadow_value_source": shadow_source,
        "shadow_markout": format_optional(shadow_markout),
        "status": status,
        "edge_bucket": edge_bucket(
            parse_optional_float(
                signal.row.get("best_adjusted_edge") or signal.row.get("adjusted_edge")
            )
        ),
        "liquidity_bucket": liquidity_bucket(
            parse_optional_float(signal.row.get("liquidity") or signal.row.get("available_size"))
        ),
        "spread_bucket": spread_bucket(parse_optional_float(signal.row.get("spread"))),
        "quote_age_bucket": quote_age_bucket(
            parse_optional_float(signal.row.get("data_age_seconds"))
        ),
    }


def markout_status_and_type(
    *,
    side: str,
    entry_price: float | None,
    entry_source: str,
    follow_up: SignalRow | None,
    markout: float | None,
    shadow_markout: float | None,
) -> tuple[str, str]:
    if entry_price is None:
        return "missing_entry_price", "unknown"
    if follow_up is None:
        return "insufficient_followup_data", "unknown"
    if side in {"BUY_YES", "SELL_YES"} and entry_source in {"ASK", "BID"}:
        if markout is not None:
            return "observed", "tradable_bid_ask"
        if shadow_markout is not None:
            return "shadow_observed", "midpoint_shadow"
        return "missing_future_exit_price", "unknown"
    if shadow_markout is not None:
        return "shadow_observed", "midpoint_shadow"
    return "missing_future_value", "unknown"


def first_follow_up(rows: list[SignalRow], target: datetime) -> SignalRow | None:
    return next(
        (
            row
            for row in sorted(rows, key=lambda r: r.timestamp)
            if target <= row.timestamp <= target + timedelta(seconds=60)
            and target
            <= (parse_time(row.row.get("orderbook_timestamp")) or datetime.fromtimestamp(0, UTC))
            <= target + timedelta(seconds=60)
        ),
        None,
    )


def signal_side(row: dict[str, str]) -> str:
    best_side = row.get("best_side", "")
    if best_side in {"BUY_YES", "SELL_YES"}:
        return best_side
    action = row.get("signal_action") or row.get("action", "")
    return action if action in {"BUY_YES", "SELL_YES"} else "NONE"


def entry_reference(row: dict[str, str], side: str) -> tuple[float | None, str]:
    if side == "BUY_YES":
        return parse_optional_float(row.get("ask") or row.get("prediction_ask")), "ASK"
    if side == "SELL_YES":
        return parse_optional_float(row.get("bid") or row.get("prediction_bid")), "BID"
    mid = parse_optional_float(row.get("mid"))
    if mid is not None:
        return mid, "MID_RESEARCH_ONLY"
    return parse_optional_float(row.get("fair_probability")), "FAIR_RESEARCH_ONLY"


def classify_evidence_type(row: dict[str, str], side: str, entry_source: str) -> str:
    if (
        row.get("final_decision") == "PAPER_CANDIDATE"
        and side in {"BUY_YES", "SELL_YES"}
        and entry_source in {"ASK", "BID"}
    ):
        return "tradable_candidate"
    if side in {"BUY_YES", "SELL_YES"} and entry_source in {"ASK", "BID"}:
        return "rejected_directional"
    return "research_only"


def compute_markout(
    side: str, entry_price: float | None, later_value: float | None
) -> float | None:
    if entry_price is None or later_value is None:
        return None
    if side == "BUY_YES":
        return later_value - entry_price
    if side == "SELL_YES":
        return entry_price - later_value
    return later_value - entry_price


def build_summary_rows(observations: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in observations:
        if row.get("status") != "observed":
            continue
        value = parse_optional_float(row.get("markout"))
        if value is None:
            continue
        horizon = row.get("horizon", "")
        dimensions = {
            "overall": "all",
            "category": row.get("category", ""),
            "evidence_type": row.get("evidence_type", ""),
            "side": row.get("side", ""),
            "fair_value_source": row.get("fair_value_source", ""),
            "edge_bucket": row.get("edge_bucket", ""),
            "liquidity_bucket": row.get("liquidity_bucket", ""),
            "spread_bucket": row.get("spread_bucket", ""),
            "quote_age_bucket": row.get("quote_age_bucket", ""),
            "final_decision": row.get("final_decision", ""),
        }
        for group_type, group in dimensions.items():
            groups[(group_type, group or "unknown", horizon)].append(value)
    return [
        summarize_group(group_type, group, horizon, values)
        for (group_type, group, horizon), values in sorted(groups.items())
    ]


def summarize_group(
    group_type: str, group: str, horizon: str, values: list[float]
) -> dict[str, str]:
    return {
        "group_type": group_type,
        "group": group,
        "horizon": horizon,
        "observations": str(len(values)),
        "average_markout": format_optional(sum(values) / len(values) if values else None),
        "median_markout": format_optional(median(values) if values else None),
        "win_rate": format_optional(
            sum(1 for value in values if value > 0) / len(values) if values else None
        ),
    }


def render_report(
    signals: list[SignalRow],
    observations: list[dict[str, str]],
    summary_rows: list[dict[str, str]],
    observations_output: Path,
    summary_output: Path,
) -> str:
    observed = [row for row in observations if row["status"] == "observed"]
    lines = [
        "# EdgeLab Markout Analysis",
        "",
        "Multi-horizon markouts for accepted and rejected signals. Tradable candidates use bid/ask entry prices; research-only rows are labeled separately and must not be mixed with paper PnL.",
        "",
        f"- signals loaded: {len(signals)}",
        f"- observations attempted: {len(observations)}",
        f"- observations observed: {len(observed)}",
        f"- observations csv: `{observations_output}`",
        f"- summary csv: `{summary_output}`",
        "",
        "## Overall Summary",
        "",
    ]
    overall = [row for row in summary_rows if row["group_type"] == "overall"]
    if overall:
        lines.extend(
            markdown_table(
                SUMMARY_COLUMNS, [[row[column] for column in SUMMARY_COLUMNS] for row in overall]
            )
        )
    else:
        lines.append(
            "No markouts were observed. Collect longer-running snapshots before drawing conclusions."
        )
    lines.extend(
        [
            "",
            "## Research Questions",
            "",
            "- Do higher edge buckets produce better future markouts?",
            "- Does the edge survive by side, spread bucket, liquidity bucket, and quote age?",
            "- Are rejected directional signals actually worse than accepted candidates?",
        ]
    )
    return "\n".join(lines)


def edge_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0:
        return "<0"
    if value < 0.01:
        return "0-1%"
    if value < 0.03:
        return "1-3%"
    if value < 0.10:
        return "3-10%"
    return ">=10%"


def liquidity_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 10:
        return "<10"
    if value < 50:
        return "10-50"
    if value < 250:
        return "50-250"
    return ">=250"


def spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 0.01:
        return "<=1%"
    if value <= 0.03:
        return "1-3%"
    if value <= 0.08:
        return "3-8%"
    return ">8%"


def quote_age_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 60:
        return "<=60s"
    if value <= 300:
        return "60-300s"
    if value <= 900:
        return "300-900s"
    return ">900s"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


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


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
