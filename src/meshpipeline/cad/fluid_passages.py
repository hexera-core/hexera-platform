# Responsibility: Say, before any mesher runs, which separate fluid passages a staged internal
# surface closes and which declared openings sit on each - so no declared port can vanish from a
# mesh because the mesher filled a different passage than the one it opens into.
# Boundaries: geometry only, on the staged per-patch triangles. No engine, no I/O beyond reading
# the staged STLs it is handed, no model call. It describes and groups; the engine decides how to
# mesh the groups.
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: The workspace record of a case meshed as several separate passages (cfMesh: every passage after
#: the first is a case directory of its own, meshed beside the first and merged).
PASSAGES_FILE = "passages.json"
#: What the passage reading found, beside every internal case it read (evidence, not an input).
PASSAGES_READ_FILE = "passages_read.json"
#: A closed piece inside another one is part of that passage's boundary (a centre rod, a vane
#: modelled on its own): its generalised winding number seen from the outer piece is about 1.
INSIDE_WINDING = 0.5
#: How many pieces are read for nesting before the reading gives up and calls each one its own
#: passage: a packed bed of 700 spheres is no set of passages anyone meshes one by one.
MAX_PIECES = 64


@dataclass
class Passage:
    """One connected fluid region: its outer shell and every closed piece standing inside it."""
    pieces: list[int]
    faces: int
    volume: float                      # m3 of the outer shell less the pieces standing in it
    ports: list[str] = field(default_factory=list)
    bbox: tuple[list[float], list[float]] = ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    closed: bool = True
    wall_faces: int = 0                # how many of its triangles are wall (none: lids alone)

    def as_dict(self) -> dict:
        return {"faces": self.faces, "volume_m3": float(f"{self.volume:.6g}"), "ports": list(self.ports),
                "bbox_min": [round(v, 6) for v in self.bbox[0]], "bbox_max": [round(v, 6) for v in self.bbox[1]],
                "closed": self.closed, "wall_faces": self.wall_faces}


@dataclass
class PassageReading:
    passages: list[Passage]            # every top-level region, largest volume first
    labels: np.ndarray                 # per staged triangle: the passage it belongs to (-1: none)
    patch_of: list[str]                # per staged triangle: its patch name
    triangles: np.ndarray              # n x 3 x 3, in the staged order
    read: bool = True
    reason: str = ""

    @property
    def with_ports(self) -> list[Passage]:
        return [p for p in self.passages if p.ports]

    @property
    def portless(self) -> list[Passage]:
        return [p for p in self.passages if not p.ports]


def _load(src) -> np.ndarray:
    from meshpipeline.cad.stl_io import read_stl_triangles

    paths = list(src) if isinstance(src, (list, tuple)) else [src]
    tris = [t for one in paths for t in read_stl_triangles(Path(one))]
    return np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)


