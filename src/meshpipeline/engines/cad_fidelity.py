# Responsibility: Measure whether a valid mesh is also FAITHFUL to the CAD it was built from: its boundary on the CAD surface, the CAD surface covered by its boundary, its fluid volume against the CAD's, and each opening at the size it was confirmed at.
# Owns: the fidelity measures, their documented thresholds, the verdict (pass / concern / rebuild) and the shared gate every flow engine runs last.
# Boundaries: reads the workspace the engine left (the staged metre surfaces, the delivered boundary, the manifest) and writes its record into the manifest; it meshes nothing, and it never judges a mesh it could not measure.
"""A MESH CAN PASS checkMesh AND STILL NOT BE THE PART.

checkMesh proves the cells are sound. It cannot see a boundary that floated off the CAD, a fin the
mesher dropped, a mesh of the wrong side of the wall, or an inlet that swallowed the wall around it.
These four measures can, and every one is relative - to the local cell size, to the part, to the
opening - so one rule holds for a 2 mm channel and a 40 m hull:

1. SURFACE DISTANCE, both ways (a sampled, Hausdorff-style one-sided distance each way):
   * mesh -> CAD: how far the mesh's wall faces sit from the CAD surface, in local cells
     (the face's own size). `wall_off_share` = the share of the wall AREA more than one cell off.
   * CAD -> mesh: how much of the CAD surface the mesh's boundary does not reach. `cad_missed_share`
     = the share of the CAD AREA farther than MISS_CELLS local cells from the mesh boundary (the
     local cell = the nearest boundary face's size). For an external body it is counted only
     inside the meshed domain's box (a half model or a body cut by the ground is not "missing" the
     half it was cut from); an internal mesh is held to its whole fluid boundary.
2. FLUID VOLUME: the mesh's fluid volume (divergence theorem over its boundary) against the volume
   the staged CAD encloses - for an internal flow the fluid itself, for an external body the body
   it displaces. Only where both sides are closed; otherwise "not computable", never a guess.
3. OPENINGS: each inlet/outlet patch's area against the opening confirmed for it (measured on the
   geometry by the staging, else declared) - a tighter band than the port-area gate's.
4. SIZE (the plausible physical size for the purpose) is read at terminal assembly by the size
   note (application/final_result.physical_scale_caveat) and joins the same record there.

THE VERDICT. Each measure is pass, CONCERN (said beside the delivered mesh, with its numbers) or
REBUILD (the mesh is not the part: refused here so the run rebuilds, the same way the port-area gate
refuses a port that is not its opening). Rebuild is kept for misses no sound mesh of the right part
shows; CAD_FIDELITY_RETRY_ENABLED=false turns it into a concern.

THRESHOLDS (each a share or a ratio, never a length):
  wall_off_share      concern > 2%      rebuild > 15% (and p95 beyond REBUILD_P95_CELLS cells)
  cad_missed_share    concern > 1%      rebuild > 8%
  volume error        concern > 3% internal / 5% external body     rebuild > 25%
  opening area ratio  concern outside 0.9-1.1 (the port-area gate refuses outside 0.6-1.6)
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: the record's key in the manifest's quality block, and its version
RECORD_KEY = "cad_fidelity"
VERSION = 1

#: samples per side of the distance measures (fixed seed: the same mesh gives the same numbers)
MAX_SAMPLES = 120_000
SEED = 20261018
#: a CAD point farther than this many local cells from the mesh boundary is "missed"
MISS_CELLS = 2.0
#: ...and farther than this share of the part's diagonal (a sub-micron miss on a coarse cell is noise)
MISS_FLOOR_OF_DIAG = 1e-4

WALL_OFF_CONCERN = 0.02
WALL_OFF_REBUILD = 0.15
REBUILD_P95_CELLS = 2.0
CAD_MISSED_CONCERN = 0.01
CAD_MISSED_REBUILD = 0.08
VOLUME_CONCERN_INTERNAL = 0.03
VOLUME_CONCERN_EXTERNAL = 0.05
VOLUME_REBUILD = 0.25
OPENING_BAND = (0.9, 1.1)
#: past this many triangles a surface of unknown orientation is not re-oriented to read its volume
#: (the volume is then "not computable" rather than a minute of work on the worker)
MAX_REORIENT_TRIANGLES = 1_500_000
#: a closed surface's volume does not move with the origin it is summed about: two origins that
#: disagree by more than this share mean the surface is not closed, and the volume is not computable
CLOSURE_TOLERANCE = 1e-3

PASS, CONCERN, REBUILD, UNMEASURED = "pass", "concern", "rebuild", "unmeasured"
_RANK = {UNMEASURED: 0, PASS: 1, CONCERN: 2, REBUILD: 3}

#: patch names that are the ground the body stands on, not the body
_GROUND_NAMES = frozenset({"ground", "floor", "road"})


# #
# surfaces
# #

@dataclass
class Surface:
    """A triangulated surface: points (n,3), triangles (m,3), and per triangle the area and the
    size of the cell it came from (sqrt of the ORIGINAL face's area - a quad split in two keeps the
    quad's size)."""
    points: np.ndarray
    tris: np.ndarray
    cell_size: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def n(self) -> int:
        return int(len(self.tris))

    def corners(self) -> np.ndarray:
        return self.points[self.tris]

    def areas(self) -> np.ndarray:
        c = self.corners()
        return 0.5 * np.linalg.norm(np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]), axis=1)

    def centroids(self) -> np.ndarray:
        return self.corners().mean(axis=1)


