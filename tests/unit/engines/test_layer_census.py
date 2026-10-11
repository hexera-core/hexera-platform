# The engine-neutral layer census: walks from each wall face into the mesh and counts the flat,
# wall-parallel cells - the layers a mesher extruded. cfMesh prints no layer coverage, so its
# manifest reported none and the reviewer could not judge the layers the brief asked for.
from __future__ import annotations

from pathlib import Path

import numpy as np

from meshpipeline.engines.layer_census import layer_census, requested_layers

HDR = ('FoamFile\n{{\n    version 2.0;\n    format ascii;\n    class {cls};\n    object {obj};\n}}\n')


def _write_column_mesh(pm: Path, heights, nx: int = 3) -> None:
    """nx x nx cells in x/y (unit width each), stacked in z with the given heights; the z=0 face
    is the patch 'wall', every other boundary face 'side'."""
    zs = np.concatenate([[0.0], np.cumsum(heights)])
    nz = len(heights)
    pid = lambda i, j, k: i + (nx + 1) * (j + (nx + 1) * k)  # noqa: E731
    pts = [(float(i), float(j), float(z)) for z in zs for j in range(nx + 1) for i in range(nx + 1)]
    cid = lambda i, j, k: i + nx * (j + nx * k)  # noqa: E731
    internal, wall, side = [], [], []
    for k in range(nz):
        for j in range(nx):
            for i in range(nx):
                c = cid(i, j, k)
                if i + 1 < nx:
                    internal.append(([pid(i + 1, j, k), pid(i + 1, j + 1, k), pid(i + 1, j + 1, k + 1),
                                      pid(i + 1, j, k + 1)], c, cid(i + 1, j, k)))
                if j + 1 < nx:
                    internal.append(([pid(i, j + 1, k), pid(i, j + 1, k + 1), pid(i + 1, j + 1, k + 1),
                                      pid(i + 1, j + 1, k)], c, cid(i, j + 1, k)))
                if k + 1 < nz:
                    internal.append(([pid(i, j, k + 1), pid(i + 1, j, k + 1), pid(i + 1, j + 1, k + 1),
                                      pid(i, j + 1, k + 1)], c, cid(i, j, k + 1)))
                if k == 0:
                    wall.append(([pid(i, j, 0), pid(i, j + 1, 0), pid(i + 1, j + 1, 0), pid(i + 1, j, 0)], c))
                if k == nz - 1:
                    side.append(([pid(i, j, nz), pid(i + 1, j, nz), pid(i + 1, j + 1, nz),
                                  pid(i, j + 1, nz)], c))
                if i == 0:
                    side.append(([pid(0, j, k), pid(0, j, k + 1), pid(0, j + 1, k + 1), pid(0, j + 1, k)], c))
                if i == nx - 1:
                    side.append(([pid(nx, j, k), pid(nx, j + 1, k), pid(nx, j + 1, k + 1), pid(nx, j, k + 1)], c))
                if j == 0:
                    side.append(([pid(i, 0, k), pid(i + 1, 0, k), pid(i + 1, 0, k + 1), pid(i, 0, k + 1)], c))
                if j == nx - 1:
                    side.append(([pid(i, nx, k), pid(i, nx, k + 1), pid(i + 1, nx, k + 1), pid(i + 1, nx, k)], c))
    faces = [f for f, _, _ in internal] + [f for f, _ in wall] + [f for f, _ in side]
    owner = [o for _, o, _ in internal] + [o for _, o in wall] + [o for _, o in side]
    neigh = [n for _, _, n in internal]
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "points").write_text(HDR.format(cls="vectorField", obj="points") + f"{len(pts)}\n(\n"
                               + "\n".join(f"({x} {y} {z})" for x, y, z in pts) + "\n)\n")
    (pm / "faces").write_text(HDR.format(cls="faceList", obj="faces") + f"{len(faces)}\n(\n"
                              + "\n".join(f"4({' '.join(map(str, f))})" for f in faces) + "\n)\n")
    (pm / "owner").write_text(HDR.format(cls="labelList", obj="owner") + f"{len(owner)}\n(\n"
                              + "\n".join(map(str, owner)) + "\n)\n")
    (pm / "neighbour").write_text(HDR.format(cls="labelList", obj="neighbour") + f"{len(neigh)}\n(\n"
                                  + "\n".join(map(str, neigh)) + "\n)\n")
    nI = len(internal)
    (pm / "boundary").write_text(
        HDR.format(cls="polyBoundaryMesh", obj="boundary") + "2\n(\n"
        f"    wall {{ type wall; nFaces {len(wall)}; startFace {nI}; }}\n"
        f"    side {{ type patch; nFaces {len(side)}; startFace {nI + len(wall)}; }}\n)\n")


def test_three_extruded_layers_are_counted(tmp_path):
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 0.06, 0.072, 1.0, 1.0, 1.0])
    out = layer_census(pm, 3, ["wall"])["wall"]
    assert out["faces"] == 9 and out["coverage_pct"] == 100.0
    assert out["area_with_full_layers"] == 1.0 and out["mean_layers"] == 3.0


def test_a_mesh_without_layers_reads_none(tmp_path):
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [1.0, 1.0, 1.0, 1.0])
    out = layer_census(pm, 3, ["wall"])["wall"]
    assert out["coverage_pct"] == 0.0 and out["area_with_any_layer"] == 0.0


def test_one_of_two_asked_layers(tmp_path):
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 1.0, 1.0])
    out = layer_census(pm, 2, ["wall"])["wall"]
    assert out["coverage_pct"] == 50.0 and out["area_with_full_layers"] == 0.0


def test_nothing_is_claimed_without_a_mesh_or_layers(tmp_path):
    assert layer_census(tmp_path / "nope", 3, ["wall"]) == {}
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [1.0, 1.0])
    assert layer_census(pm, 0, ["wall"]) == {}
    (tmp_path / "meshDict").write_text("boundaryLayers { patchBoundaryLayers { body { nLayers 3; } } }")
    assert requested_layers(tmp_path / "meshDict") == 3
    assert requested_layers(tmp_path / "missing") == 0
