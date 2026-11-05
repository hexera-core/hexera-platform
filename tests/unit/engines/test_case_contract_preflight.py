# Responsibility: Verify the pre-flight reads each engine's written case the way its mesher will, refuses a case that does not build the approved patches before any mesher starts, and that the refusal ends as an internal error naming the mismatch.
# Boundaries: the case reader, the launch seam, the executor's handling of a recorded refusal, and the admission rules that keep an undeliverable declaration from ever reaching a case writer.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meshpipeline.cad.stl_io import _box_triangles, _write_solid
from meshpipeline.contracts import mesh_execution
from meshpipeline.engines import case_contract
from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
from meshpipeline.engines.registry import get_spec

ANALYSIS = {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [1.0, 0.4, 0.3], "L": 1.0,
            "extent": [1.0, 0.4, 0.3]}
REC = {"base_cell": 0.1, "surface_level": [3, 3], "afford_level": 3, "feature_level": 4,
       "distance_bands": [(0.2, 3), (0.8, 2)], "resolve_feature_angle": 30}


def _contract(ws: Path, patches: list[tuple[str, str]]) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "patches_contract.txt").write_text(
        "Patches:\n" + "\n".join(f"  • {n}  →  type {t}" for n, t in patches) + "\n")


def _snappy_case(ws: Path, *, wall="car", farfield="farfield", ground=None, parts=None,
                 walls=None, surface_regions=True):
    from meshpipeline.engines.snappy import snappy_runner as R
    (ws / "system").mkdir(parents=True, exist_ok=True)
    with (ws / "input.stl").open("w") as fh:
        for i, name in enumerate(parts or ["body"]):
            _write_solid(fh, name, _box_triangles([1.2 * i, 0, 0], [1.2 * i + 1, 0.4, 0.3]))
    prep = R.prepare_surface(ws, geometry_file="input.stl", wall_patch=wall, body_walls=walls,
                             domain_min=[-5, -5, 0], domain_max=[5, 5, 5])
    R.render_snappy_case(ws, surface_name=prep["surface_name"], feature_file=prep["feature_file"],
                         analysis=ANALYSIS, recommendation=REC, domain_min=[-5, -5, 0],
                         domain_max=[5, 5, 5], ground=ground, farfield=farfield,
                         surface_regions=(prep["surface_regions"] or None)
                         if surface_regions else None, class_regions=False)
    return prep


# the case writers name what was approved

def test_the_box_writes_the_far_field_under_its_declared_name(tmp_path):
    _snappy_case(tmp_path, farfield="freestream")
    bm = (tmp_path / "system" / "blockMeshDict").read_text()
    assert "freestream { type patch; faces (" in bm and "farfield" not in bm


def test_one_declared_wall_on_a_many_part_surface_is_one_wall(tmp_path):
    # before: the parts were written under the file's own names and the approved wall had no faces
    prep = _snappy_case(tmp_path, wall="car_wall", parts=["Car Body", "wheels", "mirrors"])
    assert prep["surface_regions"] == []
    stl = (tmp_path / "constant" / "triSurface" / "car_wall.stl").read_text()
    assert [ln for ln in stl.splitlines() if ln.startswith("solid ")] == ["solid car_wall"]


def test_several_declared_walls_keep_each_part_under_the_declared_spelling(tmp_path):
    prep = _snappy_case(tmp_path, wall="wing_left", walls=["wing_left", "Fuselage"],
                        parts=["Wing Left", "FUSELAGE"])
    assert prep["surface_regions"] == ["wing_left", "Fuselage"]
    shm = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    assert "regions { wing_left { name wing_left; } Fuselage { name Fuselage; } }" in shm
    assert not (tmp_path / "system" / "createPatchDict").exists()   # never folded together