def _empty() -> Surface:
    return Surface(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.zeros(0))


def join(parts: list[Surface]) -> Surface:
    parts = [p for p in parts if p.n]
    if not parts:
        return _empty()
    pts, tris, size, off = [], [], [], 0
    for p in parts:
        pts.append(p.points)
        tris.append(p.tris + off)
        size.append(p.cell_size if len(p.cell_size) == p.n else np.sqrt(p.areas()))
        off += len(p.points)
    return Surface(np.vstack(pts), np.vstack(tris), np.concatenate(size))


def _from_polys(points, polys) -> Surface:
    """Fan-triangulated polygons, each triangle carrying its polygon's size."""
    pts = np.asarray(points, dtype=float)
    if len(polys) and all(len(p) == 3 for p in polys):
        s = Surface(pts, np.asarray([list(p) for p in polys], dtype=np.int64))
        s.cell_size = np.sqrt(s.areas())
        return s
    tris, owner = [], []
    for k, poly in enumerate(polys):
        poly = [int(i) for i in poly]
        for j in range(1, len(poly) - 1):
            tris.append((poly[0], poly[j], poly[j + 1]))
            owner.append(k)
    if not tris:
        return _empty()
    s = Surface(pts, np.asarray(tris, dtype=np.int64))
    poly_area = np.bincount(np.asarray(owner), weights=s.areas(), minlength=len(polys))
    s.cell_size = np.sqrt(poly_area[np.asarray(owner)])
    return s


def _from_pyvista(mesh) -> Surface:
    """A pyvista PolyData of polygons (one OpenFOAM boundary patch, say), triangulated, each
    triangle carrying the size of the face it came from (VTK's triangle filter copies cell data)."""
    poly = mesh.extract_surface(algorithm="dataset_surface")
    if poly.n_cells == 0:
        return _empty()
    poly.cell_data["_face_area"] = np.asarray(
        poly.compute_cell_sizes(length=False, area=True, volume=False).cell_data["Area"])
    tri = poly.triangulate()
    faces = np.asarray(tri.faces, dtype=np.int64).reshape(-1, 4)[:, 1:]
    return Surface(np.asarray(tri.points, dtype=float), faces,
                   np.sqrt(np.abs(np.asarray(tri.cell_data["_face_area"], dtype=float))))


def read_stl(path: Path) -> Surface:
    """A triangulated surface file (STL: binary or ASCII, one solid or many), coincident points
    merged so the triangles share their corners."""
    import pyvista as pv
    try:
        mesh = pv.read(str(path))
    except Exception:  # noqa: BLE001 - a surface it cannot read is no surface
        logger.warning("fidelity: %s unreadable", Path(path).name, exc_info=True)
        return _empty()
    s = _from_pyvista(mesh)
    s.cell_size = np.sqrt(s.areas())
    return s


