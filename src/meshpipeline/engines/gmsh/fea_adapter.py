# Responsibility: gmsh as an FEA engine adapter: mesh the staged CAD solid exactly as the FEA plan (fea_plan.json) says - its base size, its zones as gmsh size fields, its lever settings - and write the deck, the quality record and the plan's rules as measured on the mesh.
# Boundaries: plan in, mesh out. It decides no size and pulls no lever (engines/fea_plan.py and engines/fea_ladder.py do); it runs inside the mesh runner, called by engines/gmsh/driver.main when the workspace holds a plan.
# Collaborates with: engines/fea_size_field.group_zones (the same zone groups the estimate read), engines/fea_checks.py (the rules measured), engines/gmsh/driver.py (deck writer, quality helpers), engines/gmsh/abaqus_deck.py.
"""THE PLAN, MESHED BY GMSH.

Each zone group becomes a Distance field (to its CAD faces, or to the edge two faces share for a
capped corner) and a Threshold on it: the group's size out to its reach, then growing at the
plan's rate to the base size. The smallest of them is the background field, and nothing else
sizes the mesh (no curvature sizing, no sizes carried in from points or boundaries), so the mesh
is the plan's. One thread and a fixed seed, as every gmsh run here: the same plan gives the same
mesh.

Face_k in the plan is the k-th CAD face (cad/fea_measure numbers them in OpenCASCADE's order),
which is gmsh's surface tag k for the staged solid, and Face_k in the deck.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

#: 3D algorithms the plan may name (gmsh's Mesh.Algorithm3D numbers).
ALGORITHMS_3D = {"delaunay": 1, "hxt": 10}


#: THE STEP THE FEA MESHER IS ON (engines/mesher_stage.py reads it for the live line; the runner's
#: watchdog counts a new step as progress). gmsh's own output is the progress inside its steps.
STAGE_FILE = "fea_stage.json"


def note_stage(ws: Path, key: str, label: str) -> None:
    import time
    try:
        (Path(ws) / STAGE_FILE).write_text(json.dumps({"key": key, "label": label, "at": round(time.time(), 1)}))
    except OSError:
        pass


class Ticker:
    """While the adapter's OWN Python steps run (the curved-element check, the quality measures, the
    solver files: plain loops that end, with no gmsh output), the step is re-noted every `every_s`
    seconds so the watchdog sees them working. Never ticks during a gmsh call: there gmsh's own
    output is the only sign of progress."""

    def __init__(self, ws: Path, every_s: float = 30.0):
        import threading
        self.ws, self.every_s = Path(ws), float(every_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._step: tuple[str, str] = ("", "")

    def start(self, key: str, label: str) -> None:
        import threading
        self.stop()
        self._step = (key, label)
        note_stage(self.ws, key, label)
        self._stop = threading.Event()

        def beat() -> None:
            while not self._stop.wait(self.every_s):
                note_stage(self.ws, *self._step)

        self._thread = threading.Thread(target=beat, name="fea-stage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None


#: the mesher's threads when the plan names none: 0 = every core the job has (the curve and surface
#: meshing stay on one thread whatever this says - see mesh())
DEFAULT_THREADS = 0
#: the 3D algorithm when the plan names none
DEFAULT_ALGORITHM_3D = "delaunay"


def mesh_threads(run: dict) -> int:
    """The mesher's thread count: the plan's `threads`, else DEFAULT_THREADS; 0 = every core the job has."""
    raw = (run or {}).get("threads")
    try:
        n = int(DEFAULT_THREADS if raw is None else raw)
    except (TypeError, ValueError):
        n = 1
    if n > 0:
        return n
    # every core THE JOB HAS (its CPU quota), never the host's: Cloud Run's os.cpu_count() is the host's
    from meshpipeline.engines.determinism import job_cpus
    return job_cpus()


def algorithm_3d(run: dict) -> str:
    """The 3D algorithm: the plan's `algorithm3d`, else DEFAULT_ALGORITHM_3D."""
    name = str((run or {}).get("algorithm3d") or DEFAULT_ALGORITHM_3D).lower()
    return name if name in ALGORITHMS_3D else "delaunay"
#: gmsh's MeshAdapt 2D algorithm (Mesh.Algorithm 1): the other way to mesh a surface, for the
#: surfaces a pass went wrong on (LAD-fea-6) - the default (Frontal-Delaunay) left them empty or bad
MESH_ADAPT = 1
#: at most this many invalid elements are traced back to the surfaces they touch
TRACE_BAD = 500
#: the line the driver reads the surfaces a failed pass went wrong on from (LAD-fea-6)
#: How exactly gmsh integrates 1/size along a curve to place its nodes (its default is 1e-9).
LC_INTEGRATION_PRECISION = 1e-3
BAD_SURFACES = "[GMSH-FEA] BAD SURFACES:"
#: The same surfaces as the CAD's face names (Face_k, or Part_k_Face_j for an assembly): what a
#: suspected CAD defect is held against (MESH-2).
BAD_FACES = "[GMSH-FEA] BAD FACES:"


#: Where a failed fill went wrong, as a file beside the mesh (the log's tail can lose the line under a
#: long traceback - fc_folding_chair, 24 Oct): the driver reads it after a pass that built nothing.
FAILURE_FILE = "fea_failure.json"


def note_failure(ws, surfaces, faces, exc=None) -> None:
    try:
        (Path(ws) / FAILURE_FILE).write_text(json.dumps({
            "bad_surfaces": sorted(int(s) for s in surfaces), "bad_faces": sorted(str(f) for f in faces),
            "error": str(exc or "")[:500]}))
    except OSError:
        pass


def interface_surfaces(gmsh, sidecar: dict, volume_tag: dict[int, int]) -> set[int]:
    """THE FUSED INTERFACES of a bonded assembly: every surface that bounds a piece two parts share (an
    overlap), or that lies between volumes of different parts - where the fuse joined parts."""
    owners: dict[int, set[int]] = {}
    for k, row in enumerate(sidecar.get("volumes") or []):
        if k in volume_tag:
            owners[int(volume_tag[k])] = {int(p) for p in row.get("parts") or []}
    out: set[int] = set()
    for _d, s in gmsh.model.getEntities(2):
        up = [int(v) for v in gmsh.model.getAdjacencies(2, s)[0]]
        parts = set().union(*(owners.get(v, set()) for v in up)) if up else set()
        if len(parts) > 1 or any(len(owners.get(v, set())) > 1 for v in up):
            out.add(int(s))
    return out


def face_names_of(tags, face_tags: dict[str, list[int]] | None) -> list[str]:
    """The CAD face names of gmsh surface `tags`: an assembly's by its sidecar, a single part's Face_k
    (gmsh numbers a single solid's faces in OpenCASCADE's order, as the measurement does)."""
    if face_tags:
        by_tag: dict[int, list[str]] = {}
        for name, ts in face_tags.items():
            for t in ts:
                by_tag.setdefault(int(t), []).append(str(name))
        return sorted({n for t in tags for n in by_tag.get(int(t), [])})
    return [f"Face_{int(t)}" for t in sorted({int(t) for t in tags})]
#: The line that says the 3D fill left whole volumes with no elements (gmsh only warns "No elements in
#: volume N" and goes on): the driver reads it, and an assembly's part is never delivered missing.
EMPTY_VOLUMES = "[GMSH-FEA] EMPTY VOLUMES:"


def empty_volumes(gmsh, vols) -> list[int]:
    """The volumes the 3D fill left with no element (gmsh warns and carries on)."""
    out = []
    for v in vols:
        _types, tags, _nodes = gmsh.model.mesh.getElements(3, int(v))
        if not sum(len(t) for t in tags):
            out.append(int(v))
    return out
#: Distance sampling per face, per direction (gmsh's "Sampling"): enough points that the spacing is
#: about half the group's size, within these bounds.
SAMPLING_MIN, SAMPLING_MAX = 20, 300
#: The most points one Distance field samples (faces x Sampling^2). gmsh samples every face of a
#: field at the one Sampling the field's largest face needs: a perforated plate's 1,467 hole and
#: plate faces at 300 x 300 came to 132M points and ran the mesh job out of memory before any
#: surface was meshed (fea0260). Past this, the faces are split into fields sampled at their own
#: size (the same sizes: the smallest of thresholds over parts is the threshold over the whole).
MAX_FIELD_POINTS = 2_000_000


def _tag(name: str) -> int | None:
    try:
        return int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return None


def _shared_curves(gmsh, a: int, b: int) -> list[int]:
    ca = {abs(t) for _d, t in gmsh.model.getBoundary([(2, a)], oriented=False)}
    cb = {abs(t) for _d, t in gmsh.model.getBoundary([(2, b)], oriented=False)}
    return sorted(ca & cb)


def _sampling(gmsh, tags: list[int], size: float) -> int:
    longest = 0.0
    for t in tags:
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, t)
        longest = max(longest, x1 - x0, y1 - y0, z1 - z0)
    n = math.ceil(longest / max(size / 2.0, 1e-12)) if longest > 0 else SAMPLING_MIN
    return int(min(SAMPLING_MAX, max(SAMPLING_MIN, n)))


