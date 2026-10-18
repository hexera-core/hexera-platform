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


def test_square_loops_stacked_on_one_axis_are_not_a_bore_pair():
    # Co-axial, parallel, the right gap - and still not a bore, because neither loop is round.
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair.conservative import _paired_rims

    def square(z):
        polygon = BRepBuilderAPI_MakePolygon()
        for x, y in ((0, 0), (8, 0), (8, 8), (0, 8)):
            polygon.Add(gp_Pnt(x, y, z))
        polygon.Close()
        return polygon.Wire()

    assert _paired_rims([square(0.0), square(10.0)]) == set()


def _circle(radius, z, x=0.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    circle = gp_Circ(gp_Ax2(gp_Pnt(x, 0.0, z), gp_Dir(0, 0, 1)), radius)
    return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()


def test_two_round_rims_on_one_axis_pair_and_offset_or_unequal_ones_do_not():
    from meshpipeline.cad.repair.conservative import _paired_rims

    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 10.0)]) == {0, 1}
    # a draft-angled bore: rims a little different, still one passage
    assert _paired_rims([_circle(8.0, 0.0), _circle(9.0, 10.0)]) == {0, 1}
    # a countersunk hole with its walls missing: the top rim twice the bore, still one passage
    assert _paired_rims([_circle(4.0, 0.0), _circle(8.0, 10.0)]) == {0, 1}
    # two holes in two parallel faces, not on one axis
    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 10.0, x=40.0)]) == set()
    # coplanar: two holes in one face
    assert _paired_rims([_circle(8.0, 0.0), _circle(8.0, 0.0, x=40.0)]) == set()
    # far apart along the axis relative to their size: not one drilled passage
    assert _paired_rims([_circle(1.0, 0.0), _circle(1.0, 500.0)]) == set()
