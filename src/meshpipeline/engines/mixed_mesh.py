# Responsibility: The MIXED mesh of a part that is thin walls and thick regions: its shell mesh (the trimmed
# mid-faces, meshed by the shell mesher) and its solid mesh (the solid region, meshed by the FEA solid path) joined
# by SHLINK-4 - each shell edge on a cut face carried one wall into the solid and its two node rows tied - and the
# one numbering every solver file shares (shell nodes first, then the solid's), written per solver.
# Boundaries: pure over the two meshes and the cut faces; it runs no mesher (the caller hands it both meshes) and no
# solver. CalculiX / Abaqus (.inp), Nastran (.bdf) and Ansys (.cdb): the shell writers' files with the solid and the
# join added, so a mixed deck reads exactly as a shell deck does for its shell part.
# Collaborates with: cad/mixed_split.py (the split and its cut faces), engines/gmsh/shell_adapter.py (the shell mesh),
# engines/fea_driver.py + engines/gmsh/fea_adapter.py (the solid region's mesh.inp), engines/mixed_links.py (the
# ties), engines/shell_writers.py, engines/shell_links.py (the per-solver tie text).
"""THE MIXED MESH AND ITS DECKS.

Numbering: shell node k (1-based) is ShellMesh node k-1; the solid's nodes follow (shell count + its own number in
the solid deck, renumbered 1..n). Shell elements first (quads, then triangles, then the overlap quads of SHLINK-4),
then the solid's quadratic tetrahedra (C3D10 / CTETRA / SOLID187, the FEA deck's node order).

The overlap (SHLINK-4): every free element edge of the shell lying on a cut face of the solid region is a LINK
edge. Each link edge gets one quad running OVERLAP_WALLS of its wall on into the solid, square to the edge in the
element's own plane (group SHLINK_overlap); its row on the cut face is tied to the cut face's nodes, its inner row
to the solid's nodes round it. Translations only: the moment crosses as the couple of the two rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines import mixed_links as ml
from meshpipeline.engines import shell_links as sl
from meshpipeline.engines import shell_writers as sw
from meshpipeline.engines.shell_mesh import ShellMesh, edge_users

#: the group the overlap quads are written in
OVERLAP_GROUP = "SHLINK_overlap"
#: a shell node lies on a cut face within this share of its wall
ON_FACE_SHARE = 2e-3
#: MIXED-9: a shell node lies on a face of a body solid as a whole within this share of its wall - a plate's edge
#: butting the body (its mid-face ends where its skins end: 0.03 mm short of a 30 mm hub for a 2 mm spoke); a plate
#: lying ON the body's face, its edge half a wall off it, is not a plate ending there
LOOSE_SHARE = 0.25
#: the inner row is tied to the solid nodes within this share of the overlap depth of its place, along the shell
BAND_SHARE = 0.5
#: the tetrahedron's faces, solver order (corners 0-3; edge midpoints 4:(0,1) 5:(1,2) 6:(2,0) 7:(0,3) 8:(1,3) 9:(2,3))
_MID = {(0, 1): 4, (1, 2): 5, (0, 2): 6, (0, 3): 7, (1, 3): 8, (2, 3): 9}
_TET_FACES = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))


@dataclass
class SolidPart:
    """The solid region's mesh, millimetres, its own numbering 1..n: nodes, 10-node tetrahedra (solver order), and
    each named face's nodes."""
    xyz: np.ndarray                       # (n, 3)
    tets: np.ndarray                      # (e, 10), 1-based
    faces: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def n_nodes(self) -> int:
        return int(len(self.xyz))