def sampled_parts(gmsh, tags: list[int], size: float) -> list[tuple[list[int], int]]:
    """(faces, Sampling) for each Distance field a zone group's faces need: one field as before when
    it stays within MAX_FIELD_POINTS; else each face at its own Sampling, in fields of at most
    MAX_FIELD_POINTS points."""
    n = _sampling(gmsh, tags, size)
    if len(tags) * n * n <= MAX_FIELD_POINTS:
        return [(tags, n)]
    by: dict[int, list[int]] = {}
    for t in tags:
        by.setdefault(_sampling(gmsh, [t], size), []).append(t)
    out: list[tuple[list[int], int]] = []
    for k in sorted(by):
        per = max(1, MAX_FIELD_POINTS // (k * k))
        out.extend((by[k][i:i + per], k) for i in range(0, len(by[k]), per))
    return out


def _micro_place(m: dict) -> dict:
    """A micro detail as the delivery names it: where (its middle point, mm), what and how small."""
    pts = m.get("points_m") or [[0.0, 0.0, 0.0]]
    mid = pts[len(pts) // 2]
    return {"at_mm": [round(float(v) * 1000.0, 2) for v in mid], "kind": m.get("kind"),
            "size_mm": m.get("size_mm"), "faces": list(m.get("faces") or [])}


def size_fields(gmsh, plan: dict, surfaces: set[int], face_tags: dict[str, list[int]] | None = None,
                part_volumes: dict[str, list[int]] | None = None) -> int | None:
    """The plan's zones as gmsh fields; returns the background field (None when no zone binds).
    face_tags: an assembly's face names (Part_k_Face_j) -> the surface tags carrying each; a single
    part's Face_k is surface k. part_volumes: an assembly's Part_k -> the volume tags carrying it
    (the part's base size holds through those volumes, PLAN-fea-17)."""
    from meshpipeline.engines.fea_size_field import group_zones

    def tags_of(name: str) -> list[int]:
        if face_tags is not None:
            return [t for t in face_tags.get(str(name)) or [] if t in surfaces]
        t = _tag(name)
        return [t] if t in surfaces else []

    base = float(plan["base_size_m"])
    g1 = float(plan.get("growth") or 1.3) - 1.0
    ids = []
    # what each field can be at its lowest, entity by entity (MESH-6, reach_restricted)
    bounds: list[dict] = []
    for g in group_zones(plan.get("zones") or []):
        size = float(g["size"])
        if size >= base:
            continue
        if g.get("balls"):
            # PLAN-fea-3's thin spots: the wall's Threshold off its face, held only inside the spots'
            # balls (each its size inside, growing at the plan's rate outside) - the larger of the two
            tags = list(dict.fromkeys(t for f in g["faces"] for t in tags_of(f)))
            if not tags:
                continue
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", tags)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", _sampling(gmsh, tags, size))
            th = gmsh.model.mesh.field.add("Threshold")
            reach = float(g["reach"])
            for key, val in (("InField", dist), ("SizeMin", size), ("SizeMax", base), ("DistMin", reach),
                             ("DistMax", reach + (base - size) / max(g1, 1e-6))):
                gmsh.model.mesh.field.setNumber(th, key, float(val))
            balls = []
            for x, y, z, r in g["balls"]:
                ball = gmsh.model.mesh.field.add("Ball")
                for key, val in (("XCenter", x), ("YCenter", y), ("ZCenter", z), ("Radius", r), ("VIn", size),
                                 ("VOut", base), ("Thickness", (base - size) / max(g1, 1e-6))):
                    gmsh.model.mesh.field.setNumber(ball, key, float(val))
                balls.append(ball)
            near = balls[0]
            if len(balls) > 1:
                near = gmsh.model.mesh.field.add("Min")
                gmsh.model.mesh.field.setNumbers(near, "FieldsList", balls)
            both = gmsh.model.mesh.field.add("Max")
            gmsh.model.mesh.field.setNumbers(both, "FieldsList", [th, near])
            ids.append(both)
            bounds.append({"field": both, "dim": 2, "tags": tags, "size": size, "reach": reach,
                           "dist_max": reach + (base - size) / max(g1, 1e-6),
                           "balls": [[float(x), float(y), float(z), float(r)] for x, y, z, r in g["balls"]],
                           "thickness": (base - size) / max(g1, 1e-6)})
            continue
        if g.get("part"):
            # PLAN-fea-17: the part's base through its whole volume (the estimate's ceiling inside it)
            vols = sorted(set((part_volumes or {}).get(str(g["part"])) or []))
            if vols:
                const = gmsh.model.mesh.field.add("Constant")
                gmsh.model.mesh.field.setNumber(const, "VIn", size)
                gmsh.model.mesh.field.setNumber(const, "VOut", base)
                gmsh.model.mesh.field.setNumbers(const, "VolumesList", vols)
                gmsh.model.mesh.field.setNumber(const, "IncludeBoundary", 1)
                ids.append(const)
                bounds.append({"field": const, "constant": size, "volumes": vols})
        dists = []
        if g["edge"]:
            curves = sorted({c for a, b in (fs for fs in g["edges"] if len(fs) == 2)
                             for ta in tags_of(a) for tb in tags_of(b)
                             for c in _shared_curves(gmsh, ta, tb)})
            if not curves:
                continue
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", curves)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", SAMPLING_MAX)
            dists.append((dist, 1, curves, SAMPLING_MAX))
        else:
            tags = [t for f in g["faces"] for t in tags_of(f)]
            if face_tags is not None:
                tags = list(dict.fromkeys(tags))      # a face two parts share is one surface
            if not tags:
                continue
            for part, n in sampled_parts(gmsh, tags, size):
                dist = gmsh.model.mesh.field.add("Distance")
                gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", part)
                gmsh.model.mesh.field.setNumber(dist, "Sampling", n)
                dists.append((dist, 2, part, n))
        reach = float(g["reach"])
        for dist, dim, src, n_s in dists:
            th = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(th, "InField", dist)
            gmsh.model.mesh.field.setNumber(th, "SizeMin", size)
            gmsh.model.mesh.field.setNumber(th, "SizeMax", base)
            gmsh.model.mesh.field.setNumber(th, "DistMin", reach)
            gmsh.model.mesh.field.setNumber(th, "DistMax", reach + (base - size) / max(g1, 1e-6))
            ids.append(th)
            bounds.append({"field": th, "dim": dim, "tags": list(src), "size": size, "reach": reach,
                           "dist_max": reach + (base - size) / max(g1, 1e-6), "sampling": int(n_s)})
    if not ids:
        return None
    ids = reach_restricted(gmsh, ids, bounds, base)
    if len(ids) == 1:
        return ids[0]
    m = gmsh.model.mesh.field.add("Min")
    gmsh.model.mesh.field.setNumbers(m, "FieldsList", ids)
    return m


#: MESH-6: an entity's box (and a field's source box) is widened by this share of its own diagonal plus
#: this share of the model's before a distance is read off it: gmsh's boxes may come from a tessellation,
#: and a curved face can bulge a little past it. Wider only keeps more fields; it never changes a size.
BOX_PAD_OWN, BOX_PAD_MODEL = 0.05, 1e-4
#: MESH-6: a field is left out on an entity only when its lowest possible value there passes the entity's
#: ceiling by this share as well (rounding can then never make it the smallest)
PRUNE_MARGIN = 1e-6
#: MESH-6: a field's source (a face, a curve) bounds the field from above only when its area (length) is
#: above this share of the model's diagonal squared (to the first): a degenerate one may hold no sample
SAMPLED_MASS = 1e-12
#: MESH-6: the rule that consults, on each CAD entity, only the size fields that can size it
REACH_RULE = "MESH-6"


def _boxes(gmsh, dim_tags: list[tuple[int, int]], pad_model: float):
    """Each entity's padded box (N x 6: lo, hi): gmsh's box and OpenCASCADE's, joined, widened."""
    import numpy as np

    out = np.zeros((len(dim_tags), 6))
    for k, (d, t) in enumerate(dim_tags):
        b = np.asarray(gmsh.model.getBoundingBox(d, t), dtype=float)
        try:
            o = np.asarray(gmsh.model.occ.getBoundingBox(d, t), dtype=float)
            b = np.concatenate([np.minimum(b[:3], o[:3]), np.maximum(b[3:], o[3:])])
        except Exception:  # noqa: BLE001 - gmsh's own box, widened, stands
            pass
        out[k] = b
    pad = BOX_PAD_OWN * np.linalg.norm(out[:, 3:] - out[:, :3], axis=1) + pad_model
    out[:, :3] -= pad[:, None]
    out[:, 3:] += pad[:, None]
    return out


def _box_gap(a, b):
    """The least distance between each box of `a` (N x 6) and box `b` (6,): 0 where they meet."""
    import numpy as np

    d = np.maximum(0.0, np.maximum(a[:, :3] - b[3:], b[:3] - a[:, 3:]))
    return np.sqrt((d * d).sum(axis=1))


def _box_far(a, b):
    """The greatest distance between a point of each box of `a` (N x 6) and a point of box `b` (6,)."""
    import numpy as np

    d = np.maximum(np.abs(a[:, 3:] - b[:3]), np.abs(b[3:] - a[:, :3]))
    return np.sqrt((d * d).sum(axis=1))


def _threshold_low(gap, size: float, base: float, dmin: float, dmax: float):
    """gmsh's Threshold (linear, not stopped at DistMax) at distance `gap`: the lowest it can be anywhere at
    least `gap` from its sources (it never falls as the distance grows)."""
    import numpy as np

    r = np.clip((gap - dmin) / max(dmax - dmin, 1e-300), 0.0, 1.0)
    return size * (1.0 - r) + base * r


def _ball_low(gap_to_centre, radius: float, thickness: float, v_in: float, v_out: float):
    """gmsh's Ball field at distance `gap_to_centre` from its centre: the lowest it can be that far out."""
    import numpy as np

    dist = gap_to_centre - float(radius)
    ramp = v_in + np.clip(dist, 0.0, None) / max(float(thickness), 1e-300) * (v_out - v_in)
    return np.where(dist < 0.0, v_in, np.where(dist <= thickness, ramp, v_out))


#: MESH-6: the surface kinds whose sampling by a gmsh Distance field is known to cover them to within
#: own_cover (their parameters are a length, or an angle times a radius no larger than the face's box)
COVERED_KINDS = ("Plane", "Cylinder", "Cone", "Sphere", "Torus")
#: MESH-6: own_cover's shares - of the face's sample spacing, and of its box diagonal (gmsh tessellates at
#: 0.3 rad angular deflection: its chord error is under 0.04 of a chord)
COVER_SPACINGS, COVER_CHORD_SHARE = 6.0, 0.05


def own_cover(gmsh, surface: int, box, sampling: int) -> float | None:
    """How far a point of `surface` can be from the nearest point a gmsh Distance field (Sampling `sampling`)
    sampled on it; None where that is not known (a free-form face). gmsh samples a face on its tessellation,
    each triangle cut into steps under twice the spacing (the face's box diagonal / Sampling), and the
    tessellation is within its chord error of the face; with no tessellation it samples a Sampling x Sampling
    grid of the face's parameters, whose cells, for these kinds, are under 3.5 diagonals / (Sampling - 1)."""
    import numpy as np

    try:
        kind = str(gmsh.model.getType(2, int(surface)))
    except Exception:  # noqa: BLE001 - unknown: no cover read
        return None
    if kind not in COVERED_KINDS or sampling < 3:
        return None
    diag = float(np.linalg.norm(np.asarray(box[3:]) - np.asarray(box[:3])))
    return COVER_SPACINGS * diag / float(sampling) + COVER_CHORD_SHARE * diag


def _closure_of(gmsh, surface: int) -> list[tuple[int, int]]:
    """The curves and points bounding `surface` (they lie on it)."""
    curves = [(1, abs(int(t))) for _d, t in gmsh.model.getBoundary([(2, int(surface))], oriented=False)]
    points = ([(0, abs(int(t))) for _d, t in gmsh.model.getBoundary(curves, combined=False, oriented=False)]
              if curves else [])
    return curves + points


