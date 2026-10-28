# Responsibility: Pin that a CAD face MESH-8 leaves out (a skin of no thickness lying on another face of its part) is
# recorded with the face it lies on and counted as ACCOUNTED FOR - in the sidecar, the solver files' notes, the
# measured face check, the reviewer's facts and the result screen - never as a missing named face; and that no AI
# review finding about face counts can turn a valid FEA mesh into a FAIL.
# Boundaries: parts built here with OpenCASCADE and staged in-process; the deck, the records and the review's
# findings are built here; nothing is meshed and no model is asked.
from __future__ import annotations

import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from meshpipeline.agents.reviewer import review_policy as RP  # noqa: E402
from meshpipeline.agents.reviewer.eligibility import AxisFinding  # noqa: E402
from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad import zero_skins as Z  # noqa: E402
from meshpipeline.engines import fea_results, fea_review  # noqa: E402


def _wires(face):
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out, ex = [], TopExp_Explorer(face, TopAbs_WIRE)
    while ex.More():
        out.append(TopoDS.Wire_s(ex.Current()))
        ex.Next()
    return out


def _tube_with_a_pocket_to_its_bore(r_in=8.0, r_out=10.0, length=60.0, r_hole=3.0, skin_first=False):
    """A tube with a radial pocket cut to its inner wall, its floor left as a disk of the bore's own surface lying
    on the uncut bore - the CAD defect of job fb44fed7's tube (pocket floors lying on the inner wall)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepLib import BRepLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    bore_tool = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), r_in, length + 2).Shape()
    tube = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(r_out, length).Shape(), bore_tool).Shape()
    pocket = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, length / 2), gp_Dir(0, 1, 0)), r_hole, r_out + 2).Shape()
    holed = BRepAlgoAPI_Cut(tube, pocket).Shape()
    sew = BRepBuilderAPI_Sewing(1e-6)
    bore = None
    for f in Z._faces(holed):
        a = BRepAdaptor_Surface(f)
        if a.GetType() == GeomAbs_Cylinder and abs(a.Cylinder().Radius() - r_in) < 1e-9 and len(_wires(f)) > 1:
            bore = f
            continue
        sew.Add(f)
    assert bore is not None
    surf, outer = BRep_Tool.Surface_s(bore), BRepTools.OuterWire_s(bore)
    inner = [w for w in _wires(bore) if not w.IsSame(outer)][0]
    floor = BRepBuilderAPI_MakeFace(surf, inner, True).Face()                         # the pocket's floor on it
    if skin_first:
        # the skin FIRST in the solid's face order, so every face after it is numbered one on once it is left out
        sew = BRepBuilderAPI_Sewing(1e-6)
        sew.Add(floor)
        for f in Z._faces(holed):
            if not f.IsSame(bore):
                sew.Add(f)
    sew.Add(BRepBuilderAPI_MakeFace(surf, outer, True).Face())                         # the bore, uncut
    if not skin_first:
        sew.Add(floor)
    sew.Perform()
    ex = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    solid = TopoDS.Solid_s(BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(ex.Current())).Solid())
    BRepLib.OrientClosedSolid_s(solid)
    return solid


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    """The tube and a block apart from it, staged as an assembly: its sidecar, and the tube's skin."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    tube = _tube_with_a_pocket_to_its_bore()
    skins = Z.find(tube)
    assert len(skins) == 1                          # the pocket's floor, lying on the bore
    block = BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 10.0, 10.0, 10.0).Shape()
    side = A.stage(A.compound([tube, block]), A.BONDED, tmp_path_factory.mktemp("tube"), tie=True)
    n_tube = len(Z._faces(tube))
    cad = [f"Part_1_Face_{k}" for k in range(1, n_tube + 1)] + [f"Part_2_Face_{k}" for k in range(1, 7)]
    named = sorted({n for row in side["surfaces"] for n in row["names"]})
    return side, skins[0], cad, named