def read_passages(surfaces: dict, wall_name: str = "wall") -> PassageReading:
    """The passages of a staged internal surface ({patch name: STL path or list of paths}). The
    triangles are welded as the mesher reads them (float32), split into edge-connected pieces, the
    pieces standing inside another one joined to it, and each passage named by the declared
    openings (every patch but the wall) whose triangles lie on it."""
    parts = [(name, _load(src)) for name, src in surfaces.items()]
    parts = [(n, t) for n, t in parts if len(t)]
    if not parts:
        return PassageReading([], np.zeros(0, dtype=int), [], np.zeros((0, 3, 3)), read=False,
                              reason="no staged triangles")
    T = np.concatenate([t for _n, t in parts])
    patch_of = [n for n, t in parts for _ in range(len(t))]
    pts = T.reshape(-1, 3).astype(np.float32).astype(np.float64)
    uniq, inv = np.unique(pts, axis=0, return_inverse=True)
    F = np.asarray(inv).reshape(-1, 3)
    piece = _piece_labels(F, len(uniq))
    n = int(piece.max()) + 1 if len(piece) else 0
    if n > MAX_PIECES:
        labels = np.full(len(F), -1)
        return PassageReading([], labels, patch_of, T, read=False,
                              reason=f"{n} separate pieces: too many to read as passages")
    closed = _closed_pieces(F, piece, n)
    vol = np.array([_signed_volume(T[piece == k]) for k in range(n)])
    order = sorted(range(n), key=lambda k: -abs(vol[k]))
    # NESTING: a piece is inside the smallest closed piece whose winding number at one of its
    # points is about 1 (pieces bigger than it only)
    parent = [-1] * n
    for i in order:
        p = T[piece == i][0, 0]
        best = -1
        for j in order:
            if j == i or not closed[j] or abs(vol[j]) <= abs(vol[i]):
                continue
            lo, hi = T[piece == j].reshape(-1, 3).min(axis=0), T[piece == j].reshape(-1, 3).max(axis=0)
            if np.any(p < lo) or np.any(p > hi):
                continue
            if abs(_winding(T[piece == j], p)) >= INSIDE_WINDING and (best < 0 or abs(vol[j]) < abs(vol[best])):
                best = j
        parent[i] = best

    def top(k: int) -> int:
        seen = 0
        while parent[k] >= 0 and seen < n:
            k, seen = parent[k], seen + 1
        return k

    tops = [k for k in order if parent[k] < 0]
    index = {k: i for i, k in enumerate(tops)}
    labels = np.array([index[top(int(k))] for k in piece])
    names = np.asarray(patch_of)
    passages = []
    for k in tops:
        members = [m for m in range(n) if top(m) == k]
        sel = np.isin(piece, members)
        v = abs(vol[k]) - sum(abs(vol[m]) for m in members if m != k)
        P = T[sel].reshape(-1, 3)
        ports = sorted({str(x) for x in names[sel]} - {wall_name})
        passages.append(Passage(pieces=members, faces=int(sel.sum()), volume=max(v, 0.0), ports=ports,
                                bbox=(P.min(axis=0).tolist(), P.max(axis=0).tolist()),
                                closed=bool(all(closed[m] for m in members)),
                                wall_faces=int(np.sum(names[sel] == wall_name))))
    return PassageReading(passages, labels, patch_of, T)


#: A passage holding less than this share of the main passage's volume is a POCKET: a bolt hole
#: closed by its lids, a dimple read as a mouth - not a second passage anyone meant to mesh.
POCKET_SHARE = 0.01
#: At most this many separate passages are meshed in one job (a shell-and-tube bundle's tubes);
#: past it the file is a set of parts better meshed one by one, and the user is told so.
MAX_PASSAGES = 24


@dataclass
class PassagePlan:
    """What a staged internal surface is meshed as: the passages that carry declared openings, in
    meshing order (the one with the inlet first, then by volume); the closed pieces no declared
    opening touches, left out; and the declared openings that open into no passage that can be
    meshed (a lid on a pocket, a lid standing free of any closed surface)."""
    meshed: list[Passage]
    left_out: list[Passage]
    stranded: dict[str, str]           # port name -> why it opens into nothing meshable
    reading: PassageReading

    @property
    def several(self) -> bool:
        return len(self.meshed) > 1

    def as_dict(self) -> dict:
        return {"passages": [p.as_dict() for p in self.meshed],
                "left_out": [p.as_dict() for p in self.left_out],
                "stranded": dict(self.stranded), "read": self.reading.read, "reason": self.reading.reason}

    def notes(self) -> list[str]:
        out = []
        if self.several:
            out.append(f"the confirmed openings lie on {len(self.meshed)} separate passages that do not "
                       "connect; each is meshed, and the mesh holds them side by side: "
                       + "; ".join(f"passage {k + 1}: {', '.join(p.ports)}" for k, p in enumerate(self.meshed)))
            no_inlet = [k + 1 for k, p in enumerate(self.meshed) if not any(n.startswith("inlet") for n in p.ports)]
            if no_inlet:
                out.append("passage " + ", ".join(map(str, no_inlet)) + " has no opening named an inlet: give "
                           "it one before solving, or the flow there has nowhere to come from")
        if self.left_out:
            out.append(f"{len(self.left_out)} piece(s) of the surface that no confirmed opening touches were left "
                       "out of the fluid (a separate solid, a sealed cavity or a loose sheet - part of no passage)")
        return out

    def refusal(self) -> str:
        """What stops this staging before any mesher runs, in the user's words - or ""."""
        if self.stranded:
            rows = "; ".join(f"'{n}': {why}" for n, why in sorted(self.stranded.items()))
            return ("a confirmed opening opens into nothing the mesh can fill, so its patch would come back "
                    f"empty: {rows}. Mark it as not an opening on the picture (it is then part of the wall), "
                    "or move it onto the passage, then run again.")
        if len(self.meshed) > MAX_PASSAGES:
            return (f"the confirmed openings lie on {len(self.meshed)} separate passages that do not connect - "
                    f"more than the {MAX_PASSAGES} one mesh holds side by side. Mesh the passages as separate "
                    "files (or groups of them).")
        return ""


