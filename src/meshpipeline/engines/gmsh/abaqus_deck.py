# Responsibility: Write a solid FEA mesh as a flat Abaqus / CalculiX input deck - 3D elements only, every CAD face a named element-face surface and node set.
# Boundaries: write_inp is pure numpy over mesh data (nodes, tetrahedra, the boundary triangles of each CAD face) and knows no mesher; from_gmsh is the one gmsh-facing reader. Any other tet engine can hand write_inp the same arrays.
# Collaborates with: engines/gmsh/driver.py (calls write_inp after meshing), cad/face_names.py (the readable CAD face names), tests/unit/engines/test_gmsh_fea_deck.py.
"""THE FEA DECK, WRITTEN FROM MESH DATA.

gmsh's own Abaqus writer puts every named face in the deck as flat CPS6 plane-stress elements
beside the C3D10 volume. Those have no section: CalculiX stops on them ("*ERROR in gen3delem"),
meshio cannot read the file, and Abaqus warns about elements without a section. A solver wants
the faces as SURFACES made of element faces instead, so this module writes the deck itself:

- *NODE, then *ELEMENT, TYPE=C3D10 (or C3D4) - the only elements in the file;
- *ELSET, ELSET=solid - every element (and Volume_k per solid when there are several);
- for EVERY CAD face Face_k: an *ELSET per face label, a *SURFACE, NAME=Face_k, TYPE=ELEMENT
  built from those (element, S1..S4) pairs, and an *NSET, NSET=Face_k of all its nodes;
- a name the plan gave a set of faces ("fixed", "load"), and a readable name the CAD file gave a
  face ("Mounting face"), as an extra alias surface and node set over the same element faces -
  Face_k is never dropped.

Face labels follow Abaqus and CalculiX (CCX 2.21 manual): S1 = 1-2-3, S2 = 1-4-2, S3 = 2-4-3,
S4 = 3-4-1 on the corner nodes. A flat file (no *PART) so CalculiX reads the same deck.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import numpy as np

#: The corner nodes (0-based, solver order) of tet face labels S1..S4.
TET_FACE_CORNERS: tuple[tuple[int, int, int], ...] = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))
#: Every node of the same faces on a 10-node tet: the corners, then the edge midpoints between
#: them (solver order: 5 = 1-2, 6 = 2-3, 7 = 3-1, 8 = 1-4, 9 = 2-4, 10 = 3-4).
TET10_FACE_NODES: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 4, 5, 6), (0, 3, 1, 7, 8, 4), (1, 3, 2, 8, 9, 5), (2, 3, 0, 9, 7, 6))
#: gmsh's 10-node tet differs from the solver order only by its last two nodes (gmsh node 8 is
#: edge 3-4, node 9 edge 2-4); gmsh's own INP, BDF and KEY writers use this same map.
GMSH_TET10_TO_SOLVER: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7, 9, 8)
ELEMENT_TYPE = {4: "C3D4", 10: "C3D10"}

#: Data lines stay well under the 256-character limit Abaqus sets (16 ids per set line).
_PER_LINE = 16
_LABEL = re.compile(r"[^A-Za-z0-9_\-]")
#: Names a CAD exporter gives a face when nobody named it ("Face", "Face 12", "face<3>"): no
#: better than Face_k, so they are not written as readable names.
_GENERIC_FACE_NAME = re.compile(r"^\s*(face|surface|surf|f)\s*[#<(\[_\-\s]*\d*\s*[>)\]]?\s*$",
                                re.IGNORECASE)


@dataclass
class SolidMesh:
    """A solid mesh as a solver deck needs it. Element connectivity is in SOLVER node order."""

    node_ids: np.ndarray                 # (N,) positive node numbers
    xyz: np.ndarray                      # (N, 3) coordinates, metres
    elements: np.ndarray                 # (E, 4|10) node numbers, solver order
    element_volume: np.ndarray           # (E,) the solid each element belongs to
    #: CAD face tag -> (M, 3+) node numbers of the boundary triangles meshed on it (corners first)
    faces: dict[int, np.ndarray] = field(default_factory=dict)
    #: (E,) the mesher's own element numbers, in `elements` order (empty when not known)
    element_tags: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))


@dataclass
class FaceSet:
    """One CAD face as written: its deck name, the element faces on it and its nodes."""

    name: str
    tag: int
    elements: np.ndarray                 # (M,) 1-based element numbers in the deck
    labels: np.ndarray                   # (M,) 1..4 = S1..S4
    nodes: np.ndarray                    # sorted node numbers


def element_faces(elements: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(element index, face label 1..4) of the tet face under every triangle of ``faces``.

    Matched on the three corner nodes, so the answer does not depend on how the triangle is
    oriented. A triangle that is no element's face raises ValueError - a face set with a hole in
    it would put a load on part of a face. Where two elements share the triangle (a face between
    two solids) the lower element index is taken, so the surface sits on one side, the same
    side every run."""
    tets = np.asarray(elements, dtype=np.int64)[:, :4]
    tris = np.sort(np.asarray(faces, dtype=np.int64)[:, :3], axis=1)
    # face f of element e sits at row 4*e + f, so the first row of a run is the lowest element
    tf = np.sort(np.stack([tets[:, list(c)] for c in TET_FACE_CORNERS], axis=1).reshape(-1, 3),
                 axis=1)
    rows = np.concatenate([tf, tris])
    is_tri = np.concatenate([np.zeros(len(tf), dtype=np.int8), np.ones(len(tris), dtype=np.int8)])
    order = np.lexsort((is_tri, rows[:, 2], rows[:, 1], rows[:, 0]))   # stable: tet rows by index
    srt = rows[order]
    new = np.ones(len(srt), dtype=bool)
    new[1:] = np.any(srt[1:] != srt[:-1], axis=1)
    start = np.flatnonzero(new)[np.cumsum(new) - 1]                   # first row of each run
    first = order[start]                                               # a tet face if the run has one
    tri_pos = np.flatnonzero(order >= len(tf))
    tri_idx = order[tri_pos] - len(tf)
    hit = first[tri_pos]
    ok = hit < len(tf)
    if not ok.all():
        bad = int((~ok).sum())
        raise ValueError(f"{bad} boundary triangle(s) are not the face of any element")
    elem = np.empty(len(tris), dtype=np.int64)
    label = np.empty(len(tris), dtype=np.int64)
    elem[tri_idx] = hit // 4
    label[tri_idx] = hit % 4 + 1
    return elem, label


