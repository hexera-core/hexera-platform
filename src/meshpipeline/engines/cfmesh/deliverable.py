# Responsibility: Define what a complete cfMesh delivery contains, and detect an incomplete one.
# Boundaries: membership and boundary reconciliation; an owner file alone is not a delivered polyMesh.
from __future__ import annotations

import re
from pathlib import Path

#: Every file `checkMesh` and a solver need before the mesh is readable at all.
POLYMESH_COMPONENTS: tuple[str, ...] = ("boundary", "faces", "neighbour", "owner", "points")

#: Declared roles whose OpenFOAM patch TYPE is semantically load-bearing: a 2D case with a
#: front/back patch of type `patch` (a failed empty-retype) or a symmetry patch of type `patch`
#: SOLVES WRONG, yet has the right name and nonzero faces - name/face reconciliation alone cannot
#: catch it. Case-keyed (which roles the user declared), never engine-keyed.
ROLE_REQUIRED_FOAM_TYPE: dict[str, str] = {"empty": "empty", "symmetry": "symmetryPlane",
                                           "wall": "wall"}

#: OpenFOAM boundary entries are `<name>` on its own line, then `{` on the next. `\s*` not `\s+`:
#: cfMesh writes patch names at column 0 where snappy indents them.
_ENTRY = re.compile(r"^\s*([A-Za-z_]\w*)\s*\n\s*\{", re.M)
_ENTRY_BODY = re.compile(r"^\s*([A-Za-z_]\w*)\s*\n\s*\{([^}]*)\}", re.M)
_TYPE = re.compile(r"\btype\s+(\w+)\s*;")


def polymesh_dir(workspace) -> Path:
    return Path(workspace) / "constant" / "polyMesh"


def polymesh_problems(workspace) -> tuple[str, ...]:
    poly = polymesh_dir(workspace)
    if not poly.is_dir():
        return ("no constant/polyMesh directory",)
    problems: list[str] = []
    for component in POLYMESH_COMPONENTS:
        path = poly / component
        if not path.is_file():
            problems.append(f"missing {component}")
        elif path.stat().st_size == 0:
            problems.append(f"empty {component}")
    return tuple(problems)


def is_delivered(workspace) -> bool:
    return not polymesh_problems(workspace)


def delivery_problem(workspace) -> str:
    problems = polymesh_problems(workspace)
    if not problems:
        return ""
    return ("[CFMESH] constant/polyMesh is incomplete - " + ", ".join(problems)
            + ". The mesh build did not finish (a killed, timed-out or cancelled run leaves a "
              "partial polyMesh); re-run the mesh.")


def boundary_patch_names(workspace) -> list[str]:
    b = polymesh_dir(workspace) / "boundary"
    if not b.exists():
        return []
    return [n for n in _ENTRY.findall(b.read_text(errors="replace")) if n != "FoamFile"]


def boundary_foam_types(workspace) -> dict[str, str]:
    b = polymesh_dir(workspace) / "boundary"
    if not b.exists():
        return {}
    out: dict[str, str] = {}
    for m in _ENTRY_BODY.finditer(b.read_text(errors="replace")):
        name, body = m.group(1), m.group(2)
        if name == "FoamFile":
            continue
        t = _TYPE.search(body)
        if t:
            out[name] = t.group(1)
    return out


def reconcile_boundary_types(workspace, intake_patches: list) -> str:
    actual = boundary_foam_types(workspace)
    if not actual:
        return ""
    bad: list[str] = []
    for p in intake_patches or []:
        role = str(p.get("type", "")).strip()
        name = str(p.get("name", "")).strip()
        want = ROLE_REQUIRED_FOAM_TYPE.get(role)
        if want and name in actual and actual[name] != want:
            bad.append(f"{name}: declared role {role!r} requires OpenFOAM type {want!r}, "
                       f"actual boundary has {actual[name]!r}")
    if bad:
        return ("[BOUNDARY_TYPE_MISMATCH] the generated polyMesh boundary contradicts the "
                "declared contract - " + "; ".join(bad) + ". The mesh would solve wrong; "
                "re-run the mesh (the type retype/emission step failed).")
    return ""


__all__ = ["POLYMESH_COMPONENTS", "ROLE_REQUIRED_FOAM_TYPE", "boundary_foam_types",
           "boundary_patch_names", "delivery_problem", "is_delivered", "polymesh_dir",
           "polymesh_problems", "reconcile_boundary_types"]