def test_mesh8_records_each_skin_face_with_the_face_it_lies_on(staged):
    side, skin, _cad, named = staged
    face, on = f"Part_1_Face_{skin.skin}", f"Part_1_Face_{skin.on}"
    assert side["skins"][0]["pairs"] == [
        {"face": face, "on": on, "words": f"{face} lies on {on}: zero thickness, no volume - left out of the mesh"}]
    assert face not in named and on in named


def _records(side, cad, named, *, skins=True, merged=()):
    plan = {"faces": [{"name": n} for n in cad], "assembly": {"skins": side["skins"]} if skins else {}}
    q = {"cells": 1000, "fea_inverted": 0, "fatal": [],
         "deck": {"face_sets": {n: k for k, n in enumerate(named, start=1)}},
         "fea": {"plan": True, "exports": {"nastran": {"faces": len(named)}, "ansys": {"faces": len(named)}}},
         **({"fea_tiny": {"merged": [{"face": [m], "into": ["Part_2_Face_1"], "width_mm": 0.01} for m in merged]}}
            if merged else {})}
    return q, plan


def test_a_skin_left_out_is_accounted_for_never_missing(staged):
    side, skin, cad, named = staged
    q, plan = _records(side, cad, named)
    acc = fea_review.face_accounting(q, plan)
    assert len(named) == len(cad) - 1
    assert acc["complete"] is True and acc["missing"] == [] and acc["accounted"] == 1
    assert all(f["complete"] for f in acc["files"].values()) and set(acc["files"]) == {"mesh.inp", "mesh.bdf", "mesh.cdb"}
    # without MESH-8's record the face IS missing: the accounting reads the record, it does not guess
    q2, plan2 = _records(side, cad, named, skins=False)
    acc2 = fea_review.face_accounting(q2, plan2)
    assert acc2["complete"] is False and acc2["missing"] == [f"Part_1_Face_{skin.skin}"]


def test_a_tiny_face_merged_is_accounted_for_the_same_way(staged):
    side, _skin, cad, named = staged
    gone = named[-1]
    q, plan = _records(side, cad, [n for n in named if n != gone], merged=[gone])
    acc = fea_review.face_accounting(q, plan)
    assert acc["complete"] is True and acc["accounted"] == 2 and [m["face"] for m in acc["merged"]] == [gone]


def test_the_reviewers_facts_and_the_result_screen_say_where_the_faces_went(staged, tmp_path):
    side, skin, cad, named = staged
    q, plan = _records(side, cad, named)
    (tmp_path / "quality.json").write_text(json.dumps(q))
    (tmp_path / "fea_plan.json").write_text(json.dumps(plan))
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        (tmp_path / name).write_text("x")
    text = fea_review.facts_text(tmp_path)
    assert f"Part_1_Face_{skin.skin} lies on Part_1_Face_{skin.on}" in text
    assert "Every CAD face is accounted for" in text and "NOT a missing named face" in text
    res = fea_results.results(tmp_path)
    assert res["skins_left_out"][0]["face"] == f"Part_1_Face_{skin.skin}"
    assert any("accounted for" in w for w in res["plan_words"])


def test_the_face_check_settles_a_face_count_finding_and_a_valid_fea_review_passes(staged):
    side, _skin, cad, named = staged
    q, _plan = _records(side, cad, named)
    q["fea_faces"] = fea_review.face_accounting(q, _plan)
    measured = RP.measurements_from({"quality": q}, {"purpose": "structural"})
    assert measured["fea_valid"] is True and measured["fea_faces_complete"] is True
    # the finding job fb44fed7's review filed: 142 named against 146
    claim = AxisFinding(axis_key="fea_faces_named", passed=False, severity="material",
                        finding=f"{len(named)} named Face_k groups while the CAD has {len(cad)} faces",
                        brief_requirement="every face named", wrong_problem="requirement_absent")
    other = AxisFinding(axis_key="fea_solver_limits", passed=False, finding="aspect past a limit")
    settled = RP.settle((claim, other), measured)
    assert settled[0].passed is True and "Settled - measured" in settled[0].finding and not settled[0].wrong_problem
    assert settled[1].passed is False                       # a point no measurement settles stays a concern
    # A VALID FEA MESH PASSES ITS REVIEW whatever stands as a concern; an invalid one keeps its findings' verdict
    assert RP.review_passes(settled, measured) is True
    assert RP.review_passes(settled, {**measured, "fea_valid": False}) is False
    assert RP.review_passes(settled[:1], {**measured, "fea_valid": False}) is True
    # an incomplete accounting settles nothing
    assert RP.settle((claim,), {**measured, "fea_faces_complete": False})[0].passed is False