def _foundation_patch_files(ws: Path) -> list[Path]:
    """The boundary as the OpenFOAM Foundation's foamToVTK lays it out (VTK/<patch>/<patch>_<time>
    .vtk, the latest time of each) - the layout of the lab's OpenFOAM 11; the mesh image's v2412
    writes VTK/<case>_<time>/boundary/<patch>.vtp, read first."""
    out = []
    root = ws / "VTK"
    if not root.is_dir():
        return []
    for d in sorted(q for q in root.iterdir() if q.is_dir()):
        def _t(f: Path, name=d.name) -> int:
            tail = f.stem[len(name) + 1:]
            return int(tail) if tail.isdigit() else -1
        times = sorted((f for f in d.glob(f"{d.name}_*.vtk") if _t(f) >= 0), key=_t)
        if times:
            out.append(times[-1])
    return out


def mesh_patches(workspace) -> dict[str, Surface]:
    """{patch: Surface} of the delivered mesh's boundary, from whatever the engine left: the
    OpenFOAM boundary as foamToVTK wrote it, else Gmsh's mesh.msh, else VMTK's mesh.vtu. {} when
    none can be read. Named by the files, never by the engine."""
    ws = Path(workspace)
    from meshpipeline.engines.surface_deliverable import _patch_files
    files = _patch_files(ws) or _foundation_patch_files(ws)
    if files:
        import pyvista as pv
        out: dict[str, Surface] = {}
        for f in files:
            name = f.parent.name if f.suffix == ".vtk" else f.stem
            try:
                m = pv.read(str(f))
                if m.n_cells:
                    out[name] = join([out.get(name, _empty()), _from_pyvista(m)])
            except Exception:  # noqa: BLE001 - a patch it cannot read is left out, and said so
                logger.warning("fidelity: patch file %s unreadable", f.name, exc_info=True)
        return out
    mesh = None
    if (ws / "mesh.msh").exists():
        from meshpipeline.engines.gmsh.viewer_surface import read_msh
        mesh = read_msh(ws)
    elif (ws / "mesh.vtu").exists():
        from meshpipeline.engines.vmtk.viewer_surface import read_vtu
        mesh = read_vtu(ws, named=True)
    if not mesh:
        return {}
    out = {}
    for name, polys in mesh.get("patches") or []:
        out[str(name)] = join([out.get(str(name), _empty()), _from_polys(mesh["points"], polys)])
    return out


#: a staged surface larger than this is not re-staged worker-side for the check
MAX_RESTAGE_TRIANGLES = 1_500_000


def _passages(ws: Path) -> list[Path]:
    try:
        from meshpipeline.cad.fluid_passages import passage_surface_files
        return passage_surface_files(ws)
    except ValueError:
        return []


def _restaged(ws: Path, intake_patches) -> Surface | None:
    """The fluid's own closed boundary, staged from the workspace's metre surface by the shared
    internal-flow staging (cad/internal_surface: the fluid's side of the wall, the confirmed
    openings as lids) - for a case whose engine staged only the part's surface and the lids
    (the CAD path), which bound the metal as well as the fluid. None when it cannot be staged."""
    import shutil
    import tempfile

    from meshpipeline.cad.internal_surface import stage_internal_surface
    from meshpipeline.engines.workspace_facts import read_input_kind
    src = ws / "input.stl"
    if not src.exists() or not intake_patches:
        return None
    out = Path(tempfile.mkdtemp(prefix="cad_fidelity_"))
    try:
        if read_stl(src).n > MAX_RESTAGE_TRIANGLES:
            return None
        rec = stage_internal_surface(src, out, intake_patches=list(intake_patches),
                                     input_kind=read_input_kind(ws))
        fb = Path(str(rec.get("fluid_boundary") or ""))
        return read_stl(fb) if fb.exists() else None
    except Exception:  # noqa: BLE001 - the shared staging could not close it: say so, not guess
        logger.info("fidelity: the fluid boundary could not be staged for the check", exc_info=True)
        return None
    finally:
        shutil.rmtree(out, ignore_errors=True)


