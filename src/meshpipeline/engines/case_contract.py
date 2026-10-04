# Responsibility: Before any native mesher is launched, read the case the writer actually produced and hold it to the patch contract the user approved.
# Owns: the per-engine reading of a written case into the boundary it will produce, the comparison with the approved contract, and the launch check.
# Boundaries: it reads case files and the workspace's contract; it meshes nothing, repairs nothing and never guesses - a covered case it cannot read is refused, and an engine it does not cover is not judged.
# Collaborates with: contracts/mesh_execution.py (the one seam every native run passes through), engines/workspace_facts.py, and pipeline/executor.py, which ends the job on a recorded refusal.
"""The pre-flight: approved == written, checked in seconds instead of after a whole run.

A car body meshed for 26 minutes and then failed as "did not meet the required quality checks"
because the patch the user approved ("car wall") was not a patch the mesher wrote. Nothing between
the approval and the manifest check compared the two. This does, at the last moment it is cheap:
the case files are on disk and the mesher has not started.

Each engine's case is read the way its mesher will read it - blockMesh's boundary list, the
snappyHexMesh surfaces and their regions, a createPatch merge, cfMesh's renameBoundary, a gmsh
spec's physical groups - into the boundary the mesh will carry. That boundary is held to the
workspace's patch contract (the same text the builder is briefed from): every approved patch must
be written under its approved name with a type its role allows, and nothing else may be written
that would carry faces.

A mismatch in a case OUR renderer wrote is our defect: the run is refused, the refusal is recorded
in the workspace, and the executor ends the job as an internal error that names the mismatch. A
mismatch in a spec the BUILDER MODEL authored (gmsh's groups) is its mistake to fix: the refusal
goes back to it as the run's result. Either way no mesh runs.
"""
from __future__ import annotations

import json
import logging
import mmap
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: The workspace fact a refused, renderer-written case leaves for the executor.
REFUSAL_FACT = ".case_contract_refusal.json"
#: Who wrote the case: our deterministic renderer, or the builder model.
RENDERER = "renderer"
BUILDER = "builder"

#: The OpenFOAM patch type each load-bearing role requires - the same table the boundary-type gate
#: judges the built mesh by (engines/cfmesh/deliverable.ROLE_REQUIRED_FOAM_TYPE, snappy's copy);
#: the openings must not be any of them.
_ROLE_OF_TYPES: dict[str, frozenset[str]] = {
    "wall": frozenset({"wall"}),
    "symmetry": frozenset({"symmetryPlane"}),
    "empty": frozenset({"empty"}),
}
_OPENINGS = frozenset({"inlet", "outlet", "farfield"})


@dataclass(frozen=True)
class CaseBoundary:
    """The boundary a written case will produce: name -> type. `kind` says what the type is -
    "openfoam" (an OpenFOAM patch type) or "roles" (the declared role vocabulary itself)."""

    patches: dict[str, str]
    kind: str = "openfoam"
    authored_by: str = RENDERER
    #: patches the engine writes for itself that end with no faces (the box an internal carve
    #: discards) - never the user's, never an extra
    engine_owned: frozenset[str] = frozenset()
    #: a role whose UNDECLARED patches are not extras (gmsh gathers leftover surfaces into a
    #: `free` default group no one named); a DECLARED patch of that role is checked like any other
    undeclared_ok_roles: frozenset[str] = frozenset()
    notes: tuple[str, ...] = field(default_factory=tuple)


class CaseUnreadable(Exception):
    """A case this pre-flight covers, written, but not in a form it can read - judged, not
    waved through: the launch is refused with the reason, before the mesher starts."""


# OpenFOAM dictionary reading, just enough for the dicts this system renders

def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def _balanced(text: str, open_at: int) -> tuple[str, int]:
    """The text inside the brace at `open_at`, and the index after its closing brace."""
    depth = 0
    for i in range(open_at, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i], i + 1
    raise ValueError("unbalanced braces")


def _block(text: str, key: str) -> str | None:
    """The body of the first `key { ... }` in text, or None."""
    m = re.search(rf"(?<![\w.]){re.escape(key)}\s*\{{", text)
    if not m:
        return None
    body, _end = _balanced(text, m.end() - 1)
    return body


