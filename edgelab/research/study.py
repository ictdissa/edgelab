"""Frozen, resumable prospective soccer research. No order-submission dependency.

Registration is locally hash-sealed, not externally timestamped. Cycle inputs
are committed before accounting; stable signal IDs recover interrupted cycles.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from edgelab.config.settings import Settings
from edgelab.data.cache import atomic_json
from edgelab.data.schemas import (
    ExternalAnchor,
    OrderBook,
    PredictionMarket,
    StrictModel,
    ensure_utc,
)
from edgelab.execution.paper_trader import PaperTrader, write_ledger
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.matching.event_resolver import resolve_events, write_event_match_audit
from edgelab.matching.settlement import FixtureSettlement, apply_settlement_registry
from edgelab.matching.risk_identity import bind_fixture_risk
from edgelab.research.experiment import (
    ResearchProtocol,
    canonical_hash,
    file_hash,
    seal_run,
    verify_run,
)
from edgelab.research.live_analysis import add_avoid_signals, rewrite_anchors_for_matches
from edgelab.research.pipeline import write_signals_csv
from edgelab.strategies.taker_edge import generate_signals

LOGGER = logging.getLogger(__name__)
Clock = Callable[[], datetime]


class StudyDesign(StrictModel):
    """Non-secret collection/selection settings absent from the execution allowlist."""

    purpose: Literal["data_quality_pilot", "held_out_evaluation"] = "data_quality_pilot"
    provider: Literal["odds_api_io", "synthetic"] = "odds_api_io"
    sport: Literal["football"] = "football"
    league: str = Field(min_length=1)
    league_slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    league_aliases: list[str] = Field(default_factory=list)
    bookmakers: list[str] = Field(min_length=1)
    max_events: int = Field(default=10, ge=1, le=30)
    event_page_size: int = Field(default=50, ge=1, le=5000)
    max_event_pages: int = Field(default=1, ge=1, le=10)
    event_window_hours: int = Field(default=72, ge=1, le=336)
    max_markets: int = Field(default=30, ge=1, le=100)
    interval_seconds: int = Field(default=60, ge=1, le=60)
    odds_refresh_seconds: int = Field(default=120, ge=60)
    min_match_score: float = Field(default=0.85, ge=0.8, le=1)
    minimum_pre_event_seconds: int = Field(default=600, ge=300)
    followup_lateness_seconds: Literal[60] = 60
    followup_seconds: Literal[3600] = 3600
    pilot_event_std: float | None = Field(default=None, gt=0, lt=1)
    costs_validated: Literal[False] = False
    cache_dir: Path = Path("data/raw/cache/edgelab")
    shared_odds_budget_state: Path = Path(".sharpedge_cache/odds_api_io_rate_limit.json")

    @field_validator("bookmakers", "league_aliases")
    @classmethod
    def distinct_names(cls, values: list[str]) -> list[str]:
        names = [v.strip() for v in values]
        if any(not v or "," in v for v in names) or len({v.casefold() for v in names}) != len(
            names
        ):
            raise ValueError("source/league names must be nonempty, distinct and comma-free")
        return names

    @model_validator(mode="after")
    def budget(self) -> StudyDesign:
        # Worst-case discovery pages + odds batches; failed requests consume budget too.
        upper_requests = (
            (self.max_event_pages + (self.max_events + 9) // 10) * 3600 / self.odds_refresh_seconds
        )
        if upper_requests > 80:
            raise ValueError(
                "planned odds polling exceeds 80/hour; retain headroom below hard 90 cap"
            )
        if self.purpose == "held_out_evaluation" and self.pilot_event_std is None:
            raise ValueError(
                "held-out evaluation requires an independent-event pilot variance plan"
            )
        if self.provider == "odds_api_io" and self.league_slug is None:
            raise ValueError("Odds-API.io studies require an explicit league_slug")
        return self


class EventDiscovery(StrictModel):
    status: Literal["complete", "capped", "failed", "reused", "synthetic"]
    complete: bool = False
    reason: str
    league_slug: str | None = None
    pages: int = Field(default=0, ge=0)
    returned_rows: int = Field(default=0, ge=0)
    unique_events: int = Field(default=0, ge=0)
    selected_events: int = Field(default=0, ge=0)
    duplicate_rows: int = Field(default=0, ge=0)


class StudySnapshot(StrictModel):
    started_at: datetime
    as_of: datetime
    markets: list[PredictionMarket] = Field(default_factory=list)
    anchors: list[ExternalAnchor] = Field(default_factory=list)
    books: list[OrderBook] = Field(default_factory=list)
    odds_events: list[dict[str, Any]] = Field(default_factory=list)
    scope_rejections: list[dict[str, str]] = Field(default_factory=list)
    requests: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    discovery_queries: list[dict[str, Any]] = Field(default_factory=list)
    event_discovery: EventDiscovery | None = None

    @field_validator("started_at", "as_of")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def chronology(self) -> StudySnapshot:
        if self.as_of < self.started_at:
            raise ValueError("collection completion precedes start")
        return self


class StudySource(Protocol):
    def collect(
        self,
        previous: StudySnapshot | None,
        started_at: datetime,
        directory: Path,
        refresh_odds: bool,
        entries_open: bool,
    ) -> StudySnapshot: ...


class SettlementPair(StrictModel):
    market_id: str = Field(min_length=1)
    external_event_id: str = Field(min_length=1)
    book: str = Field(min_length=1)
    prediction: FixtureSettlement
    external: FixtureSettlement


def frozen_settings(manifest: dict[str, Any], design: StudyDesign) -> Settings:
    """Explicit defaults + frozen fields; environment cannot change numerical settings."""
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    defaults.update(manifest["settings"])
    defaults.update(
        cache_dir=design.cache_dir,
        odds_api_io_rate_limit_state=design.shared_odds_budget_state,
        odds_api_io_max_requests_per_hour=90,
        odds_api_io_bookmakers=",".join(design.bookmakers),
        odds_provider_event_limit=design.max_events,
    )
    return Settings(_env_file=None, **defaults)


def register_study(
    directory: Path,
    protocol: ResearchProtocol,
    design: StudyDesign,
    settings: Settings,
    registry: list[dict[str, Any]],
    now: datetime | None = None,
) -> Path:
    now = ensure_utc(now or datetime.now(UTC))
    protocol = ResearchProtocol.model_validate(protocol.model_dump())
    design = StudyDesign.model_validate(design.model_dump())
    if protocol.evaluation_start is None or protocol.evaluation_end is None:
        raise ValueError("study requires fixed evaluation dates")
    if design.provider != "synthetic" and protocol.evaluation_start <= now:
        raise ValueError("prospective evaluation must start after registration")
    if protocol.leagues != [design.league] or protocol.bookmaker_sources != design.bookmakers:
        raise ValueError("protocol league/books must exactly equal the frozen study design")
    if design.purpose == "held_out_evaluation" and not registry:
        raise ValueError("held-out evaluation requires reviewed settlement pairs")
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("study requires a new empty directory")
    clean_registry = []
    for row in registry:
        pair = SettlementPair.model_validate(row)
        for spec in (pair.prediction, pair.external):
            if spec.reviewed_at > now:
                raise ValueError("cannot register a future settlement review")
        clean_registry.append(pair.model_dump(mode="json"))
    design = design.model_copy(
        update={
            "cache_dir": design.cache_dir.resolve(),
            "shared_odds_budget_state": design.shared_odds_budget_state.resolve(),
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "study_design.json", design.model_dump(mode="json"))
    atomic_json(directory / "settlement_registry.json", clean_registry)
    return seal_run(
        directory,
        protocol,
        settings,
        "synthetic_integration" if design.provider == "synthetic" else "prospective_registration",
        [directory / "study_design.json", directory / "settlement_registry.json"],
        now=now,
    )


def seal_payload(path: Path, payload: dict[str, Any]) -> str:
    digest = canonical_hash(payload)
    if path.exists():
        raise FileExistsError("cycle artifacts cannot be overwritten")
    atomic_json(path, {"payload": payload, "sha256": digest})
    return digest


def load_payload(path: Path) -> tuple[dict[str, Any], str]:
    envelope = json.loads(path.read_text())
    if canonical_hash(envelope["payload"]) != envelope["sha256"]:
        raise ValueError(f"cycle integrity failure: {path.name}")
    return envelope["payload"], envelope["sha256"]


class StudyRunner:
    """One bounded collection per invocation; no implicit daemon or paid refresh."""

    def __init__(self, directory: Path, clock: Clock | None = None) -> None:
        self.directory = directory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.manifest = verify_run(directory)
        self.design = StudyDesign.model_validate_json((directory / "study_design.json").read_text())
        self.protocol = ResearchProtocol.model_validate(self.manifest["protocol"])
        self.settings = frozen_settings(self.manifest, self.design)
        assert self.protocol.evaluation_start and self.protocol.evaluation_end
        self.start = self.protocol.evaluation_start
        self.end = self.protocol.evaluation_end
        self.final_at = self.end + timedelta(
            seconds=self.design.followup_seconds + self.design.followup_lateness_seconds
        )

    def history(self) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        records: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        previous = self.manifest["manifest_sha256"]
        last_time = self.start - timedelta(seconds=1)
        last_slot = -1
        for index, path in enumerate(sorted((self.directory / "cycles").glob("*/snapshot.json"))):
            if path.parent.name != f"{index:06d}":
                raise ValueError("non-contiguous cycle journal")
            payload, digest = load_payload(path)
            snapshot = StudySnapshot.model_validate(payload["snapshot"])
            if (
                payload["previous_sha256"] != previous
                or snapshot.as_of < last_time
                or payload["slot"] <= last_slot
            ):
                raise ValueError("invalid cycle chain or clock rollback")
            # Consumed cache envelopes are included in each input seal, not just mutable paths.
            for relative, expected in payload["raw_hashes"].items():
                raw = (path.parent / relative).resolve()
                if not raw.is_relative_to(path.parent.resolve()):
                    raise ValueError("raw archive path escapes cycle")
                if file_hash(raw) != expected:
                    raise ValueError("consumed raw response changed")
            result_path = path.with_name("result.json")
            result = None
            if result_path.exists():
                result, previous = load_payload(result_path)
                if result["input_sha256"] != digest:
                    raise ValueError("result does not belong to sealed input")
                for name, expected in result["derived_files"].items():
                    if (
                        name not in {"signals.csv", "event_match_audit.csv"}
                        or file_hash(path.with_name(name)) != expected
                    ):
                        raise ValueError("per-cycle CSV differs from sealed result")
            else:
                previous = digest
            if records and records[-1][1] is None:
                raise ValueError("uncommitted cycle before a later cycle")
            records.append((payload, result))
            last_time, last_slot = snapshot.as_of, payload["slot"]
        return records

    def run_once(self, source: StudySource) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Check again under lock; constructor verification alone is insufficient.
            verify_run(self.directory)
            history = self.history()
            now = ensure_utc(self.clock())
            if history and now < StudySnapshot.model_validate(history[-1][0]["snapshot"]).as_of:
                raise ValueError("wall clock moved backward; collection blocked")
            state = self.directory / "portfolio.sqlite"
            if history and any(r for _, r in history) and not state.exists():
                raise ValueError("portfolio state missing; cannot reset research exposure")
            trader = PaperTrader(self.settings, portfolio_path=state)
            try:
                self._check_accounting(trader, history)
                if history and history[-1][1] is None:
                    self._process(trader, len(history) - 1, history[-1][0])
                    self._export(trader, now)
                    return "recovered"
                if now < self.start:
                    self._export(trader, now)
                    return "waiting_for_start"
                if now > self.final_at:
                    self._export(trader, now)
                    return "complete"
                slot = int((now - self.start).total_seconds()) // self.design.interval_seconds
                if history and slot <= history[-1][0]["slot"]:
                    self._export(trader, now)
                    return "not_due"
                index = len(history)
                cycle_dir = self.directory / "cycles" / f"{index:06d}"
                cycle_dir.mkdir(parents=True, exist_ok=True)
                previous = (
                    StudySnapshot.model_validate(history[-1][0]["snapshot"]) if history else None
                )
                last_odds = next(
                    (
                        StudySnapshot.model_validate(p["snapshot"]).started_at
                        for p, _ in reversed(history)
                        if p["odds_due"]
                    ),
                    None,
                )
                odds_due = (
                    last_odds is None
                    or (now - last_odds).total_seconds() >= self.design.odds_refresh_seconds
                )
                entries_open = self.start <= now < self.end
                try:
                    snapshot = source.collect(previous, now, cycle_dir, odds_due, entries_open)
                except Exception as exc:
                    # Do not persist exception text: request exceptions may contain authentication URLs.
                    LOGGER.warning(
                        "study_collection_failed cycle=%s type=%s", index, type(exc).__name__
                    )
                    snapshot = StudySnapshot(
                        started_at=now,
                        as_of=ensure_utc(self.clock()),
                        requests=json.loads((cycle_dir / "request_audit.json").read_text())
                        if (cycle_dir / "request_audit.json").exists()
                        else [],
                        errors=[f"collection_failed:{type(exc).__name__}"],
                    )
                if snapshot.started_at != now or snapshot.as_of > ensure_utc(self.clock()):
                    raise ValueError("source returned unavailable future observations")
                raw_hashes = {
                    str(p.relative_to(cycle_dir)): file_hash(p)
                    for p in sorted((cycle_dir / "raw").glob("*.json"))
                }
                last_slot = history[-1][0]["slot"] if history else -1
                payload = {
                    "slot": slot,
                    "missed_slots_before": max(0, slot - last_slot - 1),
                    "odds_due": odds_due,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "previous_sha256": self._tail(history),
                    "raw_hashes": raw_hashes,
                }
                seal_payload(cycle_dir / "snapshot.json", payload)
                self._process(trader, index, payload)
                self._export(trader, snapshot.as_of)
                return "collected"
            finally:
                trader.portfolio.connection.close()

    def _tail(self, history: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> str:
        if not history:
            return str(self.manifest["manifest_sha256"])
        return canonical_hash(history[-1][1] or history[-1][0])

    def schedule_coverage(
        self,
        history: list[tuple[dict[str, Any], dict[str, Any] | None]],
        now: datetime,
    ) -> dict[str, Any]:
        """Count the frozen schedule through as-of, including never-collected tails."""
        until = min(ensure_utc(now), self.final_at)
        expected = max(
            0, int((until - self.start).total_seconds() // self.design.interval_seconds) + 1
        )
        slots = sorted({p["slot"] for p, _ in history if 0 <= p["slot"] < expected})
        entry_total = math.ceil(
            (self.end - self.start).total_seconds() / self.design.interval_seconds
        )
        entry_expected = min(expected, entry_total)
        pending = sum(r is None for p, r in history if p["slot"] < expected)
        return {
            "expected_slots": expected,
            "attempted_slots": len(slots),
            "completed_slots": len(slots) - pending,
            "pending_slots": pending,
            "missed_slots": expected - len(slots),
            "missed_initial_slots": slots[0] if slots else expected,
            "missed_internal_slots": sum(b - a - 1 for a, b in zip(slots, slots[1:])),
            "missed_tail_slots": expected - slots[-1] - 1 if slots else 0,
            "entry_expected_slots": entry_expected,
            "entry_attempted_slots": sum(s < entry_expected for s in slots),
            "entry_missed_slots": entry_expected - sum(s < entry_expected for s in slots),
            "schedule_complete": bool(expected) and expected == len(slots) and pending == 0,
            "attempted_fraction": len(slots) / expected if expected else None,
        }

    def _check_accounting(
        self,
        trader: PaperTrader,
        history: list[tuple[dict[str, Any], dict[str, Any] | None]],
    ) -> None:
        stored = {d.signal_id: d.model_dump(mode="json") for d in trader.portfolio.decisions()}
        known: set[str] = set()
        for _, result in history:
            if result is not None:
                for decision in result["decisions"]:
                    key = decision["signal_id"]
                    if stored.get(key) != decision:
                        raise ValueError(
                            "accounting journal conflicts with committed research cycle"
                        )
                    known.add(key)
        # An interrupted cycle may have committed a subset of its stable IDs.
        if history and history[-1][1] is None:
            prefix = f"{self.protocol.protocol_id}:{len(history) - 1}:"
            known.update(key for key in stored if key.startswith(prefix))
        if set(stored) != known:
            raise ValueError("unexpected accounting decisions outside research journal")

    def _process(self, trader: PaperTrader, index: int, payload: dict[str, Any]) -> None:
        snapshot = StudySnapshot.model_validate(payload["snapshot"])
        now = snapshot.as_of
        markets, anchors = apply_settlement_registry(
            snapshot.markets,
            snapshot.anchors,
            self.directory / "settlement_registry.json",
        )
        # Reject metadata that was not available yet. Never relabel it as newly observed.
        eligible = []
        input_exclusions = []
        for m in markets:
            receipt = m.raw_metadata.get("collected_at")
            try:
                available = ensure_utc(datetime.fromisoformat(str(receipt))) if receipt else None
            except ValueError:
                available = None
            if (
                available is None
                or available > now
                or m.raw_metadata.get("metadata_cache_verified") is False
            ):
                input_exclusions.append(
                    {
                        "kind": "market",
                        "id": m.market_id,
                        "reason": "missing_invalid_unverified_or_future_metadata_receipt",
                    }
                )
                continue
            eligible.append(m)
        anchors = [
            a
            for a in anchors
            if a.timestamp <= now and a.available_at is not None and a.available_at <= now
        ]
        unavailable_rows = len(markets) - len(eligible) + len(snapshot.anchors) - len(anchors)
        before_scope = len(anchors)
        registered_leagues = {
            v.strip().casefold() for v in [self.design.league, *self.design.league_aliases]
        }
        registered_books = {v.casefold() for v in self.design.bookmakers}
        anchors = [
            a
            for a in anchors
            if a.category == "sports_1x2"
            and a.source.casefold() in registered_books
            and (
                a.metadata.get("league_slug") == self.design.league_slug
                if a.metadata.get("league_slug")
                else str(a.metadata.get("league", "")).strip().casefold() in registered_leagues
            )
        ]
        books = [
            b
            for b in snapshot.books
            if b.timestamp <= now and b.available_at is not None and b.available_at <= now
        ]
        unavailable_rows += len(snapshot.books) - len(books)
        matches = resolve_events(
            eligible, anchors, min_score=self.design.min_match_score, as_of=now
        )
        eligible = bind_fixture_risk(eligible, matches, anchors)
        rewritten = rewrite_anchors_for_matches(anchors, matches)
        fairs = SportsOddsModel(self.settings, now).compute(eligible, rewritten, books)
        bbas = [b.best_bid_ask() for b in books]
        signals = generate_signals(eligible, fairs, bbas, self.settings, now)
        signals = add_avoid_signals(
            eligible, signals, fairs, bbas, matches, anchors, self.settings, now
        )
        market_by_id = {m.market_id: m for m in eligible}
        for number, signal in enumerate(signals):
            signal.signal_id = f"{self.protocol.protocol_id}:{index}:{number}"
            market = market_by_id[signal.market_id]
            reason = None
            if not self.start <= now < self.end:
                reason = "outside frozen entry window; follow-up only"
            elif (
                market.start_time is None
                or (market.start_time - now).total_seconds() < self.design.minimum_pre_event_seconds
            ):
                reason = "missing kickoff or outside frozen pre-event buffer"
            if reason:
                signal.action, signal.reason = "AVOID", reason
        cycle = self.directory / "cycles" / f"{index:06d}"
        write_signals_csv(cycle / "signals.csv", signals)
        write_event_match_audit(cycle / "event_match_audit.csv", matches)
        # Already committed IDs after a crash retain their original fills, not duplicate skips.
        stored = {d.signal_id: d for d in trader.portfolio.decisions()}
        decisions = []
        for signal in signals:
            decisions.append(
                stored.get(signal.signal_id) or trader.execute([signal], market_by_id, now)[0]
            )
        result = {
            "input_sha256": canonical_hash(payload),
            "signals": [s.model_dump(mode="json") for s in signals],
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "matches": [
                {name: getattr(m, name) for name in m.__dataclass_fields__} for m in matches
            ],
            "fair_values": [f.model_dump(mode="json") for f in fairs],
            "eligible_market_count": len(eligible),
            "future_or_missing_receipt_rows": unavailable_rows,
            "out_of_scope_anchor_rows": before_scope - len(anchors),
            "input_exclusions": input_exclusions,
            "derived_files": {
                name: file_hash(cycle / name) for name in ("signals.csv", "event_match_audit.csv")
            },
        }
        seal_payload(cycle / "result.json", result)

    def _export(self, trader: PaperTrader, now: datetime) -> None:
        from edgelab.research.study_report import write_study_report

        history = self.history()
        decisions = trader.portfolio.decisions()
        write_ledger(self.directory / "ledger.csv", decisions, append=False)
        write_study_report(self, history, decisions, now, trader)
        atomic_json(
            self.directory / "checkpoint.json",
            {
                "as_of": now.isoformat(),
                "cycle_count": len(history),
                "tail_sha256": self._tail(history),
                "final": now >= self.final_at,
                "schedule_coverage": self.schedule_coverage(history, now),
                "evidence_type": self.manifest["evidence_type"],
                "live_trading": False,
            },
        )