def reach_restricted(gmsh, ids: list[int], bounds: list[dict], base: float, report: dict | None = None) -> list[int]:
    """MESH-6: EACH CAD ENTITY CONSULTS ONLY THE SIZE FIELDS THAT CAN SIZE IT. gmsh evaluates the background
    field at every point it places, and the smallest-of field evaluates EVERY field there - each Distance
    field a nearest-point search. ox_mc_lid (253 parts, 545 zone groups -> 2,291 fields, 753 Distance fields
    over 22.9M sample points) took 2.0 s a curve, about 10 h for its 18,229 curves before a single surface
    (lab, 26 Oct); the surface and volume stages evaluate the field many times more.

    The size at a point is the smallest field there, never more than the entity's CEILING: the base size
    (Mesh.MeshSizeMax), or a part's own base where that part's Constant field holds (its volumes and what
    bounds them). A field's LOWEST value over an entity is read from boxes alone: a Threshold is at least
    its value at the least distance between the entity's box and its sources' boxes, and a thin spot's Max
    at least the larger of that and its balls'. Where that lowest value is above the ceiling, the field can
    never be the smallest on that entity, so it is wrapped in a Restrict field that skips it there. Every
    size gmsh reads is the same number as before, to the bit: the same mesh, sooner. Returns the fields
    for the Min, each wrapped where it can be skipped somewhere."""
    import numpy as np

    by_field = {int(b["field"]): b for b in bounds}
    if len(ids) < 2 or not any(b.get("tags") for b in bounds):
        return ids
    dim_tags = [(d, int(t)) for d in (0, 1, 2, 3) for _d, t in gmsh.model.getEntities(d)]
    if not dim_tags:
        return ids
    x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
    pad_model = BOX_PAD_MODEL * (math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0)
    box = _boxes(gmsh, dim_tags, pad_model)
    row = {dt: k for k, dt in enumerate(dim_tags)}
    # THE CEILING on each entity: the base, or the smallest Constant (a part's own base) holding there - on
    # its volumes and everything bounding them, as gmsh's IncludeBoundary reads them
    ceiling = np.full(len(dim_tags), float(base))
    for cb in bounds:
        if cb.get("constant") is None:
            continue
        closure: set[tuple[int, int]] = set()
        for v in cb["volumes"]:
            closure.add((3, int(v)))
            faces = [(2, abs(int(t))) for _d, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)]
            curves = ([(1, abs(int(t))) for _d, t in gmsh.model.getBoundary(faces, combined=False, oriented=False)]
                      if faces else [])
            points = ([(0, abs(int(t))) for _d, t in gmsh.model.getBoundary(curves, combined=False, oriented=False)]
                      if curves else [])
            closure.update(faces + curves + points)
        rows = [row[dt] for dt in closure if dt in row]
        ceiling[rows] = np.minimum(ceiling[rows], float(cb["constant"]))
    # each field's LOWEST value over each entity, and its HIGHEST: each of a field's sources holds a sample
    # point, so nowhere on the entity is the field above its value at the farthest that point can be - and
    # the smallest of those highs lowers the ceiling too
    source_box: dict[tuple[int, int], np.ndarray] = {}
    sized: dict[tuple[int, int], bool] = {}
    model_diag = pad_model / BOX_PAD_MODEL
    lows: dict[int, np.ndarray] = {}
    # each face's own Threshold laws, with how near their sample points are to every point of it
    face_laws: dict[int, list[tuple[tuple[float, float, float, float], float]]] = {}
    for fid in ids:
        b = by_field.get(int(fid))
        if b is None or not b.get("tags"):
            continue
        dim = int(b["dim"])
        want = list(dict.fromkeys((dim, int(t)) for t in b["tags"] if (dim, int(t)) not in source_box))
        if want:
            for dt, bx in zip(want, _boxes(gmsh, want, pad_model), strict=True):
                source_box[dt] = bx
                try:
                    sized[dt] = float(gmsh.model.occ.getMass(*dt)) > SAMPLED_MASS * model_diag ** dt[0]
                except Exception:  # noqa: BLE001 - not known to hold a sample point: no high read off it
                    sized[dt] = False
        gap = np.full(len(dim_tags), np.inf)
        far = np.full(len(dim_tags), np.inf)
        for t in dict.fromkeys(int(t) for t in b["tags"]):
            gap = np.minimum(gap, _box_gap(box, source_box[(dim, t)]))
            if sized[(dim, t)]:
                # a degenerate face may hold no sample point, so no high is read off it
                far = np.minimum(far, _box_far(box, source_box[(dim, t)]))
        law = (float(b["size"]), float(base), float(b["reach"]), float(b["dist_max"]))
        low, high = _threshold_low(gap, *law), _threshold_low(far, *law)
        if b.get("balls"):
            near_lo = np.full(len(dim_tags), np.inf)
            near_hi = np.full(len(dim_tags), np.inf)
            for x, y, z, r in b["balls"]:
                c = np.asarray([x, y, z, x, y, z], dtype=float)
                ball = (float(r), float(b["thickness"]), float(b["size"]), float(base))
                near_lo = np.minimum(near_lo, _ball_low(_box_gap(box, c), *ball))
                near_hi = np.minimum(near_hi, _ball_low(_box_far(box, c), *ball))
            low, high = np.maximum(low, near_lo), np.maximum(high, near_hi)
        if dim == 2 and not b.get("balls") and b.get("sampling"):
            # ON ITS OWN FACE a Threshold is near its smallest: no point of the face (or of the curves and
            # points bounding it) is further than own_cover from a point the field sampled on it
            own = np.full(len(dim_tags), np.inf)
            for t in dict.fromkeys(int(t) for t in b["tags"]):
                cover = own_cover(gmsh, t, source_box[(2, t)], int(b["sampling"]))
                if cover is None:
                    continue
                rows = [row[(2, t)]] + [row[dt] for dt in _closure_of(gmsh, t) if dt in row]
                own[rows] = np.minimum(own[rows], cover)
                face_laws.setdefault(t, []).append((law, cover))
            high = np.minimum(high, _threshold_low(own, *law))
        lows[int(fid)] = low
        ceiling = np.minimum(ceiling, high)
    # INSIDE A SOLID every point is within half its box's thinnest side of a boundary face (along that side
    # it leaves the box, and so the solid, within that): where every boundary face has a law of its own, the
    # size inside is no more than the largest of those laws that far from the face
    for k, (d, v) in enumerate(dim_tags):
        if d != 3:
            continue
        half = 0.5 * float(np.min(box[k, 3:] - box[k, :3]))
        bounding = [abs(int(t)) for _d, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)]
        if not bounding or any(f not in face_laws for f in bounding):
            continue
        worst = max(min(float(_threshold_low(np.array([half + c]), *lw)[0]) for lw, c in face_laws[f]) for f in bounding)
        ceiling[k] = min(ceiling[k], worst)
    out: list[int] = []
    wrapped = skipped = 0
    for fid in ids:
        low = lows.get(int(fid))
        if low is None or int(by_field[int(fid)]["dim"]) == 1:
            # A FIELD OFF CURVES IS NEVER SKIPPED: gmsh samples a curve's Distance field from the curve's mesh
            # nodes when it has some, at the field's first use - skipped there, a field would first be used
            # after its curves were meshed, and sample other points (its highest still lowers the ceiling)
            out.append(fid)
            continue
        keep = low < ceiling * (1.0 + PRUNE_MARGIN)
        if report is not None:
            report.setdefault("keep", {})[int(fid)] = keep
            report["dim_tags"], report["ceiling"] = dim_tags, ceiling
        if keep.all():
            out.append(fid)
            continue
        lists: dict[int, list[int]] = {0: [], 1: [], 2: [], 3: []}
        for k in np.flatnonzero(keep).tolist():
            d, t = dim_tags[k]
            lists[d].append(t)
        rf = gmsh.model.mesh.field.add("Restrict")
        gmsh.model.mesh.field.setNumber(rf, "InField", int(fid))
        for d, key in ((0, "PointsList"), (1, "CurvesList"), (2, "SurfacesList"), (3, "VolumesList")):
            if lists[d]:
                gmsh.model.mesh.field.setNumbers(rf, key, lists[d])
        gmsh.model.mesh.field.setNumber(rf, "IncludeBoundary", 0)
        out.append(rf)
        wrapped += 1
        skipped += int((~keep).sum())
    print(f"[GMSH-FEA] size fields consulted only where they can size ({REACH_RULE}): {wrapped} of {len(ids)} "
          f"restricted, {skipped:,} of {len(dim_tags) * len(ids):,} field-entity pairs skipped", file=sys.stderr)
    return out


def _empty_surfaces(gmsh, surfaces: set[int]) -> list[int]:
    out: list[int] = []
    for s in sorted(surfaces):
        _types, tags, _nodes = gmsh.model.mesh.getElements(2, s)
        if not any(len(t) for t in tags):
            out.append(s)
    return out


def surface_mesh(gmsh, surfaces: set[int], remesh: set[int]) -> list[int]:
    """The surface mesh, LAD-fea-6's surfaces on MeshAdapt; a surface the default algorithm leaves
    with no mesh is meshed again the other way at once, in the same pass. Returns the surfaces
    still left with no mesh."""
    for s in sorted(remesh & surfaces):
        gmsh.model.mesh.setAlgorithm(2, s, MESH_ADAPT)
    gmsh.model.mesh.generate(2)
    empty = _empty_surfaces(gmsh, surfaces)
    again = [s for s in empty if s not in remesh]
    if again:
        print(f"[GMSH-FEA] surface(s) {again} left with no mesh - meshed again with MeshAdapt (LAD-fea-6)",
              file=sys.stderr)
        for s in again:
            gmsh.model.mesh.setAlgorithm(2, s, MESH_ADAPT)
        remesh_only(gmsh, again)
        empty = _empty_surfaces(gmsh, surfaces)
    return empty


def remesh_only(gmsh, surfaces) -> None:
    """THOSE SURFACES MESHED AGAIN, AND NOTHING ELSE: every other curve and surface keeps the mesh it has. The
    same mesh, to the byte, as clearing the whole model and meshing it all again (gmsh meshes each surface
    from its own curves, and numbers the nodes and elements afresh after each stage) - without meshing it all
    again: ox_mc_lid's four empty faces cost a second 1D and 2D of all 7,312 faces, 80 minutes (lab, 26 Oct).
    What was visible, and whether only that is meshed, is left as it was."""
    everything = gmsh.model.getEntities()
    seen = [e for e in everything if gmsh.model.getVisibility(*e)]
    only = gmsh.option.getNumber("Mesh.MeshOnlyVisible")
    gmsh.model.mesh.clear([(2, int(s)) for s in surfaces])
    gmsh.model.setVisibility(everything, 0)
    gmsh.model.setVisibility([(2, int(s)) for s in surfaces], 1, recursive=True)
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
    try:
        gmsh.model.mesh.generate(2)
    finally:
        gmsh.model.setVisibility(everything, 0)
        gmsh.model.setVisibility(seen, 1)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", only)


def fill_each_volume(gmsh, vols) -> None:
    """PARTS MESHED APART ARE FILLED APART (tie, contact): each volume on its own, from its own surface
    mesh - parts that pass into each other in space (an interference) then never make one boundary that
    crosses itself (fc_folding_chair, 24 Oct: "PLC Error: Two facets intersect" on the four overlapping
    slat pairs when all were filled at once). The same algorithm, the same surface meshes.

    MESH-7: EACH FILL SEES ONLY ITS OWN SURFACE MESH. gmsh's Delaunay reads the surface mesh of the WHOLE
    model for every volume it fills (each boundary node's size from every face's triangles, and a search
    structure of every face's triangles to classify the tetrahedra): on ox_mc_lid's 253 tied parts, 4.9M
    surface elements, that was about 30 s a volume before a single tetrahedron - two hours of a fill (lab,
    26 Oct). While a volume is filled the other surfaces' triangles are set aside, and after the last fill
    every one is put back - the same elements, tags and order - and the nodes and elements are numbered as
    gmsh numbers them after a fill. A part's boundary nodes are its own (parts meshed apart share none), so
    each fill reads exactly what it read before: the same mesh, to the byte."""
    everything = gmsh.model.getEntities()
    renumber = gmsh.option.getNumber("Mesh.Renumber")
    aside = set_aside_surfaces(gmsh)
    present: set[int] = set()
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
    gmsh.option.setNumber("Mesh.Renumber", 0)
    failed: list[tuple[int, str]] = []
    try:
        for v in vols:
            own = sorted({abs(int(t)) for _d, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)})
            for s_ in own:
                if s_ not in present:
                    _put_back(gmsh, aside, s_)
                    present.add(s_)
            gmsh.model.setVisibility(everything, 0)
            gmsh.model.setVisibility([(3, int(v))], 1, recursive=True)
            try:
                gmsh.model.mesh.generate(3)
            except Exception as exc:
                # THE PART THAT FAILED, and where: its own surfaces whose meshes cross each other. The other
                # parts are filled all the same (LAD-fea-11): the next pass keeps them and meshes only this one
                where = crossing_surfaces(gmsh, set(own)) or own[:12]
                failed.append((int(v), f"{exc} (volume {int(v)}; on surface {', surface '.join(str(t) for t in where)})"))
                print(f"[GMSH-FEA] volume {int(v)} could not be filled - the other parts are filled all the same "
                      f"({KEEP_RULE})", file=sys.stderr)
                gmsh.model.mesh.clear([(3, int(v))])
            for s_ in own:
                # as the fill left them (a fill turns its boundary triangles to face out of the volume)
                types, tags, nodes = gmsh.model.mesh.getElements(2, int(s_))
                aside[int(s_)] = [(int(et), tg, nd) for et, tg, nd in zip(types, tags, nodes, strict=True)]
                gmsh.model.mesh.removeElements(2, int(s_))
                present.discard(s_)
    finally:
        # every surface's triangles back, as they were; then numbered as a fill numbers them
        for s_ in sorted(present):
            gmsh.model.mesh.removeElements(2, int(s_))
        for s_ in sorted(aside):
            _put_back(gmsh, aside, s_)
        gmsh.option.setNumber("Mesh.Renumber", renumber)
        if renumber:
            gmsh.model.mesh.renumberNodes()
            gmsh.model.mesh.renumberElements()
        gmsh.model.setVisibility(everything, 1)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", 0)
    if failed:
        raise PartsFailed(failed)


