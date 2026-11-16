"""Small persistent rolling-window rate limiter for API collectors."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0


class RollingWindowRateLimiter:
    """Persist request timestamps so separate script runs share one limit bucket."""

    def __init__(
        self,
        state_path: Path,
        max_requests: int,
        window_seconds: int = 3600,
        now: Callable[[], float] | None = None,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self.state_path = state_path
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._now = now or time.time

    def reserve(self, tokens: int = 1) -> RateLimitDecision:
        """Reserve request capacity or return a retry delay without sleeping."""

        if tokens < 1:
            raise ValueError("tokens must be >= 1")
        with _file_lock(self._lock_path):
            timestamps = self._pruned_timestamps()
            remaining = self.max_requests - len(timestamps)
            if tokens > remaining:
                retry_after = self._retry_after_seconds(timestamps, tokens)
                return RateLimitDecision(
                    allowed=False,
                    remaining=max(0, remaining),
                    retry_after_seconds=retry_after,
                )
            now = self._now()
            timestamps.extend([now] * tokens)
            self._write_timestamps(timestamps)
            return RateLimitDecision(
                allowed=True,
                remaining=max(0, self.max_requests - len(timestamps)),
            )

    def status(self) -> RateLimitDecision:
        """Return current remaining capacity without reserving a request."""

        with _file_lock(self._lock_path):
            timestamps = self._pruned_timestamps()
            remaining = self.max_requests - len(timestamps)
            retry_after = 0.0 if remaining > 0 else self._retry_after_seconds(timestamps, 1)
            return RateLimitDecision(
                allowed=remaining > 0,
                remaining=max(0, remaining),
                retry_after_seconds=retry_after,
            )

    @property
    def _lock_path(self) -> Path:
        return self.state_path.with_suffix(self.state_path.suffix + ".lock")

    def _pruned_timestamps(self) -> list[float]:
        now = self._now()
        cutoff = now - self.window_seconds
        timestamps = [value for value in self._read_timestamps() if value > cutoff]
        self._write_timestamps(timestamps)
        return timestamps

    def _read_timestamps(self) -> list[float]:
        if not self.state_path.exists():
            return []
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Corrupt shared rate-limit state; requests blocked") from exc
        values = payload.get("request_timestamps", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            raise RuntimeError("Invalid shared rate-limit state; requests blocked")
        timestamps: list[float] = []
        for value in values:
            try:
                timestamps.append(float(value))
            except (TypeError, ValueError):
                raise RuntimeError("Invalid rate-limit timestamp; requests blocked")
        return timestamps

    def _write_timestamps(self, timestamps: list[float]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        payload = {
            "window_seconds": self.window_seconds,
            "max_requests": self.max_requests,
            "request_timestamps": timestamps,
        }
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _retry_after_seconds(self, timestamps: list[float], tokens: int) -> float:
        if not timestamps:
            return float(self.window_seconds)
        timestamps = sorted(timestamps)
        blocked_index = max(0, tokens - (self.max_requests - len(timestamps)) - 1)
        blocked_index = min(blocked_index, len(timestamps) - 1)
        oldest_blocking_request = timestamps[blocked_index]
        return max(0.0, oldest_blocking_request + self.window_seconds - self._now())


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            fcntl = None  # type: ignore[assignment]
        try:
            yield
        finally:
            try:
                if fcntl is not None:  # type: ignore[possibly-undefined]
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (NameError, OSError):
                pass