_TOKEN = re.compile(r'"[^"]*"|[^\s{};()]+')


def _entries(body: str) -> list[tuple[str, str]]:
    """The `key { ... }` entries at the top level of a dictionary body, in order. Keyword lines
    (`key value;`, `key (list);`) are skipped whole, whatever brackets their value holds."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        m = _TOKEN.match(body, i)
        if not m:
            i += 1                               # a stray ';' or bracket between entries
            continue
        key, i = m.group(0).strip('"'), m.end()
        while i < n and body[i].isspace():
            i += 1
        if i < n and body[i] == "{":
            inner, i = _balanced(body, i)
            out.append((key, inner))
            continue
        depth = 0
        while i < n:
            c = body[i]
            i += 1
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == ";" and depth <= 0:
                break
    return out


def _top(body: str) -> str:
    """A dictionary body with its nested blocks removed: only its own keywords remain."""
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "{":
            _inner, i = _balanced(body, i)
            out.append(" ")
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def _sub(body: str, key: str) -> str | None:
    return next((b for k, b in _entries(body or "") if k == key), None)


def _word(body: str, key: str) -> str | None:
    m = re.search(rf"(?<![\w.]){re.escape(key)}\s+\"?([^\s;\"]+)\"?\s*;", _top(body or ""))
    return m.group(1) if m else None


def _patch_type(body: str) -> str | None:
    info = _sub(body, "patchInfo")
    return _word(info, "type") if info is not None else None


def stl_solid_names(path: Path) -> list[str]:
    """The solid names of an ASCII STL, in file order ([] for a binary or unreadable file). Scanned
    in C over a memory map: a staged body surface can be hundreds of megabytes."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
            if not head.lstrip().startswith(b"solid") or b"facet" not in head + fh.read(4096):
                return []
            fh.seek(0)
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return [m.group(1).strip().decode("utf-8", "replace")
                        for m in re.finditer(rb"(?m)^[ \t]*solid[ \t]*([^\r\n]*)", mm)]
    except (OSError, ValueError):
        return []


def _block_mesh_patches(ws: Path) -> dict[str, str] | None:
    f = ws / "system" / "blockMeshDict"
    if not f.exists():
        return None
    text = _strip_comments(f.read_text(errors="replace"))
    m = re.search(r"(?<![\w.])boundary\s*\(", text)
    if not m:
        return {}
    depth, j = 0, m.end() - 1
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = text[m.end():j]
    return {name: (_word(inner, "type") or "patch")
            for name, inner in re.findall(r"([A-Za-z_]\w*)\s*\{([^{}]*)\}", body)}


def _apply_create_patch(ws: Path, patches: dict[str, str]) -> dict[str, str]:
    f = ws / "system" / "createPatchDict"
    if not f.exists():
        return patches
    text = _strip_comments(f.read_text(errors="replace"))
    out = dict(patches)
    for body in re.findall(r"\{\s*(name\s[^{}]*?patchInfo\s*\{[^{}]*\}[^{}]*)\}", text, re.S):
        name = _word(body, "name")
        typ = _word(_block(body, "patchInfo") or "", "type") or "patch"
        src = re.search(r"(?<![\w.])patches\s*\(([^)]*)\)", body)
        if not name or not src:
            continue
        for s in src.group(1).split():
            out.pop(s, None)
        out[name] = typ
    return out


