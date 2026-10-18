# Responsibility: Verify the planner refuses to patch either rim of a missing bore wall, and says so.
# Boundaries: the native tier, because the discriminator measures real planes on real geometry.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_missing_bore_wall_step, write_open_shell_step

import meshpipeline.settings.cad_repair as rcfg

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _filling_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_neither_rim_of_a_missing_bore_wall_is_ever_proposed_for_filling(tmp_path):
    # THE PART-RUINING CASE. Each rim is a perfectly fillable flat loop; patching both SEALS the
    # customer's bolt hole. It used to be a silent skip inside the fill executor, where nothing
    # downstream could see it. Now it is a PLANNING decision and it is reported.
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory, paired_rim_fingerprints

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    defects, _ = localize_scan(shape)

    rims = paired_rim_fingerprints(shape, defects)
    assert len(rims) >= 2, f"the discriminator found {len(rims)} rims, expected a pair"

    planner = RulesPlanner()
    history = RunHistory()
    seen: set[str] = set()
    for _ in range(10):
        candidate = planner.propose(defects, history, shape=shape)
        if candidate is None:
            break
        assert not (candidate.op.name == "fill_planar_hole"
                    and candidate.defect_fingerprint in rims), (
            "the planner proposed patching a rim of a missing bore wall")
        seen.add(f"{candidate.defect_fingerprint}|{candidate.op.name}")
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)

    assert any(r["reason"] == "paired_rim" for r in history.refusals), (
        "the refusal was not recorded, so nothing downstream can report it")


def test_an_ordinary_hole_is_still_proposed_for_filling(tmp_path):
    # The guard must not cost the case filling exists for. A single opening has nothing to pair
    # with, so it stays fillable.
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory, paired_rim_fingerprints

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    defects, _ = localize_scan(shape)

    assert paired_rim_fingerprints(shape, defects) == frozenset()

    planner, history = RulesPlanner(), RunHistory()
    proposed = set()
    for _ in range(10):
        candidate = planner.propose(defects, history, shape=shape)
        if candidate is None:
            break
        proposed.add(candidate.op.name)
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)

    assert "fill_planar_hole" in proposed or "sew_faces" in proposed


def test_loose_box_faces_are_not_a_bore_pair(tmp_path):
    # THE FALSE POSITIVE THE DISCRIMINATOR USED TO RAISE. Opposite faces of an unsewn box are
    # parallel loops offset along their normal - which was all the old check asked - yet they are
    # squares, not the rims of a drilled hole. Refusing them cost the corpus 200 fills.
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import paired_rim_fingerprints

    shape = _read(write_loose_faces_step(tmp_path / "loose.step"))
    defects, _ = localize_scan(shape)
    assert sum(1 for d in defects if d.code.value == "open_shell") >= 2
    assert paired_rim_fingerprints(shape, defects) == frozenset()


def _polygon(sides, radius, z, x=0.0):
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    polygon = BRepBuilderAPI_MakePolygon()
    for k in range(sides):
        a = 2 * math.pi * k / sides
        polygon.Add(gp_Pnt(x + radius * math.cos(a), radius * math.sin(a), z))
    polygon.Close()
    return polygon.Wire()


def _slot(z, length=10.0, radius=3.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt

    h = length / 2
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(-h, -radius, z), gp_Pnt(h, -radius, z)).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(
        gp_Pnt(h, -radius, z), gp_Pnt(h + radius, 0, z), gp_Pnt(h, radius, z)).Value()).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(h, radius, z), gp_Pnt(-h, radius, z)).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(
        gp_Pnt(-h, radius, z), gp_Pnt(-h - radius, 0, z), gp_Pnt(-h, -radius, z)).Value()).Edge())
    return wire.Wire()


def _circle(radius, z, x=0.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    circle = gp_Circ(gp_Ax2(gp_Pnt(x, 0.0, z), gp_Dir(0, 0, 1)), radius)
    return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()


@pytest.mark.parametrize("case", [
    "hex", "obround_slot", "square_pocket", "deep_1mm_in_25mm", "spot_face_3x", "16_gon"])
def test_a_through_hole_of_any_cross_section_pairs(case):
    # EVERY ONE OF THESE WAS SEALED. A roundness test, a 2.5x size-ratio cap and a 40-radius depth
    # cap each dropped a genuine through-hole from the check, and the fill patched both ends. The
    # cross-section, the flare and the depth of a passage do not change what sealing it does.
    from meshpipeline.cad.repair.conservative import _paired_rims

    loops = {
        "hex": [_polygon(6, 5.0, 0.0), _polygon(6, 5.0, 10.0)],
        "obround_slot": [_slot(0.0), _slot(10.0)],
        "square_pocket": [_polygon(4, 5.0, 0.0), _polygon(4, 5.0, 10.0)],
        "deep_1mm_in_25mm": [_circle(0.5, 0.0), _circle(0.5, 25.0)],
        "spot_face_3x": [_circle(3.0, 0.0), _circle(9.0, 10.0)],
        "16_gon": [_polygon(16, 5.0, 0.0), _polygon(16, 5.0, 10.0)],
    }[case]
    assert _paired_rims(loops) == {0, 1}


def test_two_round_rims_on_one_axis_pair_and_offset_or_coplanar_ones_do_not():
    from meshpipeline.cad.repair.conservative import _paired_rims

    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 10.0)]) == {0, 1}
    # a draft-angled bore: rims a little different, still one passage
    assert _paired_rims([_circle(8.0, 0.0), _circle(9.0, 10.0)]) == {0, 1}
    # a countersunk hole with its walls missing: the top rim twice the bore, still one passage
    assert _paired_rims([_circle(4.0, 0.0), _circle(8.0, 10.0)]) == {0, 1}
    # two holes in two parallel faces, not on one axis
    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 10.0, x=40.0)]) == set()
    assert _paired_rims([_polygon(6, 5.0, 0.0), _polygon(6, 5.0, 10.0, x=40.0)]) == set()
    # coplanar: two holes in one face
    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 0.0, x=40.0)]) == set()


def test_the_hex_plate_reproduction_refuses_the_fill_as_a_paired_rim(tmp_path):
    # THE REVIEWER'S END-TO-END REPRODUCTION. A 100x100x10 plate with an 8 mm hex through-hole and
    # its six walls missing: before the inner-wire test the planner proposed fill_planar_hole, the
    # session kept it, and the hole was gone. Both rims are inner wires of the plate's faces.
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory, paired_rim_fingerprints

    shape = _read(write_missing_bore_wall_step(tmp_path / "hex.step", profile="hex"))
    defects, _ = localize_scan(shape)

    rims = paired_rim_fingerprints(shape, defects)
    assert len(rims) == 2, f"the discriminator found {len(rims)} rims of the hex, expected 2"

    planner, history = RulesPlanner(), RunHistory()
    for _ in range(10):
        candidate = planner.propose(defects, history, shape=shape)
        if candidate is None:
            break
        assert candidate.op.name != "fill_planar_hole", "the hex through-hole would be sealed"
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)
    assert {r["defect"] for r in history.refusals if r["reason"] == "paired_rim"} == set(rims)
