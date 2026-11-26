# Responsibility: Verify the wall's faces are priced before meshing from what snappyHexMesh will refine (the feature
# edges' band), and that the size brake judges a pass at castellation's end on the castellated wall (RUN-12).
# Boundaries: staged STL / dict / boundary files written here; no OpenFOAM run.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.engines import size_brake as sb
from meshpipeline.engines.size_brake import PLAN_CELLS_FILE
from meshpipeline.engines.snappy import snappy_runner as R
from meshpipeline.engines.snappy import wall_price as wp


def _write_stl(path: Path, tris) -> None:
    with path.open("w") as fh:
        fh.write("solid body\n")
        for t in tris:
            fh.write(" facet normal 0 0 0\n  outer loop\n")
            for v in t:
                fh.write(f"   vertex {v[0]:.9g} {v[1]:.9g} {v[2]:.9g}\n")
            fh.write("  endloop\n endfacet\n")
        fh.write("endsolid body\n")


def _box(a: float = 1.0):
    """A closed box of side a, outward triangles: 12 sharp edges, 12a of them."""
    v = [(x, y, z) for x in (0, a) for y in (-a / 2, a / 2) for z in (-a / 2, a / 2)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for q in quads:
        tris += [(v[q[0]], v[q[1]], v[q[2]]), (v[q[0]], v[q[2]], v[q[3]])]
    return tris


def test_the_feature_length_is_the_edges_surfacefeatureextract_marks(tmp_path):
    _write_stl(tmp_path / "box.stl", _box(2.0))
    # 12 edges of 2 m at 90 degrees; the faces' own diagonals are flat (0 degrees) and not features
    assert wp.feature_length(tmp_path / "box.stl", 150.0) == pytest.approx(24.0)
    # includedAngle 30 marks only edges sharper than 150 degrees between normals: none on a box
    assert wp.feature_length(tmp_path / "box.stl", 30.0) == pytest.approx(0.0)
    # an open plate: its 4 open edges are features
    _write_stl(tmp_path / "plate.stl", [((0, 0, 0), (1, 0, 0), (1, 1, 0)), ((0, 0, 0), (1, 1, 0), (0, 1, 0))])
    assert wp.feature_length(tmp_path / "plate.stl", 150.0) == pytest.approx(4.0)
    assert wp.feature_length(tmp_path / "missing.stl") == 0.0


def test_the_feature_band_is_the_feature_level_tube_and_the_buffer_between(tmp_path):
    # RM5/RM3: wall at level 5 (h), features at 7, nCellsBetweenLevels 3: per metre, each side,
    # level 7: h/4 * (16 - 1)/h^2 = 3.75/h; level 6: 3 h/2 * (4 - 1)/h^2 = 4.5/h; two sides: 16.5/h
    h = 0.28
    assert wp.band_faces(1.0, wall_cell=h, surface_level=5, feature_level=7, n_between=3) == pytest.approx(16.5 / h)
    assert wp.band_faces(1.0, wall_cell=h, surface_level=5, feature_level=5) == 0.0
    # RM3 as staged (c2_rm3k): 8,544.9 m2 at 285.5 mm, 8,081 m of feature edges: priced 0.105 M before, 0.572 M now;
    # snappy built 0.484 M wall faces on both passes ((ideal with layers - castellated) / 17)
    plain = 8544.8788 / 0.285465 ** 2
    new = plain + wp.band_faces(8081.3, wall_cell=0.285465, surface_level=5, feature_level=7)
    assert plain / 484_000 < 0.25 and 1.0 < new / 484_000 < 1.25


def test_an_emesh_reads_back_its_edge_length(tmp_path):
    (tmp_path / "e.eMesh").write_text(
        "FoamFile\n{\n    version 2.0;\n    class featureEdgeMesh;\n}\n\n3\n(\n(0 0 0)\n(1 0 0)\n(1 2 0)\n)\n\n"
        "2\n(\n(0 1)\n(1 2)\n)\n")
    assert wp.emesh_length(tmp_path / "e.eMesh") == pytest.approx(3.0)


REC = {"base_cell": 0.041666666666666664, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 6.0, "cells_across_min_feature": 8}
ANALYSIS = {"bbox_min": [0.0, -0.5, -0.5], "bbox_max": [1.0, 0.5, 0.5], "L": 1.0, "surface_area": 6.0}


def _render(ws: Path, *, k: float | None = None) -> dict:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    _write_stl(ws / "constant" / "triSurface" / "body.stl", _box(1.0))
    (ws / "system" / "surfaceFeatureExtractDict").write_text("extractFromSurfaceCoeffs { includedAngle 150; }\n")
    strategy = {"n_layers": 5, "last_layer_rel": 0.35, "max_cells": 8_000_000}
    if k is not None:
        strategy["_wall_face_factor"] = k
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                         recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                         strategy=strategy)
    return json.loads((ws / PLAN_CELLS_FILE).read_text())


