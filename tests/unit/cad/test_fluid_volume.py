# Responsibility: Verify the fluid an assembly holds is found as the space inside its parts, closed
# off by flat caps where it meets the outside: a pipe drawn as two touching halves is one passage
# with its two end openings, a valve drawn shut is two spaces with one opening each, a wall with a
# hole drilled through its curved side leaks (no passage, said why), a lid a hair short of its body
# leaves a gap that is never proposed, and the staged surface is closed, named by the declaration
# and seeded inside the fluid. Also the geometry check's folding of what was found.
# Boundaries: OpenCASCADE primitives built in the test (metres for the split, millimetres in a STEP
# file for the staging hook); no engine, no storage, no model.
import math

import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

import numpy as np

from meshpipeline.cad import fluid_volume as fv
from meshpipeline.cad.fluid_volume_check import fold, wants_fluid_volume

R_OUT, R_BORE, L = 0.05, 0.04, 0.2          # m: the pipe


def _cyl(x0, r, length, axis=(1, 0, 0), y=0.0, z=0.0):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x0, y, z), gp_Dir(*axis)), r, length).Shape()


def _tube(x0, length):
    return BRepAlgoAPI_Cut(_cyl(x0, R_OUT, length), _cyl(x0 - 0.001, R_BORE, length + 0.002)).Shape()


def _comp(*shapes):
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _two_halves():
    return _comp(_tube(0.0, L / 2), _tube(L / 2, L / 2))


def test_a_pipe_of_two_touching_parts_is_one_passage_with_its_two_ends():
    x = fv.extract(_two_halves())
    assert x.found, x.reason
    assert len(x.passages) == 1
    p = x.passages[0]
    assert p.volume == pytest.approx(math.pi * R_BORE ** 2 * L, rel=0.01)
    ends = sorted(p.main_ports, key=lambda o: o.centroid[0])
    assert len(ends) == 2
    assert ends[0].centroid[0] == pytest.approx(0.0, abs=1e-6)
    assert ends[1].centroid[0] == pytest.approx(L, abs=1e-6)
    for o, out in zip(ends, (-1.0, 1.0)):
        assert o.area == pytest.approx(math.pi * R_BORE ** 2, rel=0.01)
        assert o.normal[0] == pytest.approx(out, abs=1e-6)        # out of the fluid
        assert o.on_extremity and o.clear_ahead and not o.gap
    assert p.seed is not None
    r = math.hypot(p.seed[1], p.seed[2])
    assert r < R_BORE and 0.0 < p.seed[0] < L                     # inside the bore, not the metal


def test_a_valve_drawn_shut_is_two_spaces_with_one_opening_each():
    disc = _cyl(L / 2 - 0.005, R_BORE, 0.01)                      # fills the bore: the valve is shut
    x = fv.extract(_comp(_tube(0.0, L), disc))
    assert x.found
    assert len(x.passages) == 2
    assert all(len(p.main_ports) == 1 for p in x.passages)


def test_a_hole_through_the_curved_wall_leaks_and_no_passage_is_claimed():
    leaky = BRepAlgoAPI_Cut(_tube(0.0, L / 2), _cyl(L / 4, 0.01, 0.2, axis=(0, 0, 1), z=-0.1)).Shape()
    x = fv.extract(_comp(leaky, _tube(L / 2, L / 2)))
    assert not x.found
    assert "reaches the outside" in x.reason


def test_a_lid_a_hair_short_of_its_body_leaves_a_gap_that_is_never_proposed():
    # a closed-bottom can whose open top is closed by a lid 0.3 mm short of the can's bore, with a
    # tube through the lid: the ring between lid and can is a gap, not an opening
    can = BRepAlgoAPI_Cut(_cyl(0.0, R_OUT, L), _cyl(0.01, R_BORE, L)).Shape()
    lid = BRepAlgoAPI_Cut(_cyl(L - 0.01, R_BORE - 0.0003, 0.01), _cyl(L - 0.011, 0.01, 0.012)).Shape()
    x = fv.extract(_comp(can, lid))
    assert x.found
    p = x.passages[0]
    gaps = [o for o in p.ports if o.gap]
    assert gaps, [(o.area, o.hole) for o in p.ports]
    assert all(not o.gap for o in p.main_ports)


