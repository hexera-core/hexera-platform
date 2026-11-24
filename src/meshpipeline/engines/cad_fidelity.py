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
     half it was cut from), and only on its WETTED OUTER SKIN: a body made of several solids
     (two halves joined on a plane, a wing pushed into a fuselage, a part pressed against
     another) carries faces inside the body - where the solids touch or overlap - that the air
     never reaches and no external mesh can follow. The skin is what the outside air touches,
     found by flooding the space round the body from outside on a voxel grid finer than the
     mesh's cells (`outer_skin`), the way the mesher keeps the region it reaches from its
     outside point; `cad_hidden_share` says how much of the CAD was set aside. A missing fin or a
     lost part is outside air's surface, so it is still counted. An internal mesh is held to its
     whole fluid boundary.
2. FLUID VOLUME: the mesh's fluid volume (divergence theorem over its boundary) against the volume
   the staged CAD encloses - for an internal flow the fluid itself, for an external body the body
   it displaces. Only where both sides are closed, and for an external body only when its solids
   do not touch or overlap (summed solids count an overlap twice); otherwise "not computable",
   never a guess.
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
    # the same file and openings stage the same boundary: every attempt of a run (each engine) asks for it, and
    # one staging is a minute or more (batch 7 abc 00145189: 225 s in the cfMesh attempt's gate, after 209 s in
    # the snappy attempt's) - staged once per process, a failure included
    import hashlib
    key = (hashlib.sha1(src.read_bytes()).hexdigest(),
           json.dumps(list(intake_patches), sort_keys=True, default=str), read_input_kind(ws))
    if key in _RESTAGED:
        return _RESTAGED[key]
    out = Path(tempfile.mkdtemp(prefix="cad_fidelity_"))
    got: Surface | None = None
    try:
        if read_stl(src).n <= MAX_RESTAGE_TRIANGLES:
            rec = stage_internal_surface(src, out, intake_patches=list(intake_patches),
                                         input_kind=key[2])
            fb = Path(str(rec.get("fluid_boundary") or ""))
            got = read_stl(fb) if fb.exists() else None
    except Exception:  # noqa: BLE001 - the shared staging could not close it: say so, not guess
        logger.info("fidelity: the fluid boundary could not be staged for the check", exc_info=True)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    if len(_RESTAGED) >= 4:
        _RESTAGED.pop(next(iter(_RESTAGED)))
    _RESTAGED[key] = got
    return got


#: _restaged's results this process, by (input.stl's digest, the openings, the input kind)
_RESTAGED: dict[tuple[str, str, str], Surface | None] = {}


#: VER-10: the rule a staged surface holding the wall's both sides is not taken as the fluid's own boundary
BOTH_SIDES_RULE = "VER-10"


def both_sides(s: Surface) -> bool:
    """VER-10: whether a staged surface holds BOTH SIDES of the wall - the part's whole surface with
    the openings' lids on - told by an edge three or more triangles share (at each lid's rim the bore,
    the part's end face and the lid meet). The fluid's own boundary has none. Held to such a surface,
    an internal mesh "misses" the outside of the wall it never meets: batch 7, cfMesh, abc 00188110 and
    00189603 - "49% / 27% of your CAD surface is not reached" on meshes that miss 0.0% of the fluid's
    boundary (199 cm2 of reference round 100 cm2 of fluid wall; 349 round 174). False when it cannot
    be read (held to all of it, as before)."""
    if not s.n:
        return False
    try:
        surf = _polydata(s).clean(tolerance=1e-9, absolute=False)
        edges = surf.extract_feature_edges(boundary_edges=False, non_manifold_edges=True,
                                           feature_edges=False, manifold_edges=False)
        return bool(edges.n_cells)
    except Exception:  # noqa: BLE001 - what cannot be read keeps the whole surface held
        return False


