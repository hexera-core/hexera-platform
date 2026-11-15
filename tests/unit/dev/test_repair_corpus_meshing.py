# Responsibility: Verify the corpus runner scores "does the file mesh" before and after repair.
# Boundaries: hermetic - the gmsh probe and the repair are stubbed; no CAD is read.
from __future__ import annotations

import importlib.util
from pathlib import Path

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
