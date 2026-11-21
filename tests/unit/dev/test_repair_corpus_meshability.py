# Responsibility: Verify the corpus runner's meshability report: the returned STEP is judged (a
#                 missing one is a bug), the three gmsh verdicts stay apart, hints only add to a
#                 returned STEP, the op that made a file mesh is credited, and the summary counts.
# Boundaries: hermetic - the probes, the diagonal and the inspection are stubbed; no CAD is read and
#             no gmsh process is started. Files are written to tmp_path only to be found and hashed.
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("repair_corpus_meshability_under_test", "devtools/validation/repair_corpus.py")
mp = _load("meshability_probe_under_test", "devtools/validation/meshability_probe.py")


def _setup(tmp_path: Path, *, returned: bytes | None, hints: dict | None = None,
           stale: bool = False):
    upload = tmp_path / "part.step"
    upload.write_bytes(b"UPLOAD")
    scratch = tmp_path / "out"
    scratch.mkdir()
    written = scratch / "part-repaired.step"
    if returned is not None:
        written.write_bytes(returned)
        if stale:
            old = time.time() - 3600
            os.utime(written, (old, old))
        if hints is not None:
            Path(f"{written}.mesh-hints.json").write_text(json.dumps(hints))
    return upload, scratch


def _score(row, upload, scratch, *, default=True, gmsh_default=False, hinted=True,
           before_gd=False, monkeypatch=None):
    calls = []

    def extra(path, *, diagonal_mm, mode, hints=None, budget_s):
        calls.append((Path(path).name, mode))
        if mode == "gmsh_default":
            return {"meshed": before_gd if Path(path).name == "part.step" else gmsh_default}
        return {"meshed": hinted, "kind": "" if hinted else "overlapping_facets"}

    def default_probe(path, *, diagonal_mm, budget_s):
        calls.append((Path(path).name, "default"))
        return SimpleNamespace(meshed=default, failure=None if default
                               else SimpleNamespace(kind="overlapping_facets"))

    monkeypatch.setattr(rc, "_diagonal_of", lambda p: 100.0)
    rc._score_returned(row, upload, scratch, since=time.time() - 60, extra=extra,
                       default_probe=default_probe, inspect=lambda p: {"valid": True})
    return calls


def test_no_returned_step_is_a_bug_and_gets_no_hinted_verdict(tmp_path, monkeypatch):
    upload, scratch = _setup(tmp_path, returned=None)
    row = {"outcome": "refused", "meshes_before": False, "valid_before": True}
    _score(row, upload, scratch, monkeypatch=monkeypatch)
    assert row["returned_step"] == {"present": False}
    assert row["mesh_kind_after_default"] == "no_returned_step"
    assert row["meshes_after_default"] is None and row["meshes_after_hints"] is None
    s = rc.meshability_summary([row])
    assert s["no_returned_step"] == 1 and s["steps_returned"] == 0


def test_a_stale_file_is_not_a_return(tmp_path, monkeypatch):
    upload, scratch = _setup(tmp_path, returned=b"OLD", stale=True)
    row = {"outcome": "refused", "meshes_before": False}
    _score(row, upload, scratch, monkeypatch=monkeypatch)
    assert row["returned_step"]["present"] is False


def test_passthrough_is_judged_like_any_return(tmp_path, monkeypatch):
    upload, scratch = _setup(tmp_path, returned=b"UPLOAD")
    row = {"outcome": "refused", "meshes_before": False, "valid_before": True}
    calls = _score(row, upload, scratch, default=False, monkeypatch=monkeypatch)
    assert row["returned_step"]["source"] == "passthrough"
    assert ("part-repaired.step", "default") in calls   # refused rows are probed here
    assert row["meshes_after_default"] is False


def test_pass_supplied_source_wins(tmp_path, monkeypatch):
    upload, scratch = _setup(tmp_path, returned=b"UPLOAD")
    row = {"outcome": "refused", "meshes_before": False, "delivery_source": "repaired"}
    _score(row, upload, scratch, monkeypatch=monkeypatch)
    assert row["returned_step"]["source"] == "repaired"


def test_three_verdicts_stay_apart_and_hints_add(tmp_path, monkeypatch):
    hints = {"schema": "crs-mesh-hints/1", "units": "mm",
             "faces": [{"face": 3, "fingerprint": "face:ab", "size_mm": 0.2, "why": "gap"}]}
    upload, scratch = _setup(tmp_path, returned=b"REPAIRED", hints=hints)
    row = {"outcome": "refused", "meshes_before": False, "valid_before": True}
    _score(row, upload, scratch, default=False, gmsh_default=False, hinted=True,
           monkeypatch=monkeypatch)
    assert row["returned_step"]["hints"] == "ok"
    assert (row["meshes_after_default"], row["meshes_after_gmsh_default"],
            row["meshes_after_hints"]) == (False, False, True)
    s = rc.meshability_summary([row])
    assert s["meshes_default"] == 0
    assert s["meshes_with_hints"] == 1 and s["meshes_only_with_hints"] == 1


