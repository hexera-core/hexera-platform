# Responsibility: Prove on the REAL OCCT kernel that each v4 defect injector damages a clean part, that the damage
#                 survives writing and re-reading the STEP, that it is reproducible from the seed, and that the label
#                 names what was damaged.
# Boundaries: the native tier. `cheap_verdict` (the v4 checker) does not exist on this branch, so `_probe` and the
#             `_DAMAGE` predicates below are a deliberately narrow local STAND-IN: they measure the OCCT property each
#             injector claims to change, on the file as a consumer reads it. Replace them with cheap_verdict when it lands.
from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
import warnings
from pathlib import Path

import pytest
from tests.native._v4_flush_patch_part import write_chamfered_flush_patch_box, write_flush_patch_box

pytest.importorskip("OCP.BRepCheck", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder, BRep_Tool  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf  # noqa: E402
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec2d  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.IFSelect import IFSelect_RetDone  # noqa: E402
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape  # noqa: E402


def _inject_module():
    path = Path("/srv/devtools/validation/v4_eval/inject.py")
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "devtools" / "validation" / "v4_eval" / "inject.py"
    spec = importlib.util.spec_from_file_location("v4_eval_inject", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inj = _inject_module()
CORPUS = Path("/srv/devtools/validation/corpora/mesh-failure-real.json")
if not CORPUS.exists():
    CORPUS = Path(__file__).resolve().parents[2] / "devtools" / "validation" / "corpora" / "mesh-failure-real.json"

SEED = 1


# ---- the clean fixture: a filleted box with a through hole, turned off-axis --------------------------------------
# Turned by an arbitrary rotation so its coordinates are not round numbers, as a real part's are; an axis-aligned
# fixture would leave precision_truncate little to cut. The hole gives the one cylindrical face with a seam.

def _clean_part():
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    fillet = BRepFilletAPI_MakeFillet(box)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(box, TopAbs_EDGE, edges)
    for i in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(i))
        a, b = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge))
        if abs(a.X() - b.X()) < 1e-9 and abs(a.Y() - b.Y()) < 1e-9:      # the four vertical edges
            fillet.Add(3.0, edge)
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0, 0, 1)), 4.0, 22.0).Shape()
    part = BRepAlgoAPI_Cut(fillet.Shape(), hole).Shape()
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(3.3, 1.7, 0.9), gp_Dir(1, 2, 3)), 0.37)
    return BRepBuilderAPI_Transform(part, turn, True).Shape()


def _write_step(shape, path: Path) -> Path:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return path


def _read(path: Path):
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone, f"{path} does not read"
    reader.TransferRoots()
    return reader.OneShape()


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    return _write_step(_clean_part(), tmp_path_factory.mktemp("clean") / "clean_part.step")


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("injected")


@pytest.fixture(scope="module")
def fixable_src(tmp_path_factory) -> Path:
    """A part with small faces the op could put back. `src` has none (every face spans over 5% of its diagonal), so
    `face_delete` correctly refuses it: the default kind only deletes faces whose loop is within the fill cap."""
    return write_flush_patch_box(tmp_path_factory.mktemp("fixable") / "flush_patch_box.step")


@pytest.fixture(scope="module")
def mixed_src(tmp_path_factory) -> Path:
    """Flush 3 x 3 mm patches (a plane restores them) AND one inclined 3 mm corner chamfer face (it does not): both
    are within the 5% cap, so only the op's own fill separates them."""
    return write_chamfered_flush_patch_box(tmp_path_factory.mktemp("mixed") / "chamfered_flush_patch_box.step")


def _source_of(kind: str, src: Path, fixable_src: Path, mixed_src: Path) -> Path:
    """The clean part a kind needs: the small-face kinds need small faces (`src` has none)."""
    return {"face_delete": fixable_src, "face_delete_inclined": mixed_src}.get(kind, src)


