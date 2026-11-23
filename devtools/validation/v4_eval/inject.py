# Responsibility: Damage clean STEP parts in controlled, labelled, seed-reproducible ways for the CRS v4 benchmark.
# Boundaries: needs OCP (native tier). It labels damage; it does not judge it - the v4 checker does.
#
# Design 7.2: for an injected file "the original" used for fidelity is the DAMAGED INPUT (`InjectedCase.path`),
# never the clean source. The product never sees the clean source; it is consulted here only to label the
# defect (`label["clean_source"]`, `label["entities"]`), and `InjectedCase.fidelity_reference` says so.
from __future__ import annotations

import json
import math
import random
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools, BRepTools_ReShape
from OCP.GeomAbs import GeomAbs_C2, GeomAbs_Plane
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

KINDS: tuple[str, ...] = (
    "loose_reapprox", "unsew_perturb", "tolerance_inflate", "pcurve_corrupt", "seam_variant",
    "small_entities", "face_delete", "assembly_overlap", "precision_truncate", "iges_roundtrip",
)


class InjectionNotApplicable(ValueError):
    """The source has nothing this kind can damage (e.g. seam_variant on a part with no periodic face)."""


class InjectionInfeasible(InjectionNotApplicable):
    """The kind cannot produce damage that survives writing the file and reading it back."""


@dataclass(frozen=True)
class InjectedCase:
    path: Path
    kind: str
    label: dict[str, Any]

    @property
    def fidelity_reference(self) -> Path:
        """What fidelity is measured against: the damaged input itself, never the clean source (design 7.2)."""
        return self.path


# ---- shape helpers --------------------------------------------------------------------------------

