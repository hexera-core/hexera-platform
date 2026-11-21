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


#: WHY THERE IS A BINARY WRITER AT ALL. Measured on the real four-rank case
#: (`runs/unit_cell_100um-4across`): its RECONSTRUCTED mesh is ascii and its PROCESSOR
#: DIRECTORIES are binary, from the same run - `decomposePar` and the parallel utilities obey
#: `system/controlDict`, and that case's says binary. So the form a decomposed reader meets in the
#: wild is binary, and a suite that only ever hands it ascii would leave that path covered by one
#: directory on one laptop.
#:
#: THE LAYOUT IS THE REAL FILE'S, measured before this was written:
#:   points  one list of N, then 3N packed float64
#:   faces   a faceCompactList - one list of N+1 int32 offsets, then one list of the flat vertex
#:           ids, whose declared count equals the last offset
#:   owner   one list of N int32, one per face
#: with the FoamFile block carrying `format binary;` and `arch "LSB;label=32;scalar=64"`.
_BINARY_ARCH = "LSB;label=32;scalar=64"


def _foam_binary(path: Path, cls: str, obj: str, blocks: list) -> None:
    """A binary foam file: the FoamFile block, then each (count, packed bytes) as its own list."""
    out = ("FoamFile\n{\n    version     2.0;\n    format      binary;\n"
           f"    class       {cls};\n    arch        \"{_BINARY_ARCH}\";\n"
           f"    object      {obj};\n}}\n"
           "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n"
           "\n").encode()
    for count, data in blocks:
        out += f"{count}\n(".encode() + data + b")\n"
    path.write_bytes(out)


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


#: The three shapes checkMesh's boundaryField actually takes, measured on the delivered coupon
#: 2026-10-30. Every one of its fields is a volScalarField whose internalField is one value per
#: CELL; they differ only here, and the difference is the whole contract:
#:   nonOrthoAngle   calculated, `value uniform 0`        a true statement, not a measurement
#:   skewness        calculated, nonuniform List<scalar>  one value per face of the patch
#:   aspectRatio     zeroGradient                         i.e. the owner cell's own value
CHECKMESH_FIELDS = ("nonOrthoAngle", "skewness", "aspectRatio")


def write_quality_fields(region_dir, *, cells, per_face=None, uniform=None,
                         zero_gradient=("aspectRatio",), patches=("inlet", "outlet", "wall"),
                         names=CHECKMESH_FIELDS):
    """Write checkMesh -writeFields output by hand, in those three shapes.

    `cells` is the internal field, one value per cell. `per_face` is {field: {patch: [...]}} for
    the nonuniform case, `uniform` is {field: {patch: value}}, and a field named in
    `zero_gradient` gets every patch with no values at all.
    """
    region_dir = Path(region_dir)
    region_dir.mkdir(parents=True, exist_ok=True)
    per_face = per_face or {}
    uniform = uniform or {}
    for name in names:
        out = ["FoamFile", "{", "    version     2.0;", "    format      ascii;",
               "    class       volScalarField;", f"    object      {name};", "}",
               "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //", "",
               "dimensions      [0 0 0 0 0 0 0];", "",
               "internalField   nonuniform List<scalar> ", str(len(cells)), "("]
        out += [repr(float(v)) for v in cells]
        out += [")", ";", "", "boundaryField", "{"]
        for patch, vals in (per_face.get(name) or {}).items():
            out += [f"    {patch}", "    {", "        type            calculated;",
                    "        value           nonuniform List<scalar> ", str(len(vals)), "("]
            out += [repr(float(v)) for v in vals]
            out += [")", ";", "    }"]
        for patch, val in (uniform.get(name) or {}).items():
            out += [f"    {patch}", "    {", "        type            calculated;",
                    f"        value           uniform {float(val)};", "    }"]
        if name in zero_gradient:
            for patch in patches:
                out += [f"    {patch}", "    {", "        type            zeroGradient;", "    }"]
        out += ["}", "", "// *********************************************************** //"]
        (region_dir / name).write_text("\n".join(out) + "\n")
    return region_dir


