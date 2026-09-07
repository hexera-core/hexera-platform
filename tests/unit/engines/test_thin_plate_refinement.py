# Every kind=orifice shape in the baseline corpus failed; every kind=venturi passed. The
# difference is a thin plate: a 3-9 mm disc with a bore, sitting inside a 106-290 mm pipe.
# The internal plan sizes its wall cell from the PIPE BORE (bore / cells_across ~ 4.4 mm),
# so the cell is wider than the plate, castellation never captures it, the plate's faces
# never become patches and the run dies at the manifest gate. Refining globally to 3 mm
# would detonate the budget across a 400 mm pipe, so the plate gets a LOCAL refinement box.
from __future__ import annotations

import numpy as np

from meshpipeline.cad.thin_features import thin_refinement_boxes
from meshpipeline.engines.snappy.snappy_runner import render_internal_case


def _plate(thickness: float, half: float = 0.05, n: int = 14):
    """Two opposing sheets `thickness` apart - the two faces of a plate. Triangles are
    small relative to the gap so the opposing-normal probe can see across it."""
    tris = []
    xs = np.linspace(-half, half, n)
    ys = np.linspace(-half, half, n)
    for z, flip in ((0.0, False), (thickness, True)):
        for i in range(n - 1):
            for j in range(n - 1):
                a = (xs[i], ys[j], z)
                b = (xs[i + 1], ys[j], z)
                c = (xs[i + 1], ys[j + 1], z)
                d = (xs[i], ys[j + 1], z)
                if flip:                      # opposite winding -> opposing normals
                    tris += [[a, c, b], [a, d, c]]
                else:
                    tris += [[a, b, c], [a, c, d]]
    return np.array(tris, dtype=float)


def test_a_plate_thinner_than_the_cell_is_detected_and_boxed():
    # 3 mm plate against the 4.4 mm wall cell an orifice case actually plans
    boxes = thin_refinement_boxes(_plate(0.003), cell_m=0.0044)
    assert boxes, "a 3 mm plate under a 4.4 mm cell must be detected"
    b = boxes[0]
    assert b["level_bump"] >= 1
    # the box must actually enclose the plate
    assert b["min"][2] <= 0.0 and b["max"][2] >= 0.003
    # and the refinement must be enough to fit cells across the plate
    refined_cell = 0.0044 / (2 ** b["level_bump"])
    assert refined_cell <= 0.003, "refinement must bring the cell under the plate thickness"


def test_a_chunky_wall_is_not_refined():
    # a 40 mm gap under a 4.4 mm cell is comfortably resolved - no box, no wasted cells
    assert thin_refinement_boxes(_plate(0.040), cell_m=0.0044) == []


def test_a_pinhole_cannot_buy_unbounded_refinement():
    boxes = thin_refinement_boxes(_plate(0.00002), cell_m=0.0044, max_level_bump=4)
    assert boxes and boxes[0]["level_bump"] == 4


def _render(tmp_path, thin_regions):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}
    feats = {k: f"{k}.eMesh" for k in names}
    summary = render_internal_case(
        ws, names=names, features=feats, interior_point=(0, 0, 0),
        bbox_min=(-0.2, -0.06, -0.06), bbox_max=(0.2, 0.06, 0.06),
        base_cell=0.0044, surface_level=2, feature_level=3, n_layers=3,
        wall_key="wall", thin_regions=thin_regions)
    return (ws / "system" / "snappyHexMeshDict").read_text(), summary


def test_renderer_authors_a_local_box_for_the_thin_region(tmp_path):
    region = {"min": [-0.02, -0.05, -0.001], "max": [0.02, 0.05, 0.004],
              "level_bump": 2, "thinnest_m": 0.003, "n_triangles": 120}
    text, summary = _render(tmp_path, [region])
    # a searchableBox in geometry, and a matching inside-mode refinement region
    assert "thinZone0 { type searchableBox;" in text
    assert "thinZone0 { mode inside; levels ((1e15" in text
    # the level is the wall level plus the measured bump, and it is reported honestly
    assert summary["thin_regions"][0]["level"] == summary["surface_level"][0] + 2
    assert summary["thin_regions"][0]["thinnest_m"] == 0.003


