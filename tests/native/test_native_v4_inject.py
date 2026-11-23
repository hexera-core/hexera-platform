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
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepCheck", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf  # noqa: E402
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
def cases(src, out_dir) -> dict:
    """Every feasible kind, injected once. Cached: each is a real OCCT job, slow under emulation."""
    return {k: inj.inject(src, k, SEED, out_dir) for k in inj.KINDS if k != "pcurve_corrupt"}


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


@pytest.fixture(scope="module")
def base(src) -> dict:
    return _probe(src)


def test_the_fixture_is_clean(base):
    assert base["solids"] == 1 and base["free_edges"] == 0 and base["valid"]
    assert base["max_tol"] < 1e-5


# ---- per kind: the damage is real in the file, and the label names it ---------------------------------------------

def test_loose_reapprox(cases, base):
    case = cases["loose_reapprox"]
    p = _probe(case.path)
    assert p["valid"] and p["max_tol"] > 1e-3 > base["max_tol"]          # edges no longer lie on their refitted faces
    ents = case.label["entities"]
    assert ents and all(e["type"] == "face" for e in ents)
    for e in ents:                                                       # the damage is on the NAMED faces
        assert _nearest_face_edge_tol(case.path, e["center"]) > 1e-3


def test_unsew_perturb(cases, base):
    case = cases["unsew_perturb"]
    p = _probe(case.path)
    assert base["free_edges"] == 0 and p["free_edges"] > 0 and p["solids"] == 0
    assert p["faces"] == base["faces"]                                   # nothing deleted, only unsewn
    assert case.label["entities"] and all(len(e["shift"]) == 3 for e in case.label["entities"])


def test_tolerance_inflate(cases, base):
    case = cases["tolerance_inflate"]
    p = _probe(case.path)
    assert p["valid"] and p["solids"] == 1 and p["max_tol"] > 1e-3 > base["max_tol"]
    verts = [e for e in case.label["entities"] if e["type"] == "vertex"]
    assert verts and any(e["type"] == "edge" for e in case.label["entities"])
    shape = _read(case.path)
    for v in verts:                                                      # the named vertex now carries the gap
        want = [c + s for c, s in zip(v["center"], v["shift"])]
        near = min(_all(shape, TopAbs_VERTEX), key=lambda x: math.dist(BRep_Tool.Pnt_s(TopoDS.Vertex_s(x)).Coord(), want))
        assert BRep_Tool.Tolerance_s(TopoDS.Vertex_s(near)) >= 0.5 * v["gap"]


def test_pcurve_corrupt_is_infeasible_and_says_so(src, out_dir):
    # Pcurve damage does not survive OCCT's STEP reader (it rebuilds pcurves on import), so the injector must refuse
    # rather than write a file that reads back clean. See the injector's docstring for what was tried.
    with pytest.raises(inj.InjectionInfeasible, match="rebuilds pcurves"):
        inj.inject(src, "pcurve_corrupt", SEED, out_dir)


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


def test_face_delete(cases, base):
    case = cases["face_delete"]
    p = _probe(case.path)
    assert p["faces"] == base["faces"] - 1 and p["free_edges"] > 0 and p["solids"] == 0
    (e,) = case.label["entities"]
    assert not _face_exists(case.path, e["center"], e["area"])           # the named face is the one that is gone


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


def test_precision_truncate(cases, base):
    case = cases["precision_truncate"]
    p = _probe(case.path)
    assert p["max_tol"] > 1e-3 > base["max_tol"]                         # vertices and curves no longer meet exactly
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

def test_every_label_names_its_kind_seed_and_clean_source_and_keeps_fidelity_on_the_damaged_input(cases, src):
    for kind, case in cases.items():
        assert case.kind == kind and case.label["kind"] == kind and case.label["seed"] == SEED
        assert case.label["clean_source"] == str(src)
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
def test_same_source_kind_and_seed_give_the_same_damage(kind, src, cases, tmp_path):
    again = inj.inject(src, kind, SEED, tmp_path)
    first = cases[kind]
    drop = {"iges_file"}
    assert {k: v for k, v in again.label.items() if k not in drop} == {k: v for k, v in first.label.items() if k not in drop}
    assert _normalised(again.path) == _normalised(first.path)


def test_a_different_seed_damages_differently(src, tmp_path):
    seen = {json.dumps(inj.inject(src, "face_delete", s, tmp_path).label["entities"]) for s in range(4)}
    assert len(seen) > 1


def test_inapplicable_kind_is_refused_not_faked(tmp_path):
    plain = _write_step(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.step")
    with pytest.raises(inj.InjectionNotApplicable):
        inj.inject(plain, "seam_variant", SEED, tmp_path)
    with pytest.raises(inj.InjectionNotApplicable):
        inj.inject(plain, "loose_reapprox", SEED, tmp_path)


# ---- calibrate ----------------------------------------------------------------------------------------------------

def _fake(kind, observed=None):
    label = {"observed_family": observed} if observed else {}
    return inj.InjectedCase(path=Path("x.step"), kind=kind, label=label)


def test_target_histogram_is_the_real_corpus_histogram():
    hist = inj.target_histogram(CORPUS)
    assert sum(hist.values()) == pytest.approx(1.0)
    files = json.loads(CORPUS.read_text())["files"]
    assert hist["overlapping_facets"] == pytest.approx(
        sum("overlapping facets" in f["gmsh_error"] for f in files) / len(files))
    assert hist["overlapping_facets"] > hist["plc_intersection"] > 0.05


def test_calibrate_reproduces_the_target_over_reachable_families():
    hist = inj.target_histogram(CORPUS)
    cases = [_fake(k) for k in inj.KINDS]
    weights = inj.calibrate(cases, hist)
    assert set(weights) == set(inj.KINDS) and sum(weights.values()) == pytest.approx(1.0)
    induced: dict[str, float] = {}
    for kind, w in weights.items():
        induced[inj.KIND_FAMILY_PRIOR[kind]] = induced.get(inj.KIND_FAMILY_PRIOR[kind], 0.0) + w
    reach = sum(hist[f] for f in induced)
    for family, share in induced.items():
        assert share == pytest.approx(hist[family] / reach)


def test_observed_family_overrides_the_prior():
    hist = {"no_volume": 0.5, "plc_intersection": 0.5}
    weights = inj.calibrate([_fake("face_delete", "plc_intersection"), _fake("assembly_overlap")], hist)
    assert weights == {"face_delete": pytest.approx(0.5), "assembly_overlap": pytest.approx(0.5)}   # one family, shared


def test_calibrate_refuses_when_nothing_is_reachable():
    with pytest.raises(ValueError):
        inj.calibrate([_fake("face_delete")], {"plc_intersection": 1.0})