def _snappy_boundary(ws: Path) -> CaseBoundary | None:
    """blockMesh's box patches plus one patch per snappyHexMesh refinement surface (or per
    region of it), after createPatch. Region naming as snappyHexMesh does it: a region declared
    under the geometry's regions{} takes its declared name, a single-region surface is the surface,
    and an undeclared region of a multi-region surface is <surface>_<region>."""
    box = _block_mesh_patches(ws)
    shm = ws / "system" / "snappyHexMeshDict"
    if box is None or not shm.exists():
        raise CaseUnreadable("system/blockMeshDict or system/snappyHexMeshDict is missing")
    text = _strip_comments(shm.read_text(errors="replace"))
    geometry = _block(text, "geometry")
    surfaces = _block(text, "refinementSurfaces")
    if geometry is None or surfaces is None:
        raise CaseUnreadable("snappyHexMeshDict has no geometry or refinementSurfaces block")
    geo: dict[str, tuple[str, str]] = {}          # surface name -> (geometry key, entry body)
    for key, body in _entries(geometry):
        geo[_word(body, "name") or key] = (key, body)
    patches: dict[str, str] = {}
    entries = _entries(surfaces)
    if not entries:
        raise CaseUnreadable("snappyHexMeshDict refines no surface, so it writes no body patch")
    for surf, body in entries:
        if surf not in geo:
            raise CaseUnreadable(f"refinement surface {surf!r} has no geometry entry")
        key, gbody = geo[surf]
        if (_word(gbody, "type") or "") != "triSurfaceMesh":
            raise CaseUnreadable(f"refinement surface {surf!r} is not a triSurfaceMesh")
        declared = {r: (_word(rb, "name") or r) for r, rb in _entries(_sub(gbody, "regions") or "")}
        region_types = {r: _patch_type(rb) for r, rb in _entries(_sub(body, "regions") or "")}
        surf_type = _patch_type(body) or "wall"
        solids = stl_solid_names(ws / "constant" / "triSurface" / key) or [surf]
        for solid in solids:
            if solid in declared:
                name = declared[solid]
            elif len(solids) == 1:
                name = surf
            else:
                name = f"{surf}_{solid}"
            patches[name] = region_types.get(solid) or surf_type
    internal = _topology(ws) == "internal"
    merged = _apply_create_patch(ws, {**box, **patches})
    return CaseBoundary(patches=merged, kind="openfoam", authored_by=RENDERER,
                        engine_owned=frozenset(box) if internal else frozenset())


def _cfmesh_boundary(ws: Path) -> CaseBoundary | None:
    """Every solid of the assembled surface (geom.stl) renamed and retyped by the meshDict's
    renameBoundary - a solid it does not list lands in its defaultName - plus, in 2D, the front and
    back planes cartesian2DMesh generates, as createPatch merges them."""
    md = ws / "system" / "meshDict"
    surf = ws / "geom.stl"
    if not md.exists() or not surf.exists():
        raise CaseUnreadable("system/meshDict or geom.stl is missing")
    text = _strip_comments(md.read_text(errors="replace"))
    rb = _block(text, "renameBoundary") or ""
    default_name, default_type = _word(rb, "defaultName"), _word(rb, "defaultType") or "patch"
    renames = {k: (_word(v, "newName") or k, _word(v, "type") or "patch")
               for k, v in _entries(_sub(rb, "newPatchNames") or "")}
    solids = stl_solid_names(surf)
    if not solids:
        raise CaseUnreadable("geom.stl names no solids (empty, or not ASCII)")
    patches: dict[str, str] = {}
    for s in solids:
        if s in renames:
            name, typ = renames[s]
        elif default_name:
            name, typ = default_name, default_type
        else:
            name, typ = s, "patch"
        patches[name] = typ
    if (ws / ".cartesian2d").exists():
        patches.update({"bottomEmptyFaces": "empty", "topEmptyFaces": "empty"})
    return CaseBoundary(patches=_apply_create_patch(ws, patches), kind="openfoam",
                        authored_by=RENDERER)


