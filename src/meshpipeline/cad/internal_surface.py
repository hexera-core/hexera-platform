# Responsibility: Turn a triangle surface and the openings the user confirmed on it into the one
# surface every internal-flow mesher reads: a CLOSED fluid boundary, the wall plus one patch per
# opening under the name the user gave it, and a point proven to lie inside the fluid.
# Owns: which loop or face region of the surface each declared opening is, the lids on open ends
# and bore mouths, the sealing of every other opening into the wall, which side of a thick wall
# the fluid is on, the seed point, and the checks that the result is closed and named.
# Boundaries: numpy and scipy over triangles, in metres. No OpenCASCADE, no engine, no storage, no
# model, and nothing about what the part is: every rule here is measured on the triangles. The
# B-rep path for a CAD solid stays in cad/cad_tessellate.tessellate_internal; stage_internal()
# below picks one of the two for a source file.
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: A declared opening's location picks a hole when it lies within this share of the hole's size
#: from the hole's centre - the stage put the sticker at the centre, so the real distance is ~0.
HOLE_REACH = 0.5
#: ...and a face region (a capped mouth on a closed fluid surface) when the nearest face is within
#: this share of the opening's size.
CAP_REACH = 0.2
#: The measured and declared areas of one opening must agree within this factor to be matched.
#: Loose on purpose: the binder (engines/port_binding) applies the strict band afterwards, and its
#: refusal names both numbers to the user.
MATCH_RATIO = 2.0
#: Faces meeting at more than this angle bound a capped mouth's face region.
CAP_CREASE_DEG = 30.0
#: Two faces lie in one flat region when their normals agree within 3 degrees (and each centre is
#: on the other's plane). The scout's former flat-face test (cad/scout_mesh.PLANE_COS, which the
#: openings work replaced with cad/open_ends.find_caps): this staging was proven with it.
FLAT_COS = math.cos(math.radians(3.0))
#: The seed must stand at least this share of its opening's radius clear of every face.
SEED_CLEARANCE = 0.02
#: A loop along which the wall runs OUTWARD from it (away from its axis) on at least this share of
#: its edges is a STEP in the passage - where it widens: a counterbore's floor, the lip of an exit
#: cone. A centre body's own surface lies wholly within its edge, so such a loop is never one, and
#: it is where a bore opens through a face (the CAD path's bore: the inner wire of a planar face).
STEP_SHARE = 0.5
#: ...counting a face as running outward when its direction from the loop leans away from the axis
#: by more than this (a faceted cylinder's own faces lean by its sagitta only: 0.2 at 8 sides).
OUTWARD_LEAN = 0.3


class InternalSurfaceError(RuntimeError):
    """The surface cannot be made into a closed fluid boundary with the confirmed openings - it is
    said in words the user can act on."""


@dataclass
class _Opening:
    kind: str                         # "rim" (open end) | "bore" (thick wall's mouth) | "cap" (face region)
    centroid: np.ndarray
    normal: np.ndarray                # out of the fluid, the way the fluid leaves
    area: float
    loop: np.ndarray | None = None    # vertex ids, in order, for rims and bores
    edges: np.ndarray | None = None   # the loop's edge ids
    faces: np.ndarray | None = None   # face ids of a cap
    planarity: float = 0.0
    name: str = ""
    role: str = "wall"
    #: a ring-shaped opening's inner edge: the loop of a centre body standing in the mouth, in its
    #: plane (a rod's open end, or the outline of its flush end face). The lid is the ring between.
    inner: _Opening | None = None

    @property
    def diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)

    @property
    def size(self) -> float:
        """How big the mouth is across: its outer edge for a ring, else its own area."""
        return 2.0 * math.sqrt(max(self.area + (self.inner.area if self.inner is not None else 0.0), 0.0) / math.pi)

    def all_edges(self) -> np.ndarray:
        own = self.edges if self.edges is not None else np.zeros(0, dtype=np.int64)
        if self.inner is None or self.inner.edges is None:
            return own
        return np.r_[own, self.inner.edges]

    def key(self) -> frozenset:
        return frozenset(self.loop.tolist()) if self.loop is not None else frozenset()


@dataclass
class _Port:
    name: str
    role: str
    near: np.ndarray | None
    area: float | None
    #: how far across the mouth reaches (its outer diameter, or longest side), metres - for a
    #: ring the area alone understates it
    across: float = 0.0

    @property
    def reach(self) -> float:
        return max(self.across, 2.0 * math.sqrt(self.area / math.pi) if self.area else 0.0)


@dataclass
class StagedInternal:
    """What the staging produced, before it is written."""
    verts: np.ndarray
    faces: np.ndarray
    labels: list[str]                 # one patch name per face
    ports: dict[str, _Opening]
    wall: str
    seed: np.ndarray
    facts: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------ entry points ----
def is_cad(path) -> bool:
    """Whether a source file is a CAD solid (it takes the B-rep path, tessellate_internal); every
    other file arrives as the staged metre surface (input.stl) and takes this module's path. The
    answer is the intake's one CAD-or-surface table (contracts/intake_formats), never a list here."""
    from meshpipeline.contracts.intake_formats import is_cad as _is_cad
    return _is_cad(path)


#: An engine's staging failure is recorded ONCE, as the attempt's pre-flight refusal under this gate
#: (agents/builder/attempt._record_staging_failure, which the executor reports). The engines' own
#: inspection reads its true reason back here: a staged file missing says nothing about why.
STAGING_GATE = "staging"


def staging_failure(workspace) -> str:
    """The recorded staging failure in plain words, or "" when staging did not fail."""
    from meshpipeline.engines.preflight import read_refusal
    refusal = read_refusal(workspace)
    if refusal is None or refusal.gate != STAGING_GATE:
        return ""
    return str((refusal.facts or {}).get("reason") or refusal.builder_text or "")


def stage_internal(source_path, workspace, *, prepared, intake_patches: list | None,
                   input_kind: str = "", out_name: str = "_internal_stls",
                   declared_ports=None, cad_kwargs: dict | None = None) -> dict:
    """The internal-flow staging for any source file, in the shape every engine reads (see
    stage_internal_surface). A CAD solid goes through the B-rep path exactly as before; a surface
    (STL, OBJ, PLY, VTP, ... - whatever the shared staging turned into the workspace's metre
    surface, input.stl) goes through this module. When the B-rep path cannot separate the fluid
    (an opening that is not a flat face, say) the solid's own tessellation is staged the surface
    way instead, and the record says so. A declaration that disagrees with the geometry is never
    retried: it is the user's to settle."""
    ws = Path(workspace)
    out_dir = ws / out_name
    if is_cad(source_path):
        from meshpipeline.cad.cad_tessellate import tessellate_internal
        from meshpipeline.engines.port_binding import BindError, declaration_targets
        try:
            return tessellate_internal(
                source_path, out_dir, prepared=prepared,
                fluid_solid=(str(input_kind or "").strip() == "fluid-domain"),
                declared_ports=(declared_ports if declared_ports is not None
                                else declaration_targets(intake_patches or [])),
                **(cad_kwargs or {}))
        except BindError:
            raise
        except Exception as exc:  # noqa: BLE001 - the surface path is the way out, said below
            if not (ws / "input.stl").exists():
                raise
            logger.warning("internal staging: the B-rep path could not separate the fluid (%s: %s) "
                           "- staging the solid's own surface instead", type(exc).__name__, exc)
            t = stage_internal_surface(ws / "input.stl", out_dir, intake_patches=intake_patches,
                                       input_kind=input_kind)
            t["facts"]["cad_path_failed"] = f"{type(exc).__name__}: {exc}"[:300]
            return t
    return stage_internal_surface(ws / "input.stl", out_dir, intake_patches=intake_patches,
                                  input_kind=input_kind)


