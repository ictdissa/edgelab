from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from sharpedge.config.settings import Settings
from sharpedge.data.odds_collector import OddsAPIIOProvider
from sharpedge.data.rate_limiter import RollingWindowRateLimiter


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_odds_api_io_rate_limiter_persists_and_blocks(tmp_path: Path) -> None:
    clock = FakeClock()
    state_path = tmp_path / "rate_limit.json"
    limiter = RollingWindowRateLimiter(
        state_path=state_path,
        max_requests=2,
        window_seconds=3600,
        now=clock,
    )

    assert limiter.reserve().allowed
    assert limiter.reserve().allowed

    blocked = RollingWindowRateLimiter(
        state_path=state_path,
        max_requests=2,
        window_seconds=3600,
        now=clock,
    ).reserve()
    assert not blocked.allowed
    assert blocked.remaining == 0
    assert math.isclose(blocked.retry_after_seconds, 3600.0)

    clock.now += 3600.1
    assert limiter.reserve().allowed


def test_odds_api_io_request_cap_cannot_exceed_100() -> None:
    with pytest.raises(ValidationError):
        Settings(ODDS_API_IO_MAX_REQUESTS_PER_HOUR=101)


def test_odds_api_io_normalizes_1x2_and_totals_payload() -> None:
    provider = OddsAPIIOProvider(api_key="not-used")
    rows = provider.normalize_odds_payloads(
        [
            {
                "id": 123456,
                "home": "Manchester United",
                "away": "Liverpool",
                "sport": {"name": "Football", "slug": "football"},
                "bookmakers": {
                    "Pinnacle": [
                        {
                            "name": "ML",
                            "updatedAt": "2026-05-16T12:00:00Z",
                            "odds": [{"home": "2.10", "draw": "3.40", "away": "3.20"}],
                        },
                        {
                            "name": "Totals",
                            "updatedAt": "2026-05-16T12:01:00Z",
                            "odds": [{"hdp": 2.5, "over": "1.91", "under": "1.95"}],
                        },
                    ]
                },
            }
        ]
    )

    one_x_two = {(row.market_type, row.outcome): row.decimal_odds for row in rows}
    assert one_x_two[("1x2", "Manchester United")] == 2.10
    assert one_x_two[("1x2", "Draw")] == 3.40
    assert one_x_two[("1x2", "Liverpool")] == 3.20
    assert one_x_two[("totals", "Over 2.5")] == 1.91
    assert one_x_two[("totals", "Under 2.5")] == 1.95
