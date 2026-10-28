# Responsibility: Write an FEA ASSEMBLY's mesh for each version-1 solver - Abaqus / CalculiX (.inp), Nastran (.bdf) and Ansys MAPDL (.cdb): every part its own element set (and property / material number) so a material can be given per part, every face of every part a named set (Part_k_Face_j), and the parts' joints - the shared surfaces of a bonded mesh, or the named contact (or tie) surface pairs of parts meshed apart, in each solver's own words.
# Boundaries: pure over the internal mesh (abaqus_deck.SolidMesh) and the staged assembly's sidecar (cad/fea_assembly.stage); it knows no mesher. The Nastran and Ansys files, and Abaqus, are checked by reading them back and by each format's rules, never in their solver (no licence): every one says "not yet tested in the real solver". CalculiX solves for real.
# Collaborates with: engines/gmsh/fea_adapter.py (writes all three after an assembly is meshed), engines/gmsh/abaqus_deck.py (element faces, numbering, labels), engines/fea_writers.py (the single-part writers and their read-back), cad/fea_assembly.py (the parts and pairs).
"""AN ASSEMBLY, WRITTEN FOR ITS SOLVER.

Elements are numbered part by part (Part_1's first), so each part is one run of numbers. In every
file:

* PARTS: Abaqus/CalculiX *ELSET Part_k (and ELSET=solid for all); Nastran PSOLID k -> MAT1 k on
  the part's CTETRAs; Ansys material k on the part's SOLID187s and an element component PART_K.
* FACES: Part_k_Face_j for every face of every part (a *SURFACE + *NSET; SET3 + BSURFS; a node
  component), on the part's own side of a face it shares with another part.
* BONDED (one conformal mesh): the parts share their nodes where they touch, so nothing more is
  needed to join them. Each joint's shared faces are named on each side (Bond_n_Part_k) to read
  the joint's stresses from.
* CONTACT (each part meshed on its own): each touching pair is a contact pair, slave the smaller
  surface: Abaqus/CalculiX *SURFACE + *SURFACE INTERACTION (hard, frictionless: a default to change)
  + *CONTACT PAIR; Nastran (MSC, SOL 101/400) BCPROP + BCBODY per part, BCTABLE per pair, BCPARA,
  with the surfaces also as BSURFS; Ansys TARGE170 / CONTA174 elements on the two surfaces (one
  real constant set per pair) and components naming them.
* TIE (bonded where the parts are not one mesh: meshed apart, or a small gap or a missed touch in a
  fused mesh): as contact, but the pair is tied: Abaqus/CalculiX *TIE; Nastran BCTABLE with IGLUE=1;
  Ansys CONTA174 KEYOPT(12)=5 (bonded). Every tie is settled first (settle_ties, PLAN-fea-18): the
  same slave, nodes and tolerance in every file, and no node moved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines.gmsh.abaqus_deck import (
    TET10_FACE_NODES,
    TET_FACE_CORNERS,
    FaceSet,
    SolidMesh,
    TetFaceIndex,
    _coord,
    _write_ids,
)

NOT_TESTED = "not yet tested in the real solver"
#: The contact interaction every pair is written with: a default for the user to change.
INTERACTION = "Contact_hard_frictionless"
#: Nastran ids past the faces' own (1..N) for the joints' surfaces.
JOINT_ID_BASE = 100_000


def ascii_name(name: str) -> str:
    """A CAD name as the solver files can carry it: plain ASCII ("Gehäuse" -> "Gehause"), every other
    character dropped; the files are written as ASCII, as every solver reads them."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join("".join(c for c in plain if c.isprintable()).split())


@dataclass
class Joint:
    """One joint as written: its name, how it is joined, and its two sides (slave first)."""
    name: str
    kind: str                     # "bonded" | "contact" | "tie"
    parts: tuple[int, int]        # (slave part, master part)
    sides: tuple[FaceSet, FaceSet]
    overlap: bool = False
    #: A SMALL-GAP JOINT (PLAN-fea-18): how far the solver must reach across the measured gap to join
    #: every node of it (the widest gap and a margin, cad/fea_assembly.GAP_REACH_MARGIN); None for
    #: parts that touch.
    reach: float | None = None
    #: A TIE AS SETTLED (settle_ties): the slave's nodes that are tied - each within `tolerance` of the
    #: master surface, none the master's own, none tied twice - and the tolerance every solver is given.
    #: None: not settled (written with every node of the slave surface).
    tied: np.ndarray | None = None
    tolerance: float | None = None
    #: why the slave's other nodes are not tied: {"shared": already the master's own nodes (one conformal
    #: mesh), "beyond": farther than the tolerance from the master, "twice": tied by a nearer tie, "chain": a
    #: node of another tie's master surface}
    left_out: dict = field(default_factory=dict)
    #: the slave's nodes left out on the joint itself (shared, held by a nearer tie, on a master surface): the
    #: Ansys contact elements keep off every face that holds one
    held_out: np.ndarray | None = None


@dataclass
class Prepared:
    node_ids: np.ndarray
    xyz: np.ndarray
    elements: np.ndarray          # (E, 4|10), solver order, numbered 1..E, grouped by part
    element_part: np.ndarray      # (E,) the part of each element
    parts: list[dict] = field(default_factory=list)    # {"n", "id", "name", "first", "last"}
    faces: list[FaceSet] = field(default_factory=list)  # Part_k_Face_j, part by part
    joints: list[Joint] = field(default_factory=list)
    #: MESH-8: each CAD face left out of the mesh as a skin of no thickness, and the face it lies on
    #: ({"face", "on", "words"}): accounted for in every file's notes, never a missing named face
    skins: list[dict] = field(default_factory=list)


def skin_lines(prep: Prepared, mark: str) -> list[str]:
    """MESH-8's skins in a file's comment lines (`mark` the format's comment): each face left out and the face it
    lies on, so a reader counting the named faces against the CAD's knows where the rest went."""
    if not prep.skins:
        return []
    n = len(prep.skins)
    out = [f"{mark} MESH-8: {n} CAD face{'s' if n != 1 else ''} of no thickness (a skin lying on another face of its "
           "part, no volume) left out of the mesh - no named set, accounted for:"]
    out += [f"{mark}   {s.get('words') or (str(s.get('face')) + ' lies on ' + str(s.get('on')))}" for s in prep.skins]
    return out


def _face_set(name: str, elements: np.ndarray, tris: np.ndarray, allowed: np.ndarray,
              index: TetFaceIndex | None = None) -> FaceSet | None:
    """The element faces under `tris` among the `allowed` elements (a part's own side). `index` is
    those elements' faces sorted once (TetFaceIndex), for many face sets on one side."""
    if not len(tris):
        return None
    pick = np.flatnonzero(allowed)
    e, lab = (index or TetFaceIndex(elements[pick])).lookup(tris)
    e = pick[e]
    table = np.asarray(TET10_FACE_NODES if elements.shape[1] == 10 else TET_FACE_CORNERS)
    nodes = np.unique(elements[e[:, None], table[lab - 1]])
    order = np.lexsort((lab, e))
    return FaceSet(name=name, tag=0, elements=e[order] + 1, labels=lab[order], nodes=nodes)