def read_step(path: Path) -> TopoDS_Shape:
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read STEP: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def write_step(shape: TopoDS_Shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot write STEP: {path}")


def _entities(shape: TopoDS_Shape, kind: Any) -> list[TopoDS_Shape]:
    """Sub-shapes in OCCT's map order: deterministic for one shape, so an index names one entity."""
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _diag(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _center_area(face: TopoDS_Shape) -> tuple[list[float], float]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return [round(c.X(), 6), round(c.Y(), 6), round(c.Z(), 6)], round(props.Mass(), 6)


def _edge_mid(edge: TopoDS_Shape) -> list[float]:
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    c = props.CentreOfMass()
    return [round(c.X(), 6), round(c.Y(), 6), round(c.Z(), 6)]


def _face_ref(shape: TopoDS_Shape, faces: Sequence[TopoDS_Shape], i: int) -> dict[str, Any]:
    center, area = _center_area(faces[i])
    return {"type": "face", "src_index": i, "center": center, "area": area}


def _edge_ref(edges: Sequence[TopoDS_Shape], i: int) -> dict[str, Any]:
    return {"type": "edge", "src_index": i, "center": _edge_mid(edges[i])}


def _copy(shape: TopoDS_Shape) -> TopoDS_Shape:
    return BRepBuilderAPI_Copy(shape, True).Shape()


def _pick(rng: random.Random, pool: Sequence[int], n: int) -> list[int]:
    return sorted(rng.sample(list(pool), min(n, len(pool))))


def _is_plane(face: TopoDS_Shape) -> bool:
    return BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_Plane


def _has_seam(face: TopoDS_Shape) -> bool:
    """A face that wraps round its own surface carries one edge twice: the seam."""
    f = TopoDS.Face_s(face)
    return any(BRep_Tool.IsClosed_s(TopoDS.Edge_s(e), f) for e in _entities(face, TopAbs_EDGE))


# ---- the ten injectors. Each returns (damaged shape or None, label fields); STEP-text kinds write themselves ----

@dataclass(frozen=True)
class _Ctx:
    src: Path      # the clean source: for labelling, and for the kinds that damage the file text
    out: Path      # where the damaged STEP goes


Injector = Callable[[TopoDS_Shape, random.Random, _Ctx], tuple[TopoDS_Shape | None, dict[str, Any]]]


def _loose_reapprox(shape, rng, work):
    faces = _entities(shape, TopAbs_FACE)
    candidates = [i for i, f in enumerate(faces) if not _is_plane(f) and not _has_seam(f)]
    if not candidates:
        raise InjectionNotApplicable("loose_reapprox needs a non-planar, non-periodic face")
    chosen = _pick(rng, candidates, 2)
    out = _copy(shape)
    copies = _entities(out, TopAbs_FACE)
    grid = 3 + rng.randrange(2)          # a coarse 3x3 / 4x4 sample, interpolated at degree 2: visibly loose
    builder = BRep_Builder()
    for i in chosen:
        face = TopoDS.Face_s(copies[i])
        surf = BRep_Tool.Surface_s(face)
        tol = BRep_Tool.Tolerance_s(face)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        pts = TColgp_Array2OfPnt(1, grid, 1, grid)
        for a in range(grid):
            for b in range(grid):
                pts.SetValue(a + 1, b + 1, surf.Value(u0 + (u1 - u0) * a / (grid - 1), v0 + (v1 - v0) * b / (grid - 1)))
        fit = GeomAPI_PointsToBSplineSurface(pts, 2, 2, GeomAbs_C2, 1.0).Surface()
        _fit_to_uv_box(fit, u0, u1, v0, v1)
        pcurves = []
        for edge in _entities(face, TopAbs_EDGE):
            pc = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(edge), face, 0.0, 0.0)
            if pc is not None:
                pcurves.append((TopoDS.Edge_s(edge), pc))
        builder.UpdateFace(face, fit, TopLoc_Location(), tol)
        for edge, pc in pcurves:             # the edges keep their curves; the surface under them is what moved
            builder.UpdateEdge(edge, pc, fit, TopLoc_Location(), BRep_Tool.Tolerance_s(edge))
    return out, {"entities": [_face_ref(shape, faces, i) for i in chosen], "grid": grid}


def _fit_to_uv_box(fit, u0, u1, v0, v1) -> None:
    """The fit is parametrised on its own range; put it back on the face's UV box so old pcurves still apply."""
    uk, vk = fit.UKnots(), fit.VKnots()
    a0, a1 = uk.Value(uk.Lower()), uk.Value(uk.Upper())
    b0, b1 = vk.Value(vk.Lower()), vk.Value(vk.Upper())
    for k in range(uk.Lower(), uk.Upper() + 1):
        uk.SetValue(k, u0 + (u1 - u0) * (uk.Value(k) - a0) / (a1 - a0))
    for k in range(vk.Lower(), vk.Upper() + 1):
        vk.SetValue(k, v0 + (v1 - v0) * (vk.Value(k) - b0) / (b1 - b0))
    fit.SetUKnots(uk)
    fit.SetVKnots(vk)


def _unsew_perturb(shape, rng, work):
    faces = _entities(shape, TopAbs_FACE)
    diag = _diag(shape)
    moved = _pick(rng, range(len(faces)), max(1, len(faces) // 3))
    shell, builder = TopoDS.Shell_s(_new_shell()), BRep_Builder()
    shifts: dict[int, list[float]] = {}
    for i, face in enumerate(faces):
        piece = _copy(face)                      # a copy owns its own edges: no neighbour shares them any more
        if i in moved:
            d = [rng.uniform(-1, 1) for _ in range(3)]
            n = math.sqrt(sum(c * c for c in d)) or 1.0
            mag = diag * 2e-3 * rng.uniform(1.0, 2.0)
            shifts[i] = [round(c / n * mag, 9) for c in d]
            t = gp_Trsf()
            t.SetTranslation(gp_Vec(*shifts[i]))
            piece = BRepBuilderAPI_Transform(piece, t, True).Shape()
        builder.Add(shell, piece)
    return shell, {"entities": [{**_face_ref(shape, faces, i), "shift": shifts[i]} for i in moved],
                   "unsewn_faces": len(faces)}


def _new_shell() -> TopoDS_Shape:
    from OCP.TopoDS import TopoDS_Shell
    shell = TopoDS_Shell()
    BRep_Builder().MakeShell(shell)
    return shell


def _tolerance_inflate(shape, rng, work):
    # An edge's own tolerance does not survive STEP (the file carries no per-edge tolerance), and neither
    # does the declared model uncertainty (the reader ignores it). What does survive is the geometry a
    # cross-kernel export really leaves behind: a vertex that no longer sits on its curves. The reader
    # answers a vertex-to-curve gap by raising the tolerance to cover it. So the damage is that gap.
    vertices = _entities(shape, TopAbs_VERTEX)
    edges = _entities(shape, TopAbs_EDGE)
    chosen = _pick(rng, range(len(vertices)), max(2, len(vertices) // 4))
    out = _copy(shape)
    copies = _entities(out, TopAbs_VERTEX)
    diag, builder = _diag(shape), BRep_Builder()
    refs = []
    for i in chosen:
        d = [rng.uniform(-1, 1) for _ in range(3)]
        n = math.sqrt(sum(c * c for c in d)) or 1.0
        mag = diag * 1e-3 * rng.uniform(1.0, 2.0)
        v = TopoDS.Vertex_s(copies[i])
        p = BRep_Tool.Pnt_s(v)
        shift = [round(c / n * mag, 9) for c in d]
        builder.UpdateVertex(v, gp_Pnt(p.X() + shift[0], p.Y() + shift[1], p.Z() + shift[2]), mag * 1.01)
        q = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices[i]))
        refs.append({"type": "vertex", "src_index": i, "center": [round(q.X(), 6), round(q.Y(), 6), round(q.Z(), 6)],
                     "shift": shift, "gap": round(mag, 9)})
    touched = sorted(j for j, e in enumerate(edges)
                     if any(TopoDS.Vertex_s(vertices[i]).IsSame(v) for i in chosen for v in _entities(e, TopAbs_VERTEX)))
    return out, {"entities": refs + [_edge_ref(edges, j) for j in touched], "min_gap": min(r["gap"] for r in refs)}


def _pcurve_corrupt(shape, rng, ctx):
    """INFEASIBLE through STEP, deliberately not shipped as a damager.

    Pcurves can be corrupted in memory (BRepCheck_Analyzer then says invalid) and the corrupted pcurve is written
    into the file. But OCCT's STEP reader rebuilds pcurves on import (its FromSTEP shape-processing sequence), so
    a default read returns the clean pcurves: a translated pcurve, a bowed one, a reversed one and a swapped one
    were each tried on a filleted box, and each read back valid with tolerance 1e-7, indistinguishable from the
    source. A file like that would sit in the benchmark as an unbroken "defect". The damage a pcurve fault leaves
    behind once healed is an edge that does not lie on its face; `loose_reapprox` and `seam_variant` inject that.
    """
    raise InjectionInfeasible(
        "pcurve_corrupt: the STEP reader rebuilds pcurves on import, so no pcurve damage survives a default read; "
        "use loose_reapprox or seam_variant for the edge-off-surface damage it would leave")


def _seam_variant(shape, rng, work):
    """Put the seam's 3D curve at a different angle than the cylinder's own seam: two exporters disagreeing on where
    the cylinder is cut. (Corrupting the seam's pcurves instead was tried: the STEP reader rebuilds them, so the damage
    never reaches a consumer.)"""
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Ax1

    faces = _entities(shape, TopAbs_FACE)
    seamed = [i for i, f in enumerate(faces)
              if _has_seam(f) and BRepAdaptor_Surface(TopoDS.Face_s(f)).GetType() == GeomAbs_Cylinder]
    if not seamed:
        raise InjectionNotApplicable("seam_variant needs a cylindrical face with a seam")
    i = rng.choice(seamed)
    out = _copy(shape)
    face = TopoDS.Face_s(_entities(out, TopAbs_FACE)[i])
    seam = next(TopoDS.Edge_s(e) for e in _entities(face, TopAbs_EDGE) if BRep_Tool.IsClosed_s(TopoDS.Edge_s(e), face))
    axis = BRepAdaptor_Surface(face).Cylinder().Axis()
    angle = math.radians(rng.uniform(5.0, 20.0)) * rng.choice([-1, 1])
    first, last = BRep_Tool.Range_s(seam)
    curve = BRep_Tool.Curve_s(seam, 0.0, 0.0)     # location-resolved copy
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(axis.Location(), axis.Direction()), angle)
    moved = curve.Transformed(t)
    builder = BRep_Builder()
    builder.UpdateEdge(seam, moved, TopLoc_Location(), BRep_Tool.Tolerance_s(seam))
    builder.Range(seam, first, last)
    seam_ref = {"type": "seam_edge", "face_src_index": i, "face_center": _center_area(faces[i])[0],
                "edge_center": _edge_mid(seam),
                "angle_rad": round(angle, 9)}
    return out, {"entities": [seam_ref], "variant": "seam_rotated"}


def _small_entities(shape, rng, work):
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2
    from OCP.TopAbs import TopAbs_IN

    faces = _entities(shape, TopAbs_FACE)
    planar = [i for i, f in enumerate(faces) if _is_plane(f)]
    if not planar:
        raise InjectionNotApplicable("small_entities needs a planar face to notch")
    diag = _diag(shape)
    radius, depth = diag * 1e-3 * rng.uniform(1.0, 2.0), diag * 2e-3
    for _ in range(200):
        i = rng.choice(planar)
        face = TopoDS.Face_s(faces[i])
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        u, v = rng.uniform(u0, u1), rng.uniform(v0, v1)
        ok = all(BRepClass_FaceClassifier(face, gp_Pnt2d(u + du, v + dv), 1e-7).State() == TopAbs_IN
                 for du in (-3 * radius, 0.0, 3 * radius) for dv in (-3 * radius, 0.0, 3 * radius))
        if ok:
            break
    else:
        raise InjectionNotApplicable("small_entities found no room on any planar face")
    adaptor = BRepAdaptor_Surface(face)
    p, n = adaptor.Value(u, v), adaptor.Plane().Axis().Direction()
    cutter = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(p.X() - n.X() * depth, p.Y() - n.Y() * depth, p.Z() - n.Z() * depth), n), radius, 2 * depth).Shape()
    out = BRepAlgoAPI_Cut(shape, cutter).Shape()
    before = {tuple(_center_area(f)[0]) for f in faces}
    tiny = [f for f in _entities(out, TopAbs_FACE) if _center_area(f)[1] < math.pi * (3 * radius) ** 2 + 1e-9
            and tuple(_center_area(f)[0]) not in before]
    refs = []
    for f in tiny:
        c, a = _center_area(f)
        refs.append({"type": "face", "src_index": None, "center": c, "area": a})
    return out, {"entities": refs, "host_face": _face_ref(shape, faces, i), "radius": radius, "depth": depth,
                 "at": [round(p.X(), 6), round(p.Y(), 6), round(p.Z(), 6)]}


