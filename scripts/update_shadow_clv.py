#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

HORIZONS = {
    "5m": timedelta(minutes=5),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}

MIN_TIMESTAMP = datetime.min.replace(tzinfo=UTC)

OBSERVATION_COLUMNS = [
    "signal_id",
    "entry_kind",
    "category",
    "market_id",
    "outcome",
    "shadow_side",
    "horizon",
    "entry_timestamp",
    "observed_timestamp",
    "entry_value",
    "entry_price_source",
    "later_value",
    "later_value_source",
    "tradable_clv",
    "shadow_clv",
    "clv_type",
    "status",
    "source_snapshot",
    "research_only",
    "notes",
]


@dataclass(frozen=True)
class SnapshotSignal:
    signal_id: str
    timestamp: datetime | None
    category: str
    market_id: str
    outcome: str
    action: str
    fair_probability: float | None
    bid: float | None
    ask: float | None
    mid: float | None
    adjusted_edge: float | None
    source_snapshot: str
    side_hint: str = ""
    entry_price_override: float | None = None
    entry_price_source_override: str = ""
    entry_kind: str = "SNAPSHOT_SIGNAL"
    research_only: bool = True

    @property
    def later_reference(self) -> tuple[float | None, str]:
        if self.fair_probability is not None:
            return self.fair_probability, "fair_probability"
        if self.mid is not None:
            return self.mid, "mid"
        return None, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute research-only shadow CLV from snapshots")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--paper-candidates", type=Path, default=Path("reports/paper_candidates.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/shadow_clv_report.md"))
    parser.add_argument("--observations-output", type=Path)
    args = parser.parse_args()

    observations_output = args.observations_output or args.output.parent / "shadow_clv_observations.csv"
    snapshot_signals = load_snapshot_signals(args.snapshot_dir)
    tracked_signals = load_tracked_signals(snapshot_signals, args.watchlist, args.paper_candidates)
    observations = build_shadow_observations(tracked_signals, snapshot_signals)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    observations_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(observations_output, observations, OBSERVATION_COLUMNS)
    args.output.write_text(
        render_report(tracked_signals, observations, args.snapshot_dir, observations_output),
        encoding="utf-8",
    )

    shadow_values = observed_clv_values(observations, "shadow_clv")
    tradable_values = observed_clv_values(observations, "tradable_clv")
    print(f"signals_analyzed={len(tracked_signals)}")
    print(f"shadow_clv_observations={len(shadow_values)}")
    print(f"tradable_clv_observations={len(tradable_values)}")
    print(f"average_shadow_clv={format_float(average(shadow_values))}")
    print(f"report={args.output}")
    print(f"csv={observations_output}")


def load_snapshot_signals(snapshot_dir: Path) -> list[SnapshotSignal]:
    rows: list[SnapshotSignal] = []
    paths = signal_paths_from_index(snapshot_dir) or sorted(snapshot_dir.glob("iteration_*/signals.csv"))
    for path in paths:
        for row in read_csv_rows(path):
            signal = parse_signal(row, path)
            if signal is not None:
                rows.append(signal)
    return sorted(rows, key=lambda item: item.timestamp or MIN_TIMESTAMP)


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


def parse_signal(row: dict[str, str], path: Path) -> SnapshotSignal | None:
    market_id = row.get("market_id", "")
    if not market_id:
        return None
    timestamp = parse_timestamp(row.get("timestamp"))
    return SnapshotSignal(
        signal_id=row.get("signal_id", ""),
        timestamp=timestamp,
        category=row.get("category", ""),
        market_id=market_id,
        outcome=row.get("outcome", ""),
        action=row.get("action", ""),
        fair_probability=parse_optional_float(row.get("fair_probability")),
        bid=parse_optional_float(row.get("bid") or row.get("prediction_bid")),
        ask=parse_optional_float(row.get("ask") or row.get("prediction_ask")),
        mid=parse_optional_float(row.get("mid")),
        adjusted_edge=parse_optional_float(row.get("adjusted_edge")),
        source_snapshot=str(path),
    )