def read_solid_deck(path: Path | str, *, mm_per_unit: float = 1000.0) -> SolidPart:
    """The FEA path's mesh.inp (engines/gmsh/abaqus_deck.write_inp, metres) as a SolidPart in millimetres, its nodes
    renumbered 1..n in their order."""
    text = Path(path).read_text()
    ids, xyz = [], []
    for ln in text.split("*NODE\n", 1)[1].split("*", 1)[0].strip().splitlines():
        p = ln.split(",")
        ids.append(int(p[0]))
        xyz.append([float(v) * mm_per_unit for v in p[1:4]])
    new = {old: k + 1 for k, old in enumerate(ids)}
    tets = []
    for m in re.finditer(r"\*ELEMENT, TYPE=C3D10[^\n]*\n((?:[^*][^\n]*\n)*)", text):
        vals: list[int] = []
        for ln in m.group(1).splitlines():
            vals += [int(v) for v in ln.replace(",", " ").split()]
            if len(vals) >= 11:
                tets.append([new[v] for v in vals[1:11]])
                vals = []
    faces = {m.group(1): np.array(sorted(new[int(v)] for v in re.findall(r"\d+", m.group(2))), dtype=np.int64)
             for m in re.finditer(r"\*NSET, NSET=((?:Part_\d+_)?Face_\d+)\n((?:[^*][^\n]*\n)*)", text)}
    if not tets:
        raise ValueError("the solid deck holds no 10-node tetrahedron")
    return SolidPart(xyz=np.array(xyz, dtype=float), tets=np.array(tets, dtype=np.int64), faces=faces)


def boundary_triangles(tets: np.ndarray, on: np.ndarray) -> np.ndarray:
    """The 6-node boundary triangles (1-based node ids: corners, then midpoints 1-2, 2-3, 3-1) of the tetrahedra
    whose three corners are all `on` (a bool per node, index = id): faces no other tetrahedron shares."""
    seen: dict[tuple[int, int, int], list[int] | None] = {}
    for row in np.asarray(tets, dtype=np.int64):
        for a, b, c in _TET_FACES:
            cs = (int(row[a]), int(row[b]), int(row[c]))
            if not (on[cs[0]] and on[cs[1]] and on[cs[2]]):
                continue
            s0, s1, s2 = sorted(cs)
            key = (s0, s1, s2)
            tri = [cs[0], cs[1], cs[2], int(row[_MID[(min(a, b), max(a, b))]]), int(row[_MID[(min(b, c), max(b, c))]]),
                   int(row[_MID[(min(a, c), max(a, c))]])]
            seen[key] = None if key in seen else tri          # an inner face is met twice
    return np.array([t for t in seen.values() if t is not None], dtype=np.int64).reshape(-1, 6)


@dataclass
class Link:
    """One link row: its cut-face nodes (shell ids, 0-based), the overlap row's new nodes, closed or not."""
    on_face: list[int]
    inner: list[int]
    closed: bool
    wall: float
    inward: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    normal: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))


@dataclass
class SolidRegion:
    """MIXED-9: one solid patch of a shell run's deck: its name (the group, SOLID_<name>), the body it is of (its
    material), and its tetrahedra (solid.tets[first:first + count])."""
    name: str
    part: str
    first: int
    count: int


@dataclass
class MixedMesh:
    shell: ShellMesh
    solid: SolidPart
    part: str
    ties: list[sl.Tie]
    links: list[Link]
    n_overlap: int
    notes: list[str] = field(default_factory=list)
    #: MIXED-9: the solid patches, each its own set, section and material (None: one region of `part`, as before)
    regions: list[SolidRegion] | None = None

    @property
    def counts(self) -> dict:
        return {"shell_elements": int(self.shell.n_elements - self.n_overlap), "overlap_elements": int(self.n_overlap),
                "solid_elements": int(len(self.solid.tets)), "shell_nodes": int(len(self.shell.xyz)),
                "solid_nodes": int(self.solid.n_nodes), "ties": len(self.ties), "links": len(self.links)}


def _chains(edges: list[tuple[int, int]]) -> list[tuple[list[int], bool]]:
    """Edges (node pairs) as ordered node chains: (nodes, closed). Deterministic: from the lowest free end first."""
    nbr: dict[int, list[int]] = {}
    for a, b in edges:
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)
    for v in nbr.values():
        v.sort()
    used: set[tuple[int, int]] = set()
    out = []
    starts = sorted(n for n, v in nbr.items() if len(v) != 2) + sorted(nbr)
    for s in starts:
        for first in nbr[s]:
            if (min(s, first), max(s, first)) in used:
                continue
            chain = [s]
            cur, nxt = s, first
            closed = False
            while True:
                used.add((min(cur, nxt), max(cur, nxt)))
                if nxt == s:
                    closed = True
                    break
                chain.append(nxt)
                ahead = [n for n in nbr[nxt] if (min(nxt, n), max(nxt, n)) not in used]
                if len(nbr[nxt]) != 2 or not ahead:
                    break
                cur, nxt = nxt, ahead[0]
            out.append((chain, closed))
    return out