def cad_reference(workspace, flow: str, intake_patches=None, cell: float = 0.0) -> tuple[Surface, str, bool]:
    """The CAD surface the mesh should follow, in metres, where it came from, and whether it is
    the FLUID'S side of the wall (only then can the mesh be held to all of it). Internal flow, in
    this order: the closed fluid boundary the staging wrote (fluid_boundary.stl; cfMesh's geom.stl),
    every further passage included; else the fluid's side of the staged parts, the space the lids
    close off from the outside air, read at the mesh's wall cell `cell` (wetted_side, VER-11); else
    the fluid boundary staged afresh from the metre surface; else the staged parts (the wall and the
    lids), which hold the mesh's wall to the CAD but also bound the metal - the share not reached is
    then not judged, and says so (VER-12). A staged surface that holds the wall's both sides
    (both_sides, VER-10) is not the fluid's own boundary. External flow: the body.
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
        from meshpipeline.cad.fluid_passages import openings_not_meshed
        if openings_not_meshed(ws):
            # INS-40: a cavity no inlet opens into was left unmeshed and told: the mesh is held to the passage it
            # was staged for (the staging's own surface of it - snappy's _passages/p0; cfMesh reads p0 into its
            # geom.stl and removes it), never to the whole staged boundary
            first = sorted((ws / "_passages" / "p0").glob("*.stl")) or (
                [ws / "geom.stl"] if (ws / "geom.stl").exists() else [])
            if first:
                return join([read_stl(q) for q in first]), "staged fluid boundary, the passage meshed", True
        whole = [q for q in (ws / "_internal_stls" / "fluid_boundary.stl",
                             ws / "fluid_boundary.stl", ws / "geom.stl") if q.exists()]
        staged = read_stl(whole[0]) if whole else None
        if staged is not None and not both_sides(staged):
            return staged, "staged fluid boundary", True
        # VER-10: the staged surface is the part's whole surface with the lids on (cfMesh's geom.stl on a
        # pipe: the outside of the wall too), or only its parts are on disk. VER-11: held FIRST to the fluid's
        # side of that same surface - the space the confirmed openings' lids close off from the outside air,
        # the region the engine was staged to mesh. A re-staging from the metre surface takes another path
        # (the B-rep's lids are not the surface path's) and can close another region: batch 7 abc 00157383,
        # the inlet lid 2 mm inside the bore on a 57 mm loop, not the confirmed 61.07 mm mouth; abc 00131564,
        # the part's metal (23.0 cm3 against the 59.8 cm3 fluid the engine meshed). It is the next reading
        # only when the fluid's side cannot be told (a lid with outside air on both sides).
        named = {q.stem: q for q in sorted((ws / "_internal_stls").glob("*.stl"))
                 if q.name != "fluid_boundary.stl"}
        lids = [str(p.get("name")) for p in intake_patches or []
                if isinstance(p, Mapping) and p.get("name") and str(p.get("type") or "wall") != "wall"]
        if named and lids:
            try:
                wet = wetted_side({n: read_stl(q) for n, q in named.items()}, lids, cell)
            except Exception:  # noqa: BLE001 - a reading that could not be made is the next reading's turn
                logger.warning("fidelity: %s the fluid's side of the staged surface was not read",
                               WETTED_SIDE_RULE, exc_info=True)
                wet = None
            if wet is not None:
                return wet, WETTED_SIDE_SOURCE, True
        fb = _restaged(ws, intake_patches)
        if fb is not None:
            return fb, "fluid boundary staged from your file", True
        if staged is not None:
            return staged, "staged part surface and lids", False
        if named:
            return join([read_stl(q) for q in named.values()]), "staged part surface and lids", False
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


#: the outer skin is found on a voxel grid whose cell is this share of the mesh wall's median
#: face size (so the band it keeps round the skin is narrower than the miss distance)...
SKIN_VOXEL_OF_CELL = 0.5
#: ...coarsened until the grid has at most this many voxels and the surface is drawn into it
#: with at most this many points
SKIN_MAX_VOXELS = 16_000_000
SKIN_MAX_RASTER_POINTS = 12_000_000
#: a CAD point within this many voxels of the outside air is on the skin
SKIN_BAND_VOXELS = 2
#: past this share of the CAD hidden inside the body, its solids touch or overlap and their
#: summed volume is not the volume the body displaces
HIDDEN_VOLUME_LIMIT = 0.01


def _raster_count(c: np.ndarray, h: float) -> tuple[np.ndarray, int]:
    """Per triangle the subdivisions that put its points at most h/2 apart, and the point total."""
    edge = np.max(np.linalg.norm(c - np.roll(c, 1, axis=1), axis=2), axis=1)
    k = np.maximum(1, np.ceil(edge / (0.5 * h))).astype(np.int64)
    return k, int(((k + 1) * (k + 2) // 2).sum())


def wetted_skin(cad: Surface, cell: float):
    """Which points of the CAD the FAR FIELD WETS once gaps narrower than the mesh can carry flow
    through are closed: a function points (n,3) -> bool (n,), or None when it cannot be told. The
    reading is the sealed-cavities step's own (engines/sealed_cavities.read_cavities, at the mesh's
    wall cell): the same faces the mesher was told it would reach. An assembly's parts that touch, or
    stand a gap apart that no cell crosses, carry faces inside it that the flow never reaches; a CAD
    whose parts do not quite close lets outer_skin's finer flood in there, and they were counted as
    CAD surface the mesh missed - the RM3 wave absorber, two production-grade meshes refused at
    "14.6% not reached" where the far field wets 12,303 of its 15,648 faces (rulebook 75)."""
    if not cad.n or not cell > 0:
        return None
    try:
        from scipy.spatial import cKDTree

        from meshpipeline.engines.sealed_cavities import read_cavities
        tris = cad.corners()
        rd = read_cavities(tris, cell_m=float(cell))
        if rd is None:
            return None
        wet = np.asarray(rd.wet_sides) != 0
        if len(wet) != len(tris) or wet.all() or not wet.any():
            return None
        tree = cKDTree(tris.mean(axis=1))
    except Exception:  # noqa: BLE001 - a reading we cannot make leaves outer_skin's alone
        logger.warning("fidelity: the wetted faces were not read", exc_info=True)
        return None

    def wetted(points: np.ndarray) -> np.ndarray:
        _d, j = tree.query(np.asarray(points, dtype=float))
        return wet[np.asarray(j, dtype=np.int64)]
    return wetted


#: A far-field wetting reading that keeps less than this share of the outer skin is a failed reading, not an
#: assembly: the outer skin is what the air touches, and the faces the far field wets at the mesh's scale are most of
#: it on every assembly read (RM3: 12,303 of 15,648 faces). Below it the outer skin alone is held, and said.
WETTED_MIN_OF_OUTER = 0.3


def sane_skin(cad: Surface, outer, wetted):
    """(skin, note): both_skins(outer, wetted), unless the wetted reading keeps under WETTED_MIN_OF_OUTER of the
    outer skin's area - then the outer skin alone, with the note saying so ("" when nothing was set aside)."""
    if outer is None or wetted is None or not cad.n:
        return both_skins(outer, wetted), ""
    c, a = cad.centroids(), cad.areas()
    o = np.asarray(outer(c), dtype=bool)
    if not o.any():
        return both_skins(outer, wetted), ""
    kept = float(a[o & np.asarray(wetted(c), dtype=bool)].sum() / max(a[o].sum(), 1e-300))
    if kept < WETTED_MIN_OF_OUTER:
        return outer, (f"the far-field wetting reading kept {kept * 100:.0f}% of the outer skin, under the "
                       f"{WETTED_MIN_OF_OUTER * 100:.0f}% any assembly reads: held to the whole outer skin instead")
    return both_skins(outer, wetted), ""


