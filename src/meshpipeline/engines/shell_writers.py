# Responsibility: Write one SHELL mesh (engines/shell_mesh.ShellMesh) for each solver - Nastran bulk data for FEMAP
# (.bdf: GRID, CQUAD4 / CTRIA3, PSHELL per part and wall), CalculiX (.inp: S4 / S3) and Abaqus (.inp: S4R / S3), *SHELL SECTION, and
# Ansys MAPDL (.cdb: SHELL181, SECTYPE / SECDATA) - with the same node and element numbers in every file, a group
# per CAD face pair and per part, a node set per sewn junction, and read each back.
# Boundaries: pure over the ShellMesh. Mesh only, as the solid decks (engines/fea_assembly_deck.py): part k's
# properties name material k (MAT1 k / MAT_k / MAT k), which the user adds; the header says so. Only CalculiX has
# been run on these files (tests/accuracy); the Nastran and Ansys files are checked by reading them back and are
# labelled "not yet tested in the real solver".
# Collaborates with: engines/shell_mesh.py (the mesh), engines/gmsh/shell_adapter.py (makes it),
# engines/fea_writers.py (the card helpers and the Ansys name rules).
"""THE SHELL SOLVER FILES, FROM ONE SHELL MESH.

PROPERTIES (SHMESH-10, design section 4): one per part and wall, named "<part>_t<wall>mm" (Part_3_t6mm). A wall
that varies over its mid-face (a tapered wall, SHELL-9) keeps its corner thicknesses: Nastran CQUAD4 / CTRIA3
T1..T4, CalculiX / Abaqus *NODAL THICKNESS, Ansys one section per element wall to 1 micrometre (SHELL181 sections
are per element, so an element carries its corners' mean).
GROUPS: every element group (a CAD face pair "Part_3_Face_12+Face_15", every part) is an ELSET (Abaqus / CalculiX),
an element component (Ansys CMBLOCK ... ELEM) and, Nastran bulk data having no group card, a named comment block
of element ranges ("$ HEXERA GROUP k name" then "$   a THRU b" lines) that read_bdf reads back. Every junction
(nodes shared by elements of two or more mid-faces) is a node set J_<mid>_<mid>... in every file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines.fea_writers import NOT_TESTED, _cards, _f16, ansys_name, unique_ansys_name
from meshpipeline.engines.shell_mesh import ShellMesh, edge_users

#: the shell files the download offers: (key, label, file, element types, real-solver tested)
SHELL_SOLVERS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("calculix", "CalculiX", "shell.inp", "S4 / S3", True),
    ("abaqus", "Abaqus", "shell_abaqus.inp", "S4R / S3", False),
    ("nastran", "Nastran (FEMAP)", "shell.bdf", "CQUAD4 / CTRIA3", False),
    ("ansys", "Ansys MAPDL", "shell.cdb", "SHELL181", False),
)
#: a wall is written to this many millimetres (1 micrometre): two walls equal to it share a property
WALL_DIGITS = 3
#: Mindlin plate constants (Nastran PSHELL 12I/T^3 and TS/T)
BENDING_RATIO = 1.0
SHEAR_RATIO = 5.0 / 6.0


def _num(v: float) -> str:
    return f"{float(v):.6g}"


def _r8(x: float) -> str:
    """A real in one small field (8 characters), as many digits as fit (the 1.2345-3 form)."""
    x = float(x)
    plain = f"{x:.7g}"
    if len(plain) <= 8 and "e" not in plain:
        return plain if "." in plain else plain + "."
    for d in range(6, -1, -1):
        mant, exp = f"{x:.{d}E}".split("E")
        out = mant + (str(int(exp)) if int(exp) < 0 else "+" + str(int(exp)))
        if len(out) <= 8:
            return out
    return plain[:8]


def _real(v: str) -> float:
    """A Nastran small-field real (1.5, 1.2345-3, 1.2E-3)."""
    v = v.strip()
    k = max(v.rfind("-"), v.rfind("+"))
    if k > 0 and v[k - 1] not in "Ee":
        v = v[:k] + "E" + v[k:]
    return float(v)


def wall_label(t_mm: float) -> str:
    """6 -> '6', 1.5 -> '1p5' (a name-safe wall in millimetres)."""
    return _num(round(float(t_mm), WALL_DIGITS)).replace(".", "p")


@dataclass
class ShellPrepared:
    """The numbering every file shares: node k is mesh node k-1, element e is mesh element e-1 (quads first)."""
    mesh: ShellMesh
    scale: float                                 # file length per millimetre
    parts: list[str]                             # material m (1-based) is parts[m - 1]
    props: list[tuple[str, float, int]]          # property p (1-based): (name, wall mm, material)
    prop_of: np.ndarray                          # (elements,) 1-based property of each element
    corner: dict[int, np.ndarray] = field(default_factory=dict)   # element -> corner walls (mm), variable only
    junctions: dict[str, np.ndarray] = field(default_factory=dict)  # name -> 1-based node ids
    offsets: list[float] = field(default_factory=list)            # property p's mid-plane off its nodes (mm)

    @property
    def n_nodes(self) -> int:
        return int(len(self.mesh.xyz))


def junction_sets(mesh: ShellMesh) -> dict[str, np.ndarray]:
    """The nodes of every sewn junction: an element edge whose elements lie on two or more mid-faces. Named after
    the mid-faces it joins (J_Mid_1_Mid_4), 1-based node ids, sorted."""
    out: dict[str, set[int]] = {}
    for (a, b), users in edge_users(mesh).items():
        mids = sorted({mesh.mid[e] for e, _s in users}, key=lambda s: int(s.split("_")[-1]))
        if len(mids) < 2:
            continue
        out.setdefault("J_" + "_".join(mids), set()).update((a + 1, b + 1))
    return {k: np.array(sorted(v), dtype=np.int64) for k, v in sorted(out.items())}


def prepare(mesh: ShellMesh, *, units: str = "m") -> ShellPrepared:
    """Number the parts (materials) and the part-and-wall properties, in the mesh's own order."""
    scale = 1e-3 if units == "m" else 1.0
    parts = sorted(set(mesh.part), key=lambda s: (len(s), s))
    mat = {p: i + 1 for i, p in enumerate(parts)}
    props: list[tuple[str, float, int]] = []
    offsets: list[float] = []
    index: dict[tuple[str, float, float, bool], int] = {}
    prop_of = np.zeros(mesh.n_elements, dtype=np.int64)
    varies = (np.asarray(mesh.varies, dtype=bool) if mesh.varies is not None and mesh.node_wall is not None
              else np.zeros(mesh.n_elements, dtype=bool))
    zoffs = np.asarray(mesh.zoffs, dtype=float) if mesh.zoffs is not None else np.zeros(mesh.n_elements)
    for e in range(mesh.n_elements):
        # a property per part and wall; apart for an offset mid-plane (SHELL-7) and for a varying wall (its
        # section reads the corners' walls: *NODAL THICKNESS covers every element of its set)
        z = round(float(zoffs[e]), WALL_DIGITS) + 0.0
        key = (mesh.part[e], round(float(mesh.wall[e]), WALL_DIGITS), z, bool(varies[e]))
        if key not in index:
            name = f"{key[0]}_t{wall_label(key[1])}mm"
            if z:
                name += f"_z{'m' if z < 0 else ''}{wall_label(abs(z))}"
            if key[3]:
                name += "_varies"
            props.append((name, key[1], mat[key[0]]))
            offsets.append(z)
            index[key] = len(props)
        prop_of[e] = index[key]
    corner: dict[int, np.ndarray] = {}
    if mesh.node_wall is not None and mesh.varies is not None:
        for e in np.flatnonzero(mesh.varies).tolist():
            corner[e] = np.asarray(mesh.node_wall)[mesh.element_nodes(e)]
    return ShellPrepared(mesh=mesh, scale=scale, parts=parts, props=props, prop_of=prop_of, corner=corner,
                         junctions=junction_sets(mesh), offsets=offsets)