class PartsFailed(RuntimeError):
    """Some volumes of parts meshed apart could not be filled; every other one was (LAD-fea-11)."""

    def __init__(self, failed: list[tuple[int, str]]):
        super().__init__("; ".join(msg for _v, msg in failed))
        self.volumes = [int(v) for v, _m in failed]


#: LAD-fea-11: THE PARTS A FAILED PASS DID MESH ARE KEPT - their mesh (gmsh's own file, at the scale it was
#: built at) and what the next pass needs to read it back (the scale, the sidecar's ties, which volumes)
KEEP_MSH = "fea_kept_parts.msh"
KEEP_JSON = "fea_kept_parts.json"
KEEP_RULE = "LAD-fea-11"


def save_kept(gmsh, ws: Path, *, scale: float, tied: dict, kept: list[int], failed: list[int],
              part_volumes: dict[str, list[int]] | None) -> None:
    """LAD-fea-11: the parts this pass filled, kept for the next pass: the whole model's mesh as gmsh writes it
    (with each node's place on its curve or face), no physical group in it, and which volumes it keeps."""
    groups = [(d, t, list(gmsh.model.getEntitiesForPhysicalGroup(d, t)), gmsh.model.getPhysicalName(d, t))
              for d, t in gmsh.model.getPhysicalGroups()]
    keys = ("Mesh.SaveParametric", "Mesh.SaveAll", "Mesh.Binary", "Mesh.MshFileVersion")
    before = {k: gmsh.option.getNumber(k) for k in keys}
    gmsh.model.removePhysicalGroups()
    try:
        for k, v in zip(keys, (1, 1, 1, 4.1), strict=True):
            gmsh.option.setNumber(k, v)
        gmsh.write(str(Path(ws) / KEEP_MSH))
    finally:
        for k, v in before.items():
            gmsh.option.setNumber(k, v)
        for d, t, ents, name in groups:
            gmsh.model.addPhysicalGroup(d, ents, tag=t, name=name)
    part_of = {int(v): p for p, vs in (part_volumes or {}).items() for v in vs}
    (Path(ws) / KEEP_JSON).write_text(json.dumps({
        "rule": KEEP_RULE, "scale": float(scale), "kept_volumes": sorted(int(v) for v in kept),
        "failed_volumes": sorted(int(v) for v in failed),
        "kept_parts": sorted({part_of[v] for v in kept if v in part_of}),
        "failed_parts": sorted({part_of[v] for v in failed if v in part_of}),
        "tags": {"surface_tag": {str(k): int(v) for k, v in (tied.get("surface_tag") or {}).items()},
                 "volume_tag": {str(k): int(v) for k, v in (tied.get("volume_tag") or {}).items()}}}))
    print(f"[GMSH-FEA] kept the mesh of {len(kept)} part volume(s) for the next pass; {len(failed)} to mesh again "
          f"({KEEP_RULE})", file=sys.stderr)


def kept_for(ws: Path, run: dict) -> dict | None:
    """LAD-fea-11: what an earlier pass kept, when this pass's plan says to use it and the files are here."""
    keep = (run or {}).get("keep")
    if not keep:
        return None
    try:
        meta = json.loads((Path(ws) / KEEP_JSON).read_text())
    except (OSError, ValueError):
        return None
    if not (Path(ws) / KEEP_MSH).exists() or not meta.get("kept_volumes"):
        return None
    meta["tags"] = {k: {int(a): int(b) for a, b in (meta.get("tags") or {}).get(k, {}).items()}
                    for k in ("surface_tag", "volume_tag")}
    return meta


def hold_volumes(gmsh, volumes: list[int]) -> list[tuple[int, int, object, object, object, list]]:
    """LAD-fea-11: the volumes' meshes and their faces', taken out of the model (gmsh's surface stage discards
    every volume's mesh, and the mesh of faces it read from a file); give_back_volumes puts them back, the same
    nodes (with their places on their faces), elements and tags."""
    held = []
    for v in volumes:
        faces = sorted({abs(int(t)) for _d, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)})
        for dim, tag in [(3, int(v))] + [(2, f) for f in faces]:
            tags, xyz, par = gmsh.model.mesh.getNodes(dim, tag, includeBoundary=False, returnParametricCoord=True)
            types, etags, enodes = gmsh.model.mesh.getElements(dim, tag)
            held.append((dim, tag, tags, xyz, par, list(zip(types, etags, enodes, strict=True))))
        gmsh.model.mesh.clear([(3, int(v))] + [(2, f) for f in faces])
    return held


def give_back_volumes(gmsh, held) -> None:
    """Put held meshes back. gmsh numbers new nodes and elements from the largest tag still in the model, so the
    surface stage may have taken the held tags: every held node and element comes back above every tag in use
    now, and the held elements' references to held nodes follow (those to the curves' nodes stay)."""
    import numpy as np

    node_shift = int(gmsh.model.mesh.getMaxNodeTag())
    elem_shift = int(gmsh.model.mesh.getMaxElementTag())
    held_nodes = np.concatenate([np.asarray(h[2], dtype=np.int64) for h in held] or [np.zeros(0, np.int64)])
    top = int(held_nodes.max()) if len(held_nodes) else 0
    is_held = np.zeros(top + 1, dtype=bool)
    is_held[held_nodes] = True
    for dim, tag, tags, xyz, par, _elements in sorted(held, key=lambda h: (h[0], h[1])):
        if len(tags):
            gmsh.model.mesh.addNodes(dim, tag, (np.asarray(tags, dtype=np.int64) + node_shift).tolist(), xyz,
                                     par if dim == 2 else [])
    for _dim, tag, _tags, _xyz, _par, elements in sorted(held, key=lambda h: (h[0], h[1])):
        for et, etags, enodes in elements:
            nodes = np.asarray(enodes, dtype=np.int64)
            inside = nodes <= top
            moved = nodes.copy()
            moved[inside & is_held[np.minimum(nodes, top)]] += node_shift
            gmsh.model.mesh.addElementsByType(tag, int(et), (np.asarray(etags, dtype=np.int64) + elem_shift).tolist(),
                                              moved.tolist())


def load_kept(gmsh, ws: Path, kept: list[int]) -> None:
    """LAD-fea-11: the kept parts' mesh read back onto the same entities; every other volume, with the faces,
    curves and corners bounding it, left with no mesh, to be meshed in this pass."""
    gmsh.merge(str(Path(ws) / KEEP_MSH))
    keep = {int(v) for v in kept}
    clear: set[tuple[int, int]] = set()
    for _d, v in gmsh.model.getEntities(3):
        if int(v) in keep:
            continue
        clear.add((3, int(v)))
        faces = [(2, abs(int(t))) for _d2, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)]
        curves = ([(1, abs(int(t))) for _d2, t in gmsh.model.getBoundary(faces, combined=False, oriented=False)]
                  if faces else [])
        points = ([(0, abs(int(t))) for _d2, t in gmsh.model.getBoundary(curves, combined=False, oriented=False)]
                  if curves else [])
        clear.update(faces + curves + points)
    if clear:
        gmsh.model.mesh.clear(sorted(clear))


#: MESH-7: the rule that fills each volume of parts meshed apart with only its own surface mesh in the model
FILL_ALONE_RULE = "MESH-7"


def set_aside_surfaces(gmsh) -> dict[int, list[tuple[int, object, object]]]:
    """Every surface's elements (type, tags, nodes - in gmsh's order), taken out of the model (MESH-7)."""
    aside: dict[int, list[tuple[int, object, object]]] = {}
    for _d, s_ in gmsh.model.getEntities(2):
        types, tags, nodes = gmsh.model.mesh.getElements(2, int(s_))
        if sum(len(t) for t in tags):
            aside[int(s_)] = [(int(et), tg, nd) for et, tg, nd in zip(types, tags, nodes, strict=True)]
            gmsh.model.mesh.removeElements(2, int(s_))
    return aside


def _put_back(gmsh, aside: dict, surface: int) -> None:
    for et, tags, nodes in aside.get(int(surface)) or []:
        gmsh.model.mesh.addElementsByType(int(surface), int(et), tags, nodes)


def crossing_surfaces(gmsh, surfaces: set[int], *, limit: int = 12) -> list[int]:
    """The surfaces whose surface meshes pass through each other (cad/crossings on the 2D mesh) - where a
    fill that failed on "facets intersect" went wrong when gmsh's message names no surface."""
    import numpy as np

    from meshpipeline.cad.crossings import find_crossings

    try:
        node_tags, xyz, _ = gmsh.model.mesh.getNodes()
        index = {int(t): k for k, t in enumerate(node_tags)}
        pts = np.asarray(xyz, dtype=float).reshape(-1, 3)
        tris, owner = [], []
        for s in sorted(surfaces):
            types, _tags, nodes = gmsh.model.mesh.getElements(2, s)
            for et, nd in zip(types, nodes, strict=True):
                if int(et) not in (2, 9):            # 3- or 6-node triangles: their corners
                    continue
                per = 3 if int(et) == 2 else 6
                arr = np.asarray(nd, dtype=np.int64).reshape(-1, per)[:, :3]
                tris.append(np.vectorize(index.get)(arr))
                owner.append(np.full(len(arr), s))
        if not tris:
            return []
        T = np.concatenate(tris)
        own = np.concatenate(owner)
        found = find_crossings(pts, T)
        hit: dict[int, int] = {}
        for a, b in found.pairs.tolist():
            for t in (own[a], own[b]):
                hit[int(t)] = hit.get(int(t), 0) + 1
        return [s for s, _n in sorted(hit.items(), key=lambda kv: -kv[1])[:limit]]
    except Exception:  # noqa: BLE001 - a search we cannot run names nothing
        return []


def surfaces_at_point(gmsh, text: str, surfaces: set[int], *, share: float = 1e-4) -> list[int]:
    """The surfaces at the point a gmsh error names ("... intersect at point (x,y,z)") - within `share`
    of the model's size, the nearest ones else - so a failed fill that names no surface still says where
    (fc_folding_chair, 24 Oct: "PLC Error: Two facets intersect at point (0.104,0.39,-0.146)")."""
    import math
    import re

    m = re.search(r"at point \(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)", text or "")
    if not m or not surfaces:
        return []
    p = [float(m.group(k)) for k in (1, 2, 3)]
    try:
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        size = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0
        near = []
        for s in sorted(surfaces):
            q = gmsh.model.getClosestPoint(2, s, p)[0]
            near.append((math.dist(p, q[:3]), s))
        near.sort()
        within = [s for d, s in near if d <= share * size]
        return within or [s for _d, s in near[:2]]
    except Exception:  # noqa: BLE001 - a point we cannot place names nothing
        return []


def surfaces_named(text: str) -> list[int]:
    """The surface tags a gmsh error names ("... on surface 8", "No elements in surface 9")."""
    import re

    return sorted({int(m) for m in re.findall(r"surface (\d+)", text or "")})


#: The worst element the quality record (four decimals) still reads as above 0: an element under it reads
#: SICN 0.0, and the ladder calls the pass invalid (engines/fea_ladder.pass_facts) - so it is traced as
#: invalid too (fc_glass_frames' pane: flat tets of SICN 1.5e-5, no inverted one, and no surface named
#: for LAD-fea-6 in any of four passes).
DEGENERATE_SICN = 5e-5


