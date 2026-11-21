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
            import hashlib
            doc = {"step": {"sha256": hashlib.sha256(returned).hexdigest()},
                   "diagonal_mm": 100.0, **hints}
            Path(f"{written}.mesh-hints.json").write_text(json.dumps(doc))
    return upload, scratch


class FakeHints:
    """Stands in for lane G's cad/repair/mesh_hints (read_hints only)."""

    @staticmethod
    def read_hints(path):
        doc = json.loads(Path(path).read_text())
        if doc.get("schema") != "crs-mesh-hints/1":
            raise ValueError("not a crs-mesh-hints/1 sidecar")
        return doc


def _score(row, upload, scratch, *, default=True, gmsh_default=False, hinted=True,
           before_gd=False, monkeypatch=None, hint_quality=None):
    calls = []

    def extra(path, *, diagonal_mm, mode, budget_s):
        calls.append((Path(path).name, mode))
        return {"meshed": before_gd if Path(path).name == "part.step" else gmsh_default}

    def default_probe(path, *, diagonal_mm, budget_s):
        calls.append((Path(path).name, "default"))
        return SimpleNamespace(meshed=default, failure=None if default
                               else SimpleNamespace(kind="overlapping_facets"))

    def hinted_probe(path, *, diagonal_mm, hints, quality, budget_s):
        calls.append((Path(path).name, "hints"))
        return SimpleNamespace(meshed=hinted, hints_applied=len(hints), hints_unmapped=(),
                               quality=dict(hint_quality or {}),
                               failure=None if hinted else SimpleNamespace(kind="overlapping_facets"))

    monkeypatch.setattr(rc, "_diagonal_of", lambda p: 100.0)
    rc._score_returned(row, upload, scratch, since=time.time() - 60, extra=extra,
                       default_probe=default_probe, inspect=lambda p: {"valid": True},
                       hints_mod=FakeHints, hinted_probe=hinted_probe)
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
    hints = {"schema": "crs-mesh-hints/1", "units": "mm", "status": "verified", "attempts": 2,
             "faces": [{"face": 3, "size_mm": 0.2, "why": "sub_mesh_gap",
                        "fingerprint": {"surface": "Cylinder", "area_mm2": 4.0,
                                        "centroid": [0, 0, 0], "bbox": [0, 0, 0, 1, 1, 1]}}]}
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


def test_hints_need_lane_gs_module_and_matching_bytes(tmp_path, monkeypatch):
    hints = {"schema": "crs-mesh-hints/1", "faces": [{"face": 1, "size_mm": 0.3,
                                                      "fingerprint": {}}]}
    upload, scratch = _setup(tmp_path, returned=b"REPAIRED", hints=hints)
    row = {"outcome": "refused", "meshes_before": False}
    monkeypatch.setattr(rc, "_diagonal_of", lambda p: 100.0)
    rc._score_returned(row, upload, scratch, since=time.time() - 60,
                       extra=lambda *a, **k: {"meshed": False},
                       default_probe=lambda p, **k: SimpleNamespace(
                           meshed=False, failure=SimpleNamespace(kind="x")),
                       inspect=lambda p: {"valid": True}, hints_mod=None,
                       hinted_probe=lambda *a, **k: 1 / 0)
    # no lane G module in this build: reported, never applied by a second implementation
    assert row["meshes_after_hints"] is None and row["returned_step"]["hints"] == "no_consumer"
    # a sidecar for other bytes is stale
    side = scratch / "part-repaired.step.mesh-hints.json"
    doc = json.loads(side.read_text())
    doc["step"]["sha256"] = "0" * 64
    side.write_text(json.dumps(doc))
    row2 = {"outcome": "refused", "meshes_before": False}
    rc._score_returned(row2, upload, scratch, since=time.time() - 60,
                       extra=lambda *a, **k: {"meshed": False},
                       default_probe=lambda p, **k: SimpleNamespace(
                           meshed=False, failure=SimpleNamespace(kind="x")),
                       inspect=lambda p: {"valid": True}, hints_mod=FakeHints,
                       hinted_probe=lambda *a, **k: 1 / 0)
    assert row2["returned_step"]["hints"] == "stale" and row2["meshes_after_hints"] is None


