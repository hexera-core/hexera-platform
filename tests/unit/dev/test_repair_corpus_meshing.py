# Responsibility: Verify the corpus runner scores "does the file mesh" before and after repair.
# Boundaries: hermetic - the gmsh probe and the repair are stubbed; no CAD is read.
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from meshpipeline.cad.repair import mesh_probe

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_meshing_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load()


def _row(before, after, kind_before="", kind_after=""):
    return {"outcome": "repaired", "meshes_before": before, "meshes_after": after,
            "mesh_kind_before": kind_before, "mesh_kind_after": kind_after}


def test_newly_meshed_counts_fail_to_pass():
    out = corpus.meshing_summary([_row(False, True, "overlapping_facets"), _row(True, True)])
    assert out["files"] == 2
    assert out["meshed_before"] == 1
    assert out["meshed_after"] == 2
    assert out["newly_meshed"] == 1
    assert out["newly_broken"] == 0
    assert out["still_failing_by_kind"] == {}


def test_newly_broken_is_counted_separately():
    out = corpus.meshing_summary([_row(True, False, "", "open_loop")])
    assert out["newly_broken"] == 1
    assert out["newly_meshed"] == 0
    assert out["meshed_after"] == 0
    assert out["still_failing_by_kind"] == {"open_loop": 1}


def test_unrepaired_file_keeps_its_before_verdict():
    rows = [_row(True, None), _row(False, None, "timeout")]
    out = corpus.meshing_summary(rows)
    assert out["meshed_before"] == 1
    assert out["meshed_after"] == 1
    assert out["newly_meshed"] == 0 and out["newly_broken"] == 0
    assert out["still_failing_by_kind"] == {"timeout": 1}


def test_summary_absent_without_probe():
    rows = [{"outcome": "repaired", "repaired_defects": {}}]
    assert "meshing" not in corpus.corpus_summary(rows)
    with_probe = corpus.corpus_summary([_row(False, True, "other")] + rows)
    assert with_probe["meshing"]["files"] == 1


def test_score_meshing_probes_input_then_written_file(monkeypatch, tmp_path):
    calls = []

    def fake_probe(path, *, diagonal_mm, budget_s=120.0, **_):
        calls.append((Path(path).name, diagonal_mm, budget_s))
        failed = path.name == "a.step"
        failure = mesh_probe.MeshFailure("open_loop", (), None, "") if failed else None
        return mesh_probe.ProbeResult(not failed, 0 if failed else 1, failure, {})

    monkeypatch.setattr(mesh_probe, "probe_file", fake_probe)
    monkeypatch.setattr(corpus, "_diagonal_of", lambda path: 42.0)
    (tmp_path / "a-repaired.step").write_text("x")
    row = {"outcome": "repaired"}
    corpus._score_meshing(row, tmp_path / "a.step", tmp_path)
    assert row["meshes_before"] is False and row["mesh_kind_before"] == "open_loop"
    assert row["meshes_after"] is True and row["mesh_kind_after"] == ""
    assert calls == [("a.step", 42.0, 120.0), ("a-repaired.step", 42.0, 120.0)]


def test_score_meshing_leaves_after_none_when_nothing_written(monkeypatch, tmp_path):
    monkeypatch.setattr(mesh_probe, "probe_file",
                        lambda path, **_: mesh_probe.ProbeResult(True, 1, None, {}))
    monkeypatch.setattr(corpus, "_diagonal_of", lambda path: 1.0)
    (tmp_path / "b-repaired.step").write_text("stale from an earlier run")
    row = {"outcome": "refused"}
    corpus._score_meshing(row, tmp_path / "b.step", tmp_path)
    assert row["meshes_before"] is True
    assert row["meshes_after"] is None and row["mesh_kind_after"] == ""


def test_mesh_located_keeps_only_mesh_defects_still_held_with_their_fingerprints():
    measured = {"regions": {"defects_remaining": [
        {"code": "mesh_open_loop", "entity": "face:3", "fingerprint": "ab12", "region": "x"},
        {"code": "small_edge", "entity": "edge:4", "fingerprint": "cd34"},
        {"code": "mesh_timeout", "entity": "face:0", "fingerprint": ""},
    ]}}
    assert corpus._mesh_located(measured) == [
        {"code": "mesh_open_loop", "entity": "face:3", "fingerprint": "ab12"},
        {"code": "mesh_timeout", "entity": "face:0", "fingerprint": ""},
    ]
    assert corpus._mesh_located(None) == [] and corpus._mesh_located({}) == []


def test_mesh_located_of_a_report_with_no_mesh_defects_is_empty():
    assert corpus._mesh_located({"regions": {"defects_remaining": [
        {"code": "small_edge", "entity": "edge:1", "fingerprint": "x"}]}}) == []
    assert corpus._mesh_located({"timings": {}}) == []


