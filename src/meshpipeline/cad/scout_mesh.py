# Responsibility: Read a triangle file - STL, OBJ, VTP - and propose what the CAD scout proposes
# for a STEP file: the openings, what kind of body it is, which way the fluid goes, a point inside
# the flow. Triangles carry no faces to measure, so the openings are the holes cad/open_ends finds
# (the one definition the stage's "Add an opening" shares) and the flat discs of a fluid body:
# close, not exact, and said to be.
# Boundaries: numpy over triangles. No OpenCASCADE, no model, no storage; the decisions mirror
# cad/scout so the two roads meet the same stage.
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from meshpipeline.cad.scout import (
    BOX_LIKE_FILL,
    BOX_LIKE_FLAT_SHARE,
    FINNED_FLAT_SHARE,
    MAX_MOUTHS,
    MAX_OPENINGS,
    MIN_MOUTH_OF_THICKNESS,
    MIN_OPENING_FRACTION,
    MIN_RING_BORE_FRACTION,
    MOUTH_MAX_ASPECT,
    THIN_FLAT,
    Opening,
    ScoutResult,
    _bladed,
    _name_openings,
    _wall_pairs,
    length_decimals,
    measured_faces,
)

#: This many separate pieces read as a scene - buildings, a city block - not a part with mouths.
MANY_BODIES = 6
#: ...counting only the pieces that hold at least this share of the skin's area.
MIN_PIECE_SHARE = 0.005
#: A flat region smaller than this share of the part's box is trim, not a mouth.
MIN_REGION_SHARE = 1e-4
#: A LUMEN (cad/lumen) - the fluid of a tube network - fills at most this much of its box (a
#: pulmonary tree 3-5%, a coronary tree under 1%; a car 50-95%), has at least this share of its lids
#: closing tubes of their own size, and, read from its lids alone, points them this many ways.
LUMEN_FILL = 0.2
LUMEN_CAPPED_SHARE = 0.75
LUMEN_DIRECTIONS = 3
#: ...and proposes every end it has, up to this many (a pulmonary tree has 30).
MAX_LUMEN_ENDS = 64
#: How sure a rounded tube end reads as an opening (a lid's is its rim's and its air's).
END_CONFIDENCE = 0.6