def plan_passages(reading: PassageReading, *, inlet_names: tuple[str, ...] = ()) -> PassagePlan:
    """Which passages a mesh of this staged surface fills. Every closed passage carrying a declared
    opening and holding at least POCKET_SHARE of the main one's volume is meshed; a closed piece no
    opening touches is left out (a mesher with no seed point fills the LARGEST closed region, and a
    port-less solid larger than the passage took every port's faces: the hotend nozzle's STEP); a
    declared opening on a pocket or on an open piece is stranded - it opens into nothing a mesher
    can fill, and its patch would come back with no faces."""
    if not reading.read:
        return PassagePlan(meshed=[], left_out=[], stranded={}, reading=reading)
    ported = [p for p in reading.passages if p.ports]
    if any(p.closed and not p.wall_faces for p in ported):
        # A PORT THAT IS A CLOSED SURFACE OF ITS OWN, touching no wall, is no lid the staging
        # builds (a lid is stitched to its rim): the surfaces were staged some other way, and the
        # reading cannot say what they close - the mesh is as staged
        reading = PassageReading([], reading.labels, reading.patch_of, reading.triangles, read=False,
                                 reason="a declared opening is a closed surface of its own, joined to no wall")
        return PassagePlan(meshed=[], left_out=[], stranded={}, reading=reading)
    stranded: dict[str, str] = {}
    for p in ported:
        if not p.closed and not p.wall_faces:
            for n in p.ports:
                stranded[n] = "its lid stands on its own, joined to no wall: it closes no passage"
    # an open piece WITH wall is meshed as it always was (a staging gap the mesher closes over);
    # only a closed one has a volume to weigh
    candidates = [p for p in ported if p.closed or p.wall_faces]
    closed = [p for p in candidates if p.closed]
    main = max(closed, key=lambda p: p.volume) if closed else None
    meshed = []
    for p in candidates:
        if p.closed and main is not None and p.volume < POCKET_SHARE * main.volume:
            for n in p.ports:
                stranded[n] = (f"it opens into a closed pocket {100.0 * p.volume / main.volume:.2g}% "
                               "the size of the passage, not into the passage")
            continue
        meshed.append(p)
    inlets = set(inlet_names)
    meshed.sort(key=lambda p: (not (set(p.ports) & inlets), -p.volume))
    left_out = [p for p in reading.passages if not p.ports]
    return PassagePlan(meshed=meshed, left_out=left_out, stranded=stranded, reading=reading)


def passage_surfaces(plan: PassagePlan) -> list[dict[str, list]]:
    """The staged triangles of each meshed passage, by patch name, in the plan's order."""
    r = plan.reading
    names = np.asarray(r.patch_of)
    index = {id(p): k for k, p in enumerate(r.passages)}
    out = []
    for p in plan.meshed:
        sel = r.labels == index[id(p)]
        groups: dict[str, list] = {}
        for name in dict.fromkeys(names[sel].tolist()):
            tri = r.triangles[sel & (names == name)]
            groups[str(name)] = [tuple(map(tuple, t)) for t in tri.tolist()]
        out.append(groups)
    return out


def passage_surface_files(ws: Path) -> list[Path]:
    """The surface of every further passage a case's PASSAGES_FILE lists, in its order ([] for a
    case of one passage). Raises ValueError for a record that cannot be read or names a surface
    that is missing: a case that says it has more passages than it carries is not one to trust."""
    import json

    rec = Path(ws) / PASSAGES_FILE
    if not rec.exists():
        return []
    try:
        runs = json.loads(rec.read_text()).get("runs") or []
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"{PASSAGES_FILE} is unreadable: {exc}") from exc
    out = []
    for rel in runs:
        surf = Path(ws) / str(rel) / "geom.stl"
        if not surf.exists():
            raise ValueError(f"the surface of passage {rel} is missing")
        out.append(surf)
    return out