def bad_surfaces(gmsh, tags, qualities, *, floor: float = 0.0, skip=()) -> list[int]:
    """The CAD surfaces the invalid elements (quality not above 0) touch: their nodes on a surface,
    or on a curve or corner of one - where LAD-fea-6 meshes the next pass the other way.

    A SLIVER LOCKED BETWEEN TWO FACES (LAD-fea-6) - an element whose four corners all lie on two
    surfaces, none inside - names only the second of the two: two faces facing each other across a
    wall one element thick (fc_glass_frames' 10 mm pane at 7.5 mm, faces 5 and 6) get the same surface
    mesh, node across from node, and every four of them make a flat tet no optimiser can move. Meshing
    BOTH the other way mirrors them again; meshing one breaks the mirror (worst SICN 1.5e-5 -> 0.22)."""
    out: set[int] = set()
    locked: set[tuple[int, int]] = set()
    loose: set[int] = set()
    skipped = {int(t) for t in skip}
    bad = [int(t) for t, q in zip(tags, qualities, strict=True) if q <= floor and int(t) not in skipped][:TRACE_BAD]
    for t in bad:
        _etype, nodes, _dim, _tag = gmsh.model.mesh.getElement(t)
        mine: set[int] = set()
        corners = []
        for k, n in enumerate(nodes):
            _xyz, _par, dim, ent = gmsh.model.mesh.getNode(int(n))
            if k < 4:
                corners.append((int(dim), int(ent)))
            if dim == 2:
                mine.add(int(ent))
            elif dim in (0, 1):
                up = [int(c) for c in gmsh.model.getAdjacencies(dim, int(ent))[0]]
                if dim == 0:
                    up = [int(s) for c in up for s in gmsh.model.getAdjacencies(1, c)[0]]
                mine.update(up)
        faces = {e for d, e in corners}
        if all(d == 2 for d, _e in corners) and len(faces) == 2:
            a, b = sorted(faces)
            locked.add((a, b))
        else:
            loose.update(mine)
        out.update(mine)
    # the first face of each locked pair keeps its mesh, unless another invalid element touches it too
    keep = {a for a, _b in locked} - loose - {b for _a, b in locked}
    return sorted((out - keep) | alike_seconds(gmsh, locked))


#: Two faces are alike (same kind, area and gap to their partner) to this share of their area / the part
_ALIKE = 1e-6


def alike_seconds(gmsh, locked) -> set[int]:
    """LAD-fea-6: THE PAIRS ALIKE A LOCKED PAIR. Faces the same as a pair that locked a sliver (the same kind
    and area, the same distance apart) mesh the same way and lock their own as soon as sizes move: twelve
    alike panes in fc_glass_frames took one pass each, pass after pass (6, then 3, then 1). The second face of
    every alike pair is named with them, so one pass breaks every mirror. Best effort: nothing when the
    model cannot be measured."""
    import numpy as np

    if not locked:
        return set()
    try:
        tags = sorted(int(t) for _d, t in gmsh.model.getEntities(2))
        info = {t: (float(gmsh.model.occ.getMass(2, t)), np.asarray(gmsh.model.occ.getCenterOfMass(2, t), dtype=float),
                    str(gmsh.model.getType(2, t))) for t in tags}
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        diag = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0
    except Exception:  # noqa: BLE001 - no geometry to compare: the locked pairs alone
        return set()
    out: set[int] = set()
    for a, b in sorted(locked):
        area, kind = info[a][0], info[a][2]
        gap = float(np.linalg.norm(info[a][1] - info[b][1]))
        same = [t for t in tags if info[t][2] == kind and abs(info[t][0] - area) <= _ALIKE * max(area, 1e-300)]
        for i, s in enumerate(same):
            for q in same[i + 1:]:
                if abs(float(np.linalg.norm(info[s][1] - info[q][1])) - gap) <= _ALIKE * diag:
                    out.add(q)
    return out