def _face_delete(shape, rng, work):
    faces = _entities(shape, TopAbs_FACE)
    i = rng.randrange(len(faces))
    reshape = BRepTools_ReShape()
    reshape.Remove(faces[i])
    return reshape.Apply(shape), {"entities": [_face_ref(shape, faces, i)]}


def _assembly_overlap(shape, rng, work):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    extents = [x1 - x0, y1 - y0, z1 - z0]
    axis = rng.randrange(3)
    shift = [0.0, 0.0, 0.0]
    shift[axis] = extents[axis] * rng.uniform(0.15, 0.35) * rng.choice([-1, 1])   # less than the part's width: they interpenetrate
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*shift))
    twin = BRepBuilderAPI_Transform(_copy(shape), t, True).Shape()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(BRepAlgoAPI_Common(shape, twin).Shape(), props)
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, twin)
    bodies = [{"type": "body", "body_index": 0, "role": "original"},
              {"type": "body", "body_index": 1, "role": "translated_copy", "shift": [round(c, 9) for c in shift]}]
    return compound, {"entities": bodies, "overlap_volume": round(props.Mass(), 6)}


_REAL = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TUPLE_LINE = re.compile(r"^(#(\d+)\s*=\s*(CARTESIAN_POINT|DIRECTION)\s*\(\s*'[^']*'\s*,\s*\()([^)]*)(\)\s*\)\s*;)\s*$")


