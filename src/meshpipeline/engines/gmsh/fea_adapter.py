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
            dists.append(dist)
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
                dists.append(dist)
        reach = float(g["reach"])
        for dist in dists:
            th = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(th, "InField", dist)
            gmsh.model.mesh.field.setNumber(th, "SizeMin", size)
            gmsh.model.mesh.field.setNumber(th, "SizeMax", base)
            gmsh.model.mesh.field.setNumber(th, "DistMin", reach)
            gmsh.model.mesh.field.setNumber(th, "DistMax", reach + (base - size) / max(g1, 1e-6))
            ids.append(th)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    m = gmsh.model.mesh.field.add("Min")
    gmsh.model.mesh.field.setNumbers(m, "FieldsList", ids)
    return m


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
        gmsh.model.mesh.clear()
        gmsh.model.mesh.generate(2)
        empty = _empty_surfaces(gmsh, surfaces)
    return empty


def fill_each_volume(gmsh, vols) -> None:
    """PARTS MESHED APART ARE FILLED APART (tie, contact): each volume on its own, from its own surface
    mesh - parts that pass into each other in space (an interference) then never make one boundary that
    crosses itself (fc_folding_chair, 24 Oct: "PLC Error: Two facets intersect" on the four overlapping
    slat pairs when all were filled at once). The same algorithm, the same surface meshes."""
    everything = gmsh.model.getEntities()
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
    try:
        for v in vols:
            gmsh.model.setVisibility(everything, 0)
            gmsh.model.setVisibility([(3, int(v))], 1, recursive=True)
            try:
                gmsh.model.mesh.generate(3)
            except Exception as exc:
                # THE PART THAT FAILED, and where: its own surfaces whose meshes cross each other
                own = {abs(int(t)) for _d, t in gmsh.model.getBoundary([(3, int(v))], oriented=False)}
                where = crossing_surfaces(gmsh, own) or sorted(own)[:12]
                raise RuntimeError(f"{exc} (volume {int(v)}; on surface {', surface '.join(str(t) for t in where)})") from exc
    finally:
        gmsh.model.setVisibility(everything, 1)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", 0)


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


def bad_surfaces(gmsh, tags, qualities) -> list[int]:
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
    bad = [int(t) for t, q in zip(tags, qualities, strict=True) if q <= 0.0][:TRACE_BAD]
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
    return sorted(out - keep)


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
                        "slave": j.sides[0].name, "master": j.sides[1].name, "overlap": j.overlap}
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
    """MESH-5: A FACE WHOSE SURFACE MESH REPEATS A TRIANGLE keeps one copy. gmsh's periodic mesher can lay
    the same three nodes down twice where a seam runs into a pole (nakuja_rocket's nose cone, Part_2_Face_5:
    an interior node placed just across the seam, at u = -0.017, makes the triangle beside the apex on both
    sides of it), and the fill then refuses the boundary ("Found two duplicated facets", "overlapping
    facets on surface 5 surface 5") whatever the 2D algorithm or the scale. A copy adds no surface: the
    face is the same without it. A surface mesh with no repeat - every one the fill accepts - is left
    untouched. Returns {surface: copies dropped}."""
    import numpy as np

    dropped: dict[int, int] = {}
    for s in sorted(int(t) for t in surfaces):
        types, tags, nodes = gmsh.model.mesh.getElements(2, s)
        if len(types) != 1 or int(types[0]) != 2:          # only a face of plain 3-node triangles
            continue
        tri = np.asarray(nodes[0], dtype=np.int64).reshape(-1, 3)
        _u, first = np.unique(np.sort(tri, axis=1), axis=0, return_index=True)
        if len(first) == len(tri):
            continue
        keep = np.sort(first)
        gmsh.model.mesh.removeElements(2, s)
        gmsh.model.mesh.addElementsByType(s, 2, [int(t) for t in np.asarray(tags[0])[keep]],
                                          [int(n) for n in tri[keep].ravel()])
        dropped[s] = int(len(tri) - len(keep))
    if dropped:
        print(f"[GMSH-FEA] repeated surface triangles dropped {dropped} ({REPEATED_TRIANGLES_RULE})", file=sys.stderr)
    return dropped


