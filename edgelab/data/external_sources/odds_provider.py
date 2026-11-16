"""Sports odds provider interface, selection, diagnosis, and odds utilities."""

from __future__ import annotations

import json
import re
import math
import hashlib
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any, Mapping, Sequence

from edgelab.config.settings import Settings
from edgelab.data.cache import HttpGet
from edgelab.data.schemas import ExternalAnchor

SUPPORTED_ODDS_PROVIDERS = {"odds_api_io", "the_odds_api", "disabled", "auto"}
SENSITIVE_PARAM_NAMES = {"apikey", "api_key", "key", "token", "access_token", "authorization"}


@dataclass(frozen=True)
class ProviderCredentials:
    provider: str
    api_key: str | None
    key_source: str | None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


@dataclass(frozen=True)
class ProviderDiagnosis:
    selected_provider: str
    base_url: str
    key_present: bool
    key_fingerprint: str
    key_source: str | None
    endpoint_path: str
    http_status: int | None
    provider_error_body: str | None
    auth_succeeded: bool
    sport_key_valid: bool | None
    odds_or_events_returned: bool
    returned_count: int
    quota: dict[str, str] = field(default_factory=dict)
    failure_reason: str | None = None
    recommended_next_action: str = ""

    @property
    def succeeded(self) -> bool:
        return (
            self.auth_succeeded
            and self.sport_key_valid is not False
            and self.failure_reason is None
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SportsOddsProvider(ABC):
    provider_name: str
    base_url: str

    @abstractmethod
    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        """Validate auth and basic sport support with a small request."""

    @abstractmethod
    def list_sports(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Return provider sport metadata when the provider supports it."""

    @abstractmethod
    def list_events(
        self,
        sport: str,
        limit: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Return provider event payloads."""

    @abstractmethod
    def fetch_odds(
        self,
        sport: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> list[ExternalAnchor]:
        """Return normalized sports odds anchors."""

    @abstractmethod
    def normalize_to_external_odds(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> list[ExternalAnchor]:
        """Normalize provider payloads into EdgeLab external anchors."""


class DisabledOddsProvider(SportsOddsProvider):
    provider_name = "disabled"
    base_url = "disabled"

    def diagnose(self, sport: str, limit: int = 3) -> ProviderDiagnosis:
        _ = sport, limit
        return ProviderDiagnosis(
            selected_provider="disabled",
            base_url=self.base_url,
            key_present=False,
            key_fingerprint="missing",
            key_source=None,
            endpoint_path="",
            http_status=None,
            provider_error_body=None,
            auth_succeeded=False,
            sport_key_valid=None,
            odds_or_events_returned=False,
            returned_count=0,
            failure_reason="key missing",
            recommended_next_action="Set ODDS_PROVIDER and the matching provider-specific key.",
        )

    def list_sports(self, refresh: bool = False) -> list[dict[str, Any]]:
        _ = refresh
        return []

    def list_events(
        self,
        sport: str,
        limit: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        _ = sport, limit, refresh
        return []

    def fetch_odds(
        self,
        sport: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> list[ExternalAnchor]:
        _ = sport, limit, refresh
        return []

    def normalize_to_external_odds(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> list[ExternalAnchor]:
        _ = payloads
        return []


def select_sports_odds_provider(
    settings: Settings,
    provider: str | None = None,
    http_get: HttpGet | None = None,
    refresh: bool = False,
) -> SportsOddsProvider:
    """Select a sports odds provider without crossing provider-specific keys."""

    selected = normalize_provider_name(provider or settings.odds_provider)
    if selected == "disabled":
        return DisabledOddsProvider()
    if selected == "auto":
        selected = infer_provider_from_settings(settings)
    if selected == "odds_api_io":
        from edgelab.data.external_sources.odds_api_io_provider import OddsAPIIOProvider

        return OddsAPIIOProvider(settings=settings, http_get=http_get, refresh=refresh)
    if selected == "the_odds_api":
        from edgelab.data.external_sources.the_odds_api_provider import TheOddsAPIProvider

        return TheOddsAPIProvider(settings=settings, http_get=http_get, refresh=refresh)
    raise ValueError(f"Unsupported ODDS_PROVIDER={selected!r}")


def normalize_provider_name(value: str | None) -> str:
    normalized = (value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "oddsapiio": "odds_api_io",
        "odds_api.io": "odds_api_io",
        "odds_api": "the_odds_api",
        "the_oddsapi": "the_odds_api",
        "the_odds": "the_odds_api",
        "none": "disabled",
        "off": "disabled",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_ODDS_PROVIDERS:
        raise ValueError(
            "ODDS_PROVIDER must be one of odds_api_io, the_odds_api, disabled, or auto"
        )
    return normalized


def infer_provider_from_settings(settings: Settings) -> str:
    """Infer only when configuration is unambiguous; otherwise fail closed."""

    has_io = bool(settings.odds_api_io_key)
    has_the = bool(settings.the_odds_api_key)
    has_fallback = bool(settings.odds_api_key)
    if has_io and not has_the:
        return "odds_api_io"
    if has_the and not has_io:
        return "the_odds_api"
    if has_io and has_the:
        raise ValueError(
            "ODDS_PROVIDER is required when both ODDS_API_IO_KEY and THE_ODDS_API_KEY are set"
        )
    if has_fallback:
        return "the_odds_api"
    raise ValueError("ODDS_PROVIDER is missing and no provider-specific odds key is configured")


def credentials_for_provider(settings: Settings, provider: str) -> ProviderCredentials:
    selected = normalize_provider_name(provider)
    if selected == "odds_api_io":
        if settings.odds_api_io_key:
            return ProviderCredentials(selected, settings.odds_api_io_key, "ODDS_API_IO_KEY")
        return ProviderCredentials(selected, settings.odds_api_key, "ODDS_API_KEY")
    if selected == "the_odds_api":
        if settings.the_odds_api_key:
            return ProviderCredentials(selected, settings.the_odds_api_key, "THE_ODDS_API_KEY")
        return ProviderCredentials(selected, settings.odds_api_key, "ODDS_API_KEY")
    return ProviderCredentials(selected, None, None)


def american_to_decimal(odds: int | float) -> float:
    value = float(odds)
    if not math.isfinite(value) or value == 0:
        raise ValueError("American odds cannot be zero")
    if value > 0:
        return 1.0 + value / 100.0
    return 1.0 + 100.0 / abs(value)


def fractional_to_decimal(value: str) -> float:
    try:
        fraction = Fraction(value)
        return validate_decimal_odds(1.0 + float(fraction))
    except ZeroDivisionError as exc:
        raise ValueError("Invalid fractional odds") from exc


def validate_decimal_odds(value: float) -> float:
    decimal = float(value)
    if not math.isfinite(decimal) or decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1")
    return decimal


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_timestamp(value: Any) -> datetime | None:
    from datetime import UTC

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        text = value.strip() if isinstance(value, str) else ""
        if isinstance(value, int | float) or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            if abs(numeric) > 10_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, UTC)
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def key_fingerprint(key: str | None) -> str:
    if not key:
        return "missing"
    return "sha256:" + hashlib.sha256(key.strip().encode()).hexdigest()[:12]


def redact_secret_text(value: Any, secrets: Sequence[str | None] = ()) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, sort_keys=True)
        except TypeError:
            value = str(value)
    text = value
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"((?:apiKey|api_key|key|token)=)[^&\s]+", r"\1***", text, flags=re.I)
    text = re.sub(r'("(?:apiKey|api_key|key|token)"\s*:\s*")[^"]+(")', r"\1***\2", text, flags=re.I)
    return text.replace("\n", " ").strip()[:1000]


def quota_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    output: dict[str, str] = {}
    for key, value in headers.items():
        lower = str(key).lower()
        if any(term in lower for term in ("quota", "remaining", "requests", "rate")):
            output[str(key)] = str(value)
    return output


def clean_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None and str(value).strip()}


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in sorted(params.items()):
        normalized = key.lower()
        if normalized in SENSITIVE_PARAM_NAMES or normalized.endswith("_key"):
            output[key] = "***"
        else:
            output[key] = value
    return output


def classify_provider_failure(
    *,
    provider: str,
    key_source: str | None,
    status_code: int | None,
    body: str | None,
    expected_shape_ok: bool,
    sport_key_valid: bool | None,
) -> str | None:
    if not key_source:
        return "key missing"
    if status_code is None:
        return "network failure"
    text = (body or "").lower()
    if status_code in {401, 403} and key_source == "ODDS_API_KEY":
        return "key sent to wrong provider"
    if status_code in {401, 403}:
        if any(term in text for term in ("subscription", "plan", "quota", "not subscribed")):
            return "subscription does not include endpoint"
        return "provider rejected key"
    if status_code in {400, 404, 422} and sport_key_valid is False:
        return "unsupported sport key"
    if status_code in {402, 429}:
        return "subscription does not include endpoint"
    if status_code == 404 and any(term in text for term in ("not found", "cannot", "endpoint")):
        return "wrong endpoint for provider"
    if status_code >= 400:
        return (
            "provider rejected key"
            if "invalid" in text and "key" in text
            else "unknown response shape"
        )
    if not expected_shape_ok:
        return "unknown response shape"
    _ = provider
    return None


def recommendation_for_failure(
    failure_reason: str | None,
    provider: str,
    key_source: str | None,
    sport: str,
) -> str:
    if failure_reason == "key missing":
        env_name = "ODDS_API_IO_KEY" if provider == "odds_api_io" else "THE_ODDS_API_KEY"
        return f"Set ODDS_PROVIDER={provider} and {env_name}; avoid relying on ODDS_API_KEY."
    if failure_reason == "key sent to wrong provider":
        return (
            "Set ODDS_PROVIDER to the vendor that issued the key and move the secret into "
            "the provider-specific ODDS_API_IO_KEY or THE_ODDS_API_KEY. Do not reuse "
            "ODDS_API_KEY for new runs."
        )
    if failure_reason == "provider rejected key":
        return (
            f"Verify the {key_source} value with {provider}; rotate it if it was pasted into chat."
        )
    if failure_reason == "wrong endpoint for provider":
        return f"Confirm ODDS_PROVIDER={provider} matches the account vendor and base URL."
    if failure_reason == "unsupported sport key":
        return f"Use a sport key supported by {provider}; current sport={sport!r}."
    if failure_reason == "subscription does not include endpoint":
        return (
            "Check the provider subscription plan, endpoint access, quota, and remaining requests."
        )
    if failure_reason == "network failure":
        return "Retry when network access is available; no fair value should be used from this run."
    if failure_reason == "unknown response shape":
        return "Inspect the redacted provider body and add a fixture-backed parser before trading."
    return "Provider diagnosis passed; continue to live research collection."


def is_success_status(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def payload_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        for key in ("events", "data", "results", "bookmakers"):
            value = payload.get(key)
            if isinstance(value, list | dict):
                return len(value)
    return 0


__all__ = [
    "DisabledOddsProvider",
    "ProviderCredentials",
    "ProviderDiagnosis",
    "SportsOddsProvider",
    "american_to_decimal",
    "clean_params",
    "classify_provider_failure",
    "credentials_for_provider",
    "fractional_to_decimal",
    "infer_provider_from_settings",
    "key_fingerprint",
    "normalize_provider_name",
    "parse_timestamp",
    "payload_count",
    "quota_headers",
    "recommendation_for_failure",
    "redact_params",
    "redact_secret_text",
    "select_sports_odds_provider",
    "split_csv",
    "validate_decimal_odds",
]