def both_skins(a, b):
    """The CAD points both readings call skin (either one alone when the other could not be made)."""
    if a is None or b is None:
        return a if b is None else b

    def both(points: np.ndarray) -> np.ndarray:
        return np.asarray(a(points), dtype=bool) & np.asarray(b(points), dtype=bool)
    return both


def outer_skin(cad: Surface, cell: float):
    """Which points of the CAD the OUTSIDE AIR touches: a function points (n,3) -> bool (n,), or
    None when it cannot be told. The CAD is drawn into a voxel grid (cell <= SKIN_VOXEL_OF_CELL of
    `cell`, coarsened to fit the budget), the empty voxels connected to the grid's border are the
    outside air, and a point within SKIN_BAND_VOXELS voxels of that air is on the skin. Faces
    where solids touch or overlap, faces inside another solid and a sealed cavity's walls are
    not; a surface with a hole lets the air in, so everything stays counted (the old reading).
    Orientation-free: it needs no solid to be closed or wound one way."""
    drawn = _drawn(cad, cell)
    if drawn is None:
        return None
    from scipy import ndimage
    solid, origin, h, dims = drawn
    labels, _ = ndimage.label(~solid)          # faces-only (6-) connectivity: air never leaks
    air = labels == labels[0, 0, 0]            # through an edge or corner the surface was drawn on
    del labels
    near = ndimage.binary_dilation(air, structure=np.ones((3, 3, 3), dtype=bool),
                                   iterations=SKIN_BAND_VOXELS)

    def touched(points: np.ndarray) -> np.ndarray:
        idx = np.clip(np.floor((np.asarray(points, dtype=float) - origin) / h).astype(np.int64),
                      0, dims - 1)
        return near[idx[:, 0], idx[:, 1], idx[:, 2]]
    return touched