def cad_reference(workspace, flow: str, intake_patches=None) -> tuple[Surface, str, bool]:
    """The CAD surface the mesh should follow, in metres, where it came from, and whether it is
    the FLUID'S OWN closed boundary (only then can the mesh be held to all of it and to its
    volume). Internal flow, in this order: the closed fluid boundary the staging wrote
    (fluid_boundary.stl; cfMesh's geom.stl), every further passage included; else the same staged
    afresh from the metre surface; else the staged parts (the wall and the lids), which hold the
    mesh's wall to the CAD but also bound the metal. External flow: the body.
    (empty, "", False) when none is on disk."""
    ws = Path(workspace)
    if flow == "internal":
        further = _passages(ws)
        if further and (ws / "geom.stl").exists():
            # a case meshed as several passages (cad/fluid_passages): the first passage's own
            # surface and each further one's, once each - the whole staged boundary already holds
            # every passage, so it is never added to them
            return (join([read_stl(q) for q in [ws / "geom.stl", *further]]),
                    "staged fluid boundary, every passage", True)
        whole = [q for q in (ws / "_internal_stls" / "fluid_boundary.stl",
                             ws / "fluid_boundary.stl", ws / "geom.stl") if q.exists()]
        if whole:
            return read_stl(whole[0]), "staged fluid boundary", True
        fb = _restaged(ws, intake_patches)
        if fb is not None:
            return fb, "fluid boundary staged from your file", True
        parts = sorted((ws / "_internal_stls").glob("*.stl"))
        if parts:
            return join([read_stl(q) for q in parts]), "staged part surface and lids", False
    if (ws / "input.stl").exists():
        return read_stl(ws / "input.stl"), "the uploaded geometry (metres)", True
    return _empty(), "", False


# #
# measures
# #

def _polydata(s: Surface):
    import pyvista as pv
    return pv.PolyData(s.points, np.hstack([np.full((s.n, 1), 3, dtype=np.int64), s.tris]).ravel())


def _distance(points: np.ndarray, to: Surface) -> np.ndarray:
    """|distance| from each point to the surface `to` (VTK's implicit polydata distance)."""
    import pyvista as pv
    cloud = pv.PolyData(np.asarray(points, dtype=float))
    d = cloud.compute_implicit_distance(_polydata(to)).point_data["implicit_distance"]
    return np.abs(np.asarray(d, dtype=float))


def _sample(n: int, rng) -> np.ndarray:
    return np.arange(n) if n <= MAX_SAMPLES else np.sort(rng.choice(n, MAX_SAMPLES, replace=False))


#: the least number of points the CAD surface is sampled at (a CAD of a few large facets is
#: sampled across each facet, not at its centroid alone)
MIN_CAD_SAMPLES = 20_000


def _area_samples(s: Surface, rng) -> tuple[np.ndarray, np.ndarray]:
    """Points spread over the surface in proportion to area, each standing for an equal share of
    it: (points (n,3), area each point stands for (n,))."""
    a = s.areas()
    total = float(a.sum())
    if total <= 0:
        return s.centroids(), a
    n = int(min(MAX_SAMPLES, max(MIN_CAD_SAMPLES, s.n)))
    pick = rng.choice(s.n, size=n, p=a / total)
    r1, r2 = rng.random(n), rng.random(n)
    flip = r1 + r2 > 1.0
    r1[flip], r2[flip] = 1.0 - r1[flip], 1.0 - r2[flip]
    c = s.corners()[pick]
    pts = c[:, 0] + r1[:, None] * (c[:, 1] - c[:, 0]) + r2[:, None] * (c[:, 2] - c[:, 0])
    return pts, np.full(n, total / n)


