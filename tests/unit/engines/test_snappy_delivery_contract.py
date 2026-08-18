# Responsibility: Verify what snappyHexMesh must have produced before a case is delivered, finalized and published.
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from tests._scan import scanned

import meshpipeline.engines.snappy.finalize as F
from meshpipeline.engines.snappy.parallel_stages import REQUIRED_POLYMESH, polymesh_complete

_BOUNDARY = """FoamFile { version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
2
(
body
{
    type            wall;
    nFaces          120;
    startFace       0;
}
farfield
{
    type            patch;
    nFaces          80;
    startFace       120;
}
)
"""


def _complete_case(root: Path, *, with_marker: bool = True) -> Path:
    if with_marker:
        (root / "blockMesh.log").write_text("blockMesh finished\n")
        time.sleep(0.01)            # the mesh is written after the attempt starts
    pm = root / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    for component in REQUIRED_POLYMESH:
        (pm / component).write_text(_BOUNDARY if component == "boundary" else "(0 0 0)\n")
    return root


# 1-6: the deliverable itself
def test_the_required_component_set_is_snappys_own_declared_set():
    assert set(REQUIRED_POLYMESH) == {"points", "faces", "owner", "neighbour", "boundary"}
    assert len(REQUIRED_POLYMESH) == 5


def test_a_complete_case_is_delivered(tmp_path):
    _complete_case(tmp_path)
    ok, problems = polymesh_complete(tmp_path, since=F._attempt_started(tmp_path))
    assert ok and problems == []


@pytest.mark.parametrize("missing", REQUIRED_POLYMESH)
def test_every_required_component_is_individually_required(tmp_path, missing):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / missing).unlink()
    ok, problems = polymesh_complete(tmp_path)
    assert not ok and problems == [f"missing {missing}"]


@pytest.mark.parametrize("emptied", REQUIRED_POLYMESH)
def test_a_present_but_empty_component_is_not_a_deliverable(tmp_path, emptied):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / emptied).write_text("")
    ok, problems = polymesh_complete(tmp_path)
    assert not ok and problems == [f"empty {emptied}"]


def test_the_interrupted_run_that_owner_alone_accepted(tmp_path):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")
    ok, problems = polymesh_complete(tmp_path)
    assert not ok
    assert set(problems) == {"missing points", "missing faces", "missing neighbour",
                             "missing boundary"}
    assert len(problems) == 4


def test_a_directory_masquerading_as_a_component_is_refused(tmp_path):
    _complete_case(tmp_path)
    comp = tmp_path / "constant" / "polyMesh" / "points"
    comp.unlink()
    comp.mkdir()
    ok, problems = polymesh_complete(tmp_path)
    assert not ok and problems == ["missing points"]


def test_output_from_a_previous_attempt_is_refused(tmp_path):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    for component in REQUIRED_POLYMESH:
        (pm / component).write_text("from a previous attempt")
    stale = time.time() - 3600
    for component in REQUIRED_POLYMESH:
        os.utime(pm / component, (stale, stale))
    (tmp_path / "blockMesh.log").write_text("this attempt started now\n")

    ok, problems = polymesh_complete(tmp_path, since=F._attempt_started(tmp_path))
    assert not ok
    assert all(p.startswith("stale ") for p in problems), problems
    assert len(problems) == 5, "every component of the previous attempt must be named"


def test_staleness_is_not_asserted_when_the_attempt_marker_is_absent(tmp_path):
    _complete_case(tmp_path, with_marker=False)
    assert F._attempt_started(tmp_path) is None
    ok, _ = polymesh_complete(tmp_path, since=F._attempt_started(tmp_path))
    assert ok


def test_the_deliverable_is_the_case_root_not_a_region(tmp_path):
    region = tmp_path / "constant" / "fluid" / "polyMesh"
    region.mkdir(parents=True)
    for component in REQUIRED_POLYMESH:
        (region / component).write_text("x")
    (tmp_path / "constant" / "regionProperties").write_text("regions ( fluid (a) );")
    ok, _ = polymesh_complete(tmp_path)
    assert not ok, "a per-region mesh satisfied the single-region gate"

    _complete_case(tmp_path)
    ok, _ = polymesh_complete(tmp_path, since=F._attempt_started(tmp_path))
    assert ok, "an irrelevant regionProperties changed the snappy verdict"