def plan_staged(ws: Path, srcs: dict, wall_name: str, declaration: list | None) -> PassagePlan | None:
    """The passage plan of a staged internal surface, recorded beside the case as
    PASSAGES_READ_FILE (what was meshed, left out or refused, and why); None when the surface
    cannot be read at all, which changes nothing - the mesh is then as staged."""
    import json
    import logging

    inlets = tuple(str(p.get("name")) for p in declaration or []
                   if isinstance(p, dict) and p.get("type") == "inlet")
    try:
        plan = plan_passages(read_passages(srcs, wall_name), inlet_names=inlets)
    except Exception:  # noqa: BLE001 - a reading that fails is no evidence; the staging stands
        logging.getLogger(__name__).warning("the staged passages could not be read", exc_info=True)
        return None
    try:
        (Path(ws) / PASSAGES_READ_FILE).write_text(json.dumps(
            {**plan.as_dict(), "notes": plan.notes(), "refusal": plan.refusal()}, indent=1))
    except OSError:
        pass
    return plan


def write_passage(group: dict[str, list], out_dir: Path, prefix: str = "") -> dict[str, str]:
    """One passage's triangles written one binary STL per patch: {patch name: path}."""
    from meshpipeline.cad.stl_io import write_stl_binary

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out = {}
    for name, tris in group.items():
        path = Path(out_dir) / f"{prefix}{name}.stl"
        write_stl_binary(path, tris)
        out[name] = str(path)
    return out


#: The proposal is vetted only on a skin this small: the staging it runs is the mesher's own, and
#: the geometry check must stay seconds, not minutes.
VET_MAX_TRIANGLES = 400_000
VET_MAX_OPENINGS = 32


def stranded_openings(surface_path: Path, patches: list[dict]) -> dict[str, str]:
    """THE CHECK'S PROPOSAL HELD TO THE MESHER'S STAGING: the declared openings (intake patches,
    the shape geometry_confirmation.patches_from makes) that would open into nothing a mesher can
    fill - a bolt hole through a flange, whose lid stands on the outer skin joined to no wall of
    the passage; a dimple closed into a pocket. {name: why}; {} when the skin is too large to stage
    quickly, when the staging refuses (its own words reach the user at meshing), or on any error:
    the vetting can only take a wrong opening away, never add a failure."""
    import tempfile

    from meshpipeline.cad.internal_surface import InternalSurfaceError, stage_triangles, write_staged
    from meshpipeline.cad.scout_mesh import read_triangles

    ports = [p for p in patches if p.get("type") in ("inlet", "outlet")]
    if len(ports) < 2 or len(ports) > VET_MAX_OPENINGS:
        return {}
    try:
        p = Path(surface_path)
        if (p.stat().st_size - 84) // 50 > VET_MAX_TRIANGLES:
            return {}
        staged = stage_triangles(read_triangles(p), intake_patches=patches, surface_path=p, leak_check=False)
        with tempfile.TemporaryDirectory(prefix="vet_") as tmp:
            rec = write_staged(staged, Path(tmp))
            plan = plan_passages(read_passages(rec["stls"], rec.get("wall_name") or "wall"))
    except InternalSurfaceError:
        return {}
    except Exception:  # noqa: BLE001 - a vetting that fails leaves the proposal as it was
        import logging
        logging.getLogger(__name__).warning("the proposal could not be vetted against the staging", exc_info=True)
        return {}
    return dict(plan.stranded)


