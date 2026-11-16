from __future__ import annotations

from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import select_sports_odds_provider


class FakeResponse:
    headers = {"x-requests-remaining": "9"}

    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_missing_key_produces_clear_diagnosis(tmp_path: Path) -> None:
    settings = Settings(
        ODDS_PROVIDER="odds_api_io",
        ODDS_API_IO_KEY=None,
        ODDS_API_KEY=None,
        EDGELAB_CACHE_DIR=tmp_path / "cache",
    )
    diagnosis = select_sports_odds_provider(settings).diagnose("football")

    assert not diagnosis.auth_succeeded
    assert diagnosis.failure_reason == "key missing"
    assert "ODDS_API_IO_KEY" in diagnosis.recommended_next_action


def test_401_invalid_key_produces_provider_rejected_key(tmp_path: Path) -> None:
    def fake_get(url: str, **kwargs):
        assert kwargs["params"]["apiKey"] == "bad-provider-key"
        return FakeResponse(
            401,
            text='{"message":"API key is not valid","error_code":"INVALID_KEY"}',
        )

    settings = Settings(
        ODDS_PROVIDER="the_odds_api",
        THE_ODDS_API_KEY="bad-provider-key",
        EDGELAB_CACHE_DIR=tmp_path / "cache",
    )
    diagnosis = select_sports_odds_provider(settings, http_get=fake_get).diagnose("soccer_epl")

    assert not diagnosis.auth_succeeded
    assert diagnosis.failure_reason == "provider rejected key"
    assert "bad-provider-key" not in (diagnosis.provider_error_body or "")
    assert diagnosis.quota["x-requests-remaining"] == "9"


def test_generic_fallback_401_identifies_wrong_provider_risk(tmp_path: Path) -> None:
    def fake_get(url: str, **kwargs):
        assert kwargs["params"]["apiKey"] == "generic-key"
        return FakeResponse(401, text='{"error_code":"INVALID_KEY"}')

    settings = Settings(
        ODDS_PROVIDER="the_odds_api",
        ODDS_API_KEY="generic-key",
        THE_ODDS_API_KEY=None,
        EDGELAB_CACHE_DIR=tmp_path / "cache",
    )
    diagnosis = select_sports_odds_provider(settings, http_get=fake_get).diagnose("soccer_epl")

    assert diagnosis.failure_reason == "key sent to wrong provider"
    assert "provider-specific" in diagnosis.recommended_next_action