def micro_curves_one_segment(gmsh) -> list[int]:
    """MESH-4: every CAD curve shorter than PLAN-fea-12's micro length (cad/fea_measure.MICRO_FRACTION of
    the model's diagonal) meshed as ONE segment. gmsh gives a curved edge at least two, and on a curve a
    few nanometres long the node between them lies on the chord: the face beside it gets a flat triangle
    folded onto its neighbour's ("overlapping facets", dihedral 1.8e-5 degrees - libredrive_pulley_housing's
    24 nm edges where a half-hole grazes the bore). Returns the curves."""
    from meshpipeline.cad.fea_measure import MICRO_FRACTION

    x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
    limit = MICRO_FRACTION * math.dist((x0, y0, z0), (x1, y1, z1))
    short = [int(c) for _d, c in gmsh.model.getEntities(1) if float(gmsh.model.occ.getMass(1, c)) < limit]
    for c in short:
        gmsh.model.mesh.setTransfiniteCurve(c, 2)
    return short


def build_volume(gmsh, ws: Path, plan: dict, geom: Path, asm: dict | None, sidecar: dict, *,
                 scale: float = 1.0, tags: dict | None = None) -> dict:
    """Read the staged solid (`scale` times its metres: OpenCASCADE's own similarity as it reads), size it
    by the plan at that scale, and mesh its surfaces and its volume. Returns what the rest of the mesh
    run reads; raises NoSolid, or FillFailed with the surfaces it went wrong on. `tags`: an assembly's
    sidecar ties from an earlier read (the same file read at another scale has the same entities). Read
    at any scale but metres (MESH-4, after a failure) its micro curves are one segment each."""
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
    # LAD-fea-8: after a surface stage that stalled, gmsh's first mesh of every face is kept (its repeats
    # dropped below, MESH-5) rather than thrown away for a MeshAdapt run that never ends
    gmsh.option.setNumber("Mesh.AlgorithmSwitchOnFailure", 0 if run.get("surface_switch") is False else 1)
    note_stage(ws, "surface", "meshing the surfaces")
    try:
        empty = surface_mesh(gmsh, surfaces, remesh)
    except Exception as exc:
        # A SURFACE NO 2D ALGORITHM COULD MESH ("Impossible to mesh periodic surface 26"): gmsh stops
        # there - the surface it names and every one it left empty are where it went wrong
        named = sorted({s for s in surfaces_named(str(exc)) if s in surfaces}
                       | set(_empty_surfaces(gmsh, surfaces) if not surfaces_named(str(exc)) else []))
        raise FillFailed(exc, named, face_tags, tied) from exc
    repeated = drop_repeated_triangles(gmsh, surfaces)
    try:
        note_stage(ws, "volume", "filling the solid with elements")
        if asm and str((sidecar or {}).get("mode") or "") in ("tie", "contact"):
            fill_each_volume(gmsh, vols)
        else:
            gmsh.model.mesh.generate(3)
    except Exception as exc:
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
        tags: dict | None = None
        scale = 1.0
        try:
            built = build_volume(gmsh, ws, plan, geom, asm, sidecar, scale=scale)
        except NoSolid:
            print("[GMSH-FEA] no solid volume in geometry.step - an FEA mesh needs a closed solid",
                  file=sys.stderr)
            return 3
        except FillFailed as first:
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
        note_stage(ws, "optimising", "improving the elements")
        for _ in range(int(run.get("optimise_passes") or 1)):
            gmsh.model.mesh.optimize("Netgen")
        order = int((plan.get("element") or {}).get("order") or 2)
        settled: dict = {"straightened": 0, "set_aside": [], "cusp_places_m": [], "cusp_elements": 0,
                         "micro_used": [], "micro_elements": 0}
        straight: dict = {}
        if order > 1:
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
        if order > 1:
            tick.start("settling", "checking the curved elements")
            # PLAN-fea-11 (never worse than straight) and PLAN-fea-10 (the cusps set aside)
            settled = fea_settle.settle(gmsh, straight, [c["at_m"] for c in plan.get("cusps") or []],
                                        D.SICN_FLOOR, micro=plan.get("micro") or [])

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
        traced = bad_surfaces(gmsh, all_tags, qualities) if min_all <= 0.0 else []
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
            written = AD.write_all(ws, prep, units=COMPLETED_MESH_UNIT.value, source="Hexera gmsh engine",
                                   connection=str(asm.get("connection") or "bonded"))
            deck = written["deck"]
            exports = {"nastran": written["nastran"], "ansys": written["ansys"]}
            assembly_out = assembly_summary(asm, sidecar, prep)
            # the viewer's clickable faces: each name and the mesh surfaces that carry it
            assembly_out["face_tags"] = {k: sorted(set(v)) for k, v in (face_tags or {}).items()}
        else:
            deck = D._write_deck(gmsh, ws, discrete=False)
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
                                                units=COMPLETED_MESH_UNIT.value, source="Hexera gmsh engine")
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
                    **({"assembly": assembly_out} if asm else {})},
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
