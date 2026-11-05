# Responsibility: Verify what cfMesh must have produced before a case counts as delivered, finalized and published.
# Boundaries: completeness is asserted on the files, not on the absence of a complaint from a tool that could not run.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh import deliverable as D

#: A minimally readable single-region polyMesh: every component present and non-empty.
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


def _complete_case(root: Path) -> Path:
    pm = root / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    for component in D.POLYMESH_COMPONENTS:
        (pm / component).write_text(_BOUNDARY if component == "boundary" else "(0 0 0)\n")
    return root


# 10-12: the deliverable itself
def test_the_required_component_set_is_the_five_polymesh_files():
    assert D.POLYMESH_COMPONENTS == ("boundary", "faces", "neighbour", "owner", "points")


def test_a_complete_case_is_delivered(tmp_path):
    _complete_case(tmp_path)
    assert D.polymesh_problems(tmp_path) == ()
    assert D.is_delivered(tmp_path) is True
    assert D.delivery_problem(tmp_path) == ""


@pytest.mark.parametrize("missing", D.POLYMESH_COMPONENTS)
def test_every_required_component_is_individually_required(tmp_path, missing):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / missing).unlink()
    assert D.polymesh_problems(tmp_path) == (f"missing {missing}",)
    assert not D.is_delivered(tmp_path)
    assert missing in D.delivery_problem(tmp_path)


@pytest.mark.parametrize("emptied", D.POLYMESH_COMPONENTS)
def test_a_present_but_empty_component_is_not_a_deliverable(tmp_path, emptied):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / emptied).write_text("")
    assert D.polymesh_problems(tmp_path) == (f"empty {emptied}",)
    assert not D.is_delivered(tmp_path)


def test_the_interrupted_run_that_owner_alone_accepted(tmp_path):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")
    problems = D.polymesh_problems(tmp_path)
    assert set(problems) == {"missing boundary", "missing faces", "missing neighbour",
                             "missing points"}, problems
    assert len(problems) == 4, "the owner-only case must fail on all four remaining components"


def test_no_polymesh_at_all_is_named_as_such(tmp_path):
    assert D.polymesh_problems(tmp_path) == ("no constant/polyMesh directory",)


def test_the_deliverable_is_the_case_ROOT_not_a_region(tmp_path):
    _complete_case(tmp_path / "_elsewhere")
    region = tmp_path / "constant" / "fluid" / "polyMesh"
    region.mkdir(parents=True)
    for component in D.POLYMESH_COMPONENTS:
        (region / component).write_text("x")
    assert D.polymesh_dir(tmp_path) == tmp_path / "constant" / "polyMesh"
    assert not D.is_delivered(tmp_path), "a per-region mesh satisfied the single-region gate"


def test_cfmesh_never_consults_region_properties(tmp_path):
    src = (Path(D.__file__)).read_text()
    assert "regionProperties" not in src, (
        "the cfMesh deliverable authority reads regionProperties - that is multiregion's file")
    _complete_case(tmp_path)
    (tmp_path / "constant" / "regionProperties").write_text("regions ( fluid (a) );")
    assert D.is_delivered(tmp_path), "an irrelevant regionProperties changed the cfMesh verdict"


# 13-14: the boundary file and the types it declares
def test_the_boundary_file_parses_to_names_and_types(tmp_path):
    _complete_case(tmp_path)
    assert D.boundary_patch_names(tmp_path) == ["body", "farfield"]
    assert D.boundary_foam_types(tmp_path) == {"body": "wall", "farfield": "patch"}


def test_a_malformed_boundary_file_yields_nothing_rather_than_a_wrong_answer(tmp_path):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / "boundary").write_text("this is not a boundary file")
    assert D.boundary_patch_names(tmp_path) == []
    assert D.boundary_foam_types(tmp_path) == {}


def test_an_absent_boundary_file_is_not_an_exception(tmp_path):
    assert D.boundary_patch_names(tmp_path) == []
    assert D.boundary_foam_types(tmp_path) == {}


def test_the_load_bearing_roles_are_the_ones_that_solve_wrong_when_mistyped():
    assert D.ROLE_REQUIRED_FOAM_TYPE == {"empty": "empty", "symmetry": "symmetryPlane",
                                         "wall": "wall"}


def test_a_declared_role_whose_foam_type_is_wrong_is_rejected(tmp_path):
    _complete_case(tmp_path)
    (tmp_path / "constant" / "polyMesh" / "boundary").write_text(
        _BOUNDARY.replace("type            wall;", "type            patch;"))
    reason = D.reconcile_boundary_types(tmp_path, [{"name": "body", "type": "wall"}])
    assert reason.startswith("[BOUNDARY_TYPE_MISMATCH]")
    assert "requires OpenFOAM type 'wall'" in reason and "'patch'" in reason


