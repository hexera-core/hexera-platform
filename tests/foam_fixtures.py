# Responsibility: Write small, exact OpenFOAM polyMeshes by hand so quality measurements can be
# checked against numbers worked out on paper, not against whatever a mesher happened to produce.
# Boundaries: ASCII polyMesh files only - the same format every reader in the product accepts.
from __future__ import annotations

import math
from pathlib import Path

#: Shearing the last cell's far points by this much in y makes the face between cells 1 and 2
#: non-orthogonal by atan(SHEAR / 2): cell 2's centre moves to y = 0.5 + SHEAR/2, so the
#: centre-to-centre vector is (1, SHEAR/2, 0) against a face normal of (1, 0, 0).
ROW_SHEAR = 5.0
ROW_SHEAR_NON_ORTHO_DEG = math.degrees(math.atan(ROW_SHEAR / 2.0))      # 68.2


def _p(ix: int, iy: int, iz: int) -> int:
    return ix * 4 + iy * 2 + iz


def _foam(path: Path, cls: str, obj: str, count: int, records: list[str]) -> None:
    path.write_text(
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
        f"    class       {cls};\n    object      {obj};\n}}\n"
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
        f"{count}\n(\n" + "\n".join(records) + "\n)\n")


def write_row_of_hexes(polymesh: Path, *, shear_last_y: float = 0.0,
                       degenerate_wall_face: bool = False, last_cell_length: float = 1.0) -> dict:
    """Three unit hexes in a row along x, as an OpenFOAM polyMesh.

    Points p(ix,iy,iz) = ix*4 + iy*2 + iz for ix in 0..3. Faces are ordered the way OpenFOAM
    orders them - the two internal faces first, then the boundary patches: `inlet` (x=0),
    `outlet` (x=3), `wall` (the twelve side faces). Every face is wound so its normal points out
    of its owner; the quality formulas depend on that sign.

    `shear_last_y` moves the four points at x=3 in y, which skews cell 2 and makes the x=2 face
    non-orthogonal by a known angle. `degenerate_wall_face` appends a 2-vertex face to the wall
    patch - the surface reader drops such faces, and a per-face field must drop it too.
    `last_cell_length` stretches cell 2 along x (its far points sit at x = 2 + L), which gives it
    checkMesh's aspect ratio L for L >= 1 while cells 0 and 1 stay unit cubes.
    Returns {"n_wall": faces in the wall patch as written}.
    """
    polymesh.mkdir(parents=True, exist_ok=True)
    pts = []
    for ix in range(4):
        for iy in range(2):
            for iz in range(2):
                y = iy + (shear_last_y if ix == 3 else 0.0)
                x = 2 + last_cell_length if ix == 3 else ix
                pts.append(f"({x} {y} {iz})")
    faces: list[str] = []
    owner: list[int] = []
    neighbour: list[int] = []

    def face(ids, own, nei=None):
        faces.append(f"{len(ids)}(" + " ".join(str(i) for i in ids) + ")")
        owner.append(own)
        if nei is not None:
            neighbour.append(nei)

    # internal faces at x=1 and x=2, normal +x (owner -> neighbour)
    face([_p(1, 0, 0), _p(1, 1, 0), _p(1, 1, 1), _p(1, 0, 1)], 0, 1)
    face([_p(2, 0, 0), _p(2, 1, 0), _p(2, 1, 1), _p(2, 0, 1)], 1, 2)
    inlet_start = len(faces)
    face([_p(0, 0, 0), _p(0, 0, 1), _p(0, 1, 1), _p(0, 1, 0)], 0)           # -x
    outlet_start = len(faces)
    face([_p(3, 0, 0), _p(3, 1, 0), _p(3, 1, 1), _p(3, 0, 1)], 2)           # +x
    wall_start = len(faces)
    for c in range(3):
        face([_p(c, 0, 0), _p(c + 1, 0, 0), _p(c + 1, 0, 1), _p(c, 0, 1)], c)      # -y
        face([_p(c, 1, 0), _p(c, 1, 1), _p(c + 1, 1, 1), _p(c + 1, 1, 0)], c)      # +y
        face([_p(c, 0, 0), _p(c, 1, 0), _p(c + 1, 1, 0), _p(c + 1, 0, 0)], c)      # -z
        face([_p(c, 0, 1), _p(c + 1, 0, 1), _p(c + 1, 1, 1), _p(c, 1, 1)], c)      # +z
    if degenerate_wall_face:
        face([_p(0, 0, 0), _p(0, 0, 1)], 0)
    n_wall = len(faces) - wall_start

    _foam(polymesh / "points", "vectorField", "points", len(pts), pts)
    _foam(polymesh / "faces", "faceList", "faces", len(faces), faces)
    _foam(polymesh / "owner", "labelList", "owner", len(owner), [str(o) for o in owner])
    _foam(polymesh / "neighbour", "labelList", "neighbour", len(neighbour),
          [str(n) for n in neighbour])
    patches = [("inlet", "patch", inlet_start, 1), ("outlet", "patch", outlet_start, 1),
               ("wall", "wall", wall_start, n_wall)]
    body = [f"    {name}\n    {{\n        type {ptype};\n"
            f"        nFaces {n};\n        startFace {start};\n    }}"
            for name, ptype, start, n in patches]
    _foam(polymesh / "boundary", "polyBoundaryMesh", "boundary", len(patches), body)
    return {"n_wall": n_wall}