def test_the_first_plan_prices_the_feature_band_and_a_learned_count_still_decides(tmp_path):
    rec = _render(tmp_path / "a")
    assert rec["feature_length_m"] == pytest.approx(12.0)
    assert rec["wall_faces_priced"] > rec["wall_faces_est"], "the band is in the first plan's price"
    assert rec["wall_face_factor"] == pytest.approx(rec["wall_faces_priced"] / rec["wall_faces_est"], rel=1e-3)
    # after a stopped pass the learned factor (RUN-8) is the measure; the priced one is not added on top
    learned = _render(tmp_path / "b", k=7.5)
    assert learned["wall_face_factor"] == pytest.approx(7.5) and "wall_faces_priced" not in learned


def _boundary(path: Path, patches) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"    {n}\n    {{\n        type            {t};\n        nFaces          {f};\n"
                   f"        startFace       0;\n    }}\n" for n, t, f in patches)
    path.write_text(f"FoamFile\n{{\n    class polyBoundaryMesh;\n}}\n\n{len(patches)}\n(\n{body})\n")


def test_the_brake_stops_a_pass_at_castellations_end_on_its_castellated_wall(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text(
        "addLayersControls { relativeSizes false; layers { body { nSurfaceLayers 17; } \"ground.*\" "
        "{ nSurfaceLayers 0; } } firstLayerThickness 0.004; }\n")
    assert sb.layer_counts_of(tmp_path) == {"body": 17}
    # RM5's first pass, on two ranks: 3.12 M castellated cells, 0.50 M wall faces under 17 layers
    for r, faces in ((0, 260_000), (1, 241_357)):
        _boundary(tmp_path / f"processor{r}" / "constant" / "polyMesh" / "boundary",
                  [("farfield", "patch", 9000), ("body", "wall", faces), ("ground", "wall", 4000),
                   (f"procBoundary{r}to{1 - r}", "processor", 7000)])
    log = tmp_path / "snappyHexMesh.log"
    brake = sb.SizeBrake(log, 4_000_000, case_dir=tmp_path, layers=sb.layer_counts_of(tmp_path))
    log.write_text("Refined mesh : cells:3124294  faces:9000000  points:3500000\n")
    assert brake() is None, "castellation has not ended: the wall is not on disk yet"
    with log.open("a") as fh:
        fh.write("Writing mesh to time constant\nWrote mesh in = 2.56 s.\nMesh refined in = 1275.40 s.\n")
    why = brake()
    assert why and "before snapping" in why
    t = brake.tripped
    assert t["stage"] == "with layers" and t["source"] == sb.CASTELLATED_WALL
    assert t["wall_faces"] == 501_357 and t["cells"] == 3_124_294 + 17 * 501_357
    # the re-plan learns the wall from it as from snappy's own with-layers count (RUN-8)
    assert sb.learned_face_factor(t, {"wall_faces_est": 42_000, "layers_est": 17}) == pytest.approx(501_357 / 42_000)


def test_a_pass_whose_castellated_wall_fits_runs_on(tmp_path):
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    _boundary(tmp_path / "constant" / "polyMesh" / "boundary", [("body", "wall", 100_000)])
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:1000000  faces:3000000\nMesh refined in = 10 s.\n")
    brake = sb.SizeBrake(log, 4_000_000, case_dir=tmp_path, layers={"body": 5})
    assert brake() is None and brake.seen[sb.CASTELLATED_WALL] == 1_500_000
    # without the case (cfMesh, or an old caller) it reads the log alone, as before
    old = sb.SizeBrake(tmp_path / "snappyHexMesh.log", 4_000_000)
    assert old() is None and sb.CASTELLATED_WALL not in old.seen