def test_reuses_the_headline_probe_already_taken_on_a_repaired_file(tmp_path, monkeypatch):
    upload, scratch = _setup(tmp_path, returned=b"REPAIRED")
    row = {"outcome": "repaired", "meshes_before": False, "meshes_after": True,
           "mesh_kind_after": "", "valid_before": True, "valid_after": False}
    calls = _score(row, upload, scratch, monkeypatch=monkeypatch)
    assert ("part-repaired.step", "default") not in calls
    assert row["meshes_after_default"] is True
    assert row["valid_to_invalid"] is True


def test_meshed_by_credits_the_first_pass_op_whose_probe_flips():
    row = {"meshes_before": False, "meshes_after_default": True,
           "meshability_pass": [
               {"op": "merge_same_curve_edges", "rung": 1, "fingerprint": "edge:1",
                "verdict": "kept", "probe_after": {"meshes": False, "kind": "x"}},
               {"op": "resolve_sub_mesh_gap", "rung": 2, "fingerprint": "face:2",
                "verdict": "kept", "probe_after": {"meshes": True, "kind": ""}},
               {"op": "late", "verdict": "kept", "probe_after": {"meshes": True}}],
           "operations": [{"op": "fix_small_faces", "status": "kept", "targets": ["face:9"]}]}
    assert rc.meshed_by(row) == {"by": "pass", "op": "resolve_sub_mesh_gap", "rung": 2}


def test_meshed_by_falls_to_the_loop_excluding_pass_ops():
    row = {"meshes_before": False, "meshes_after_default": True,
           "meshability_pass": [{"op": "resolve_sub_mesh_gap", "fingerprint": "face:2",
                                 "verdict": "kept", "probe_after": {"meshes": False}}],
           "operations": [
               {"op": "resolve_sub_mesh_gap", "status": "kept", "targets": ["face:2"]},
               {"op": "fix_small_faces", "status": "kept", "targets": ["face:9"]},
               {"op": "sew_faces", "status": "rolled_back", "targets": ["edge:1"]}]}
    assert rc.meshed_by(row) == {"by": "loop", "op": "fix_small_faces"}
    row["operations"].append({"op": "sew_faces", "status": "kept", "targets": ["edge:4"]})
    assert rc.meshed_by(row) == {"by": "loop_combo", "ops": ["fix_small_faces", "sew_faces"]}


def test_meshed_by_none_unless_newly_meshing():
    assert rc.meshed_by({"meshes_before": True, "meshes_after_default": True}) is None
    assert rc.meshed_by({"meshes_before": False, "meshes_after_default": False}) is None
    assert rc.meshed_by({"meshes_before": False, "meshes_after_default": True}) == {
        "by": "unattributed"}


def test_summary_counts_broken_and_credit():
    rows = [
        {"returned_step": {"present": True, "source": "repaired", "hints": "none"},
         "meshes_before": False, "meshes_after_default": True,
         "meshed_by": {"by": "pass", "op": "resolve_sub_mesh_gap"}},
        {"returned_step": {"present": True, "source": "passthrough", "hints": "none"},
         "meshes_before": True, "meshes_after_default": False, "valid_to_invalid": True},
        {"returned_step": {"present": False}, "meshes_before": False},
        {"outcome": "crashed"},
    ]
    s = rc.meshability_summary(rows)
    assert s["files"] == 3 and s["crashed"] == 1
    assert s["newly_meshes_default"] == 1 and s["newly_broken_default"] == 1
    assert s["valid_to_invalid"] == 1 and s["no_returned_step"] == 1
    assert s["source_passthrough"] == 1
    assert s["meshed_by"] == {"pass:resolve_sub_mesh_gap": 1}
    assert rc.meshability_summary([{"meshes_before": True}]) == {}


def test_pass_rows_copied_only_when_present():
    assert rc._pass_rows({"x": 1}) == {}
    assert rc._pass_rows({"meshability_pass": [], "delivery_source": "passthrough"}) == {
        "meshability_pass": [], "delivery_source": "passthrough"}


def test_hints_schema_is_checked(tmp_path):
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"schema": "crs-mesh-hints/1",
                                "faces": [{"face": 2, "size_mm": 0.5}], "face_count": 9}))
    assert mp.load_hints(good) == {"faces": [{"face": 2, "size_mm": 0.5}],
                                   "global_max_mm": None, "face_count": 9}
    for bad in ({"schema": "other", "faces": []},
                {"schema": "crs-mesh-hints/1", "faces": [{"face": 0, "size_mm": 1}]},
                {"schema": "crs-mesh-hints/1", "faces": [{"face": 1, "size_mm": -1}]},
                {"schema": "crs-mesh-hints/1"}):
        p = tmp_path / "b.json"
        p.write_text(json.dumps(bad))
        assert mp.load_hints(p) is None
    assert mp.load_hints(tmp_path / "missing.json") is None
    assert mp.hints_path(Path("/x/y.step")) == Path("/x/y.step.mesh-hints.json")