def test_no_thin_regions_leaves_the_case_exactly_as_before(tmp_path):
    text, summary = _render(tmp_path, [])
    assert "thinZone" not in text
    assert summary["thin_regions"] == []


# ---- local and bounded: the blade-row lesson (job 3cd77f85) ------------------------------------
# Five trailing-edge strips around an annulus are thin along their whole span. One hull over all
# of them was the entire passage, refined to the finest level: 7 M cells for a 2 M budget.


def _strip(cx, cy, angle, thickness=0.002, length=0.09, width=0.012, n=12):
    """A thin plate 'length' long, 'width' wide, rotated by 'angle' about z at (cx, cy)."""
    ca, sa = np.cos(angle), np.sin(angle)
    tris = []
    us = np.linspace(-length / 2, length / 2, n)
    vs = np.linspace(-width / 2, width / 2, 3)
    for z, flip in ((0.0, False), (thickness, True)):
        for i in range(n - 1):
            for j in range(2):
                def P(u, v, z=z):
                    return (cx + u * ca - v * sa, cy + u * sa + v * ca, z)
                a, b = P(us[i], vs[j]), P(us[i + 1], vs[j])
                c, d = P(us[i + 1], vs[j + 1]), P(us[i], vs[j + 1])
                tris += ([[a, c, b], [a, d, c]] if flip else [[a, b, c], [a, c, d]])
    return np.array(tris, dtype=float)


def _blade_row_edges():
    strips = [_strip(0.2 * np.cos(t), 0.2 * np.sin(t), t + np.pi / 2)
              for t in np.linspace(0, 2 * np.pi, 5, endpoint=False)]
    return np.concatenate(strips, axis=0)


def test_scattered_thin_strips_get_tight_boxes_not_one_hull():
    tris = _blade_row_edges()
    boxes = thin_refinement_boxes(tris, cell_m=0.016)
    assert len(boxes) >= 5, "five separate strips must not collapse into one box"
    # the hull every thin triangle shares, padded the way each box is (0.6 cell each side)
    hull = np.ptp(tris.reshape(-1, 3), axis=0) + 2 * 0.6 * 0.016
    hull_vol = float(np.prod(hull))
    total = sum(b["volume_m3"] for b in boxes)
    assert total < 0.25 * hull_vol, f"boxes cover {total / hull_vol:.0%} of the hull - not local"
    # every thin triangle is inside some box
    cents = tris.mean(axis=1)
    inside = np.zeros(len(cents), dtype=bool)
    for b in boxes:
        lo, hi = np.asarray(b["min"]), np.asarray(b["max"])
        inside |= np.all((cents >= lo) & (cents <= hi), axis=1)
    assert inside.all()


def test_the_cell_budget_bounds_the_extra_refinement():
    plate = _plate(0.003)
    free = thin_refinement_boxes(plate, cell_m=0.0044)
    assert free and all(b["level_bump"] == free[0]["level_bump"] for b in free)
    assert free[0]["level_bump"] >= 1 and all("est_cells" in b for b in free)
    tight = thin_refinement_boxes(plate, cell_m=0.0044, budget_cells=1_000)
    assert tight and tight[0]["level_bump"] == 1, "a tiny budget must clamp the bump to +1"
    roomy = thin_refinement_boxes(plate, cell_m=0.0044, budget_cells=10 ** 9)
    assert roomy[0]["level_bump"] == free[0]["level_bump"], "a roomy budget clamps nothing"


def test_a_single_plate_is_still_one_compact_box():
    boxes = thin_refinement_boxes(_plate(0.003), cell_m=0.0044)
    assert 1 <= len(boxes) <= 2
    lo, hi = np.asarray(boxes[0]["min"]), np.asarray(boxes[0]["max"])
    assert (lo <= [-0.05, -0.05, 0.0]).all() and (hi >= [0.05, 0.05, 0.003]).all()