def stage_internal_surface(surface_path, out_dir, *, intake_patches: list | None = None,
                           input_kind: str = "") -> dict:
    """Stage a metre triangle surface for internal flow and write it into `out_dir`.

    Returns the record the CAD path (tessellate_internal) returns, so every engine reads one shape:
      stls            patch name -> STL path (binary, metres); the wall under "wall"
      openings        port name -> {"area", "centroid", "normal", "kind"}
      interior_point  a point proven inside the fluid (generalised winding number of the closed
                      boundary, and ray parity both ways), clear of every face
      bbox_min/max    of the staged fluid boundary
      fluid_boundary  ONE STL holding every patch as a named solid (cfMesh, gmsh read this)
      sealed          the openings that were not declared, closed into the wall
      facts           the checks: closed, manifold, consistently wound, lids that cross nothing,
                      one patch per opening
    Port names are the declared ones; with no declaration the openings the measuring step proposes
    are taken, the largest called inlet and the rest outlet (outlet_1, outlet_2, ...)."""
    from meshpipeline.cad.scout_mesh import read_triangles
    tris = read_triangles(Path(surface_path))
    staged = stage_triangles(tris, intake_patches=intake_patches, input_kind=input_kind,
                             surface_path=Path(surface_path))
    return write_staged(staged, Path(out_dir))


def stage_triangles(tris, *, intake_patches: list | None = None, input_kind: str = "",
                    surface_path: Path | None = None) -> StagedInternal:
    from meshpipeline.cad.open_ends import _Mesh, skin_faces

    tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if len(tris) < 4:
        raise InternalSurfaceError("the surface holds too few triangles to bound a fluid")
    verts, faces = skin_faces(tris)
    # the coordinates as they will be written (binary STL is float32): every check below is made
    # on exactly the numbers the mesher reads
    verts, faces = _weld_exact(verts.astype(np.float32).astype(np.float64), faces)
    mesh = _Mesh(skin=(verts, faces))
    diag = mesh.diag
    rims = _rims(mesh)
    outlines = _outlines(mesh, {r.key() for r in rims})
    bores = _bores(mesh, outlines)
    ports_decl, wall = _declared(intake_patches, surface_path, rims, bores, mesh)
    matched = _match(ports_decl, rims, bores, mesh, inner_pool=[*rims, *outlines], input_kind=input_kind,
                     step_pool=outlines)
    port_loops = [o for o in matched.values() if o.loop is not None]
    caps = [o for o in matched.values() if o.kind == "cap"]
    # every open end that is not a declared opening is sealed into the wall: the fluid may leave
    # only where the user said it does (a ring opening's inner edge is part of its own lid)
    port_loop_keys = {o.key() for o in port_loops} | {o.inner.key() for o in port_loops if o.inner is not None}
    seal_rims = [r for r in rims if r.key() not in port_loop_keys]
    seal_bores: list[_Opening] = []
    lidded = port_loops + seal_rims
    pieces, chosen, conflict = _fluid_side(mesh, lidded, caps)
    if conflict:
        # THE FLUID REACHES THE OUTSIDE BY ANOTHER WAY: a thick wall's bore the user did not declare
        # (a drain, a stub left open) joins the passage to the outer skin. Its mouth is sealed into
        # the wall, as the CAD path seals an undeclared opening, and the sides are read again - once:
        # with no undeclared bore left to seal, nothing would change on a second reading (a loop that
        # read the same sides forever is what held the rocket nozzle's STL for the lab's 90 minutes).
        seal_bores = [b for b in bores if b.key() not in port_loop_keys]
        if seal_bores:
            lidded = port_loops + seal_rims + seal_bores
            pieces, chosen, conflict = _fluid_side(mesh, lidded, caps)
    if conflict:
        raise InternalSurfaceError(
            "the openings confirmed on the picture do not close the fluid in: the passage reaches the "
            "outside of the part some other way. Add the missing opening on the picture (or mark it "
            "as not an opening so it is sealed), then run again.")
    lids = {id(o): _lid_for(mesh, o) for o in lidded}
    V, F, L, is_lid, lid_report, sealed = _assemble(mesh, pieces, chosen, lidded, caps, wall, lids)
    F = _orient(V, F)
    # PIECES NO OPENING TOUCHES: a closed body standing in the fluid (a centre rod, a vane modelled
    # on its own) is part of the fluid's boundary; one outside it (a bracket beside the part) is
    # not, and neither is a loose open sheet (a stray flap of a scan): it bounds no volume
    touched = set()
    for o in port_loops:
        touched.update(np.unique(pieces[_loop_faces(mesh, o)]).tolist())
    for o in caps:
        touched.update(np.unique(pieces[o.faces]).tolist())
    edged = np.zeros(int(pieces.max()) + 1, dtype=bool)
    open_e = np.flatnonzero(mesh.count != 2)
    edged[pieces[np.r_[mesh.f0[open_e], mesh.f1[open_e][mesh.f1[open_e] >= 0]]]] = True
    for o in lidded:
        edged[pieces[_loop_faces(mesh, o)]] = True
    rest = [p for p in range(int(pieces.max()) + 1)
            if p not in touched and p not in set(chosen.tolist()) and not edged[p]]
    loose = [p for p in range(int(pieces.max()) + 1)
             if p not in touched and p not in set(chosen.tolist()) and edged[p]]
    if rest:
        T = V[F]
        probes = []
        for p in rest:
            fs = np.flatnonzero(pieces == p)
            probes.append(mesh.centre[fs[np.argmax(mesh.area[fs])]])
        w = _winding(T, np.asarray(probes))
        inner = [p for p, wi in zip(rest, w) if wi >= 0.5]
        if inner:
            chosen = np.asarray(sorted(set(chosen.tolist()) | set(inner)), dtype=np.int64)
            V, F, L, is_lid, lid_report, sealed = _assemble(mesh, pieces, chosen, lidded, caps, wall, lids)
            F = _orient(V, F)
    facts = _verify(V, F, L, list(matched), wall, is_lid=is_lid)
    facts.update({"lids": lid_report, "sealed": sealed, "input_kind": str(input_kind or ""),
                  "pieces_kept": int(len(chosen)), "pieces": int(pieces.max() + 1) if len(pieces) else 0,
                  "loose_open_pieces_left_out": [{"faces": int(np.sum(pieces == p))} for p in loose],
                  "rims_found": len(rims), "bores_found": len(bores), "diag_m": round(diag, 6)})
    for o in matched.values():
        o.normal = _outward(V, F, L, o)
    seed, seed_facts = _seed(V, F, L, matched)
    facts["seed"] = seed_facts
    return StagedInternal(verts=V, faces=F, labels=L.tolist(), ports=matched, wall=wall, seed=seed,
                          facts=facts)


