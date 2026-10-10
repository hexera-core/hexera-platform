# Responsibility: Measure, before any engine is chosen, the shape facts an engine recommendation reads.
# Boundaries: engine-neutral measurement on triangles; it chooses no engine, refuses nothing and never fails a run.
# Collaborates with: engines/fitness.py (which reads these facts), cad/ingest/canonical.py, cad/scout.py.
"""SHAPE TRAITS - what kind of shape this is, MEASURED from the file, never guessed from its name.

The engine recommendation compares a new geometry with the shapes the lab has meshed. That needs
a handful of facts every geometry has, measured the same way on a STEP solid and on an STL, on an
internal passage and on an external body, cheaply enough to run while the user is still talking
to the intake (a few seconds on a large car). They are mostly DIMENSIONLESS, so a part drawn in
inches compares with one drawn in millimetres without knowing either unit.

What is measured, on the welded triangles of the surface:
* bodies (`n_solids`), whether the surface is closed, and its `genus` - the handles of a closed
  surface: a through-hole, a tube crossing a shell (a tube bank shows up as many), a duct through
  a nacelle. Euler's formula, V - E + F, on the welded mesh.
* `sharp_edges` - the length of edges where the surface turns by more than 40 degrees, per square
  root of the area (how much sharp detail), and `knife_edges` - the same for edges turning by more
  than 135 degrees (knife edges: thin trailing edges, blade tips).
* FLUID CHORDS - from 1,500 points spread over the surface by area, a ray along the surface normal
  INTO THE FLUID to the wall facing back across it (the passage the flow goes through: inward for a
  fluid-domain solid, outward for a pipe wall or an external body), and one into the solid (its
  thickness). From them:
    - `passage_rel`: the median fluid chord over the part's size (`passage`: the same chord in
      metres, when the file's unit is known);
    - `neck`: the narrowest chords (5th percentile) over the median - a throat, a pinch;
    - `scale_ratio`: the part's size over the narrowest chord - how many narrowest gaps fit along it;
    - `slenderness`: wetted area / (pi * median chord^2) - about length / diameter for a tube,
      about 2 for a ball-like chamber;
    - `gap_vs_port`: the median chord over the LARGEST declared opening's hydraulic diameter -
      about 1 in a round tube (a tree of branches too: its trunk is as wide as its biggest port),
      about 0.5 in an annulus or a flat duct (the chord spans the gap, the hydraulic diameter twice
      it), above 1.4 in a chamber much wider than its ports;
    - `narrow_vs_volume`: the narrowest chords over the cube root of the fluid volume - times the
      cube root of a cell budget, about the cells a uniform mesh of that budget puts across the
      narrowest passage (`cells_across_at_budget`, engines/fitness.py);
    - `thin_wall_fraction`: the share of the wetted wall or body whose solid is thinner than 1%
      of the part's size;
    - `thickness_ratio` (external): the body's smallest cross-flow extent over its streamwise
      extent - a wing reads ~0.1, a car ~0.3, a cylinder in cross-flow 1, a disc facing the flow
      more;
    - `gap_share` (external): the share of the body that faces another wall within 5% of its
      length (wheel arches, slat gaps, blades close together).

A fact that cannot be measured is None, never a guess: an open or non-manifold surface has no
genus, a body with no fluid chords has no passage. Nothing here fails a run - a file it cannot
read gives empty traits and the recommendation says it has no measurement.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: Bumped whenever a measurement changes meaning: the fitness table records the version its
#: shapes were measured with, and a mismatch is reported instead of compared silently.
TRAITS_VERSION = 2
#: Rays cast from this many surface points (area-weighted, fixed seed: the same file always reads
#: the same). 1,500 reads a median to a few percent and costs well under a second.
RAY_SAMPLES = 1500
#: An edge turning by more than this is a feature edge; by more than KNIFE_DEG a knife edge.
FEATURE_DEG = 40.0
KNIFE_DEG = 135.0
#: A chord is read only when the wall it lands on faces back across it (cosine): a ray meeting a
#: wall at a glance runs along the passage, not across it (engines/radius_field.py, same rule).
CHORD_MIN_FACING = 0.5
#: Wall or body thinner than this share of the part's size counts as thin.
THIN_WALL_REL = 0.01
#: External: another wall within this share of the streamwise length is a narrow gap.
NARROW_GAP_REL = 0.05
#: Past this many triangles the chords are skipped (the topology and edges are still read).
MAX_RAY_TRIANGLES = 4_000_000
#: A port ratio outside this band means the ports and the triangles are in different units.
_PORT_RATIO_BAND = (0.02, 50.0)


@dataclass(frozen=True)
class ShapeTraits:
    """The measured facts of one geometry. Every length-bearing value is a ratio."""

    version: int = TRAITS_VERSION
    flow: str = ""
    form: str = ""
    input_kind: str = ""
    n_triangles: int = 0
    n_solids: int = 0
    closed: bool | None = None
    genus: int | None = None
    ports: int | None = None
    sharp_edges: float | None = None
    knife_edges: float | None = None
    passage_rel: float | None = None
    passage: float | None = None
    narrow_vs_volume: float | None = None
    neck: float | None = None
    scale_ratio: float | None = None
    slenderness: float | None = None
    gap_vs_port: float | None = None
    wet_share: float | None = None
    thin_wall_fraction: float | None = None
    thickness_ratio: float | None = None
    gap_share: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def measured(self) -> bool:
        return self.n_triangles > 0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["notes"] = list(self.notes)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}

    @classmethod
    def from_dict(cls, data: Mapping | None) -> ShapeTraits:
        if not isinstance(data, Mapping):
            return cls()
        known = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in data.items() if k in known}
        kw["notes"] = tuple(kw.get("notes") or ())
        try:
            return cls(**kw)
        except TypeError:
            return cls()


# #
# the welded surface
# #

@dataclass(frozen=True)
class _Welded:
    points: np.ndarray      # (V, 3)
    faces: np.ndarray       # (F, 3) int
    diag: float


def _weld(tris: np.ndarray) -> _Welded | None:
    """Merge coincident corners so the triangles become one connected mesh (an STL repeats every
    corner once per triangle; a CAD tessellation once per face)."""
    t = np.asarray(tris, dtype=float).reshape(-1, 3)
    if len(t) < 3 or not np.isfinite(t).all():
        return None
    lo, hi = t.min(axis=0), t.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    if diag <= 0.0:
        return None
    tol = diag * 1e-7
    q = np.round((t - lo) / tol).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    faces = np.asarray(inv, dtype=np.int64).reshape(-1, 3)
    ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    pts = t[first]
    faces = faces[ok]
    if len(faces) == 0:
        return None
    return _Welded(points=pts, faces=faces, diag=diag)


def _oriented(w: _Welded, closed: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Faces wound consistently (outward when the surface is closed), their unit normals and
    areas. vtk's consistency pass rewinds cells in place and keeps their order."""
    import pyvista as pv
    cells = np.hstack([np.full((len(w.faces), 1), 3, dtype=np.int64), w.faces]).ravel()
    mesh = pv.PolyData(w.points, cells)
    try:
        m = mesh.compute_normals(cell_normals=True, point_normals=False, consistent_normals=True,
                                 auto_orient_normals=closed, split_vertices=False,
                                 non_manifold_traversal=False)
        faces = np.asarray(m.faces, dtype=np.int64).reshape(-1, 4)[:, 1:]
        if len(faces) != len(w.faces):
            raise ValueError("the normal filter changed the cell count")
    except Exception as exc:  # noqa: BLE001 - the source winding is the fallback
        logger.debug("shape traits: normal consistency pass skipped (%s)", exc)
        faces = w.faces
    p = w.points
    cr = np.cross(p[faces[:, 1]] - p[faces[:, 0]], p[faces[:, 2]] - p[faces[:, 0]])
    area2 = np.linalg.norm(cr, axis=1)
    normals = cr / np.maximum(area2, 1e-300)[:, None]
    if closed:
        # a closed surface wound inward everywhere reads a negative volume: turn it outward
        vol = float(np.einsum("ij,ij->i", p[faces[:, 0]], cr).sum()) / 6.0
        if vol < 0:
            faces = faces[:, ::-1].copy()
            normals = -normals
    return faces, normals, 0.5 * area2