def _union(name: str, sets: list[FaceSet]) -> FaceSet:
    e = np.concatenate([s.elements for s in sets]) if sets else np.zeros(0, dtype=np.int64)
    lab = np.concatenate([s.labels for s in sets]) if sets else np.zeros(0, dtype=np.int64)
    key = np.unique(np.stack([e, lab], axis=1), axis=0) if len(e) else np.zeros((0, 2), dtype=np.int64)
    nodes = np.unique(np.concatenate([s.nodes for s in sets])) if sets else np.zeros(0, dtype=np.int64)
    return FaceSet(name=name, tag=0, elements=key[:, 0], labels=key[:, 1], nodes=nodes)


def _area(prep_xyz_of, fs: FaceSet, elements: np.ndarray) -> float:
    if not len(fs.elements):
        return 0.0
    conn = elements[fs.elements - 1]
    tri = np.asarray(TET_FACE_CORNERS)[fs.labels - 1]
    a, b, c = (prep_xyz_of(conn[np.arange(len(conn)), tri[:, k]]) for k in range(3))
    return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())


def prepare(mesh: SolidMesh, sidecar: dict, *, surface_tag: dict[int, int], volume_tag: dict[int, int]) -> Prepared:
    """Number the assembly's mesh once for every file. `surface_tag` / `volume_tag` give the mesh's
    face / solid tag of each sidecar surface / volume (index, 0-based)."""
    vol_parts = {int(volume_tag[k]): [int(p) for p in v.get("parts") or []]
                 for k, v in enumerate(sidecar.get("volumes") or []) if k in volume_tag}
    raw_vol = np.asarray(mesh.element_volume, dtype=np.int64)
    owner = np.array([min(vol_parts.get(int(v)) or [0]) for v in raw_vol], dtype=np.int64)
    order = np.argsort(owner, kind="stable")
    elements = np.asarray(mesh.elements, dtype=np.int64)[order]
    owner = owner[order]
    vol = raw_vol[order]
    ids = np.asarray(mesh.node_ids, dtype=np.int64)
    used = np.zeros(int(ids.max()) + 1, dtype=bool)
    used[elements.ravel()] = True
    keep = used[ids]
    o = np.argsort(ids[keep])
    node_ids = ids[keep][o]
    xyz = np.asarray(mesh.xyz, dtype=float)[keep][o]
    index = np.zeros(int(ids.max()) + 1, dtype=np.int64)
    index[node_ids] = np.arange(len(node_ids))

    def xyz_of(nids):
        return xyz[index[nids]]

    parts_meta = sidecar.get("parts") or []
    num = np.arange(1, len(elements) + 1)
    parts = []
    for n in sorted(set(owner.tolist())):
        sel = num[owner == n]
        meta = parts_meta[n - 1] if 0 < n <= len(parts_meta) else {}
        parts.append({"n": int(n), "id": f"Part_{n}", "name": ascii_name(str(meta.get("name") or "")),
                      "first": int(sel[0]), "last": int(sel[-1])})
    # a part's side: the elements of every solid it has a share in (an overlap piece counts for both)
    side = {p["n"]: np.isin(vol, [v for v, ps in vol_parts.items() if p["n"] in ps]) for p in parts}
    indexes: dict[int, TetFaceIndex] = {}

    def index_of(n: int) -> TetFaceIndex:
        # each part's element faces sorted ONCE, for every face set on its side
        if n not in indexes:
            indexes[n] = TetFaceIndex(elements[np.flatnonzero(side[n])])
        return indexes[n]

    # every named face: the sidecar surfaces that carry its name
    by_name: dict[str, list[int]] = {}
    for k, s in enumerate(sidecar.get("surfaces") or []):
        if k not in surface_tag:
            continue
        for nm in s.get("names") or []:
            by_name.setdefault(str(nm), []).append(int(surface_tag[k]))

    def tris_of(tags: list[int]) -> np.ndarray:
        blocks = [np.asarray(mesh.faces[t], dtype=np.int64)[:, :3] for t in tags if t in mesh.faces]
        return np.concatenate(blocks) if blocks else np.zeros((0, 3), dtype=np.int64)

    def part_of(name: str) -> int:
        try:
            return int(name.split("_")[1])
        except (IndexError, ValueError):
            return 0

    faces: list[FaceSet] = []
    named: dict[str, FaceSet] = {}
    for name in sorted(by_name, key=lambda s: (part_of(s), int(s.rsplit("_", 1)[-1]) if s.rsplit("_", 1)[-1].isdigit() else 0)):
        p = part_of(name)
        if p not in side:
            continue
        fs = _face_set(name, elements, tris_of(by_name[name]), side[p], index_of(p))
        if fs is not None:
            faces.append(fs)
            named[name] = fs

    joints: list[Joint] = []
    for pair in sidecar.get("pairs") or []:
        p, q = (int(x) for x in pair.get("parts") or (0, 0))
        if p not in side or q not in side:
            continue
        kind = str(pair.get("kind"))
        jid = str(pair.get("id") or f"Interface_{len(joints) + 1}").replace("Interface", "")
        if kind == "bonded":
            tags = [int(surface_tag[k]) for k in pair.get("surfaces") or [] if k in surface_tag]
            a = _face_set(f"Bond{jid}_Part_{p}", elements, tris_of(tags), side[p], index_of(p))
            b = _face_set(f"Bond{jid}_Part_{q}", elements, tris_of(tags), side[q], index_of(q))
            if a is not None and b is not None:
                joints.append(Joint(name=f"Bond{jid}", kind=kind, parts=(p, q), sides=(a, b)))
            continue
        word = "Tie" if kind == "tie" else "Contact"
        fa = pair.get("faces") or {}
        a = _union(f"{word}{jid}_Part_{p}", [named[n] for n in fa.get(str(p)) or [] if n in named])
        b = _union(f"{word}{jid}_Part_{q}", [named[n] for n in fa.get(str(q)) or [] if n in named])
        if not len(a.elements) or not len(b.elements):
            continue
        # SLAVE = the smaller surface (the usual rule: the smaller side follows the larger); a tie's own
        # slave is then chosen on its nodes (settle_ties)
        if _area(xyz_of, a, elements) > _area(xyz_of, b, elements):
            a, b, p, q = b, a, q, p
        reach = float(pair.get("reach") or 0.0)
        joints.append(Joint(name=f"{word}{jid}", kind=kind, parts=(p, q), sides=(a, b),
                            overlap=bool(pair.get("overlap")), reach=reach if reach > 0 else None))
    joints = settle_ties(joints, elements, xyz, index, side)
    from meshpipeline.cad.fea_assembly import skin_pairs
    return Prepared(node_ids=node_ids, xyz=xyz, elements=elements, element_part=owner, parts=parts,
                    faces=faces, joints=joints, skins=skin_pairs(sidecar.get("skins")))