def _ranges(ids: np.ndarray) -> list[tuple[int, int]]:
    ids = np.unique(np.asarray(ids, dtype=np.int64))
    out: list[tuple[int, int]] = []
    for v in ids.tolist():
        if out and v == out[-1][1] + 1:
            out[-1] = (out[-1][0], v)
        else:
            out.append((v, v))
    return out


def _units_line(units: str, mark: str) -> list[str]:
    return [f"{mark} UNITS: lengths and walls in {'metres (m)' if units == 'm' else units}; give the rest in one "
            "consistent set", f"{mark}   (SI: N, Pa, kg/m^3, s)."]


# ------------------------------------------------------------------------------------------- Nastran ----
def write_bdf(path: Path | str, prep: ShellPrepared, *, units: str = "m", source: str = "") -> dict:
    m, s = prep.mesh, prep.scale
    q = len(m.quads)
    lines = [
        "$ Hexera shell mesh - Nastran bulk data (MSC / NX), for FEMAP.",
        "$ " + NOT_TESTED.capitalize() + ".",
        *([f"$ Source: {source}"] if source else []),
        *_units_line(units, "$"),
        f"$ Mesh only: {q} CQUAD4 and {len(m.trias)} CTRIA3 in {len(prep.props)} PSHELL. Part k's PSHELLs name",
        "$   MAT1 k - add MAT1 k for every part, the SPCs, loads and the case control:",
        *[f"$   MAT1 {i} = {p}" for i, p in enumerate(prep.parts, start=1)],
        "$ Bulk data only: INCLUDE this file after BEGIN BULK in your run deck.",
        "$ Groups (Nastran has no group card): '$ HEXERA GROUP k name' then its element ranges.",
    ]
    for i, (name, _t, _mid) in enumerate(prep.props, start=1):
        lines.append(f"$ Femap Property {i} : {name}")
    for i, p in enumerate(prep.parts, start=1):
        lines.append(f"$ Femap Material {i} : {p}")
    for k, (x, y, z) in enumerate((m.xyz * s).tolist(), start=1):
        lines.append(f"{'GRID*':<8}{k:>16d}{'':>16}{_f16(x)}{_f16(y)}*")
        lines.append(f"{'*':<8}{_f16(z)}")
    for e, conn in enumerate(m.elements()):
        ids = [int(v) + 1 for v in conn]
        card = "CQUAD4" if len(ids) == 4 else "CTRIA3"
        fields: list = [e + 1, int(prep.prop_of[e]), *ids]
        z = prep.offsets[int(prep.prop_of[e]) - 1] if prep.offsets else 0.0
        if e in prep.corner:
            # after the corners: THETA, ZOFFS (and CTRIA3's blank 9th field), then the continuation's blank and
            # TFLAG, then T1..Tn the corner walls
            pad = [""] * (4 if card == "CQUAD4" else 5)
            if z:
                pad[1] = _r8(z * s)
            fields += [*pad, *[_r8(t * s) for t in prep.corner[e].tolist()]]
        elif z:
            # SHELL-7: ZOFFS, the element's mid-plane off its grids' plane along its normal
            fields += ["", _r8(z * s)]
        lines.extend(_cards(card, fields))
    for i, (_name, t, mid) in enumerate(prep.props, start=1):
        lines.extend(_cards("PSHELL", [i, mid, _r8(t * s), mid, _r8(BENDING_RATIO), mid, _r8(SHEAR_RATIO)]))
    for k, (name, els) in enumerate(sorted(m.groups.items()), start=1):
        lines.append(f"$ HEXERA GROUP {k} {name}")
        for a, b in _ranges(np.asarray(els) + 1):
            lines.append(f"$   {a} THRU {b}")
    for k, (name, nodes) in enumerate(prep.junctions.items(), start=1):
        lines.append(f"$ {name}")
        lines.extend(_cards("SET3", [k, "GRID", *nodes.tolist()]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "elements": m.n_elements, "properties": len(prep.props)}


@dataclass
class ShellReadBack:
    nodes: dict[int, tuple[float, float, float]]
    elements: dict[int, tuple[int, ...]]
    prop: dict[int, int]                                  # element -> property
    walls: dict[int, float]                               # property -> wall
    corner: dict[int, tuple[float, ...]]                  # element -> corner walls (variable only)
    groups: dict[str, set[int]]
    junctions: dict[str, set[int]]
    names: dict[int, str] = field(default_factory=dict)  # property -> name
    zoffs: dict[int, float] = field(default_factory=dict)  # element -> its mid-plane offset (offset elements only)


def read_bdf(path: Path | str) -> ShellReadBack:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    prop: dict[int, int] = {}
    walls: dict[int, float] = {}
    corner: dict[int, tuple[float, ...]] = {}
    groups: dict[str, set[int]] = {}
    junctions: dict[str, set[int]] = {}
    names: dict[int, str] = {}
    zoffs: dict[int, float] = {}
    lines = Path(path).read_text(encoding="ascii").splitlines()
    group: str | None = None
    last_comment = ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("$ Femap Property "):
            k, _, nm = ln[len("$ Femap Property "):].partition(" : ")
            names[int(k)] = nm
        if ln.startswith("$ HEXERA GROUP "):
            _k, group = ln[len("$ HEXERA GROUP "):].split(" ", 1)
            groups[group] = set()
            i += 1
            continue
        if group is not None and ln.startswith("$   ") and " THRU " in ln:
            a, b = (int(v) for v in ln[4:].split(" THRU "))
            groups[group].update(range(a, b + 1))
            i += 1
            continue
        if ln.startswith("$"):
            group = None
            last_comment = ln[2:].strip()
            i += 1
            continue
        group = None
        if ln.startswith("GRID*"):
            nodes[int(ln[8:24])] = (float(ln[40:56]), float(ln[56:72]), float(lines[i + 1][8:24]))
            i += 2
            continue
        name = ln[:8].strip()
        f = [ln[k:k + 8].strip() for k in range(8, len(ln), 8)]
        i += 1
        while i < len(lines) and lines[i].startswith("+"):
            f += [lines[i][k:k + 8].strip() for k in range(8, len(lines[i]), 8)]
            i += 1
        if name in ("CQUAD4", "CTRIA3"):
            n = 4 if name == "CQUAD4" else 3
            e = int(f[0])
            elements[e] = tuple(int(v) for v in f[2:2 + n])
            prop[e] = int(f[1])
            t = [_real(v) for v in f[10:10 + n] if v]
            if t:
                corner[e] = tuple(t)
            zf = f[3 + n] if len(f) > 3 + n else ""
            if zf:
                zoffs[e] = _real(zf)
        elif name == "PSHELL":
            walls[int(f[0])] = _real(f[2])
        elif name == "SET3":
            junctions[last_comment] = {int(v) for v in f[2:] if v}
    return ShellReadBack(nodes, elements, prop, walls, corner, groups, junctions, names, zoffs)


# ---------------------------------------------------------------------------- Abaqus / CalculiX ----
def _id_lines(ids) -> list[str]:
    ids = [int(v) for v in ids]
    return [", ".join(str(v) for v in ids[i:i + 16]) for i in range(0, len(ids), 16)]


#: THE QUAD ELEMENT PER SOLVER. CalculiX has no shell element of its own: it expands each shell into solid
#: elements, S4R into ONE layer of reduced-integration C3D8R, which has next to no bending stiffness (a 200 x 20 x 2
#: cantilever bent 96 mm for 9.4: ten times too far); S4 expands into C3D8I (incompatible modes) and is right
#: (0.6% off). Abaqus's S4R is a true shell and its default.
QUAD_TYPE = {"calculix": "S4", "abaqus": "S4R"}


def write_inp(path: Path | str, prep: ShellPrepared, *, units: str = "m", source: str = "", solver: str = "calculix",
              materials: dict[int, list[str]] | None = None) -> dict:
    """The CalculiX (S4) or Abaqus (S4R) deck: *NODE, *ELEMENT quads and S3, an ELSET per property (P_k), per group and the
    whole mesh (EALL), an NSET per junction, a *SHELL SECTION per property naming MAT_m (*NODAL THICKNESS where
    the wall varies). `materials` (tests only): material lines to write in place of the 'add' note."""
    m, s = prep.mesh, prep.scale
    q = len(m.quads)
    quad = QUAD_TYPE[solver]
    lines = [
        f"** Hexera shell mesh - {'CalculiX' if solver == 'calculix' else 'Abaqus'} input.",
        *([] if solver == "calculix" else ["** " + NOT_TESTED.capitalize() + "."]),
        *([f"** Source: {source}"] if source else []),
        *_units_line(units, "**"),
        f"** Mesh only: {q} {quad} and {len(m.trias)} S3. Part k's sections name MATERIAL MAT_k - add *MATERIAL,",
        "**   NAME=MAT_k for every part, the boundary conditions, loads and the *STEP:",
        *[f"**   MAT_{i} = {p}" for i, p in enumerate(prep.parts, start=1)],
        "*NODE, NSET=NALL",
    ]
    for k, (x, y, z) in enumerate((m.xyz * s).tolist(), start=1):
        lines.append(f"{k}, {x:.12g}, {y:.12g}, {z:.12g}")
    if q:
        lines.append(f"*ELEMENT, TYPE={quad}, ELSET=EALL")
        lines += [f"{e + 1}, " + ", ".join(str(int(v) + 1) for v in m.quads[e]) for e in range(q)]
    if len(m.trias):
        lines.append("*ELEMENT, TYPE=S3, ELSET=EALL")
        lines += [f"{q + r + 1}, " + ", ".join(str(int(v) + 1) for v in m.trias[r]) for r in range(len(m.trias))]
    for i, (name, _t, _mid) in enumerate(prep.props, start=1):
        lines.append(f"** {name}")
        lines.append(f"*ELSET, ELSET=P_{i}")
        lines += _id_lines(np.flatnonzero(prep.prop_of == i) + 1)
    for name, els in sorted(m.groups.items()):
        lines.append(f"*ELSET, ELSET={_inp_name(name)}")
        lines += _id_lines(np.sort(np.asarray(els)) + 1)
    for name, nodes in prep.junctions.items():
        lines.append(f"*NSET, NSET={name}")
        lines += _id_lines(nodes)
    if prep.corner:
        lines.append("*NODAL THICKNESS")
        wall = np.asarray(m.node_wall)
        used = sorted({int(v) for e in prep.corner for v in m.element_nodes(e)})
        lines += [f"{k + 1}, {wall[k] * s:.9g}" for k in used]
    varied = {int(prep.prop_of[e]) for e in prep.corner}
    for i, (_name, t, mid) in enumerate(prep.props, start=1):
        extra = ", NODAL THICKNESS" if i in varied else ""
        z = prep.offsets[i - 1] if prep.offsets else 0.0
        if z:
            # SHELL-7: OFFSET is the nodes' plane off the shell's mid-surface, in walls along the normal
            extra += f", OFFSET={-z / t:.9g}"
        lines.append(f"*SHELL SECTION, ELSET=P_{i}, MATERIAL=MAT_{mid}{extra}")
        lines.append(f"{t * s:.9g}")
    for mid, body in sorted((materials or {}).items()):
        lines.append(f"*MATERIAL, NAME=MAT_{mid}")
        lines += body
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "elements": m.n_elements, "properties": len(prep.props)}