def load_tracked_signals(
    snapshot_signals: list[SnapshotSignal],
    watchlist_path: Path | None,
    paper_candidates_path: Path,
) -> list[SnapshotSignal]:
    tracked: list[SnapshotSignal] = []
    tracked.extend(load_paper_candidate_signals(paper_candidates_path))
    if watchlist_path is not None:
        tracked.extend(load_watchlist_signals(watchlist_path))
    return sorted(tracked or snapshot_signals, key=lambda item: item.timestamp or MIN_TIMESTAMP)


def load_watchlist_signals(path: Path) -> list[SnapshotSignal]:
    output: list[SnapshotSignal] = []
    for row in read_csv_rows(path):
        market_id = row.get("market_id", "")
        if not market_id:
            continue
        output.append(
            SnapshotSignal(
                signal_id=row.get("watchlist_id") or row.get("source_signal_id", ""),
                timestamp=parse_timestamp(row.get("timestamp")),
                category=row.get("category", ""),
                market_id=market_id,
                outcome=row.get("outcome", ""),
                action=row.get("action", ""),
                fair_probability=parse_optional_float(row.get("fair_probability")),
                bid=parse_optional_float(row.get("bid")),
                ask=parse_optional_float(row.get("ask")),
                mid=parse_optional_float(row.get("mid")),
                adjusted_edge=parse_optional_float(row.get("adjusted_edge")),
                source_snapshot=row.get("source_snapshot", ""),
                side_hint=row.get("side", ""),
                entry_kind=row.get("watchlist_type") or "RESEARCH_WATCHLIST",
                research_only=True,
            )
        )
    return output


def load_paper_candidate_signals(path: Path) -> list[SnapshotSignal]:
    output: list[SnapshotSignal] = []
    for row in read_csv_rows(path):
        market_id = row.get("market_id", "")
        side = row.get("side", "")
        if not market_id or side not in {"BUY_YES", "SELL_YES"}:
            continue
        output.append(
            SnapshotSignal(
                signal_id=row.get("candidate_id", ""),
                timestamp=parse_timestamp(row.get("timestamp")),
                category=row.get("category", ""),
                market_id=market_id,
                outcome=row.get("outcome", ""),
                action=side,
                fair_probability=parse_optional_float(row.get("fair_probability_at_entry")),
                bid=parse_optional_float(row.get("bid_at_entry")),
                ask=parse_optional_float(row.get("ask_at_entry")),
                mid=parse_optional_float(row.get("mid_at_entry")),
                adjusted_edge=parse_optional_float(row.get("adjusted_edge_at_entry")),
                source_snapshot=row.get("source_snapshot", ""),
                side_hint=side,
                entry_price_override=parse_optional_float(row.get("execution_price")),
                entry_price_source_override=row.get("execution_price_source", ""),
                entry_kind="PAPER_CANDIDATE",
                research_only=False,
            )
        )
    return output


def build_shadow_observations(
    signals: list[SnapshotSignal],
    follow_up_signals: list[SnapshotSignal] | None = None,
) -> list[dict[str, str]]:
    follow_up_signals = follow_up_signals or signals
    rows_by_contract: dict[tuple[str, str], list[SnapshotSignal]] = defaultdict(list)
    for signal in follow_up_signals:
        rows_by_contract[(signal.market_id, signal.outcome)].append(signal)
    for rows in rows_by_contract.values():
        rows.sort(key=lambda item: item.timestamp or MIN_TIMESTAMP)

    observations: list[dict[str, str]] = []
    for signal in signals:
        side = infer_shadow_side(signal)
        entry_value, entry_source = entry_reference(signal, side)
        contract_rows = rows_by_contract.get((signal.market_id, signal.outcome), [])
        for horizon, delta in HORIZONS.items():
            observations.append(
                observe_signal(signal, side, entry_value, entry_source, contract_rows, horizon, delta)
            )
    return observations