# 7-13: finalization, quality and the Reviewer fence
class _Engine:
    name = "snappy"
    review_surface_is_body_only = True

    def __init__(self, quality=None):
        self.quality = quality if quality is not None else {"cells": 1000, "fatal": []}
        self.manifests: list[dict] = []
        self.check_mesh_calls = 0

    def check_mesh(self, ws, **kw):
        self.check_mesh_calls += 1
        return dict(self.quality)

    def read_stl_solids(self, stl):
        return {}

    def build_review_msh(self, ws, solids):
        return {}, (0.0,) * 6

    def export_volume_vtk(self, ws):
        return None

    def inspect_stl(self, ws):
        return {}

    def write_manifest(self, ws, **kw):
        self.manifests.append(kw)


def _finalize(ws, engine, monkeypatch, patches=None):
    import meshpipeline.engines.runtime as runtime
    monkeypatch.setattr(runtime, "get_engine", lambda name: engine)
    return F.finalize(str(ws), patches or [], "snappy")


def test_a_complete_case_with_clean_quality_finalizes_and_publishes(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    eng = _Engine({"cells": 1000, "fatal": [], "skew_fraction": 0.001})
    res = _finalize(tmp_path, eng, monkeypatch)
    assert res["success"] is True
    assert len(eng.manifests) == 1, "exactly one manifest is published per finalize"


def test_a_partial_case_never_reaches_quality_or_the_manifest(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")
    eng = _Engine()
    res = _finalize(tmp_path, eng, monkeypatch)
    assert res["success"] is False
    assert eng.check_mesh_calls == 0, "checkMesh was run on a partial mesh"
    assert eng.manifests == [], "a partial mesh published a manifest"


def test_a_partial_mesh_is_not_rescued_by_a_checkmesh_that_could_not_run(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")
    eng = _Engine({"mesh_ok": False, "fatal": []})
    assert _finalize(tmp_path, eng, monkeypatch)["success"] is False


def test_the_diagnostic_names_the_incomplete_deliverable_and_the_engine(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("x")
    out = _finalize(tmp_path, _Engine(), monkeypatch)["output"]
    assert out.startswith("[SNAPPY]"), f"the diagnostic does not name snappy: {out[:40]}"
    assert "CFMESH" not in out, "snappy reports a cfMesh diagnostic"
    for component in ("points", "faces", "neighbour", "boundary"):
        assert component in out, f"the diagnostic does not name the missing {component}"


def test_a_fatal_quality_result_fails_the_case(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    eng = _Engine({"cells": 1000, "fatal": ["negative-volume cells"]})
    res = _finalize(tmp_path, eng, monkeypatch)
    assert res["success"] is False and "negative-volume cells" in res["output"]


def test_snappys_gating_quality_bars_are_unchanged():
    from meshpipeline.engines.snappy.criteria import CRITERIA_ROWS

    gating = {c.key for c in CRITERIA_ROWS if c.gating}
    assert {"fatal", "skew_fraction", "wall_faces"} <= gating, gating


def test_the_manifest_publishes_the_measured_quality_and_states_metres(tmp_path, monkeypatch):
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT

    _complete_case(tmp_path)
    measured = {"cells": 4242, "fatal": [], "skew_fraction": 0.002, "max_non_ortho": 61.5}
    eng = _Engine(dict(measured))
    _finalize(tmp_path, eng, monkeypatch)
    published = eng.manifests[0]
    for key, value in measured.items():
        assert published["quality"][key] == value, f"the manifest contradicts its own {key}"
    assert published["mesh_units"] == COMPLETED_MESH_UNIT.value
    assert published["mesh_mode"] == "snappy"


def test_finalization_has_exactly_one_implementation():
    import ast
    import inspect

    import meshpipeline.engines.snappy.snappy_runner as runner
    from meshpipeline.engines.runtime import get_engine

    assert get_engine("snappy").finalize is F.finalize
    names = {n.name for n in ast.parse(inspect.getsource(runner)).body
             if isinstance(n, ast.FunctionDef)}
    assert "finalize" not in names, "the runner carries a second finalization path"


def test_snappy_reaches_no_other_engines_internals_for_this_gate():
    import ast

    src_root = Path(F.__file__).parents[1]
    offenders = []
    for py in scanned(sorted((src_root / "snappy").rglob("*.py")), "the snappy bundle modules"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8", errors="replace"))):
            mod = (node.module or "") if isinstance(node, ast.ImportFrom) else (
                node.names[0].name if isinstance(node, ast.Import) else "")
            for other in ("cfmesh", "snappy_multiregion", "gmsh", "vmtk"):
                if mod.startswith(f"meshpipeline.engines.{other}."):
                    offenders.append(f"{py.name}:{node.lineno} -> {mod}")
    assert not offenders, f"snappy reaches into another engine: {offenders}"