#: WHAT A DECOMPOSED CASE LOOKS LIKE, and these three properties are MEASURED rather than assumed.
#: Read off a real 4-rank `decomposePar` + `splitMeshRegions` case
#: (runs/unit_cell_100um-4across), which carries both the per-rank meshes and the reconstructed
#: ones, so the two can be compared directly:
#:
#:   1. EVERY rank lists EVERY global real patch, with `nFaces 0` where that rank holds none of
#:      it. So a reader may NOT take processor0's list as the complete set by luck - it is
#:      complete by rule - but equally may not assume a listed patch has faces on this rank.
#:   2. Each rank's real patches are CONTIGUOUS, and the processor patches come LAST.
#:   3. Summing a real patch's `nFaces` across ranks reproduces the reconstructed mesh's count
#:      exactly. On that case: fluid {fluid_to_solid 93600, ymax 128, ymin 128} with 840
#:      processor faces excluded, and solid {solid_to_fluid 93600, xmax 67604, xmin 67604,
#:      ymax 404, ymin 404, zmax 13000, zmin 3900} with 2,484 processor faces excluded.
#:
#: `type processor` is the ONLY thing that distinguishes a processor patch from a real one. The
#: names happen to be `procBoundaryM_toN` today, and nothing may depend on that spelling.
PROCESSOR_PATCH_TYPE = "processor"