# --force-repair: the benchmark's runner-only switch for files the inspector calls clean.
def _clean_seams(monkeypatch, tmp_path, *, probe_meshes: bool):
    from types import SimpleNamespace

    from meshpipeline.cad.repair import conservative

    sessions: list[Path] = []
    fake_report = SimpleNamespace(defects=[], measurements=[], to_dict=lambda: {"defects": [], "measurements": [], "entities": []})
    monkeypatch.setattr(corpus, "_inspect", lambda path: {
        "report": fake_report, "defects": [], "entities": [], "valid": True,
        "counts": {}, "located": {}})

    def fake_repair(source, destination, **_):
        sessions.append(Path(source))
        raise conservative.RepairUnavailable("seam")

    monkeypatch.setattr(conservative, "repair_step_file", fake_repair)
    failure = None if probe_meshes else mesh_probe.MeshFailure("edge_through_face", (), None, "")
    monkeypatch.setattr(mesh_probe, "probe_file", lambda path, **_: mesh_probe.ProbeResult(
        probe_meshes, 1 if probe_meshes else 0, failure, {}))
    monkeypatch.setattr(corpus, "_diagonal_of", lambda path: 10.0)
    src = tmp_path / "clean.step"
    src.write_text("x")
    return src, sessions


def test_force_repair_runs_the_session_on_clean_inspections(monkeypatch, tmp_path):
    src, sessions = _clean_seams(monkeypatch, tmp_path, probe_meshes=False)
    row = corpus._one(src, tmp_path, mesh_probe=True, force_repair=True)
    assert sessions == [src]
    assert row["outcome"] == "unavailable"


def test_force_repair_leaves_a_meshing_clean_file_alone(monkeypatch, tmp_path):
    src, sessions = _clean_seams(monkeypatch, tmp_path, probe_meshes=True)
    row = corpus._one(src, tmp_path, mesh_probe=True, force_repair=True)
    assert sessions == [] and row["outcome"] == "nothing_to_repair"
    assert "forced_repair" not in row


def test_force_repair_rows_are_marked(monkeypatch, tmp_path):
    src, _ = _clean_seams(monkeypatch, tmp_path, probe_meshes=False)
    row = corpus._one(src, tmp_path, mesh_probe=True, force_repair=True)
    assert row["forced_repair"] is True
    assert row["triage_route"] == "mesh_as_is"
    assert row["meshes_before"] is False
    out = corpus.meshing_summary([row, _row(True, True)])
    assert out["forced_repairs"] == 1
    assert "forced_repairs" not in corpus.meshing_summary([_row(True, True)])


def test_without_the_flag_rows_are_unchanged(monkeypatch, tmp_path):
    src, sessions = _clean_seams(monkeypatch, tmp_path, probe_meshes=False)
    row = corpus._one(src, tmp_path, mesh_probe=True)
    assert sessions == [] and row["outcome"] == "nothing_to_repair"
    assert "forced_repair" not in row and "triage_route" not in row


# A ROW THE RE-INSPECTION CREDITED IS STILL NOT "REPAIRED" WHEN THE EXPORTED STEP WILL NOT MESH
# (eval wave 4, abc/0051_00513292: ending `repaired`, meshes_after False, edge_through_face).

def _scored_after(monkeypatch, tmp_path, kind_after, ending="repaired"):
    def fake_probe(path, **_):
        written = path.name.endswith("-repaired.step")
        if written and kind_after:
            return mesh_probe.ProbeResult(
                False, 0, mesh_probe.MeshFailure(kind_after, (), None, ""), {})
        failure = None if written else mesh_probe.MeshFailure("edge_through_face", (), None, "")
        return mesh_probe.ProbeResult(written, int(written), failure, {})

    monkeypatch.setattr(mesh_probe, "probe_file", fake_probe)
    monkeypatch.setattr(corpus, "_diagonal_of", lambda path: 10.0)
    (tmp_path / "p-repaired.step").write_text("x")
    row = {"outcome": "repaired", "ending": ending, "loop_ending": "repaired"}
    corpus._score_meshing(row, tmp_path / "p.step", tmp_path)
    return row


def test_a_repaired_row_whose_written_file_still_fails_to_mesh_is_unmeshable(monkeypatch,
                                                                              tmp_path):
    row = _scored_after(monkeypatch, tmp_path, "edge_through_face")
    assert row["meshes_after"] is False
    assert row["ending"] == "unmeshable"
    # ADDITIVE: the legacy re-inspection word and the loop's own word are untouched.
    assert row["outcome"] == "repaired" and row["loop_ending"] == "repaired"
    assert corpus.corpus_summary([row])["endings"] == {"unmeshable": 1}


@pytest.mark.parametrize("kind", ["timeout", "probe_error"])
def test_a_final_probe_that_never_answered_does_not_make_a_row_unmeshable(monkeypatch, tmp_path,
                                                                           kind):
    row = _scored_after(monkeypatch, tmp_path, kind)
    assert row["meshes_after"] is False
    assert row["ending"] == "repaired"