def _truncate(x: float, places: int) -> str:
    f = 10 ** places
    return f"{math.trunc(x * f) / f:.{places}f}"


def _precision_truncate(shape, rng, ctx):
    """Re-export with the coordinates cut to few decimals, as a lossy exporter or a hand-trimmed file does."""
    quantum = _diag(shape) * 2e-3
    places = max(0, math.ceil(-math.log10(quantum))) + rng.choice([0, 1])
    lines, changed = ctx.src.read_text().split("\n"), []
    for n, line in enumerate(lines):
        m = _TUPLE_LINE.match(line.strip())
        if not m:
            continue
        head, ident, entity, body, tail = m.groups()
        p = places if entity == "CARTESIAN_POINT" else places + 2   # a direction cut as hard as a point is not a direction
        values = re.findall(_REAL, body)
        if len(values) != 3:                 # 2D parameter-space points are healed on read; only model-space ones matter
            continue
        new_body = ",".join(_truncate(float(x), p) for x in values)
        if new_body != ",".join(x.strip() for x in body.split(",")):
            lines[n] = f"{head}{new_body}{tail}"
            changed.append({"type": "step_entity", "id": f"#{ident}", "entity": entity})
    ctx.out.write_text("\n".join(lines))
    return None, {"entities": changed, "decimals": places, "scope": "whole_file"}


def _iges_roundtrip(shape, rng, ctx):
    """Send the part through IGES and back. IGES carries trimmed surfaces, not a sewn solid, at its own precision."""
    writer = IGESControl_Writer("MM", 0)
    writer.AddShape(shape)
    writer.ComputeModel()
    igs = ctx.out.with_suffix(".igs")
    if not writer.Write(str(igs)):
        raise ValueError("IGES write failed")
    reader = IGESControl_Reader()
    if reader.ReadFile(str(igs)) != IFSelect_RetDone:
        raise ValueError("IGES read failed")
    reader.TransferRoots()
    faces = _entities(shape, TopAbs_FACE)
    return reader.OneShape(), {"entities": [_face_ref(shape, faces, i) for i in range(len(faces))],
                               "scope": "whole_file", "iges_file": str(igs)}