def test_cfmesh_stages_a_many_part_surface_as_the_declared_wall(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as C
    with (tmp_path / "input.stl").open("w") as fh:
        for i, name in enumerate(["Car Body", "wheels"]):
            _write_solid(fh, name, _box_triangles([1.2 * i, 0, 0], [1.2 * i + 1, 0.4, 0.3]))
    orig = C._to_fms
    try:
        C._to_fms = lambda *_a, **_k: "geom.stl"   # the surfaceFeatureEdges subprocess
        C.prepare_surface(tmp_path, geometry_file="input.stl", domain_min=[-5] * 3,
                          domain_max=[5] * 3, wall_patch="car_body", farfield_patch="far_field",
                          body_walls=["car_body"])
    finally:
        C._to_fms = orig
    assert case_contract.stl_solid_names(tmp_path / "geom.stl") == ["car_body", "far_field"]


# the pre-flight reads a written case the way its mesher will

def test_it_reads_a_grounded_external_snappy_case(tmp_path):
    _snappy_case(tmp_path, farfield="freestream", ground="ground")
    case = case_contract.read_case_boundary(tmp_path, "snappy")
    assert case.patches == {"freestream": "patch", "ground": "wall", "car": "wall"}
    _contract(tmp_path, [("car", "wall"), ("ground", "wall"), ("freestream", "farfield")])
    assert case_contract.check(tmp_path, "snappy")[0] == []


def test_undeclared_regions_of_a_many_part_surface_read_as_surface_underscore_part(tmp_path):
    # the naming snappyHexMesh gives regions a dict does not declare: the pre-flight must see
    # that no approved wall would carry faces
    _snappy_case(tmp_path, wall="wing", walls=["wing", "tail"], parts=["wing", "tail"],
                 surface_regions=False)
    case = case_contract.read_case_boundary(tmp_path, "snappy")
    assert {"wing_wing", "wing_tail"} <= set(case.patches)


def test_it_names_every_departure(tmp_path):
    _snappy_case(tmp_path, farfield="farfield")
    _contract(tmp_path, [("car", "wall"), ("freestream", "farfield")])
    problems, case = case_contract.check(tmp_path, "snappy")
    assert case is not None
    assert any("'freestream'" in p and "not written" in p for p in problems)
    assert any("'farfield', which no approved patch names" in p for p in problems)


def test_a_wall_written_as_an_open_patch_is_a_departure(tmp_path):
    case = case_contract.CaseBoundary(patches={"car": "patch", "farfield": "patch"})
    problems = case_contract.compare([{"name": "car", "type": "wall"},
                                      {"name": "farfield", "type": "farfield"}], case)
    assert problems == ["'car' is written as OpenFOAM type patch, but was approved as a wall"]


def test_an_engine_it_does_not_cover_is_not_judged(tmp_path):
    _contract(tmp_path, [("car", "wall")])
    assert case_contract.check(tmp_path, "snappy_multiregion") == ([], None)
    # a vmtk lumen uploaded as-is: the engine staged no ports, so nothing names the caps yet
    assert case_contract.check(tmp_path, "vmtk") == ([], None)


@pytest.mark.parametrize("engine,author", [("snappy", "renderer"), ("cfmesh", "renderer"),
                                           ("gmsh", "builder")])
def test_a_covered_case_it_cannot_read_is_refused_not_waved_through(tmp_path, engine, author):
    # Greptile on #88: an unreadable case produced no problems, so the mesher started anyway and
    # the early diagnosis this exists for was lost
    _contract(tmp_path, [("car", "wall"), ("farfield", "farfield")])
    problems, case = case_contract.check(tmp_path, engine)
    assert problems and "could not be read" in problems[0], problems
    assert case is not None and case.authored_by == author


def test_a_snappy_surface_it_does_not_understand_is_refused(tmp_path):
    _snappy_case(tmp_path)
    shm = tmp_path / "system" / "snappyHexMeshDict"
    shm.write_text(shm.read_text().replace("type triSurfaceMesh", "type searchableBox"))
    _contract(tmp_path, [("car", "wall"), ("farfield", "farfield")])
    problems, _case = case_contract.check(tmp_path, "snappy")
    assert any("not a triSurfaceMesh" in p for p in problems), problems


# gmsh groups with role free

def _gmsh(tmp_path, declared, groups):
    _contract(tmp_path, declared)
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": [
        {"name": n, "role": r, "surface_tags": [i + 1]} for i, (n, r) in enumerate(groups)]}))
    return case_contract.check(tmp_path, "gmsh")[0]


def test_an_approved_free_group_the_spec_leaves_out_is_a_departure(tmp_path):
    # Greptile on #88: a named `free` group the user approved was skipped, so a deck without it
    # passed both the pre-flight and the patch gate
    problems = _gmsh(tmp_path, [("base", "fixed"), ("top", "load"), ("outer_skin", "free")],
                     [("base", "fixed"), ("top", "load")])
    assert any("'outer_skin'" in p and "not written" in p for p in problems), problems


