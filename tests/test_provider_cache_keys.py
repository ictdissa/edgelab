from __future__ import annotations

from pathlib import Path

from edgelab.data.cache import CachedHttpClient, RollingRateLimiter


def test_provider_cache_keys_do_not_mix_providers_or_leak_keys(tmp_path: Path) -> None:
    limiter = RollingRateLimiter(tmp_path / "rate.json", max_requests=10)
    io_client = CachedHttpClient(tmp_path / "cache", "odds_api_io", limiter)
    the_client = CachedHttpClient(tmp_path / "cache", "the_odds_api", limiter)

    params = {"apiKey": "super-secret-key", "sport": "football", "markets": "h2h"}
    io_path = io_client._cache_path("https://example.com/events", params)
    the_path = the_client._cache_path("https://example.com/events", params)

    assert io_path != the_path
    assert io_path.parent.name == "odds_api_io"
    assert the_path.parent.name == "the_odds_api"
    assert "super-secret-key" not in str(io_path)
    assert "super-secret-key" not in str(the_path)