def infer_shadow_side(signal: SnapshotSignal) -> str:
    if signal.side_hint in {"BUY_YES", "SELL_YES"}:
        return signal.side_hint
    if signal.action in {"BUY_YES", "SELL_YES"}:
        return signal.action
    if signal.fair_probability is None:
        return ""
    buy_edge = signal.fair_probability - signal.ask if signal.ask is not None else None
    sell_edge = signal.bid - signal.fair_probability if signal.bid is not None else None
    candidates: list[tuple[str, float]] = []
    if buy_edge is not None:
        candidates.append(("BUY_YES", buy_edge))
    if sell_edge is not None:
        candidates.append(("SELL_YES", sell_edge))
    positive_candidates = [item for item in candidates if item[1] > 0]
    if positive_candidates:
        return max(positive_candidates, key=lambda item: item[1])[0]
    if signal.mid is not None:
        if signal.fair_probability > signal.mid:
            return "BUY_YES"
        if signal.fair_probability < signal.mid:
            return "SELL_YES"
    return ""


def entry_reference(signal: SnapshotSignal, side: str) -> tuple[float | None, str]:
    if signal.entry_price_override is not None:
        return signal.entry_price_override, signal.entry_price_source_override or "PAPER_CANDIDATE_PRICE"
    if side == "BUY_YES" and signal.ask is not None:
        return signal.ask, "ASK_OBSERVED_QUOTE"
    if side == "SELL_YES" and signal.bid is not None:
        return signal.bid, "BID_OBSERVED_QUOTE"
    if side in {"BUY_YES", "SELL_YES"} and signal.mid is not None:
        return signal.mid, "MID_RESEARCH_ONLY"
    return None, ""


def observe_signal(
    signal: SnapshotSignal,
    side: str,
    entry_value: float | None,
    entry_source: str,
    contract_rows: list[SnapshotSignal],
    horizon: str,
    delta: timedelta,
) -> dict[str, str]:
    base = base_observation_row(signal, side, horizon, entry_value, entry_source)
    if signal.timestamp is None:
        return base | {"status": "missing_entry_timestamp", "notes": "entry timestamp missing"}
    if not side:
        return base | {"status": "missing_shadow_side", "notes": "could not infer BUY_YES/SELL_YES"}
    if entry_value is None:
        return base | {"status": "missing_entry_reference", "notes": "no bid/ask or mid at entry"}
    target = signal.timestamp + delta
    follow_up = next(
        (row for row in contract_rows if row.timestamp is not None and row.timestamp >= target),
        None,
    )
    if follow_up is None:
        return base | {
            "status": "insufficient_followup_data",
            "notes": f"no later snapshot at or after {horizon}",
        }
    later_value, later_source = follow_up.later_reference
    if later_value is None:
        return base | {
            "observed_timestamp": follow_up.timestamp.isoformat(),
            "status": "missing_followup_reference",
            "notes": "follow-up row lacks fair probability and mid",
        }
    clv = later_value - entry_value if side == "BUY_YES" else entry_value - later_value
    clv_fields = (
        {"tradable_clv": format_float(clv), "shadow_clv": "", "clv_type": "TRADABLE_CANDIDATE_CLV"}
        if not signal.research_only
        else {"tradable_clv": "", "shadow_clv": format_float(clv), "clv_type": "SHADOW_RESEARCH_ONLY"}
    )
    return base | clv_fields | {
        "observed_timestamp": follow_up.timestamp.isoformat(),
        "later_value": format_float(later_value),
        "later_value_source": later_source,
        "status": "observed",
        "notes": "paper-candidate CLV" if not signal.research_only else "research-only shadow CLV; not a paper fill",
    }


