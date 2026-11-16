from __future__ import annotations

from pathlib import Path

from sharpedge.config.settings import Settings
from sharpedge.data.odds_api_io_provider import OddsAPIIOProvider


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_cached_provider_makes_zero_api_calls_when_use_cache_enabled(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url.endswith("/events"):
            return FakeResponse(
                [
                    {
                        "id": 1,
                        "home": "Arsenal",
                        "away": "Chelsea",
                        "sport": {"name": "Football", "slug": "football"},
                        "date": "2026-06-01T12:00:00Z",
                    }
                ]
            )
        return FakeResponse(
            {
                "id": 1,
                "home": "Arsenal",
                "away": "Chelsea",
                "sport": {"name": "Football", "slug": "football"},
                "bookmakers": {
                    "Bet365": [
                        {
                            "name": "ML",
                            "odds": [{"home": "2.1", "draw": "3.4", "away": "3.2"}],
                        }
                    ]
                },
            }
        )

    settings = Settings(
        ODDS_API_IO_KEY="test-key",
        ODDS_API_IO_CACHE_DIR=tmp_path / "cache",
        ODDS_API_IO_RATE_LIMIT_STATE=tmp_path / "rate_limit.json",
        ODDS_API_IO_MAX_REQUESTS_PER_HOUR=90,
    )
    provider = OddsAPIIOProvider(settings, http_get=fake_get, write_outputs=False)
    first = provider.fetch_snapshot(
        sport="football",
        bookmakers="Bet365",
        max_events=1,
        refresh=True,
    )
    assert first.requests_made == 2
    assert len(calls) == 2

    def forbidden_get(url: str, **kwargs):
        raise AssertionError("cache path should not call HTTP")

    cached_provider = OddsAPIIOProvider(settings, http_get=forbidden_get, write_outputs=False)
    second = cached_provider.fetch_snapshot(
        sport="football",
        bookmakers="Bet365",
        max_events=1,
        use_cache=True,
    )
    assert second.requests_made == 0
    assert second.cache_hits == 2
    assert len(second.events) == 1
    assert len(second.normalized_odds) == 3