_INJECTORS: dict[str, Injector] = {
    "loose_reapprox": _loose_reapprox,
    "unsew_perturb": _unsew_perturb,
    "tolerance_inflate": _tolerance_inflate,
    "pcurve_corrupt": _pcurve_corrupt,
    "seam_variant": _seam_variant,
    "small_entities": _small_entities,
    "face_delete": _face_delete,
    "assembly_overlap": _assembly_overlap,
    "precision_truncate": _precision_truncate,
    "iges_roundtrip": _iges_roundtrip,
}


def inject(src_step: Path | str, kind: str, seed: int, out_dir: Path | None = None) -> InjectedCase:
    """Damage `src_step` with `kind`; the same source, kind and seed always give the same damage."""
    if kind not in _INJECTORS:
        raise ValueError(f"unknown injector kind {kind!r}; known: {', '.join(KINDS)}")
    src = Path(src_step)
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="v4-inject-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.{kind}.{seed}.step"
    rng = random.Random(f"{kind}:{seed}")
    shape = read_step(src)
    damaged, fields = _INJECTORS[kind](shape, rng, _Ctx(src=src, out=out))
    if damaged is not None:
        write_step(damaged, out)
    label = {
        "kind": kind, "seed": seed, "clean_source": str(src),
        "fidelity_reference": "damaged_input",     # design 7.2: the clean source only labels the defect
        **fields,
    }
    return InjectedCase(path=out, kind=kind, label=label)


# ---- calibration against the real failure histogram ------------------------------------------------

#: gmsh error text -> failure family, in match order. The families are those of mesh-failure-real.json.
_FAMILIES: tuple[tuple[str, str], ...] = (
    ("overlapping facets", "overlapping_facets"),
    ("PLC Error", "plc_intersection"),
    ("timeout", "timeout_crash"),
    ("crash", "timeout_crash"),
    ("periodic surface", "periodic_surface"),
    ("closed loop", "open_loop"),
    ("no volume elements", "no_volume"),
    ("Could not recover boundary", "boundary_recovery"),
)

#: What each kind is EXPECTED to fail as in gmsh. A prior, not a measurement: the re-probe (Task 8) runs gmsh
#: on the injected files and records the observed family in `label["observed_family"]`, which wins.
KIND_FAMILY_PRIOR: dict[str, str] = {
    "loose_reapprox": "overlapping_facets", "unsew_perturb": "open_loop", "tolerance_inflate": "plc_intersection",
    "pcurve_corrupt": "overlapping_facets", "seam_variant": "periodic_surface", "small_entities": "overlapping_facets",
    "face_delete": "no_volume", "assembly_overlap": "plc_intersection", "precision_truncate": "overlapping_facets",
    "iges_roundtrip": "open_loop",
}


def failure_family(gmsh_error: str) -> str:
    for needle, family in _FAMILIES:
        if needle in gmsh_error:
            return family
    return "other"


def target_histogram(corpus: Path | str) -> dict[str, float]:
    """Failure-family shares of mesh-failure-real.json (sums to 1)."""
    files = json.loads(Path(corpus).read_text())["files"]
    counts = Counter(failure_family(f["gmsh_error"]) for f in files)
    total = sum(counts.values())
    return {family: n / total for family, n in sorted(counts.items())}


def _case_family(case: InjectedCase) -> str:
    return str(case.label.get("observed_family") or KIND_FAMILY_PRIOR[case.kind])


def calibrate(cases: Sequence[InjectedCase], target_hist: dict[str, float]) -> dict[str, float]:
    """Weight each kind so a weighted draw of `cases` reproduces `target_hist` over failure families.

    A kind belongs to the family most of its cases fall in; a family's target share is split evenly among the kinds
    present in it. Families no case reaches cannot be matched: their share is dropped and the rest renormalised.
    """
    by_kind: dict[str, Counter[str]] = {}
    for c in cases:
        by_kind.setdefault(c.kind, Counter())[_case_family(c)] += 1
    kind_family = {k: sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, cnt in by_kind.items()}
    members: dict[str, list[str]] = {}
    for kind, family in sorted(kind_family.items()):
        members.setdefault(family, []).append(kind)
    reachable = {f: target_hist.get(f, 0.0) for f in members}
    total = sum(reachable.values())
    if total <= 0:
        raise ValueError("no injected kind reaches any family with weight in the target histogram")
    return {kind: reachable[family] / total / len(kinds) for family, kinds in members.items() for kind in kinds}