def test_the_staged_fluid_is_closed_named_by_the_declaration_and_seeded_inside(tmp_path):
    x = fv.extract(_two_halves())
    declared = [{"name": "feed", "area_m2": math.pi * R_BORE ** 2, "near_m": (0.0, 0.0, 0.0)},
                {"name": "drain", "area_m2": math.pi * R_BORE ** 2, "near_m": (L, 0.0, 0.0)}]
    rec = fv.stage(x, declared, tmp_path / "stls")
    assert rec is not None
    assert set(rec["stls"]) == {"wall", "feed", "drain"}
    assert rec["wall_bounds_fluid"] is True
    facts = rec["facts"]
    assert facts["open_edges"] == 0 and facts["nonmanifold_edges"] == 0 and facts["inconsistent_edges"] == 0
    assert facts["one_patch_per_opening"]
    assert abs(facts["volume_m3"]) == pytest.approx(math.pi * R_BORE ** 2 * L, rel=0.02)
    for name in ("feed", "drain"):
        assert rec["openings"][name]["declared"] is True
        assert rec["openings"][name]["flow_area"] == pytest.approx(math.pi * R_BORE ** 2, rel=0.01)
    s = rec["interior_point"]
    assert math.hypot(s[1], s[2]) < R_BORE


def test_a_declaration_made_on_another_reading_is_not_staged_this_way(tmp_path):
    x = fv.extract(_two_halves())
    elsewhere = [{"name": "inlet", "area_m2": 1e-4, "near_m": (L / 2, 0.0, R_OUT)},
                 {"name": "outlet", "area_m2": math.pi * R_BORE ** 2, "near_m": (L, 0.0, 0.0)}]
    assert fv.match_declared(x, elsewhere) is None
    assert fv.stage(x, elsewhere, tmp_path / "s") is None


def test_a_triangle_file_of_two_touching_parts_is_split_the_same_way():
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    shape = _two_halves()
    BRepMesh_IncrementalMesh(shape, 0.001, False, 0.2, True)
    tris = [t for f in fv._faces(shape) for t in fv._face_triangles(f)]
    assert fv.closed_pieces(tris) == 2
    parts, why = fv.from_triangles(tris)
    assert parts is not None, why
    x = fv.extract(parts)
    assert x.found and len(x.passages) == 1
    assert len(x.passages[0].main_ports) == 2


def test_the_check_counts_a_triangle_files_parts_on_its_own_triangles(tmp_path):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    from meshpipeline.cad.fluid_volume_check import triangle_parts
    from meshpipeline.cad.stl_io import write_stl_binary
    shape = _two_halves()
    BRepMesh_IncrementalMesh(shape, 0.001, False, 0.2, True)
    tris = [t for f in fv._faces(shape) for t in fv._face_triangles(f)]
    write_stl_binary(tmp_path / "halves.stl", tris)
    assert triangle_parts(tmp_path / "halves.stl") == 2      # pressed face to face, still two parts