def base_observation_row(
    signal: SnapshotSignal,
    side: str,
    horizon: str,
    entry_value: float | None,
    entry_source: str,
) -> dict[str, str]:
    return {
        "signal_id": signal.signal_id,
        "entry_kind": signal.entry_kind,
        "category": signal.category,
        "market_id": signal.market_id,
        "outcome": signal.outcome,
        "shadow_side": side,
        "horizon": horizon,
        "entry_timestamp": "" if signal.timestamp is None else signal.timestamp.isoformat(),
        "observed_timestamp": "",
        "entry_value": format_float(entry_value),
        "entry_price_source": entry_source,
        "later_value": "",
        "later_value_source": "",
        "tradable_clv": "",
        "shadow_clv": "",
        "clv_type": "SHADOW_RESEARCH_ONLY" if signal.research_only else "TRADABLE_CANDIDATE_CLV",
        "status": "",
        "source_snapshot": signal.source_snapshot,
        "research_only": "True" if signal.research_only else "False",
        "notes": "",
    }


def render_report(
    signals: list[SnapshotSignal],
    observations: list[dict[str, str]],
    snapshot_dir: Path,
    observations_output: Path,
) -> str:
    observed_values = observed_clv_values(observations, "shadow_clv")
    tradable_values = observed_clv_values(observations, "tradable_clv")
    by_category: dict[str, list[float]] = defaultdict(list)
    by_horizon: dict[str, list[float]] = defaultdict(list)
    for row in observations:
        if row.get("status") != "observed":
            continue
        value = parse_optional_float(row.get("shadow_clv"))
        if value is None:
            continue
        by_category[row.get("category", "unknown") or "unknown"].append(value)
        by_horizon[row.get("horizon", "")].append(value)
    status_counts = Counter(row.get("status", "") for row in observations)

    lines = [
        "# EdgeLab Shadow CLV Report",
        "",
        "Shadow CLV is a research-only signal-quality metric for signals that were not paper-filled. "
        "It must not be mixed with tradable CLV from paper or live fills.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- observations csv: `{observations_output}`",
        f"- signals analyzed: {len(signals)}",
        f"- shadow CLV attempts: {len(observations)}",
        f"- observed shadow CLV rows: {len(observed_values)}",
        f"- observed paper-candidate CLV rows: {len(tradable_values)}",
        f"- average shadow CLV: {format_float(average(observed_values)) or 'n/a'}",
        f"- median shadow CLV: {format_float(median(observed_values)) if observed_values else 'n/a'}",
        f"- average paper-candidate CLV: {format_float(average(tradable_values)) or 'n/a'}",
        "",
        "## Tradable CLV vs Shadow CLV",
        "",
        "- tradable CLV: measured from paper-ledger fills at executable bid or ask.",
        "- paper-candidate CLV in this file: candidate-level monitoring at bid/ask, not a ledger fill.",
        "- shadow CLV: research-only watchlist movement, sometimes using midpoint as a "
        "non-executable reference. It is not mixed with tradable CLV.",
        "",
    ]
    if len(observed_values) < 20:
        lines.extend(
            [
                "## Evidence Status",
                "",
                "Insufficient shadow evidence: fewer than 20 observed shadow CLV rows are available.",
                "",
            ]
        )
    lines.extend(["## Status Counts", ""])
    lines.extend(markdown_table(["status", "rows"], [[key, str(value)] for key, value in status_counts.items()]))
    lines.extend(["", "## Shadow CLV By Category", ""])
    lines.extend(render_grouped_clv(by_category))
    lines.extend(["", "## Shadow CLV By Horizon", ""])
    lines.extend(render_grouped_clv(by_horizon))
    return "\n".join(lines)


def observed_clv_values(observations: list[dict[str, str]], column: str = "shadow_clv") -> list[float]:
    return [
        value
        for row in observations
        if row.get("status") == "observed"
        for value in [parse_optional_float(row.get(column))]
        if value is not None
    ]


def render_grouped_clv(groups: dict[str, list[float]]) -> list[str]:
    if not groups:
        return ["No observed shadow CLV rows are available."]
    return markdown_table(
        ["group", "observations", "average", "median"],
        [
            [
                group,
                str(len(values)),
                format_float(average(values)),
                format_float(median(values)),
            ]
            for group, values in sorted(groups.items())
        ],
    )


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


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


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