@pytest.fixture(scope="module")
def cases(src, fixable_src, mixed_src, out_dir) -> dict:
    """Every feasible kind, injected once. Cached: each is a real OCCT job, slow under emulation."""
    return {k: inj.inject(_source_of(k, src, fixable_src, mixed_src), k, SEED, out_dir)
            for k in inj.KINDS if k != "pcurve_corrupt"}


# ---- the stand-in for cheap_verdict -------------------------------------------------------------------------------

def _all(shape, kind):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _centre(shape, linear=False):
    props = GProp_GProps()
    (BRepGProp.LinearProperties_s if linear else BRepGProp.SurfaceProperties_s)(shape, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z()), props.Mass()


def _probe(path: Path) -> dict:
    """What a consumer sees reading the FILE: counts, openness, tolerance, validity, smallest edge."""
    shape = _read(path)
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    free = sum(1 for i in range(1, ancestors.Extent() + 1) if ancestors.FindFromIndex(i).Size() == 1)
    return {
        "faces": len(_all(shape, TopAbs_FACE)), "edges": len(_all(shape, TopAbs_EDGE)),
        "solids": len(_all(shape, TopAbs_SOLID)), "free_edges": free,
        "max_tol": ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1),
        "valid": BRepCheck_Analyzer(shape).IsValid(),
        "min_edge": min(_centre(e, True)[1] for e in _all(shape, TopAbs_EDGE)),
    }


def _nearest_face_edge_tol(path: Path, centre) -> float:
    """Highest edge or vertex tolerance on the face whose centre is nearest `centre`: did the damage land on the named face."""
    faces = _all(_read(path), TopAbs_FACE)
    face = min(faces, key=lambda f: math.dist(_centre(f)[0], centre))
    return max([BRep_Tool.Tolerance_s(TopoDS.Edge_s(e)) for e in _all(face, TopAbs_EDGE)]
               + [BRep_Tool.Tolerance_s(TopoDS.Vertex_s(v)) for v in _all(face, TopAbs_VERTEX)])


def _face_exists(path: Path, centre, area, tol=1e-3) -> bool:
    for f in _all(_read(path), TopAbs_FACE):
        c, a = _centre(f)
        if math.dist(c, centre) < tol and abs(a - area) < 1e-2 * max(area, 1e-9):
            return True
    return False


#: The spec's deviation scale (the repair tier's CAD_REPAIR_MAX_DEVIATION_RATIO is 0.001 of the diagonal): a defect
#: measured in geometry should exceed what the repair tier itself treats as noise.
DEVIATION_RATIO = 1e-3