def test_the_leftover_free_group_nobody_named_is_not_an_extra(tmp_path):
    assert _gmsh(tmp_path, [("base", "fixed"), ("top", "load")],
                 [("base", "fixed"), ("top", "load"), ("free", "free")]) == []


def _gmsh_gate(tmp_path, declared, delivered):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_gmsh_region_contract
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({
        "patch_types": dict(delivered), "patches": {n: [] for n, _r in delivered}}))
    ctx = GateCtx(workspace=tmp_path, engine="gmsh",
                  intake_patches=[{"name": n, "type": r} for n, r in declared])
    return _gate_gmsh_region_contract(ctx)[0]


def test_the_gmsh_gate_holds_an_approved_free_group_to_the_deck(tmp_path):
    declared = [("base", "fixed"), ("outer_skin", "free")]
    assert _gmsh_gate(tmp_path, declared, [("base", "fixed"), ("free", "free")]) is False
    assert _gmsh_gate(tmp_path, declared, [("base", "fixed"), ("outer_skin", "free"),
                                           ("free", "free")]) is True
    # nothing declared as free: the leftover default group is still not an extra
    assert _gmsh_gate(tmp_path, [("base", "fixed")], [("base", "fixed"), ("free", "free")]) is True


# the launch seam

class _Executor:
    def __init__(self):
        self.calls = 0

    def run(self, workspace, *, engine, timeout):
        self.calls += 1
        return {"rc": 0, "timed_out": False, "log_tail": ""}


@pytest.fixture
def seam(monkeypatch):
    ex = _Executor()
    monkeypatch.setattr(mesh_execution, "_executor", ex)
    monkeypatch.setattr(mesh_execution, "_launch_check", case_contract.launch_check)
    return ex


def test_a_renderer_case_that_drops_an_approved_patch_never_reaches_the_mesher(tmp_path, seam):
    _snappy_case(tmp_path, farfield="farfield")
    _contract(tmp_path, [("car", "wall"), ("freestream", "farfield")])
    res = mesh_execution.run_mesh(tmp_path, engine="snappy", timeout=10)
    assert seam.calls == 0
    assert res["rc"] == mesh_execution.RC_CASE_CONTRACT and res["internal_defect"] is True
    assert "[CASE_CONTRACT_MISMATCH]" in res["log_tail"] and "freestream" in res["log_tail"]
    refusal = case_contract.refusal_of(tmp_path)
    assert refusal and refusal["engine"] == "snappy"


def test_a_matching_case_launches_and_clears_an_old_refusal(tmp_path, seam):
    _snappy_case(tmp_path, farfield="freestream")
    _contract(tmp_path, [("car", "wall"), ("freestream", "farfield")])
    (tmp_path / case_contract.REFUSAL_FACT).write_text(json.dumps({"problems": ["old"]}))
    assert mesh_execution.run_mesh(tmp_path, engine="snappy", timeout=10)["rc"] == 0
    assert seam.calls == 1 and case_contract.refusal_of(tmp_path) is None


def test_a_builder_spec_that_misnames_a_group_goes_back_to_the_builder(tmp_path, seam):
    _contract(tmp_path, [("fixed_support", "fixed"), ("load_face", "load")])
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"groups": [
        {"name": "fixed_support", "role": "fixed", "surface_tags": [1]},
        {"name": "load face", "role": "load", "surface_tags": [2]}]}))
    res = mesh_execution.run_mesh(tmp_path, engine="gmsh", timeout=10)
    assert seam.calls == 0 and res["internal_defect"] is False
    assert case_contract.refusal_of(tmp_path) is None          # the model's to fix, not ours
    from meshpipeline.agents.builder.tools.meshing import (
        PreparedMeshRun,
        execute_prepared_mesh_run,
    )

    class _Ctx:
        workspace = tmp_path
    out = execute_prepared_mesh_run(_Ctx(), PreparedMeshRun(engine="gmsh", cap=10))
    assert out["success"] is False and out["patch_contract_mismatch"] is True
    assert "Fix the group names" in out["guidance"] and "load_face" in out["guidance"]


def test_the_composition_root_installs_the_pre_flight():
    src = (Path(__file__).resolve().parents[3] / "src" / "meshpipeline" / "runtime"
           / "composition.py").read_text()
    assert "mesh_execution.set_launch_check(launch_check)" in src