def _peak_rss_mb() -> float | None:
    """This process's peak resident memory (MB), or None where the platform does not say. Read as
    /proc's VmHWM, which starts afresh at exec: getrusage's ru_maxrss carries the parent's peak
    across fork and exec, so a mesher started by a planner that peaked at 10 GB read 10 GB."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return round(float(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError):
        return None
    return None


#: A staged face or solid is the sidecar's when its area (volume) agrees to this share and its centre
#: to this share of the model's size.
_SAME_SIZE, _SAME_PLACE = 1e-6, 1e-6


def staged_tags(gmsh, sidecar: dict) -> tuple[dict[int, int], dict[int, int]]:
    """gmsh's tag of each sidecar surface and volume (index, 0-based). gmsh numbers the faces and
    solids of a B-rep it reads in OpenCASCADE's MapShapes order - the sidecar's - and every one is
    checked by its area (volume) and centre; should the order not hold, each is tied to the nearest
    by both, and said."""
    import numpy as np

    x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
    diag = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0

    def tie(dim: int, rows: list[dict], key: str, among=None) -> dict[int, int]:
        tags = sorted(t for _d, t in gmsh.model.getEntities(dim))
        got = {t: (float(gmsh.model.occ.getMass(dim, t)), np.asarray(gmsh.model.occ.getCenterOfMass(dim, t)))
               for t in tags}

        def gap(t: int, row: dict) -> float:
            m, c = got[t]
            want = float(row.get(key) or 0.0)
            return (abs(m - want) / max(abs(want), 1e-300)
                    + float(np.linalg.norm(c - np.asarray(row.get("centroid_m") or [0, 0, 0], dtype=float))) / diag)

        if len(tags) == len(rows) and all(gap(tags[k], r) <= _SAME_SIZE + _SAME_PLACE for k, r in enumerate(rows)):
            return {k: tags[k] for k in range(len(rows))}
        print(f"[GMSH-FEA] the staged assembly's {'faces' if dim == 2 else 'solids'} are not in the sidecar's "
              "order - tied by size and place", file=sys.stderr)
        # ONE TO ONE, closest first, each among the entities of its own parts (two parts' faces where
        # they touch have the same size and place: only the part tells them apart)
        cands = sorted((gap(t, r), k, t) for k, r in enumerate(rows)
                       for t in (among(r) if among else tags) if t in got)
        out: dict[int, int] = {}
        used: set[int] = set()
        for _g, k, t in cands:
            if k not in out and t not in used:
                out[k] = t
                used.add(t)
        return out

    vols = sidecar.get("volumes") or []
    volume_tag = tie(3, vols, "volume_m3")
    owned: dict[int, set[int]] = {}
    for k, row in enumerate(vols):
        if k in volume_tag:
            bound = {abs(int(t)) for _d, t in gmsh.model.getBoundary([(3, volume_tag[k])], oriented=False)}
            for p in row.get("parts") or []:
                owned.setdefault(int(p), set()).update(bound)

    def own_faces(row: dict) -> set[int]:
        parts = set()
        for nm in row.get("names") or []:
            try:
                parts.add(int(str(nm).split("_")[1]))
            except (IndexError, ValueError):
                pass
        return set().union(*(owned.get(p, set()) for p in parts)) if parts else set()

    return tie(2, sidecar.get("surfaces") or [], "area_m2", among=own_faces), volume_tag


def assembly_summary(asm: dict, sidecar: dict, prep) -> dict:
    """What the delivery says of an assembly: how the parts are joined, each part with its element
    count, every joint as written, and the notes."""
    return {"connection": asm.get("connection"), "mode": sidecar.get("mode"),
            "parts": [{"id": p["id"], "name": p["name"], "elements": p["last"] - p["first"] + 1} for p in prep.parts],
            "joints": [{"name": j.name, "kind": j.kind, "parts": [f"Part_{j.parts[0]}", f"Part_{j.parts[1]}"],
                        "slave": j.sides[0].name, "master": j.sides[1].name, "overlap": j.overlap,
                        **({"reach_m": j.reach} if j.reach else {})}
                       for j in prep.joints],
            # a part with no elements of its own: wholly inside another, so fused with it (bonded)
            "absorbed": [p["id"] for p in sidecar.get("parts") or []
                         if p["id"] not in {q["id"] for q in prep.parts}],
            "interfaces": len(sidecar.get("interfaces") or []),
            "faces": len(prep.faces), "notes": list(sidecar.get("notes") or [])}


#: MESH-4: millimetres per metre - the scale the staged solid is meshed again at when it could not be
#: meshed in metres
MM_PER_M = 1000.0
#: MESH-4: the line that says the mesh was built at the CAD's millimetre scale (quality.json says it too)
MM_SCALE = "[GMSH-FEA] MESHED AT MILLIMETRE SCALE"
#: the rule that meshes a CAD solid at its own scale after the metre build failed
MM_SCALE_RULE = "MESH-4"


class NoSolid(Exception):
    """The staged file holds no solid volume."""


class FillFailed(Exception):
    """The surface mesh or the fill failed: the gmsh error, the surfaces it went wrong on, and the
    staged tags (an assembly's sidecar ties, read once at the metre scale)."""

    def __init__(self, exc: BaseException, named: list[int], face_tags: dict | None, tags: dict,
                 hollow: list[int] | None = None):
        super().__init__(str(exc))
        self.exc, self.named, self.face_tags, self.tags, self.hollow = exc, named, face_tags, tags, hollow or []

    def report(self, ws) -> None:
        """The lines (and the file) the driver reads the failure's surfaces from (LAD-fea-6, MESH-2)."""
        if self.hollow:
            print(f"{EMPTY_VOLUMES} {','.join(str(v) for v in self.hollow)}", file=sys.stderr)
        print(f"{BAD_SURFACES} {','.join(str(s) for s in self.named)}", file=sys.stderr)
        print(f"{BAD_FACES} {','.join(face_names_of(self.named, self.face_tags))}", file=sys.stderr)
        note_failure(ws, self.named, face_names_of(self.named, self.face_tags), None if self.hollow else self.exc)


def scaled_plan(plan: dict, k: float) -> dict:
    """The plan with every length (base size, zone sizes and reaches, thin-spot balls) times `k`: the
    plan as the size fields read it in a model `k` times the metre one (MESH-4)."""
    if k == 1.0:
        return plan
    out = dict(plan)
    out["base_size_m"] = float(plan["base_size_m"]) * k
    zones = []
    for z in plan.get("zones") or []:
        z = dict(z)
        for key in ("size_m", "reach_m", "radius_m"):
            if z.get(key) is not None:
                z[key] = float(z[key]) * k
        if z.get("at_m") is not None:
            z["at_m"] = [float(v) * k for v in z["at_m"]]
        zones.append(z)
    out["zones"] = zones
    return out


def to_m(gmsh, scale: float) -> None:
    """Every mesh node of a model meshed `scale` times the metre one, back in metres (MESH-4)."""
    s = 1.0 / float(scale)
    gmsh.model.mesh.affineTransform([s, 0.0, 0.0, 0.0, 0.0, s, 0.0, 0.0, 0.0, 0.0, s, 0.0])
    print(f"{MM_SCALE} (nodes scaled by {s:g} back to metres)", file=sys.stderr)


#: MESH-5: the rule that drops a face's repeated triangles before the fill
REPEATED_TRIANGLES_RULE = "MESH-5"


def drop_repeated_triangles(gmsh, surfaces) -> dict[int, int]:
    """MESH-5: A FACE WHOSE SURFACE MESH REPEATS A TRIANGLE. gmsh's periodic mesher can lay the same three
    nodes down twice where a seam runs into a pole (nakuja_rocket's nose cone, Part_2_Face_5: an interior
    node placed just across the seam, at u = -0.017, beside the apex), and the fill then refuses the
    boundary ("Found two duplicated facets", "overlapping facets on surface 5 surface 5") whatever the 2D
    algorithm or the scale. A copy adds no surface, so one is dropped; and when the one left is a FLAP - an
    edge of it bounds no other triangle of the part's surface, so the pair was a fold out and back over
    nothing (the nose cone: edges used 1, 3 and 1 times) - it is dropped too: the surface closes without
    it, and a triangle no element has as a face would stop the solver files. A surface mesh with no repeat
    - every one the fill accepts - is left untouched. Returns {surface: triangles dropped}."""
    import numpy as np

    faces: dict[int, tuple] = {}
    for s in sorted(int(t) for t in surfaces):
        types, tags, nodes = gmsh.model.mesh.getElements(2, s)
        if len(types) != 1 or int(types[0]) != 2:          # only a face of plain 3-node triangles
            continue
        tri = np.asarray(nodes[0], dtype=np.int64).reshape(-1, 3)
        _u, first, count = np.unique(np.sort(tri, axis=1), axis=0, return_index=True, return_counts=True)
        if len(first) < len(tri):
            faces[s] = (tri, np.asarray(tags[0]), first, count)
    if not faces:
        return {}
    # how many triangles of the part's surface use each edge, every triangle counted once
    edge_use: dict[tuple[int, int], int] = {}
    for s in sorted(int(t) for t in surfaces):
        types, _tags, nodes = gmsh.model.mesh.getElements(2, s)
        for et, nd in zip(types, nodes, strict=True):
            if int(et) != 2:
                continue
            for a, b, c in np.unique(np.sort(np.asarray(nd, dtype=np.int64).reshape(-1, 3), axis=1), axis=0).tolist():
                for e in ((a, b), (b, c), (a, c)):
                    edge_use[e] = edge_use.get(e, 0) + 1
    dropped: dict[int, int] = {}
    for s, (tri, tags, first, count) in faces.items():
        keep = []
        for k, n in zip(first.tolist(), count.tolist(), strict=True):
            if n > 1:
                a, b, c = sorted(int(v) for v in tri[k])
                if any(edge_use.get(e, 0) < 2 for e in ((a, b), (b, c), (a, c))):
                    continue                               # a flap: no copy is kept
            keep.append(k)
        keep.sort()
        gmsh.model.mesh.removeElements(2, s)
        gmsh.model.mesh.addElementsByType(s, 2, [int(t) for t in tags[keep]], [int(v) for v in tri[keep].ravel()])
        dropped[s] = int(len(tri) - len(keep))
    print(f"[GMSH-FEA] repeated surface triangles dropped {dropped} ({REPEATED_TRIANGLES_RULE})", file=sys.stderr)
    return dropped


def micro_curves_one_segment(gmsh) -> list[int]:
    """MESH-4: every CAD curve shorter than PLAN-fea-12's micro length (cad/fea_measure.MICRO_FRACTION of
    the model's diagonal) meshed as ONE segment. gmsh gives a curved edge at least two, and on a curve a
    few nanometres long the node between them lies on the chord: the face beside it gets a flat triangle
    folded onto its neighbour's ("overlapping facets", dihedral 1.8e-5 degrees - libredrive_pulley_housing's
    24 nm edges where a half-hole grazes the bore). THE PART'S diagonal: in an assembly, the smallest solid
    the curve bounds - the whole model's would make every curve under a millimetre one segment in a 945 mm
    assembly (ox_mc_lid: 7,533 curves, on screws planned at 0.05 mm), against the plan. A single solid's is
    the model's, as before. Returns the curves."""
    from meshpipeline.cad.fea_measure import MICRO_FRACTION

    def diag(dim: int, tag: int) -> float:
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(dim, tag)
        return math.dist((x0, y0, z0), (x1, y1, z1))

    whole = diag(-1, -1)
    solids = {int(v): diag(3, int(v)) for _d, v in gmsh.model.getEntities(3)}
    single = len(solids) <= 1

    def limit(c: int) -> float:
        if single:
            return MICRO_FRACTION * whole
        sizes = [solids[int(v)] for s in gmsh.model.getAdjacencies(1, c)[0]
                 for v in gmsh.model.getAdjacencies(2, int(s))[0] if int(v) in solids]
        return MICRO_FRACTION * (min(sizes) if sizes else whole)

    short = [int(c) for _d, c in gmsh.model.getEntities(1) if float(gmsh.model.occ.getMass(1, c)) < limit(int(c))]
    for c in short:
        gmsh.model.mesh.setTransfiniteCurve(c, 2)
    return short


def _source(ftw: dict) -> str:
    """The engine line the solver files carry."""
    if not ftw:
        return "Hexera gmsh engine"
    return f"Hexera gmsh engine + {ftw.get('version') or 'fTetWild'} (LAD-fea-9: {', '.join(ftw.get('parts') or [])})"


def rung_volumes(run: dict, vols: list[int], part_volumes: dict[str, list[int]] | None) -> list[int]:
    """LAD-fea-9: the volumes fTetWild meshes - the parts the plan names (plan["mesh"]["ftetwild"]
    ["parts"]), every volume when it names none (a single part, or every part of an assembly)."""
    parts = ((run or {}).get("ftetwild") or {}).get("parts")
    if not parts or not part_volumes:
        return sorted(vols)
    picked = {int(v) for p in parts for v in part_volumes.get(f"Part_{int(p)}") or []}
    return sorted(v for v in vols if v in picked) or sorted(vols)


def last_rung(gmsh, ws: Path, plan: dict, geom: Path, built: dict, asm, sidecar: dict, *, order: int,
              tick: Ticker, fell_back: str = "", scale: float = 1.0) -> dict:
    """LAD-fea-9: fTetWild's mesh of the rung's volumes, written into the model gmsh meshed the other parts
    in (engines/fea_tetwild.mesh_into). Returns the rung's record for quality.json."""
    from meshpipeline.engines import fea_tetwild as TW
    from meshpipeline.engines.determinism import job_cpus

    vols, surfaces = built["vols"], built["surfaces"]
    # each gmsh volume tag -> its solid (MapShapes order), each face index -> its gmsh surface and name
    if asm:
        volume_index = {int(t): int(k) for k, t in built["volume_tag"].items()}
        surface_tag_of = {int(k): int(t) for k, t in built["surface_tag"].items()}
        names = [str((row.get("names") or [f"Face_{k + 1}"])[0]) for k, row in enumerate(sidecar.get("surfaces") or [])]
    else:
        volume_index = {int(v): k for k, v in enumerate(sorted(vols))}
        surface_tag_of = {k: k + 1 for k in range(len(surfaces))}
        names = [f"Face_{k + 1}" for k in range(len(surfaces))]
    # fTetWild's own output is the sign of progress while it runs (the ticker would hide a stuck one)
    tick.stop()
    # the model was built `scale` times its metres and its mesh is back in metres now (MESH-4's to_m): fTetWild's
    # parts go in in metres too
    info = TW.mesh_into(gmsh, ws, plan, geom, built["rung_volumes"], volume_index=volume_index,
                        surface_tag_of=surface_tag_of, names=names, order=order, workers=job_cpus(),
                        model_scale=float(scale))
    info["tied"] = bool(built.get("gmsh_volumes"))
    info["gmsh_parts"] = len(built.get("gmsh_volumes") or [])
    if fell_back:
        info["gmsh_failed_again"] = fell_back
    info["words"] = TW.words(info)
    return info


def rung_build(gmsh, ws: Path, plan: dict, geom: Path, asm: dict | None, sidecar: dict, *,
               scale: float = 1.0, tags: dict | None = None) -> dict:
    """LAD-fea-9's build: gmsh meshes the parts fTetWild does not take (or keeps them from an earlier pass,
    LAD-fea-11); should it fail on them in this pass too, every part goes to fTetWild (said as "fell_back").
    Raises NoSolid only."""
    try:
        return build_volume(gmsh, ws, plan, geom, asm, sidecar, scale=scale, tags=tags, rung="named")
    except FillFailed as first:
        said = f"{type(first.exc).__name__}: {str(first.exc)[:300]}"
        print(f"[FTETWILD] gmsh failed on the other parts too ({said}) - every part goes to fTetWild",
              file=sys.stderr)
        gmsh.clear()
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", 0)
        return {**build_volume(gmsh, ws, plan, geom, asm, sidecar, rung="all"), "fell_back": said}


def build_volume(gmsh, ws: Path, plan: dict, geom: Path, asm: dict | None, sidecar: dict, *,
                 scale: float = 1.0, tags: dict | None = None, rung: str | None = None) -> dict:
    """Read the staged solid (`scale` times its metres: OpenCASCADE's own similarity as it reads), size it
    by the plan at that scale, and mesh its surfaces and its volume. Returns what the rest of the mesh
    run reads; raises NoSolid, or FillFailed with the surfaces it went wrong on. `tags`: an assembly's
    sidecar ties from an earlier read (the same file read at another scale has the same entities). Read
    at any scale but metres (MESH-4, after a failure) its micro curves are one segment each. `rung`
    (LAD-fea-9): "named" leaves the parts the plan gives fTetWild unmeshed and meshes the others, alone,
    as every pass meshes them; "all" meshes none - fTetWild takes every part (`rung_volumes`)."""
    run = plan.get("mesh") or {}
    gmsh.model.add("fea")
    gmsh.option.setNumber("Geometry.OCCScaling", float(scale))
    try:
        gmsh.model.occ.importShapes(str(geom))
    finally:
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
    gmsh.model.occ.synchronize()
    vols = [t for _, t in gmsh.model.getEntities(3)]
    if not vols:
        raise NoSolid()
    surfaces = {t for _, t in gmsh.model.getEntities(2)}
    gmsh.model.addPhysicalGroup(3, vols, name="solid")
    gmsh.model.addPhysicalGroup(2, sorted(surfaces), name="free")
    sized = scaled_plan(plan, scale)
    gmsh.option.setNumber("Mesh.MeshSizeMax", float(sized["base_size_m"]))
    face_tags: dict[str, list[int]] | None = None
    surface_tag: dict[int, int] = {}
    volume_tag: dict[int, int] = {}
    if asm:
        # tied by area and centre against the sidecar's metres: read once, at the metre scale
        if tags is None:
            surface_tag, volume_tag = staged_tags(gmsh, sidecar)
        else:
            surface_tag, volume_tag = tags["surface_tag"], tags["volume_tag"]
        face_tags = {}
        for k, row in enumerate(sidecar.get("surfaces") or []):
            for nm in row.get("names") or []:
                if k in surface_tag:
                    face_tags.setdefault(str(nm), []).append(surface_tag[k])
    part_volumes: dict[str, list[int]] | None = None
    if asm:
        part_volumes = {}
        for k, row in enumerate(sidecar.get("volumes") or []):
            for p in row.get("parts") or []:
                if k in volume_tag:
                    part_volumes.setdefault(f"Part_{int(p)}", []).append(volume_tag[k])
    tied = {"surface_tag": surface_tag, "volume_tag": volume_tag}
    micro: list[int] = []
    if scale != 1.0:
        micro = micro_curves_one_segment(gmsh)
        if micro:
            print(f"[GMSH-FEA] micro curve(s) {micro[:20]} meshed as one segment each (MESH-4)", file=sys.stderr)
    note_stage(ws, "sizing", "setting the element sizes")
    bg = size_fields(gmsh, sized, surfaces, face_tags, part_volumes)
    if bg is not None:
        gmsh.model.mesh.field.setAsBackgroundMesh(bg)
    remesh = {int(s) for s in run.get("remesh_surfaces") or []}
    # LAD-fea-9, THE LAST RUNG: fTetWild's parts are left unmeshed; gmsh meshes only the others (their own
    # surfaces, each volume alone), exactly as a pass meshes them. Without a rung nothing here changes
    rung_vols: list[int] = []
    # LAD-fea-11: THE PARTS AN EARLIER PASS FILLED ARE KEPT (parts meshed apart): read back, and only the others
    # meshed in this pass. Without a kept file nothing here changes
    kept: list[int] = []
    meta = (kept_for(ws, run) if asm and rung != "all" and str((sidecar or {}).get("mode") or "") in ("tie", "contact")
            else None)
    if meta is not None and float(meta.get("scale") or 1.0) == float(scale):
        kept = [v for v in meta["kept_volumes"] if v in set(vols)]
        if kept:
            load_kept(gmsh, ws, kept)
            print(f"[GMSH-FEA] {len(kept)} part volume(s) kept from the pass before; meshing the other "
                  f"{len(vols) - len(kept)} ({KEEP_RULE})", file=sys.stderr)
    if rung is not None or kept:
        rung_vols = [] if rung is None else (sorted(vols) if rung == "all" else rung_volumes(run, vols, part_volumes))
        rest = [v for v in vols if v not in rung_vols and v not in set(kept)]
        all_vols, all_surfaces = vols, surfaces
        if not rest:
            if rung is None:
                return {"vols": vols, "surfaces": surfaces, "face_tags": face_tags, "surface_tag": surface_tag,
                        "volume_tag": volume_tag, "part_volumes": part_volumes, "micro_curves": micro,
                        "repeated_triangles": {}, "kept_volumes": kept}
            return {"vols": vols, "surfaces": surfaces, "face_tags": face_tags, "surface_tag": surface_tag,
                    "volume_tag": volume_tag, "part_volumes": part_volumes, "micro_curves": micro,
                    "repeated_triangles": {}, "rung_volumes": rung_vols,
                    "gmsh_volumes": sorted(v for v in vols if v not in rung_vols), "kept_volumes": kept}
        everything = gmsh.model.getEntities()
        gmsh.model.setVisibility(everything, 0)
        gmsh.model.setVisibility([(3, int(v)) for v in rest], 1, recursive=True)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
        vols = rest
        surfaces = {abs(int(t)) for v in rest for _d, t in gmsh.model.getBoundary([(3, v)], oriented=False)} & surfaces
    # LAD-fea-8: after a surface stage that stalled, gmsh's first mesh of every face is kept (its repeats
    # dropped below, MESH-5) rather than thrown away for a MeshAdapt run that never ends
    gmsh.option.setNumber("Mesh.AlgorithmSwitchOnFailure", 0 if run.get("surface_switch") is False else 1)
    note_stage(ws, "surface", "meshing the surfaces")
    # gmsh's surface stage throws away every volume's mesh: the kept parts' are held aside through it, and no
    # node is numbered afresh meanwhile (the held elements name their curves' nodes by tag; the fill numbers all)
    renumber = gmsh.option.getNumber("Mesh.Renumber")
    if kept:
        gmsh.option.setNumber("Mesh.Renumber", 0)
    held = hold_volumes(gmsh, kept) if kept else []
    try:
        empty = surface_mesh(gmsh, surfaces, remesh)
    except Exception as exc:
        # A SURFACE NO 2D ALGORITHM COULD MESH ("Impossible to mesh periodic surface 26"): gmsh stops
        # there - the surface it names and every one it left empty are where it went wrong
        named = sorted({s for s in surfaces_named(str(exc)) if s in surfaces}
                       | set(_empty_surfaces(gmsh, surfaces) if not surfaces_named(str(exc)) else []))
        raise FillFailed(exc, named, face_tags, tied) from exc
    finally:
        if held:
            give_back_volumes(gmsh, held)
        gmsh.option.setNumber("Mesh.Renumber", renumber)
    repeated = drop_repeated_triangles(gmsh, surfaces)
    try:
        note_stage(ws, "volume", "filling the solid with elements")
        if asm and str((sidecar or {}).get("mode") or "") in ("tie", "contact"):
            fill_each_volume(gmsh, vols)
        else:
            gmsh.model.mesh.generate(3)
    except Exception as exc:
        if isinstance(exc, PartsFailed):
            # LAD-fea-11: every other part was filled - kept, with the parts kept before, for the next pass
            good = sorted({int(v) for v in (all_vols if (rung is not None or kept) else vols)}
                          - set(exc.volumes) - set(rung_vols))
            if good:
                save_kept(gmsh, ws, scale=scale, tied=tied, kept=good, failed=exc.volumes, part_volumes=part_volumes)
        # the surfaces the fill failed on, for the next pass (LAD-fea-6)
        named = sorted(set(empty) | {s for s in surfaces_named(str(exc)) if s in surfaces})
        if not named:
            # the error names a point, not a surface: the surfaces there - else the surfaces whose
            # surface meshes pass through each other (the "facets intersect" a fill reports)
            named = surfaces_at_point(gmsh, str(exc), surfaces) or crossing_surfaces(gmsh, surfaces)
        raise FillFailed(exc, named, face_tags, tied) from exc
    # A VOLUME LEFT EMPTY is a failed fill, not a mesh (fc_computer_desk, 24 Oct: "No elements in
    # volume 5", then the deck writer crashed on the faces of a part with no elements): said with
    # its surfaces, as a fill failure is, and never delivered
    hollow = empty_volumes(gmsh, vols)
    if hollow:
        named = sorted({abs(int(t)) for v in hollow for _d, t in gmsh.model.getBoundary([(3, v)], oriented=False)
                        if int(t) in surfaces} | set(empty))
        raise FillFailed(RuntimeError(f"{EMPTY_VOLUMES} the 3D fill left volume(s) {hollow} with no elements"),
                         named, face_tags, tied, hollow=hollow)
    if rung is not None or kept:
        gmsh.model.setVisibility(gmsh.model.getEntities(), 1)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", 0)
        out = {"vols": all_vols, "surfaces": all_surfaces, "face_tags": face_tags, "surface_tag": surface_tag,
               "volume_tag": volume_tag, "part_volumes": part_volumes, "micro_curves": micro,
               "repeated_triangles": repeated, "kept_volumes": kept}
        if rung is not None:
            out.update(rung_volumes=rung_vols, gmsh_volumes=sorted(v for v in all_vols if v not in rung_vols))
        return out
    return {"vols": vols, "surfaces": surfaces, "face_tags": face_tags, "surface_tag": surface_tag,
            "volume_tag": volume_tag, "part_volumes": part_volumes, "micro_curves": micro,
            "repeated_triangles": repeated}


def mesh(workspace) -> int:
    """Mesh the staged solid by the plan. Exit codes as the driver's: 0 a valid mesh, 2 no
    geometry, 3 no solid, 4 fatal defects."""
    from meshpipeline.engines import fea_checks as checks
    from meshpipeline.engines.fea_plan import PLAN_FILE, STAGED_SOLIDS
    from meshpipeline.engines.gmsh import driver as D

    ws = Path(workspace)
    plan = json.loads((ws / PLAN_FILE).read_text())
    # PLAN-fea-15: the staged solid the plan was measured on (gmsh's own B-rep first)
    geom = next((ws / n for n in STAGED_SOLIDS if (ws / n).exists()), ws / "geometry.step")
    # AN ASSEMBLY (PLAN-fea-17): the parts as the driver staged them - fused where they are bonded,
    # side by side where they touch or are tied - and what each face and solid of that file is
    asm = plan.get("assembly") or None
    sidecar: dict = {}
    if asm:
        geom = ws / str(asm.get("staged") or "assembly.brep")
        try:
            sidecar = json.loads((ws / str(asm.get("sidecar") or "assembly.json")).read_text())
        except (OSError, ValueError):
            print("[GMSH-FEA] the assembly's sidecar is missing - an assembly needs it", file=sys.stderr)
            return 2
    if not geom.exists():
        print(f"[GMSH-FEA] geometry.step missing in {ws}", file=sys.stderr)
        return 2
    run = plan.get("mesh") or {}
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 1)
        # THE JOB'S CORES, REPEATABLY (Areen, 24 Oct; lab 24 Oct, 12 largest training parts run twice):
        # gmsh's own parallel work (the optimiser, the curving) on every core, while the curve and
        # surface meshing stay on ONE thread - their parallel versions are not repeatable (0/17 meshes
        # the same twice at 8 threads; and 3 of 5 on 22 Oct). So the mesh is byte-identical to the
        # one-thread mesh (12/12) and to itself run twice (12/12), about 1.5 times sooner.
        threads = mesh_threads(run)
        gmsh.option.setNumber("General.NumThreads", threads)
        gmsh.option.setNumber("Mesh.MaxNumThreads1D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads2D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads3D", threads)
        gmsh.option.setNumber("Mesh.RandomSeed", 1)
        # THE CURVES MESHED IN SECONDS, NOT MINUTES (fc_folding_chair, 24 Oct: 979 s of a 1,064 s pass in
        # 1D): gmsh integrates 1/size along every curve to its default precision of 1e-9, evaluating the
        # whole size field each step; 1e-3 places the same nodes to a tenth of a percent (97,319 vs 97,484
        # nodes after 2D) in 26 s
        gmsh.option.setNumber("Mesh.LcIntegrationPrecision", LC_INTEGRATION_PRECISION)
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", ALGORITHMS_3D.get(algorithm_3d(run), 1))
        base = float(plan["base_size_m"])
        tick = Ticker(ws)
        # LAD-fea-9: the last rung's pass (plan["mesh"]["engine"] == "ftetwild", engines/fea_tetwild.py)
        rung = str(run.get("engine") or "") == "ftetwild"
        ftw: dict = {}
        tags: dict | None = None
        scale = 1.0
        # LAD-fea-11: parts an earlier pass kept are read back at the scale they were built at
        kept_meta = kept_for(ws, run) if asm else None
        if kept_meta is not None:
            scale, tags = float(kept_meta.get("scale") or 1.0), kept_meta["tags"]
        try:
            built = (build_volume(gmsh, ws, plan, geom, asm, sidecar, scale=scale, tags=tags) if not rung
                     else rung_build(gmsh, ws, plan, geom, asm, sidecar, scale=scale, tags=tags))
        except NoSolid:
            print("[GMSH-FEA] no solid volume in geometry.step - an FEA mesh needs a closed solid",
                  file=sys.stderr)
            return 3
        except FillFailed as first:
            if scale != 1.0:
                # built at the CAD's own scale already (the kept parts' scale): nothing more to try in this pass
                first.report(ws)
                raise first.exc from None
            # MESH-4: THE CAD'S OWN SCALE. The staged solid is in metres; OpenCASCADE's fixed
            # tolerances (1e-7 of a model unit) are then a tenth of a micron, and a CAD edge of a few
            # nanometres where a feature grazes another (libredrive_pulley_housing: a 1.8 mm half-hole
            # touching a 28 mm bore, three edges of 24 nm, 56 um and 24 nm) falls inside them - a
            # periodic face no 2D algorithm can mesh, folded facets no fill can take. The same solid
            # read in millimetres, as the CAD was drawn, holds those edges a thousand times above the
            # kernel's tolerance. Only after the metre build failed: every part that meshes in metres
            # meshes exactly as before.
            print(f"[GMSH-FEA] the solid could not be meshed at the metre scale ({str(first.exc)[:200]}) - "
                  f"meshing it again at the CAD's own millimetre scale ({MM_SCALE_RULE})", file=sys.stderr)
            tags = first.tags
            gmsh.clear()
            scale = MM_PER_M
            try:
                built = build_volume(gmsh, ws, plan, geom, asm, sidecar, scale=scale, tags=tags)
            except FillFailed as again:
                again.report(ws)
                raise again.exc from None
        vols, face_tags = built["vols"], built["face_tags"]
        surface_tag, volume_tag = built["surface_tag"], built["volume_tag"]
        # gmsh's own steps on what gmsh meshed (every volume, or the rung's other parts; none when
        # fTetWild takes every part)
        meshed = not rung or bool(built.get("gmsh_volumes"))
        note_stage(ws, "optimising", "improving the elements")
        for _ in range(int(run.get("optimise_passes") or 1) if meshed else 0):
            gmsh.model.mesh.optimize("Netgen")
        order = int((plan.get("element") or {}).get("order") or 2)
        settled: dict = {"straightened": 0, "set_aside": [], "cusp_places_m": [], "cusp_elements": 0,
                         "micro_used": [], "micro_elements": 0}
        straight: dict = {}
        if order > 1 and meshed:
            from meshpipeline.engines.gmsh import fea_settle

            # each element's straight shape, before curving: what PLAN-fea-11 compares against
            st_tags, st_q = fea_settle.qualities(gmsh)
            straight = dict(zip(st_tags.tolist(), st_q.tolist(), strict=True))
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)
            note_stage(ws, "curving", "curving the elements to the surface")
            # curved on the CAD at the scale it was meshed at (its midside nodes go onto the surface)
            gmsh.model.mesh.setOrder(order)
        if scale != 1.0:
            # MESH-4: the mesh back to metres, the unit every file and figure below is in (a similarity:
            # every element keeps its shape and quality)
            to_m(gmsh, scale)
        if order > 1 and meshed:
            tick.start("settling", "checking the curved elements")
            # PLAN-fea-11 (never worse than straight) and PLAN-fea-10 (the cusps set aside)
            settled = fea_settle.settle(gmsh, straight, [c["at_m"] for c in plan.get("cusps") or []],
                                        D.SICN_FLOOR, micro=plan.get("micro") or [])
        if rung:
            # LAD-fea-9: fTetWild's parts into the same model, after gmsh's own (straight 10-node tets)
            ftw = last_rung(gmsh, ws, plan, geom, built, asm, sidecar, order=order, tick=tick, scale=scale,
                            fell_back=str(built.get("fell_back") or ""))

        tick.start("writing", "measuring the elements and writing the solver files")
        etypes, etags, _ = gmsh.model.mesh.getElements(3)
        all_tags = [t for arr in etags for t in arr]
        qualities = gmsh.model.mesh.getElementQualities(all_tags, "minSICN") if all_tags else []
        n_elem = len(all_tags)
        n_nodes = len(gmsh.model.mesh.getNodes()[0])
        aside = set(settled["set_aside"])
        held = [q for t, q in zip(all_tags, qualities, strict=True) if int(t) not in aside]
        # THE FLOOR holds every element but the ones set aside at a cusp (PLAN-fea-10); validity
        # (no degenerate element) holds every element without exception
        min_sicn = float(min(held)) if len(held) else (float(min(qualities)) if len(qualities) else 0.0)
        min_all = float(min(qualities)) if len(qualities) else 0.0
        # THE NOTE FLOOR: SICN 0.1 for straight elements, QUADRATIC_SCALED_JACOBIAN (0.01) for curved
        # ones - a note either way; validity (above 0) is the only blocker
        from meshpipeline.engines.fea_quality import QUADRATIC_SCALED_JACOBIAN
        note_floor = QUADRATIC_SCALED_JACOBIAN if order > 1 else D.SICN_FLOOR
        low = int(sum(1 for q in held if q < note_floor))
        aside_q = [q for t, q in zip(all_tags, qualities, strict=True) if int(t) in aside]
        fatal = []
        # LAD-fea-6: where the invalid elements are, for the next pass
        if min_all <= 0.0:
            traced = bad_surfaces(gmsh, all_tags, qualities)
        elif round(min_sicn, 4) <= 0.0:
            # INVALID BY THE RECORD (SICN under DEGENERATE_SICN, none inverted): where those elements are,
            # the cusp and micro elements set aside apart - LAD-fea-6 meshes the next pass the other way there
            traced = bad_surfaces(gmsh, all_tags, qualities, floor=DEGENERATE_SICN, skip=aside)
        else:
            traced = []
        if n_elem == 0:
            fatal.append("no volume elements generated")
        if min_all <= 0.0:
            fatal.append("degenerate elements (SICN <= 0)")
        assembly_out: dict = {}
        exports: dict = {}
        if asm:
            # AN ASSEMBLY'S FILES (engines/fea_assembly_deck.py): every part its own set, every face
            # Part_k_Face_j on its own part's side, and the joints - shared, contact or tied
            from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
            from meshpipeline.engines import fea_assembly_deck as AD
            from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh

            prep = AD.prepare(from_gmsh(gmsh, sorted(vols)), sidecar, surface_tag=surface_tag,
                              volume_tag=volume_tag)
            written = AD.write_all(ws, prep, units=COMPLETED_MESH_UNIT.value, source=_source(ftw),
                                   connection=str(asm.get("connection") or "bonded"))
            deck = written["deck"]
            exports = {"nastran": written["nastran"], "ansys": written["ansys"]}
            assembly_out = assembly_summary(asm, sidecar, prep)
            # the viewer's clickable faces: each name and the mesh surfaces that carry it
            assembly_out["face_tags"] = {k: sorted(set(v)) for k, v in (face_tags or {}).items()}
        else:
            deck = D._write_deck(gmsh, ws, discrete=False, source=_source(ftw))
        gmsh.write(str(ws / "mesh.msh"))
        rules: dict = {}
        shape: dict = {}
        solid = None
        try:
            from meshpipeline.engines import fea_quality
            from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh
            solid = from_gmsh(gmsh, sorted(vols))
            rules = checks.measure(solid, plan, tags_by_name=face_tags)
            # THE ELEMENTS AS THE SOLVERS JUDGE THEM (engines/fea_quality.py): the gate refuses an
            # inverted element (VER-1); the verdict holds the rest to the production limits (VER-2),
            # the elements set aside at a cusp apart (PLAN-fea-10: reported on their own)
            shape = fea_quality.measure(solid, set_aside=sorted(aside))
        except Exception as exc:  # noqa: BLE001 - evidence; an unmeasured row decides as unmeasured
            print(f"[GMSH-FEA] plan rules or element quality not measured: {exc}", file=sys.stderr)
        # MESH ONCE, EXPORT MANY (design 8): the Nastran and Ansys files from the same mesh, with the
        # deck's numbers and face names; the user picks one at download
        if not asm:                                    # an assembly's were written with its deck, above
            try:
                from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
                from meshpipeline.engines import fea_writers
                if solid is None:
                    raise ValueError("no internal mesh was read")
                exports = fea_writers.write_all(ws, solid, cad_names=deck.get("cad_face_names") or {},
                                                units=COMPLETED_MESH_UNIT.value, source=_source(ftw))
            except Exception as exc:  # noqa: BLE001 - the deck stands; a missing file is offered as missing
                print(f"[GMSH-FEA] Nastran / Ansys files not written: {exc}", file=sys.stderr)
        if shape.get("fea_inverted"):
            fatal.append(f"{shape['fea_inverted']} inverted element(s) (Jacobian not above zero)")
        tw = (rules.get("through_wall") or {}).get("min")
        (ws / "quality.json").write_text(json.dumps({
            "cells": n_elem, "nodes": n_nodes, "element_order": order,
            "min_sicn": round(min_sicn, 4),
            # PLAN-fea-11 / PLAN-fea-10: elements given straight sides, and those set aside at a cusp
            "fea_straightened": int(settled["straightened"]),
            "fea_set_aside": {"rule": "PLAN-fea-10/PLAN-fea-12", "elements": len(aside),
                              "cusp_elements": int(settled["cusp_elements"]),
                              "micro_elements": int(settled["micro_elements"]),
                              "min_sicn": round(float(min(aside_q)), 4) if aside_q else None,
                              "places_mm": [[round(v * 1000.0, 2) for v in p] for p in settled["cusp_places_m"]],
                              "micro": [_micro_place(plan["micro"][i]) for i in settled["micro_used"]]},
            "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
            "fatal": fatal, "size_h": base, "quality_bar": "fea", "sicn_floor": note_floor,
            "elements_under_floor": low,
            "bounds": D._final_node_bounds(gmsh),
            "groups": {"free": "free"}, "default_group": "free", "default_group_used": True,
            "deck": deck,
            # the worst figures, top level, where the production limits read them
            **{k: shape.get(k) for k in ("fea_min_scaled_jacobian", "fea_max_jacobian_ratio",
                                          "fea_max_aspect_ratio", "fea_min_dihedral_deg",
                                          "fea_max_dihedral_deg", "fea_inverted") if k in shape},
            "fea_rules_short": len(rules.get("short") or []) if rules else None,
            # MESH-5: a face's repeated surface triangles dropped before the fill
            **({"fea_repeated_triangles": {str(k): v for k, v in built["repeated_triangles"].items()}}
               if built.get("repeated_triangles") else {}),
            # MESH-4: meshed at the CAD's millimetre scale after the metre build failed
            **({"fea_mesh_scale": "mm", "fea_mesh_scale_rule": MM_SCALE_RULE,
                "fea_micro_curves_one_segment": len(built.get("micro_curves") or [])}
               if scale != 1.0 else {}),
            "fea_bad_surfaces": traced,
            "fea_bad_faces": face_names_of(traced, face_tags),
            # PLAN-fea-17: the invalid elements' surfaces that are FUSED INTERFACES (a bonded assembly)
            "fea_bad_interface": (sorted(set(traced) & interface_surfaces(gmsh, sidecar, volume_tag))
                                  if asm and traced and str(sidecar.get("mode") or "") == "bonded" else []),
            # PLAN-fea-16: the mesher's peak memory, for the bytes-per-element model
            "peak_rss_mb": _peak_rss_mb(),
            # THE PLAN'S RULES AS MET (engines/fea_checks.py): the gate reads the through-wall
            # count in place of the flow floor (engines/gmsh/gates.py)
            "fea": {"plan": True, "limit": (plan.get("limit") or {}).get("elements"),
                    "estimate": (plan.get("estimate") or {}).get("elements"),
                    # PLAN-fea-8: what a plan coarsened to fit leaves short
                    **({"coarsened": plan["coarsened"],
                        "resolution_short": list(plan.get("resolution_short") or [])}
                       if plan.get("coarsened") else {}),
                    "through_wall_min": tw, "rules": rules, "quality": shape, "exports": exports,
                    # PLAN-fea-17: the parts, how they are joined, and every joint as written
                    **({"assembly": assembly_out} if asm else {}),
                    # LAD-fea-9: the parts fTetWild meshed, how close to the CAD, the faces it affected
                    **({"ftetwild": ftw} if ftw else {})},
            **({"engine_rung": "ftetwild"} if ftw else {}),
        }, indent=1))
        print(f"[GMSH-FEA] elements={n_elem} (planned ~{(plan.get('estimate') or {}).get('elements')}) "
              f"nodes={n_nodes} order={order} min_sicn={min_sicn:.3f} through_wall_min={tw} "
              f"short={rules.get('short')} worst_sj={shape.get('fea_min_scaled_jacobian')} "
              f"jac_ratio={shape.get('fea_max_jacobian_ratio')} aspect={shape.get('fea_max_aspect_ratio')} "
              f"dihedral={shape.get('fea_min_dihedral_deg')}-{shape.get('fea_max_dihedral_deg')}")
        tick.stop()
        return 0 if not fatal else 4
    finally:
        gmsh.finalize()


__all__ = ["BAD_FACES", "face_names_of", "ALGORITHMS_3D", "BAD_SURFACES", "MESH_ADAPT", "bad_surfaces", "mesh", "size_fields", "surface_mesh",
           "surfaces_named"]