def _normal(p: np.ndarray) -> np.ndarray:
    n = np.cross(p[2] - p[0], p[3] - p[1]) if len(p) == 4 else np.cross(p[1] - p[0], p[2] - p[0])
    return n / max(float(np.linalg.norm(n)), 1e-300)


def join(shell: ShellMesh, solid: SolidPart, cut_faces: list, *, part: str,
         cut_face_names: list[str] | None = None, loose_faces: list | None = None) -> MixedMesh:
    """SHLINK-4 on a shell mesh and a solid mesh of one mixed body (both mm): the link rows found on the cut faces
    (OCC faces, mm), the overlap quads added to the shell, the ties made. The shell mesh is extended in place.
    `loose_faces` (MIXED-9): faces of a body that is solid as a whole, which a shell edge meets within LOOSE_SHARE of
    its wall (a plate of another body ending on it: its mid-face stops where its two skins stop, a hair short of a
    curved face) - their solid nodes are found as the cut faces' are."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    from meshpipeline.cad import midsurface as ms

    notes: list[str] = []
    users = edge_users(shell)
    elems = shell.elements()
    free = [(k, u[0]) for k, u in users.items() if len(u) == 1]
    tight = ms._compound(cut_faces) if cut_faces else None
    loose = ms._compound(loose_faces) if loose_faces else None
    cut = ms._compound(list(cut_faces) + list(loose_faces or []))
    on_cache: dict[int, bool] = {}

    def near(n: int, shape, share: float, e: int) -> bool:
        if shape is None:
            return False
        v = BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, shell.xyz[n]))).Vertex()
        d = BRepExtrema_DistShapeShape(v, shape)
        return d.IsDone() and d.Value() <= share * float(shell.wall[e]) + 1e-6

    def on_cut(n: int, e: int) -> bool:
        if n not in on_cache:
            on_cache[n] = near(n, tight, ON_FACE_SHARE, e) or near(n, loose, LOOSE_SHARE, e)
        return on_cache[n]

    link_edges = {key: eu for key, eu in free if on_cut(key[0], eu[0]) and on_cut(key[1], eu[0])}
    if not link_edges:
        raise ValueError("no shell edge lies on a cut face of the solid region")
    # each link edge's element and its outward direction (out of the element, into the solid, in its own plane)
    out_dir: dict[tuple[int, int], np.ndarray] = {}
    elem_of: dict[tuple[int, int], int] = {}
    for key, (e, _s) in link_edges.items():
        conn = [int(v) for v in elems[e]]
        p = shell.xyz[conn]
        n = _normal(p)
        a, b = key
        tau = shell.xyz[b] - shell.xyz[a]
        o = np.cross(tau, n)
        o /= max(float(np.linalg.norm(o)), 1e-300)
        if float(np.dot(p.mean(axis=0) - 0.5 * (shell.xyz[a] + shell.xyz[b]), o)) > 0:
            o = -o
        out_dir[key] = o
        elem_of[key] = e
    xyz = [shell.xyz]
    n_new = len(shell.xyz)
    links: list[Link] = []
    quads, q_wall, q_mid, q_part = [], [], [], []
    ties: list[sl.Tie] = []
    for chain, closed in _chains(sorted(link_edges)):
        segs = [(chain[i], chain[(i + 1) % len(chain)]) for i in range(len(chain) if closed else len(chain) - 1)]
        keys = [(min(a, b), max(a, b)) for a, b in segs]
        walls = [float(shell.wall[elem_of[k]]) for k in keys]
        inward, normal, wall = [], [], []
        for node in chain:
            ks = [k for k in keys if node in k]
            d = np.sum([out_dir[k] for k in ks], axis=0)
            inward.append(d / max(float(np.linalg.norm(d)), 1e-300))
            nn = np.sum([_normal(shell.xyz[[int(v) for v in elems[elem_of[k]]]]) for k in ks], axis=0)
            normal.append(nn / max(float(np.linalg.norm(nn)), 1e-300))
            wall.append(max(float(shell.wall[elem_of[k]]) for k in ks))
        inward_a, normal_a, wall_a = np.array(inward), np.array(normal), np.array(wall)
        new_of: dict[int, int] = {}       # a node two rows share (a T on the cut face) gets an inner node per row
        depth = ml.OVERLAP_WALLS * wall_a
        inner = []
        for i, node in enumerate(chain):
            new_of[node] = n_new
            inner.append(n_new)
            xyz.append((shell.xyz[node] + depth[i] * inward_a[i])[None, :])
            n_new += 1
        for (a, b), k in zip(segs, keys, strict=True):
            e = elem_of[k]
            conn = [int(v) for v in elems[e]]
            # the element runs the edge one way; the overlap quad runs it the other, so their normals agree
            ia = conn.index(a)
            forward = conn[(ia + 1) % len(conn)] == b
            s, t = (a, b) if forward else (b, a)
            quads.append([t, s, new_of[s], new_of[t]])
            q_wall.append(float(shell.wall[e]))
            q_mid.append(shell.mid[e])
            q_part.append(shell.part[e])
        links.append(Link(on_face=list(chain), inner=inner, closed=closed, wall=float(max(walls)), inward=inward_a,
                          normal=normal_a))
    all_xyz = np.vstack(xyz)
    base = len(all_xyz)                     # solid node j (1-based) is deck node base + j
    # the cut faces' nodes and their area shares (the faces' 6-node boundary triangles)
    on = np.zeros(solid.n_nodes + 1, dtype=bool)
    if cut_face_names is not None:
        for name in cut_face_names:
            on[solid.faces[name]] = True
    else:
        lo, hi = ms._box(cut)
        tol = ON_FACE_SHARE * float(np.min(shell.wall)) + 1e-6
        inside = np.flatnonzero(np.all((solid.xyz >= lo - tol) & (solid.xyz <= hi + tol), axis=1))
        for j in inside.tolist():
            v = BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, solid.xyz[j]))).Vertex()
            d = BRepExtrema_DistShapeShape(v, cut)
            on[j + 1] = d.IsDone() and d.Value() <= tol
    tri = boundary_triangles(solid.tets, on)
    face_xyz = {base + int(i): solid.xyz[int(i) - 1] for i in np.unique(tri)}
    face_area = {base + i: a for i, a in ml.face_node_areas({int(i): solid.xyz[int(i) - 1] for i in np.unique(tri)},
                                                           tri).items()}
    missing = 0
    for lk in links:
        p_on = all_xyz[lk.on_face]
        row_a = ml.Row([n + 1 for n in lk.on_face], p_on, lk.closed)
        fid = np.array(sorted(face_xyz), dtype=np.int64)
        fx = np.array([face_xyz[i] for i in fid.tolist()]).reshape(-1, 3)
        keep = _within(fx, p_on, 0.5 * lk.wall * 1.05 + 1e-6, lk.closed)
        near = {int(i): fx[k] for k, i in enumerate(fid.tolist()) if keep[k]}
        ta = ml.section_ties(row_a, near, face_area)
        p_in = all_xyz[lk.inner]
        n_row = len(lk.on_face)
        row_b = ml.Row([n + 1 for n in lk.inner], p_in, lk.closed)
        d_in = ml.OVERLAP_WALLS * lk.wall
        reach = float(np.hypot(BAND_SHARE * d_in, 0.5 * lk.wall)) * 1.05 + 1e-6
        keep = _within(solid.xyz, p_in, reach, lk.closed)
        near_b = {base + int(j) + 1: solid.xyz[j] for j in np.flatnonzero(keep).tolist()}
        tb = ml.band_ties(row_b, lk.inward, lk.normal, np.full(n_row, lk.wall), near_b, depth=d_in,
                          half_band=BAND_SHARE * d_in)
        missing += (len(lk.on_face) - len({t.node for t in ta})) + (len(lk.inner) - len({t.node for t in tb}))
        # a node two rows share (a rib meeting the plate on the cut face) is tied once, by the first row: a solver
        # takes a freedom as the dependent side of one equation only (CalculiX stops on the second)
        done = {(t.node, t.dof) for t in ties}
        ties += [t for t in ta + tb if (t.node, t.dof) not in done]
    if missing:
        notes.append(f"{missing} link node(s) found no solid node to tie to")
    q = np.array(quads, dtype=np.int64).reshape(-1, 4)
    n_q = len(shell.quads)
    ext = ShellMesh(xyz=all_xyz, quads=np.vstack([shell.quads, q]), trias=shell.trias,
                    wall=np.concatenate([shell.wall[:n_q], q_wall, shell.wall[n_q:]]),
                    mid=list(shell.mid[:n_q]) + q_mid + list(shell.mid[n_q:]),
                    part=list(shell.part[:n_q]) + q_part + list(shell.part[n_q:]), notes=list(shell.notes))
    # element indices move: the triangles come after the new quads
    shift = np.where(np.arange(shell.n_elements) >= n_q, len(q), 0)
    ext.groups = {g: np.asarray(v, dtype=np.int64) + shift[np.asarray(v, dtype=np.int64)] for g, v in shell.groups.items()}
    ext.groups[OVERLAP_GROUP] = np.arange(n_q, n_q + len(q), dtype=np.int64)
    if shell.node_wall is not None:
        nw = np.asarray(shell.node_wall, float)
        ext.node_wall = np.concatenate([nw, [nw[a] for lk in links for a in lk.on_face]])
    for name in ("varies", "zoffs"):
        arr = getattr(shell, name)
        if arr is not None:
            arr = np.asarray(arr)
            extra = np.zeros(len(q), dtype=arr.dtype)
            if name == "zoffs":
                extra = np.array([arr[elem_of[(min(a, b), max(a, b))]] for lk in links
                                  for a, b in _segs(lk)], dtype=arr.dtype).reshape(-1)
            setattr(ext, name, np.concatenate([arr[:n_q], extra, arr[n_q:]]))
    return MixedMesh(shell=ext, solid=solid, part=part, ties=ties, links=links, n_overlap=len(q), notes=notes)


def _segs(lk: Link) -> list[tuple[int, int]]:
    c = lk.on_face
    return [(c[i], c[(i + 1) % len(c)]) for i in range(len(c) if lk.closed else len(c) - 1)]


def _within(x: np.ndarray, pts: np.ndarray, reach: float, closed: bool) -> np.ndarray:
    """Which of the points x (m, 3) lie within `reach` of the polyline through pts (closed: back to its first)."""
    x = np.asarray(x, float).reshape(-1, 3)
    lo, hi = pts.min(axis=0) - reach, pts.max(axis=0) + reach
    out = np.zeros(len(x), dtype=bool)
    cand = np.flatnonzero(np.all((x >= lo) & (x <= hi), axis=1))
    if not len(cand):
        return out
    q = x[cand]
    if len(pts) == 1:
        out[cand] = np.linalg.norm(q - pts[0], axis=1) <= reach
        return out
    a = pts if closed else pts[:-1]
    b = np.roll(pts, -1, axis=0) if closed else pts[1:]
    best = np.full(len(q), np.inf)
    for s0, s1 in zip(a, b, strict=True):
        seg = s1 - s0
        ll = max(float(seg @ seg), 1e-300)
        t = np.clip((q - s0) @ seg / ll, 0.0, 1.0)
        best = np.minimum(best, np.linalg.norm(s0 + t[:, None] * seg - q, axis=1))
    out[cand] = best <= reach
    return out


# ------------------------------------------------------------------------------------------------- decks ----
def _solid_material(prep: sw.ShellPrepared, part: str) -> int:
    return prep.parts.index(part) + 1 if part in prep.parts else 1


def region_materials(prep: sw.ShellPrepared, regions: list[SolidRegion]) -> dict[str, int]:
    """MIXED-9: each solid patch's body -> its material number: a body with shell elements keeps its shell
    material (MAT_k, k its place among the shell's parts); a body that is solid only is numbered on after them."""
    out = {p: i + 1 for i, p in enumerate(prep.parts)}
    for r in regions:
        if r.part not in out:
            out[r.part] = len(out) + 1
    return out


def _patch_lines_inp(mm: MixedMesh, prep: sw.ShellPrepared) -> list[str]:
    """MIXED-9: the solid patches of a shell run in the CalculiX / Abaqus deck: their nodes, a C3D10 set and a
    *SOLID SECTION per patch, their faces' node sets, then the ties."""
    s = prep.scale
    base, e0 = len(mm.shell.xyz), mm.shell.n_elements
    regions = mm.regions or []
    mats = region_materials(prep, regions)
    lines = [f"** SOLID PATCHES (MIXED-9, tied by SHLINK-4): {len(mm.solid.tets)} C3D10 in {len(regions)} patch(es), "
             f"nodes {base + 1}..{base + mm.solid.n_nodes}; each patch's section names its part's MAT_k."]
    lines += [f"**   MAT_{mats[p]} = {p} (solid only)" for p in sorted(mats, key=lambda p: mats[p]) if p not in prep.parts]
    lines.append("*NODE, NSET=NSOLID")
    lines += [f"{base + j + 1}, {x * s:.12g}, {y * s:.12g}, {z * s:.12g}" for j, (x, y, z) in enumerate(mm.solid.xyz.tolist())]
    for r in regions:
        lines.append(f"*ELEMENT, TYPE=C3D10, ELSET=SOLID_{r.name}")
        lines += [f"{e0 + k + 1}, " + ", ".join(str(base + int(v)) for v in mm.solid.tets[k])
                  for k in range(r.first, r.first + r.count)]
    for r in regions:
        lines.append(f"*SOLID SECTION, ELSET=SOLID_{r.name}, MATERIAL=MAT_{mats[r.part]}")
    for name, ids in sorted(mm.solid.faces.items()):
        lines.append(f"*NSET, NSET=SOLID_{name}")
        lines += [", ".join(str(base + int(v)) for v in ids[i:i + 16]) for i in range(0, len(ids), 16)]
    lines.append(f"** SHLINK-4: {len(mm.ties)} ties (translations) of the shell's link rows to the solid patches")
    lines += sl.equations(mm.ties)
    return lines


def write_inp(path: Path | str, mm: MixedMesh, *, units: str = "m", source: str = "", solver: str = "calculix",
              materials: dict[int, list[str]] | None = None) -> dict:
    prep = sw.prepare(mm.shell, units=units)
    got = sw.write_inp(path, prep, units=units, source=source, solver=solver, materials=materials)
    if mm.regions is not None:
        with open(path, "a", encoding="ascii", newline="\n") as fh:
            fh.write("\n".join(_patch_lines_inp(mm, prep)) + "\n")
        return {**got, "elements": int(mm.shell.n_elements + len(mm.solid.tets)), "ties": len(mm.ties)}
    s = prep.scale
    base, e0 = len(mm.shell.xyz), mm.shell.n_elements
    mat = _solid_material(prep, mm.part)
    lines = [f"** MIXED (SHLINK-4): the solid region of {mm.part}, {len(mm.solid.tets)} C3D10, nodes "
             f"{base + 1}..{base + mm.solid.n_nodes}; its sections name MAT_{mat}.", "*NODE, NSET=NSOLID"]
    lines += [f"{base + j + 1}, {x * s:.12g}, {y * s:.12g}, {z * s:.12g}" for j, (x, y, z) in enumerate(mm.solid.xyz.tolist())]
    lines.append(f"*ELEMENT, TYPE=C3D10, ELSET=SOLID_{mm.part}")
    lines += [f"{e0 + k + 1}, " + ", ".join(str(base + int(v)) for v in row) for k, row in enumerate(mm.solid.tets)]
    lines.append(f"*SOLID SECTION, ELSET=SOLID_{mm.part}, MATERIAL=MAT_{mat}")
    for name, ids in sorted(mm.solid.faces.items()):
        lines.append(f"*NSET, NSET=SOLID_{name}")
        lines += [", ".join(str(base + int(v)) for v in ids[i:i + 16]) for i in range(0, len(ids), 16)]
    lines.append(f"** SHLINK-4: {len(mm.ties)} ties (translations) of the shell's link rows to the solid")
    lines += sl.equations(mm.ties)
    with open(path, "a", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return {**got, "elements": int(e0 + len(mm.solid.tets)), "ties": len(mm.ties)}


def write_bdf(path: Path | str, mm: MixedMesh, *, units: str = "m", source: str = "") -> dict:
    from meshpipeline.engines.fea_writers import _cards, _f16

    prep = sw.prepare(mm.shell, units=units)
    got = sw.write_bdf(path, prep, units=units, source=source)
    s = prep.scale
    base, e0 = len(mm.shell.xyz), mm.shell.n_elements
    if mm.regions is not None:
        # MIXED-9: a PSOLID per solid patch (its part's MAT1), its CTETRA, a group per patch, the ties as MPC set 1
        mats = region_materials(prep, mm.regions)
        lines = [f"$ SOLID PATCHES (MIXED-9, tied by SHLINK-4): {len(mm.solid.tets)} 10-node CTETRA in "
                 f"{len(mm.regions)} PSOLID; the join is MPC set 1 - add MPC = 1 to the case control."]
        lines += [f"$   MAT1 {mats[p]} = {p} (solid only)" for p in sorted(mats, key=lambda p: mats[p])
                  if p not in prep.parts]
        lines += [f"$ Femap Property {len(prep.props) + i + 1} : SOLID_{r.name}" for i, r in enumerate(mm.regions)]
        for j, (x, y, z) in enumerate((mm.solid.xyz * s).tolist()):
            lines.append(f"{'GRID*':<8}{base + j + 1:>16d}{'':>16}{_f16(x)}{_f16(y)}*")
            lines.append(f"{'*':<8}{_f16(z)}")
        for i, r in enumerate(mm.regions):
            pid = len(prep.props) + i + 1
            for k in range(r.first, r.first + r.count):
                lines.extend(_cards("CTETRA", [e0 + k + 1, pid, *[base + int(v) for v in mm.solid.tets[k]]]))
        for i, r in enumerate(mm.regions):
            lines.extend(_cards("PSOLID", [len(prep.props) + i + 1, mats[r.part]]))
        for k, r in enumerate(mm.regions, start=len(mm.shell.groups) + 1):
            lines.append(f"$ HEXERA GROUP {k} SOLID_{r.name}")
            lines.append(f"$   {e0 + r.first + 1} THRU {e0 + r.first + r.count}")
        lines += sl.nastran_mpcs(mm.ties, sid=1)
        with open(path, "a", encoding="ascii", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        return {**got, "elements": int(e0 + len(mm.solid.tets)), "ties": len(mm.ties)}
    pid = len(prep.props) + 1
    mat = _solid_material(prep, mm.part)
    lines = [f"$ MIXED (SHLINK-4): the solid region of {mm.part}, {len(mm.solid.tets)} 10-node CTETRA in PSOLID {pid}"
             f" (MAT1 {mat}); the join is MPC set 1 - add MPC = 1 to the case control."]
    for j, (x, y, z) in enumerate((mm.solid.xyz * s).tolist()):
        lines.append(f"{'GRID*':<8}{base + j + 1:>16d}{'':>16}{_f16(x)}{_f16(y)}*")
        lines.append(f"{'*':<8}{_f16(z)}")
    for k, row in enumerate(mm.solid.tets.tolist()):
        lines.extend(_cards("CTETRA", [e0 + k + 1, pid, *[base + int(v) for v in row]]))
    lines.extend(_cards("PSOLID", [pid, mat]))
    lines += sl.nastran_mpcs(mm.ties, sid=1)
    with open(path, "a", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return {**got, "elements": int(e0 + len(mm.solid.tets)), "ties": len(mm.ties)}


def write_cdb(path: Path | str, mm: MixedMesh, *, units: str = "m", source: str = "") -> dict:
    prep = sw.prepare(mm.shell, units=units)
    got = sw.write_cdb(path, prep, units=units, source=source)
    s = prep.scale
    base, e0 = len(mm.shell.xyz), mm.shell.n_elements
    mat = _solid_material(prep, mm.part)
    text = Path(path).read_text(encoding="ascii")
    assert text.rstrip().endswith("FINISH")
    text = text.rstrip()[: -len("FINISH")]
    n = mm.solid.n_nodes
    # MIXED-9: each solid patch's tetrahedra carry their part's material
    mats = region_materials(prep, mm.regions) if mm.regions is not None else {}
    mat_of = np.full(len(mm.solid.tets), mat, dtype=np.int64)
    for r in mm.regions or []:
        mat_of[r.first:r.first + r.count] = mats[r.part]
    head_line = (f"! MIXED (SHLINK-4): the solid region of {mm.part}, {len(mm.solid.tets)} SOLID187 (type 2, MAT {mat}); "
                 "the join is constraint equations (CE).") if mm.regions is None else (
        f"! SOLID PATCHES (MIXED-9, tied by SHLINK-4): {len(mm.solid.tets)} SOLID187 (type 2) in {len(mm.regions)} "
        "patch(es), each its part's MAT; the join is constraint equations (CE).")
    lines = [head_line, *[f"!   MAT {mats[p]} = {p} (solid only)" for p in sorted(mats, key=lambda p: mats[p])
                          if p not in prep.parts], "ET,2,187",
             f"NBLOCK,6,SOLID,{base + n:>10d},{n:>10d}", "(3i9,6e21.13e3)"]
    for j, (x, y, z) in enumerate((mm.solid.xyz * s).tolist()):
        lines.append(f"{base + j + 1:>9d}{0:>9d}{0:>9d}{x:21.13E}{y:21.13E}{z:21.13E}")
    lines.append("N,R5.3,LOC,       -1,")
    lines += [f"EBLOCK,19,SOLID,{e0 + len(mm.solid.tets):>10d},{len(mm.solid.tets):>10d}", "(19i9)"]
    for k, row in enumerate(mm.solid.tets.tolist()):
        conn = [base + int(v) for v in row]
        head = [int(mat_of[k]), 2, 1, 1, 0, 0, 0, 0, 10, 0, e0 + k + 1]
        lines.append("".join(f"{v:>9d}" for v in head + conn[:8]))
        lines.append("".join(f"{v:>9d}" for v in conn[8:]))
    lines.append(f"{-1:>9d}")
    lines += sl.ansys_ces(mm.ties)
    lines.append("FINISH")
    Path(path).write_text(text + "\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {**got, "elements": int(e0 + len(mm.solid.tets)), "ties": len(mm.ties)}


def write_all(workspace: Path | str, mm: MixedMesh, *, units: str = "m", source: str = "",
              stem: str = "mixed") -> dict:
    """mixed.inp (CalculiX), mixed_abaqus.inp, mixed.bdf and mixed.cdb, one numbering. `stem`: the files' name
    ("shell": a shell run's own files, its solid patches in them - MIXED-9)."""
    ws = Path(workspace)
    return {"calculix": write_inp(ws / f"{stem}.inp", mm, units=units, source=source),
            "abaqus": write_inp(ws / f"{stem}_abaqus.inp", mm, units=units, source=source, solver="abaqus"),
            "nastran": write_bdf(ws / f"{stem}.bdf", mm, units=units, source=source),
            "ansys": write_cdb(ws / f"{stem}.cdb", mm, units=units, source=source)}


__all__ = ["Link", "MixedMesh", "OVERLAP_GROUP", "SolidPart", "SolidRegion", "boundary_triangles", "join",
           "read_solid_deck", "region_materials", "write_all", "write_bdf", "write_cdb", "write_inp"]