def write_staged(staged: StagedInternal, out_dir: Path) -> dict:
    from meshpipeline.cad.stl_io import _write_solid, write_stl_binary

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    V, F = staged.verts, staged.faces
    L = np.asarray(staged.labels, dtype=object)
    names = [staged.wall, *staged.ports]
    stls: dict[str, str] = {}
    solids: dict[str, list] = {}
    for name in names:
        sel = F[L == name]
        tris = [tuple(V[i].tolist() for i in f) for f in sel]
        key = "wall" if name == staged.wall else name
        path = out_dir / f"{_file_safe(key)}.stl"
        write_stl_binary(path, tris)
        stls[key] = str(path)
        solids[name] = [[tris[i] for i in piece] for piece in _pieces(sel)]
    # ONE FILE, EVERY PATCH A NAMED SOLID - and a patch in several separate pieces (a wall that is
    # a pipe and a centre rod) as several solids of that one name: a mesher that builds topology
    # from the file (gmsh) makes one surface per solid and never has to split one itself
    boundary = out_dir / "fluid_boundary.stl"
    with boundary.open("w", encoding="utf-8") as fh:
        for name, pieces in solids.items():
            for piece in pieces:
                _write_solid(fh, name, piece)
    lo, hi = V.min(axis=0), V.max(axis=0)
    openings = {}
    for n, o in staged.ports.items():
        rec = {"area": round(float(o.area), 10),
               "centroid": [round(float(v), 6) for v in o.centroid],
               "normal": [round(float(v), 6) for v in o.normal],
               "kind": o.kind}
        if o.inner is not None:
            # a ring: what its inner edge encloses, in the shape the CAD path reports a ring port
            # face's inner wire - so the binder holds a declaration against the ring, the centre
            # body's disc or the whole mouth (engines/port_binding._measures)
            rec["opening"] = {"area": round(float(o.inner.area), 10),
                              "centroid": [round(float(v), 6) for v in o.inner.centroid]}
            rec["kind"] = "ring"
        openings[n] = rec
    record = {
        "stls": stls,
        "interior_point": [round(float(v), 7) for v in staged.seed],
        "bbox_min": [round(float(v), 6) for v in lo],
        "bbox_max": [round(float(v), 6) for v in hi],
        "openings": openings,
        "n_wall_faces": int(np.sum(L == staged.wall)),
        # the wall is the fluid's own side (a thick wall is staged from its fluid side), never
        # a hollow part's whole metal skin (engines/passage.passage_field_of_stls reads it whole)
        "wall_bounds_fluid": True,
        "sealed": {"undeclared_openings": staged.facts.get("sealed", []), "open_rims": []},
        "fluid_boundary": str(boundary),
        "wall_name": staged.wall,
        "source": "surface",
        "facts": staged.facts,
    }
    (out_dir / "internal_surface.json").write_text(json.dumps(record, indent=1, default=_jsonable))
    return record


def _jsonable(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return str(v)


def _file_safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name)) or "patch"