def face_nodes(element: np.ndarray, label: int) -> np.ndarray:
    """The node numbers of face ``label`` (1..4) of one element in solver order."""
    el = np.asarray(element)
    idx = TET10_FACE_NODES[label - 1] if len(el) == 10 else TET_FACE_CORNERS[label - 1]
    return el[list(idx)]


def deck_label(name: str) -> str:
    """A set name every solver accepts: letters, digits, _ and -, starting with a letter, at most
    80 characters (Abaqus rules; CalculiX and Abaqus compare them without case)."""
    s = _LABEL.sub("_", str(name).strip()) or "group"
    if not s[0].isalpha():
        s = f"G_{s}"
    return s[:80].rstrip("_") or "group"


def readable_face_name(name: str | None) -> str:
    """The CAD's own name for a face, or "" when it carries none worth writing (empty, or an
    exporter's generic "Face 12")."""
    n = " ".join(str(name or "").split())
    return "" if not n or _GENERIC_FACE_NAME.match(n) else n


def face_sets(mesh: SolidMesh) -> list[FaceSet]:
    """Face_1..Face_N for the CAD faces in tag order (Face_k = the k-th face; for a CAD solid
    that is its own tag)."""
    elements = np.asarray(mesh.elements, dtype=np.int64)
    # a face's nodes are read off the element faces themselves (midside nodes included), so the
    # node set and the surface are the same face by construction
    table = np.asarray(TET10_FACE_NODES if elements.shape[1] == 10 else TET_FACE_CORNERS)
    tags = sorted(mesh.faces)
    if not tags:
        return []
    # ONE MATCH FOR EVERY FACE: the element faces are sorted once and every CAD face's triangles
    # looked up together (each triangle's answer is its own, so the sets are the same as matching
    # face by face) - face by face it sorted the whole mesh once per CAD face, 62 of 170 s on a
    # 290k-element, 36-face part (fix pack, 22 Oct)
    tris = [np.asarray(mesh.faces[t], dtype=np.int64)[:, :3] for t in tags]
    e_all, lab_all = element_faces(elements, np.concatenate(tris))
    cuts = np.cumsum([len(t) for t in tris])[:-1]
    out = []
    for k, (tag, e, lab) in enumerate(zip(tags, np.split(e_all, cuts), np.split(lab_all, cuts), strict=True),
                                      start=1):
        nodes = np.unique(elements[e[:, None], table[lab - 1]])
        out.append(FaceSet(name=f"Face_{k}", tag=int(tag), elements=e + 1, labels=lab,
                           nodes=nodes))
    return out


#: CalculiX reads every number of a data line as a Fortran f20.0 field: a value written in more than
#: 20 characters is cut, silently ("-5.76226301486252e-18" was read as -0.576, which turned a
#: groove's elements inside out - FEA build step 7's accuracy suite found it). 13 significant
#: figures fit any value in 20 characters, and still resolve a nanometre on a 10 km part.
COORD_DIGITS = 13


def _coord(v: float) -> str:
    s = f"{float(v):.{COORD_DIGITS}g}"
    return "0" if s in ("-0", "0") else s