def test_the_executor_ends_a_refused_case_as_a_contract_mismatch_naming_it(tmp_path, monkeypatch):
    # The refusal is reported as the gate it stands in for, with its cause - so the user reads the
    # mismatch and the retry policy ends the run (a re-render writes the same case) - and the
    # operator record it always carried stays: a dead letter naming it as an INTERNAL defect.
    import meshpipeline.errors as errors
    import meshpipeline.pipeline.executor as ex
    from meshpipeline.application import execution_fence
    from meshpipeline.contracts.failure_cause import retry_can_help
    from meshpipeline.errors import FailureClass

    class _Pub:
        def __getattr__(self, name):
            async def _any(*a, **k):
                return None
            return _any

    async def _owner(*_a, **_k):
        return None
    monkeypatch.setattr(ex, "execution_publisher", lambda *a, **k: _Pub())
    monkeypatch.setattr(execution_fence, "assert_current_owner", _owner)
    dead: list = []
    monkeypatch.setattr(errors, "record_dead_letter", lambda *a, **k: dead.append(a))
    monkeypatch.setattr(ex, "TrainingLogger",
                        type("_TL", (), {"__init__": lambda s, j: None,
                                         "log": lambda s, *a, **k: None}))
    (tmp_path / case_contract.REFUSAL_FACT).write_text(json.dumps(
        {"engine": "snappy", "problems": ["the approved patch 'freestream' is not written"]}))
    out = asyncio.run(ex.node_executor({"job_id": "j", "openfoam_workspace": str(tmp_path),
                                        "engine": "snappy"}))
    assert out["executor_success"] is False and out["executor_failed_gate"] == "patch_contract"
    assert out["executor_failure_cause"] == "contract_mismatch"
    assert "freestream" in out["executor_failure_facts"]["problems"][0]
    assert not retry_can_help(out["executor_failure_cause"], out["executor_failure_facts"])
    assert dead and dead[0][1] is FailureClass.INTERNAL and "freestream" in dead[0][3]


def test_vmtk_draws_the_wall_under_its_declared_name(tmp_path, monkeypatch):
    # vmtk's wall is entity 1, drawn "wall"; the manifest listed "lumen_wall" with nothing on it
    # beside an undeclared "wall" the reviewer was never told about
    from types import SimpleNamespace

    from meshpipeline.engines.vmtk import vmtk_runner as V

    drawn: dict = {}
    (tmp_path / "mesh.vtu").write_text("<VTKFile/>")
    monkeypatch.setattr(V, "check_mesh", lambda ws: {"cells": 10, "fatal": [], "mesh_ok": True})
    monkeypatch.setattr(V, "_read_surface",
                        lambda p: SimpleNamespace(bounds=(0, 1, 0, 1, 0, 1)))
    monkeypatch.setattr("meshpipeline.engines.vmtk.viewer_surface.surface_patches",
                        lambda ws, named=False: {"wall": [[(0, 0, 0)] * 3],
                                                 "inlet": [[(0, 0, 0)] * 3]})

    def _review(ws, patches):
        drawn.update(patches)
        (Path(ws) / "mesh.msh").write_text("$MeshFormat\n")
        return {n: [1] for n in patches}, (0,) * 6
    monkeypatch.setattr("meshpipeline.render.review_artifacts.build_review_msh", _review)
    V.finalize(str(tmp_path), [{"name": "lumen_wall", "type": "wall"},
                               {"name": "inlet", "type": "inlet"}], "vmtk")
    assert set(drawn) == {"lumen_wall", "inlet"}
    manifest = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert set(manifest["patches"]) == {"lumen_wall", "inlet"}


# what admission refuses so no case writer is handed an undeliverable declaration

def _codes(engine, purpose, patches, *, dim="3D", ik="body-surface", facts=None):
    ev = AdmissionEvidence(engine=engine, purpose=purpose, input_kind=ik, dimensionality=dim,
                           surface_analysis=facts,
                           patches=tuple(PatchSummary(n, t) for n, t in patches))
    return {r.code for r in get_spec(engine).admit(ev)}


@pytest.mark.parametrize("engine", ["snappy", "cfmesh"])
def test_a_box_that_is_one_surface_takes_one_far_field(engine):
    assert "boundary_count_unsupported" in _codes(
        engine, "external_cfd", [("body", "wall"), ("in", "farfield"), ("out", "farfield")])
    assert "boundary_count_unsupported" not in _codes(
        "gmsh", "external_cfd", [("body", "wall"), ("in", "farfield"), ("out", "farfield")],
        ik="fluid-domain")


