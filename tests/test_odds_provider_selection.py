from __future__ import annotations

from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import select_sports_odds_provider


class FakeResponse:
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_odds_api_io_key_is_not_sent_to_the_odds_api(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs["params"]))
        return FakeResponse([])

    settings = Settings(
        ODDS_PROVIDER="odds_api_io",
        ODDS_API_IO_KEY="io-secret-key",
        THE_ODDS_API_KEY="the-secret-key",
        EDGELAB_CACHE_DIR=tmp_path / "cache",
    )
    provider = select_sports_odds_provider(settings, http_get=fake_get)
    diagnosis = provider.diagnose("football", limit=3)

    assert diagnosis.selected_provider == "odds_api_io"
    assert calls
    assert all("api.odds-api.io" in url for url, _params in calls)
    assert calls[0][1]["apiKey"] == "io-secret-key"


def test_the_odds_api_key_is_not_sent_to_odds_api_io(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs["params"]))
        return FakeResponse([])

    settings = Settings(
        ODDS_PROVIDER="the_odds_api",
        ODDS_API_IO_KEY="io-secret-key",
        THE_ODDS_API_KEY="the-secret-key",
        EDGELAB_CACHE_DIR=tmp_path / "cache",
    )
    provider = select_sports_odds_provider(settings, http_get=fake_get)
    diagnosis = provider.diagnose("soccer_epl", limit=3)

    assert diagnosis.selected_provider == "the_odds_api"
    assert calls
    assert all("api.the-odds-api.com" in url for url, _params in calls)
    assert calls[0][1]["apiKey"] == "the-secret-key"
