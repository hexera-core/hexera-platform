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
* TIE (bonded, but the parts could not be fused, so meshed apart): as contact, but the pair is
  tied: Abaqus/CalculiX *TIE; Nastran BCTABLE with IGLUE=1; Ansys CONTA174 KEYOPT(12)=5 (bonded).
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
    _coord,
    _write_ids,
    element_faces,
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


@dataclass
class Prepared:
    node_ids: np.ndarray
    xyz: np.ndarray
    elements: np.ndarray          # (E, 4|10), solver order, numbered 1..E, grouped by part
    element_part: np.ndarray      # (E,) the part of each element
    parts: list[dict] = field(default_factory=list)    # {"n", "id", "name", "first", "last"}
    faces: list[FaceSet] = field(default_factory=list)  # Part_k_Face_j, part by part
    joints: list[Joint] = field(default_factory=list)


def _face_set(name: str, elements: np.ndarray, tris: np.ndarray, allowed: np.ndarray) -> FaceSet | None:
    """The element faces under `tris` among the `allowed` elements (a part's own side)."""
    if not len(tris):
        return None
    pick = np.flatnonzero(allowed)
    e, lab = element_faces(elements[pick], tris)
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
        fs = _face_set(name, elements, tris_of(by_name[name]), side[p])
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
            a = _face_set(f"Bond{jid}_Part_{p}", elements, tris_of(tags), side[p])
            b = _face_set(f"Bond{jid}_Part_{q}", elements, tris_of(tags), side[q])
            if a is not None and b is not None:
                joints.append(Joint(name=f"Bond{jid}", kind=kind, parts=(p, q), sides=(a, b)))
            continue
        word = "Tie" if kind == "tie" else "Contact"
        fa = pair.get("faces") or {}
        a = _union(f"{word}{jid}_Part_{p}", [named[n] for n in fa.get(str(p)) or [] if n in named])
        b = _union(f"{word}{jid}_Part_{q}", [named[n] for n in fa.get(str(q)) or [] if n in named])
        if not len(a.elements) or not len(b.elements):
            continue
        # SLAVE = the smaller surface (the usual rule: the finer, smaller side follows the larger)
        if _area(xyz_of, a, elements) > _area(xyz_of, b, elements):
            a, b, p, q = b, a, q, p
        joints.append(Joint(name=f"{word}{jid}", kind=kind, parts=(p, q), sides=(a, b),
                            overlap=bool(pair.get("overlap"))))
    return Prepared(node_ids=node_ids, xyz=xyz, elements=elements, element_part=owner, parts=parts,
                    faces=faces, joints=joints)


# #
# Abaqus / CalculiX
# #

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
        head += ["** TIED: the parts could not be fused into one mesh, so each is meshed on its own and tied",
                 "**   where they touch (*TIE: bonded, the slave's nodes follow the master surface)."]
    if any(j.overlap for j in contacts):
        head.append("** Some contact pairs start overlapping (the CAD parts pass into each other there).")
    head.append("** Every face of every part is a *SURFACE and *NSET named Part_k_Face_j.")
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
            fh.write(f"*TIE, NAME={j.name}\n")
            fh.write(f"{j.sides[0].name}, {j.sides[1].name}\n")
    return {"element_type": etype, "elements": int(len(prep.elements)), "units": units,
            "parts": [{k: p[k] for k in ("id", "name", "first", "last")} for p in prep.parts],
            "face_sets": {f.name: i for i, f in enumerate(prep.faces, start=1)},
            "joints": [{"name": j.name, "kind": j.kind, "slave": j.sides[0].name, "master": j.sides[1].name,
                        "overlap": j.overlap} for j in prep.joints],
            "cad_face_names": {}, "face_aliases": {}}


# #
# Nastran
# #

def write_bdf(path: Path | str, prep: Prepared, *, units: str = "m", source: str = "") -> dict:
    from meshpipeline.engines.fea_writers import _cards, _f16

    per = prep.elements.shape[1]
    contacts = [j for j in prep.joints if j.kind in ("contact", "tie")]
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
            rows.append(f"{'':<8}{'SLAVE':<8}{j.parts[0]:>8d}{'0.':>8}{'0.':>8}{'0.':>8}{'0.':>8}"
                        f"{1 if j.kind == 'tie' else 0:>8d}{0:>8d}")
            rows.append(f"{'':<8}{'MASTERS':<8}{j.parts[1]:>8d}")
        lines.extend(rows)
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "faces": len(prep.faces), "elements": int(len(prep.elements)),
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
    contacts = [j for j in prep.joints if j.kind in ("contact", "tie")]
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
    ]
    if contacts:
        lines += ["! CONTACT: each pair is TARGE170 (master) and CONTA174 (slave) elements sharing one real",
                  "!   constant set; CONTA174 KEYOPT(12) = 5 (bonded, always) for a tied pair, 0 (standard,",
                  "!   frictionless) for contact - defaults to change to your case."]
    lines += ["/PREP7", f"ET,1,{et}"]
    if any(j.kind == "contact" for j in contacts):
        lines += ["ET,2,170", "ET,3,174", "KEYOPT,3,12,0"]
    if any(j.kind == "tie" for j in contacts):
        lines += ["ET,4,170", "ET,5,174", "KEYOPT,5,12,5"]
    for i, _j in enumerate(contacts, start=2):
        lines.append(f"R,{i}")
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
    return {"file": Path(path).name, "faces": len(prep.faces), "elements": int(len(prep.elements)),
            "parts": len(prep.parts), "pairs": len(contacts), "contact_elements": len(surf)}


def _elem_cmblock(name: str, ids: np.ndarray) -> list[str]:
    ids = np.unique(np.asarray(ids, dtype=np.int64))
    out = [f"CMBLOCK,{name},ELEM,{len(ids):>8d}", "(8i10)"]
    for i in range(0, len(ids), 8):
        out.append("".join(f"{v:>10d}" for v in ids[i:i + 8].tolist()))
    return out


def write_all(workspace, prep: Prepared, *, units: str = "m", source: str = "", connection: str = "bonded") -> dict:
    """mesh.inp, mesh.bdf and mesh.cdb for the assembly. Returns (the deck's record, exports)."""
    ws = Path(workspace)
    deck = write_inp(ws / "mesh.inp", prep, units=units, source=source, connection=connection)
    return {"deck": deck,
            "nastran": write_bdf(ws / "mesh.bdf", prep, units=units, source=source),
            "ansys": write_cdb(ws / "mesh.cdb", prep, units=units, source=source)}


__all__ = ["INTERACTION", "JOINT_ID_BASE", "Joint", "NOT_TESTED", "Prepared", "prepare", "write_all", "write_bdf",
           "write_cdb", "write_inp"]