def _gmsh_boundary(ws: Path) -> CaseBoundary | None:
    """The physical groups gmsh_spec.json asks for, by role. The builder model writes this spec,
    so a mismatch is its to fix. Every APPROVED group is checked, a `free` one included; an
    undeclared `free` group is the leftover surfaces gmsh gathers by default, not an extra - the
    same rule the patch gate applies to the deck."""
    f = ws / "gmsh_spec.json"
    try:
        spec = json.loads(f.read_text())
    except (OSError, ValueError) as exc:
        raise CaseUnreadable(f"gmsh_spec.json is missing or not JSON ({exc})") from exc
    from meshpipeline.engines.workspace_facts import (
        port_declaration,
        read_flow_topology,
        read_input_kind,
    )
    declared = [p for p in port_declaration(ws) if isinstance(p, dict) and p.get("name")]
    groups = spec.get("groups") if isinstance(spec, dict) else None
    # WITHOUT BUILDER GROUPS THE ENGINE NAMES THEM (engines/gmsh/driver.py): an external body is
    # cut out of a far-field box and its groups are the declared wall and far field; ports are
    # bound to their faces and the rest walled. The builder's groups, when written, are carried
    # through the cut and stand.
    if not groups and read_flow_topology(ws) == "external" \
            and read_input_kind(ws) in ("solid-body", "body-surface"):
        wall = next((p["name"] for p in declared if p.get("type") == "wall"), "body")
        far = next((p["name"] for p in declared if p.get("type") == "farfield"), "farfield")
        return CaseBoundary(patches={str(wall): "wall", str(far): "farfield"}, kind="roles",
                            authored_by=RENDERER)
    if not groups and any(p.get("type") in ("inlet", "outlet") for p in declared):
        wall = next((p["name"] for p in declared if p.get("type") == "wall"), "wall")
        patches = {str(p["name"]): str(p["type"]) for p in declared
                   if p.get("type") in ("inlet", "outlet")}
        patches[str(wall)] = "wall"
        return CaseBoundary(patches=patches, kind="roles", authored_by=RENDERER)
    if not isinstance(groups, list):
        raise CaseUnreadable("gmsh_spec.json has no groups list")
    patches = {str(g.get("name") or "").strip(): str(g.get("role") or "free").strip()
               for g in groups if isinstance(g, dict) and str(g.get("name") or "").strip()}
    return CaseBoundary(patches=patches, kind="roles", authored_by=BUILDER,
                        undeclared_ok_roles=frozenset({"free"}))


def _vmtk_boundary(ws: Path) -> CaseBoundary | None:
    """The lumen wall and the ports the engine opened (vmtk_staging.json). The mesh carries the
    wall as ONE entity, so only the first declared wall is delivered. A lumen surface uploaded
    as-is is not staged by the engine at all - nothing names its caps before the run - so that
    path is not judged here."""
    from meshpipeline.engines.workspace_facts import contract_patches
    fact = ws / "vmtk_staging.json"
    if not fact.exists():
        return None
    try:
        staged = json.loads(fact.read_text())
    except (OSError, ValueError) as exc:
        raise CaseUnreadable(f"vmtk_staging.json is not JSON ({exc})") from exc
    ports = staged.get("ports") if isinstance(staged, dict) else None
    if not isinstance(ports, list) or not ports:
        return None
    walls = [p["name"] for p in contract_patches(ws) if p["type"] == "wall"]
    patches = {str(p.get("name")): str(p.get("role") or "") for p in ports if isinstance(p, dict)}
    if walls:
        patches[walls[0]] = "wall"
    return CaseBoundary(patches=patches, kind="roles", authored_by=RENDERER)


#: How each engine's written case is read. An engine not listed here is not judged (the
#: multi-region case splits regions after meshing, and its user boundaries cannot be read from
#: the dicts beforehand).
_READERS = {
    "snappy": _snappy_boundary,
    "cfmesh": _cfmesh_boundary,
    "gmsh": _gmsh_boundary,
    "vmtk": _vmtk_boundary,
}


def _topology(ws: Path) -> str:
    from meshpipeline.engines.workspace_facts import read_flow_topology
    return read_flow_topology(ws)


#: Who writes each covered engine's case - which says whose mistake an unreadable one is.
_AUTHOR = {"snappy": RENDERER, "cfmesh": RENDERER, "gmsh": BUILDER, "vmtk": RENDERER}


def read_case_boundary(workspace, engine: str) -> CaseBoundary | None:
    """The boundary the written case will produce. None when this pre-flight does not judge the
    engine or path (multi-region; a vmtk lumen uploaded as-is). A covered case that is not there,
    or not readable, raises CaseUnreadable - it is refused, never waved through."""
    reader = _READERS.get(str(engine or "").strip().lower())
    if reader is None:
        return None
    try:
        return reader(Path(workspace))
    except CaseUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 - any reading failure is an unreadable case
        logger.warning("case contract: could not read the %s case in %s", engine, workspace,
                       exc_info=True)
        raise CaseUnreadable(f"{type(exc).__name__}: {exc}") from exc