#: A TOUCHING TIE'S TOLERANCE, as a share of the master's typical element edge: parts that touch are
#: tied where their meshes lie on each other, and a quadratic face's facets lie off the round CAD face
#: they mesh by a sliver of an edge. A small-gap joint's own reach (PLAN-fea-18) is used instead.
TOUCH_SHARE = 0.05
#: PARTS THAT PASS INTO EACH OTHER (an overlap pair: a press fit, a pin drawn a hair too big): the slave's
#: nodes inside the master part are the joint, tied where they stand down to this share of the master's
#: typical element edge (asm_fw_case, 28 Oct: its five press-fit pins sit 0.23-0.3 of an edge deep, the
#: deepest 1.0); a node deeper is not on the joint.
OVERLAP_DEPTH_SHARE = 1.0
#: a tet face's corners (TET_FACE_CORNERS) leave out this corner: the one inside, behind the face
_OPPOSITE = np.array([3, 2, 0, 1])


def _depth(points: np.ndarray, face: np.ndarray, fs: FaceSet, elements: np.ndarray, xyz_of) -> np.ndarray:
    """How far each point lies behind the plane of its element face of `fs` (`face`: the face's row),
    into that face's own element: > 0 inside the part."""
    conn = elements[fs.elements[face] - 1]
    corners = np.take_along_axis(conn, np.asarray(TET_FACE_CORNERS)[fs.labels[face] - 1], axis=1)
    a, b, c = (xyz_of(corners[:, k]) for k in range(3))
    inner = xyz_of(conn[np.arange(len(conn)), _OPPOSITE[fs.labels[face] - 1]])
    n = np.cross(b - a, c - a)
    n /= np.maximum(np.linalg.norm(n, axis=1), 1e-300)[:, None]
    n[np.einsum("ij,ij->i", n, inner - a) < 0] *= -1                     # into the element
    return np.einsum("ij,ij->i", points - a, n)


