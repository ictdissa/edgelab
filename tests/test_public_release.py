"""Publication must not silently include local research data or credentials."""

import json
from zipfile import ZipFile

import pytest

from scripts.export_public_release import (
    MANIFEST,
    TOP_FILES,
    configured_secrets,
    check_contents,
    export_release,
    public_path,
    verify_release,
)


def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in TOP_FILES:
        (root / name).write_text("research only\n")
    for name in (
        "edgelab/__init__.py",
        "edgelab/data/samples/markets.json",
        "edgelab/config/example.env",
        "reports/public/evidence.md",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if path.suffix == ".json" else "# research only\n")
    return root


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "data/raw/response.json",
        "reports/private/raw/response.json",
        "reports/run/source_code.zip",
        "experiments/old/ledger.csv",
        "docs/archive/old.md",
        "docs/cv_wording.md",
        ".git/config",
        "CV.pdf",
        "edgelab/config/private.env",
        "../README.md",
        "/README.md",
    ],
)
def test_public_path_rejects_private_members(name):
    assert not public_path(name)


def test_export_is_deterministic_and_omits_private_data(tmp_path):
    root = source(tmp_path)
    (root / ".env").write_text("ODDS_API_IO_KEY=PRIVATE_TEST_SECRET\n")
    private = root / "reports/old_run/raw.json"
    private.parent.mkdir(parents=True)
    private.write_text("PRIVATE_TEST_SECRET")
    notes = root / "docs/cv_wording.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Personal application notes, not project documentation.\n")
    workflow = root / ".github/workflows/research-quality.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: test\n")
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    export_release(root, one, [b"PRIVATE_TEST_SECRET"])
    export_release(root, two, [b"PRIVATE_TEST_SECRET"])
    assert one.read_bytes() == two.read_bytes()
    assert verify_release(one)["live_orders"] is False
    with ZipFile(one) as handle:
        assert ".env" not in handle.namelist()
        assert "reports/old_run/raw.json" not in handle.namelist()
        assert "docs/cv_wording.md" not in handle.namelist()
        assert "edgelab/data/samples/markets.json" in handle.namelist()
        assert ".github/workflows/research-quality.yml" in handle.namelist()
    assert notes.is_file()
    with pytest.raises(FileExistsError):
        export_release(root, one)


def test_configured_secret_is_blocked_without_echoing_value(tmp_path):
    root = source(tmp_path)
    private = b"super-private-value-not-for-publication"
    (root / "README.md").write_bytes(private)
    with pytest.raises(ValueError) as caught:
        export_release(root, tmp_path / "blocked.zip", [private])
    assert private.decode() not in str(caught.value)
    assert not (tmp_path / "blocked.zip").exists()


def test_unknown_credential_pattern_is_blocked(tmp_path):
    root = source(tmp_path)
    (root / "edgelab/__init__.py").write_text('api_key = "' + "a" * 40 + '"')
    with pytest.raises(ValueError, match="credential assignment"):
        export_release(root, tmp_path / "blocked.zip")


def test_private_pem_detection_distinguishes_source_escape_from_actual_header():
    check_contents("source.py", rb'header = "-----BEGIN PRIVATE KEY-----\n"', ())
    with pytest.raises(ValueError, match="private key material"):
        check_contents("material", b"-----BEGIN PRIVATE KEY-----\nDUMMY\n", ())


def test_symlink_is_rejected(tmp_path):
    root = source(tmp_path)
    private = tmp_path / "private.py"
    private.write_text("never publish")
    (root / "edgelab/private.py").symlink_to(private)
    with pytest.raises(ValueError, match="symlink"):
        export_release(root, tmp_path / "blocked.zip")


def test_tampered_or_unallowlisted_zip_member_fails_verification(tmp_path):
    root = source(tmp_path)
    original = tmp_path / "original.zip"
    export_release(root, original)
    for extra in (False, True):
        altered = tmp_path / f"altered-{extra}.zip"
        with ZipFile(original) as reader, ZipFile(altered, "w") as writer:
            manifest = json.loads(reader.read(MANIFEST))
            for name in reader.namelist():
                if name != MANIFEST:
                    writer.writestr(name, b"changed" if name == "README.md" else reader.read(name))
            if extra:
                writer.writestr(".env", "private")
                manifest["files"][".env"] = "irrelevant"
            writer.writestr(MANIFEST, json.dumps(manifest))
        with pytest.raises(ValueError):
            verify_release(altered)


def test_secret_discovery_never_treats_public_api_base_as_a_key(tmp_path):
    (tmp_path / ".env").write_text(
        "ODDS_API_IO_KEY=PRIVATE_TEST_SECRET\nODDS_API_BASE=https://example.test\n"
    )
    secrets = configured_secrets(tmp_path, environment={})
    assert secrets == [b"PRIVATE_TEST_SECRET"]
