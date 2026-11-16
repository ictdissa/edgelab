"""Frozen protocols and verifiable run artifacts; never a profitability certificate."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import zipfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from edgelab.config.settings import Settings
from edgelab.data.schemas import StrictModel, ensure_utc

ROOT = Path(__file__).resolve().parents[2]
SAFE_SETTINGS = (
    "enable_live_trading",
    "max_position_per_market",
    "max_position_per_event",
    "max_total_unresolved_exposure",
    "max_daily_loss",
    "max_trade_size",
    "min_liquidity",
    "max_spread",
    "min_adjusted_edge",
    "default_slippage_bps",
    "fee_bps",
    "model_uncertainty_bps",
    "resolution_risk_bps",
    "safety_margin_bps",
    "max_data_age_seconds",
    "paper_starting_cash",
    "max_orderbook_age_seconds",
    "max_odds_age_seconds",
    "max_cross_source_skew_seconds",
)


class ResearchProtocol(StrictModel):
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    hypothesis: str = Field(min_length=30)
    category: Literal["sports_1x2"] = "sports_1x2"
    leagues: list[str] = Field(default_factory=list)
    bookmaker_sources: list[str] = Field(default_factory=list)
    selection_rule: str = Field(min_length=30)
    contract_scope: Literal["pre_match_regulation_time_complete_1x2"]
    primary_horizon_seconds: Literal[300] = 300
    primary_estimand: Literal["equal_weight_event_net_quote_markout"]
    minimum_effect_size: float = Field(gt=0, lt=1)
    significance_level: float = Field(default=0.05, gt=0, lt=0.5)
    target_power: float = Field(default=0.8, gt=0.5, lt=1)
    evaluation_start: datetime | None = None
    evaluation_end: datetime | None = None
    exclusions: list[str] = Field(min_length=1)
    benchmark: str = Field(min_length=10)
    stopping_rule: str = Field(min_length=20)
    cost_policy: str = Field(min_length=20)
    uncertainty_policy: str = Field(min_length=20)

    @field_validator("evaluation_start", "evaluation_end")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def valid_window(self) -> ResearchProtocol:
        if (self.evaluation_start is None) != (self.evaluation_end is None):
            raise ValueError("both evaluation boundaries must be specified")
        if (
            self.evaluation_start
            and self.evaluation_end
            and self.evaluation_end <= self.evaluation_start
        ):
            raise ValueError("evaluation end must follow start")
        return self


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root: Path = ROOT) -> dict[str, str]:
    paths: set[Path] = set()
    for folder in ("edgelab", "sharpedge", "scripts", "tests"):
        paths.update((root / folder).rglob("*.py"))
    paths.update(
        p for name in ("pyproject.toml", "requirements.lock") if (p := root / name).exists()
    )
    return {str(p.relative_to(root)): file_hash(p) for p in sorted(paths)}


def settings_snapshot(settings: Settings) -> dict[str, Any]:
    """An allowlist, never BaseSettings.model_dump(): credentials are excluded."""
    return {name: getattr(settings, name) for name in SAFE_SETTINGS}


def independent_block_sample_size(
    pilot_std: float,
    effect: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Approximate normal-mean planning calculation, NOT proof of independence.

    The pilot standard deviation must refer to independent event means. A zero
    pilot variance is not permission to infer perfect power from a tiny sample.
    """
    if not all(math.isfinite(x) for x in (pilot_std, effect, alpha, power)):
        raise ValueError("power inputs must be finite")
    if pilot_std <= 0 or effect <= 0 or not 0 < alpha < 0.5 or not 0.5 < power < 1:
        raise ValueError("invalid power planning inputs")
    normal = NormalDist()
    return math.ceil(
        ((normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)) * pilot_std / effect) ** 2
    )


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or Path(relative).is_absolute():
        raise ValueError("artifact path escapes run directory")
    return path


def seal_run(
    directory: Path,
    protocol: ResearchProtocol,
    settings: Settings,
    evidence_type: Literal["synthetic_integration", "prospective_registration"],
    artifacts: list[Path],
    root: Path = ROOT,
    now: datetime | None = None,
) -> Path:
    now = ensure_utc(now or datetime.now(UTC))
    if evidence_type == "prospective_registration":
        if protocol.evaluation_start is None or protocol.evaluation_start <= now:
            raise ValueError("prospective registration requires an evaluation window in the future")
        if not protocol.leagues or not protocol.bookmaker_sources:
            raise ValueError("registration requires a fixed league and bookmaker universe")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    if path.exists():
        raise FileExistsError("a sealed manifest cannot be overwritten")
    hashes = source_hashes(root)
    archive = directory / "source_code.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as handle:
        for relative in hashes:
            handle.write(_safe_path(root, relative), arcname=relative)
    artifacts = [*artifacts, archive]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "evidence_type": evidence_type,
        "live_trading_enabled": False,
        "protocol": protocol.model_dump(mode="json"),
        "settings": settings_snapshot(settings),
        "source_hashes": hashes,
        "artifacts": {str(p.relative_to(directory)): file_hash(p) for p in artifacts},
        "runtime": {
            "python": platform.python_version(),
            **{name: version(name) for name in ("pydantic", "pandas", "numpy", "scipy", "httpx")},
        },
        "profitability_claim": False,
        "limitations": [
            "placeholder execution costs",
            "no validated live performance",
            "registration is local and hash-sealed, not an external timestamp authority",
        ],
    }
    manifest["manifest_sha256"] = canonical_hash(manifest)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True, allow_nan=False)
    return path


def verify_run(directory: Path, root: Path = ROOT, check_source: bool = True) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    digest = manifest.pop("manifest_sha256")
    if canonical_hash(manifest) != digest:
        raise ValueError("manifest integrity check failed")
    if (
        manifest.get("live_trading_enabled") is not False
        or manifest.get("profitability_claim") is not False
    ):
        raise ValueError("invalid research-only manifest")
    if set(manifest["settings"]) != set(SAFE_SETTINGS):
        raise ValueError("manifest settings are not the approved credential-free schema")
    protocol = ResearchProtocol.model_validate(manifest["protocol"])
    kind = manifest.get("evidence_type")
    if kind not in {"synthetic_integration", "prospective_registration"}:
        raise ValueError("unknown evidence type")
    if kind == "prospective_registration" and (
        protocol.evaluation_start is None
        or protocol.evaluation_start <= datetime.fromisoformat(manifest["created_at"])
        or not protocol.leagues
        or not protocol.bookmaker_sources
    ):
        raise ValueError("invalid prospective registration")
    Settings(_env_file=None, **manifest["settings"])
    for relative, expected in manifest["artifacts"].items():
        if file_hash(_safe_path(directory, relative)) != expected:
            raise ValueError(f"artifact changed: {relative}")
    with zipfile.ZipFile(directory / "source_code.zip") as handle:
        names = handle.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["source_hashes"]):
            raise ValueError("invalid source archive inventory")
        for relative, expected in manifest["source_hashes"].items():
            _safe_path(root, relative)
            if hashlib.sha256(handle.read(relative)).hexdigest() != expected:
                raise ValueError("archived source does not match manifest")
    if check_source and source_hashes(root) != manifest["source_hashes"]:
        raise ValueError("source tree changed; replay requires the recorded source version")
    manifest["manifest_sha256"] = digest
    return manifest
