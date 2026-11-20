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
