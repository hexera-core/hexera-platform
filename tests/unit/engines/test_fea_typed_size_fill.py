# Responsibility: Verify MESH-25's exception is only what it says - a typed overall size on a ONE-part model is the mesh's base size and never a size held through a volume, so the fill stays HXT; an assembly whose parts are held at DIFFERENT sizes still gets each part's size (filled by Delaunay); nothing typed plans exactly as before.
# Boundaries: the filleted bracket built in the test (tests/fea_fixtures.py), measured and planned in-process, and two small gmsh models; no model call, no mesh runner.
from __future__ import annotations

import pytest

pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter as A  # noqa: E402


@pytest.fixture(scope="module")
def bracket(tmp_path_factory):
    import gmsh

    ws = tmp_path_factory.mktemp("bracket")
    src = fx.write_step(fx.bracket(4.0), ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return FD.measure_staged(ws)


def _global(mm: float) -> dict:
    req = MR.empty()
    req["sizes"]["global_mm"] = mm
    return MR.section("used", requirements=req)


def _fields(plan: dict, part_volumes=None, vols=(1,)):
    """size_fields on a box per volume; returns whether a size is held through a volume (MESH-25's exception)."""
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        for i, _v in enumerate(vols):
            gmsh.model.occ.addBox(0.1 * i, 0, 0, 0.1, 0.1, 0.1)
        if len(vols) > 1:
            gmsh.model.occ.fragment([(3, 1)], [(3, v) for v in vols[1:]])
        gmsh.model.occ.synchronize()
        surfaces = {t for _d, t in gmsh.model.getEntities(2)}
        A._VOLUME_SIZED[0] = False
        A.size_fields(gmsh, plan, surfaces, part_volumes=part_volumes)
        return A._VOLUME_SIZED[0]
    finally:
        gmsh.finalize()


def test_a_typed_overall_size_on_one_part_is_its_base_and_no_size_through_a_volume(bracket):
    """The owner's knuckle (26 Nov, "3mm global cell size"): the typed size is the plan's base, no part zone holds a
    size through the volume, so MESH-25's exception never turns HXT off for it."""
    record, surface, diag = bracket
    sizes, _scaled, _c = FD.requirement_sizes(record, _global(3.0), {"status": "none"}, diag)
    plan = FD.choose_plan(record, surface, diag, sizes=sizes or None)
    assert plan["base_size_m"] == pytest.approx(0.003)
    assert not [z for z in plan["zones"] if z.get("kind") == "part"]
    assert _fields(plan) is False
    assert A.algorithm_3d(plan.get("mesh") or {}) == "hxt"


def test_nothing_typed_plans_as_before(bracket):
    record, surface, diag = bracket
    sizes, _scaled, _c = FD.requirement_sizes(record, None, {"status": "none"}, diag)
    assert not sizes
    assert FD.choose_plan(record, surface, diag) == FD.choose_plan(record, surface, diag, sizes=None)


def test_an_assembly_whose_parts_have_different_sizes_holds_each_through_its_volume():
    plan = {"base_size_m": 0.02, "growth": 1.3,
            "zones": [{"id": "P1", "kind": "part", "part": "Part_1", "faces": ["Part_1_Face_1"], "size_m": 0.02,
                       "reach_m": 0.0},
                      {"id": "P2", "kind": "part", "part": "Part_2", "faces": ["Part_2_Face_1"], "size_m": 0.005,
                       "reach_m": 0.0}]}
    assert _fields(plan, part_volumes={"Part_1": [1], "Part_2": [2]}, vols=(1, 2)) is True
    # the same size on every part (a typed overall size on an assembly) is the base: nothing held through a volume
    same = {**plan, "zones": [{**z, "size_m": 0.02} for z in plan["zones"]]}
    assert _fields(same, part_volumes={"Part_1": [1], "Part_2": [2]}, vols=(1, 2)) is False