def _hinted_row(tmp_path, monkeypatch, quality, attempts=3):
    hints = {"schema": "crs-mesh-hints/1", "attempts": attempts, "status": "verified",
             "faces": [{"face": 1, "size_mm": 0.3, "fingerprint": {}}]}
    upload, scratch = _setup(tmp_path, returned=b"REPAIRED", hints=hints)
    row = {"outcome": "refused", "meshes_before": False, "valid_before": True}
    _score(row, upload, scratch, default=False, hinted=True, hint_quality=quality,
           monkeypatch=monkeypatch)
    return row


def test_hinted_quality_is_recorded_with_attempts(tmp_path, monkeypatch):
    row = _hinted_row(tmp_path, monkeypatch, {"elements": 2_000_000, "min_sicn": 0.12,
                                               "min_gamma": 0.05, "min_edge_mm": 0.01})
    assert row["meshes_after_hints"] is True
    assert row["mesh_quality_after_hints"]["attempts"] == 3
    assert row["mesh_quality_after_hints"]["hints_applied"] == 1
    assert row["mesh_quality_after_hints"]["hint_status"] == "verified"
    s = rc.meshability_summary([row])
    assert s["hints_over_element_cap"] == 1 and s["hints_degenerate"] == 0
    assert s["hints_quality"] == {"files": 1, "worst_min_sicn": 0.12, "worst_min_gamma": 0.05,
                                  "smallest_edge_mm": 0.01, "most_elements": 2_000_000,
                                  "most_attempts": 3}


def test_a_degenerate_hinted_mesh_does_not_count(tmp_path, monkeypatch):
    row = _hinted_row(tmp_path, monkeypatch, {"elements": 500, "min_sicn": -0.2})
    assert row["meshes_after_hints"] is False
    assert row["mesh_kind_after_hints"] == "degenerate"
    s = rc.meshability_summary([row])
    assert s["meshes_with_hints"] == 0 and s["hints_degenerate"] == 1


def test_a_file_crs_never_touched_returns_the_upload(tmp_path, monkeypatch):
    # Clean-control parts mostly never reach the repair: the route sends them on as uploaded.
    # That is a returned STEP (the upload), not a no_returned_step bug, and it is not re-probed.
    for outcome in ("nothing_to_repair", "not_attempted:mesh_as_is"):
        d = tmp_path / outcome.replace(":", "_")
        d.mkdir()
        upload, scratch = _setup(d, returned=None)
        row = {"outcome": outcome, "meshes_before": True, "mesh_kind_before": "",
               "valid_before": True}
        calls = _score(row, upload, scratch, before_gd=True, monkeypatch=monkeypatch)
        assert row["returned_step"]["present"] is True
        assert row["returned_step"]["source"] == "untouched"
        assert row["meshes_after_default"] is True and row["meshes_after_gmsh_default"] is True
        assert ("part.step", "default") not in calls
        assert row["valid_to_invalid"] is False
        s = rc.meshability_summary([row])
        assert s["no_returned_step"] == 0 and s["source_untouched"] == 1


def test_sicn_floor_sensitivity_counts_hint_only_files_per_floor():
    def r(before, default, hints, sicn=None):
        return {"returned_step": {"present": True, "source": "repaired", "hints": "ok"},
                "meshes_before": before, "meshes_after_default": default,
                "meshes_after_hints": hints,
                "mesh_quality_after_hints": {} if sicn is None else {"min_sicn": sicn}}
    rows = [r(False, True, None),            # meshes at default: counts at every floor
            r(False, False, True, 0.0005),   # hints-only, very poor
            r(False, False, True, 0.02),     # hints-only, fair
            r(False, False, True, None),     # hints-only, no reading: never counts by floor
            r(True, True, None)]             # meshed before: not newly
    s = rc.meshability_summary(rows)
    assert s["meshes_with_hints_by_sicn_floor"] == {"0.0": 4, "0.001": 3, "0.01": 3, "0.05": 2}
    assert s["newly_meshes_with_hints_by_sicn_floor"] == {
        "0.0": 3, "0.001": 2, "0.01": 2, "0.05": 1}
    assert s["meshes_with_hints"] == 5   # the headline applies no floor beyond SICN > 0


def test_unplaced_defects_is_read_as_the_count_lane_gs_schema_records():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "devtools/validation/repair_corpus.py"
    spec = importlib.util.spec_from_file_location("repair_corpus_count", path)
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    assert rc._count(3) == 3
    assert rc._count(0) == 0
    assert rc._count(None) == 0
    assert rc._count([{"a": 1}, {"b": 2}]) == 2