def _edges(faces: np.ndarray, n_points: int):
    """Each undirected edge once: its key, how many faces share it, and for the two-face edges
    the pair of faces."""
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    owner = np.tile(np.arange(len(faces)), 3)
    e.sort(axis=1)
    key = e[:, 0] * np.int64(n_points) + e[:, 1]
    order = np.argsort(key, kind="stable")
    key, owner, e = key[order], owner[order], e[order]
    uniq, start, counts = np.unique(key, return_index=True, return_counts=True)
    two = counts == 2
    pairs = np.stack([owner[start[two]], owner[start[two] + 1]], axis=1)
    return e[start], counts, pairs, e[start[two]]


def _components(faces: np.ndarray, n_points: int) -> tuple[int, np.ndarray]:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    g = coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(n_points, n_points))
    n, labels = connected_components(g, directed=False)
    return int(n), labels


# #
# the chords
# #

def _chords(points: np.ndarray, faces: np.ndarray, normals: np.ndarray, areas: np.ndarray,
            diag: float, *, seed: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """From RAY_SAMPLES area-weighted surface points: the chord into the solid (along -n) and the
    chord out of it (along +n) to the first wall facing back, NaN where no such wall is met. Also
    the sample's face area weight (uniform: the samples are already area-weighted) and its point."""
    import pyvista as pv
    import vtk
    rng = np.random.default_rng(seed)
    total = float(areas.sum())
    if total <= 0:
        empty = np.full(0, np.nan)
        return empty, empty, empty, np.zeros((0, 3))
    k = min(RAY_SAMPLES, max(64, len(faces)))
    pick = rng.choice(len(faces), size=k, replace=True, p=areas / total)
    r1, r2 = rng.random(k), rng.random(k)
    flip = r1 + r2 > 1.0
    r1[flip], r2[flip] = 1.0 - r1[flip], 1.0 - r2[flip]
    a, b, c = (points[faces[pick, i]] for i in range(3))
    pts = a + r1[:, None] * (b - a) + r2[:, None] * (c - a)
    n = normals[pick]
    cells = np.hstack([np.full((len(faces), 1), 3, dtype=np.int64), faces]).ravel()
    mesh = pv.PolyData(points, cells)
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(mesh)
    obb.BuildLocator()
    hits, ids = vtk.vtkPoints(), vtk.vtkIdList()
    reach = 1.05 * diag
    eps = 1e-6 * diag
    t_in = np.full(k, np.nan)
    t_out = np.full(k, np.nan)
    for i in range(k):
        for sign, out in ((-1.0, t_in), (1.0, t_out)):
            d = sign * n[i]
            hits.Reset()
            ids.Reset()
            if not obb.IntersectWithLine(pts[i] + d * eps, pts[i] + d * reach, hits, ids):
                continue
            best = np.inf
            best_cell = -1
            for j in range(hits.GetNumberOfPoints()):
                cid = ids.GetId(j)
                if cid == pick[i]:
                    continue
                h = np.asarray(hits.GetPoint(j))
                dist = float(np.dot(h - pts[i], d))
                if eps < dist < best:
                    best, best_cell = dist, cid
            if best_cell < 0:
                continue
            facing = float(np.dot(normals[best_cell], d))
            # into the solid the far wall's outward normal runs WITH the ray; out of it the next
            # wall faces back AGAINST it
            if (sign < 0 and facing >= CHORD_MIN_FACING) or (sign > 0 and facing <= -CHORD_MIN_FACING):
                out[i] = best
    return t_in, t_out, np.ones(k), pts


def _axis_index(flow_axis: str) -> int:
    s = str(flow_axis or "").strip().lower().lstrip("+-")
    return {"x": 0, "y": 1, "z": 2}.get(s[:1], 0)


def port_hydraulic_diameters(patches: Sequence[Mapping] | None, *, scale: float = 1.0) -> list[float]:
    """Hydraulic diameters (4 x area / perimeter) of the declared openings, in `scale` x the
    declared unit: a round bore its diameter, an annulus its outer minus inner diameter, a
    rectangle 2wh/(w+h). Reads both the intake's patches (`diameter_mm`, `inner_diameter_mm`,
    `width_mm`, `height_mm`) and port records (`diameter`, `inner_diameter`, `width`, `height`).
    Walls and far fields have none."""
    def num(p: Mapping, *keys: str) -> float | None:
        for key in keys:
            v = p.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        return None

    out: list[float] = []
    for p in patches or ():
        if not isinstance(p, Mapping):
            continue
        kind = str(p.get("type") or p.get("role") or "").lower()
        if kind in ("wall", "farfield", "symmetry", "empty", "patch"):
            continue
        d = num(p, "diameter_mm", "diameter")
        di = num(p, "inner_diameter_mm", "inner_diameter")
        w, h = num(p, "width_mm", "width"), num(p, "height_mm", "height")
        dh: float | None
        if d and di and di < d:
            dh = d - di
        elif w and h:
            dh = 2.0 * w * h / (w + h)
        elif d:
            dh = d
        else:
            a = num(p, "area_mm2", "area")
            dh = math.sqrt(4.0 * a / math.pi) if a else None
        if dh:
            out.append(dh * scale)
    return out


def measure(tris, *, flow: str = "", input_kind: str = "", form: str = "",
            port_diameters: Sequence[float] = (), n_ports: int | None = None,
            flow_axis: str = "", scale_to_m: float | None = None, seed: int = 7) -> ShapeTraits:
    """The traits of a triangle soup `tris` ((n, 3, 3), any unit). `port_diameters` are the
    declared openings' hydraulic diameters IN THE SAME UNIT as the triangles; `n_ports` the
    declared opening count (defaults to len(port_diameters)); `scale_to_m` turns the triangles'
    unit into metres (only `passage` needs it - every other trait is a ratio)."""
    notes: list[str] = []
    base: dict[str, Any] = {"flow": flow, "form": form, "input_kind": input_kind,
            "ports": (n_ports if n_ports is not None else (len(port_diameters) or None))}
    w = _weld(np.asarray(tris, dtype=float))
    if w is None:
        return ShapeTraits(**base, notes=("no triangles could be read",))
    f, v = len(w.faces), len(w.points)
    keys, counts, pairs, pair_edges = _edges(w.faces, v)
    n_boundary = int((counts == 1).sum())
    n_nonmanifold = int((counts > 2).sum())
    closed = n_boundary == 0 and n_nonmanifold == 0
    n_comp, labels = _components(w.faces, v)
    faces, normals, areas = _oriented(w, closed)
    area = float(areas.sum())
    # bodies: components that carry real area (a stray sliver is not a body)
    comp_area = np.bincount(labels[faces[:, 0]], weights=areas, minlength=n_comp)
    n_solids = int((comp_area > 1e-3 * max(area, 1e-300)).sum())
    genus = None
    if closed:
        euler = v - len(keys) + f
        genus = max(0, (2 * n_comp - euler) // 2)
    # edges: the turn between the two faces of every two-face edge
    cosang = np.einsum("ij,ij->i", normals[pairs[:, 0]], normals[pairs[:, 1]]).clip(-1.0, 1.0)
    turn = np.degrees(np.arccos(cosang))
    elen = np.linalg.norm(w.points[pair_edges[:, 0]] - w.points[pair_edges[:, 1]], axis=1)
    root_a = math.sqrt(area) if area > 0 else 1.0
    sharp_edges = float(elen[turn > FEATURE_DEG].sum() / root_a)
    knife_edges = float(elen[turn > KNIFE_DEG].sum() / root_a)
    traits: dict[str, Any] = dict(base, n_triangles=f, n_solids=n_solids, closed=closed,
                                  genus=genus, sharp_edges=sharp_edges, knife_edges=knife_edges)
    p0 = w.points[faces[:, 0]]
    volume = abs(float(np.einsum("ij,ij->i", p0, normals * (2.0 * areas)[:, None]).sum()) / 6.0)
    if not closed:
        notes.append(f"open surface: {n_boundary} open and {n_nonmanifold} non-manifold edges")
    if f > MAX_RAY_TRIANGLES:
        notes.append(f"{f} triangles: the chords were not cast")
        return ShapeTraits(**traits, notes=tuple(notes))
    try:
        t_in, t_out, _, _ = _chords(w.points, faces, normals, areas, w.diag, seed=seed)
    except Exception as exc:  # noqa: BLE001 - a chord we cannot cast is a fact we do not have
        notes.append(f"chords not cast ({type(exc).__name__})")
        return ShapeTraits(**traits, notes=tuple(notes))
    k = len(t_in)
    fluid_inside = (flow == "internal" and input_kind in ("", "fluid-domain"))
    if flow == "internal" and not fluid_inside and not closed:
        # an open sheet (a lumen exported as its wall alone) has no inside of its own and its
        # winding says nothing: the fluid is the side whose chords land on a wall facing back
        fluid_inside = int(np.isfinite(t_in).sum()) > int(np.isfinite(t_out).sum())
        notes.append("open wall: the fluid side read from the chords")
    gap = t_in if fluid_inside else t_out
    solid = None if fluid_inside else t_in
    ok = np.isfinite(gap)
    if flow == "internal" and ok.sum() >= max(10, 0.02 * k):
        g = gap[ok]
        p50, p05 = float(np.median(g)), float(np.percentile(g, 5))
        wet = float(ok.mean())
        # the fluid's volume: the solid's own when the solid IS the fluid and closed; else the
        # wetted area times a quarter of the chord (exact for a round tube: pi D L x D/4)
        fluid_volume = volume if (fluid_inside and closed and volume > 0) else area * wet * p50 / 4.0
        traits.update(passage_rel=p50 / w.diag, neck=(p05 / p50) if p50 > 0 else None,
                      scale_ratio=(w.diag / p05) if p05 > 0 else None, wet_share=wet,
                      slenderness=(area * wet) / (math.pi * p50 * p50) if p50 > 0 else None,
                      narrow_vs_volume=(p05 / fluid_volume ** (1.0 / 3.0)) if fluid_volume > 0 else None,
                      passage=(p50 * scale_to_m) if scale_to_m else None)
        dh = [d for d in port_diameters if d and d > 0]
        if dh and p50 > 0:
            ratio = p50 / float(max(dh))
            if _PORT_RATIO_BAND[0] <= ratio <= _PORT_RATIO_BAND[1]:
                traits["gap_vs_port"] = ratio
            else:
                notes.append("the declared openings are not in the file's unit: no port ratio")
    elif flow == "internal":
        notes.append("no chord crossed the fluid: no passage measured")
    if solid is not None:
        # the WETTED wall only: inside a pipe the bore's own wall; around a body all of it
        wetted = ok if flow == "internal" else np.ones(k, dtype=bool)
        s_ok = np.isfinite(solid) & wetted
        if wetted.any():
            traits["thin_wall_fraction"] = float(
                (s_ok & (np.nan_to_num(solid, nan=np.inf) < THIN_WALL_REL * w.diag)).sum() / wetted.sum())
    if flow == "external":
        ext = w.points.max(axis=0) - w.points.min(axis=0)
        ax = _axis_index(flow_axis)
        stream = float(ext[ax])
        cross = [float(ext[i]) for i in range(3) if i != ax]
        if stream > 0:
            traits["thickness_ratio"] = min(cross) / stream
            traits["gap_share"] = float((np.isfinite(t_out) & (t_out < NARROW_GAP_REL * stream)).sum() / k)
            fin = t_in[np.isfinite(t_in)]
            if len(fin):
                traits["scale_ratio"] = w.diag / max(float(np.percentile(fin, 5)), 1e-300)
    return ShapeTraits(**traits, notes=tuple(notes))


# #
# from a file
# #

_CAD_SUFFIXES = (".step", ".stp", ".igs", ".iges", ".brep", ".brp")
_CACHE: dict[tuple, ShapeTraits] = {}


def read_triangles(path: Path, *, workdir: Path | None = None) -> tuple[np.ndarray, str]:
    """(triangles in the file's own unit - millimetres for a CAD solid, as OpenCASCADE reads it -
    , the form 'cad' | 'surface'). Any format the product reads goes through its canonical form."""
    import tempfile

    from meshpipeline.cad.ingest.canonical import canonicalise
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="traits-", dir=workdir) as tmp:
        canon = canonicalise(path, Path(tmp))
        cpath = Path(canon.path)
        if cpath.suffix.lower() in _CAD_SUFFIXES:
            return _cad_triangles(cpath, Path(tmp)), "cad"
        from meshpipeline.cad.scout_mesh import read_triangles as _read
        return np.asarray(_read(cpath), dtype=float).reshape(-1, 3, 3), "surface"


def _cad_triangles(path: Path, tmp: Path) -> np.ndarray:
    """A CAD solid tessellated at 1/1000 of its size (what a meshing surface would roughly be),
    written through OpenCASCADE's own STL writer (face orientation honoured) and read back."""
    from OCP.StlAPI import StlAPI_Writer

    from meshpipeline.cad.occ_box import mesh_to_size
    from meshpipeline.cad.scout import _read_shape
    shape = _read_shape(path)
    mesh_to_size(shape, 1.0 / 1000.0, 0.35)
    out = tmp / "traits.stl"
    w = StlAPI_Writer()
    w.ASCIIMode = False
    if not w.Write(shape, str(out)):
        raise ValueError("the CAD solid could not be tessellated")
    return _read_binary_stl(out)


def _read_binary_stl(path: Path) -> np.ndarray:
    data = Path(path).read_bytes()
    n = int(np.frombuffer(data[80:84], dtype="<u4")[0]) if len(data) >= 84 else 0
    if n <= 0 or len(data) < 84 + 50 * n:
        from meshpipeline.cad.stl_io import read_stl_triangles
        return np.asarray(read_stl_triangles(path), dtype=float).reshape(-1, 3, 3)
    rec = np.frombuffer(data[84:84 + 50 * n], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)),
                                                               ("a", "<u2")]))
    return rec["v"].astype(float)


