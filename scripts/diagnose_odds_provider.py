#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import (
    ProviderDiagnosis,
    key_fingerprint,
    normalize_provider_name,
    recommendation_for_failure,
    select_sports_odds_provider,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose sports odds provider auth/config")
    parser.add_argument(
        "--provider",
        choices=["auto", "odds_api_io", "the_odds_api", "disabled"],
        default="auto",
    )
    parser.add_argument("--sport", default="football")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--json-output", type=Path, default=Path("reports/odds_provider_diagnosis.json"))
    parser.add_argument("--md-output", type=Path, default=Path("reports/odds_provider_diagnosis.md"))
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    settings = Settings()
    diagnosis = diagnose(settings=settings, provider=args.provider, sport=args.sport, limit=args.limit)
    write_diagnosis_reports(diagnosis, args.md_output, args.json_output)
    print(render_console(diagnosis))


def diagnose(settings: Settings, provider: str, sport: str, limit: int) -> ProviderDiagnosis:
    selected = normalize_provider_name(provider)
    try:
        odds_provider = select_sports_odds_provider(settings, selected)
    except ValueError as exc:
        return _configuration_failure(settings, selected, str(exc), sport)
    return odds_provider.diagnose(sport=sport, limit=limit)


def write_diagnosis_reports(
    diagnosis: ProviderDiagnosis,
    md_output: Path,
    json_output: Path,
) -> None:
    md_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(diagnosis.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    md_output.write_text(render_markdown(diagnosis), encoding="utf-8")


def render_console(diagnosis: ProviderDiagnosis) -> str:
    status = "passed" if diagnosis.succeeded else "failed"
    reason = diagnosis.failure_reason or "none"
    return (
        f"Provider diagnosis {status}: provider={diagnosis.selected_provider}, "
        f"status={diagnosis.http_status}, reason={reason}. "
        f"Wrote reports/odds_provider_diagnosis.md"
    )


def render_markdown(diagnosis: ProviderDiagnosis) -> str:
    lines = [
        "# Odds Provider Diagnosis",
        "",
        f"- as_of: `{datetime.now(UTC).isoformat()}`",
        f"- selected provider: `{diagnosis.selected_provider}`",
        f"- base URL: `{diagnosis.base_url}`",
        f"- key present: `{diagnosis.key_present}`",
        f"- key source: `{diagnosis.key_source or ''}`",
        f"- key fingerprint: `{diagnosis.key_fingerprint}`",
        f"- endpoint path: `{diagnosis.endpoint_path}`",
        f"- HTTP status: `{'' if diagnosis.http_status is None else diagnosis.http_status}`",
        f"- auth succeeded: `{diagnosis.auth_succeeded}`",
        f"- sport key valid: `{diagnosis.sport_key_valid}`",
        f"- odds/events returned: `{diagnosis.odds_or_events_returned}`",
        f"- returned count: `{diagnosis.returned_count}`",
        f"- failure reason: `{diagnosis.failure_reason or ''}`",
        f"- recommended next action: {diagnosis.recommended_next_action}",
        "",
        "## Quota / Remaining Headers",
        "",
    ]
    if diagnosis.quota:
        for key, value in diagnosis.quota.items():
            lines.append(f"- `{key}`: `{value}`")
    else:
        lines.append("No quota or remaining-request headers were returned.")
    lines.extend(["", "## Provider Error Body", ""])
    lines.append(diagnosis.provider_error_body or "No provider error body.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            _interpretation(diagnosis),
        ]
    )
    return "\n".join(lines)


def _interpretation(diagnosis: ProviderDiagnosis) -> str:
    if diagnosis.succeeded:
        if diagnosis.odds_or_events_returned:
            return "Auth and sport-key checks passed and the provider returned at least one event/odds row."
        return "Auth passed, but no events/odds were returned for this sport and limit."
    if diagnosis.failure_reason == "key missing":
        return "No provider-specific key was available. The sports workflow must fail closed."
    if diagnosis.failure_reason == "key sent to wrong provider":
        return (
            "The fallback ODDS_API_KEY was rejected by the selected provider. This usually means "
            "the key belongs to the other similarly named odds vendor."
        )
    if diagnosis.failure_reason == "provider rejected key":
        return "The selected provider rejected the configured key. Do not use these odds as fair value."
    if diagnosis.failure_reason == "unsupported sport key":
        return "The key may be valid, but the requested sport key is not valid for this provider."
    if diagnosis.failure_reason == "subscription does not include endpoint":
        return "The key appears unable to access the endpoint or quota needed for this workflow."
    if diagnosis.failure_reason == "network failure":
        return "The request did not complete. Keep sports fair values disabled for this run."
    return "The provider response did not match a known success shape. Keep sports fair values disabled."


def _configuration_failure(settings: Settings, selected: str, error: str, sport: str) -> ProviderDiagnosis:
    any_key = bool(settings.odds_api_io_key or settings.the_odds_api_key or settings.odds_api_key)
    if settings.odds_api_io_key and settings.the_odds_api_key:
        fingerprint = "multiple provider-specific keys"
    else:
        key = settings.odds_api_io_key or settings.the_odds_api_key or settings.odds_api_key
        fingerprint = key_fingerprint(key)
    failure_reason = "key missing" if not any_key else "unknown response shape"
    return ProviderDiagnosis(
        selected_provider=selected,
        base_url="",
        key_present=any_key,
        key_fingerprint=fingerprint,
        key_source=None,
        endpoint_path="",
        http_status=None,
        provider_error_body=error,
        auth_succeeded=False,
        sport_key_valid=None,
        odds_or_events_returned=False,
        returned_count=0,
        failure_reason=failure_reason,
        recommended_next_action=recommendation_for_failure(failure_reason, selected, None, sport),
    )


if __name__ == "__main__":
    main()