def _diag(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _surface_deviation(src_path: Path, out_path: Path, centre) -> float:
    """Sampled distance from the SOURCE face's surface to the damaged file's surface for the face named by `centre`."""
    sf = min(_all(_read(src_path), TopAbs_FACE), key=lambda f: math.dist(_centre(f)[0], centre))
    of = min(_all(_read(out_path), TopAbs_FACE), key=lambda f: math.dist(_centre(f)[0], centre))
    u0, u1, v0, v1 = BRepTools.UVBounds_s(TopoDS.Face_s(sf))
    s_surf, o_surf = BRep_Tool.Surface_s(TopoDS.Face_s(sf)), BRep_Tool.Surface_s(TopoDS.Face_s(of))
    worst = 0.0
    for a in range(6):
        for b in range(6):
            p = s_surf.Value(u0 + (u1 - u0) * a / 5, v0 + (v1 - v0) * b / 5)
            worst = max(worst, GeomAPI_ProjectPointOnSurf(p, o_surf).LowerDistance())
    return worst


def _vertex_drift(src_path: Path, out_path: Path) -> float:
    """Largest distance from any source vertex to the nearest vertex of the damaged file."""
    out_pts = [BRep_Tool.Pnt_s(TopoDS.Vertex_s(v)) for v in _all(_read(out_path), TopAbs_VERTEX)]
    return max(min(p.Distance(q) for q in out_pts)
               for p in (BRep_Tool.Pnt_s(TopoDS.Vertex_s(v)) for v in _all(_read(src_path), TopAbs_VERTEX)))


@pytest.fixture(scope="module")
def base(src) -> dict:
    return _probe(src)


def test_the_fixture_is_clean(base):
    assert base["solids"] == 1 and base["free_edges"] == 0 and base["valid"]
    assert base["max_tol"] < 1e-5


# ---- per kind: the damage is real in the file, and the label names it ---------------------------------------------

def test_loose_reapprox(cases, base, src):
    case = cases["loose_reapprox"]
    p = _probe(case.path)
    # QUARANTINED kind (see inject.py): this proves the surface really moved, not that the file fails clean-STEP.
    assert p["max_tol"] > 1e-3 > base["max_tol"]
    ents = case.label["entities"]
    assert ents and all(e["type"] == "face" for e in ents)
    for e in ents:                                                       # the damage is on the NAMED faces
        assert _nearest_face_edge_tol(case.path, e["center"]) > 1e-3
        # Measured 0.031 on a 70 mm diagonal: the surface really moved, yet LESS than DEVIATION_RATIO * diagonal (0.07),
        # the scale the repair tier itself treats as noise. That is why this kind stays quarantined.
        assert _surface_deviation(src, case.path, e["center"]) > 1e-2


def test_unsew_perturb(cases, base):
    case = cases["unsew_perturb"]
    p = _probe(case.path)
    assert base["free_edges"] == 0 and p["free_edges"] > 0 and p["solids"] == 0
    assert p["faces"] == base["faces"]                                   # nothing deleted, only unsewn
    assert case.label["entities"] and all(len(e["shift"]) == 3 for e in case.label["entities"])


def test_tolerance_inflate(cases, base):
    case = cases["tolerance_inflate"]
    p = _probe(case.path)
    assert p["solids"] == 1 and p["max_tol"] > 1e-3 > base["max_tol"]      # QUARANTINED kind: tolerance-only damage
    verts = [e for e in case.label["entities"] if e["type"] == "vertex"]
    assert verts and any(e["type"] == "edge" for e in case.label["entities"])
    shape = _read(case.path)
    for v in verts:                                                      # the named vertex now carries the gap
        want = [c + s for c, s in zip(v["center"], v["shift"])]
        near = min(_all(shape, TopAbs_VERTEX), key=lambda x: math.dist(BRep_Tool.Pnt_s(TopoDS.Vertex_s(x)).Coord(), want))
        assert BRep_Tool.Tolerance_s(TopoDS.Vertex_s(near)) >= 0.5 * v["gap"]


def test_pcurve_corrupt_is_infeasible_and_says_so(src, out_dir):
    # Pcurve damage does not survive OCCT's STEP reader (it rebuilds pcurves on import), so the injector must refuse
    # rather than write a file that reads back clean.
    with pytest.raises(inj.InjectionInfeasible, match="rebuilds pcurves"):
        inj.inject(src, "pcurve_corrupt", SEED, out_dir)


def test_infeasible_is_not_a_not_applicable_skip():
    assert not issubclass(inj.InjectionInfeasible, inj.InjectionNotApplicable)
    assert not issubclass(inj.InjectionNotApplicable, inj.InjectionInfeasible)


def test_canary_pcurve_corruption_is_healed_by_the_step_reader(src, tmp_path):
    """Pins the negative result behind pcurve_corrupt. If this fails, the reader no longer heals pcurves (or the
    config changed) and pcurve_corrupt may have become feasible: revisit the injector."""
    shape = inj.read_step(src)
    builder, corrupted = BRep_Builder(), 0
    for f in _all(shape, TopAbs_FACE):
        face = TopoDS.Face_s(f)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        for e in _all(face, TopAbs_EDGE):
            edge = TopoDS.Edge_s(e)
            pc = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
            if pc is None:
                continue
            moved = pc.Copy()
            moved.Translate(gp_Vec2d((u1 - u0) * 0.1, (v1 - v0) * 0.1))
            builder.UpdateEdge(edge, moved, face, BRep_Tool.Tolerance_s(edge))
            corrupted += 1
    assert corrupted and not BRepCheck_Analyzer(shape).IsValid()         # the corruption is real in memory
    written = tmp_path / "pcurve_corrupt.step"
    inj.write_step(shape, written)
    p = _probe(written)
    assert p["valid"] and p["max_tol"] < 1e-6                            # ...and gone once read back


def test_seam_variant(cases, base):
    case = cases["seam_variant"]
    p = _probe(case.path)
    assert p["max_tol"] > 1e-3 > base["max_tol"]                         # the seam no longer sits where the cylinder is cut
    (seam,) = case.label["entities"]
    assert seam["type"] == "seam_edge" and case.label["variant"] == "seam_rotated"
    assert _nearest_face_edge_tol(case.path, seam["face_center"]) > 1e-3


def test_small_entities(cases, base):
    case = cases["small_entities"]
    p = _probe(case.path)
    assert p["valid"] and p["solids"] == 1 and p["faces"] > base["faces"]
    assert p["min_edge"] < base["min_edge"] / 10                         # features far below the part's mesh size
    assert case.label["entities"]
    for e in case.label["entities"]:                                     # the named tiny faces are in the file
        assert _face_exists(case.path, e["center"], e["area"])
        assert e["area"] < 1.0


def _free_loop_ratio(path: Path) -> float:
    """The span of the file's FREE edges (the hole's loop) over the part diagonal, measured the way `fill_loop`
    measures it and read off the FILE, not off the label: an independent check of what the label claims."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    shape = _read(path)
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    box = Bnd_Box()
    for i in range(1, ancestors.Extent() + 1):
        if ancestors.FindFromIndex(i).Size() == 1:
            BRepBndLib.Add_s(ancestors.FindKey(i), box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1)) / _diag(shape)


def test_face_delete(cases, fixable_src):
    case = cases["face_delete"]
    p, base = _probe(case.path), _probe(fixable_src)
    assert p["faces"] == base["faces"] - 1 and p["free_edges"] > 0 and p["solids"] == 0
    (e,) = case.label["entities"]
    assert not _face_exists(case.path, e["center"], e["area"])           # the named face is the one that is gone


@pytest.mark.parametrize("seed", range(6))
def test_face_delete_only_deletes_a_face_the_op_could_fill(fixable_src, tmp_path, seed):
    """The injector and `fill_loop` read ONE cap (limits.MAX_FILL_SPAN_RATIO). Measured on the file, not the label."""
    from meshpipeline.cad.repair_v4 import limits

    case = inj.inject(fixable_src, "face_delete", seed, tmp_path)
    measured = _free_loop_ratio(case.path)
    assert measured <= limits.MAX_FILL_SPAN_RATIO, f"seed {seed}: the hole spans {measured:.1%} of the diagonal"
    assert case.label["loop_span_ratio"] == pytest.approx(measured, abs=1e-4)
    assert case.label["fill_cap_ratio"] == limits.MAX_FILL_SPAN_RATIO
    assert case.label["status"] == "structural" and "expected_outcome" not in case.label


def test_the_injector_and_the_faithful_layer_share_one_fill_threshold():
    from meshpipeline.cad.repair_v4 import limits
    from meshpipeline.cad.repair_v4.ops import faces
    from meshpipeline.cad.repair_v4.verdict import faithful_kernel

    assert inj.limits is limits                                          # read through the module, never copied
    assert not hasattr(faces, "MAX_FILL_SPAN_RATIO")                    # B12: the op no longer caps a fill
    assert faithful_kernel.FILL_SPAN_FRACTION is limits.MAX_FILL_SPAN_RATIO
    assert limits.MAX_FILL_SPAN_RATIO == 0.05                            # design 7 decision 2: not ours to move here


def test_the_injector_follows_the_shared_cap_when_it_moves(fixable_src, tmp_path, monkeypatch):
    """Drift proof: move the ONE constant and the injector moves with it. A second typed 5% in inject.py would not."""
    from meshpipeline.cad.repair_v4 import limits

    with monkeypatch.context() as m:
        m.setattr(limits, "MAX_FILL_SPAN_RATIO", 0.001)                  # nothing is small any more
        with pytest.raises(inj.InjectionNotApplicable, match="no face has a boundary loop within 0.1%"):
            inj.inject(fixable_src, "face_delete", SEED, tmp_path)
        assert inj.inject(fixable_src, "face_delete_large", SEED, tmp_path).label["fill_cap_ratio"] == 0.001
    with monkeypatch.context() as m:
        m.setattr(limits, "MAX_FILL_SPAN_RATIO", 0.99)                   # nothing is large any more
        with pytest.raises(inj.InjectionNotApplicable, match="no face has a boundary loop above 99%"):
            inj.inject(fixable_src, "face_delete_large", SEED, tmp_path)


SEEDS = range(1, 13)


def _op_verdict(path: Path, tmp_path: Path) -> tuple[bool, str]:
    """Run the real `fill_loop` kernel function on the FILE, with the free edges as the search would target them."""
    import meshpipeline.cad.repair_v4.ops  # noqa: F401  (registers the ops; ws_kernel imported alone is circular)
    from meshpipeline.cad.repair.identity import quantum_for
    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.ops import faces as face_ops

    read = STEPControl_Reader()
    assert read.ReadFile(str(path)) == IFSelect_RetDone
    read.TransferRoots()
    brep = str(tmp_path / f"{path.stem}.brep")
    ws_kernel.write_brep(read.OneShape(), brep)
    shape = ws_kernel.read_brep(brep)
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    index = ws_kernel._maps(shape)["edge"]
    free = [index.FindIndex(ancestors.FindKey(i)) for i in range(1, ancestors.Extent() + 1)
            if ancestors.FindFromIndex(i).Size() == 1]
    answer = face_ops._fill_loop({"brep_path": brep, "out_path": str(tmp_path / f"{path.stem}.out.brep"),
                                  "edges": free, "continuity": "G0", "quantum": quantum_for(ws_kernel.diagonal_of(shape))})
    if "declined" in answer:
        return False, str(answer["declined"])
    # B12: the op no longer refuses an unfaithful fill; it reports. The bound is applied to its OWN measurement.
    m = answer.get("params", {}).get("measured", {})
    if float(m["fill_to_surroundings"]) > float(m["bound_mm"]):
        return False, f"over_bound (measured): {m['fill_to_surroundings']} > {m['bound_mm']}"
    return True, ""


def test_face_delete_never_deletes_a_face_the_op_declines_and_inclined_always_does(mixed_src, tmp_path):
    """Both small-face kinds on a part holding a restorable and an unrestorable face, over many seeds. The verdict is the
    op's, run on the written FILE (not the label, not the injector's own call)."""
    seen_fixable_ratio = set()
    for seed in SEEDS:
        fixable = inj.inject(mixed_src, "face_delete", seed, tmp_path)
        accepted, why = _op_verdict(fixable.path, tmp_path)
        assert accepted, f"seed {seed}: face_delete produced a hole the op declines: {why}"
        assert fixable.label["fill_loop_accepts"] is True and fixable.label["status"] == "structural"
        assert fixable.label["fill_fill_to_surroundings"] <= fixable.label["fill_bound_mm"]
        seen_fixable_ratio.add(fixable.label["loop_span_ratio"])

        inclined = inj.inject(mixed_src, "face_delete_inclined", seed, tmp_path)
        accepted, why = _op_verdict(inclined.path, tmp_path)
        assert not accepted and "over_bound" in why, f"seed {seed}: the 'inclined' hole was fillable: {why!r}"
        assert inclined.label["fill_loop_accepts"] is False and "over_bound" in inclined.label["fill_loop_declined"]
        assert inclined.label["status"] == "declined_stratum" and inclined.label["expected_outcome"] == "correctly_declined"
        assert inclined.label["loop_span_ratio"] <= inclined.label["fill_cap_ratio"]   # within the cap: a DIFFERENT reason
    assert len(seen_fixable_ratio) >= 1


def test_face_delete_inclined_is_a_declined_stratum_kind_not_a_default_draw():
    assert inj.KIND_STATUS["face_delete_inclined"] == "declined_stratum"
    assert "face_delete_inclined" not in inj.DEFAULT_KINDS and "face_delete_inclined" in inj.ADMITTED_KINDS
    assert inj.EXPECTED_OUTCOME["face_delete_inclined"] == "correctly_declined"


def test_face_delete_inclined_refuses_a_part_whose_small_faces_are_all_fillable(fixable_src, tmp_path):
    with pytest.raises(inj.InjectionNotApplicable, match="no face has a boundary loop within 5%.*declines"):
        inj.inject(fixable_src, "face_delete_inclined", SEED, tmp_path)


def test_face_delete_refuses_a_part_whose_only_small_face_is_inclined(tmp_path):
    from tests.native._v4_flush_patch_part import chamfer_corner

    only = _write_step(chamfer_corner(BRepPrimAPI_MakeBox(100.0, 60.0, 40.0).Shape(), 3.0), tmp_path / "chamfer.step")
    with pytest.raises(inj.InjectionNotApplicable, match="no face has a boundary loop within 5%.*accepts"):
        inj.inject(only, "face_delete", SEED, tmp_path)
    assert inj.inject(only, "face_delete_inclined", SEED, tmp_path).label["fill_loop_accepts"] is False


def test_face_delete_refuses_a_part_that_has_no_small_face(tmp_path):
    """The T31 trap: every face of a 10 mm box spans 57-82% of its diagonal. The default kind must refuse the box
    rather than hand the benchmark a defect no op may repair."""
    plain = _write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.step")
    with pytest.raises(inj.InjectionNotApplicable, match="no face has a boundary loop within 5%"):
        inj.inject(plain, "face_delete", SEED, tmp_path)


def test_face_delete_large_is_the_labelled_declined_stratum(fixable_src, tmp_path):
    from meshpipeline.cad.repair_v4 import limits

    case = inj.inject(fixable_src, "face_delete_large", SEED, tmp_path)
    assert case.label["kind"] == "face_delete_large" and case.label["status"] == "declined_stratum"
    assert case.label["expected_outcome"] == "correctly_declined"
    measured = _free_loop_ratio(case.path)
    assert measured > limits.MAX_FILL_SPAN_RATIO and case.label["loop_span_ratio"] == pytest.approx(measured, abs=1e-4)
    assert "face_delete_large" not in inj.DEFAULT_KINDS and "face_delete_large" in inj.ADMITTED_KINDS
    assert "face_delete_large" not in inj.QUARANTINED_KINDS
    with pytest.raises(ValueError, match="declined stratum"):             # never weighted into the calibration
        inj.calibrate([case], {"no_volume": 1.0})


def test_assembly_overlap(cases, base):
    case = cases["assembly_overlap"]
    p = _probe(case.path)
    assert base["solids"] == 1 and p["solids"] == 2
    a, b = _all(_read(case.path), TopAbs_SOLID)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(BRepAlgoAPI_Common(a, b).Shape(), props)
    assert props.Mass() > 1.0                                            # genuinely interpenetrating, not merely adjacent
    assert props.Mass() == pytest.approx(case.label["overlap_volume"], rel=1e-2)
    assert [e["body_index"] for e in case.label["entities"]] == [0, 1]


def test_precision_truncate(cases, base, src):
    case = cases["precision_truncate"]
    p = _probe(case.path)
    assert p["max_tol"] > 1e-3 > base["max_tol"]                         # QUARANTINED kind: tolerance-only damage
    assert _vertex_drift(src, case.path) > DEVIATION_RATIO * _diag(_read(src)) / 10   # the cut coordinates really moved
    places = case.label["decimals"]
    ents = case.label["entities"]
    assert ents
    text = {m.group(1): m.group(2) for m in re.finditer(r"^#(\d+)\s*=\s*(?:CARTESIAN_POINT|DIRECTION)\(([^;]*)\);", case.path.read_text(), re.M)}
    for e in ents[:20]:                                                  # the named entities really are short
        nums = re.findall(r"[-+]?\d+\.\d*", text[e["id"][1:]])
        assert nums and all(len(n.split(".")[1]) <= places + 2 for n in nums)


def test_iges_roundtrip(cases, base):
    case = cases["iges_roundtrip"]
    p = _probe(case.path)
    assert base["solids"] == 1 and p["solids"] == 0 and p["free_edges"] > 0   # IGES hands back unsewn trimmed surfaces
    assert Path(case.label["iges_file"]).exists()
    assert len(case.label["entities"]) == base["faces"]


# ---- the interface contract ---------------------------------------------------------------------------------------

def test_every_label_names_its_kind_seed_and_clean_source_and_keeps_fidelity_on_the_damaged_input(cases, src, fixable_src, mixed_src):
    for kind, case in cases.items():
        assert case.kind == kind and case.label["kind"] == kind and case.label["seed"] == SEED
        assert case.label["clean_source"] == str(_source_of(kind, src, fixable_src, mixed_src))
        assert case.label["fidelity_reference"] == "damaged_input"
        assert case.fidelity_reference == case.path != src               # fidelity is measured on the damaged file
        assert case.label["entities"], f"{kind} names no damaged entity"
        json.dumps(case.label)                                           # the label must be storable as it is


def test_unknown_kind_is_refused(src):
    with pytest.raises(ValueError, match="unknown injector kind"):
        inj.inject(src, "nope", SEED)


def _normalised(path: Path) -> str:
    # the writer stamps a per-process counter into product ids; that and the header are not geometry
    text = re.sub(r"(OCCURRENCE\(')\d+", r"\1N", path.read_text())
    text = re.sub(r"translator 7\.\d+ \d+", "translator", text)
    return "\n".join(line for line in text.split("\n") if "FILE_NAME" not in line)


@pytest.mark.parametrize("kind", [k for k in inj.KINDS if k != "pcurve_corrupt"])
def test_same_source_kind_and_seed_give_the_same_damage(kind, src, fixable_src, mixed_src, cases, tmp_path):
    again = inj.inject(_source_of(kind, src, fixable_src, mixed_src), kind, SEED, tmp_path)
    first = cases[kind]
    drop = {"iges_file"}
    assert {k: v for k, v in again.label.items() if k not in drop} == {k: v for k, v in first.label.items() if k not in drop}
    assert _normalised(again.path) == _normalised(first.path)


def test_a_different_seed_damages_differently(fixable_src, tmp_path):
    seen = {json.dumps(inj.inject(fixable_src, "face_delete", s, tmp_path).label["entities"]) for s in range(4)}
    assert len(seen) > 1


def test_inapplicable_kind_is_refused_not_faked(tmp_path):
    plain = _write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.step")
    with pytest.raises(inj.InjectionNotApplicable):
        inj.inject(plain, "seam_variant", SEED, tmp_path)
    with pytest.raises(inj.InjectionNotApplicable):
        inj.inject(plain, "loose_reapprox", SEED, tmp_path)


def test_quarantine_is_explicit_and_labelled(cases):
    assert set(inj.QUARANTINED_KINDS) == {"loose_reapprox", "tolerance_inflate", "seam_variant", "precision_truncate"}
    assert not set(inj.DEFAULT_KINDS) & set(inj.QUARANTINED_KINDS) and "pcurve_corrupt" not in inj.DEFAULT_KINDS
    assert set(inj.DEFAULT_KINDS) == {"unsew_perturb", "small_entities", "face_delete", "assembly_overlap", "iges_roundtrip"}
    for kind, case in cases.items():
        assert case.label["status"] == inj.KIND_STATUS[kind]


def test_a_noop_injection_is_refused_not_written(tmp_path):
    # integer coordinates: truncating them changes nothing, so there is nothing to label
    plain = _write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.step")
    with pytest.raises(inj.InjectionNotApplicable, match="nothing"):
        inj.inject(plain, "precision_truncate", SEED, tmp_path)
    assert not (tmp_path / "box.precision_truncate.1.step").exists()


def test_precision_truncate_reads_a_source_whose_exporter_wraps_lines(src, tmp_path):
    wrapped = tmp_path / "wrapped.step"
    wrapped.write_text(re.sub(r"(CARTESIAN_POINT\('',)\(", r"\1\n  (", src.read_text()).replace(",", ",\n   ", 40))
    case = inj.inject(wrapped, "precision_truncate", SEED, tmp_path)
    assert case.label["entities"]


# ---- calibrate ----------------------------------------------------------------------------------------------------

def _case(kind, observed=None):
    label = {"observed_family": observed} if observed else {}
    return inj.InjectedCase(path=Path("x.step"), kind=kind, label=label)


def test_target_histogram_is_the_real_corpus_histogram():
    hist = inj.target_histogram(CORPUS)
    assert sum(hist.values()) == pytest.approx(1.0)
    files = json.loads(CORPUS.read_text())["files"]
    assert hist["overlapping_facets"] == pytest.approx(
        sum("overlapping facets" in f["gmsh_error"] for f in files) / len(files))
    assert hist["overlapping_facets"] > hist["plc_intersection"] > 0.05


def test_calibrate_from_observed_families_reproduces_the_target_it_can_reach():
    # Observed families chosen here, independent of any prior; the expected weights are worked out by hand from the
    # real histogram: two kinds share one family, the others are alone, the unreached families are reported dropped.
    hist = inj.target_histogram(CORPUS)
    cases = [_case("face_delete", "no_volume"), _case("unsew_perturb", "open_loop"),
             _case("assembly_overlap", "plc_intersection"), _case("small_entities", "plc_intersection")]
    with warnings.catch_warnings():
        warnings.simplefilter("error")                                   # observed cases must not warn
        weights = inj.calibrate(cases, hist)
    reach = hist["no_volume"] + hist["open_loop"] + hist["plc_intersection"]
    assert weights == {"face_delete": pytest.approx(hist["no_volume"] / reach),
                       "unsew_perturb": pytest.approx(hist["open_loop"] / reach),
                       "assembly_overlap": pytest.approx(hist["plc_intersection"] / reach / 2),
                       "small_entities": pytest.approx(hist["plc_intersection"] / reach / 2)}
    assert weights.basis == "observed"
    assert weights.dropped_mass == pytest.approx(1.0 - reach)
    assert weights.dropped_mass > 0.3 and "overlapping_facets" in weights.dropped_families   # the large unmatched share


def test_calibrate_says_so_when_it_leans_on_the_prior():
    hist = inj.target_histogram(CORPUS)
    with pytest.warns(UserWarning, match="no observed_family"):
        weights = inj.calibrate([_case("face_delete"), _case("unsew_perturb", "open_loop")], hist)
    assert weights.basis == "mixed"
    with pytest.warns(UserWarning):
        assert inj.calibrate([_case("face_delete")], hist).basis == "prior"


def test_calibrate_refuses_a_kind_with_neither_observation_nor_prior():
    hist = inj.target_histogram(CORPUS)
    for kind in inj.QUARANTINED_KINDS:
        assert kind not in inj.KIND_FAMILY_PRIOR
        with pytest.raises(ValueError, match="no prior"):
            inj.calibrate([_case(kind)], hist)
    assert "pcurve_corrupt" not in inj.KIND_FAMILY_PRIOR


def test_calibrate_refuses_when_nothing_is_reachable():
    with pytest.raises(ValueError):
        inj.calibrate([_case("face_delete", "no_volume")], {"plc_intersection": 1.0})
