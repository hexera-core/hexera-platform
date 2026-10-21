# Responsibility: Write one solid FEA mesh for each version-1 solver - Nastran (.bdf) and Ansys MAPDL (.cdb) beside the Abaqus / CalculiX deck (engines/gmsh/abaqus_deck.py) - with every CAD face a named set, the units stated, the solver's own element type and node order, and the same node and element numbers in every file; and read them back.
# Boundaries: pure over the internal mesh (abaqus_deck.SolidMesh: nodes, 4- or 10-node tetrahedra in solver order, the triangles of each CAD face). It knows no mesher. No solver was run on the Nastran or Ansys files: they are checked by reading them back and by each format's own rules, and are labelled "not yet tested in the real solver" wherever they are offered.
# Collaborates with: engines/gmsh/fea_adapter.py (writes all three after meshing - mesh once, export many), engines/gmsh/abaqus_deck.py (face sets, numbering), engines/fea_results.py (offers each file, per solver), api/v1/simulation.py (serves the one picked at download).
"""THE SOLVER FILES, FROM ONE MESH.

Node numbers, element numbers and Face_k are the same in every file (Face_k is the k-th CAD face,
as in the Abaqus deck). A 10-node tetrahedron's node order is the same in all three solvers:
corners 1-4, then the edge midpoints 1-2, 2-3, 3-1, 1-4, 2-4, 3-4 (Abaqus C3D10, Nastran CTETRA,
Ansys SOLID187).

NASTRAN (.bdf, MSC/NX bulk data):
  GRID* (large field, 16-character coordinates), CTETRA (property 1), PSOLID 1 -> material 1
  (the user adds MAT1 1). Each Face_k: a SET3 of its grids (DES=GRID) and a BSURFS of its element
  faces (element, then three corner grids), both numbered k; the names are in comments.
ANSYS (.cdb, MAPDL archive):
  ET,1,187 (SOLID187; SOLID285-type 4-node files use ET,1,285), NBLOCK, EBLOCK (solid format),
  and one node component per face (CMBLOCK, FACE_k), plus a component per readable CAD name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines.gmsh.abaqus_deck import (
    TET_FACE_CORNERS,
    SolidMesh,
    face_sets,
    readable_face_name,
)

#: The solvers the download offers, in order: (key, label, file, element type, real-solver tested)
SOLVERS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("calculix", "CalculiX", "mesh.inp", "C3D10", True),
    ("abaqus", "Abaqus", "mesh.inp", "C3D10", False),
    ("nastran", "Nastran", "mesh.bdf", "CTETRA (10-node)", False),
    ("ansys", "Ansys MAPDL", "mesh.cdb", "SOLID187", False),
)
NOT_TESTED = "not yet tested in the real solver"
#: The downloads an FEA mesh offers, one per solver file: (logical key, file, label). The labels say
#: which solver has really run the file (design 13: no Abaqus, Nastran or Ansys licence).
EXPORT_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("export_calculix_abaqus", "mesh.inp",
     "CalculiX / Abaqus deck (.inp) - solved in CalculiX; Abaqus not yet tested in the real solver"),
    ("export_nastran", "mesh.bdf", "Nastran bulk data (.bdf) - not yet tested in the real solver"),
    ("export_ansys", "mesh.cdb", "Ansys MAPDL archive (.cdb) - not yet tested in the real solver"),
)
_ANSYS_NAME = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class Prepared:
    """The mesh numbered once for every file."""
    node_ids: np.ndarray            # used nodes, in id order
    xyz: np.ndarray
    elements: np.ndarray            # (E, 4|10) node ids, solver order, numbered 1..E
    faces: list = field(default_factory=list)   # abaqus_deck.FaceSet, Face_1..Face_N


def prepare(mesh: SolidMesh) -> Prepared:
    """The numbering the Abaqus deck uses: elements grouped by solid (stable), numbered 1..E."""
    elements = np.asarray(mesh.elements, dtype=np.int64)
    order = np.argsort(np.asarray(mesh.element_volume), kind="stable")
    elements = elements[order]
    vol = np.asarray(mesh.element_volume)[order]
    fsets = face_sets(SolidMesh(mesh.node_ids, mesh.xyz, elements, vol, mesh.faces))
    ids = np.asarray(mesh.node_ids, dtype=np.int64)
    used = np.zeros(int(ids.max()) + 1, dtype=bool)
    used[elements.ravel()] = True
    keep = used[ids]
    o = np.argsort(ids[keep])
    return Prepared(node_ids=ids[keep][o], xyz=np.asarray(mesh.xyz, dtype=float)[keep][o],
                    elements=elements, faces=fsets)


# #
# Nastran
# #

def _f8(v) -> str:
    return f"{v:<8}"[:8] if isinstance(v, str) else f"{int(v):>8d}"


def _f16(x: float) -> str:
    """A real in 16 characters, never losing its exponent."""
    s = f"{x:.9E}"
    if len(s) > 16:
        s = f"{x:.8E}"
    return f"{s:>16}"


def _cards(name: str, fields: list) -> list[str]:
    """A small-field card: 8 fields per line, continuations marked '+'."""
    out, line = [], _f8(name)
    for i, v in enumerate(fields):
        if i and i % 8 == 0:
            out.append(line)
            line = _f8("+")
        line += _f8(v)
    out.append(line)
    return out


def write_bdf(path: Path | str, prep: Prepared, *, cad_names: dict[str, str] | None = None,
              units: str = "m", source: str = "") -> dict:
    cad_names = cad_names or {}
    per = prep.elements.shape[1]
    lines = [
        "$ Hexera FEA mesh - Nastran bulk data (MSC / NX).",
        "$ " + NOT_TESTED.capitalize() + ".",
        *([f"$ Source: {source}"] if source else []),
        f"$ UNITS: lengths in {'metres (m)' if units == 'm' else units}; give the rest in one consistent set",
        "$   (SI: N, Pa, kg/m^3, s).",
        f"$ Mesh only: {len(prep.elements)} {per}-node CTETRA in PSOLID 1, which names MAT1 1 - add MAT1 1,",
        "$   the SPCs, loads and the case control.",
        "$ Bulk data only: INCLUDE this file after BEGIN BULK in your run deck",
        "$   (it has no BEGIN BULK or ENDDATA of its own, so it drops into any deck).",
        "$ Every CAD face is a SET3 of its grids and a BSURFS of its element faces,",
        "$   both numbered k:",
    ]
    for k, f in enumerate(prep.faces, start=1):
        lines.append(f"$   {k:>6} = {f.name}" + (f" ({cad_names[f.name]})" if f.name in cad_names else ""))
    for i, (x, y, z) in zip(prep.node_ids.tolist(), prep.xyz.tolist(), strict=True):
        lines.append(f"{'GRID*':<8}{int(i):>16d}{'':>16}{_f16(x)}{_f16(y)}*")
        lines.append(f"{'*':<8}{_f16(z)}")
    for e, conn in enumerate(prep.elements.tolist(), start=1):
        lines.extend(_cards("CTETRA", [e, 1, *conn]))
    lines.extend(_cards("PSOLID", [1, 1]))
    for k, f in enumerate(prep.faces, start=1):
        lines.append(f"$ {f.name}" + (f" ({cad_names[f.name]})" if f.name in cad_names else ""))
        lines.extend(_cards("SET3", [k, "GRID", *f.nodes.tolist()]))
        tri = []
        for el, lab in zip(f.elements.tolist(), f.labels.tolist(), strict=True):
            conn = prep.elements[el - 1]
            tri.extend([el, *conn[list(TET_FACE_CORNERS[lab - 1])].tolist()])
        # BSURFS: ID, then (EID, G1, G2, G3) groups starting in field 6 (fields 3-5 blank)
        lines.extend(_cards("BSURFS", [k, "", "", "", *tri]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "faces": len(prep.faces), "elements": int(len(prep.elements))}


# #
# Ansys MAPDL
# #

def ansys_name(raw: str) -> str:
    """A component name MAPDL accepts: letters, digits and _, a letter first, 32 characters."""
    s = _ANSYS_NAME.sub("_", str(raw).strip()).upper() or "COMP"
    if not s[0].isalpha():
        s = "C_" + s
    return s[:32]


def _cmblock(name: str, ids: np.ndarray) -> list[str]:
    ids = np.unique(np.asarray(ids, dtype=np.int64))
    out = [f"CMBLOCK,{name},NODE,{len(ids):>8d}", "(8i10)"]
    for i in range(0, len(ids), 8):
        out.append("".join(f"{v:>10d}" for v in ids[i:i + 8].tolist()))
    return out


def write_cdb(path: Path | str, prep: Prepared, *, cad_names: dict[str, str] | None = None,
              units: str = "m", source: str = "") -> dict:
    cad_names = cad_names or {}
    per = prep.elements.shape[1]
    et = 187 if per == 10 else 285
    lines = [
        "! Hexera FEA mesh - Ansys MAPDL archive (CDB). " + NOT_TESTED.capitalize() + ".",
        *([f"! Source: {source}"] if source else []),
        f"! UNITS: lengths in {'metres (m)' if units == 'm' else units}; give the rest in one consistent set",
        "!   (SI: N, Pa, kg/m^3, s).",
        f"! Mesh only: {len(prep.elements)} SOLID{et} elements, material 1. Add MP,EX/PRXY/DENS for MAT 1,",
        "!   the supports, loads and the solution.",
        "! Every CAD face is a node component FACE_k (and one per readable CAD name).",
        "/PREP7",
        f"ET,1,{et}",
        f"NBLOCK,6,SOLID,{int(prep.node_ids.max()):>10d},{len(prep.node_ids):>10d}",
        "(3i9,6e21.13e3)",
    ]
    for i, (x, y, z) in zip(prep.node_ids.tolist(), prep.xyz.tolist(), strict=True):
        lines.append(f"{int(i):>9d}{0:>9d}{0:>9d}{x:21.13E}{y:21.13E}{z:21.13E}")
    lines.append("N,R5.3,LOC,       -1,")
    lines.append(f"EBLOCK,19,SOLID,{len(prep.elements):>10d},{len(prep.elements):>10d}")
    lines.append("(19i9)")
    for e, conn in enumerate(prep.elements.tolist(), start=1):
        # material, type, real, section, esys, birth/death, solid-model ref, shape flag,
        # number of nodes, (unused), element number, then up to 8 nodes; the rest on the next line
        head = [1, 1, 1, 1, 0, 0, 0, 0, per, 0, e]
        lines.append("".join(f"{v:>9d}" for v in head + conn[:8]))
        if per > 8:
            lines.append("".join(f"{v:>9d}" for v in conn[8:]))
    lines.append(f"{-1:>9d}")
    names_used = set()
    for f in prep.faces:
        nm = ansys_name(f.name)
        names_used.add(nm)
        lines.extend(_cmblock(nm, f.nodes))
    by_cad: dict[str, list] = {}
    for f in prep.faces:
        raw = readable_face_name(cad_names.get(f.name))
        if raw:
            by_cad.setdefault(raw, []).append(f.nodes)
    for raw, groups in by_cad.items():
        nm = ansys_name(raw)
        while nm in names_used:
            nm = ansys_name(nm[:30] + "_C")
        names_used.add(nm)
        lines.extend(_cmblock(nm, np.concatenate(groups)))
    lines.append("FINISH")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "faces": len(prep.faces), "elements": int(len(prep.elements))}


def write_all(workspace, mesh: SolidMesh, *, cad_names: dict[str, str] | None = None, units: str = "m",
              source: str = "") -> dict:
    """mesh.bdf and mesh.cdb beside the deck (the deck is written by abaqus_deck.write_inp)."""
    ws = Path(workspace)
    prep = prepare(mesh)
    return {"nastran": write_bdf(ws / "mesh.bdf", prep, cad_names=cad_names, units=units, source=source),
            "ansys": write_cdb(ws / "mesh.cdb", prep, cad_names=cad_names, units=units, source=source)}


# #
# reading them back (the round trip every file is held to)
# #

@dataclass
class ReadBack:
    nodes: dict[int, tuple[float, float, float]]
    elements: dict[int, tuple[int, ...]]
    face_nodes: dict[str, set[int]]                 # Face_k -> its node ids
    face_elements: dict[str, set[tuple[int, int]]]  # Face_k -> (element, face label 1..4) [inp, bdf]


def _face_label(conn: tuple[int, ...], corners: tuple[int, int, int]) -> int:
    c = set(corners)
    for lab, idx in enumerate(TET_FACE_CORNERS, start=1):
        if {conn[i] for i in idx} == c:
            return lab
    return 0


def read_bdf(path: Path | str) -> ReadBack:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    sets: dict[str, set[int]] = {}
    surfs: dict[str, set[tuple[int, int]]] = {}
    lines = Path(path).read_text(encoding="ascii").splitlines()
    cards: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln or ln.startswith("$") or ln.startswith(("BEGIN", "ENDDATA")):
            i += 1
            continue
        if ln.startswith("GRID*"):
            nid = int(ln[8:24])
            x, y = float(ln[40:56]), float(ln[56:72])
            z = float(lines[i + 1][8:24])
            nodes[nid] = (x, y, z)
            i += 2
            continue
        name = ln[:8].strip()
        fields = [ln[k:k + 8].strip() for k in range(8, len(ln), 8)]
        i += 1
        while i < len(lines) and lines[i].startswith("+"):
            fields += [lines[i][k:k + 8].strip() for k in range(8, len(lines[i]), 8)]
            i += 1
        cards.append((name, fields))
    for name, f in cards:
        if name == "CTETRA":
            elements[int(f[0])] = tuple(int(v) for v in f[2:] if v)
    for name, f in cards:
        if name == "SET3":
            sets[f"Face_{int(f[0])}"] = {int(v) for v in f[2:] if v}
        elif name == "BSURFS":
            vals = [int(v) for v in f[4:] if v]
            got = set()
            for j in range(0, len(vals), 4):
                e = vals[j]
                got.add((e, _face_label(elements[e], (vals[j + 1], vals[j + 2], vals[j + 3]))))
            surfs[f"Face_{int(f[0])}"] = got
    return ReadBack(nodes, elements, sets, surfs)


def read_cdb(path: Path | str) -> ReadBack:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    comps: dict[str, set[int]] = {}
    lines = Path(path).read_text(encoding="ascii").splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("NBLOCK"):
            i += 2
            while not lines[i].startswith("N,"):
                row = lines[i]
                nodes[int(row[0:9])] = (float(row[27:48]), float(row[48:69]), float(row[69:90]))
                i += 1
        elif ln.startswith("EBLOCK"):
            i += 2
            while lines[i].strip() != "-1":
                vals9 = [int(lines[i][k:k + 9]) for k in range(0, len(lines[i]), 9)]
                n, e, conn = vals9[8], vals9[10], vals9[11:]
                if n > 8:
                    i += 1
                    conn += [int(lines[i][k:k + 9]) for k in range(0, len(lines[i]), 9)]
                elements[e] = tuple(conn[:n])
                i += 1
        elif ln.startswith("CMBLOCK"):
            parts = ln.split(",")
            name, count = parts[1], int(parts[3])
            i += 2
            ids: list[int] = []
            while len(ids) < count:
                ids += [int(lines[i][k:k + 10]) for k in range(0, len(lines[i]), 10)]
                i += 1
            comps[name] = set(ids)
            continue
        i += 1
    faces = {f"Face_{n[5:]}": v for n, v in comps.items() if n.startswith("FACE_") and n[5:].isdigit()}
    return ReadBack(nodes, elements, faces, {})


__all__ = ["EXPORT_ARTIFACTS", "NOT_TESTED", "Prepared", "ReadBack", "SOLVERS", "ansys_name", "prepare", "read_bdf", "read_cdb",
           "write_all", "write_bdf", "write_cdb"]
