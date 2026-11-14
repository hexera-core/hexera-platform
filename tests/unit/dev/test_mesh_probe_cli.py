# Responsibility: Pin the mesh-failure-real track's hermetic parts: the manifest, resolving files
#                 by sha256, and the agreement rules between two probe results.
# Boundaries: hermetic - no gmsh, no OCP, no CAD. Files are tiny stand-in bytes in tmp_path.
from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
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
    # Binary gate: both fail with different kinds still agree.
    assert cli.agreement(_result(False, "open_loop"), _result(False, "timeout"))
    assert not cli.agreement(_result(True), _result(False, "open_loop"))
    assert not cli.agreement(_result(False, "open_loop"), _result(True))


def test_same_kind_is_the_stricter_secondary_rule():
    assert cli.same_kind(_result(True), _result(True))
    assert cli.same_kind(_result(False, "open_loop"), _result(False, "open_loop"))
    assert not cli.same_kind(_result(False, "open_loop"), _result(False, "timeout"))
    assert not cli.same_kind(_result(True), _result(False, "open_loop"))


def test_summarise_gates_on_binary_and_reports_kind_swaps():
    rows = [
        {"name": "a", "file": "meshes", "reexport": "meshes", "loop": "meshes"},
        {"name": "b", "file": "x", "reexport": "open_loop", "loop": "timeout"},
        {"name": "c", "file": "x", "reexport": "meshes", "loop": "open_loop"},
    ]
    out = cli.summarise(rows, [])
    assert out["loop_agrees_reexport"] == 2
    assert [r["name"] for r in out["loop_disagrees_reexport"]] == ["c"]
    assert [r["name"] for r in out["kind_swaps_both_fail"]] == ["b"]
    assert not out["stop_rule_tripped"]


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
    # 13 diagonals come from OCP (the corpus build's child died before measuring the part) and one
    # is unavailable: OCP cannot read that file.
    for f in files:
        if f["diag_source"] == "unavailable":  # OCP cannot read it (bug30790)
            assert f["diag_mm"] is None
        else:
            assert f["diag_source"] in ("gmsh", "ocp") and f["diag_mm"] > 0
    # Names and hashes only: no CAD is committed, here or beside the manifest.
    assert not list(MANIFEST.parent.glob("*.st*p")) and not list(MANIFEST.parent.glob("*.igs"))
    assert MANIFEST.stat().st_size < 100_000
    assert "ISO-10303" not in MANIFEST.read_text()


def _die_on_b(task):
    if task[0] == "b":
        os._exit(139)
    return {"name": task[0]}


def test_dead_worker_is_an_error_row_and_the_rest_come_back():
    tasks = [(n, "p", 1.0) for n in "abcde"]
    rows = cli.run_all(tasks, 2, fn=_die_on_b, context=multiprocessing.get_context("fork"))
    by_name = {r["name"]: r for r in rows}
    assert sorted(by_name) == list("abcde")
    assert "died" in by_name["b"]["error"]
    assert all("error" not in by_name[n] for n in "acde")