def test_cfmesh_2d_takes_one_empty_patch():
    assert "boundary_count_unsupported" in _codes(
        "cfmesh", "external_cfd", [("af", "wall"), ("ff", "farfield"), ("front", "empty"),
                                   ("back", "empty")], dim="2D")


@pytest.mark.parametrize("engine,ik", [("snappy", "solid-body"), ("cfmesh", "body-surface"),
                                       ("vmtk", "body-surface")])
def test_a_carved_duct_has_one_wall(engine, ik):
    codes = _codes(engine, "internal_cfd", [("pipe", "wall"), ("flange", "wall"),
                                            ("in", "inlet"), ("out", "outlet")], ik=ik)
    assert "multiple_wall_patches_unsupported" in codes


def test_no_symmetry_plane_inside_a_carved_duct():
    assert "boundary_count_unsupported" in _codes(
        "snappy", "internal_cfd", [("pipe", "wall"), ("in", "inlet"), ("out", "outlet"),
                                   ("mid", "symmetry")], ik="solid-body")


def test_parts_a_step_names_are_not_promised_by_engines_that_flatten_it():
    facts = {"region_names": ["wing", "fuselage"], "region_count": 2,
             "region_source": "assembly"}
    walls = [("wing", "wall"), ("fuselage", "wall"), ("farfield", "farfield")]
    assert "multiple_wall_patches_unsupported" in _codes("snappy", "external_cfd", walls,
                                                         facts=facts)
    assert "multiple_wall_patches_unsupported" not in _codes(
        "snappy", "external_cfd", walls,
        facts={**facts, "region_source": "stl-solids"})


def test_a_part_matches_the_declared_wall_it_became_at_the_boundary():
    # "Wing Left" in the file, declared "Wing Left" and made mesh-safe as Wing_Left at intake
    facts = {"region_names": ["Wing Left", "fuselage"], "region_count": 2,
             "region_source": "stl-solids"}
    assert "multiple_wall_patches_unsupported" not in _codes(
        "snappy", "external_cfd", [("Wing_Left", "wall"), ("Fuselage", "wall"),
                                   ("farfield", "farfield")], facts=facts)


@pytest.mark.parametrize("engine", ["snappy", "cfmesh"])
def test_a_named_part_no_declared_wall_takes_is_asked_about_at_intake(engine):
    # Greptile on #88: two walls matching two of three named parts were admitted, the third part
    # was staged as a patch nobody approved, and the pre-flight ended the job as an internal error
    facts = {"region_names": ["wing", "fuselage", "tail"], "region_count": 3,
             "region_source": "stl-solids"}
    walls = [("wing", "wall"), ("fuselage", "wall"), ("farfield", "farfield")]
    ev = AdmissionEvidence(engine=engine, purpose="external_cfd", input_kind="body-surface",
                           dimensionality="3D", surface_analysis=facts,
                           patches=tuple(PatchSummary(n, t) for n, t in walls))
    rej = [r for r in get_spec(engine).admit(ev) if r.code == "multiple_wall_patches_unsupported"]
    assert rej and "tail would become a patch nobody approved" in rej[0].message, rej
    assert "ONE wall patch" in rej[0].message
    # every part named by a declared wall: admitted
    assert "multiple_wall_patches_unsupported" not in _codes(
        engine, "external_cfd", [*walls[:2], ("tail", "wall"), ("farfield", "farfield")],
        facts=facts)


def test_an_engine_that_groups_surfaces_itself_leaves_extra_parts_to_its_default_group():
    facts = {"region_names": ["wing", "fuselage", "tail"], "region_count": 3,
             "region_source": "stl-solids"}
    assert "multiple_wall_patches_unsupported" not in _codes(
        "gmsh", "external_cfd", [("wing", "wall"), ("fuselage", "wall"), ("farfield", "farfield")],
        ik="fluid-domain", facts=facts)


def test_a_reserved_name_is_refused_by_every_engine():
    for engine in ("snappy", "cfmesh", "gmsh", "vmtk", "snappy_multiregion"):
        assert "patch_name_reserved" in _codes(engine, "external_cfd",
                                               [("outer", "wall"), ("farfield", "farfield")])
