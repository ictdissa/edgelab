from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Iterable


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def parse_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def parse_int(value: object) -> int:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else 0


def parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{100 * numerator / denominator:.1f}%"


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def trimmed_mean(values: list[float], trim: float = 0.1) -> float | None:
    if not values:
        return None
    if len(values) < 10:
        return average(values)
    ordered = sorted(values)
    low = int(len(ordered) * trim)
    high = len(ordered) - low
    trimmed = ordered[low:high] or ordered
    return average(trimmed)


def bootstrap_ci(values: list[float], iterations: int = 500, seed: int = 7) -> tuple[float | None, float | None]:
    if len(values) < 20:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [rng.choice(values) for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def markdown_table(headers: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


def default_experiment_dir() -> Path:
    candidates = sorted(Path("experiments").glob("falsification_campaign_*"))
    return candidates[-1] if candidates else Path("experiments/falsification_campaign_20260521")


def signal_paths(snapshot_dir: Path) -> list[Path]:
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


def warning_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        paths = []
        for row in read_csv_rows(index_path):
            value = row.get("edge_sanity_warnings_path", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() and not path.exists():
                path = snapshot_dir / path
            if path.exists():
                paths.append(path)
        if paths:
            return paths
    return sorted(snapshot_dir.glob("iteration_*/edge_sanity_warnings.csv"))


def relative_value_candidate_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        paths = []
        for row in read_csv_rows(index_path):
            value = row.get("relative_value_candidates_path", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() and not path.exists():
                path = snapshot_dir / path
            if path.exists():
                paths.append(path)
        if paths:
            return paths
    return sorted(snapshot_dir.glob("iteration_*/relative_value_candidates.csv"))


def load_snapshot_signals(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in signal_paths(snapshot_dir):
        iteration = path.parent.name
        for row in read_csv_rows(path):
            rows.append({**row, "_snapshot_path": str(path), "_iteration": iteration})
    return rows


def load_snapshot_warnings(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in warning_paths(snapshot_dir):
        iteration = path.parent.name
        for row in read_csv_rows(path):
            rows.append({**row, "_snapshot_path": str(path), "_iteration": iteration})
    return rows


def load_relative_value_candidates(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in relative_value_candidate_paths(snapshot_dir):
        iteration = path.parent.name
        for row in read_csv_rows(path):
            rows.append({**row, "_snapshot_path": str(path), "_iteration": iteration})
    return rows


def signal_lookup(signals: list[dict[str, str]]) -> dict[tuple[str, str, str], dict[str, str]]:
    lookup = {}
    for row in signals:
        key = (row.get("timestamp", "").replace("Z", "+00:00"), row.get("market_id", ""), row.get("outcome", ""))
        lookup[key] = row
        if row.get("signal_id"):
            lookup[(row["signal_id"], row.get("market_id", ""), row.get("outcome", ""))] = row
    return lookup


def warning_lookup(warnings: list[dict[str, str]]) -> dict[tuple[str, str], set[str]]:
    lookup: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in warnings:
        key = (row.get("market_id", ""), row.get("outcome", ""))
        if row.get("warning_code"):
            lookup[key].add(row["warning_code"])
    return lookup


def count_values(rows: Iterable[dict[str, str]], column: str) -> Counter[str]:
    return Counter(row.get(column, "") or "missing" for row in rows)


def row_price_fields(row: dict[str, str]) -> list[tuple[str, float | None]]:
    return [(name, parse_float(row.get(name))) for name in ("entry_price", "later_value", "later_bid", "later_ask", "later_mid")]


def split_reason(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return []
    if cleaned.startswith("["):
        try:
            decoded = json.loads(cleaned)
        except json.JSONDecodeError:
            decoded = []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if str(item)]
    return [part.strip() for part in cleaned.replace("|", ";").split(";") if part.strip()]


def numeric_median(values: list[float]) -> float | None:
    return median(values) if values else None