def write_decomposed_row_of_hexes(case, *, ranks: int = 2, n_cells: int = 6,
                                  region: str | None = None, binary: bool = False) -> dict:
    """`n_cells` unit hexes in a row along x, split over `ranks` processor directories.

    The serial equivalent of this mesh is `write_row_of_hexes` generalised to n cells: the same
    geometry, the same three real patches (`inlet` at x=0, `outlet` at x=n, `wall` around), cut
    into contiguous chunks along x. Every cut introduces one processor face on each side, which
    is exactly the duplication that makes a rank-summed face count larger than the mesh's.

    Written under `case/processorN/constant/polyMesh`, or `.../constant/<region>/polyMesh` when
    `region` is given, which is where `splitMeshRegions -parallel` leaves a multi-region case.

    `binary` writes `points`, `faces`, `owner` and `neighbour` the way a real decomposed case
    has them - see `_foam_binary` for the measured layout and for why that is the realistic form.
    The `boundary` file stays a dictionary either way, which is also what the real case does: its
    header says binary and its contents are text.

    Returns what a reader should find: {"patches": {name: total real faces}, "processor_faces":
    n, "cells": n_cells} - the totals being what summing across ranks must reproduce.
    """
    case = Path(case)
    if int(ranks) < 1:
        raise ValueError(f"a decomposed case needs at least one rank, got {ranks}")
    if int(n_cells) < int(ranks):
        raise ValueError(f"{n_cells} cells cannot be split over {ranks} ranks")
    bounds = [(r * int(n_cells)) // int(ranks) for r in range(int(ranks) + 1)]
    n_proc_faces = 0

    for r in range(int(ranks)):
        n_proc_faces += _write_one_rank(case, r, bounds[r], bounds[r + 1],
                                        ranks=int(ranks), region=region, binary=bool(binary))

    return {"patches": {"inlet": 1, "outlet": 1, "wall": 4 * int(n_cells)},
            "processor_faces": n_proc_faces, "cells": int(n_cells)}


def _write_one_rank(case, r: int, c0: int, c1: int, *, ranks: int, region: str | None,
                    binary: bool = False) -> int:
    """One rank's polyMesh for cells [c0, c1) of the row. Returns its processor-face count.

    A function of its own rather than a loop body, so the little `face` helper closes over THIS
    rank's lists and no other - the bug ruff's B023 is named for, and one that would have been
    silent here because every call happens inside the iteration that defined them.
    """
    nc = c1 - c0
    polymesh = Path(case) / f"processor{r}" / "constant"
    if region:
        polymesh = polymesh / region
    polymesh = polymesh / "polyMesh"
    polymesh.mkdir(parents=True, exist_ok=True)

    xyz = [(float(c0 + ix), float(iy), float(iz))
           for ix in range(nc + 1) for iy in range(2) for iz in range(2)]
    pts = [f"({x:g} {y:g} {z:g})" for x, y, z in xyz]

    def lp(ix: int, iy: int, iz: int) -> int:
        return ix * 4 + iy * 2 + iz

    faces: list[str] = []
    face_ids: list[list[int]] = []
    owner: list[int] = []
    neighbour: list[int] = []
    n_proc_faces = 0

    def face(ids, own, nei=None):
        faces.append(f"{len(ids)}(" + " ".join(str(i) for i in ids) + ")")
        face_ids.append([int(i) for i in ids])
        owner.append(own)
        if nei is not None:
            neighbour.append(nei)

    if True:
        # internal faces of this rank's own chunk, +x normal
        for ix in range(1, nc):
            face([lp(ix, 0, 0), lp(ix, 1, 0), lp(ix, 1, 1), lp(ix, 0, 1)], ix - 1, ix)

        # THE REAL PATCHES, every one listed on every rank even at nFaces 0 (property 1).
        inlet_start = len(faces)
        if r == 0:
            face([lp(0, 0, 0), lp(0, 0, 1), lp(0, 1, 1), lp(0, 1, 0)], 0)
        n_inlet = len(faces) - inlet_start

        outlet_start = len(faces)
        if r == ranks - 1:
            face([lp(nc, 0, 0), lp(nc, 1, 0), lp(nc, 1, 1), lp(nc, 0, 1)], nc - 1)
        n_outlet = len(faces) - outlet_start

        wall_start = len(faces)
        for c in range(nc):
            face([lp(c, 0, 0), lp(c + 1, 0, 0), lp(c + 1, 0, 1), lp(c, 0, 1)], c)
            face([lp(c, 1, 0), lp(c, 1, 1), lp(c + 1, 1, 1), lp(c + 1, 1, 0)], c)
            face([lp(c, 0, 0), lp(c, 1, 0), lp(c + 1, 1, 0), lp(c + 1, 0, 0)], c)
            face([lp(c, 0, 1), lp(c + 1, 0, 1), lp(c + 1, 1, 1), lp(c, 1, 1)], c)
        n_wall = len(faces) - wall_start

        patches = [("inlet", "patch", inlet_start, n_inlet, None),
                   ("outlet", "patch", outlet_start, n_outlet, None),
                   ("wall", "wall", wall_start, n_wall, None)]

        # ...AND THE PROCESSOR PATCHES LAST (property 2). One face each side of every cut.
        if r > 0:
            s = len(faces)
            face([lp(0, 0, 0), lp(0, 0, 1), lp(0, 1, 1), lp(0, 1, 0)], 0)
            patches.append((f"procBoundary{r}to{r - 1}", PROCESSOR_PATCH_TYPE, s, 1, r - 1))
            n_proc_faces += 1
        if r < ranks - 1:
            s = len(faces)
            face([lp(nc, 0, 0), lp(nc, 1, 0), lp(nc, 1, 1), lp(nc, 0, 1)], nc - 1)
            patches.append((f"procBoundary{r}to{r + 1}", PROCESSOR_PATCH_TYPE, s, 1, r + 1))
            n_proc_faces += 1

        if binary:
            import numpy as np

            offs = [0]
            for ids in face_ids:
                offs.append(offs[-1] + len(ids))
            flat = [i for ids in face_ids for i in ids]
            _foam_binary(polymesh / "points", "vectorField", "points",
                         [(len(xyz), np.asarray(xyz, dtype="<f8").tobytes())])
            # TWO LISTS, which is what a faceCompactList is: the offsets, then the flat vertices.
            _foam_binary(polymesh / "faces", "faceCompactList", "faces",
                         [(len(offs), np.asarray(offs, dtype="<i4").tobytes()),
                          (len(flat), np.asarray(flat, dtype="<i4").tobytes())])
            _foam_binary(polymesh / "owner", "labelList", "owner",
                         [(len(owner), np.asarray(owner, dtype="<i4").tobytes())])
            _foam_binary(polymesh / "neighbour", "labelList", "neighbour",
                         [(len(neighbour), np.asarray(neighbour, dtype="<i4").tobytes())])
        else:
            _foam(polymesh / "points", "vectorField", "points", len(pts), pts)
            _foam(polymesh / "faces", "faceList", "faces", len(faces), faces)
            _foam(polymesh / "owner", "labelList", "owner", len(owner), [str(o) for o in owner])
            _foam(polymesh / "neighbour", "labelList", "neighbour", len(neighbour),
                  [str(n) for n in neighbour])

        body = []
        for name, ptype, start, n, nbr in patches:
            lines = [f"    {name}", "    {", f"        type {ptype};",
                     f"        nFaces {n};", f"        startFace {start};"]
            if nbr is not None:
                # the two keys an OpenFOAM processor patch always carries
                lines += [f"        myProcNo {r};", f"        neighbProcNo {nbr};"]
            lines.append("    }")
            body.append("\n".join(lines))
        _foam(polymesh / "boundary", "polyBoundaryMesh", "boundary", len(patches), body)
    return n_proc_faces
