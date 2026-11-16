"""Protocol timing, content integrity and credential-free reproduction."""

import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from edgelab.config.settings import Settings
from edgelab.research.experiment import (
    ROOT,
    ResearchProtocol,
    file_hash,
    independent_block_sample_size,
    seal_run,
    verify_run,
)
from scripts.reproduce_research import reproduce_mock


def protocol():
    return ResearchProtocol.model_validate_json(
        (ROOT / "research/protocol_soccer_1x2.json").read_text()
    )


def test_reproducible_mock_manifest_never_contains_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ODDS_API_IO_KEY", "DUMMY_SECRET_MUST_NOT_APPEAR")
    monkeypatch.setenv("FEE_BPS", "9999")
    path = reproduce_mock(tmp_path, ROOT / "research/protocol_soccer_1x2.json")
    manifest = verify_run(tmp_path)
    assert not any(name.endswith(".csv.lock") for name in manifest["artifacts"])
    assert "DUMMY_SECRET" not in path.read_text()
    assert manifest["settings"]["fee_bps"] == 20
    assert manifest["evidence_type"] == "synthetic_integration"
    assert "SYNTHETIC_INTEGRATION_ONLY" in (tmp_path / "report.md").read_text()
    assert "Paper trades entered: 0" in (tmp_path / "report.md").read_text()
    with pytest.raises(ValueError, match="empty"):
        reproduce_mock(tmp_path, ROOT / "research/protocol_soccer_1x2.json")


def test_changed_artifact_invalidates_run(tmp_path):
    reproduce_mock(tmp_path, ROOT / "research/protocol_soccer_1x2.json")
    (tmp_path / "signals.csv").write_text("modified")
    with pytest.raises(ValueError, match="artifact changed"):
        verify_run(tmp_path)


def test_archived_inputs_reproduce_numerical_artifacts(tmp_path):
    original, repeated = tmp_path / "original", tmp_path / "repeated"
    reproduce_mock(original, ROOT / "research/protocol_soccer_1x2.json")
    reproduce_mock(repeated, ROOT / "research/protocol_soccer_1x2.json", original)
    for name in ("signals.csv", "decisions.csv", "event_match_audit.csv"):
        assert file_hash(original / name) == file_hash(repeated / name)
    assert verify_run(repeated)["profitability_claim"] is False


def test_source_archive_is_complete_and_excludes_credentials(tmp_path):
    reproduce_mock(tmp_path, ROOT / "research/protocol_soccer_1x2.json")
    manifest = verify_run(tmp_path)
    with zipfile.ZipFile(tmp_path / "source_code.zip") as archive:
        assert set(archive.namelist()) == set(manifest["source_hashes"])
        assert not any(".env" in name or name.startswith("data/") for name in archive.namelist())
        assert "requirements.lock" in archive.namelist()
    assert verify_run(tmp_path, root=tmp_path, check_source=False)["profitability_claim"] is False
    with pytest.raises(ValueError, match="source tree changed"):
        verify_run(tmp_path, root=tmp_path)


def test_changed_manifest_invalidates_run(tmp_path):
    reproduce_mock(tmp_path, ROOT / "research/protocol_soccer_1x2.json")
    path = tmp_path / "manifest.json"
    data = json.loads(path.read_text())
    data["evidence_type"] = "prospective_registration"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integrity"):
        verify_run(tmp_path)


def test_cannot_backdate_prospective_registration(tmp_path):
    now = datetime(2026, 10, 13, tzinfo=UTC)
    spec = protocol().model_copy(
        update={
            "evaluation_start": now - timedelta(days=1),
            "evaluation_end": now + timedelta(days=1),
        }
    )
    with pytest.raises(ValueError, match="future"):
        seal_run(tmp_path, spec, Settings(_env_file=None), "prospective_registration", [], now=now)
    spec.evaluation_start = now + timedelta(hours=1)
    spec.leagues = ["synthetic-league"]
    spec.bookmaker_sources = ["synthetic-book"]
    path = seal_run(
        tmp_path, spec, Settings(_env_file=None), "prospective_registration", [], now=now
    )
    assert path.exists()
    assert verify_run(tmp_path)["profitability_claim"] is False
    with pytest.raises(FileExistsError):
        seal_run(tmp_path, spec, Settings(_env_file=None), "prospective_registration", [], now=now)


def test_power_requirement_uses_independent_block_variance():
    n = independent_block_sample_size(0.02, 0.005)
    assert n == 126
    assert independent_block_sample_size(0.02, 0.0025) > n
    assert independent_block_sample_size(0.03, 0.005) > n
    for bad in (0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            independent_block_sample_size(bad, 0.005)
