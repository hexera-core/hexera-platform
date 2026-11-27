# Responsibility: Say which of checkMesh's own failed checks make an OpenFOAM mesh invalid - one reading for every OpenFOAM-family engine.
# Boundaries: pure text reading of checkMesh's output; it runs nothing and judges no quality bar (skewness, aspect ratio and the like stay quality).
# Collaborates with: engines/{snappy,cfmesh,netgen,snappy_multiregion}/foam_exec.check_mesh and engines/snapgrid/native.parse_check_mesh (each sets q["fatal"] from it).
"""WHAT CHECKMESH CALLS INVALID IS INVALID HERE (docs/RULES.md VER-1, SW-1).

Every gate, the cfMesh rescue, the fallback ladder and the delivery read a mesh's validity from one
list, `quality["fatal"]`. Each OpenFOAM engine filled it from its own copy of four markers -
negative volumes, open cells, wrongly oriented faces, empty patches on a 3D mesh - and nothing else.
checkMesh v2412 fails a mesh on more than that. Two of its failures were missed and the meshes were
delivered (board batches 2 to 4, 21 Nov 2026; checkMesh v2412 re-run on the delivered archives):

  - a cfMesh cooling pipe (7.2 M cells): " ***Zero or negative face area detected.  Minimum area: 0"
    (5 faces) and " ***Number of non-orthogonality errors: 5." (worst 112.9 degrees);
  - a cfMesh manifold (1.06 M cells): 2 zero-area faces and 2 non-orthogonality errors (118.7 degrees);
    its rescue even KEPT that mesh as "no fatal defect" over two that were plainly broken;
  - three more of the same on the board (two cfMesh, one snappyHexMesh coronary model).

A zero-area face is a wall triangle with two corners on the same point (read off the manifold's own
points: an edge of length 0); a non-orthogonality error is a face at 90 degrees or more, where the
line between the two cell centres runs backwards through it. No finite-volume scheme is defined there.

So the validity list is checkMesh's own: the four markers as before, plus every failed check
("***" line) of the kind the board's cross-check counts as validity - zero or negative face area,
non-orthogonality errors, faces out of upper-triangular order, unused points, duplicates, points out of
range, broken topology. Skewness, aspect ratio, determinants, weights, warped or concave faces stay
quality: they are measured and told, never "invalid".
"""
from __future__ import annotations

import re

#: (marker in the lower-cased output, the defect in words). Kept as markers over the whole output for the
#: four checks the engines have always read; every new marker is read on checkMesh's "***" lines only (an
#: "... OK." line names the same check, e.g. "Upper triangular ordering OK.").
CHECKMESH_FATAL: tuple[tuple[str, str], ...] = (
    ("negative volume", "negative-volume cells"),
    ("open cell", "open cells"),
    ("incorrectly oriented", "incorrectly oriented faces"),
    # "Total number of faces on empty patches is not divisible by the number of cells in the mesh. Hence
    # this mesh is not 1D or 2D." An `empty` patch on a 3D mesh makes every solver refuse the case.
    ("not 1d or 2d", "empty patches on a 3D mesh"),
)

#: checkMesh's failed checks ("***" lines) that make a mesh unusable by any solver, with the defect in words.
STARRED_FATAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"zero or negative face area", re.I), "zero-area faces"),
    (re.compile(r"non-orthogonality errors", re.I), "faces 90 degrees or more out of square"),
    (re.compile(r"zero or negative cell volume|negative (cell )?volume", re.I), "negative-volume cells"),
    (re.compile(r"face pyramids", re.I), "incorrectly oriented faces"),
    (re.compile(r"upper triangular", re.I), "faces out of upper-triangular order"),
    (re.compile(r"unused points", re.I), "unused points"),
    (re.compile(r"duplicate", re.I), "duplicate faces or points"),
    (re.compile(r"out of range|illegal", re.I), "faces that name points the mesh does not have"),
    (re.compile(r"not closed|topolog", re.I), "broken mesh topology"),
)


def failed_lines(out: str) -> list[str]:
    """checkMesh's failed-check lines ("***..."), whitespace squeezed."""
    return [re.sub(r"\s+", " ", ln.strip()) for ln in (out or "").splitlines() if ln.strip().startswith("***")]


def fatal_defects(out: str) -> list[str]:
    """The validity defects checkMesh's output names, in words, each once ([] for a valid mesh)."""
    low = (out or "").lower()
    found = [label for marker, label in CHECKMESH_FATAL if marker in low]
    stars = failed_lines(out)
    for pattern, label in STARRED_FATAL:
        if label not in found and any(pattern.search(s) for s in stars):
            found.append(label)
    return found


__all__ = ["CHECKMESH_FATAL", "STARRED_FATAL", "failed_lines", "fatal_defects"]