#: VER-11: the source a mesh is held to when it is the fluid's side of the engine's own staged surface
WETTED_SIDE_SOURCE = "the fluid's side of the staged surface"
#: VER-11: the rule that reads it
WETTED_SIDE_RULE = "VER-11"
#: VER-11: how far off a lid, in voxels, the space it closes is looked for (the first free voxel either side)
LID_PROBE_VOXELS = (1.5, 2.5, 3.5, 5.0)
#: VER-11: the nearest faces searched for the one across a thin wall, and how square across the wall it must lie
ACROSS_NEIGHBOURS = 32
ACROSS_COS = 0.7


def wetted_side(parts: Mapping[str, Surface], lids, cell: float) -> Surface | None:
    """VER-11: the FLUID'S SIDE of the surface the engine was staged with - the part's surface and the lids on
    the confirmed openings (`parts`: patch name -> surface; `lids`: the opening names). The surface is drawn
    into outer_skin's voxel grid; the free space a lid closes off from the outside air (the first free voxel
    off either side of each lid triangle that is not the air) is the fluid; every face within SKIN_BAND_VOXELS
    of it is kept - the bore, its walls and the lids - and the outside of the part is not. A wall thinner than
    the band keeps its outer face too, which lies within a cell of the bore. None when it cannot be told: no
    lid, a lid with outside air on both sides (the passage leaks), or a reading that keeps nothing."""
    lid_names = [n for n in lids if n in parts and parts[n].n]
    if not lid_names or not cell > 0:
        return None
    whole = join(list(parts.values()))
    drawn = _drawn(whole, cell)
    if drawn is None:
        return None
    from scipy import ndimage
    solid, origin, h, dims = drawn
    labels, _ = ndimage.label(~solid)
    air = labels[0, 0, 0]

    def label_at(p: np.ndarray) -> np.ndarray:
        idx = np.floor((p - origin) / h).astype(np.int64)
        inside = np.all((idx >= 0) & (idx < dims), axis=1)
        idx = np.clip(idx, 0, dims - 1)
        out = labels[idx[:, 0], idx[:, 1], idx[:, 2]]
        return np.where(inside, out, air)
    fluid: set[int] = set()
    for name in lid_names:
        lid = parts[name]
        c, a = lid.centroids(), lid.areas()
        nrm = np.cross(lid.corners()[:, 1] - lid.corners()[:, 0], lid.corners()[:, 2] - lid.corners()[:, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-300)
        got = np.zeros(len(c), dtype=np.int64)
        for k in LID_PROBE_VOXELS:
            for sign in (1.0, -1.0):
                lab = label_at(c + sign * k * h * nrm)
                take = (got == 0) & (lab != 0) & (lab != air)
                got[take] = lab[take]
        if not (got != 0).any():
            return None                       # outside air both sides of this lid: the passage leaks
        votes = np.bincount(got[got != 0], weights=a[got != 0])
        fluid.add(int(np.argmax(votes)))
    is_fluid = np.isin(labels, sorted(fluid))
    near = ndimage.binary_dilation(is_fluid, structure=np.ones((3, 3, 3), dtype=bool), iterations=SKIN_BAND_VOXELS)
    cen = whole.centroids()
    idx = np.clip(np.floor((cen - origin) / h).astype(np.int64), 0, dims - 1)
    keep = near[idx[:, 0], idx[:, 1], idx[:, 2]]
    nrm = np.cross(whole.corners()[:, 1] - whole.corners()[:, 0], whole.corners()[:, 2] - whole.corners()[:, 0])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-300)
    # THE FLUID IN FRONT OF THE FACE, not beside it: a face by the fluid is kept when the first free voxel off
    # it along its normal, one side or the other, is the fluid. The part's end face round a lid lies in the
    # lid's plane, beside the fluid inside the rim, with the air in front and the metal behind (batch 7 cfMesh
    # meshes read 1.0-1.1% "not reached" on such annuli, 0.5-0.9 mm outside the rim): with the air on one side
    # and no fluid on either it is dropped. A face with the metal or nothing read on both sides is kept.
    band = np.flatnonzero(keep)
    side = np.zeros((len(band), 2), dtype=np.int64)
    for k in LID_PROBE_VOXELS:
        for s, sign in enumerate((1.0, -1.0)):
            lab = label_at(cen[band] + sign * k * h * nrm[band])
            take = (side[:, s] == 0) & (lab != 0)
            side[take, s] = lab[take]
    wet_side = np.isin(side, sorted(fluid))
    keep[band[~wet_side.any(axis=1) & (side == air).any(axis=1)]] = False
    both = np.zeros(whole.n, dtype=bool)
    both[band[wet_side.any(axis=1) & (side == air).any(axis=1)]] = True
    if both.any():
        # A WALL THINNER THAN THE PROBE: off its bore face and off its outer face alike, one side reads the fluid
        # and the other the air (the grid is coarsened to fit the budget, so a voxel can be wider than the wall).
        # Each such face looks AWAY from the face across the wall from it: the bore face sees the fluid, the outer
        # face the air, which is dropped. A face with no face across it is kept.
        from scipy.spatial import cKDTree
        tree = cKDTree(cen)
        reach = (SKIN_BAND_VOXELS + 1) * h
        amb = np.flatnonzero(both)
        dist, nb = tree.query(cen[amb], k=min(ACROSS_NEIGHBOURS, whole.n), distance_upper_bound=reach)
        dist, nb = np.atleast_2d(dist), np.atleast_2d(nb)
        for row, f in enumerate(amb):
            ok = np.isfinite(dist[row]) & (nb[row] != f) & (nb[row] < whole.n)
            if not ok.any():
                continue
            g = nb[row][ok]
            v = cen[g] - cen[f]
            cos = (v @ nrm[f]) / np.maximum(np.linalg.norm(v, axis=1), 1e-300)
            across = np.abs(cos) > ACROSS_COS
            if not across.any():
                continue
            j = int(np.flatnonzero(across)[0])          # the nearest face across the wall
            mid = int(label_at((0.5 * (cen[f] + cen[g[j]]))[None, :])[0])
            if mid != 0 and mid != air:
                continue                                 # across the fluid (a narrow channel), not the metal
            away = -np.sign(cos[j]) * nrm[f]
            for k in LID_PROBE_VOXELS:
                lab = int(label_at((cen[f] + k * h * away)[None, :])[0])
                if lab == 0:
                    continue
                keep[f] = lab != air
                break
    if not keep.any():
        return None
    return Surface(whole.points, whole.tris[keep], whole.cell_size[keep])