def measure_file(path, *, flow: str = "", input_kind: str = "", patches: Sequence[Mapping] = (),
                 surface_unit_to_m: float | None = None, flow_axis: str = "",
                 workdir: Path | None = None) -> ShapeTraits:
    """The traits of a geometry file. `patches` are the declared openings in millimetres (the
    intake's patches: `diameter_mm`, `width_mm`...). A CAD solid is read in millimetres
    (OpenCASCADE's unit), so they compare directly; a triangle file carries no unit, and
    `surface_unit_to_m` says what its numbers are (None: millimetres assumed for the port ratio,
    which is checked for plausibility, and no `passage` in metres). Never raises: an unreadable
    file gives empty traits that say why."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return ShapeTraits(flow=flow, input_kind=input_kind, notes=("the file is not there",))
    patch_key = tuple(sorted(str(sorted((k, str(v)) for k, v in dict(x).items()))
                             for x in patches if isinstance(x, Mapping)))
    key = (str(p), st.st_size, st.st_mtime_ns, flow, input_kind, patch_key, surface_unit_to_m,
           flow_axis, TRAITS_VERSION)
    if key in _CACHE:
        return _CACHE[key]
    try:
        tris, form = read_triangles(p, workdir=workdir)
        if form == "cad":
            unit_to_m: float | None = 0.001
        else:
            unit_to_m = float(surface_unit_to_m) if surface_unit_to_m else None
        mm_to_file = 0.001 / unit_to_m if unit_to_m else 1.0
        dh = port_hydraulic_diameters(patches, scale=mm_to_file)
        n_ports = sum(1 for x in patches if isinstance(x, Mapping)
                      and str(x.get("type") or x.get("role") or "").lower() in ("inlet", "outlet"))
        traits = measure(tris, flow=flow, input_kind=input_kind, form=form, port_diameters=dh,
                         n_ports=n_ports or None, flow_axis=flow_axis, scale_to_m=unit_to_m)
    except Exception as exc:  # noqa: BLE001 - a measurement is advice, never a failure
        logger.warning("shape traits: %s could not be measured (%s: %s)", p.name,
                       type(exc).__name__, str(exc)[:200])
        traits = ShapeTraits(flow=flow, input_kind=input_kind,
                             notes=(f"the file could not be measured ({type(exc).__name__})",))
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = traits
    return traits


__all__ = ["FEATURE_DEG", "KNIFE_DEG", "RAY_SAMPLES", "TRAITS_VERSION", "ShapeTraits", "measure",
           "measure_file", "port_hydraulic_diameters", "read_triangles"]