def compare(declared: list[dict], case: CaseBoundary) -> list[str]:
    """Every way the written case departs from the approved patches, in plain words ([] = none)."""
    problems: list[str] = []
    want = {str(p.get("name") or "").strip(): str(p.get("type") or "").strip()
            for p in declared if isinstance(p, dict) and str(p.get("name") or "").strip()}
    have = {n: t for n, t in case.patches.items()
            if not (case.kind == "roles" and t in case.undeclared_ok_roles and n not in want)}
    written = ", ".join(sorted(have)) or "nothing"
    for name, role in sorted(want.items()):
        if name not in have:
            problems.append(f"the approved patch '{name}' ({role}) is not written - the case "
                            f"writes {written}")
            continue
        got = have[name]
        if case.kind == "roles":
            if got != role:
                problems.append(f"'{name}' is written as a {got}, but was approved as a {role}")
        elif role in _ROLE_OF_TYPES and got not in _ROLE_OF_TYPES[role]:
            problems.append(f"'{name}' is written as OpenFOAM type {got}, but was approved as a "
                            f"{role}")
        elif role in _OPENINGS and got in {t for ts in _ROLE_OF_TYPES.values() for t in ts}:
            problems.append(f"'{name}' is written as OpenFOAM type {got}, but was approved as an "
                            f"open boundary ({role})")
    for name in sorted(set(have) - set(want) - case.engine_owned):
        problems.append(f"the case writes '{name}', which no approved patch names")
    return problems


def check(workspace, engine: str) -> tuple[list[str], CaseBoundary | None]:
    """(problems, the boundary read) for the case in `workspace`, against its patch contract.
    No contract (a direct dispatch), or an engine/path this does not judge: ([], None). A covered
    case that cannot be read is a problem in itself - the early diagnosis is the point, and a case
    whose boundary nobody can state is not one to spend a run on."""
    from meshpipeline.engines.workspace_facts import contract_patches
    declared = contract_patches(workspace)
    if not declared:
        return [], None
    try:
        case = read_case_boundary(workspace, engine)
    except CaseUnreadable as exc:
        author = _AUTHOR.get(str(engine or "").strip().lower(), RENDERER)
        return ([f"the written {engine} case could not be read to check it against the approved "
                 f"patches: {exc}"], CaseBoundary(patches={}, authored_by=author))
    if case is None:
        return [], None
    return compare(declared, case), case


def refusal_of(workspace) -> dict | None:
    """The recorded refusal of a renderer-written case in this workspace, or None."""
    try:
        rec = json.loads((Path(workspace) / REFUSAL_FACT).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("problems") else None


def launch_check(workspace, engine: str) -> dict | None:
    """Run before every native mesher launch (contracts/mesh_execution.run_mesh). None lets the
    run go; a result dict refuses it, shaped like any native result so every caller reads it the
    way it reads a failed run - and marked, so none of them mistakes it for one."""
    from meshpipeline.contracts.mesh_execution import RC_CASE_CONTRACT
    problems, case = check(workspace, engine)
    ws = Path(workspace)
    if not problems or case is None:
        try:
            (ws / REFUSAL_FACT).unlink(missing_ok=True)
        except OSError:
            pass
        return None
    detail = "; ".join(problems)
    internal = case.authored_by == RENDERER
    logger.error("case contract: %s case in %s does not match the approved patches (%s) - "
                 "refusing to launch the mesher: %s", engine, workspace,
                 "internal defect" if internal else "builder spec", detail)
    if internal:
        try:
            (ws / REFUSAL_FACT).write_text(json.dumps(
                {"engine": engine, "problems": problems, "written": case.patches}, indent=1))
        except OSError:
            logger.warning("case contract: could not record the refusal in %s", workspace)
    return {"rc": RC_CASE_CONTRACT, "timed_out": False,
            "log_tail": f"[CASE_CONTRACT_MISMATCH] {detail}",
            "case_contract_mismatch": problems, "internal_defect": internal}


__all__ = ["BUILDER", "REFUSAL_FACT", "RENDERER", "CaseBoundary", "CaseUnreadable", "check",
           "compare", "launch_check", "read_case_boundary", "refusal_of", "stl_solid_names"]
