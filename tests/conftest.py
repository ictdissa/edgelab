"""Tests are offline by construction, including when developer credentials exist."""

import socket
import pytest


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ODDS_API_IO_RATE_LIMIT_STATE", str(tmp_path/"isolated_odds_budget.json"))
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real network is prohibited in tests; inject a fake provider")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