def _inp_name(raw: str) -> str:
    """An ELSET name Abaqus and CalculiX accept: no spaces or commas, 80 characters (CalculiX keeps 80)."""
    out = "".join(c if c.isalnum() or c in "_-+." else "_" for c in raw)
    return out[:80] or "SET"


def read_inp(path: Path | str) -> ShellReadBack:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    sets: dict[str, set[int]] = {}
    nsets: dict[str, set[int]] = {}
    walls: dict[int, float] = {}
    nodal: dict[int, float] = {}
    mode, name = "", ""
    lines = Path(path).read_text(encoding="ascii").splitlines()
    for ln in lines:
        if ln.startswith("**") or not ln.strip():
            continue
        if ln.startswith("*"):
            head = [x.strip() for x in ln.split(",")]
            key = head[0].upper()
            opts = dict(x.split("=", 1) for x in head[1:] if "=" in x)
            mode = key
            if key == "*ELSET":
                name = opts["ELSET"]
                sets.setdefault(name, set())
            elif key == "*NSET" and opts.get("NSET") != "NALL":
                name = opts["NSET"]
                nsets.setdefault(name, set())
            elif key == "*SHELL SECTION":
                name = opts["ELSET"]
            continue
        vals = [v.strip() for v in ln.split(",") if v.strip()]
        if mode == "*NODE":
            nodes[int(vals[0])] = tuple(float(v) for v in vals[1:4])  # type: ignore[assignment]
        elif mode == "*ELEMENT":
            elements[int(vals[0])] = tuple(int(v) for v in vals[1:])
        elif mode == "*ELSET":
            sets[name].update(int(v) for v in vals)
        elif mode == "*NSET":
            nsets[name].update(int(v) for v in vals)
        elif mode == "*NODAL THICKNESS":
            nodal[int(vals[0])] = float(vals[1])
        elif mode == "*SHELL SECTION":
            walls[int(name.split("_")[1])] = float(vals[0])
    prop = {e: int(k.split("_")[1]) for k, es in sets.items() if k.startswith("P_") for e in es}
    corner = {e: tuple(nodal[n] for n in conn) for e, conn in elements.items() if all(n in nodal for n in conn)}
    groups = {k: v for k, v in sets.items() if not k.startswith("P_")}
    return ShellReadBack(nodes, elements, prop, walls, corner, groups, nsets)


