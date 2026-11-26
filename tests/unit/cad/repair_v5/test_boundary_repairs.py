"""CRS v5 round 2 (invalid_brep_tolerance / closed_periodic_face): the missing-edge and bad-seam detectors' faults,
their aim on the defects (a periodic failure naming only bodies is aimed at their closed faces), the menu rows, and
(where OCP is installed) the injected pole case through the detector and `rebuild_boundary_edge`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v5 import brep_faults as F
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import loop

REVERSE = {("F", 1): "F1", ("F", 2): "F2", ("F", 3): "F3", ("F", 4): "F4", ("B", 1): "B1", ("B", 2): "B2"}
FACTS = {"diag_mm": 100.0, "tolerance": {}, "splines": [], "curve_splines": [],
         "poles": [{"face": 1, "edge": None, "why": "missing_edge", "after": 1, "gap_mm": 2.5},
                   {"face": 2, "edge": None, "why": "missing_edge", "after": 1, "gap_mm": 2.5},
                   {"face": 3, "edge": 7, "why": "degenerate_not_collapsed", "spread_mm": 1.0, "tol_mm": 1e-7}],
         "bad_seams": [{"face": 4, "edge": 9, "dev_mm": 10.8, "tol_mm": 10.8}],
         "closed_faces": [{"face": 3, "solid": 1}, {"face": 4, "solid": 2}]}


def _defect(code: str, cls: str, kind: str = "validity", entities: tuple = ("F1",)) -> Defect:
    return Defect(id=f"S@{code}", kind=kind, code=code, entities=entities, cls=cls, rule=f"{code} -> {cls}")


def test_faults_split_missing_edges_and_bad_seams_from_poles() -> None:
    faults = {f.kind: f for f in F.faults_of(FACTS, REVERSE)}
    assert faults["missing_edge"].entities == ("F1", "F2")
    assert faults["pole"].entities == ("F3",)
    assert faults["bad_seam"].entities == ("F4",) and "10.8 mm" in faults["bad_seam"].why
    assert F.closed_faces_of(FACTS, REVERSE) == [("F3", "B1"), ("F4", "B2")]


def test_brep_invalid_on_a_missing_edge_takes_the_rebuild_as_its_rule_seed() -> None:
    faults = F.faults_of(FACTS, REVERSE)
    out = D.attach_faults([_defect("brep_invalid", "invalid_brep_tolerance", entities=("F1", "F2"))], faults, {})
    d = out[0]
    assert d.cause["kind"] == "missing_edge" and d.cause["aim"] == ["F1", "F2"]
    assert loop.table_key(d) == "invalid_brep_tolerance:missing_edge"
    assert loop.CLASS_REPAIRS[loop.table_key(d)][0] == ("rebuild_boundary_edge",)
    assert Q.params_for("rebuild_boundary_edge", d) == (["F1", "F2"], {})


def test_periodic_failure_naming_bodies_is_aimed_at_their_closed_faces_or_bad_seam() -> None:
    closed = F.closed_faces_of(FACTS, REVERSE)
    # no bad seam on B1's closed faces: aimed at them (split_closed then has faces)
    d = D.attach_faults([_defect("periodic_surface", "closed_periodic_face", "mesher", ("B1",))],
                        [f for f in F.faults_of(FACTS, REVERSE) if f.kind != "bad_seam"], {}, closed_faces=closed)[0]
    assert d.cause["aim"] == ["F3"] and Q.aim_reason("split_closed", d) is None
    assert Q.params_for("split_closed", d)[0] == ["F3"]
    # a bad seam on B2's closed face: the seam rebuild is the rule seed
    d = D.attach_faults([_defect("periodic_surface", "closed_periodic_face", "mesher", ("B2",))],
                        F.faults_of(FACTS, REVERSE), {}, closed_faces=closed)[0]
    assert d.cause["kind"] == "bad_seam" and d.cause["aim"] == ["F4"]
    assert loop.CLASS_REPAIRS[loop.table_key(d)][0] == ("rebuild_seam",)
    # a periodic failure on a face of its own keeps its face
    d = D.attach_faults([_defect("periodic_surface", "closed_periodic_face", "mesher", ("F3",))],
                        F.faults_of(FACTS, REVERSE), {}, closed_faces=closed)[0]
    assert "aim" not in d.cause and loop.table_key(d) == "closed_periodic_face"
    # a body-only defect whose SITE located faces (VM, injected seam_variant: `periodic_surface on B1`, narrowest
    # face F25): the site's closed faces are the aim
    d = D._periodic(_defect("periodic_surface", "closed_periodic_face", "mesher", ("B1",)), {"B1", "F3", "F1"},
                    [], closed)
    assert d.cause["aim"] == ["F3"]


def test_new_ops_are_registered_for_the_class_menus_only() -> None:
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.proposer import offered_ops

    for name in ("rebuild_boundary_edge", "rebuild_seam"):
        assert name in REGISTRY and name not in offered_ops()
    assert "missing_edge" in loop.REMEASURED_KINDS and "bad_seam" in loop.REMEASURED_KINDS


# --- real geometry (OCP) -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pcurves_too", [False, True])
def test_a_turned_seam_is_measured_and_rebuilt_onto_its_vertices(tmp_path: Path, pcurves_too: bool) -> None:
    """The seam's 3D curve turned 0.3 rad (and, as the STEP reader leaves it, its pcurves with it): the seam is off
    its vertices; rebuild_seam puts it back on the iso through them and the face is valid again."""
    pytest.importorskip("OCP")
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec2d
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: F401 - registers the kernel fns
    from meshpipeline.cad.repair_v4.ops.boundary import SEAM_DEV_MM, _rebuild_seam, seam_deviation, seams_of
    from meshpipeline.cad.repair_v4.ws_kernel import _maps

    shape = BRepPrimAPI_MakeCylinder(10, 30).Shape()
    fmap = _maps(shape)["face"]
    index = next(i for i in range(1, fmap.Extent() + 1) if seams_of(TopoDS.Face_s(fmap.FindKey(i))))
    face = TopoDS.Face_s(fmap.FindKey(index))
    seam = seams_of(face)[0]
    first, last = BRep_Tool.Range_s(seam)
    builder = BRep_Builder()
    if pcurves_too:
        fwd = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(seam.Oriented(TopAbs_FORWARD)), face, 0.0, 0.0)
        rev = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(seam.Oriented(TopAbs_REVERSED)), face, 0.0, 0.0)
        builder.UpdateEdge(seam, fwd.Translated(gp_Vec2d(0.3, 0)), rev.Translated(gp_Vec2d(0.3, 0)), face, 1e-7)
    turned = BRep_Tool.Curve_s(seam, 0.0, 0.0).Rotated(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 0.3)
    builder.UpdateEdge(seam, turned, TopLoc_Location(), 1e-7)
    builder.Range(seam, first, last)
    assert seam_deviation(face, seam) > SEAM_DEV_MM
    brep = tmp_path / "in.brep"
    BRepTools.Write_s(shape, str(brep))
    res = _rebuild_seam({"brep_path": str(brep), "out_path": str(tmp_path / "out.brep"), "quantum": 1e-6,
                         "part_diag_mm": 50.0, "targets": {"face": [index], "edge": [], "solid": []}})
    assert "declined" not in res and res["params"]["valid_after"]
    assert res["params"]["seams"][0]["dev_after_mm"] < SEAM_DEV_MM


# --- the injected pole case through the detector and the rule seed ------------------------------------------------

def test_injected_pole_reads_back_missing_an_edge_and_the_rebuild_closes_it(tmp_path: Path) -> None:
    pytest.importorskip("OCP")
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: F401 - registers the kernel fns
    from meshpipeline.cad.repair_v4.ops.boundary import _rebuild_boundary_edge
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep
    from meshpipeline.cad.repair_v5.brep_facts_kernel import _v5_brep_facts

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools" / "validation" / "v4_eval"))
    import inject

    src = tmp_path / "src.step"
    inject.write_step(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40, 20, 10).Shape(), src)
    damaged = inject.read_step(inject.inject(src, "pole_degenerate", 1, tmp_path / "pole").path)
    brep = tmp_path / "in.brep"
    BRepTools.Write_s(damaged, str(brep))
    gaps = sorted({p["face"] for p in _v5_brep_facts({"brep_path": str(brep)})["poles"]
                   if p["why"] == "missing_edge"})
    assert len(gaps) == 2                                  # both faces that shared the dropped edge
    out = tmp_path / "out.brep"
    res = _rebuild_boundary_edge({"brep_path": str(brep), "out_path": str(out), "quantum": 1e-6,
                                  "part_diag_mm": 45.8, "targets": {"face": gaps, "edge": [], "solid": []}})
    assert "declined" not in res and res["params"]["edges_added"][0]["curve"] == "line"
    assert BRepCheck_Analyzer(read_brep(str(out))).IsValid()
    assert not [p for p in _v5_brep_facts({"brep_path": str(out)})["poles"] if p["why"] == "missing_edge"]
