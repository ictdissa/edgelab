"""Cached, rate-limited HTTP utilities for research data adapters."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import math
import os
import tempfile
import fcntl
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

LOGGER = logging.getLogger(__name__)

HttpGet = Callable[..., Any]


class CachedJSONError(RuntimeError):
    status_code = 200


class CacheMissError(RuntimeError):
    """A cache-only research run must not spend a request on a missing response."""


class CachedHttpError(RuntimeError):
    def __init__(
        self, provider: str, status: int, body: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(f"{provider} HTTP {status}: {body}")
        self.status_code = status
        self.headers = headers or {}


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class RollingRateLimiter:
    """Persistent local rolling-window limiter.

    This is deliberately local and conservative. It prevents accidental rapid polling from
    scripts and tests; vendor-side limits still apply.
    """

    def __init__(self, state_path: Path, max_requests: int, window_seconds: int = 60) -> None:
        self.state_path = state_path
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        if max_requests < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")

    def reserve(self, now: float | None = None) -> RateLimitDecision:
        now = time.time() if now is None else now
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            timestamps = [ts for ts in self._read() if now - ts < self.window_seconds]
            if len(timestamps) >= self.max_requests:
                retry_after = self.window_seconds - (now - min(timestamps))
                return RateLimitDecision(False, 0, max(0.0, retry_after))
            timestamps.append(now)
            self._write(timestamps)
            return RateLimitDecision(True, self.max_requests - len(timestamps), 0.0)

    def _read(self) -> list[float]:
        if not self.state_path.exists():
            return []
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("Corrupt rate-limit state; requests blocked") from exc
        values = (
            payload.get("request_timestamps", payload.get("timestamps"))
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(values, list) or any(
            not isinstance(x, int | float) or not math.isfinite(x) for x in values
        ):
            raise RuntimeError("Invalid rate-limit state; requests blocked")
        return [float(value) for value in values]

    def status(self, now: float | None = None) -> RateLimitDecision:
        """Inspect the shared budget without reserving or resetting a request."""
        now = time.time() if now is None else now
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.with_suffix(self.state_path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            active = [ts for ts in self._read() if now - ts < self.window_seconds]
            remaining = max(0, self.max_requests - len(active))
            retry = (
                max(0.0, self.window_seconds - (now - min(active)))
                if active and not remaining
                else 0.0
            )
            return RateLimitDecision(remaining > 0, remaining, retry)

    def _write(self, timestamps: list[float]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_path, {"request_timestamps": timestamps})


@dataclass(frozen=True)
class CachedResponse:
    payload: Any
    cache_hit: bool
    cache_path: Path
    fetched_at: datetime = datetime.fromtimestamp(0, UTC)
    verified: bool = False
    headers: dict[str, str] = field(default_factory=dict)


class CachedHttpClient:
    def __init__(
        self,
        cache_dir: Path,
        provider: str,
        rate_limiter: RollingRateLimiter,
        http_get: HttpGet | None = None,
        cache_only: bool = False,
    ) -> None:
        self.cache_dir = cache_dir / provider
        self.provider = provider
        self.rate_limiter = rate_limiter
        self.http_get = http_get
        self.cache_only = cache_only

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        refresh: bool = False,
        timeout: float = 20.0,
    ) -> CachedResponse:
        if self.cache_only and refresh:
            raise ValueError("cache-only mode cannot refresh")
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and str(value).strip()
        }
        cache_path = self._cache_path(url, clean_params)
        if cache_path.exists() and not refresh:
            LOGGER.info("cache_hit provider=%s path=%s", self.provider, cache_path)
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("cache_schema") == 1:
                payload = stored["payload"]
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                if digest != stored.get("payload_sha256"):
                    raise RuntimeError("Cache integrity failure; refresh explicitly")
                if stored.get("parse_error"):
                    raise CachedJSONError(
                        f"{self.provider} invalid JSON response; cached, refresh explicitly"
                    )
                if stored.get("http_status", 200) >= 400:
                    raise CachedHttpError(
                        self.provider, stored["http_status"], str(payload), stored.get("quota", {})
                    )
                return CachedResponse(
                    payload,
                    True,
                    cache_path,
                    datetime.fromisoformat(stored["fetched_at"]),
                    True,
                    stored.get("quota", {}),
                )
            # Never relabel an old unversioned response as newly observed.
            return CachedResponse(stored, True, cache_path)
        if self.cache_only:
            raise CacheMissError(f"{self.provider} cache miss; network disabled")
        decision = self.rate_limiter.reserve()
        if not decision.allowed:
            raise RuntimeError(
                f"{self.provider} local rate limit exceeded; "
                f"retry_after={decision.retry_after_seconds:.1f}s"
            )
        response = self._http_get(url, params=clean_params, timeout=timeout)
        status_code = int(getattr(response, "status_code", 200))
        parse_error = False
        if status_code >= 400:
            payload = str(getattr(response, "text", ""))[:1000]
            for key, value in clean_params.items():
                if _is_sensitive_param(key) and str(value):
                    payload = payload.replace(str(value), "[REDACTED]")
        else:
            try:
                payload = response.json()
            except ValueError:
                parse_error = True
                payload = str(getattr(response, "text", ""))[:1000]
                for key, value in clean_params.items():
                    if _is_sensitive_param(key) and str(value):
                        payload = payload.replace(str(value), "[REDACTED]")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fetched_at = datetime.now(UTC)
        quota = {
            str(k).lower(): str(v)
            for k, v in dict(getattr(response, "headers", {})).items()
            if str(k).lower().startswith("x-requests-")
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        envelope = {
            "cache_schema": 1,
            "fetched_at": fetched_at.isoformat(),
            "payload_sha256": digest,
            "http_status": status_code,
            "payload": payload,
            "quota": quota,
            "parse_error": parse_error,
        }
        archive = (
            self.cache_dir / "archive" / f"{cache_path.stem}-{time.time_ns()}-{digest[:12]}.json"
        )
        atomic_json(archive, envelope)
        atomic_json(cache_path, envelope)
        if parse_error:
            raise CachedJSONError(
                f"{self.provider} invalid JSON response; cached, refresh explicitly"
            )
        if status_code >= 400:
            raise CachedHttpError(self.provider, status_code, payload, quota)
        LOGGER.info("cache_write provider=%s path=%s", self.provider, cache_path)
        return CachedResponse(payload, False, cache_path, fetched_at, True, quota)

    def _http_get(self, url: str, **kwargs: Any) -> Any:
        if self.http_get:
            return self.http_get(url, **kwargs)
        import httpx

        logging.getLogger("httpx").setLevel(logging.WARNING)
        return httpx.get(url, **kwargs)

    def _cache_path(self, url: str, params: Mapping[str, Any]) -> Path:
        safe_params = {
            key: "***" if _is_sensitive_param(key) else str(value)
            for key, value in sorted(params.items())
        }
        digest = hashlib.sha256(
            json.dumps({"url": url, "params": safe_params}, sort_keys=True).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"


def _is_sensitive_param(key: str) -> bool:
    normalized = key.lower()
    return normalized in {"apikey", "api_key", "key", "token", "access_token", "authorization"} or (
        normalized.endswith("_key") and normalized != "token_id"
    )


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