# ---------------------------------------------------------------- MESH-8 for a single part ----

def test_a_single_parts_skin_is_left_out_and_every_face_keeps_its_cad_name(tmp_path):
    """The tube alone, its skin sewn in so it is not the last face: the part is staged opened, and every face of
    the opened solid is named for the CAD face it came from - the faces after the skin are not renumbered."""
    from OCP.BRepTools import BRepTools

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines.gmsh.abaqus_deck import SolidMesh, retagged

    tube = _tube_with_a_pocket_to_its_bore(skin_first=True)
    (skin,) = Z.find(tube)
    n = len(Z._faces(tube))
    BRepTools.Write_s(tube, str(tmp_path / "geometry.brep"))
    info = FD.open_single_skins(tmp_path)
    assert info and len(info["origin"]) == n - 1 and skin.skin not in info["origin"]
    assert info["skins"][0]["pairs"][0]["face"] == f"Face_{skin.skin}"
    assert (tmp_path / FD.CAD_SOLID).exists() and (tmp_path / FD.SKINS_FILE).exists()
    # driven again, the workspace's record stands (the staged solid is the opened one now)
    assert FD.open_single_skins(tmp_path) == info
    record, surface, _diag = FD.measure_staged(tmp_path)
    named = FD.named_as_cad(record, surface, info)
    assert set(named["faces"]) == {f"Face_{k}" for k in range(1, n + 1)}
    assert named["faces"][f"Face_{skin.skin}"]["kind"] == "skin"
    assert sorted(surface.names) == sorted(f"Face_{o}" for o in info["origin"])
    assert any("left out of the mesh" in t for t in named["notes"])
    # the mesh's faces keyed by the CAD's tags: Face_k is the CAD's face k
    import numpy as np
    m = SolidMesh(np.zeros(0, dtype=np.int64), np.zeros((0, 3)), np.zeros((0, 4), dtype=np.int64),
                  np.zeros(0, dtype=np.int64), {k: np.zeros((0, 3)) for k in range(1, n)}, by_tag=True)
    assert sorted(retagged(m, {k: o for k, o in enumerate(info["origin"], start=1)}).faces) == sorted(info["origin"])


def test_a_single_part_without_a_skin_is_staged_as_it_was(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools

    from meshpipeline.engines import fea_driver as FD

    BRepTools.Write_s(BRepPrimAPI_MakeCylinder(5.0, 20.0).Shape(), str(tmp_path / "geometry.brep"))
    before = (tmp_path / "geometry.brep").read_bytes()
    assert FD.open_single_skins(tmp_path) is None
    assert (tmp_path / "geometry.brep").read_bytes() == before and not (tmp_path / FD.SKINS_FILE).exists()


def test_a_single_parts_skins_are_accounted_for_from_its_plan():
    plan = {"faces": [{"name": f"Face_{k}"} for k in range(1, 7)],
            "skins": [{"part": "The part", "faces": ["Face_5"], "on": ["Face_6"],
                       "pairs": [{"face": "Face_5", "on": "Face_6", "words": "Face_5 lies on Face_6"}]}]}
    q = {"deck": {"face_sets": {f"Face_{k}": k for k in (1, 2, 3, 4, 6)}}}
    acc = fea_review.face_accounting(q, plan)
    assert acc["complete"] is True and acc["accounted"] == 1 and acc["missing"] == []