def _drawn(cad: Surface, cell: float):
    """outer_skin's voxel grid with `cad` drawn into it: (solid (bool grid), origin, voxel edge, dims), or None
    when the surface is empty or does not fit the budget."""
    if not cad.n or not cell > 0:
        return None
    c = cad.corners()
    lo, hi = cad.points.min(axis=0), cad.points.max(axis=0)
    span = np.maximum(hi - lo, 1e-12)
    h = SKIN_VOXEL_OF_CELL * float(cell)
    pad = SKIN_BAND_VOXELS + 2
    for _ in range(12):
        dims = np.ceil(span / h).astype(np.int64) + 2 * pad + 1
        k, total = _raster_count(c, h)
        if float(np.prod(dims.astype(float))) <= SKIN_MAX_VOXELS and total <= SKIN_MAX_RASTER_POINTS:
            break
        h *= 1.5
    else:
        return None
    origin = lo - pad * h
    solid = np.zeros(tuple(int(d) for d in dims), dtype=bool)
    for kk in np.unique(k):
        i, j = np.meshgrid(np.arange(kk + 1), np.arange(kk + 1), indexing="ij")
        keep = (i + j) <= kk
        u, v = (i[keep] / kk)[None, :, None], (j[keep] / kk)[None, :, None]
        tris = np.flatnonzero(k == kk)
        step = max(1, 2_000_000 // int(keep.sum()))
        for s in range(0, len(tris), step):
            t = c[tris[s:s + step]]
            p = (t[:, 0, None, :] + u * (t[:, 1] - t[:, 0])[:, None, :]
                 + v * (t[:, 2] - t[:, 0])[:, None, :]).reshape(-1, 3)
            idx = np.clip(np.floor((p - origin) / h).astype(np.int64), 0, dims - 1)
            solid[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    return solid, origin, h, dims


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


def surface_distance(mesh_wall: Surface, mesh_all: Surface, cad: Surface, *, box=None,
                     skin=None) -> dict:
    """Both one-sided distances, sampled and area-weighted (see the module notes). `skin`, when
    given, keeps only the CAD points the outside air touches (see outer_skin)."""
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
        lo = hi = None
        if box is not None:
            # ONLY THE CAD ON THE FLUID'S SIDE OF THE CUT IS HELD: a body on the ground is seated into the
            # floor (the ground contact) and a half model is cut on its symmetry plane, so the CAD under the
            # floor, or past the plane, was never the fluid's to mesh. The pad only absorbs rounding: a pad
            # of 1% of the domain (0.27 m on an Ahmed body's) counted a bottom face seated 8.6 mm into the
            # floor as missed and refused a sound mesh (AhmedML run 1, e2e 2 Nov)
            lo, hi = np.asarray(box[0], dtype=float), np.asarray(box[1], dtype=float)
            pad = 1e-6 * float(np.linalg.norm(hi - lo))
            inside = np.all((c >= lo - pad) & (c <= hi + pad), axis=1)
            c, a = c[inside], a[inside]
        hidden = None
        if skin is not None and len(c):
            wet = np.asarray(skin(c), dtype=bool)
            hidden = float(a[~wet].sum() / max(a.sum(), 1e-300))
            c, a = c[wet], a[wet]
        if len(c):
            i = _sample(len(c), rng)
            d = _distance(c[i], mesh_all)
            from scipy.spatial import cKDTree
            _, near = cKDTree(mesh_all.centroids()).query(c[i])
            h = mesh_all.cell_size[near]
            missed = (d > MISS_CELLS * h) & (d > MISS_FLOOR_OF_DIAG * diag)
            w = a[i]
            if lo is not None:
                # CAD ON THE CUT ITSELF - within the miss distance of the floor or a symmetry plane - is where the
                # body meets that plane: the mesh has the plane there, never a wall, so it is not a miss
                gap = np.minimum(c[i] - lo, hi - c[i]).min(axis=1)
                on_cut = gap <= MISS_CELLS * h
                missed = missed & ~on_cut
                w = np.where(on_cut, 0.0, w)
            out["cad_to_mesh"] = {
                "samples": int(len(i)),
                "p95_cells": _q(np.percentile(d / np.maximum(h, 1e-300), 95)),
                "max_m": float(f"{d.max():.4g}"),
                "max_of_diag": _q(d.max() / diag, 6) if diag else None,
                "cad_missed_share": _q(float(w[missed].sum() / max(w.sum(), 1e-300)), 5)}
        if hidden is not None:
            out.setdefault("cad_to_mesh", {})["cad_hidden_share"] = _q(hidden, 5)
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
    # A SHELL IS NOT HELD TO THE SOLID'S SKIN. A shell mesh lies on the part's mid-surface: half a wall
    # inside the CAD surface by construction, and a part kept solid beside it is not in it at all. Held
    # to the outer surface, thick plates read as "off the CAD" and "missed" though every element is
    # where it belongs (a welded plate column, 10-32 mm walls at 8 mm elements: 20% "off", 7% "missed",
    # refused on both attempts). Where the shell lies is its own record's business (shell.json).
    from meshpipeline.engines.shell_delivery import is_shell_workspace
    if is_shell_workspace(ws):
        return {**rec, "verdict": UNMEASURED,
                "why": "a shell mesh lies on the part's mid-surface, not on its outer surface"}
    try:
        patches = mesh_patches(ws)
        _sizes = join(list(patches.values())).cell_size
        cad, source, fluid_side = cad_reference(ws, flow, intake_patches,
                                                cell=float(np.median(_sizes)) if len(_sizes) else 0.0)
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
        # THE BODY'S BOUNDARY NOT READ IS A FAILED MEASUREMENT, NEVER A 100% MISS. RM5 (c2_rm5 attempt 2): a
        # production-grade snappy mesh with 285,321 faces on its wall patch 'body' was measured with no body patch
        # read (patches_compared []): the CAD was held to the far field and the ground alone and read "100.0% of
        # your CAD surface is not reached", and the gate threw the mesh away.
        faces = manifest.get("patch_face_counts") or {}
        declared = sorted(str(n) for n, ty in roles.items() if ty == "wall" and str(n).lower() not in _GROUND_NAMES
                          and int(faces.get(n) or 0) > 0)
        if not body_names and declared:
            logger.warning("fidelity: the wall patch(es) %s carry faces but were not read from the mesh on disk "
                           "(read: %s) - not measured", declared, sorted(patches))
            return {**rec, "verdict": UNMEASURED,
                    "why": (f"the mesh's wall {', '.join(declared)} was not read from the mesh on disk (read: "
                            f"{', '.join(sorted(patches)) or 'nothing'})")}
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
        skin = None
        if flow == "external" and fluid_side and wall.n:
            # the outside air's skin, on a grid finer than the mesh's own wall faces - and only the
            # faces the far field wets at the mesh's own scale (wetted_skin)
            _cell = float(np.median(wall.cell_size))
            skin, _skin_note = sane_skin(cad, outer_skin(cad, _cell), wetted_skin(cad, _cell))
            if _skin_note:
                logger.warning("fidelity: %s", _skin_note)
                rec["skin_note"] = _skin_note
        dist = surface_distance(wall, allb, cad if fluid_side else _empty(),
                                box=(lo, hi) if flow == "external" else None, skin=skin)
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
    if "cad_missed_share" in c2m:
        miss = float(c2m["cad_missed_share"])
        verdicts["cad_to_mesh"] = (REBUILD if miss > CAD_MISSED_REBUILD else
                                   CONCERN if miss > CAD_MISSED_CONCERN else PASS)
    elif flow == "internal" and not fluid_side and m2c:
        # VER-12: NO UNJUDGED PASS. The fluid's side of the wall could not be told (no closed fluid boundary,
        # no lid that closes the fluid off, no re-staging): whether the mesh reaches all of it is not judged,
        # and a check not made is never read as met - batch 7 abc 00157702 passed with it unjudged
        verdicts["cad_to_mesh"] = CONCERN
        dist["cad_to_mesh_not_judged"] = True

    # 2. volume
    vol: dict = {}
    try:
        # solids that touch or overlap: their summed volume counts the overlap twice
        joined = float(c2m.get("cad_hidden_share") or 0.0) > HIDDEN_VOLUME_LIMIT
        # VER-11: the fluid's side of the staged surface keeps a thin wall's outer face too: not a closed surface
        wet = source == WETTED_SIDE_SOURCE
        cad_v = enclosed_volume(cad) if fluid_side and not joined and not wet else None
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
                          "the fluid's side of the staged surface is not a closed surface" if wet else
                          "the CAD's solids touch or overlap inside the body, so their summed "
                          "volume is not the volume the body displaces" if joined else
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
    if v.get("cad_to_mesh") in (CONCERN, REBUILD) and s.get("cad_to_mesh_not_judged"):
        # VER-12: a check not made, said as that
        out.append("Whether the mesh reaches all of your CAD surface was not judged: the fluid's side of the "
                   "wall could not be told from your file and its openings, so this check is not counted as met.")
    elif v.get("cad_to_mesh") in (CONCERN, REBUILD):
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


#: A WRONG REGION, not a fidelity miss: more of the mesh's wall than this lies more than a cell off the fluid's own
#: surface...
WRONG_REGION_OFF_SHARE = 0.5
#: ...and its median wall face lies this many cells off it: a mesh that misses a surface lies a cell or two off it;
#: one of another region lies tens of cells off (the siphoned box's mesh of its metal: 81%, a median of 30 to 47)
WRONG_REGION_MEDIAN_CELLS = 5.0


def wrong_region(rec: Mapping) -> str:
    """An internal mesh of another region than the fluid - the part's metal, or the space outside it - said in
    plain words with its numbers; "" for any other record. Measured against the FLUID's own closed boundary
    only: a reference that also bounds the metal cannot tell."""
    if rec.get("flow") != "internal" or not str(rec.get("cad_source") or "").startswith(
            ("staged fluid", "fluid boundary", WETTED_SIDE_SOURCE)):
        return ""
    raw = rec.get("surface")
    surf: Mapping = raw if isinstance(raw, Mapping) else {}
    raw = surf.get("mesh_to_cad")
    m2c: Mapping = raw if isinstance(raw, Mapping) else {}
    o, m = m2c.get("wall_off_share"), m2c.get("p50_cells")
    if not isinstance(o, (int, float)) or not isinstance(m, (int, float)):
        return ""
    off, p50 = float(o), float(m)
    if off <= WRONG_REGION_OFF_SHARE or p50 <= WRONG_REGION_MEDIAN_CELLS:
        return ""
    far = m2c.get("max_m")
    tail = f", up to {float(far) * 1e3:.0f} mm" if isinstance(far, (int, float)) else ""
    return (f"the mesh filled the part's metal or the space outside it, not the fluid: {off * 100:.0f}% of its "
            f"wall lies more than one cell off the fluid's surface, half of it more than {p50:.0f} cells away{tail}")


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
        region = wrong_region(rec)
    except Exception:  # noqa: BLE001 - a measurement that broke is evidence missing, never a crash
        logger.warning("fidelity: the check could not run - the mesh is not judged by it",
                       exc_info=True)
        return True, ""
    if region:
        # THE WRONG REGION (LAD-cfd-8, rulebook 75): not a mesh that misses the fluid's surface here and
        # there, but one of another region - the part's metal, or the space outside it
        detail = region
        return False, refuse(
            f"[FIDELITY] {detail}. Check the openings on the geometry stage: the fluid is the space your "
            "inlets and outlets open into. If the part itself is the fluid (a duct modelled as a solid), "
            "say so in your notes. Every engine meshes the same staged region, so no other engine was tried.",
            FailureCause.PATCH_NOT_CAPTURED, detail=detail, wrong_region=True,
            fidelity={k: rec.get(k) for k in ("verdicts", "surface", "volume")})
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
           "WRONG_REGION_MEDIAN_CELLS", "WRONG_REGION_OFF_SHARE", "wrong_region",
           "BOTH_SIDES_RULE", "WETTED_SIDE_RULE", "WETTED_SIDE_SOURCE", "Surface", "both_sides", "wetted_side", "cad_reference", "concerns", "enclosed_volume", "gate_cad_fidelity", "join",
           "outer_skin",
           "measure", "mesh_patches", "outward_volume", "read_stl", "record_into_manifest",
           "surface_distance"]