def test_a_step_assembly_is_staged_as_its_fluid_through_tessellate_internal(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import GeometryInterpretation, LengthUnit, ResolutionBasis
    from meshpipeline.engines.port_binding import declaration_targets

    tr = gp_Trsf()
    tr.SetScaleFactor(1000.0)                    # written in millimetres
    mm = BRepBuilderAPI_Transform(_two_halves(), tr, True).Shape()
    path = tmp_path / "pipe.step"
    w = STEPControl_Writer()
    w.Transfer(mm, STEPControl_AsIs)
    assert w.Write(str(path)) == IFSelect_RetDone
    interp = GeometryInterpretation(interpretation_id="t", owner_id="t", geometry_source_id="t",
                                    unit=LengthUnit.millimetre, scale_to_metres=0.001,
                                    basis=ResolutionBasis.file_declared, evidence="declared")
    declared = declaration_targets([
        {"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0], "diameter_mm": 2000 * R_BORE},
        {"name": "outlet", "type": "outlet", "near_mm": [1000 * L, 0.0, 0.0], "diameter_mm": 2000 * R_BORE},
        {"name": "wall", "type": "wall"}])
    t = tessellate_internal(path, tmp_path / "out", prepared=from_occ_transfer(interp, LengthUnit.millimetre),
                            declared_ports=declared)
    assert t["source"] == "fluid_volume"
    assert set(t["openings"]) == {"inlet", "outlet"}
    assert t["wall_bounds_fluid"] is True
    lo, hi = np.asarray(t["bbox_min"]), np.asarray(t["bbox_max"])
    assert hi[0] - lo[0] == pytest.approx(L, rel=1e-3)
    assert hi[1] - lo[1] == pytest.approx(2 * R_BORE, rel=0.02)   # the fluid, not the pipe's metal


def test_a_bounded_run_that_runs_out_of_time_says_so(tmp_path):
    ans = fv.run_bounded({"task": "propose", "source": str(tmp_path / "none.step")}, tmp_path, 0.001)
    assert "error" in ans


# ------------------------------------------------------------------------ the check's fold ----
def _answer(passages):
    return {"found": True, "reason": "", "parts": 3, "caps": 9, "regions": 12, "seconds": 1.0,
            "pockets_left_out": 2, "passages": passages}


def _port(x, area=1000.0, hole=None, proposed=True, gap=False, clear=True):
    return {"centroid_mm": [x, 0.0, 0.0], "normal": [1.0 if x > 0 else -1.0, 0.0, 0.0], "area_mm2": area,
            "hole_mm2": hole or area, "wh_mm": [35.68, 35.68], "on_extremity": True, "clear_ahead": clear,
            "gap": gap, "proposed": proposed}


def test_the_check_proposes_the_caps_as_the_openings_of_an_internal_assembly():
    facts = {"flow": "internal", "input_kind": "body-surface", "body_kind": "pipe_wall", "solids": 3,
             "openings": [{"id": k, "role": "outlet"} for k in range(1, 5)], "notes": []}
    assert wants_fluid_volume(facts)
    fold(facts, _answer([{"volume_l": 1.5, "seed_mm": [10.0, 0.0, 0.0],
                          "openings": [_port(-100.0), _port(100.0), _port(0.0, area=10.0, proposed=False),
                                       _port(50.0, area=3.0, hole=900.0, proposed=False, gap=True)]}]))
    ops = facts["openings"]
    assert [o["id"] for o in ops] == [1, 2]
    assert {o["role"] for o in ops} == {"inlet", "outlet"}
    assert ops[0]["diameter_mm"] == pytest.approx(35.68, abs=0.1)
    assert facts["openings_from"] == "fluid_volume"
    assert facts["body_kind"] == "assembly_cavity" and facts["flow"] == "internal"
    assert facts["seed_point_mm"] == [10.0, 0.0, 0.0]
    assert any("space inside the 3 parts" in n for n in facts["notes"])
    assert any("gap" in n for n in facts["notes"])
    assert any("sealed into the wall" in n for n in facts["notes"])


def test_a_body_read_as_external_keeps_its_reading_but_gets_the_openings_for_a_flip():
    facts = {"flow": "external", "input_kind": "solid-body", "body_kind": "single_solid", "solids": 40,
             "openings": [], "notes": []}
    fold(facts, _answer([{"volume_l": 30.0, "seed_mm": [0.0, 0.0, 0.0],
                          "openings": [_port(-100.0), _port(100.0)]}]))
    assert facts["flow"] == "external"
    assert len(facts["openings"]) == 2


def test_two_spaces_that_do_not_connect_are_said_and_nothing_is_invented_when_unsure():
    facts = {"flow": "internal", "body_kind": "pipe_wall", "solids": 14, "openings": [], "notes": []}
    fold(facts, _answer([{"volume_l": 0.17, "seed_mm": [-14.0, 0.0, 0.0], "openings": [_port(-30.0)]},
                         {"volume_l": 0.02, "seed_mm": [22.0, 0.0, 0.0], "openings": [_port(30.0)]}]))
    assert len(facts["openings"]) == 2
    assert any("do not connect" in n for n in facts["notes"])
    kept = {"flow": "internal", "solids": 2, "openings": [{"id": 1, "role": "inlet"}], "notes": []}
    fold(kept, {"error": "finding the fluid inside the parts took over 240 s"})
    assert kept["openings"] == [{"id": 1, "role": "inlet"}]
    assert any("could not be found" in n for n in kept["notes"])