def passage_count(tris, patches: list[dict], *, input_kind: str = "") -> int | None:
    """HOW MANY SEPARATE PASSAGES the mesher's staging would mesh for these declared openings - the
    count every engine's capability is held to (EngineSpec: FlowSupport.max_passages) before an
    engine is recommended or switched to. `tris` is the surface in METRES (the staging binds an
    opening by its near_mm / 1000); `input_kind` is what the user confirmed the file is. Run through the very staging the internal drivers use
    (cad/internal_surface, then plan_passages), on a skin small enough to stage in seconds. None
    when it cannot be told - fewer than two openings, a skin or a declaration too large, a staging
    that refuses (its own words reach the user at meshing), any error: a count is evidence, never
    a failure."""
    import tempfile

    from meshpipeline.cad.internal_surface import InternalSurfaceError, stage_triangles, write_staged

    ports = [p for p in patches or [] if isinstance(p, dict) and p.get("type") in ("inlet", "outlet")]
    if len(ports) < 2 or len(ports) > VET_MAX_OPENINGS:
        return None
    try:
        tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
        if len(tris) > VET_MAX_TRIANGLES:
            return None
        staged = stage_triangles(tris, intake_patches=[dict(p) for p in patches if isinstance(p, dict)],
                                 input_kind=input_kind, leak_check=False)
        with tempfile.TemporaryDirectory(prefix="passages_") as tmp:
            rec = write_staged(staged, Path(tmp))
            plan = plan_passages(read_passages(rec["stls"], rec.get("wall_name") or "wall"))
    except InternalSurfaceError:
        return None
    except Exception:  # noqa: BLE001 - a count that cannot be made is no evidence
        import logging
        logging.getLogger(__name__).info("the passages could not be counted", exc_info=True)
        return None
    return len(plan.meshed) if plan.reading.read and plan.meshed else None


def staged_passage_count(ws) -> int | None:
    """How many separate passages an attempt's own staging meshed (or refused to), from the
    PASSAGES_READ_FILE plan_staged writes beside every internal case it reads - the count the
    mesher itself saw. None when the workspace holds no reading, or one that could not read the
    surface."""
    import json

    try:
        rec = json.loads((Path(ws) / PASSAGES_READ_FILE).read_text())
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(rec, dict) or not rec.get("read"):
        return None
    passages = rec.get("passages")
    return len(passages) if isinstance(passages, list) and passages else None


def _piece_labels(F: np.ndarray, n_verts: int) -> np.ndarray:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    nf = len(F)
    if nf == 0:
        return np.zeros(0, dtype=int)
    # faces sharing an edge are one piece
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    key = e[:, 0].astype(np.int64) * (n_verts + 1) + e[:, 1]
    owner = np.tile(np.arange(nf), 3)
    order = np.argsort(key, kind="stable")
    ks, os_ = key[order], owner[order]
    same = ks[1:] == ks[:-1]
    a, b = os_[:-1][same], os_[1:][same]
    g = coo_matrix((np.ones(len(a)), (a, b)), shape=(nf, nf))
    _, lab = connected_components(g, directed=False)
    return lab


def _closed_pieces(F: np.ndarray, piece: np.ndarray, n: int) -> list[bool]:
    out = []
    for k in range(n):
        f = F[piece == k]
        e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
        _, counts = np.unique(e, axis=0, return_counts=True)
        out.append(bool(len(counts)) and bool(np.all(counts >= 2)))
    return out


def _signed_volume(T: np.ndarray) -> float:
    if not len(T):
        return 0.0
    return float(np.einsum("ij,ij->i", T[:, 0], np.cross(T[:, 1], T[:, 2])).sum() / 6.0)


def _winding(T: np.ndarray, p: np.ndarray) -> float:
    a, b, c = T[:, 0] - p, T[:, 1] - p, T[:, 2] - p
    la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
    num = np.einsum("ij,ij->i", a, np.cross(b, c))
    den = la * lb * lc + np.einsum("ij,ij->i", a, b) * lc + np.einsum("ij,ij->i", b, c) * la \
        + np.einsum("ij,ij->i", c, a) * lb
    return float(np.sum(2.0 * np.arctan2(num, den))) / (4.0 * math.pi)


__all__ = ["INSIDE_WINDING", "MAX_PASSAGES", "MAX_PIECES", "PASSAGES_FILE", "PASSAGES_READ_FILE", "POCKET_SHARE", "Passage", "PassagePlan", "PassageReading",
           "passage_count", "passage_surface_files", "passage_surfaces", "plan_passages", "plan_staged",
           "read_passages", "staged_passage_count", "stranded_openings", "write_passage"]