def _write_ids(fh: IO[str], values, per: int = _PER_LINE) -> None:
    v = np.asarray(values, dtype=np.int64)
    for i in range(0, len(v), per):
        fh.write(", ".join(map(str, v[i:i + per].tolist())) + "\n")


def _alias_names(fsets: list[FaceSet], vols: list[int], aliases: dict[str, list[int]],
                 readable: dict[int, str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Deck names for the plan's groups, then for the CAD's readable face names. A name never
    takes a Face_k, a Face_k_S<n> element set, "solid" or a Volume_k; a readable name that would
    only repeat a plan group over the same faces is not written twice."""
    by_tag = {f.tag: f for f in fsets}
    reserved = {"solid"} | {f.name.lower() for f in fsets}
    if len(vols) > 1:
        reserved |= {f"volume_{i}" for i in range(1, len(vols) + 1)}

    def claim(raw: str, suffix: str) -> str:
        name = deck_label(raw)
        if name.lower() in reserved or any(name.lower().startswith(f"{f.name.lower()}_s")
                                           for f in fsets):
            name = deck_label(f"{name}_{suffix}")
        while name.lower() in reserved:
            name = deck_label(f"{name}_")
        reserved.add(name.lower())
        return name

    out: dict[str, list[str]] = {}
    for raw, tags in aliases.items():
        members = [by_tag[int(t)].name for t in tags if int(t) in by_tag]
        if members:
            out[claim(raw, "group")] = members
    by_name: dict[str, list[str]] = {}
    for tag, raw in sorted(readable.items()):
        if int(tag) in by_tag and readable_face_name(raw):
            by_name.setdefault(readable_face_name(raw), []).append(by_tag[int(tag)].name)
    cad_names: dict[str, str] = {}
    for raw, members in by_name.items():
        same = deck_label(raw).lower()
        if any(n.lower() == same and sorted(m) == sorted(members) for n, m in out.items()):
            name = next(n for n in out if n.lower() == same)
        else:
            name = claim(raw, "cad")
            out[name] = members
        for m in members:
            cad_names[m] = name
    return out, cad_names


def write_inp(path: Path | str, mesh: SolidMesh, aliases: dict[str, list[int]] | None = None,
              *, face_names: dict[int, str] | None = None, units: str = "m",
              source: str = "") -> dict:
    """Write the deck; return what it holds for the run's record: {"face_sets": {Face_k: CAD
    tag}, "face_aliases": {name: [Face_k, ...]}, "cad_face_names": {Face_k: name}, "elements",
    "element_type", "units"}. ``face_names`` maps a CAD face tag to the name the CAD file gave it."""
    elements = np.asarray(mesh.elements, dtype=np.int64)
    per = elements.shape[1]
    if per not in ELEMENT_TYPE:
        raise ValueError(f"only 4- and 10-node tetrahedra are written, not {per}-node elements")
    etype = ELEMENT_TYPE[per]
    # elements numbered 1..E, grouped by solid, so every solid is one run of numbers
    order = np.argsort(np.asarray(mesh.element_volume), kind="stable")
    elements = elements[order]
    vol = np.asarray(mesh.element_volume)[order]
    fsets = face_sets(SolidMesh(mesh.node_ids, mesh.xyz, elements, vol, mesh.faces))
    vols = list(dict.fromkeys(int(v) for v in vol))
    alias_out, cad_names = _alias_names(fsets, vols, aliases or {}, face_names or {})

    head: list[str] = [
        "** Hexera FEA mesh deck - Abaqus / CalculiX input (flat file, no *PART).",
        *([f"** Source: {source}"] if source else []),
        f"** UNITS: lengths in {'metres (m)' if units == 'm' else units}. The deck carries no",
        "**   other unit: give the rest in one consistent set (SI: N, Pa, kg/m^3, s).",
        f"** Mesh only: {len(elements)} {etype} elements in ELSET=solid. Add *MATERIAL,",
        "**   *SOLID SECTION, ELSET=solid, boundary conditions, loads and a *STEP.",
        "** Every CAD face is a named *SURFACE (element faces) and *NSET of the same name:",
    ]
    pairs = [f"{f.name} = CAD face {f.tag}"
             + (f" ({cad_names[f.name]})" if f.name in cad_names else "") for f in fsets]
    head.extend("**   " + ", ".join(pairs[i:i + 6]) for i in range(0, len(pairs), 6))
    if alias_out:
        head.append("** Named groups (a *SURFACE and *NSET over the same faces):")
    for name, members in alias_out.items():
        head.extend(f"**   {name}{' =' if i == 0 else ' +'} {', '.join(members[i:i + 10])}"
                    for i in range(0, len(members), 10))

    ids = np.asarray(mesh.node_ids, dtype=np.int64)
    xyz = np.asarray(mesh.xyz, dtype=float)
    used = np.zeros(int(ids.max()) + 1, dtype=bool)
    used[elements.ravel()] = True
    keep = used[ids]
    num = np.arange(1, len(elements) + 1)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(head) + "\n*NODE\n")
        fh.writelines(f"{int(i)}, {_coord(x)}, {_coord(y)}, {_coord(z)}\n"
                      for i, (x, y, z) in zip(ids[keep].tolist(), xyz[keep].tolist(), strict=True))
        vol_ranges = []
        for k, v in enumerate(vols, start=1):
            sel = vol == v
            elset = "solid" if len(vols) == 1 else f"Volume_{k}"
            vol_ranges.append((int(num[sel][0]), int(num[sel][-1])))
            fh.write(f"*ELEMENT, TYPE={etype}, ELSET={elset}\n")
            fh.writelines(f"{e}, " + ", ".join(map(str, conn)) + "\n"
                          for e, conn in zip(num[sel].tolist(), elements[sel].tolist(),
                                             strict=True))
        if len(vols) > 1:
            fh.write("*ELSET, ELSET=solid, GENERATE\n")
            fh.writelines(f"{a}, {b}, 1\n" for a, b in vol_ranges)
        for f in fsets:
            for lab in range(1, 5):
                sel = np.sort(f.elements[f.labels == lab])
                if len(sel):
                    fh.write(f"*ELSET, ELSET={f.name}_S{lab}\n")
                    _write_ids(fh, sel)
            fh.write(f"*SURFACE, NAME={f.name}, TYPE=ELEMENT\n")
            fh.writelines(f"{f.name}_S{lab}, S{lab}\n" for lab in range(1, 5)
                          if (f.labels == lab).any())
            fh.write(f"*NSET, NSET={f.name}\n")
            _write_ids(fh, f.nodes)
        by_name = {f.name: f for f in fsets}
        for name, members in alias_out.items():
            mem = [by_name[m] for m in members]
            fh.write(f"*SURFACE, NAME={name}, TYPE=ELEMENT\n")
            fh.writelines(f"{f.name}_S{lab}, S{lab}\n" for f in mem for lab in range(1, 5)
                          if (f.labels == lab).any())
            fh.write(f"*NSET, NSET={name}\n")
            _write_ids(fh, np.unique(np.concatenate([f.nodes for f in mem])))
    return {"element_type": etype, "elements": int(len(elements)), "units": units,
            "face_sets": {f.name: f.tag for f in fsets},
            "face_aliases": alias_out,
            "cad_face_names": cad_names}


def from_gmsh(gmsh, volumes: list[int]) -> SolidMesh:
    """The current gmsh model's volume mesh over ``volumes`` and the triangles of every CAD
    face that bounds one of them."""
    tags, coords, _ = gmsh.model.mesh.getNodes()
    xyz = np.asarray(coords, dtype=float).reshape(-1, 3)
    conn_blocks, vol_blocks, tag_blocks = [], [], []
    for v in volumes:
        etypes, etags_v, enodes = gmsh.model.mesh.getElements(3, int(v))
        for et, et_tags, en in zip(etypes, etags_v, enodes, strict=True):
            tag_blocks.append(np.asarray(et_tags, dtype=np.int64))
            nn = int(gmsh.model.mesh.getElementProperties(int(et))[3])
            if int(et) == 11:
                c = np.asarray(en, dtype=np.int64).reshape(-1, 10)[:, list(GMSH_TET10_TO_SOLVER)]
            elif int(et) == 4:
                c = np.asarray(en, dtype=np.int64).reshape(-1, 4)
            else:
                raise ValueError(f"gmsh element type {int(et)} ({nn} nodes) is not a tetrahedron")
            conn_blocks.append(c)
            vol_blocks.append(np.full(len(c), int(v), dtype=np.int64))
    if not conn_blocks:
        raise ValueError("no volume elements to write")
    elements = np.concatenate(conn_blocks)
    faces: dict[int, np.ndarray] = {}
    bounding = {int(t) for v in volumes
                for _, t in gmsh.model.getBoundary([(3, int(v))], oriented=False, combined=False)}
    for s in sorted(bounding):
        etypes, _etags, enodes = gmsh.model.mesh.getElements(2, s)
        # only the corner nodes are matched against the elements: 3- and 6-node triangles alike
        blocks = [np.asarray(en, dtype=np.int64).reshape(-1, 3 if int(et) == 2 else 6)[:, :3]
                  for et, en in zip(etypes, enodes, strict=True) if int(et) in (2, 9)]
        if blocks:
            faces[s] = np.concatenate(blocks)
    return SolidMesh(node_ids=np.asarray(tags, dtype=np.int64), xyz=xyz, elements=elements,
                     element_volume=np.concatenate(vol_blocks), faces=faces,
                     element_tags=np.concatenate(tag_blocks))