# ------------------------------------------------------------------------------------------- Ansys ----
def write_cdb(path: Path | str, prep: ShellPrepared, *, units: str = "m", source: str = "") -> dict:
    """SHELL181 (ET,1,181; KEYOPT,1,3,2 full integration with incompatible modes - the accurate choice for bending,
    as Ansys advises for thin shells; KEYOPT 1 stays 0, bending and membrane). One section per property; an element
    of a varying wall gets its own section at its corners' mean (to WALL_DIGITS)."""
    m, s = prep.mesh, prep.scale
    offs = prep.offsets or [0.0] * len(prep.props)
    secs: list[tuple[float, int, float]] = [(t, mid, z) for (_n, t, mid), z in zip(prep.props, offs, strict=True)]
    sec_of = prep.prop_of.copy()
    seen: dict[tuple[float, int, float], int] = {}
    for e, ws in sorted(prep.corner.items()):
        key = (round(float(np.mean(ws)), WALL_DIGITS), prep.props[int(prep.prop_of[e]) - 1][2],
               offs[int(prep.prop_of[e]) - 1])
        if key not in seen:
            secs.append(key)
            seen[key] = len(secs)
        sec_of[e] = seen[key]
    lines = [
        "! Hexera shell mesh - Ansys MAPDL archive (CDB). " + NOT_TESTED.capitalize() + ".",
        *([f"! Source: {source}"] if source else []),
        *_units_line(units, "!"),
        f"! Mesh only: {m.n_elements} SHELL181. Part k's sections use MAT k - add MP,EX/PRXY/DENS for every part:",
        *[f"!   MAT {i} = {p}" for i, p in enumerate(prep.parts, start=1)],
        "! Every group is an element component; every junction a node component.",
        "/PREP7",
        "ET,1,181",
        "KEYOPT,1,3,2",
    ]
    for i, (t, mid, z) in enumerate(secs, start=1):
        label = prep.props[i - 1][0] if i <= len(prep.props) else f"VARIES_{i}"
        lines += [f"SECTYPE,{i},SHELL,,{ansys_name(label)}", f"SECDATA,{t * s:.9g},{mid}"]
        if z:
            # SHELL-7: the node plane off the mid-plane, along the normal
            lines.append(f"SECOFFSET,USER,{-z * s:.9g}")
    lines += [f"NBLOCK,6,SOLID,{len(m.xyz):>10d},{len(m.xyz):>10d}", "(3i9,6e21.13e3)"]
    for k, (x, y, z) in enumerate((m.xyz * s).tolist(), start=1):
        lines.append(f"{k:>9d}{0:>9d}{0:>9d}{x:21.13E}{y:21.13E}{z:21.13E}")
    lines.append("N,R5.3,LOC,       -1,")
    lines += [f"EBLOCK,19,SOLID,{m.n_elements:>10d},{m.n_elements:>10d}", "(19i9)"]
    for e, conn in enumerate(m.elements()):
        ids = [int(v) + 1 for v in conn]
        if len(ids) == 3:
            ids = ids + [ids[2]]           # a SHELL181 triangle: K repeated as L
        mid = prep.props[int(prep.prop_of[e]) - 1][2]
        head = [mid, 1, int(sec_of[e]), int(sec_of[e]), 0, 0, 0, 0, 4, 0, e + 1]
        lines.append("".join(f"{v:>9d}" for v in head + ids))
    lines.append(f"{-1:>9d}")
    taken: set[str] = set()
    for name, els in sorted(m.groups.items()):
        nm = unique_ansys_name(name, taken)
        taken.add(nm)
        ids = np.unique(np.asarray(els) + 1)
        lines += [f"CMBLOCK,{nm},ELEM,{len(ids):>8d}", "(8i10)"]
        lines += ["".join(f"{v:>10d}" for v in ids[i:i + 8].tolist()) for i in range(0, len(ids), 8)]
    for name, nodes in prep.junctions.items():
        nm = unique_ansys_name(name, taken)
        taken.add(nm)
        lines += [f"CMBLOCK,{nm},NODE,{len(nodes):>8d}", "(8i10)"]
        lines += ["".join(f"{v:>10d}" for v in nodes[i:i + 8].tolist()) for i in range(0, len(nodes), 8)]
    lines.append("FINISH")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return {"file": Path(path).name, "elements": m.n_elements, "sections": len(secs)}