def _master_triangles(fs: FaceSet, elements: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The master surface as flat triangles (a quadratic face as its four corner/midpoint triangles, as
    CalculiX itself triangulates it): (T, 3) node numbers, (T,) the element face each came from, and
    (F, 6|3) every node of each element face."""
    conn = elements[fs.elements - 1]
    if elements.shape[1] == 10:
        table = np.asarray(TET10_FACE_NODES)[fs.labels - 1]
        nodes = np.take_along_axis(conn, table, axis=1)                      # c1 c2 c3 m12 m23 m31
        sub = np.array([[0, 3, 5], [3, 1, 4], [5, 4, 2], [3, 4, 5]])
        tris = nodes[:, sub].reshape(-1, 3)
        face = np.repeat(np.arange(len(nodes)), 4)
    else:
        nodes = np.take_along_axis(conn, np.asarray(TET_FACE_CORNERS)[fs.labels - 1], axis=1)
        tris, face = nodes, np.arange(len(nodes))
    return tris, face, nodes


#: A slave node is tied only where it lies OVER the master: the foot of its perpendicular inside a master
#: triangle, to this share of the triangle (barycentric). CalculiX ties no node whose foot falls off the
#: master ("no opposite master face found"): a node beside the master's edge is not on the joint, however
#: near (the customer anchorage, 28 Oct: 5,100 such nodes in its boss-to-tube ties, listed and never tied).
IN_FACE = 1e-6


def _point_triangle(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The distance from each point to the plane of its triangle (all (N, 3)) where the foot of the
    perpendicular falls inside the triangle (IN_FACE); inf where it falls outside."""
    ab, ac = b - a, c - a
    n = np.cross(ab, ac)
    nn = np.einsum("ij,ij->i", n, n)
    ap = p - a
    w = np.full(len(p), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        # barycentric coordinates of the foot of the perpendicular
        u = np.einsum("ij,ij->i", np.cross(ap, ac), n) / nn
        v = np.einsum("ij,ij->i", np.cross(ab, ap), n) / nn
        inside = (nn > 0) & (u >= -IN_FACE) & (v >= -IN_FACE) & (u + v <= 1 + IN_FACE)
        w[inside] = np.abs(np.einsum("ij,ij->i", ap[inside], n[inside])) / np.sqrt(nn[inside])
    return w


def _nearest_edge(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The nearest point of each triangle's edges to each point, and its distance (all (N, 3))."""
    best = np.full(len(p), np.inf)
    at = np.zeros_like(p)
    for x, y in ((a, b), (b, c), (c, a)):
        d = y - x
        dd = np.einsum("ij,ij->i", d, d)
        t = np.clip(np.einsum("ij,ij->i", p - x, d) / np.where(dd > 0, dd, 1.0), 0.0, 1.0)
        q = x + t[:, None] * d
        r = np.linalg.norm(p - q, axis=1)
        closer = r < best
        best[closer] = r[closer]
        at[closer] = q[closer]
    return at, best


def _nearest_master(points: np.ndarray, tri_xyz: np.ndarray, limit: float,
                    tri_ids: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """For each point: its distance to the nearest master triangle it lies over, within `limit` (inf if
    none), and that triangle's row (-1 if none). `tri_ids` (the triangles' node numbers): a point over a
    convex crease of the master - in the wedge no flat facet's perpendicular reaches, as a curved face's
    would - counts too, at its distance to the crease when that is the nearer; never a point beside the
    master's own edge."""
    from scipy.spatial import cKDTree

    dist = np.full(len(points), np.inf)
    best = np.full(len(points), -1, dtype=np.int64)
    if not len(points) or not len(tri_xyz):
        return dist, best
    centre = tri_xyz.mean(axis=1)
    radius = np.linalg.norm(tri_xyz - centre[:, None, :], axis=2).max(axis=1)
    # TRIANGLES OF A SIZE TOGETHER (radius classes a factor 2 apart): each class searched only as far as its
    # own largest triangle reaches, so one coarse facet never widens the search for every node
    cls = np.floor(np.log2(np.maximum(radius, 1e-300) / max(float(radius.max()), 1e-300))).astype(np.int64)
    # the nearest point of the master's EDGES, for the points over no facet (the crease wedges)
    edge_d = np.full(len(points), np.inf)
    edge_at = np.zeros_like(points)
    edge_tri = np.full(len(points), -1, dtype=np.int64)
    for c in np.unique(cls):
        rows = np.flatnonzero(cls == c)
        tree = cKDTree(centre[rows])
        reach = limit + float(radius[rows].max())
        for s in range(0, len(points), 100_000):
            near = tree.query_ball_point(points[s:s + 100_000], r=reach)
            counts = np.fromiter((len(x) for x in near), dtype=np.int64, count=len(near))
            if not counts.sum():
                continue
            pp = s + np.repeat(np.arange(len(near)), counts)
            tt = rows[np.fromiter((t for x in near for t in x), dtype=np.int64, count=int(counts.sum()))]
            d = _point_triangle(points[pp], tri_xyz[tt, 0], tri_xyz[tt, 1], tri_xyz[tt, 2])
            if tri_ids is not None:
                q, e = _nearest_edge(points[pp], tri_xyz[tt, 0], tri_xyz[tt, 1], tri_xyz[tt, 2])
                order = np.lexsort((e, pp))
                f = order[np.r_[True, pp[order][1:] != pp[order][:-1]]]
                closer = e[f] < edge_d[pp[f]]
                edge_d[pp[f][closer]] = e[f][closer]
                edge_at[pp[f][closer]] = q[f][closer]
                edge_tri[pp[f][closer]] = tt[f][closer]
            order = np.lexsort((d, pp))
            pp, tt, d = pp[order], tt[order], d[order]
            first = np.r_[True, pp[1:] != pp[:-1]]
            pp, tt, d = pp[first], tt[first], d[first]
            better = d < dist[pp]
            dist[pp[better]] = d[better]
            best[pp[better]] = tt[better]
    if tri_ids is not None:
        wedge = np.flatnonzero((edge_d <= limit) & (edge_d < dist))
        if len(wedge):
            # a crease inside the master, not its rim: the nearest point lies on no edge of one triangle only
            e = np.sort(np.concatenate([tri_ids[:, [0, 1]], tri_ids[:, [1, 2]], tri_ids[:, [2, 0]]]), axis=1)
            key, count = np.unique(e, axis=0, return_counts=True)
            rim = key[count == 1]
            if len(rim):
                ids = np.unique(tri_ids)
                where = np.zeros((int(ids.max()) + 1, 3))
                where[tri_ids.ravel()] = tri_xyz.reshape(-1, 3)
                a, b = where[rim[:, 0]], where[rim[:, 1]]
                mid = 0.5 * (a + b)
                half = 0.5 * float(np.linalg.norm(b - a, axis=1).max())
                near = cKDTree(mid).query_ball_point(edge_at[wedge], r=half + 1e-12)
                on_rim = np.zeros(len(wedge), dtype=bool)
                for k, cand in enumerate(near):
                    if cand:
                        _q, r = _nearest_edge(np.repeat(edge_at[wedge][k:k + 1], len(cand), axis=0), a[cand], b[cand], b[cand])
                        on_rim[k] = bool((r <= 1e-9 * max(half, 1e-300)).any())
                wedge = wedge[~on_rim]
            dist[wedge] = edge_d[wedge]
            best[wedge] = edge_tri[wedge]
    return dist, best


def settle_ties(joints: list[Joint], elements: np.ndarray, xyz: np.ndarray, index: np.ndarray,
                side: dict) -> list[Joint]:
    """EVERY TIE AS EVERY SOLVER CAN TAKE IT (PLAN-fea-18): which side is the slave, which of its nodes
    are tied, and how far the solver reaches. The slave is the side with more nodes within the tolerance
    of the other (the finer mesh on the joint; the smaller surface when even). A slave node is tied when

    * it is not the master's own node: where two parts already share nodes (a bonded mesh's fused
      joint) they are one piece there, and a node tied to a face it lies on is an equation with nothing
      on its dependent side (CalculiX: "zero coefficient on the dependent side");
    * it lies within the tolerance of the master surface: a small-gap joint's reach, or a sliver of the
      master's element edge where the parts touch (TOUCH_SHARE), or - parts that pass into each other -
      inside the master part down to OVERLAP_DEPTH_SHARE of its edge; a node beyond is not on the joint;
    * no nearer tie already holds it: a node follows one master only;
    * it is no node of another tie's master surface: a master is never a slave too. A chain of ties (a
      slave whose master's nodes are themselves tied on) comes back round wherever the parts are also
      fused, and CalculiX then loops on "zero coefficient" without end (h_g_c0003, 28 Oct: Part_1 tied to
      Part_2 and Part_6, Part_2 to Part_3, Part_3 fused to Part_1 and Part_6, Part_2 and Part_6 one piece
      where they overlap). Leaving out only the nodes a tied node lies over did not stop it there: the
      whole master surface is kept out.

    Every solver ties the nodes where they stand (CalculiX ADJUST=NO): moving them onto the master
    would move a corner without its midpoints and turn elements inside out ("nonpositive jacobian").
    A tie with no node left but shared ones is a bonded joint: written as one, its faces still named.
    """
    ties = [k for k, j in enumerate(joints) if j.kind == "tie"]
    if not ties:
        return joints
    on_part: dict[int, np.ndarray] = {}

    def nodes_on(part: int) -> np.ndarray:
        if part not in on_part:
            mask = np.zeros(int(index.shape[0]), dtype=bool)
            mask[elements[side[part]].ravel()] = True
            on_part[part] = mask
        return on_part[part]

    def reach(slave: FaceSet, master: FaceSet, master_part: int, gap: float | None, overlap: bool):
        """(nodes within the tolerance, their distances, their master faces' nodes, shared, beyond, tolerance)"""
        tris, face, face_nodes = _master_triangles(master, elements)
        corners = xyz[index[face_nodes[:, :3]]]
        edge = np.linalg.norm(corners - np.roll(corners, 1, axis=1), axis=2)
        typical = float(np.median(edge)) if edge.size else 0.0
        limit = float(gap) if gap else TOUCH_SHARE * typical
        # parts that pass into each other: the slave's nodes inside the master, down to OVERLAP_DEPTH_SHARE
        deep = max(limit, OVERLAP_DEPTH_SHARE * typical) if overlap else limit
        cand = slave.nodes[~nodes_on(master_part)[slave.nodes]]
        dist, tri = _nearest_master(xyz[index[cand]], xyz[index[tris]], deep, tri_ids=tris)
        ok = dist <= limit
        if deep > limit:
            near = np.flatnonzero(~ok & (dist <= deep))
            inside = _depth(xyz[index[cand[near]]], face[tri[near]], master, elements, lambda n: xyz[index[n]]) > 0
            ok[near[inside]] = True
        return (cand[ok], dist[ok], int(len(slave.nodes) - len(cand)), int((~ok).sum()), deep,
                slave.nodes[nodes_on(master_part)[slave.nodes]])

    rows: list[tuple[np.ndarray, np.ndarray, int]] = []                     # (node, distance, tie)
    shared: dict[int, int] = {}
    beyond: dict[int, int] = {}
    tolerance: dict[int, float] = {}
    own: dict[int, np.ndarray] = {}                                     # each slave's nodes the master shares
    joints = list(joints)
    # SLAVE AND MASTER: the slave is the side with more nodes on the joint - the finer mesh there - so the
    # joint is tied all over, not at the coarser side's few nodes (equal: the smaller surface, as staged) ...
    both = {k: (reach(joints[k].sides[0], joints[k].sides[1], joints[k].parts[1], joints[k].reach, joints[k].overlap),
                reach(joints[k].sides[1], joints[k].sides[0], joints[k].parts[0], joints[k].reach, joints[k].overlap))
            for k in ties}
    flip = {k: len(both[k][1][0]) > len(both[k][0][0]) for k in ties}

    def sides_of(k: int) -> tuple[FaceSet, FaceSet]:
        s = joints[k].sides
        return (s[1], s[0]) if flip[k] else (s[0], s[1])

    # ... unless that makes it a master elsewhere: a surface that is one tie's master is every tie's master
    # where it can be (a master is never a slave too - NO CHAINS below)
    for _round in range(4):
        changed = False
        for k in ties:
            others = [sides_of(m)[1].nodes for m in ties if m != k and len(both[m][int(flip[m])][0])]
            if not others:
                continue
            taken = np.unique(np.concatenate(others))
            slave, master = sides_of(k)
            if np.isin(slave.nodes, taken).sum() > np.isin(master.nodes, taken).sum() and len(both[k][int(not flip[k])][0]):
                flip[k] = not flip[k]
                changed = True
        if not changed:
            break
    for k in ties:
        j = joints[k]
        got = both[k][int(flip[k])]
        if flip[k]:
            joints[k] = Joint(name=j.name, kind=j.kind, parts=(j.parts[1], j.parts[0]), sides=(j.sides[1], j.sides[0]),
                              overlap=j.overlap, reach=j.reach)
        rows.append((got[0], got[1], k))
        shared[k], beyond[k], tolerance[k] = got[2], got[3], got[4]
        own[k] = got[5]
    node = np.concatenate([r[0] for r in rows])
    dist = np.concatenate([r[1] for r in rows])
    tie_of = np.concatenate([np.full(len(r[0]), r[2]) for r in rows])
    # A NODE FOLLOWS ONE MASTER: the nearest (then the first tie)
    order = np.lexsort((tie_of, dist, node))
    node, dist, tie_of = node[order], dist[order], tie_of[order]
    first = np.r_[True, node[1:] != node[:-1]] if len(node) else np.zeros(0, dtype=bool)
    twice = np.bincount(tie_of[~first], minlength=len(joints)) if len(node) else np.zeros(len(joints), dtype=np.int64)
    lost = {k: node[~first][tie_of[~first] == k] for k in ties}
    node, dist, tie_of = node[first], dist[first], tie_of[first]
    # NO CHAINS: every node of the master surface of a tie that still ties something is that tie's master,
    # never a slave anywhere
    live = sorted(set(tie_of.tolist()))
    masters = np.unique(np.concatenate([joints[k].sides[1].nodes for k in live] or [np.zeros(0, dtype=np.int64)]))
    chain = np.isin(node, masters)
    keep = ~chain
    out = list(joints)
    for k in ties:
        j = joints[k]
        mine = keep & (tie_of == k)
        tied = np.sort(node[mine])
        left = {"shared": shared[k], "beyond": beyond[k], "twice": int(twice[k]),
                "chain": int((chain & (tie_of == k)).sum())}
        held_out = np.unique(np.concatenate([own[k], lost[k], node[chain & (tie_of == k)]]))
        if not len(tied) and shared[k]:
            # THE TWO SIDES ARE ONE MESH THERE (a fused, bonded joint the measurement also found): bonded
            name = j.name.replace("Tie", "Bond", 1)
            a, b = (FaceSet(name=s.name.replace(j.name, name, 1), tag=s.tag, elements=s.elements, labels=s.labels,
                            nodes=s.nodes) for s in j.sides)
            out[k] = Joint(name=name, kind="bonded", parts=j.parts, sides=(a, b), overlap=j.overlap, left_out=left)
            continue
        out[k] = Joint(name=j.name, kind=j.kind, parts=j.parts, sides=j.sides, overlap=j.overlap, reach=j.reach,
                       tied=tied, tolerance=tolerance[k], left_out=left, held_out=held_out)
    return out


def _f8(v: float) -> str:
    """A positive real for an 8-column Nastran field, at most 7 characters so a space always parts it
    from the field before it ("5.932-4")."""
    for digits in range(6, 0, -1):
        t = f"{float(v):.{digits}E}".replace("E-0", "-").replace("E+0", "+").replace("E-", "-").replace("E+", "+")
        if len(t) <= 7:
            return t
    return f"{float(v):.1E}"[:7]


def _surface(fh, fs: FaceSet) -> None:
    for lab in range(1, 5):
        sel = np.sort(fs.elements[fs.labels == lab])
        if len(sel):
            fh.write(f"*ELSET, ELSET={fs.name}_S{lab}\n")
            _write_ids(fh, sel)
    fh.write(f"*SURFACE, NAME={fs.name}, TYPE=ELEMENT\n")
    fh.writelines(f"{fs.name}_S{lab}, S{lab}\n" for lab in range(1, 5) if (fs.labels == lab).any())
    fh.write(f"*NSET, NSET={fs.name}\n")
    _write_ids(fh, fs.nodes)


def tied_nodes(j: Joint) -> np.ndarray:
    """The slave nodes a tie ties (every node of its slave surface when it was not settled)."""
    return j.tied if j.tied is not None else j.sides[0].nodes


def tie_tolerance(j: Joint) -> float | None:
    """How far every solver reaches for a tie's master: the settled tolerance, else a gap joint's reach."""
    return j.tolerance if j.tolerance is not None else j.reach


def _joined(joints: list[Joint]) -> list[Joint]:
    """The contact pairs, and the ties that tie something: the pairs the Nastran and Ansys files write."""
    return [j for j in joints if j.kind == "contact" or (j.kind == "tie" and len(tied_nodes(j)))]


def _left_out_words(j: Joint) -> str:
    said = {"shared": "already the master's own", "beyond": "beyond the tolerance", "twice": "held by a nearer tie",
            "chain": "on another tie's master surface"}
    parts = [f"{n} {said[k]}" for k, n in j.left_out.items() if n]
    return f" (left out: {', '.join(parts)})" if parts else ""


def write_inp(path: Path | str, prep: Prepared, *, units: str = "m", source: str = "",
              connection: str = "bonded") -> dict:
    per = prep.elements.shape[1]
    etype = {4: "C3D4", 10: "C3D10"}[per]
    contacts = [j for j in prep.joints if j.kind == "contact"]
    ties = [j for j in prep.joints if j.kind == "tie"]
    bonds = [j for j in prep.joints if j.kind == "bonded"]
    head = [
        "** Hexera FEA mesh deck - an ASSEMBLY - Abaqus / CalculiX input (flat file, no *PART).",
        *([f"** Source: {source}"] if source else []),
        f"** UNITS: lengths in {'metres (m)' if units == 'm' else units}. Give the rest in one consistent set",
        "**   (SI: N, Pa, kg/m^3, s).",
        f"** {len(prep.parts)} parts, {len(prep.elements)} {etype} elements in ELSET=solid. Each part is its own",
        "**   *ELSET (Part_k): give each its *MATERIAL and *SOLID SECTION, then supports, loads and a *STEP.",
    ]
    for p in prep.parts:
        head.append(f"**   {p['id']}" + (f" ({p['name']})" if p["name"] else "") + f": elements {p['first']}-{p['last']}")
    if connection == "bonded" and not ties:
        head += ["** BONDED: the parts share their nodes where they touch (one conformal mesh) - joined as",
                 f"**   welded or glued; nothing more is needed. The faces of each of the {len(bonds)} joint(s) are",
                 "**   named Bond_n_Part_k on each side."]
    if contacts:
        head += ["** CONTACT: each part is meshed on its own; each touching pair is a *CONTACT PAIR (slave",
                 f"**   surface first) with the interaction {INTERACTION} - hard, frictionless: a default to",
                 "**   change to your case (friction, stiffness). " + NOT_TESTED.capitalize() + " in Abaqus;",
                 "**   contact needs a nonlinear *STEP in CalculiX."]
    if ties:
        head += ["** TIED: where parts are not one mesh they are tied (*TIE: bonded). Each tie names the slave",
                 "**   nodes it ties (a node surface, <slave>_Tied): those within its POSITION TOLERANCE of the",
                 "**   master, never the master's own nodes, never one node twice; ADJUST=NO ties them where they",
                 "**   stand. A node a tie holds must not be held by a support too."]
    if any(j.overlap for j in contacts):
        head.append("** Some contact pairs start overlapping (the CAD parts pass into each other there).")
    head.append("** Every face of every part is a *SURFACE and *NSET named Part_k_Face_j.")
    head += skin_lines(prep, "**")
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(head) + "\n*NODE\n")
        fh.writelines(f"{int(i)}, {_coord(x)}, {_coord(y)}, {_coord(z)}\n"
                      for i, (x, y, z) in zip(prep.node_ids.tolist(), prep.xyz.tolist(), strict=True))
        num = np.arange(1, len(prep.elements) + 1)
        for p in prep.parts:
            sel = (num >= p["first"]) & (num <= p["last"])
            fh.write(f"*ELEMENT, TYPE={etype}, ELSET={p['id']}\n")
            fh.writelines(f"{e}, " + ", ".join(map(str, conn)) + "\n"
                          for e, conn in zip(num[sel].tolist(), prep.elements[sel].tolist(), strict=True))
        fh.write("*ELSET, ELSET=solid, GENERATE\n")
        fh.writelines(f"{p['first']}, {p['last']}, 1\n" for p in prep.parts)
        for fs in prep.faces:
            _surface(fh, fs)
        for j in prep.joints:
            for s in j.sides:
                _surface(fh, s)
        if contacts:
            fh.write(f"*SURFACE INTERACTION, NAME={INTERACTION}\n")
            fh.write("*SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=HARD\n")
            for j in contacts:
                fh.write(f"** {j.name}: Part_{j.parts[0]} (slave) on Part_{j.parts[1]} (master)"
                         + (" - they start overlapping" if j.overlap else "") + "\n")
                fh.write(f"*CONTACT PAIR, INTERACTION={INTERACTION}, TYPE=SURFACE TO SURFACE\n")
                fh.write(f"{j.sides[0].name}, {j.sides[1].name}\n")
        for j in ties:
            tied = tied_nodes(j)
            fh.write(f"** {j.name}: Part_{j.parts[0]} (slave) tied to Part_{j.parts[1]} (master)"
                     + (f", a small-gap joint - the parts are {_coord(j.reach)} or less apart there" if j.reach else "")
                     + f"; {len(tied)} of the slave's {len(j.sides[0].nodes)} nodes tied" + _left_out_words(j) + "\n")
            if not len(tied):
                fh.write(f"** {j.name}: no slave node lies within the tolerance of the master - nothing to tie\n")
                continue
            # THE NODES TIED, as a node surface (settle_ties): never the master's own, never one twice
            fh.write(f"*NSET, NSET={j.sides[0].name}_Tied\n")
            _write_ids(fh, tied)
            fh.write(f"*SURFACE, NAME={j.sides[0].name}_Tied, TYPE=NODE\n{j.sides[0].name}_Tied\n")
            tol = tie_tolerance(j)
            # ADJUST=NO: each node is tied where it stands; moving a corner onto the master without its edge
            # midpoints turns elements inside out
            fh.write(f"*TIE, NAME={j.name}" + (f", POSITION TOLERANCE={_coord(tol)}" if tol else "") + ", ADJUST=NO\n")
            fh.write(f"{j.sides[0].name}_Tied, {j.sides[1].name}\n")
    return {"element_type": etype, "elements": int(len(prep.elements)), "units": units,
            "parts": [{k: p[k] for k in ("id", "name", "first", "last")} for p in prep.parts],
            "face_sets": {f.name: i for i, f in enumerate(prep.faces, start=1)},
            "joints": [{"name": j.name, "kind": j.kind, "slave": j.sides[0].name, "master": j.sides[1].name,
                        "overlap": j.overlap, **({"reach": j.reach} if j.reach else {}),
                        **({"tolerance": tie_tolerance(j), "tied_nodes": int(len(tied_nodes(j))),
                            "left_out": dict(j.left_out)} if j.kind == "tie" else {}),
                        **({"from_tie": dict(j.left_out)} if j.kind == "bonded" and j.left_out else {})}
                       for j in prep.joints],
            "cad_face_names": {}, "face_aliases": {},
            # MESH-8: the CAD faces left out as skins of no thickness, each with the face it lies on
            **({"skins_left_out": [dict(s) for s in prep.skins]} if prep.skins else {})}


# #
# Nastran
# #

def write_bdf(path: Path | str, prep: Prepared, *, units: str = "m", source: str = "") -> dict:
    from meshpipeline.engines.fea_writers import _cards, _f16

    per = prep.elements.shape[1]
    contacts = _joined(prep.joints)
    lines = [
        "$ Hexera FEA mesh - an ASSEMBLY - Nastran bulk data (MSC / NX).",
        "$ " + NOT_TESTED.capitalize() + ".",
        *([f"$ Source: {source}"] if source else []),
        f"$ UNITS: lengths in {'metres (m)' if units == 'm' else units}; give the rest in one consistent set",
        "$   (SI: N, Pa, kg/m^3, s).",
        f"$ Mesh only: {len(prep.elements)} {per}-node CTETRA. Each part k is PSOLID k, which names MAT1 k -",
        "$   add MAT1 k for every part, the SPCs, loads and the case control.",
        "$ Bulk data only: INCLUDE this file after BEGIN BULK in your run deck.",
    ]
    for p in prep.parts:
        lines.append(f"$   PSOLID {p['n']} = {p['id']}" + (f" ({p['name']})" if p["name"] else ""))
    lines += skin_lines(prep, "$")
    lines.append("$ Every face of every part is a SET3 of its grids and a BSURFS of its element faces, both numbered k:")
    for k, f in enumerate(prep.faces, start=1):
        lines.append(f"$   {k:>6} = {f.name}")
    if contacts:
        lines += ["$ CONTACT (MSC Nastran SOL 101 / 400): every part in a pair is a deformable body (BCBODY k on",
                  "$   BCPROP k = PSOLID k); BCTABLE 1 lists the pairs (IGLUE 1 = tied, 0 = touching, frictionless).",
                  "$   Put BCONTACT = 1 in the case control. NX / Simcenter users: the BSURFS of each pair's two",
                  "$   surfaces are written too (ids below) for BCTSET (contact) or BGSET (glue).",
                  *[f"$   {j.name}: BSURFS {JOINT_ID_BASE + 2 * i + 1} (slave, {j.sides[0].name}), "
                    f"{JOINT_ID_BASE + 2 * i + 2} (master, {j.sides[1].name})" for i, j in enumerate(contacts)]]
    bonds = [j for j in prep.joints if j.kind == "bonded"]
    if bonds:
        lines += ["$ BONDED: one conformal mesh - the parts share their grids where they touch. Each joint's faces:",
                  *[f"$   {j.name}: BSURFS {JOINT_ID_BASE + 2 * i + 1} ({j.sides[0].name}), "
                    f"{JOINT_ID_BASE + 2 * i + 2} ({j.sides[1].name})" for i, j in enumerate(bonds)]]
    for i, (x, y, z) in zip(prep.node_ids.tolist(), prep.xyz.tolist(), strict=True):
        lines.append(f"{'GRID*':<8}{int(i):>16d}{'':>16}{_f16(x)}{_f16(y)}*")
        lines.append(f"{'*':<8}{_f16(z)}")
    for e, (conn, p) in enumerate(zip(prep.elements.tolist(), prep.element_part.tolist(), strict=True), start=1):
        lines.extend(_cards("CTETRA", [e, int(p), *conn]))
    for p in prep.parts:
        lines.extend(_cards("PSOLID", [p["n"], p["n"]]))

    def bsurfs(sid: int, fs: FaceSet) -> list[str]:
        tri = []
        for el, lab in zip(fs.elements.tolist(), fs.labels.tolist(), strict=True):
            conn = prep.elements[el - 1]
            tri.extend([el, *conn[list(TET_FACE_CORNERS[lab - 1])].tolist()])
        return _cards("BSURFS", [sid, "", "", "", *tri])

    for k, f in enumerate(prep.faces, start=1):
        lines.append(f"$ {f.name}")
        lines.extend(_cards("SET3", [k, "GRID", *f.nodes.tolist()]))
        lines.extend(bsurfs(k, f))
    for i, j in enumerate(contacts + bonds):
        for s, side in enumerate(j.sides):
            lines.append(f"$ {side.name}")
            lines.extend(bsurfs(JOINT_ID_BASE + 2 * i + 1 + s, side))
    if contacts:
        bodies = sorted({p for j in contacts for p in j.parts})
        lines.extend(_cards("BCPARA", [0, "NBODIES", len(bodies)]))
        for body in bodies:
            lines.extend(_cards("BCPROP", [body, body]))
            lines.extend(_cards("BCBODY", [body, "3D", "DEFORM", body, 0, 0.0]))
        # BCTABLE ID, then one SLAVE / MASTERS group per pair: (slave body, error, fntol, friction,
        # cinterf, iglue, isearch) and the master body
        rows = [f"{'BCTABLE':<8}{1:>8d}{'':>8}{'':>8}{len(contacts):>8d}"]
        for j in contacts:
            # ERROR: the distance below which a node is taken as touching - a tie's own tolerance (settle_ties:
            # the small-gap joint's reach, or the touching tie's), the same as the other solvers'
            tol = tie_tolerance(j) if j.kind == "tie" else j.reach
            err = _f8(tol) if tol else "0."
            rows.append(f"{'':<8}{'SLAVE':<8}{j.parts[0]:>8d}{err:>8}{'0.':>8}{'0.':>8}{'0.':>8}"
                        f"{1 if j.kind == 'tie' else 0:>8d}{0:>8d}")
            rows.append(f"{'':<8}{'MASTERS':<8}{j.parts[1]:>8d}")
        lines.extend(rows)
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "faces": len(prep.faces), "skins_left_out": len(prep.skins),
            "elements": int(len(prep.elements)),
            "parts": len(prep.parts), "pairs": len(contacts)}


# #
# Ansys MAPDL
# #

def _oriented(conn: np.ndarray, lab: int, xyz_of) -> list[int]:
    """The six nodes of a tet face (three corners, then the edge midpoints 1-2, 2-3, 3-1, or three
    corners for a 4-node tet), ordered so the right-hand normal points out of the element."""
    corners = list(TET_FACE_CORNERS[lab - 1])
    other = ({0, 1, 2, 3} - set(corners)).pop()
    a, b, c = (xyz_of(np.asarray([conn[i]]))[0] for i in corners)
    d = xyz_of(np.asarray([conn[other]]))[0]
    if float(np.dot(np.cross(b - a, c - a), d - a)) > 0:     # points in: turn it round
        corners = [corners[0], corners[2], corners[1]]
    nodes = [int(conn[i]) for i in corners]
    if len(conn) == 10:
        mid = {frozenset((0, 1)): 4, frozenset((1, 2)): 5, frozenset((2, 0)): 6, frozenset((0, 3)): 7,
               frozenset((1, 3)): 8, frozenset((2, 3)): 9}
        nodes += [int(conn[mid[frozenset((corners[i], corners[(i + 1) % 3]))]]) for i in range(3)]
    return nodes


def write_cdb(path: Path | str, prep: Prepared, *, units: str = "m", source: str = "") -> dict:
    from meshpipeline.engines.fea_writers import _cmblock, unique_ansys_name

    per = prep.elements.shape[1]
    et = 187 if per == 10 else 285
    contacts = _joined(prep.joints)
    index = np.zeros(int(prep.node_ids.max()) + 1, dtype=np.int64)
    index[prep.node_ids] = np.arange(len(prep.node_ids))

    def xyz_of(nids):
        return prep.xyz[index[np.asarray(nids, dtype=np.int64)]]

    lines = [
        "! Hexera FEA mesh - an ASSEMBLY - Ansys MAPDL archive (CDB). " + NOT_TESTED.capitalize() + ".",
        *([f"! Source: {source}"] if source else []),
        f"! UNITS: lengths in {'metres (m)' if units == 'm' else units}; give the rest in one consistent set",
        "!   (SI: N, Pa, kg/m^3, s).",
        f"! Mesh only: {len(prep.elements)} SOLID{et} elements; part k is material k and the element component",
        "!   PART_K - add MP,EX/PRXY/DENS for each material, the supports, loads and the solution.",
        "! Every face of every part is a node component PART_K_FACE_J.",
        *skin_lines(prep, "!"),
    ]
    if contacts:
        lines += ["! CONTACT: each pair is TARGE170 (master) and CONTA174 (slave) elements sharing one real",
                  "!   constant set; CONTA174 KEYOPT(12) = 5 (bonded, always) for a tied pair, 0 (standard,",
                  "!   frictionless) for contact - defaults to change to your case."]
    lines += ["/PREP7", f"ET,1,{et}"]
    if any(j.kind == "contact" for j in contacts):
        lines += ["ET,2,170", "ET,3,174", "KEYOPT,3,12,0"]
    if any(j.kind == "tie" for j in contacts):
        # KEYOPT(9) = 1: bonded where the faces stand, the gap neither closed nor a node moved (ADJUST=NO)
        lines += ["ET,4,170", "ET,5,174", "KEYOPT,5,12,5", "KEYOPT,5,9,1"]
    for i, j in enumerate(contacts, start=2):
        # PINB (the 6th real constant): a tie's own tolerance (settle_ties), the same as CalculiX's and Nastran's
        tol = tie_tolerance(j) if j.kind == "tie" else j.reach
        lines.append(f"R,{i},,,,,,{tol:.6E}" if tol else f"R,{i}")
    lines += [f"NBLOCK,6,SOLID,{int(prep.node_ids.max()):>10d},{len(prep.node_ids):>10d}", "(3i9,6e21.13e3)"]
    for i, (x, y, z) in zip(prep.node_ids.tolist(), prep.xyz.tolist(), strict=True):
        lines.append(f"{int(i):>9d}{0:>9d}{0:>9d}{x:21.13E}{y:21.13E}{z:21.13E}")
    lines.append("N,R5.3,LOC,       -1,")
    surf = []
    nxt = len(prep.elements) + 1
    #: (joint, side) -> the element numbers of its contact (side 0, the slave) or target (side 1) elements
    ranges: dict[tuple[int, int], tuple[int, int]] = {}
    for i, j in enumerate(contacts, start=2):
        t_target, t_contact = (2, 3) if j.kind == "contact" else (4, 5)
        for s, etype in ((1, t_target), (0, t_contact)):
            first = nxt
            fs = j.sides[s]
            if s == 0 and j.kind == "tie":
                # A TIE'S CONTACT ELEMENTS: only the slave faces that hold a node it ties and none it left out on
                # the joint (settle_ties) - a bond there would join what the other files keep apart
                fs = _faces_with(fs, tied_nodes(j), prep.elements, j.held_out)
            for el, lab in zip(fs.elements.tolist(), fs.labels.tolist(), strict=True):
                nodes = _oriented(prep.elements[el - 1], lab, xyz_of)
                if etype in (3, 5) and len(nodes) == 6:
                    c1, c2, c3, m12, m23, m31 = nodes
                    nodes = [c1, c2, c3, c3, m12, m23, c3, m31]       # CONTA174 as a triangle (K = L = O)
                surf.append((nxt, etype, i, nodes))
                nxt += 1
            ranges[(id(j), s)] = (first, nxt - 1)
    total = len(prep.elements) + len(surf)
    lines += [f"EBLOCK,19,SOLID,{total:>10d},{total:>10d}", "(19i9)"]
    for e, (conn, p) in enumerate(zip(prep.elements.tolist(), prep.element_part.tolist(), strict=True), start=1):
        head = [int(p), 1, 1, 1, 0, 0, 0, 0, per, 0, e]
        lines.append("".join(f"{v:>9d}" for v in head + conn[:8]))
        if per > 8:
            lines.append("".join(f"{v:>9d}" for v in conn[8:]))
    for e, etype, real, nodes in surf:
        head = [1, etype, real, 1, 0, 0, 0, 0, len(nodes), 0, e]
        lines.append("".join(f"{v:>9d}" for v in head + nodes[:8]))
        if len(nodes) > 8:
            lines.append("".join(f"{v:>9d}" for v in nodes[8:]))
    lines.append(f"{-1:>9d}")
    taken: set[str] = set()
    for p in prep.parts:
        nm = unique_ansys_name(p["id"], taken)
        taken.add(nm)
        lines.extend(_elem_cmblock(nm, np.arange(p["first"], p["last"] + 1)))
    for f in prep.faces:
        nm = unique_ansys_name(f.name, taken)
        taken.add(nm)
        lines.extend(_cmblock(nm, f.nodes))
    for j in prep.joints:
        for s, side in enumerate(j.sides):
            nm = unique_ansys_name(side.name, taken)
            taken.add(nm)
            lines.extend(_cmblock(nm, side.nodes))
            rng = ranges.get((id(j), s))
            if rng and rng[1] >= rng[0]:
                enm = unique_ansys_name(f"{side.name}_{'CONTA' if s == 0 else 'TARGE'}", taken)
                taken.add(enm)
                lines.extend(_elem_cmblock(enm, np.arange(rng[0], rng[1] + 1)))
    lines.append("FINISH")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "faces": len(prep.faces), "skins_left_out": len(prep.skins),
            "elements": int(len(prep.elements)),
            "parts": len(prep.parts), "pairs": len(contacts), "contact_elements": len(surf)}


def _faces_with(fs: FaceSet, nodes: np.ndarray, elements: np.ndarray, never: np.ndarray | None = None) -> FaceSet:
    """The element faces of `fs` that hold at least one of `nodes` and none of `never`."""
    if len(nodes) == len(fs.nodes):
        return fs
    table = np.asarray(TET10_FACE_NODES if elements.shape[1] == 10 else TET_FACE_CORNERS)
    face = elements[fs.elements[:, None] - 1, table[fs.labels - 1]]
    keep = np.isin(face, nodes).any(axis=1)
    if never is not None and len(never):
        keep &= ~np.isin(face, never).any(axis=1)
    return FaceSet(name=fs.name, tag=fs.tag, elements=fs.elements[keep], labels=fs.labels[keep], nodes=fs.nodes)


def _elem_cmblock(name: str, ids: np.ndarray) -> list[str]:
    ids = np.unique(np.asarray(ids, dtype=np.int64))
    out = [f"CMBLOCK,{name},ELEM,{len(ids):>8d}", "(8i10)"]
    for i in range(0, len(ids), 8):
        out.append("".join(f"{v:>10d}" for v in ids[i:i + 8].tolist()))
    return out


def write_all(workspace, prep: Prepared, *, units: str = "m", source: str = "", connection: str = "bonded") -> dict:
    """mesh.inp, mesh.bdf and mesh.cdb for the assembly. Returns (the deck's record, exports). The deck
    is the deliverable; EACH solver file is written on its own, and one that fails is left out with
    its reason ({"error": ...}) - a note, never a lost mesh (Areen, 24 Oct: only a broken mesh fails)."""
    ws = Path(workspace)
    deck = write_inp(ws / "mesh.inp", prep, units=units, source=source, connection=connection)
    out: dict = {"deck": deck}
    for solver, name, write in (("nastran", "mesh.bdf", write_bdf), ("ansys", "mesh.cdb", write_cdb)):
        try:
            out[solver] = write(ws / name, prep, units=units, source=source)
        except Exception as exc:  # noqa: BLE001 - a missing solver file is a note
            (ws / name).unlink(missing_ok=True)
            out[solver] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


__all__ = ["INTERACTION", "JOINT_ID_BASE", "Joint", "NOT_TESTED", "Prepared", "prepare", "write_all", "write_bdf",
           "write_cdb", "write_inp"]