def _q(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def surface_distance(mesh_wall: Surface, mesh_all: Surface, cad: Surface, *, box=None) -> dict:
    """Both one-sided distances, sampled and area-weighted (see the module notes)."""
    rng = np.random.default_rng(SEED)
    out: dict = {}
    diag = float(np.linalg.norm(cad.points.max(axis=0) - cad.points.min(axis=0))) if cad.n else 0.0
    if mesh_wall.n and cad.n:
        i = _sample(mesh_wall.n, rng)
        d = _distance(mesh_wall.centroids()[i], cad)
        h = np.maximum(mesh_wall.cell_size[i], 1e-300)
        w = mesh_wall.areas()[i]
        r = d / h
        out["mesh_to_cad"] = {
            "samples": int(len(i)),
            "p50_cells": _q(np.percentile(r, 50)), "p95_cells": _q(np.percentile(r, 95)),
            "max_cells": _q(r.max(), 3),
            "max_m": float(f"{d.max():.4g}"),
            "max_of_diag": _q(d.max() / diag, 6) if diag else None,
            "wall_off_share": _q(float(w[r > 1.0].sum() / max(w.sum(), 1e-300)), 5)}
    if mesh_all.n and cad.n:
        c, a = _area_samples(cad, rng)
        if box is not None:
            lo, hi = np.asarray(box[0], dtype=float), np.asarray(box[1], dtype=float)
            pad = 0.01 * float(np.linalg.norm(hi - lo))
            inside = np.all((c >= lo - pad) & (c <= hi + pad), axis=1)
            c, a = c[inside], a[inside]
        if len(c):
            i = _sample(len(c), rng)
            d = _distance(c[i], mesh_all)
            from scipy.spatial import cKDTree
            _, near = cKDTree(mesh_all.centroids()).query(c[i])
            h = mesh_all.cell_size[near]
            missed = (d > MISS_CELLS * h) & (d > MISS_FLOOR_OF_DIAG * diag)
            w = a[i]
            out["cad_to_mesh"] = {
                "samples": int(len(i)),
                "p95_cells": _q(np.percentile(d / np.maximum(h, 1e-300), 95)),
                "max_m": float(f"{d.max():.4g}"),
                "max_of_diag": _q(d.max() / diag, 6) if diag else None,
                "cad_missed_share": _q(float(w[missed].sum() / max(w.sum(), 1e-300)), 5)}
    return out


def _signed_volume(s: Surface, origin) -> float:
    c = s.corners() - np.asarray(origin, dtype=float)
    return float(np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum() / 6.0)


def is_closed(s: Surface) -> bool:
    """Every edge shared by exactly two triangles once coincident points are merged. The only
    proof of closure a volume is read on: two openings of equal area facing opposite ways (a slab
    between two symmetry planes, with no end caps) cancel in any sum over the faces."""
    if not s.n:
        return False
    try:
        surf = _polydata(s).clean(tolerance=1e-9, absolute=False)
        edges = surf.extract_feature_edges(boundary_edges=True, non_manifold_edges=True,
                                           feature_edges=False, manifold_edges=False)
        return edges.n_cells == 0
    except Exception:  # noqa: BLE001 - what cannot be read is not proven closed
        return False


def outward_volume(s: Surface) -> float | None:
    """The volume a surface whose faces already point OUT of the region encloses (an OpenFOAM
    boundary points out of the fluid), or None when it is not closed (an open edge, or a sum that
    moves with the origin - faces wound both ways)."""
    if not s.n or not is_closed(s):
        return None
    lo, hi = s.points.min(axis=0), s.points.max(axis=0)
    span = float(np.linalg.norm(hi - lo)) or 1.0
    v1 = _signed_volume(s, (lo + hi) / 2.0)
    v2 = _signed_volume(s, (lo + hi) / 2.0 + span)
    if abs(v1 - v2) > CLOSURE_TOLERANCE * max(abs(v1), 1e-300):
        return None
    return v1


def enclosed_volume(s: Surface) -> float | None:
    """The volume a closed surface of unknown orientation encloses. A surface already wound one
    way (an OpenFOAM boundary) is read directly. Otherwise every connected piece is wound
    consistently on its own and then turned to face OUT of the solid, decided by one ray-cast
    inside/outside test just off its largest triangle - so a staged boundary written as separate
    parts (the wall, one lid per opening, their rims not sharing vertices) reads as the one closed
    region it is. None when the sum still moves with the origin (the surface is not closed)."""
    if not s.n:
        return None
    v = outward_volume(s)
    if v is not None and abs(v) > 0:
        return abs(v)
    if s.n > MAX_REORIENT_TRIANGLES:
        return None
    try:
        import pyvista as pv
        whole = _polydata(s).clean()
        regions = whole.connectivity(extraction_mode="all")
        rid = np.asarray(regions.cell_data["RegionId"])
        pieces, probes = [], []
        diag = float(np.linalg.norm(np.asarray(whole.bounds[1::2]) - np.asarray(whole.bounds[0::2])))
        for r in np.unique(rid):
            piece = regions.extract_cells(np.flatnonzero(rid == r)).extract_surface(
                algorithm="dataset_surface").compute_normals(
                cell_normals=True, point_normals=False, consistent_normals=True,
                auto_orient_normals=False, split_vertices=False)
            faces = np.asarray(piece.faces).reshape(-1, 4)[:, 1:].astype(np.int64)
            sh = Surface(np.asarray(piece.points, dtype=float), faces)
            k = int(np.argmax(sh.areas()))
            c = sh.corners()[k]
            n = np.cross(c[1] - c[0], c[2] - c[0])
            n = n / (np.linalg.norm(n) or 1.0)
            step = max(1e-6 * diag, 0.05 * float(np.sqrt(sh.areas()[k])))
            pieces.append(sh)
            probes.append(c.mean(axis=0) + step * n)
        sel = pv.PolyData(np.asarray(probes)).select_interior_points(whole, check_surface=False)
        inside = np.asarray(sel.point_data["selected_points"]).astype(bool)
        oriented = [Surface(sh.points, sh.tris[:, ::-1] if inn else sh.tris)
                    for sh, inn in zip(pieces, inside, strict=True)]
    except Exception:  # noqa: BLE001 - a volume that cannot be read is not computable
        logger.warning("fidelity: enclosed volume unreadable", exc_info=True)
        return None
    v = outward_volume(join(oriented))
    return abs(v) if v is not None and abs(v) > 0 else None


def _on_box_share(s: Surface, lo, hi) -> float:
    """The share of a patch's area lying flat on a face of the box [lo, hi]."""
    if not s.n:
        return 0.0
    span = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo))) or 1.0
    tol = 1e-6 * span
    c = s.corners()
    flat = np.zeros(s.n, dtype=bool)
    for k in range(3):
        for plane in (lo[k], hi[k]):
            flat |= np.all(np.abs(c[:, :, k] - plane) <= tol, axis=1)
    a = s.areas()
    return float(a[flat].sum() / max(a.sum(), 1e-300))


