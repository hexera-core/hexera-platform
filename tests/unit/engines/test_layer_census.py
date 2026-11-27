# The engine-neutral layer census: walks from each wall face into the mesh and counts the flat,
# wall-parallel cells - the layers a mesher extruded. cfMesh prints no layer coverage, so its
# manifest reported none and the reviewer could not judge the layers the brief asked for.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.layer_census import bl_cells, layer_census, requested_layers

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


def test_a_flat_background_cell_past_one_layer_is_not_a_second_layer(tmp_path):
    # ONE thin layer, then the first background cell - flat enough (0.5 of the face width) and
    # parallel, but ten times the layer's height: a single layer, not the two that were asked
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 0.5, 1.0, 1.0])
    out = layer_census(pm, 2, ["wall"])["wall"]
    assert out["mean_layers"] == 1.0 and out["coverage_pct"] == 50.0
    assert out["layers_by_area"] == {1: 1.0}
    assert out["area_with_full_layers"] == 0.0


def test_a_growing_stack_is_counted_with_its_heights(tmp_path):
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.02, 0.024, 0.0288, 0.03456, 0.041472, 1.0, 1.0])
    out = layer_census(pm, 5, ["wall"])["wall"]
    assert out["mean_layers"] == 5.0 and out["mean_layers_found"] == 5.0
    assert abs(out["first_layer_m"] - 0.02) < 1e-9
    assert abs(out["growth"] - 1.2) < 1e-6
    assert abs(out["stack_m"] - 0.148832) < 1e-6


def test_layers_short_of_the_request_read_as_short(tmp_path):
    # two layers where five were asked: 40% of the requested layer cells, and the figures say so
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 0.06, 1.0, 1.0])
    out = layer_census(pm, 5, ["wall"])["wall"]
    assert out["mean_layers"] == 2.0 and out["coverage_pct"] == 40.0


def test_why_a_mesh_is_not_measured_is_said(tmp_path, monkeypatch):
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 1.0])
    monkeypatch.setattr(lc, "CENSUS_MAX_CELLS", 3)
    out, why = lc.census(pm, 2, ["wall"])
    assert out == {} and "above the census limit" in why
    monkeypatch.undo()
    (pm / "owner").write_text((pm / "owner").read_text().replace("format ascii", "format binary"))
    out, why = lc.census(pm, 2, ["wall"])
    assert out == {} and "ascii" in why


def test_the_requested_count_is_the_users(tmp_path):
    from meshpipeline.engines.layer_census import requested_layers_for, stated_layers
    assert stated_layers("Resolve the near-wall flow with 5 prism layers (expansion ratio 1.2).") == 5
    assert stated_layers("Near-wall: wall functions, five boundary layers, growth 1.2") == 5
    assert stated_layers("n_layers = 8") == 8
    assert stated_layers("Inflate 5 prism boundary layers on the body.") == 5
    assert stated_layers("Add five wall boundary layers targeting y+ = 30-300.") == 5
    assert stated_layers("Use 3 layers on the wall for y+.") == 3
    assert stated_layers("a 3 layers deep stack of plates") == 0     # no near-wall layer word
    assert stated_layers("Standard mesh fidelity, 2 inlets.") == 0
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text("boundaryLayers { patchBoundaryLayers { wall { nLayers 2; } } }")
    assert requested_layers_for(tmp_path) == (2, 0, 2)
    (tmp_path / "request.txt").write_text("Internal CFD mesh ... 5 prism layers (expansion ratio 1.2).")
    assert requested_layers_for(tmp_path) == (5, 5, 2)


def test_nothing_is_claimed_without_a_mesh_or_layers(tmp_path):
    assert layer_census(tmp_path / "nope", 3, ["wall"]) == {}
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [1.0, 1.0])
    assert layer_census(pm, 0, ["wall"]) == {}
    (tmp_path / "meshDict").write_text("boundaryLayers { patchBoundaryLayers { body { nLayers 3; } } }")
    assert requested_layers(tmp_path / "meshDict") == 3
    assert requested_layers(tmp_path / "missing") == 0


# CFDREQ-14: a layer is the one asked only at the height asked
def test_layers_at_the_planned_height_count(tmp_path):
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.02, 0.024, 1.0, 1.0])
    out, _ = lc.census(pm, 2, ["wall"], planned_first_m=0.02)
    w = out["wall"]
    assert w["coverage_pct"] == 100.0 and w["coverage_at_plan_pct"] == 100.0 and w["area_at_plan"] == 1.0
    assert abs(w["first_cell_m"] - 0.02) < 1e-9 and w["first_vs_plan"] == 1.0


def test_a_stack_far_thicker_than_planned_is_not_the_layer_asked(tmp_path):
    # the shape of two layers, but the first one 150 times the planned height (static_mixer_004, cfMesh, resolved)
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.03, 0.036, 1.0, 1.0])
    out, _ = lc.census(pm, 2, ["wall"], planned_first_m=0.0002)
    w = out["wall"]
    assert w["coverage_pct"] == 100.0                     # by shape
    assert w["coverage_at_plan_pct"] == 0.0 and w["area_at_plan"] == 0.0
    assert w["first_vs_plan"] == 150.0
    q = lc.height_quality(out)
    assert q["layer_coverage_basis"] == "height" and q["layer_area_at_plan_pct"] == 0.0
    assert abs(q["layer_first_cell_m"] - 0.03) < 1e-9