def test_a_repaired_row_that_meshes_after_stays_repaired(monkeypatch, tmp_path):
    row = _scored_after(monkeypatch, tmp_path, "")
    assert row["meshes_after"] is True and row["ending"] == "repaired"


def test_only_a_repaired_ending_is_relabelled(monkeypatch, tmp_path):
    row = _scored_after(monkeypatch, tmp_path, "edge_through_face", ending="no_change")
    assert row["ending"] == "no_change"


# THE OTHER DIRECTION (eval wave 6, bfa7c9a3): a part that NEWLY MESHES from the delivered STEP
# after kept, reviewer-accepted operations was reported `no_change`, `unmeshable` or `escalated`,
# never `repaired`. The re-inspection is geometry-only, so a mesh-only cure clears no located
# defect (`outcome` no_change); and the loop's in-session word (`unmeshable`/`escalated`) was
# carried over the delivered-STEP truth. Rows below are shaped like the real ones.

_WAVE6 = {
    # 0059_00592808 (det): loop says repaired/resolved, re-inspection no_change.
    "0059_00592808": {"outcome": "no_change", "ending": "no_change", "loop_ending": "repaired",
                      "stopped_because": "resolved", "operations_kept": 1,
                      "operations_attempted": 1},
    # 0060_00608290 (v3, det): the same shape, stopped regions_exhausted.
    "0060_00608290": {"outcome": "no_change", "ending": "no_change", "loop_ending": "repaired",
                      "stopped_because": "regions_exhausted", "operations_kept": 1,
                      "operations_attempted": 1},
    # 0059_00590755 (det): in-session scan still holds overlapping_facets, delivered meshes.
    "0059_00590755": {"outcome": "no_change", "ending": "unmeshable",
                      "loop_ending": "unmeshable", "stopped_because": "unmeshable",
                      "operations_kept": 1, "operations_attempted": 1},
    # det: another region escalated; the committed one made the delivered part mesh.
    "escalated": {"outcome": "no_change", "ending": "escalated", "loop_ending": "escalated",
                  "stopped_because": "escalated", "operations_kept": 2,
                  "operations_attempted": 5},
}


def _delivered(monkeypatch, tmp_path, row, *, before_meshes=False, after=(True, "")):
    def fake_probe(path, **_):
        if path.name.endswith("-repaired.step"):
            ok, kind = after
        else:
            ok, kind = before_meshes, "" if before_meshes else "overlapping_facets"
        failure = None if ok else mesh_probe.MeshFailure(kind, (), None, "")
        return mesh_probe.ProbeResult(ok, int(ok), failure, {})

    monkeypatch.setattr(mesh_probe, "probe_file", fake_probe)
    monkeypatch.setattr(corpus, "_diagonal_of", lambda path: 10.0)
    (tmp_path / "w-repaired.step").write_text("x")
    row = dict(row)
    corpus._score_meshing(row, tmp_path / "w.step", tmp_path)
    return row


@pytest.mark.parametrize("name", sorted(_WAVE6))
def test_a_part_that_newly_meshes_after_kept_ops_is_repaired(monkeypatch, tmp_path, name):
    original = _WAVE6[name]
    row = _delivered(monkeypatch, tmp_path, original)
    assert row["meshes_before"] is False and row["meshes_after"] is True
    assert row["ending"] == "repaired"
    # ADDITIVE: the loop's word, its reason and the re-inspection's word are untouched.
    assert row["loop_ending"] == original["loop_ending"]
    assert row["stopped_because"] == original["stopped_because"]
    assert row["outcome"] == original["outcome"]
    assert corpus.corpus_summary([row])["endings"] == {"repaired": 1}


@pytest.mark.parametrize("kind", ["timeout", "probe_error"])
def test_a_timed_out_delivered_probe_never_upgrades_to_repaired(monkeypatch, tmp_path, kind):
    for original in _WAVE6.values():
        row = _delivered(monkeypatch, tmp_path, original, after=(False, kind))
        assert row["meshes_after"] is False
        assert row["ending"] == original["ending"]


def test_a_part_that_already_meshed_is_not_upgraded(monkeypatch, tmp_path):
    row = _delivered(monkeypatch, tmp_path, _WAVE6["0059_00590755"], before_meshes=True)
    assert row["meshes_after"] is True and row["ending"] == "unmeshable"


def test_newly_meshing_with_nothing_kept_is_not_repaired(monkeypatch, tmp_path):
    original = {**_WAVE6["0059_00592808"], "operations_kept": 0}
    row = _delivered(monkeypatch, tmp_path, original)
    assert row["meshes_after"] is True and row["ending"] == "no_change"


def test_a_scan_incomplete_run_is_not_upgraded(monkeypatch, tmp_path):
    original = {"outcome": "no_change", "ending": "scan_incomplete",
                "loop_ending": "scan_incomplete", "stopped_because": "scan_incomplete",
                "operations_kept": 1, "operations_attempted": 1}
    row = _delivered(monkeypatch, tmp_path, original)
    assert row["ending"] == "scan_incomplete"