# #
# the record and the verdict
# #

def _worst(*verdicts: str) -> str:
    vs = [v for v in verdicts if v]
    return max(vs, key=lambda v: _RANK[v]) if vs else UNMEASURED


def measure(workspace, *, intake_patches, manifest: Mapping | None = None,
            flow: str = "") -> dict:
    """The fidelity record of the mesh in `workspace` (see the module notes for every key). Never
    raises: what cannot be measured says so."""
    ws = Path(workspace)
    manifest = dict(manifest or {})
    flow = flow or str(manifest.get("flow_topology") or "")
    roles = {str(p.get("name")): str(p.get("type") or "") for p in intake_patches or []
             if isinstance(p, Mapping) and p.get("name")}
    roles.update({str(k): str(v) for k, v in (manifest.get("patch_types") or {}).items()
                  if str(k) not in roles})
    rec: dict = {"version": VERSION, "flow": flow}
    try:
        patches = mesh_patches(ws)
        cad, source, fluid_side = cad_reference(ws, flow, intake_patches)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fidelity: surfaces unreadable", exc_info=True)
        return {**rec, "verdict": UNMEASURED, "why": f"surfaces unreadable ({type(exc).__name__})"}
    if not patches or not cad.n:
        return {**rec, "verdict": UNMEASURED,
                "why": "no mesh boundary on disk" if not patches else "no CAD surface on disk"}
    rec["cad_source"] = source
    allb = join(list(patches.values()))
    lo, hi = allb.points.min(axis=0), allb.points.max(axis=0)
    if flow == "external":
        body_names = [n for n, s in patches.items()
                      if roles.get(n, "wall") == "wall" and n.lower() not in _GROUND_NAMES
                      and _on_box_share(s, lo, hi) < 0.9]
    else:
        # an internal fluid, a fluid domain, a solid: its whole boundary is the staged surface
        body_names = list(patches)
    wall = join([patches[n] for n in body_names])
    rec["patches_compared"] = sorted(body_names)
    verdicts: dict[str, str] = {}

    # 1. surface distance
    try:
        # an external domain is cut from the space round the body: a half model or a body on
        # the ground is meshed only on its side of the cut, so the CAD is counted inside the box;
        # an internal mesh must reach the whole fluid boundary, so all of it is counted
        dist = surface_distance(wall, allb, cad if fluid_side else _empty(),
                                box=(lo, hi) if flow == "external" else None)
        if not fluid_side:
            # the staged surface bounds the metal too: the mesh's wall is held to it, but the
            # mesh is not expected to reach all of it
            dist.update(surface_distance(wall, _empty(), cad))
            dist["cad_to_mesh_why"] = "the staged CAD surface is not the fluid's own boundary"
    except Exception as exc:  # noqa: BLE001
        logger.warning("fidelity: distance not measured", exc_info=True)
        dist = {"why": f"not measured ({type(exc).__name__})"}
    rec["surface"] = dist
    m2c, c2m = dist.get("mesh_to_cad") or {}, dist.get("cad_to_mesh") or {}
    if m2c:
        off = float(m2c["wall_off_share"])
        verdicts["mesh_to_cad"] = (REBUILD if off > WALL_OFF_REBUILD
                                   and float(m2c["p95_cells"]) > REBUILD_P95_CELLS else
                                   CONCERN if off > WALL_OFF_CONCERN else PASS)
    if c2m:
        miss = float(c2m["cad_missed_share"])
        verdicts["cad_to_mesh"] = (REBUILD if miss > CAD_MISSED_REBUILD else
                                   CONCERN if miss > CAD_MISSED_CONCERN else PASS)

    # 2. volume
    vol: dict = {}
    try:
        cad_v = enclosed_volume(cad) if fluid_side else None
        if flow == "external":
            mesh_v = outward_volume(wall)
            mesh_v = -mesh_v if mesh_v is not None else None   # wall faces point into the body
            if mesh_v is None or mesh_v <= 0:
                mesh_v = enclosed_volume(wall)                # an orientation we cannot trust
            what = "body"
        else:
            mesh_v = outward_volume(allb)
            if mesh_v is None or mesh_v <= 0:
                mesh_v = enclosed_volume(allb)
            what = "fluid"
        vol["what"] = what
        if cad_v and mesh_v:
            err = (mesh_v - cad_v) / cad_v
            vol.update(cad_m3=float(f"{cad_v:.6g}"), mesh_m3=float(f"{mesh_v:.6g}"),
                       error=_q(err, 5))
            concern = VOLUME_CONCERN_EXTERNAL if what == "body" else VOLUME_CONCERN_INTERNAL
            verdicts["volume"] = (REBUILD if abs(err) > VOLUME_REBUILD else
                                  CONCERN if abs(err) > concern else PASS)
        else:
            vol["why"] = ("the staged CAD surface is not the fluid's own boundary"
                          if not fluid_side else
                          "the CAD surface is not closed" if not cad_v else
                          f"the mesh's {what} boundary is not closed (it meets the ground, a "
                          "symmetry plane or the box)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("fidelity: volume not measured", exc_info=True)
        vol["why"] = f"not measured ({type(exc).__name__})"
    rec["volume"] = vol

    # 3. openings
    try:
        from meshpipeline.engines.region_check import (
            delivered_areas,
            expected_port_areas,
            measured_openings,
        )
        delivered = delivered_areas(manifest) or {n: float(s.areas().sum())
                                                  for n, s in patches.items()}
        expected = expected_port_areas(intake_patches or [], measured_openings(manifest))
        ports: dict[str, dict] = {}
        for name, (want, basis) in expected.items():
            got = delivered.get(name)
            if not got or not want:
                continue
            ratio = float(got) / float(want)
            ports[name] = {"delivered_m2": float(f"{got:.6g}"), "expected_m2": float(f"{want:.6g}"),
                           "expected_from": basis, "ratio": _q(ratio, 3)}
        rec["openings"] = ports
        if ports:
            verdicts["openings"] = (CONCERN if any(
                not OPENING_BAND[0] <= float(str(p["ratio"])) <= OPENING_BAND[1]
                for p in ports.values()) else PASS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fidelity: openings not measured", exc_info=True)
        rec["openings"] = {"why": f"not measured ({type(exc).__name__})"}

    rec["verdicts"] = verdicts
    rec["verdict"] = _worst(*verdicts.values())
    return rec


def concerns(rec: Mapping) -> list[str]:
    """Each measure that missed its bar, said in plain words with its numbers ([] when none)."""
    raw_v, raw_s = rec.get("verdicts"), rec.get("surface")
    v: Mapping = raw_v if isinstance(raw_v, Mapping) else {}
    s: Mapping = raw_s if isinstance(raw_s, Mapping) else {}
    out: list[str] = []
    if v.get("mesh_to_cad") in (CONCERN, REBUILD):
        m = s.get("mesh_to_cad") or {}
        out.append(f"About {float(m.get('wall_off_share', 0)) * 100:.1f}% of the mesh's wall sits "
                   "more than one cell away from your CAD surface (the worst spot is "
                   f"{float(m.get('max_cells', 0)):.1f} cells away). Sharp edges or small details "
                   "there may be rounded off.")
    if v.get("cad_to_mesh") in (CONCERN, REBUILD):
        m = s.get("cad_to_mesh") or {}
        out.append(f"About {float(m.get('cad_missed_share', 0)) * 100:.1f}% of your CAD surface is "
                   f"not reached by the mesh (more than {MISS_CELLS:g} cells away). Thin parts or "
                   "small features there may be missing from the mesh.")
    if v.get("volume") in (CONCERN, REBUILD):
        vol = rec.get("volume") or {}
        what = "fluid" if vol.get("what") == "fluid" else "body"
        err = float(vol.get("error") or 0.0)
        out.append(f"The mesh's {what} volume is {abs(err) * 100:.1f}% "
                   f"{'larger' if err > 0 else 'smaller'} than your CAD's.")
    if v.get("openings") == CONCERN:
        for name, p in (rec.get("openings") or {}).items():
            r = float(p.get("ratio") or 1.0)
            if not OPENING_BAND[0] <= r <= OPENING_BAND[1]:
                out.append(f"The '{name}' patch is {r:.2f} times the opening it was confirmed as.")
    return out


def record_into_manifest(workspace, rec: Mapping) -> None:
    mp = Path(workspace) / "mesh_manifest.json"
    try:
        m = json.loads(mp.read_text())
        m.setdefault("quality", {})[RECORD_KEY] = dict(rec)
        mp.write_text(json.dumps(m, indent=1, default=str))
    except (OSError, ValueError, AttributeError):
        logger.warning("fidelity: the record could not be written into the manifest", exc_info=True)


def gate_cad_fidelity(ctx) -> tuple[bool, str]:
    """THE SHARED GATE, run last: measure, record the numbers in the manifest, and refuse only a
    mesh that is not the part (a REBUILD verdict) while CAD_FIDELITY_RETRY_ENABLED. A concern passes
    here and is said beside the delivered mesh (application/final_result.cad_fidelity_caveat)."""
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.gates import refuse
    try:
        manifest = ctx.manifest_or_load()
        rec = measure(ctx.workspace, intake_patches=ctx.intake_patches, manifest=manifest)
        record_into_manifest(ctx.workspace, rec)
        if isinstance(ctx.manifest, dict) and ctx.manifest:
            ctx.manifest.setdefault("quality", {})[RECORD_KEY] = rec
        if rec.get("verdict") != REBUILD or not polcfg.CAD_FIDELITY_RETRY_ENABLED:
            return True, ""
        said = " ".join(concerns(rec))
    except Exception:  # noqa: BLE001 - a measurement that broke is evidence missing, never a crash
        logger.warning("fidelity: the check could not run - the mesh is not judged by it",
                       exc_info=True)
        return True, ""
    detail = f"the mesh does not follow your CAD: {said}"
    return False, refuse(
        f"[FIDELITY] {detail} The mesh is sound but it is not the part. Rebuild it so its "
        "boundary lies on the staged surface: check the region the mesh fills (the point inside "
        "the fluid), and refine where the surface is missed; then run_mesh again.",
        FailureCause.PATCH_NOT_CAPTURED, detail=detail,
        fidelity={k: rec.get(k) for k in ("verdicts", "surface", "volume")})


__all__ = ["CAD_MISSED_CONCERN", "CAD_MISSED_REBUILD", "CONCERN", "OPENING_BAND", "PASS",
           "REBUILD", "RECORD_KEY", "UNMEASURED", "VOLUME_CONCERN_EXTERNAL",
           "VOLUME_CONCERN_INTERNAL", "VOLUME_REBUILD", "WALL_OFF_CONCERN", "WALL_OFF_REBUILD",
           "Surface", "cad_reference", "concerns", "enclosed_volume", "gate_cad_fidelity", "join",
           "measure", "mesh_patches", "outward_volume", "read_stl", "record_into_manifest",
           "surface_distance"]