# ---------------------------------------------------------------------------- the surface ----
def _weld_exact(verts: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Points that became one point when rounded to what the file holds are one point, and the
    faces that collapsed with them go."""
    uniq, inv = np.unique(verts, axis=0, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    f = inv[faces]
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    return uniq, f[ok]


def _rims(mesh) -> list[_Opening]:
    """Every closed loop of open edges, measured, with the side the fluid leaves by: away from the
    wall that runs back from the loop."""
    out = []
    for vidx, eidx in mesh._open_chains():
        P = mesh.verts[vidx]
        try:
            from meshpipeline.cad.lids import frame
            c, n, area, dev = frame(P)
        except Exception:  # noqa: BLE001, S112 - a loop with no area is no opening
            continue
        f = mesh.f0[eidx]
        mid = (P + np.roll(P, -1, axis=0)) / 2.0
        back = float(np.sum(mesh.area[f] * ((mesh.centre[f] - mid) @ n)))
        out_n = -n if back > 0 else n
        out.append(_Opening(kind="rim", centroid=c, normal=out_n, area=area, loop=np.asarray(vidx),
                            edges=np.asarray(eidx), planarity=dev / max(2.0 * math.sqrt(area / math.pi), 1e-30)))
    return out


def _outlines(mesh, skip: set) -> list[_Opening]:
    """Every outline of a smooth patch of the surface (faces joined across edges that are not
    sharp), once each, measured: the loops a bore's mouth or a centre body's flush end can be."""
    from meshpipeline.cad.lids import frame

    out, seen = [], set(skip)
    for vidx, eidx in mesh._patch_outlines():
        key = frozenset(np.asarray(vidx).tolist())
        if key in seen:
            continue
        seen.add(key)
        P = mesh.verts[vidx]
        try:
            c, n, area, dev = frame(P)
        except Exception:  # noqa: BLE001, S112 - no area, no loop
            continue
        d = 2.0 * math.sqrt(area / math.pi)
        if d <= 0:
            continue
        out.append(_Opening(kind="outline", centroid=c, normal=n, area=area, loop=np.asarray(vidx),
                            edges=np.asarray(eidx), planarity=dev / d))
    return out


def _bores(mesh, outlines: list[_Opening]) -> list[_Opening]:
    """The mouths of a thick wall's bores, by the hole finder's own definition (cad/open_ends)."""
    from meshpipeline.cad.open_ends import MAX_PROBED, _distinct

    shaped = []
    for o in outlines:
        s = mesh.shape((o.loop, o.edges))
        if s is None or s["kind"] != "bore":
            continue
        s["vidx"], s["eidx"] = o.loop, o.edges
        shaped.append(s)
    shaped.sort(key=lambda s: s["area"], reverse=True)
    found = []
    by_hole = {}
    for s in shaped[:MAX_PROBED]:
        hb = mesh.probe(s)
        if hb is None:
            continue
        found.append(hb)
        by_hole[id(hb[0])] = s
    out = []
    for h in _distinct(found):
        s = by_hole[id(h)]
        out.append(_Opening(kind="bore", centroid=h.centroid, normal=h.normal, area=h.area,
                            loop=np.asarray(s["vidx"]), edges=np.asarray(s["eidx"]),
                            planarity=h.planarity))
    return out


# ---------------------------------------------------------------------- the declared openings ----
def _declared(intake_patches, surface_path, rims, bores, mesh) -> tuple[list[_Port], str]:
    from meshpipeline.engines.port_binding import DeclaredPatch

    entries = [p for p in (intake_patches or []) if isinstance(p, dict)]
    walls = [str(p.get("name")) for p in entries if str(p.get("type") or "").strip() == "wall" and p.get("name")]
    wall = walls[0] if walls else "wall"
    ports: list[_Port] = []
    for p in entries:
        role = str(p.get("type") or "").strip()
        if role not in ("inlet", "outlet") or not p.get("name"):
            continue
        dp = DeclaredPatch.from_intake(p)
        near = np.asarray(dp.near_mm, dtype=float) / 1000.0 if dp.near_mm is not None else None
        across = max([v for v in (dp.diameter_mm, dp.width_mm, dp.height_mm)
                      if isinstance(v, (int, float)) and v > 0] or [0.0]) / 1000.0
        ports.append(_Port(name=str(p["name"]), role=role, near=near, area=dp.declared_area_m2(),
                           across=across))
    if ports and all(p.near is not None for p in ports):
        return ports, wall
    proposed = _proposal(surface_path, rims, bores, mesh)
    if not ports:
        # NOTHING DECLARED (a programmatic submit): the openings the measuring step proposes, the
        # largest the inlet - the CAD path's rule, said in the record
        proposed.sort(key=lambda o: -o.area)
        names = ["inlet"] + (["outlet"] if len(proposed) == 2 else [f"outlet_{k}" for k in range(1, len(proposed))])
        return [_Port(name=n, role="inlet" if k == 0 else "outlet", near=o.centroid, area=o.area)
                for k, (n, o) in enumerate(zip(names, proposed))], wall
    # some declared without a location (named in the chat with a size only): the proposal's
    # openings are matched to them by size, the way the CAD path picks faces for a declaration
    from meshpipeline.cad.cad_tessellate import select_declared_openings
    taken = {tuple(np.round(p.near, 6)) for p in ports if p.near is not None}
    pool = [o for o in proposed if tuple(np.round(o.centroid, 6)) not in taken]
    unlocated = [p for p in ports if p.near is None]
    cands = [(i, o.area, tuple(o.centroid)) for i, o in enumerate(pool)]
    try:
        picks = select_declared_openings(cands, [{"name": p.name, "area_m2": p.area, "near_m": None}
                                                 for p in sorted(unlocated, key=lambda q: q.name)])
    except ValueError as exc:
        from meshpipeline.engines.port_binding import BindError
        raise BindError(str(exc)) from exc
    for port, i in zip(sorted(unlocated, key=lambda q: q.name), picks):
        port.near = pool[i].centroid
    return ports, wall


def _proposal(surface_path, rims, bores, mesh) -> list[_Opening]:
    """The openings the measuring step would propose for this surface: its holes, or - a closed
    fluid body - the flat discs it finds (cad/scout_mesh)."""
    if rims or bores:
        return [*rims, *bores]
    if surface_path is None:
        return []
    from meshpipeline.cad.scout_mesh import scout_mesh
    try:
        res = scout_mesh(Path(surface_path), scale_to_m=1.0)
    except Exception:  # noqa: BLE001 - no proposal is a refusal below, with the reason
        return []
    out = []
    for o in res.openings:
        out.append(_Opening(kind="cap", centroid=np.asarray(o.centroid, dtype=float),
                            normal=np.asarray(o.normal, dtype=float), area=float(o.area)))
    return out


def _listing(rims, bores) -> str:
    rows = [f"  {o.kind} at ({o.centroid[0]:.4f}, {o.centroid[1]:.4f}, {o.centroid[2]:.4f}) m, "
            f"{o.diameter * 1000:.1f} mm across" for o in sorted([*rims, *bores], key=lambda q: -q.area)[:12]]
    return "\n".join(rows) if rows else "  (no open ends or bore mouths)"


def _inner_loop(o: _Opening, pool: list[_Opening], mesh) -> _Opening | None:
    """The largest loop lying in the mouth's own plane, around its centre and wholly inside it -
    the edge of a centre body standing in the opening (an annular passage's rod: its open end, or
    the outline of its flush end face). The opening is then the ring between the two. A loop the
    wall runs outward from is not a body's edge but a step in the passage just inside the mouth
    (the rocket nozzle's exit cone, 0.76 mm below its 73 mm counterbore): never a ring's inner
    edge, or the ring's lid leaves the narrower bore open and the fluid reaches the outside."""
    d = o.diameter
    dev = o.planarity * d
    best = None
    for q in pool:
        if q.loop is None or q.key() == o.key() or q.area >= 0.95 * o.area:
            continue
        if abs(float(q.normal @ o.normal)) < 0.95:
            continue
        rel = q.centroid - o.centroid
        axial = float(rel @ o.normal)
        if abs(axial) > 0.02 * d + dev + q.planarity * q.diameter:
            continue
        if float(np.linalg.norm(rel - axial * o.normal)) > 0.3 * d:
            continue
        if best is not None and q.area <= best.area:
            continue
        if not _inside_loop(mesh, o, q) or _step_share(mesh, q) >= STEP_SHARE:
            continue
        best = q
    return best


def _step_share(mesh, o: _Opening) -> float:
    """The share of a loop's edges along which a face runs OUTWARD from it - away from the loop's
    own axis, by more than OUTWARD_LEAN of its direction: the wall widening there (STEP_SHARE).
    Orientation-free: it reads where the faces lie, never which way they were wound."""
    e = np.asarray(o.edges if o.edges is not None else [], dtype=np.int64)
    if len(e) == 0:
        return 0.0
    P = mesh.verts
    mid = (P[mesh.ea[e]] + P[mesh.eb[e]]) / 2.0
    rel = mid - o.centroid
    rad = rel - np.outer(rel @ o.normal, o.normal)
    rad /= np.linalg.norm(rad, axis=1, keepdims=True).clip(1e-30)
    out = np.zeros(len(e), dtype=bool)
    for f in (mesh.f0[e], mesh.f1[e]):
        d = mesh.centre[np.maximum(f, 0)] - mid
        d /= np.linalg.norm(d, axis=1, keepdims=True).clip(1e-30)
        out |= (f >= 0) & (np.einsum("ij,ij->i", d, rad) > OUTWARD_LEAN)
    return float(out.mean())


def _step_mouth(mesh, o: _Opening) -> _Opening:
    """A step loop as an opening (a bore's mouth, as the CAD path calls the inner wire of a planar
    face), facing the way the fluid leaves: away from the wall that runs back from it, the rims'
    own rule."""
    P = mesh.verts
    e = np.asarray(o.edges, dtype=np.int64)
    mid = (P[mesh.ea[e]] + P[mesh.eb[e]]) / 2.0
    back = 0.0
    for f in (mesh.f0[e], mesh.f1[e]):
        ok = f >= 0
        back += float(np.sum(mesh.area[f[ok]] * ((mesh.centre[f[ok]] - mid[ok]) @ o.normal)))
    n = -o.normal if back > 0 else o.normal
    return _Opening(kind="bore", centroid=o.centroid, normal=n, area=o.area, loop=o.loop, edges=o.edges,
                    planarity=o.planarity)


def _inside_loop(mesh, outer: _Opening, inner: _Opening) -> bool:
    """Every point of `inner` lies closer to the outer loop's centre, in its plane, than every
    point of the outer loop does."""
    n = outer.normal
    def radial(idx):
        rel = mesh.verts[idx] - outer.centroid
        return np.linalg.norm(rel - np.outer(rel @ n, n), axis=1)
    return float(radial(inner.loop).max()) < float(radial(outer.loop).min())


def _match(ports: list[_Port], rims, bores, mesh, inner_pool=(), input_kind: str = "",
           step_pool=()) -> dict[str, _Opening]:
    """Each declared opening as a loop of the surface (an open end or a bore's mouth, or the ring
    between such a loop and a centre body's edge inside it) or, on a closed fluid surface, as the
    face region that caps it. A location decides; the size has to agree within MATCH_RATIO with
    one of the opening's measures (its own area; for a ring also the outer disc and the inner).

    The hole finder proposes one mouth per opening, the outermost (cad/open_ends._distinct), but a
    user may place the opening at a step just inside it - the narrower bore behind a counterbore,
    where the CAD path reads the inner wire of the step's planar face. On a part's wall, every
    loop of `step_pool` the wall widens from (STEP_SHARE) is such a mouth too. Not on a body
    declared to be the fluid itself: its openings are its faces."""
    from meshpipeline.engines.port_binding import BindError

    if len(ports) < 1:
        raise BindError("no opening was declared or found on the surface - an internal flow needs "
                        "at least an inlet and an outlet")
    out: dict[str, _Opening] = {}
    taken: set = set()
    fluid_body = str(input_kind or "").strip() == "fluid-domain"
    holes = [*rims, *bores]
    if not fluid_body:
        known = {h.key() for h in holes}

        def near_a_port(q) -> bool:
            return any(p.near is not None and float(np.linalg.norm(q.centroid - p.near))
                       <= HOLE_REACH * max(q.diameter, p.reach) for p in ports)
        holes += [_step_mouth(mesh, q) for q in step_pool
                  if q.loop is not None and q.key() not in known and near_a_port(q)
                  and _step_share(mesh, q) >= STEP_SHARE]
    rings: dict[int, _Opening | None] = {}

    def size_err(measures, declared: float | None) -> float:
        if not declared:
            return 0.0
        errs = [abs(math.log(a / declared)) for a in measures if a > 0]
        ok = [e for e in errs if e <= math.log(MATCH_RATIO)]
        return min(ok) if ok else math.inf

    for p in sorted(ports, key=lambda q: q.name):
        assert p.near is not None
        # every candidate scored (size agreement, kind, distance): the best size wins, a hole
        # before a capped face when the two agree as well (the stage put the sticker on a hole),
        # except on a body declared to be the fluid itself, whose openings are its faces and whose
        # bores are only where a centre body passes through
        cands = []
        for k, o in enumerate(holes):
            if k in taken:
                continue
            d = float(np.linalg.norm(o.centroid - p.near))
            size = max(o.diameter, p.reach)
            if d > HOLE_REACH * size:
                continue
            if k not in rings:
                rings[k] = _inner_loop(o, list(inner_pool), mesh)
            inner = rings[k]
            measures = [o.area] if inner is None else [o.area - inner.area, inner.area, o.area]
            err = size_err(measures, p.area)
            if err == math.inf:
                continue
            pref = 2 if (fluid_body and o.kind == "bore") else 0
            # distances within a twentieth of the mouth are one place (a ring's two loops share a
            # centre to rounding): there the wider mouth wins, the ring before its centre body
            cands.append((round(err / 0.05), pref, round(d / max(size, 1e-30) / 0.05), -o.area, k))
        cap = _cap_at(mesh, p, used=[o.faces for o in out.values() if o.faces is not None])
        if cap is not None:
            err = size_err([cap.area, cap.area + _holes_area(mesh, cap.faces)], p.area)
            if err < math.inf:
                d = float(np.linalg.norm(cap.centroid - p.near))
                cands.append((round(err / 0.05), 1, round(d / max(cap.size, 1e-30) / 0.05), -cap.area, -1))
        if not cands:
            raise BindError(
                f"opening '{p.name}' at ({p.near[0] * 1000:.0f}, {p.near[1] * 1000:.0f}, "
                f"{p.near[2] * 1000:.0f}) mm matches no open end, bore mouth or capped face of the "
                f"surface. Measured openings:\n{_listing(rims, bores)}\n"
                "Move the opening onto one of them on the picture, or add it there.")
        best = min(cands)[-1]
        if best < 0:
            assert cap is not None
            cap.name, cap.role = p.name, p.role
            out[p.name] = cap
            continue
        taken.add(best)
        o = holes[best]
        inner = rings.get(best)
        if inner is not None:
            # the centre body's own edge is part of this opening, never an opening of its own
            taken.update(k for k, h in enumerate(holes) if h.key() == inner.key())
        area = o.area - inner.area if inner is not None else o.area
        out[p.name] = _Opening(kind=o.kind, centroid=o.centroid, normal=o.normal, area=area,
                               loop=o.loop, edges=o.edges, planarity=o.planarity, name=p.name,
                               role=p.role, inner=inner)
    return out


def _cap_at(mesh, p: _Port, used) -> _Opening | None:
    """The face region capping a mouth of a closed fluid surface at the declared location: the
    smooth patch around the nearest face, bounded by creases - or, where no crease bounds it (a
    rounded end), the faces facing the patch's way within the opening's radius of its axis."""
    d_decl = p.reach
    if p.near is None:
        return None
    dist = _point_face_distance(mesh.verts, mesh.faces, p.near)
    f0 = int(np.argmin(dist))
    reach = CAP_REACH * d_decl if d_decl else 0.02 * mesh.diag
    taken = np.concatenate(used) if used else np.zeros(0, dtype=np.int64)
    total = float(mesh.area.sum())
    flat = _flat_labels(mesh)
    if dist[f0] > max(reach, 1e-3 * mesh.diag):
        # THE CENTRE IS NOT ON THE SURFACE: a ring-shaped cap around a centre body (an annular
        # fluid body's end) has its centre in the body's hole. The flat face region near the
        # centre, within the opening's radius, whose area agrees, is the cap.
        if not d_decl:
            return None
        near = np.flatnonzero(dist <= 0.55 * d_decl)
        best = None
        for lab in np.unique(flat[near]).tolist():
            region = np.flatnonzero(flat == lab)
            if not _cap_ok(mesh, region, p, total, taken):
                continue
            err = abs(math.log(float(mesh.area[region].sum()) / (p.area or 1.0)))
            if best is None or err < best[0]:
                best = (err, region)
        if best is None:
            return None
        region = best[1]
        return _cap_record(mesh, region)
    sharp = mesh.feature | (mesh.count != 2)
    region = _grow(mesh, f0, lambda e, g: not sharp[e])
    ok = _cap_ok(mesh, region, p, total, taken)
    if not ok:
        # the flat face the measuring step itself proposes as a mouth (its "disc"): coplanar faces
        region = np.flatnonzero(flat == flat[f0])
        ok = _cap_ok(mesh, region, p, total, taken)
    if not ok:
        # no crease bounds it: the faces turned the seed face's way, inside the opening's radius
        n0 = _patch_normal(mesh, _grow(mesh, f0, lambda e, g: float(np.linalg.norm(mesh.centre[g] - p.near)) <= 0.25 * max(d_decl, 1e-9)))
        r = 0.5 * d_decl * 1.05 if d_decl else 0.05 * mesh.diag
        cosmax = math.cos(math.radians(45.0))

        def inside(e, g) -> bool:
            if mesh.count[e] != 2:
                return False
            rel = mesh.centre[g] - p.near
            lateral = float(np.linalg.norm(rel - (rel @ n0) * n0))
            return lateral <= r and float(mesh.normal[g] @ n0) >= cosmax
        region = _grow(mesh, f0, inside)
        ok = _cap_ok(mesh, region, p, total, taken)
    if not ok:
        return None
    return _cap_record(mesh, region)


def _cap_record(mesh, region) -> _Opening:
    a = mesh.area[region]
    c = (mesh.centre[region] * a[:, None]).sum(axis=0) / a.sum()
    n = _patch_normal(mesh, region)
    return _Opening(kind="cap", centroid=c, normal=n, area=float(a.sum()), faces=np.asarray(region))


def _flat_labels(mesh) -> np.ndarray:
    """Each face's flat region: faces joined across an edge when their normals agree within 3
    degrees and each one's centre lies on the other's plane (cad/scout_mesh's own test, so a cap
    here is the disc the stage proposed)."""
    cached = getattr(mesh, "_flat", None)
    if cached is not None:
        return cached
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    e = np.flatnonzero((mesh.count == 2) & (mesh.f1 >= 0))
    a, b = mesh.f0[e], mesh.f1[e]
    na, nb = mesh.normal[a], mesh.normal[b]
    gap = mesh.centre[b] - mesh.centre[a]
    tol = 1e-3 * mesh.diag
    ok = ((np.einsum("ij,ij->i", na, nb) >= FLAT_COS)
          & (np.abs(np.einsum("ij,ij->i", na, gap)) <= tol)
          & (np.abs(np.einsum("ij,ij->i", nb, gap)) <= tol))
    nf = len(mesh.faces)
    g = coo_matrix((np.ones(int(ok.sum())), (a[ok], b[ok])), shape=(nf, nf))
    _, lab = connected_components(g, directed=False)
    mesh._flat = lab
    return lab


def _cap_ok(mesh, region, p: _Port, total: float, taken) -> bool:
    if len(region) == 0 or np.isin(region, taken).any():
        return False
    area = float(mesh.area[region].sum())
    if area >= 0.5 * total:
        return False                             # the whole body, not a mouth
    if p.area and not any(1.0 / MATCH_RATIO <= a / p.area <= MATCH_RATIO
                          for a in (area, area + _holes_area(mesh, region))):
        return False
    a = mesh.area[region]
    coherent = float(np.linalg.norm((mesh.normal[region] * a[:, None]).sum(axis=0))) / max(float(a.sum()), 1e-300)
    return coherent >= 0.6                       # a mouth faces one way, more or less


def _holes_area(mesh, region) -> float:
    """The area a face region's inner outlines enclose - the centre body's hole in a ring-shaped
    cap - so a mouth declared by its outer size is still recognised. 0 for a plain disc."""
    from meshpipeline.cad.lids import frame
    f = mesh.faces[region]
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    uniq, cnt = np.unique(e, axis=0, return_counts=True)
    areas = []
    for lp in _edge_loops(uniq[cnt == 1]):
        try:
            areas.append(frame(mesh.verts[lp])[2])
        except Exception:  # noqa: BLE001, S112 - a loop with no area adds none
            continue
    if len(areas) < 2:
        return 0.0
    areas.sort(reverse=True)
    return float(sum(areas[1:]))


def _edge_loops(edge_list: np.ndarray) -> list[list[int]]:
    """Closed vertex loops walked along the given edges; a vertex met by more than two edges is
    taken through whichever unvisited edge comes first. (cad/scout_mesh's former _loops, kept here
    when the openings work moved the scout's own flat-face reading into cad/open_ends.)"""
    if len(edge_list) == 0:
        return []
    adj: dict[int, list[int]] = {}
    for a, b in edge_list.tolist():
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    used: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    for start in adj:
        for nxt in adj[start]:
            if (start, nxt) in used or (nxt, start) in used:
                continue
            loop = [start]
            prev, cur = start, nxt
            used.add((prev, cur))
            while cur != start and len(loop) < 200_000:
                loop.append(cur)
                choices = [v for v in adj.get(cur, []) if (cur, v) not in used and (v, cur) not in used]
                if not choices:
                    break
                prev, cur = cur, choices[0]
                used.add((prev, cur))
            if cur == start and len(loop) >= 3:
                loops.append(loop)
    return loops


def _patch_normal(mesh, region) -> np.ndarray:
    a = mesh.area[region]
    n = (mesh.normal[region] * a[:, None]).sum(axis=0)
    L = float(np.linalg.norm(n))
    return n / L if L > 0 else np.array([0.0, 0.0, 1.0])


def _grow(mesh, f0: int, may_cross) -> np.ndarray:
    """Faces reached from f0 across edges `may_cross(edge, next_face)` allows."""
    from collections import deque
    corners = mesh.edge_of.reshape(-1, 3)
    seen = {f0}
    todo = deque([f0])
    while todo and len(seen) < 2_000_000:
        f = todo.popleft()
        for e in corners[f]:
            g = int(mesh.f1[e]) if int(mesh.f0[e]) == f else int(mesh.f0[e])
            if g < 0 or g in seen or not may_cross(e, g):
                continue
            seen.add(g)
            todo.append(g)
    return np.asarray(sorted(seen), dtype=np.int64)


# ------------------------------------------------------------------------- the fluid's side ----
def _loop_faces(mesh, o: _Opening) -> np.ndarray:
    e = o.all_edges()
    f = np.r_[mesh.f0[e], mesh.f1[e]]
    return f[f >= 0]


def _fluid_side(mesh, lidded: list[_Opening], caps: list[_Opening]):
    """Which pieces of the surface bound the fluid. The surface is cut along every lidded loop;
    at each loop edge the face the fluid side holds is the one running back from the loop, away
    from where the fluid leaves (a thick wall's bore, never its end face). A piece the fluid holds
    at one loop and the outside holds at another means the fluid reaches the outside: a conflict.
    A closed fluid surface with capped mouths has no loops, and its pieces with a cap are kept.
    Pieces no loop touches are kept when they lie inside the kept boundary (a centre body)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    nf = len(mesh.faces)
    cut = np.zeros(len(mesh.count), dtype=bool)
    for o in lidded:
        cut[o.all_edges()] = True
    join = (mesh.count >= 2) & ~cut & (mesh.f1 >= 0)
    g = coo_matrix((np.ones(int(join.sum())), (mesh.f0[join], mesh.f1[join])), shape=(nf, nf))
    # an edge shared by more than two faces joins them all: only the first pair is in f0/f1,
    # so join the rest through the edge table of each face corner
    many = np.flatnonzero((mesh.count > 2) & ~cut)
    if len(many):
        corner_edge = mesh.edge_of
        owner = np.repeat(np.arange(nf), 3)
        sel = np.isin(corner_edge, many)
        rows = owner[sel]
        cols = mesh.f0[corner_edge[sel]]
        g = g + coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(nf, nf))
    _, piece = connected_components(g, directed=False)
    fluid = np.zeros(piece.max() + 1)
    outer = np.zeros(piece.max() + 1)
    for o in lidded:
        if o.role == "wall":
            continue                      # a sealed loop cuts, but only a declared opening votes
        P = mesh.verts
        edges = o.all_edges()
        a, b = mesh.ea[edges], mesh.eb[edges]
        mid = (P[a] + P[b]) / 2.0
        f0, f1 = mesh.f0[edges], mesh.f1[edges]
        for k in range(len(edges)):
            fs = [int(f) for f in (f0[k], f1[k]) if f >= 0]
            if len(fs) == 1:
                fluid[piece[fs[0]]] += 1
                continue
            back = [float((mesh.centre[f] - mid[k]) @ (-o.normal)) /
                    max(float(np.linalg.norm(mesh.centre[f] - mid[k])), 1e-30) for f in fs]
            i = int(np.argmax(back))
            fluid[piece[fs[i]]] += 1
            for j, f in enumerate(fs):
                if j != i:
                    outer[piece[f]] += 1
    chosen = set(np.flatnonzero(fluid > outer).tolist())
    for o in caps:
        chosen.update(np.unique(piece[o.faces]).tolist())
    conflict = any(outer[p] > 0.1 * fluid[p] for p in chosen if fluid[p] > 0)
    if not chosen:
        return piece, np.zeros(0, dtype=np.int64), True
    return piece, np.asarray(sorted(chosen), dtype=np.int64), conflict


def _assemble(mesh, pieces, chosen, lidded, caps, wall: str, lids: dict):
    """The kept pieces of the surface and the lids on the loops they touch, as one face list:
    (points, faces, patch per face, which faces are lids, what each lid is, what was sealed)."""
    keep = np.isin(pieces, chosen)
    labels = np.full(len(mesh.faces), wall, dtype=object)
    for o in caps:
        labels[o.faces] = o.name
    out_v = [mesh.verts]
    out_f = [mesh.faces[keep]]
    out_l = [labels[keep]]
    out_lid = [np.zeros(int(keep.sum()), dtype=bool)]
    lid_report, sealed = [], []
    n_pts = len(mesh.verts)
    for o in lidded:
        # a sealing lid belongs on a loop the kept surface touches; a port's lid always
        if o.role == "wall" and not keep[_loop_faces(mesh, o)].any():
            continue
        extra, lt, info = lids[id(o)]
        loop = o.loop if o.inner is None else np.r_[o.loop, o.inner.loop]
        m = len(loop)
        if len(extra):
            lt = np.where(lt >= m, lt - m + n_pts, loop[np.minimum(lt, m - 1)])
            out_v.append(extra)
            n_pts += len(extra)
        else:
            lt = loop[lt]
        out_f.append(lt)
        name = o.name if o.role != "wall" else wall
        out_l.append(np.full(len(lt), name, dtype=object))
        out_lid.append(np.ones(len(lt), dtype=bool))
        lid_report.append({"name": name, "kind": o.kind, "triangles": int(len(lt)), **info})
        if o.role == "wall":
            sealed.append({"kind": o.kind, "area": round(float(o.area), 10),
                           "centroid": [round(float(v), 6) for v in o.centroid]})
    V = np.vstack(out_v)
    F = np.vstack(out_f).astype(np.int64)
    L = np.concatenate(out_l)
    is_lid = np.concatenate(out_lid)
    V, F, L, is_lid = _compact(V, F, L, is_lid)
    return V, F, L, is_lid, lid_report, sealed


def _lid_for(mesh, o: _Opening):
    """(extra points, triangles over o.loop's points then the extras, facts). A ring opening's
    triangles run over its outer loop's points and then its inner loop's, as one loop list (the
    caller's index mapping reads `o.loop` extended by `o.inner.loop`)."""
    from meshpipeline.cad.lids import LidError, lid, ring

    P = mesh.verts[o.loop]
    try:
        if o.inner is not None:
            tris = ring(P, mesh.verts[o.inner.loop])
            return np.zeros((0, 3)), tris, {"method": "ring", "planarity": round(o.planarity, 5)}
        extra, tris, info = lid(P)
    except LidError as exc:
        raise InternalSurfaceError(f"the {o.kind} at ({o.centroid[0]:.4f}, {o.centroid[1]:.4f}, "
                                   f"{o.centroid[2]:.4f}) m could not be closed: {exc}") from exc
    return extra, np.asarray(tris, dtype=np.int64), info


def _compact(V, F, L, is_lid):
    used = np.unique(F)
    remap = np.full(len(V), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    F2 = remap[F]
    ok = (F2[:, 0] != F2[:, 1]) & (F2[:, 1] != F2[:, 2]) & (F2[:, 0] != F2[:, 2])
    return V[used], F2[ok], L[ok], is_lid[ok]


# --------------------------------------------------------------------------- the winding ----
def _edges(F: np.ndarray):
    """Undirected edges of a face list: (unique edge rows, per-corner edge id, use count)."""
    a = F.reshape(-1)
    b = F[:, [1, 2, 0]].reshape(-1)
    key = np.c_[np.minimum(a, b), np.maximum(a, b)]
    uniq, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    return uniq, np.asarray(inv).reshape(-1), cnt


def _orient(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    """Every face turned to agree with its neighbours across each two-face edge, then every
    closed shell turned so the fluid is inside: an outermost shell faces out, a shell standing in
    the fluid (a centre body) faces into itself - the region the boundary encloses is the fluid."""
    from collections import deque

    nf = len(F)
    _, eid, cnt = _edges(F)
    a = F.reshape(-1)
    b = F[:, [1, 2, 0]].reshape(-1)
    fwd = a < b
    face = np.repeat(np.arange(nf), 3)
    order = np.argsort(eid, kind="stable")
    es = eid[order]
    starts = np.r_[0, np.flatnonzero(np.diff(es)) + 1]
    nbr: list[list[tuple[int, bool]]] = [[] for _ in range(nf)]
    for s in starts.tolist():
        e = es[s]
        if cnt[e] != 2:
            continue
        h0, h1 = order[s], order[s + 1]
        same = bool(fwd[h0] == fwd[h1])            # both run the edge one way: one is turned
        f0, f1 = int(face[h0]), int(face[h1])
        nbr[f0].append((f1, same))
        nbr[f1].append((f0, same))
    flip = np.zeros(nf, dtype=bool)
    shell = np.full(nf, -1, dtype=np.int64)
    n_shell = 0
    for s0 in range(nf):
        if shell[s0] >= 0:
            continue
        shell[s0] = n_shell
        todo = deque([s0])
        while todo:
            x = todo.popleft()
            for y, same in nbr[x]:
                if shell[y] < 0:
                    shell[y] = n_shell
                    flip[y] = flip[x] ^ same
                    todo.append(y)
        n_shell += 1
    F = F.copy()
    F[flip] = F[flip][:, [0, 2, 1]]
    T = V[F]
    ref = V.mean(axis=0)
    vol = np.einsum("ij,ij->i", T[:, 0] - ref, np.cross(T[:, 1] - ref, T[:, 2] - ref)) / 6.0
    shell_vol = np.bincount(shell, weights=vol, minlength=n_shell)
    # nesting: a shell inside an odd number of others bounds a body standing in the fluid
    probes = np.array([T[np.flatnonzero(shell == s)[0]].mean(axis=0) for s in range(n_shell)])
    depth = np.zeros(n_shell, dtype=int)
    if n_shell > 1:
        for s in range(n_shell):
            for t in range(n_shell):
                if t == s:
                    continue
                w = _winding(T[shell == t], probes[s][None, :])[0]
                if abs(w) >= 0.5:
                    depth[s] += 1
    want_positive = depth % 2 == 0
    turn = (shell_vol > 0) != want_positive
    F[turn[shell]] = F[turn[shell]][:, [0, 2, 1]]
    return F


def _winding(T: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """The generalised winding number of the triangles T (n x 3 x 3) at each point: 1 inside a
    closed surface wound outward, 0 outside, and still about right where the surface has a small
    gap - which is why it decides inside, not a single ray."""
    out = np.zeros(len(pts))
    for k, p in enumerate(np.asarray(pts, dtype=float)):
        a, b, c = T[:, 0] - p, T[:, 1] - p, T[:, 2] - p
        la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
        num = np.einsum("ij,ij->i", a, np.cross(b, c))
        den = la * lb * lc + np.einsum("ij,ij->i", a, b) * lc + np.einsum("ij,ij->i", b, c) * la \
            + np.einsum("ij,ij->i", c, a) * lb
        out[k] = float(np.sum(2.0 * np.arctan2(num, den))) / (4.0 * math.pi)
    return out


def _ray_parity(T: np.ndarray, p: np.ndarray, dirs: np.ndarray) -> list[int]:
    """How many faces each ray from p crosses: odd for a point inside a closed surface."""
    v0, e1, e2 = T[:, 0], T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
    s = p - v0
    q = np.cross(s, e1)
    out = []
    for d in dirs:
        pv = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, pv)
        ok = np.abs(det) > 1e-30
        inv = np.divide(1.0, det, out=np.zeros_like(det), where=ok)
        u = np.einsum("ij,ij->i", s, pv) * inv
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv
        out.append(int(np.sum(ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0))))
    return out


def _point_face_distance(V: np.ndarray, F: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Exact distance from p to every triangle (Ericson's closest point on a triangle)."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    denom = va + vb + vc
    denom = np.where(np.abs(denom) > 1e-300, denom, 1e-300)
    v = vb / denom
    w = vc / denom
    q = a + ab * v[:, None] + ac * w[:, None]                       # inside the face
    # the regions outside the face, most specific last
    with np.errstate(divide="ignore", invalid="ignore"):
        e_bc = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)
        t = np.where(e_bc, (d4 - d3) / np.where((d4 - d3) + (d5 - d6) != 0, (d4 - d3) + (d5 - d6), 1), 0)
        q = np.where(e_bc[:, None], b + (c - b) * t[:, None], q)
        e_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        t = np.where(e_ac, d2 / np.where(d2 - d6 != 0, d2 - d6, 1), 0)
        q = np.where(e_ac[:, None], a + ac * t[:, None], q)
        e_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        t = np.where(e_ab, d1 / np.where(d1 - d3 != 0, d1 - d3, 1), 0)
        q = np.where(e_ab[:, None], a + ab * t[:, None], q)
    q = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, q)
    q = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, q)
    q = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, q)
    return np.linalg.norm(q - p, axis=1)


# -------------------------------------------------------------------------- the checks ----
def _verify(V, F, L, port_names: list[str], wall: str, *, is_lid: np.ndarray) -> dict:
    """Closed (every edge on exactly two faces), manifold, wound one way across every edge, the
    lids crossing nothing, and every opening one connected patch of its own."""
    uniq, eid, cnt = _edges(F)
    a = F.reshape(-1)
    b = F[:, [1, 2, 0]].reshape(-1)
    fwd = (a < b).astype(int)
    # a consistently wound closed surface runs each edge once each way
    ways = np.bincount(eid, weights=fwd, minlength=len(uniq))
    two = cnt == 2
    inconsistent = int(np.sum(two & (ways != 1)))
    T = V[F]
    ref = V.mean(axis=0)
    vol = float(np.sum(np.einsum("ij,ij->i", T[:, 0] - ref, np.cross(T[:, 1] - ref, T[:, 2] - ref))) / 6.0)
    patches = {}
    for name in [wall, *port_names]:
        sel = np.flatnonzero(L == name)
        patches[name] = {"faces": int(len(sel)), "pieces": _n_pieces(F[sel]) if len(sel) else 0}
    crossings = _lid_crossings(V, F, is_lid)
    one_each = all(patches[n]["faces"] > 0 and patches[n]["pieces"] == 1 for n in port_names)
    return {"open_edges": int(np.sum(cnt == 1)), "nonmanifold_edges": int(np.sum(cnt > 2)),
            "inconsistent_edges": inconsistent, "volume_m3": vol,
            "watertight": bool(np.all(cnt == 2)), "manifold": bool(np.all(cnt <= 2)),
            "lid_crossings": crossings, "patches": patches, "one_patch_per_opening": bool(one_each),
            "faces": int(len(F)), "points": int(len(V))}


def _piece_labels(F: np.ndarray) -> np.ndarray:
    """Each face's edge-connected piece."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    if len(F) == 0:
        return np.zeros(0, dtype=np.int64)
    uniq, eid, cnt = _edges(F)
    face = np.repeat(np.arange(len(F)), 3)
    order = np.argsort(eid, kind="stable")
    es = eid[order]
    same = np.flatnonzero(es[1:] == es[:-1])
    rows, cols = face[order[same]], face[order[same + 1]]
    g = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(F), len(F)))
    return connected_components(g, directed=False)[1]


def _n_pieces(F: np.ndarray) -> int:
    lab = _piece_labels(F)
    return int(lab.max() + 1) if len(lab) else 0


def _pieces(F: np.ndarray) -> list[np.ndarray]:
    """The face ids of each edge-connected piece of F, largest first."""
    lab = _piece_labels(F)
    if not len(lab):
        return []
    out = [np.flatnonzero(lab == k) for k in range(int(lab.max()) + 1)]
    return sorted(out, key=len, reverse=True)


def _lid_crossings(V, F, is_lid: np.ndarray) -> int:
    """How many lid triangles pass through a face that shares no point with them: every face near
    a lid is tested against it (each edge of one through the other, both ways)."""
    lid_ids = np.flatnonzero(is_lid)
    if len(lid_ids) == 0:
        return 0
    from scipy.spatial import cKDTree
    T = V[F]
    cen = T.mean(axis=1)
    rad = np.max(np.linalg.norm(T - cen[:, None, :], axis=2), axis=1)
    tree = cKDTree(cen)
    bad = 0
    rmax = float(rad.max())
    for i in lid_ids.tolist():
        near = np.asarray(tree.query_ball_point(cen[i], rad[i] + rmax), dtype=np.int64)
        near = near[near != i]
        if len(near) == 0:
            continue
        near = near[~np.isin(F[near], F[i]).any(axis=1)]
        near = near[np.linalg.norm(cen[near] - cen[i], axis=1) <= rad[near] + rad[i]]
        if len(near) and _tri_tri_any(T[i], T[near]):
            bad += 1
    return bad


def _tri_tri_any(t: np.ndarray, others: np.ndarray) -> bool:
    """Whether triangle t crosses any of `others`: an edge of one passing through the other."""
    def seg_hits(p0, p1, tri):
        d = p1 - p0
        v0, e1, e2 = tri[:, 0], tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        pv = np.cross(np.broadcast_to(d, e2.shape), e2)
        det = np.einsum("ij,ij->i", e1, pv)
        ok = np.abs(det) > 1e-30
        inv = np.divide(1.0, det, out=np.zeros_like(det), where=ok)
        s = p0 - v0
        u = np.einsum("ij,ij->i", s, pv) * inv
        q = np.cross(s, e1)
        v = np.einsum("ij,j->i", q, d) * inv
        tt = np.einsum("ij,ij->i", e2, q) * inv
        e = 1e-9
        return ok & (u > e) & (v > e) & (u + v < 1 - e) & (tt > e) & (tt < 1 - e)
    hit = np.zeros(len(others), dtype=bool)
    for k in range(3):
        hit |= seg_hits(t[k], t[(k + 1) % 3], others)
    for k in range(3):
        p0, p1 = others[:, k], others[:, (k + 1) % 3]
        d = p1 - p0
        v0, e1, e2 = t[0], t[1] - t[0], t[2] - t[0]
        pv = np.cross(d, e2)
        det = pv @ e1
        ok = np.abs(det) > 1e-30
        inv = np.divide(1.0, det, out=np.zeros_like(det), where=ok)
        s = p0 - v0
        u = np.einsum("ij,ij->i", s, pv) * inv
        q = np.cross(s, e1)
        v = np.einsum("ij,ij->i", q, d) * inv
        tt = (q @ e2) * inv
        e = 1e-9
        hit |= ok & (u > e) & (v > e) & (u + v < 1 - e) & (tt > e) & (tt < 1 - e)
    return bool(hit.any())


# -------------------------------------------------------------------------- the seed ----
def _outward(V, F, L, o: _Opening) -> np.ndarray:
    """The opening's normal from its own patch on the oriented boundary: out of the fluid."""
    sel = F[L == o.name]
    if len(sel) == 0:
        return o.normal
    T = V[sel]
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]).sum(axis=0)
    L2 = float(np.linalg.norm(n))
    return n / L2 if L2 > 0 else o.normal


def _seed(V, F, L, ports: dict[str, _Opening]) -> tuple[np.ndarray, dict]:
    """A point proven inside the fluid: the closed boundary's winding number there is one, and
    rays both ways along three axes cross it an odd number of times. Candidates step in from every
    opening along its inward normal, on its axis and off it; among the proven ones the one
    farthest from any face wins, and no proven point closer than SEED_CLEARANCE of its opening's
    radius to a face is taken. Thin or curved passages are what the off-axis and shallow steps are
    for."""
    T = V[F]
    cand_list: list[np.ndarray] = []
    owners: list[tuple[str, float]] = []
    for name, o in ports.items():
        r = 0.5 * o.diameter
        n = o.normal / (np.linalg.norm(o.normal) or 1.0)
        u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(n, u)
        for depth in (0.5, 0.25, 1.0, 0.1, 2.0):
            for lat, ang in [(0.0, 0.0)] + [(f, a) for f in (0.5, 0.8) for a in np.linspace(0, 2 * math.pi, 6, endpoint=False)]:
                p = o.centroid - n * depth * r + lat * r * (math.cos(ang) * u + math.sin(ang) * w)
                cand_list.append(p)
                owners.append((name, r))
        # ...and from points spread over the opening's own patch: a ring's centre is in its
        # centre body, and a long thin slot's is close to its sides - its faces are in the flow
        patch = np.flatnonzero(L == name)
        if len(patch):
            a = np.linalg.norm(np.cross(T[patch, 1] - T[patch, 0], T[patch, 2] - T[patch, 0]), axis=1)
            pick = patch[np.argsort(-a)[:12]]
            for f in pick.tolist():
                c = T[f].mean(axis=0)
                for depth in (0.1, 0.25, 0.5):
                    cand_list.append(c - n * depth * r)
                    owners.append((name, r))
    cands = np.round(np.asarray(cand_list), 7)
    wn = _winding(T, cands)
    inside = np.flatnonzero(np.abs(wn - 1.0) < 0.1)
    best: tuple[int, float, float] | None = None
    for i in inside.tolist():
        clear = float(_point_face_distance(V, F, cands[i]).min())
        r = owners[i][1]
        if clear < SEED_CLEARANCE * r:
            continue
        if best is None or clear / r > best[1]:
            best = (i, clear / r, clear)
        if clear >= 0.4 * r:
            break
    if best is None:
        # the boundary holds no point near any opening: try the middle of the fluid's box on a grid
        lo, hi = V.min(axis=0), V.max(axis=0)
        g = np.stack(np.meshgrid(*[np.linspace(lo[k], hi[k], 9)[1:-1] for k in range(3)], indexing="ij"), -1).reshape(-1, 3)
        wg = _winding(T, g)
        ok = np.flatnonzero(np.abs(wg - 1.0) < 0.1)
        scored = []
        for i in ok.tolist():
            scored.append((float(_point_face_distance(V, F, g[i]).min()), i))
        if scored:
            clear_g, i = max(scored)
            cands = np.vstack([cands, g[i][None, :]])
            owners.append(("box", clear_g))
            best = (len(cands) - 1, 1.0, clear_g)
    if best is None:
        raise InternalSurfaceError(
            "no point could be proven inside the fluid: the surface with its openings closed does not "
            "enclose a region (is the surface the fluid's boundary, and are the openings on it?)")
    seed = np.round(cands[best[0]], 7)
    axes = np.eye(3)
    dirs = np.vstack([axes, -axes]) + 1e-3 * np.array([[0.31, 0.17, 0.11]])
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    parity = _ray_parity(T, seed, dirs)
    odd = sum(1 for k in parity if k % 2 == 1)
    w_seed = float(_winding(T, seed[None, :])[0])
    if odd < 4:
        raise InternalSurfaceError(
            f"the point chosen inside the fluid is not inside on most rays ({odd} of 6 cross the "
            "boundary an odd number of times) - the staged boundary is not closed around the flow")
    return seed, {"point": seed.tolist(), "winding": round(w_seed, 4), "odd_rays": odd,
                  "clearance_m": round(float(best[2]), 7), "from": owners[best[0]][0]}


__all__ = ["STAGING_GATE", "InternalSurfaceError", "StagedInternal", "is_cad",
           "stage_internal", "stage_internal_surface", "stage_triangles", "staging_failure",
           "write_staged"]