def read_triangles(path: Path) -> np.ndarray:
    """The file's triangles as an (n, 3, 3) array in the file's own unit."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".stl":
        from meshpipeline.cad.stl_io import read_stl_triangles
        tris = read_stl_triangles(path)
        return np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if suffix == ".obj":
        return _read_obj(path)
    if suffix == ".vtp":
        import pyvista as pv
        mesh = pv.read(str(path)).triangulate()
        faces = np.asarray(mesh.faces).reshape(-1, 4)[:, 1:]
        return np.asarray(mesh.points, dtype=float)[faces]
    raise ValueError(f"not a triangle file: {suffix}")


def _read_obj(path: Path) -> np.ndarray:
    verts: list[list[float]] = []
    tris: list[tuple[int, int, int]] = []
    with path.open() as fh:
        for line in fh:
            if line.startswith("v "):
                parts = line.split()
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    i = int(tok.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):        # a polygon fans into triangles
                    tris.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not tris:
        raise ValueError("the OBJ file holds no triangles")
    v = np.asarray(verts, dtype=float)
    return v[np.asarray(tris, dtype=int)]


def write_mesh_skin(path: Path, dest: Path, *, scale_to_m: float) -> Path:
    """The same triangles, in metres, as the binary STL the pictures and the stage draw."""
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = read_triangles(path) * float(scale_to_m)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(dest, [(t[0].tolist(), t[1].tolist(), t[2].tolist()) for t in tris])
    return dest


# ------------------------------------------------------------------------------ the scout ----
def scout_mesh(path: Path, *, scale_to_m: float, mapped: bool = True) -> ScoutResult:
    """`mapped=False` reads the openings by the hole finder's shape rules alone, with no
    inside-space map (the reading the check falls back to when the map's openings cannot close
    the fluid in and the shape rules' can - geometry_check.keep_a_stageable_proposal)."""
    tris = read_triangles(path) * float(scale_to_m)
    from meshpipeline.cad.open_ends import find_caps, find_holes, inside_map, surface

    # the skin as one surface, read once for the holes and the lids: an assembly's faces where its
    # solids touch are inside the part
    mesh = surface(tris)
    if mesh is None:
        raise ValueError("the file holds too few triangles to read")
    verts, faces = mesh.verts, mesh.faces
    bbox_min, bbox_max = verts.min(axis=0), verts.max(axis=0)
    diag = float(np.linalg.norm(bbox_max - bbox_min)) or 1.0

    edges, edge_faces, counts = _edges(faces)
    interior = counts == 2
    components = _components(len(faces), edge_faces[interior])
    # a fragment of a scan (a few triangles a segmentation left beside a vessel tree) is no body of
    # its own: only pieces holding a real share of the skin count
    piece_area = np.bincount(components, weights=mesh.area, minlength=int(components.max()) + 1) \
        if len(components) else np.zeros(1)
    n_components = max(int(np.sum(piece_area >= MIN_PIECE_SHARE * float(piece_area.sum()))), 1)

    notes: list[str] = []
    candidates: list[Opening] = []

    # THE HOLES, by the one definition the stage's "Add an opening" uses too (cad/open_ends): the
    # mouths of the part's inside-space map (cad/inside_space, INS-2) - a thin wall's open rim (a
    # "rim"), or the mouth of a thick wall's bore at its cut end (a "ring", the scout's word for
    # an end face whose hole is the opening) - flat or not, square to the axis or not, wherever
    # the part sits. A mouth of a side pocket (a bolt hole beside the bore) is kept for the stage
    # and proposed as not an opening.
    holes = find_holes(mesh=mesh, mapped=mapped)
    imap = inside_map(mesh) if mapped else None
    for h in holes:
        candidates.append(_opening(-1, "rim" if h.kind == "rim" else "ring", h.centroid, h.normal, h.area, h.wh,
                                   bbox_min, bbox_max, diag))
        candidates[-1].confidence = h.confidence
        # an annular mouth: what stands in it (a centre rod or hub), so the flow area is the ring
        candidates[-1].filled = h.filled
        candidates[-1].side = not h.passage  # type: ignore[attr-defined]

    # FLAT FACES, each measured as a lid might be (cad/open_ends, WHAT A CAP IS). A fluid body's
    # mouths are lids: flat, with a sharp rim all the way round and open air outside - wherever
    # they sit, a side branch's end as much as the part's far end. A coarse tessellation's flat
    # facets are not lids, and a face with a real hole in it is the holes' business above. Every
    # flat face big enough to click on stays for "add an opening"; only the lids are proposed.
    sx, sy, sz = (float(v) for v in (bbox_max - bbox_min))
    box_skin = 2.0 * (sx * sy + sy * sz + sz * sx) or 1.0
    caps = find_caps(mesh=mesh, min_area=MIN_REGION_SHARE * box_skin)
    lids: set[int] = set()
    tube_lids: set[int] = set()       # lids that close a tube of their own size (a branch's end)
    ground_area = 0.0
    # how much of the part is flat faces as its CAD model had them (Cap.face_of_part). A coarse
    # tessellation's facets of a curved wall - an elbow's torus drawn in flat quads - are flat
    # too, but no bigger than the facets round them and meeting them as gently as the wall
    # curves; counted as a box's sides they turned every coarse elbow into a "solid body".
    drawn_flat = 0.0
    for cap in caps:
        if cap.area < MIN_REGION_SHARE * box_skin and not cap.likely:
            continue
        if min(cap.wh) < 0.05 * max(cap.wh):
            continue          # a facet strip of a curved skin, not a flat face of the part
        if cap.normal[2] < -0.9 and abs(cap.centroid[2] - bbox_min[2]) < 0.01 * diag:
            ground_area += cap.area
        if cap.inner >= MIN_RING_BORE_FRACTION * (cap.area + cap.inner):
            continue          # an end face around a hole: the hole, if it is one, is found above
        candidates.append(_opening(cap.face, "disc", cap.centroid, cap.normal, cap.area, cap.wh,
                                   bbox_min, bbox_max, diag))
        candidates[-1].confidence = cap.confidence
        if cap.face_of_part:
            drawn_flat += cap.area
        if cap.likely:
            lids.add(id(candidates[-1]))
            if cap.tube_end:
                tube_lids.add(id(candidates[-1]))

    measured = measured_faces(candidates, length_decimals(bbox_max - bbox_min))
    side_mouths = [c for c in candidates if getattr(c, "side", False)]
    candidates = [c for c in candidates if not getattr(c, "side", False)]
    rings = [c for c in candidates if c.kind == "ring"]
    # a passage's mouth is no sliver: a lid far longer than it is wide is a wing's tip or a plate's
    # edge, unless its narrow side is a real size against the part (a wide flat duct's mouth) -
    # cad/scout's rule for a mouth's shape
    discs = [c for c in candidates if c.kind == "disc" and id(c) in lids
             and (max(c.wh) <= MOUTH_MAX_ASPECT * max(min(c.wh), 1e-12) or min(c.wh) >= THIN_FLAT * diag)]
    # A DUCT'S SIDE WALLS are lids by every local measure - flat, cornered, open air outside - but
    # each has a partner of its size facing it across the duct (cad/scout reads them the same way);
    # a short fat passage whose two ends face each other is the exception, its ends all it has
    fill = _fill(verts, faces, bbox_min, bbox_max)
    walls = _wall_pairs(discs)
    if discs and all(id(o) in walls for o in discs) and fill >= BOX_LIKE_FILL:
        walls = set()
    discs = [o for o in discs if id(o) not in walls]
    rim_openings = [c for c in candidates if c.kind == "rim"]
    size = bbox_max - bbox_min
    footprint = float(size[0] * size[1]) or 1.0
    grounded = ground_area >= 0.3 * footprint or _most_touch_ground(verts, faces, components, bbox_min, diag)

    # `pool` is the openings the part has IF THE FLUID FLOWS THROUGH IT, whatever the reading: a body
    # read as one the fluid flows around keeps them too, so a user (or the picture naming) who says
    # it is a passage after all has openings to confirm instead of none
    lumen = None
    if n_components >= MANY_BODIES:
        body_kind, input_kind, flow, conf = "scene", "solid-body", "external", 0.7
        pool = rings + rim_openings + discs
        notes.append(f"{n_components} separate pieces: this reads as a scene the fluid flows around, not a part with openings")
    elif len(rings) >= 2:
        body_kind, input_kind, flow, pool, conf = "pipe_wall", "body-surface", "internal", rings, 0.75
    elif rim_openings and (len(rim_openings) >= 2 or rings):
        body_kind, input_kind, flow, pool, conf = "surface", "body-surface", "internal", rim_openings + rings, 0.55
        notes.append("the mesh is open; its rims are read as the openings")
    elif rim_openings:
        body_kind, input_kind, flow, pool, conf = "surface", "solid-body", "external", rim_openings, 0.45
        notes.append("the mesh is open on one side only, so it reads as a body in a flow; say if it is a passage")
        # AN OPEN LUMEN WITH ONE MOUTH (an airway tree cut at the trachea, its branches closed): its
        # other openings are its tubes' closed ends (cad/lumen), read on the skin with its open end
        # lidded - so "the fluid flows through it" has somewhere to leave by (INS-2)
        ends = _open_lumen_ends(mesh, rim_openings, bbox_min, bbox_max, diag)
        if ends:
            pool = rim_openings + ends
            notes.append(f"it reads as the fluid inside a network of tubes open at one end: its {len(ends)} closed "
                         "tube ends are kept as the other openings, should the fluid flow through it")
    else:
        # as cad/scout reads a closed solid: a body whose flat faces cover much of its box AND which
        # fills it is a body (the Ahmed body), a bent or branched passage fills far less of its box
        # however flat its walls (a square-section elbow); same-size flats fanning round an axis are
        # blade tips, and more than a dozen lids is a machined part (a connector's pins, a heat
        # sink's fins), not a passage's mouths
        flat_share = drawn_flat / box_skin
        box_like = (flat_share >= BOX_LIKE_FLAT_SHARE and fill >= BOX_LIKE_FILL) or flat_share >= FINNED_FLAT_SHARE
        bladed = _bladed(discs)
        machined = bladed or len(discs) > MAX_MOUTHS
        # a passage's widest mouth is a real share of the part's thickness; a nacelle's 8 mm tail
        # flat on a 200 mm body is no mouth (its small branches may be: only the widest is held to it)
        thinnest = max(min(sx, sy, sz), 1e-12)
        mouthed = bool(discs) and max(o.equivalent_diameter for o in discs) >= MIN_MOUTH_OF_THICKNESS * thinnest
        if len(discs) >= 2 and mouthed and not box_like and not machined:
            body_kind, input_kind, flow, pool, conf = "single_solid", "fluid-domain", "internal", discs, 0.6
        elif not box_like and (lumen := _lumen(mesh, discs, tube_lids, fill, bbox_min, bbox_max, diag))[0]:
            # A LUMEN: the fluid of a tube network, its ends closed by lids or by rounded caps - an
            # artery tree's two dozen outlets are no machined part, and its 2 mm branches are mouths
            # however big the tree's box
            body_kind, input_kind, flow, conf = "single_solid", "fluid-domain", "internal", 0.65
            pool = lumen[1]
            notes.append(f"it reads as the fluid inside {lumen[2]}, so its {len(pool)} tube ends are proposed as the openings")
        else:
            body_kind, input_kind, flow, conf = "single_solid", "solid-body", "external", 0.6
            # its lids, and the tube ends no lid closes (a tube with one flat and one rounded end has both)
            pool = discs + (lumen[1] if lumen is not None else [])
            if len(discs) >= 2 and box_like:
                notes.append(f"flat faces cover {100 * flat_share:.0f}% of the part's box and the part fills "
                             f"{100 * fill:.0f}% of it, so it reads as a solid body in a flow, not a fluid passage")
            elif len(discs) > MAX_MOUTHS:
                notes.append(f"{len(discs)} flat ends are more than a passage has mouths, so it reads as a machined "
                             "body in a flow, not a fluid passage")
            elif bladed:
                notes.append("its flat ends are alike and fan out round an axis like blade tips, so it reads as a "
                             "body in a flow, not a fluid passage")
            elif len(discs) >= 2:
                notes.append("its flat ends are all small against the part's thickness, so it reads as a body in "
                             "a flow, not a fluid passage")
    notes.append("measured from triangles: positions and sizes are close, not exact")

    pool = sorted(pool, key=lambda o: o.area, reverse=True)
    if pool:
        largest = pool[0].area
        # a hole is an opening however small (an aorta's 3.7 mm branch beside its 27.7 mm root),
        # and so is a lid (its 1.6 mm side branch's) and a tube's end; any other flat face has to be
        # a real share of the largest to be one
        pool = [o for o in pool if o.kind in ("ring", "rim", "end") or id(o) in lids
                or o.area >= MIN_OPENING_FRACTION * largest]
    # a tube network has as many openings as it has ends (a pulmonary tree has 30): every one is
    # proposed, up to MAX_LUMEN_ENDS; anything else past MAX_OPENINGS is a perforated face
    most = MAX_LUMEN_ENDS if lumen is not None and lumen[0] and flow == "internal" else MAX_OPENINGS
    # ...and so is a set of open ends: every rim is a hole in the skin the fluid would leave by, and
    # one left off the proposal is sealed shut without a word - a sixteen-tube bundle's 32 ends were
    # cut to 16, and half its tubes were meshed with one end and no way through
    if len(pool) > most and all(o.kind in ("ring", "rim", "end") for o in pool[most:]):
        most = max(most, min(len(pool), MAX_LUMEN_ENDS))
    if len(pool) > most:
        notes.append(f"{len(pool)} candidate openings found; only the {most} largest are proposed")
        pool = pool[:most]
    openings = _name_openings(pool)
    # the mouths of side pockets go last, as not openings: on the stage to click, never a port
    for k, o in enumerate(sorted(side_mouths, key=lambda o: o.area, reverse=True)[:max(0, MAX_LUMEN_ENDS - len(openings))],
                          start=1):
        o.name, o.role = f"side_{k}", "not_an_opening"
        openings.append(o)
    if side_mouths:
        notes.append(f"{len(side_mouths)} opening(s) lead into a separate small pocket (a bolt hole, a blind "
                     "dimple), not into the passage: shown as not an opening; click one to make it one")
    for k, o in enumerate(openings, start=1):
        o.sticker = k                 # its confidence is the finder's: how surely its shape reads as one
    # read as a body in a flow, the openings are kept but not proposed: the stage shows them only once
    # the user says the fluid flows through it after all, and no point inside the flow is sought

    seed = None
    ports = [o for o in openings if o.role in ("inlet", "outlet")]
    if ports and flow == "internal":
        # THE POINT INSIDE THE FLOW (INS-4): the deepest point of the passage - farthest from every
        # wall - read off the map; a body that IS the fluid, the deepest point inside it
        deep = None
        if imap is not None and input_kind != "fluid-domain" and imap.passages():
            deep = imap.passages()[0].deepest
        elif imap is not None and input_kind == "fluid-domain" and imap.enclosed_deepest is not None:
            deep = imap.enclosed_deepest
        if deep is not None:
            seed = (float(deep[0]), float(deep[1]), float(deep[2]))
            notes.append("the point inside the flow is the deepest point of the passage, farthest from every wall")
        else:
            big = max(ports, key=lambda o: o.area)
            inward = -np.asarray(big.normal)
            p = np.asarray(big.centroid) + inward * 0.5 * big.equivalent_diameter
            seed = (float(p[0]), float(p[1]), float(p[2]))
            notes.append("the point inside the flow is placed just inside the largest opening; the mesher checks it")

    result = ScoutResult(
        body_kind=body_kind, input_kind=input_kind, flow=flow, solids=n_components,
        planar_faces=len(caps),
        bbox_min=(float(bbox_min[0]), float(bbox_min[1]), float(bbox_min[2])),
        bbox_max=(float(bbox_max[0]), float(bbox_max[1]), float(bbox_max[2])),
        openings=openings, seed_point=seed,
        confidence={"input_kind": conf,
                    "openings": (sum(o.confidence for o in openings) / len(openings)) if openings else 0.0},
        notes=notes, faces=measured)
    # facts the CAD scout does not know: read by the check when it builds the proposal
    result.extra = {  # type: ignore[attr-defined]
        "components": n_components, "grounded": bool(grounded),
        "flow_axis_guess": "+x" if size[0] >= size[1] else "+y",
        # every hole, for the stage: "Add an opening" snaps to these, so a sticker added by hand
        # lands where the measuring step would have put it
        "holes": [h.as_dict(decimals=length_decimals(bbox_max - bbox_min)) for h in holes],
        # the part's inside-space map, as the stage reads it (INS-1): whether the fluid has
        # somewhere to flow through
        "inside": imap.facts() if imap is not None else {"rule": "INS-1", "mapped": False},
        # THE SURFACE AS DRAWN (INS-7): open edges that edge no opening, and loose pieces - found
        # and said, never healed (the CAD repair stage's work)
        "surface": surface_health(mesh, holes, edges=edges, counts=counts, pieces=n_components),
    }
    return result


def surface_health(mesh, holes, *, edges: np.ndarray | None = None, counts: np.ndarray | None = None,
                   pieces: int | None = None) -> dict:
    """What the drawn surface itself says (INS-7): its open edges that lie on no hole's loop (a hole
    is an opening as the hole finder reads it: a map mouth or a shape-rule rim), and how many
    loose pieces hold a real share of it. Detection only - nothing is closed, joined or turned."""
    if edges is None or counts is None or pieces is None:
        edges, edge_faces, counts = _edges(mesh.faces)
        comp = _components(len(mesh.faces), edge_faces[counts == 2])
        area = np.bincount(comp, weights=mesh.area, minlength=int(comp.max()) + 1) if len(comp) else np.zeros(1)
        pieces = max(int(np.sum(area >= MIN_PIECE_SHARE * float(area.sum()))), 1)
    open_e = edges[counts == 1]
    stray = 0
    if len(open_e):
        on_loop = np.zeros(len(mesh.verts), dtype=bool)
        loops = [np.asarray(h.loop, dtype=float).reshape(-1, 3) for h in holes if h.loop is not None]
        if loops:
            from scipy.spatial import cKDTree
            span = float(np.linalg.norm(mesh.verts.max(axis=0) - mesh.verts.min(axis=0))) or 1.0
            d, i = cKDTree(mesh.verts).query(np.concatenate(loops), distance_upper_bound=1e-6 * span)
            on_loop[i[np.isfinite(d)]] = True
        stray = int(np.sum(~(on_loop[open_e[:, 0]] & on_loop[open_e[:, 1]])))
    return {"rule": "INS-7", "open_edges": int(len(open_e)), "open_edges_not_openings": stray,
            "pieces": int(pieces)}


# ------------------------------------------------------------------------------ the lumen ----
def _lumen(mesh, discs: list[Opening], tube_lids: set[int], fill: float, bbox_min, bbox_max,
           diag: float) -> tuple[bool, list[Opening], str]:
    """Whether a closed body the mouth rules read as a body in a flow is the fluid inside a tube
    network after all (cad/lumen) - (yes, its openings, what it is) - and, either way, the openings it
    has if the fluid flows through it: its lids, and the tube ends no lid closes. It is a lumen when it
    is sparse and EITHER a tree of capped tubes (three or more lids each closing a tube of its own
    size, pointing three or more ways, and most of its lids are such) OR a network of round, thin,
    long tubes that branches or winds (its ends rounded, or closed by lids that close its tubes)."""
    from meshpipeline.cad.lumen import read_lumen

    try:
        reading = read_lumen(mesh)
    except Exception:  # noqa: BLE001 - a reading that fails is no evidence; the mouth rules stand
        reading = None
    ends: list[Opening] = []
    for e in (reading.ends if reading is not None else []):
        # an end a lid closes is that lid's opening
        if any(float(np.linalg.norm(np.asarray(o.centroid) - e.apex)) <= 1.5 * max(e.radius, 0.5 * o.equivalent_diameter)
               for o in discs):
            continue
        r = e.radius
        ends.append(_opening(-1, "end", e.apex, e.normal, math.pi * r * r, (2.0 * r, 2.0 * r), bbox_min, bbox_max, diag))
        ends[-1].confidence = END_CONFIDENCE
    capped = [o for o in discs if id(o) in tube_lids]
    sparse = fill <= LUMEN_FILL
    mostly_tubes = len(capped) >= LUMEN_CAPPED_SHARE * len(discs)
    if sparse and mostly_tubes and len(capped) >= 3 and _directions(capped) >= LUMEN_DIRECTIONS:
        return True, discs + ends, f"a tree of {len(capped)} capped tubes"
    if sparse and mostly_tubes and reading is not None and reading.lumen:
        return True, discs + ends, "a network of thin round tubes"
    return False, ends, ""


def _open_lumen_ends(mesh, rims: list[Opening], bbox_min, bbox_max, diag: float) -> list[Opening]:
    """The closed tube ends of an open tube network (cad/lumen), read on its skin with every open
    rim lidded (cad/lids): each end not at a rim, as an "end" opening - or [] when the closed skin
    reads as no tube network, or cannot be closed."""
    from meshpipeline.cad.lids import LidError, lid
    from meshpipeline.cad.lumen import read_lumen
    from meshpipeline.cad.open_ends import _Mesh

    V, F = mesh.verts, mesh.faces
    extra_v, extra_f = [], []
    n = len(V)
    try:
        for vidx, _eidx in mesh._open_chains():
            P = V[vidx]
            pts, tris, _info = lid(P)
            loop = np.asarray(vidx)
            m = len(loop)
            idx = np.where(tris >= m, tris - m + n, loop[np.minimum(tris, m - 1)])
            extra_f.append(idx)
            if len(pts):
                extra_v.append(pts)
                n += len(pts)
    except (LidError, ValueError, IndexError):
        return []
    if not extra_f:
        return []
    try:
        closed = _Mesh(skin=(np.vstack([V, *extra_v]) if extra_v else V, np.vstack([F, *extra_f])))
        reading = read_lumen(closed)
    except Exception:  # noqa: BLE001 - a reading that fails is no evidence
        return []
    if reading is None or not reading.lumen:
        return []
    out = []
    for e in reading.ends:
        if any(float(np.linalg.norm(np.asarray(o.centroid) - e.apex)) <= 1.5 * max(e.radius, 0.5 * o.equivalent_diameter)
               for o in rims):
            continue                       # the lidded rim's own end
        r = e.radius
        out.append(_opening(-1, "end", e.apex, e.normal, math.pi * r * r, (2.0 * r, 2.0 * r), bbox_min, bbox_max, diag))
        out[-1].confidence = END_CONFIDENCE
    return out[:MAX_LUMEN_ENDS]


def _directions(openings: list[Opening]) -> int:
    """How many ways the openings point: normals within ~25 degrees of each other point one way."""
    axes: list[np.ndarray] = []
    for o in openings:
        n = np.asarray(o.normal, dtype=float)
        if all(float(n @ a) < 0.9 for a in axes):
            axes.append(n)
    return len(axes)


# ---------------------------------------------------------------------------- the geometry ----
def _edges(faces):
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    uniq, inverse, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    # the two faces on each interior edge, in the order they were met
    edge_faces = np.full((len(uniq), 2), -1, dtype=np.int64)
    order = np.argsort(inverse, kind="stable")
    seen = np.zeros(len(uniq), dtype=np.int64)
    for k in order:
        u = inverse[k]
        if seen[u] < 2:
            edge_faces[u, seen[u]] = owner[k]
            seen[u] += 1
    return uniq, edge_faces, counts


def _components(n_faces: int, pairs: np.ndarray) -> np.ndarray:
    parent = np.arange(n_faces)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a < 0 or b < 0:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    roots = np.array([find(i) for i in range(n_faces)])
    _, labels = np.unique(roots, return_inverse=True)
    return labels


def _opening(face_index: int, kind: str, c, n, area: float, wh, bbox_min, bbox_max, diag: float,
             outer_wh=None) -> Opening:
    n = np.asarray(n, dtype=float)
    ts = [((bbox_max[k] if n[k] > 0 else bbox_min[k]) - c[k]) / n[k] for k in range(3) if abs(n[k]) > 1e-6]
    on_extremity = bool(ts) and bool(min(ts) <= 0.03 * diag)     # a plain bool: numpy's is not JSON
    return Opening(face_index=face_index, kind=kind, centroid=(float(c[0]), float(c[1]), float(c[2])),
                   normal=(float(n[0]), float(n[1]), float(n[2])), area=float(area),
                   wh=(float(wh[0]), float(wh[1])), clear_ahead=on_extremity, on_extremity=on_extremity,
                   outer_wh=(float(outer_wh[0]), float(outer_wh[1])) if outer_wh is not None else (float(wh[0]), float(wh[1])))


def _fill(verts, faces, bbox_min, bbox_max) -> float:
    """How much of its box the part fills: the volume its faces enclose (they face out, see
    cad/open_ends), over the box's."""
    a, b, c = (verts[faces[:, k]] - bbox_min for k in range(3))
    vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())) / 6.0
    box = float(np.prod(np.maximum(bbox_max - bbox_min, 1e-12)))
    return vol / box


def _most_touch_ground(verts, faces, components, bbox_min, diag: float) -> bool:
    """A scene stands on the ground when most of its pieces reach the lowest plane."""
    n = int(components.max() + 1) if len(components) else 0
    if n < 2:
        return False
    lows = np.full(n, np.inf)
    zmin = verts[faces].min(axis=1)[:, 2]
    np.minimum.at(lows, components, zmin)
    return bool(np.mean(lows <= bbox_min[2] + 0.01 * diag) >= 0.5)


__all__ = ["read_triangles", "scout_mesh", "write_mesh_skin"]