def read_cdb(path: Path | str) -> ShellReadBack:
    nodes: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, tuple[int, ...]] = {}
    prop: dict[int, int] = {}
    walls: dict[int, float] = {}
    groups: dict[str, set[int]] = {}
    junctions: dict[str, set[int]] = {}
    lines = Path(path).read_text(encoding="ascii").splitlines()
    i = 0
    sec = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("SECTYPE"):
            sec = int(ln.split(",")[1])
        elif ln.startswith("SECDATA"):
            walls[sec] = float(ln.split(",")[1])
        elif ln.startswith("NBLOCK"):
            i += 2
            while not lines[i].startswith("N,"):
                r = lines[i]
                nodes[int(r[0:9])] = (float(r[27:48]), float(r[48:69]), float(r[69:90]))
                i += 1
        elif ln.startswith("EBLOCK"):
            i += 2
            while lines[i].strip() != "-1":
                v = [int(lines[i][k:k + 9]) for k in range(0, len(lines[i]), 9)]
                conn = tuple(v[11:15])
                elements[v[10]] = conn[:3] if conn[2] == conn[3] else conn
                prop[v[10]] = v[3]
                i += 1
        elif ln.startswith("CMBLOCK"):
            parts = ln.split(",")
            nm, kind, count = parts[1], parts[2], int(parts[3])
            i += 2
            ids: list[int] = []
            while len(ids) < count:
                ids += [int(lines[i][k:k + 10]) for k in range(0, len(lines[i]), 10)]
                i += 1
            (groups if kind == "ELEM" else junctions)[nm] = set(ids)
            continue
        i += 1
    return ShellReadBack(nodes, elements, prop, walls, {}, groups, junctions)


def write_all(workspace: Path | str, mesh: ShellMesh, *, units: str = "m", source: str = "") -> dict:
    """Write shell.bdf, shell.inp (CalculiX), shell_abaqus.inp and shell.cdb into `workspace`, one numbering."""
    ws = Path(workspace)
    prep = prepare(mesh, units=units)
    return {"nastran": write_bdf(ws / "shell.bdf", prep, units=units, source=source),
            "calculix": write_inp(ws / "shell.inp", prep, units=units, source=source),
            "abaqus": write_inp(ws / "shell_abaqus.inp", prep, units=units, source=source, solver="abaqus"),
            "ansys": write_cdb(ws / "shell.cdb", prep, units=units, source=source)}


__all__ = ["SHELL_SOLVERS", "ShellPrepared", "ShellReadBack", "junction_sets", "prepare", "read_bdf", "read_cdb",
           "read_inp", "wall_label", "write_all", "write_bdf", "write_cdb", "write_inp"]