def test_a_boundary_that_matches_the_contract_passes(tmp_path):
    _complete_case(tmp_path)
    assert D.reconcile_boundary_types(
        tmp_path, [{"name": "body", "type": "wall"},
                   {"name": "farfield", "type": "farfield"}]) == ""


def test_a_role_with_no_required_foam_type_is_not_second_guessed(tmp_path):
    _complete_case(tmp_path)
    assert D.reconcile_boundary_types(tmp_path, [{"name": "farfield", "type": "inlet"}]) == ""


def test_an_unreadable_boundary_does_not_silently_pass_the_type_gate(tmp_path):
    assert D.reconcile_boundary_types(tmp_path, [{"name": "body", "type": "wall"}]) == ""
    assert not D.is_delivered(tmp_path), "the deliverable gate must be the one that rejects it"


# 15-21: finalization, quality and the Reviewer fence
class _Engine:

    name = "cfmesh"
    review_surface_is_body_only = True

    def __init__(self, quality=None):
        self.quality = quality if quality is not None else {"cells": 1000, "fatal": []}
        self.manifests: list[dict] = []

    def check_mesh(self, ws, **kw):
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


def _finalize(ws, engine, patches=None, monkeypatch=None):
    # `finalize` resolves the engine through a deferred import, so the patch goes at the source.
    import meshpipeline.engines.cfmesh.finalize as fin
    import meshpipeline.engines.runtime as runtime
    monkeypatch.setattr(runtime, "get_engine", lambda name: engine)
    return fin.finalize(str(ws), patches or [], "cfmesh")


def test_a_complete_case_with_clean_quality_finalizes_and_publishes(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    eng = _Engine({"cells": 1000, "fatal": [], "max_non_ortho": 42.0})
    res = _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert res["success"] is True
    assert len(eng.manifests) == 1, "exactly one manifest is published per finalize"


def test_a_fatal_quality_result_fails_the_case(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    eng = _Engine({"cells": 1000, "fatal": ["negative-volume cells"]})
    res = _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert res["success"] is False
    assert "negative-volume cells" in res["output"]


def test_an_incomplete_case_never_reaches_quality_or_the_manifest(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")

    class _Exploding(_Engine):
        def check_mesh(self, ws, **kw):
            raise AssertionError("checkMesh was run on a partial mesh")

    eng = _Exploding()
    res = _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert res["success"] is False
    assert eng.manifests == [], "a partial mesh published a manifest"
    assert "incomplete" in res["output"] and "re-run the mesh" in res["output"]


def test_a_partial_mesh_is_not_rescued_by_a_checkmesh_that_could_not_run(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("interrupted")
    eng = _Engine({"mesh_ok": False, "fatal": []})       # what a failed checkMesh returns
    res = _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert res["success"] is False, (
        "an empty fatal list from a checkMesh that read nothing was taken for a good mesh")


def test_the_manifest_publishes_the_same_quality_facts_that_were_measured(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    measured = {"cells": 4242, "fatal": [], "max_non_ortho": 61.5, "max_skewness": 3.1}
    eng = _Engine(dict(measured))
    _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    published = eng.manifests[0]["quality"]
    for key, value in measured.items():
        assert published[key] == value, f"the manifest contradicts its own measurement of {key}"


def test_the_manifest_states_metres_and_applies_no_conversion(tmp_path, monkeypatch):
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT

    _complete_case(tmp_path)
    eng = _Engine()
    _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert eng.manifests[0]["mesh_units"] == COMPLETED_MESH_UNIT.value


def test_the_manifest_records_the_engine_that_actually_built_the_mesh(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    eng = _Engine()
    _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert eng.manifests[0]["mesh_mode"] == "cfmesh"


def test_the_requested_domain_box_is_republished_not_remeasured(tmp_path, monkeypatch):
    _complete_case(tmp_path)
    (tmp_path / "geom_box.json").write_text(json.dumps(
        {"domain_min": [-1.0, -2.0, -3.0], "domain_max": [4.0, 5.0, 6.0]}))
    eng = _Engine()
    _finalize(tmp_path, eng, monkeypatch=monkeypatch)
    assert eng.manifests[0]["requested_box"] == [[-1.0, -2.0, -3.0], [4.0, 5.0, 6.0]]


def test_finalization_is_load_bearing_and_has_exactly_one_implementation():
    import meshpipeline.engines.cfmesh.cfmesh_runner as runner
    import meshpipeline.engines.cfmesh.finalize as fin
    from meshpipeline.engines.runtime import get_engine

    assert get_engine("cfmesh").finalize is fin.finalize
    assert runner.finalize is fin.finalize, "the engine namespace points at a different finalize"

    import ast
    import inspect
    # the runner must not re-implement the deliverable or quality verdict
    src = inspect.getsource(runner)
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not (names & {"finalize", "reconcile_boundary_types", "polymesh_problems"}), (
        f"the runner carries its own copy of finalization policy: {names}")
