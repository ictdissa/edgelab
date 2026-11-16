"""Data freshness diagnostics for live research runs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class FreshnessRecord:
    source: str
    rows: int
    latest_timestamp: datetime | None
    age_seconds: float | None
    status: str
    reason: str


def freshness_record(
    source: str,
    timestamps: list[datetime],
    rows: int,
    as_of: datetime,
    max_age_seconds: int,
    missing_reason: str = "no rows",
) -> FreshnessRecord:
    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    if not timestamps:
        return FreshnessRecord(source, rows, None, None, "missing", missing_reason)
    latest = max(
        timestamp.astimezone(UTC) if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
        for timestamp in timestamps
    )
    if latest > as_of:
        return FreshnessRecord(
            source, rows, latest, 0.0, "future_data", "timestamp is after run time"
        )
    age = (as_of - latest).total_seconds()
    normalized = [ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC) for ts in timestamps]
    stale_rows = sum((as_of - ts).total_seconds() > max_age_seconds for ts in normalized)
    if len(timestamps) < rows:
        return FreshnessRecord(
            source, rows, latest, age, "unverified", "some rows lack observation time"
        )
    if stale_rows and stale_rows < rows:
        return FreshnessRecord(
            source,
            rows,
            latest,
            age,
            "partial_stale",
            f"{stale_rows}/{rows} rows stale; latest does not validate entire source",
        )
    if age > max_age_seconds:
        return FreshnessRecord(
            source, rows, latest, age, "stale", "latest timestamp exceeds max age"
        )
    return FreshnessRecord(source, rows, latest, age, "fresh", "ok")


def write_freshness_report(path: Path, records: list[FreshnessRecord], as_of: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# EdgeLab Data Freshness Report", "", f"- as_of: `{as_of.isoformat()}`", ""]
    lines.extend(
        markdown_table(
            ["source", "rows", "latest_timestamp", "age_seconds", "status", "reason"],
            [
                [
                    record.source,
                    str(record.rows),
                    "" if record.latest_timestamp is None else record.latest_timestamp.isoformat(),
                    "" if record.age_seconds is None else f"{record.age_seconds:.0f}",
                    record.status,
                    record.reason,
                ]
                for record in records
            ],
        )
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source", "rows", "latest_timestamp", "age_seconds", "status", "reason"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "source": record.source,
                    "rows": record.rows,
                    "latest_timestamp": ""
                    if record.latest_timestamp is None
                    else record.latest_timestamp.isoformat(),
                    "age_seconds": ""
                    if record.age_seconds is None
                    else f"{record.age_seconds:.0f}",
                    "status": record.status,
                    "reason": record.reason,
                }
            )


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return output