def test_the_first_cell_is_read_where_no_layer_grew(tmp_path):
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.7, 1.0])
    out, _ = lc.census(pm, 1, ["wall"], planned_first_m=0.01)
    assert abs(out["wall"]["first_cell_m"] - 0.7) < 1e-9 and out["wall"]["area_at_plan"] == 0.0


def test_a_neighbour_list_padded_for_boundary_faces_is_read_to_its_internal_faces(tmp_path):
    # cfMesh writes -1 in neighbour for every boundary face: the walk must not step through a wall into cell -1
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.05, 0.06, 0.072, 1.0])
    plain, _ = lc.census(pm, 3, ["wall"])
    nb = (pm / "neighbour").read_text()
    head, _, body = nb.partition("\n(\n")
    n_int = int(head.strip().split()[-1])
    n_all = len((pm / "owner").read_text().partition("\n(\n")[2].split()) - 1
    vals = body.rsplit(")", 1)[0].split() + ["-1"] * (n_all - n_int)
    (pm / "neighbour").write_text(head.rsplit(str(n_int), 1)[0] + f"{n_all}\n(\n" + "\n".join(vals) + "\n)\n")
    padded, _ = lc.census(pm, 3, ["wall"])
    assert padded == plain


def test_the_planned_first_layer_is_read_from_the_case(tmp_path):
    from meshpipeline.engines.layer_census import planned_first_of
    (tmp_path / "system").mkdir()
    assert planned_first_of(tmp_path) is None
    (tmp_path / "system" / "meshDict").write_text(
        "wall { nLayers 2; thicknessRatio 1.2; maxFirstLayerThickness 2.09567e-05; allowedDiscontinuity 0; }")
    assert planned_first_of(tmp_path) == 2.09567e-05
    (tmp_path / "system" / "snappyHexMeshDict").write_text("relativeSizes true; firstLayerThickness 0.3;")
    assert planned_first_of(tmp_path) is None


# CFDREQ-21: one wall-function layer at the height asked is a layer, whatever its share of the face's width
def test_one_wall_function_layer_at_the_planned_height_covers_the_wall(tmp_path):
    from meshpipeline.engines import layer_census as lc
    pm = tmp_path / "polyMesh"
    # unit-wide wall faces, one layer 0.62 high (planned 0.5: half the wall cell), then the core
    _write_column_mesh(pm, [0.62, 0.5, 1.0, 1.0])
    out, _ = lc.census(pm, 1, ["wall"], planned_first_m=0.5)
    w = out["wall"]
    assert w["coverage_pct"] == 0.0                       # by shape: too tall for FLAT_RATIO
    assert w["area_at_plan"] == 1.0 and w["coverage_at_plan_pct"] == 100.0
    assert lc.height_quality(out)["layer_coverage_at_plan_pct"] == 100.0
    # the first cell twice the plan is not the layer asked, nor is a wall cell as tall as it is wide
    out, _ = lc.census(pm, 1, ["wall"], planned_first_m=0.3)
    assert out["wall"]["area_at_plan"] == 0.0 and out["wall"]["coverage_at_plan_pct"] == 0.0
    pm2 = tmp_path / "cubes" / "polyMesh"
    _write_column_mesh(pm2, [1.0, 1.0, 1.0])
    out, _ = lc.census(pm2, 1, ["wall"], planned_first_m=0.7)
    assert out["wall"]["area_at_plan"] == 0.0


def test_the_cells_across_a_boundary_layer_count_layers_and_core_cells(tmp_path):
    # CFDREQ-22: 4 layers (0.1, 0.12, 0.144, 0.173 = 0.537) then unit core cells: across a 1.0 layer, 4 layers and
    # 0.463 of the first core cell
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.1, 0.12, 0.144, 0.1728, 1.0, 1.0, 1.0])
    got, why = bl_cells(pm, ["wall"], 1.0, met_at=4.0)
    assert why == "" and got["bl_cells_median"] == pytest.approx(4.4632, abs=0.01)
    assert got["bl_area_met_pct"] == 100.0 and got["bl_faces"] == 9
    got, _ = bl_cells(pm, ["wall"], 1.0, met_at=9.5)
    assert got["bl_area_met_pct"] == 0.0
    # a layer thicker than the whole mesh counts what was crossed, nothing more
    got, _ = bl_cells(pm, ["wall"], 50.0, met_at=9.5)
    assert got["bl_cells_median"] == pytest.approx(7.0, abs=1e-6)
    # ten thin cells across a thin layer
    pm2 = tmp_path / "fine" / "polyMesh"
    _write_column_mesh(pm2, [0.01] * 12)
    got, _ = bl_cells(pm2, ["wall"], 0.1, met_at=9.5)
    assert got["bl_cells_median"] == pytest.approx(10.0, abs=1e-6) and got["bl_area_met_pct"] == 100.0
    assert bl_cells(pm2, ["nowall"], 0.1, met_at=9.5)[0] == {}
