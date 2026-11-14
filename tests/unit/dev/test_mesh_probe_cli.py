# Responsibility: Pin the mesh-failure-real track's hermetic parts: the manifest, resolving files
#                 by sha256, and the agreement rules between two probe results.
# Boundaries: hermetic - no gmsh, no OCP, no CAD. Files are tiny stand-in bytes in tmp_path.
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MODULE = REPO / "devtools" / "validation" / "mesh_probe.py"
MANIFEST = REPO / "devtools" / "validation" / "corpora" / "mesh-failure-real.json"


def _load():
    spec = importlib.util.spec_from_file_location("mesh_probe_cli_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cli = _load()


def _result(meshed: bool, kind: str | None = None):
    failure = None if meshed else cli.MeshFailure(kind or "other", (), None, "m")
    return cli.ProbeResult(meshed, 1 if meshed else 0, failure, {})


def test_resolve_by_hash_reports_missing(tmp_path):
    (tmp_path / "sub").mkdir()
    found = tmp_path / "sub" / "renamed.step"
    found.write_bytes(b"present")
    manifest = {"files": [
        {"name": "a.step", "sha256": hashlib.sha256(b"present").hexdigest()},
        {"name": "b.step", "sha256": hashlib.sha256(b"absent").hexdigest()},
    ]}
    resolved, missing = cli.resolve_inputs(manifest, [tmp_path])
    assert resolved == {"a.step": found}
    assert missing == ["b.step"]


def test_agreement_rules():
    assert cli.agreement(_result(True), _result(True))
    assert cli.agreement(_result(False, "open_loop"), _result(False, "open_loop"))
    assert not cli.agreement(_result(False, "open_loop"), _result(False, "timeout"))
    assert not cli.agreement(_result(True), _result(False, "open_loop"))
    assert not cli.agreement(_result(False, "open_loop"), _result(True))


def test_load_manifest_rejects_wrong_track(tmp_path):
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"track": "other", "files": []}))
    try:
        cli.load_manifest(bad)
    except ValueError:
        return
    raise AssertionError("a manifest of another track must be refused")


def test_manifest_has_80_files_and_no_cad():
    manifest = cli.load_manifest(MANIFEST)
    files = manifest["files"]
    assert len(files) == 80
    assert len({f["sha256"] for f in files}) == 80
    assert {f["source"] for f in files} == {s["name"] for s in manifest["sources"]}
    assert all(len(s["archive_sha256"]) == 64 for s in manifest["sources"])
    # diag_mm is null where the corpus build's child died before measuring the part.
    assert all(f["diag_mm"] is None or f["diag_mm"] > 0 for f in files)
    # Names and hashes only: no CAD is committed, here or beside the manifest.
    assert not list(MANIFEST.parent.glob("*.st*p")) and not list(MANIFEST.parent.glob("*.igs"))
    assert MANIFEST.stat().st_size < 100_000
    assert "ISO-10303" not in MANIFEST.read_text()
