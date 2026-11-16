#!/usr/bin/env python
"""Export an allowlisted, secret-checked source ZIP. Never upload or call providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "PUBLIC_RELEASE_MANIFEST.json"
TOP_FILES = {"README.md", "LICENSE", "pyproject.toml", "requirements.lock", ".gitignore"}
PUBLIC_REPORTS = {
    "evidence.md",
    "validation.md",
    "release_validation.json",
    "mock_example.md",
    "mock_signals.csv",
    "mock_decisions.csv",
    "mock_event_match_audit.csv",
}
EXCLUSIONS = [
    "credentials and environment files",
    "raw provider responses",
    "private portfolios",
    "historical run/source archives",
    "CVs and personal application documents",
    "application wording notes",
    "Git history",
]
CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key|password|secret)"
    rb"[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_-]{24,})[\"']"
)
PLACEHOLDERS = (b"DUMMY", b"TEST", b"EXAMPLE", b"REPLACE", b"YOUR_")
PRIVATE_PEM = re.compile(rb"-----BEGIN (?:(?:RSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----\r?\n")


def public_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        return False
    if name in TOP_FILES or name == ".github/workflows/research-quality.yml":
        return True
    parts = path.parts
    if not parts or "__pycache__" in parts:
        return False
    if parts[0] in {"edgelab", "sharpedge", "scripts", "tests"} and path.suffix == ".py":
        return True
    if len(parts) == 3 and parts[0] in {"edgelab", "sharpedge"}:
        return parts[1:] == ("config", "example.env")
    if len(parts) == 4 and parts[0] in {"edgelab", "sharpedge"}:
        return parts[1:3] == ("data", "samples") and path.suffix == ".json"
    if len(parts) == 2 and parts[0] == "docs" and path.suffix == ".md":
        return path.name != "cv_wording.md"
    if len(parts) == 2 and parts[0] == "research":
        return path.suffix == ".json" or parts[1] == "soccer_1x2_experiment_plan.md"
    return len(parts) == 3 and parts[:2] == ("reports", "public") and parts[2] in PUBLIC_REPORTS


def configured_secrets(root: Path, environment: Mapping[str, str] | None = None) -> list[bytes]:
    values = {
        **dotenv_values(root / ".env"),
        **(environment if environment is not None else os.environ),
    }
    return sorted(
        {
            str(value).encode()
            for key, value in values.items()
            if value
            and len(str(value)) >= 8
            and (
                key.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
                or key.upper() in {"PRIVATE_KEY", "WALLET_ADDRESS"}
            )
        }
    )


def check_contents(name: str, contents: bytes, secrets: Iterable[bytes]) -> None:
    if any(value and value in contents for value in secrets):
        raise ValueError(f"configured secret detected in {name}; value withheld")
    if PRIVATE_PEM.search(contents):
        raise ValueError(f"private key material detected in {name}; value withheld")
    for match in CREDENTIAL_ASSIGNMENT.finditer(contents):
        if not match.group(1).upper().startswith(PLACEHOLDERS):
            raise ValueError(f"possible credential assignment in {name}; value withheld")


def release_files(root: Path) -> list[Path]:
    if not all((root / name).is_file() for name in TOP_FILES):
        raise ValueError("release is missing a required root document/configuration")
    candidates = [root / name for name in TOP_FILES]
    for folder in (
        ".github",
        "edgelab",
        "sharpedge",
        "scripts",
        "tests",
        "docs",
        "research",
        "reports/public",
    ):
        candidates.extend((root / folder).rglob("*") if (root / folder).exists() else [])
    selected = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if not public_path(relative):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"symlink/escaping release member rejected: {relative}")
        if path.is_file():
            selected.append(path)
    return sorted(set(selected), key=lambda p: p.relative_to(root).as_posix())


def zip_member(archive: ZipFile, name: str, contents: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, contents)


def export_release(root: Path, output: Path, secrets: Iterable[bytes] = ()) -> dict[str, Any]:
    """Validate every candidate before creating an immutable export."""
    secrets = tuple(secrets)
    payloads = {}
    for path in release_files(root):
        name, contents = path.relative_to(root).as_posix(), path.read_bytes()
        check_contents(name, contents, secrets)
        payloads[name] = contents
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "live_orders": False,
        "profitability_established": False,
        "exclusions": EXCLUSIONS,
        "files": {
            name: hashlib.sha256(contents).hexdigest() for name, contents in payloads.items()
        },
        "security_caveat": "Configured-secret and selected-pattern checks, not general security certification",
    }
    if output.exists():
        raise FileExistsError("public export cannot overwrite an existing archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x") as archive:
        for name, contents in payloads.items():
            zip_member(archive, name, contents)
        zip_member(
            archive, MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
        )
    return manifest


def verify_release(path: Path, secrets: Iterable[bytes] = ()) -> dict[str, Any]:
    secrets = tuple(secrets)
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or MANIFEST not in names:
            raise ValueError("invalid release member inventory")
        if sum(i.file_size for i in archive.infolist()) > 200_000_000:
            raise ValueError("release exceeds uncompressed size budget")
        manifest = json.loads(archive.read(MANIFEST))
        if (
            manifest.get("schema_version") != 1
            or manifest.get("research_only") is not True
            or manifest.get("live_orders") is not False
            or manifest.get("profitability_established") is not False
        ):
            raise ValueError("invalid research-only release manifest")
        if set(names) != set(manifest["files"]) | {MANIFEST}:
            raise ValueError("unmanifested or missing release member")
        for name in names:
            if name != MANIFEST and not public_path(name):
                raise ValueError("non-allowlisted release member")
            if stat.S_ISLNK(archive.getinfo(name).external_attr >> 16):
                raise ValueError("symlink release member")
            contents = archive.read(name)
            check_contents(name, contents, secrets)
            if name != MANIFEST and hashlib.sha256(contents).hexdigest() != manifest["files"][name]:
                raise ValueError("release member hash mismatch")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    args = parser.parse_args()
    secrets = configured_secrets(ROOT)
    if args.verify:
        manifest = verify_release(args.verify, secrets)
    else:
        manifest = export_release(ROOT, args.output, secrets)
        verify_release(args.output, secrets)
    print(
        json.dumps(
            {
                "allowlisted_files": len(manifest["files"]),
                "secret_checks": "passed",
                "uploaded": False,
                "market_data_requests": 0,
                "live_orders": False,
            }
        )
    )


if __name__ == "__main__":
    main()
