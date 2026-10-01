# Responsibility: Verify faces lying on each other never read as a thin plate, and local thin refinement never passes an over-budget request through.
# Boundaries: cad/thin_features.thin_refinement_boxes on synthetic surfaces; the driver's use of it is tested beside the driver.
"""Job 470c3eb9 (shell-and-tube exchanger, shell side): the staged wall carries zero-thickness
sheets - baffle and end faces lying on each other, a baffle rim touching the shell - and the
thin-feature probe read them as plates "0.0 mm across". That bought the maximum level bump, an
estimated 8.6 M cells of local refinement went through on a 1 M allowance, and snappyHexMesh ran
out of time twice. A reading inside the faces' own tessellation error is a contact, not a plate;
and a refinement the budget cannot pay for is cut down, with a note, never passed through."""
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.thin_features import (
    ThinRegions,
    measure_from_triangles,
    thin_refinement_boxes,
)


def _sheet(z: float, *, flip: bool, half: float = 0.05, n: int = 14, shift: float = 0.0):
    """One square sheet at height z; `flip` reverses its winding (its normal)."""
    xs = np.linspace(-half, half, n) + shift
    ys = np.linspace(-half, half, n) + shift
    tris = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b = (xs[i], ys[j], z), (xs[i + 1], ys[j], z)
            c, d = (xs[i + 1], ys[j + 1], z), (xs[i], ys[j + 1], z)
            tris += ([[a, c, b], [a, d, c]] if flip else [[a, b, c], [a, c, d]])
    return np.array(tris, dtype=float)


def _plate(thickness: float, **kw):
    return np.concatenate([_sheet(0.0, flip=False, **kw), _sheet(thickness, flip=True, **kw)])


def _cylinder(radius: float, *, segments: int, outward: bool, phase: float = 0.0,
              length: float = 0.1, rings: int = 4):
    """A tessellated cylinder about z, its normals outward or inward."""
    th = np.linspace(0.0, 2 * math.pi, segments + 1)[:-1] + phase
    zs = np.linspace(0.0, length, rings + 1)
    tris = []
    for k in range(rings):
        for i in range(segments):
            t0, t1 = th[i], th[(i + 1) % segments]
            p = [(radius * math.cos(t), radius * math.sin(t), z) for t in (t0, t1) for z in
                 (zs[k], zs[k + 1])]
            a, a_up, b, b_up = p
            tris += ([[a, b, b_up], [a, b_up, a_up]] if outward
                     else [[a, b_up, b], [a, a_up, b_up]])
    return np.array(tris, dtype=float)


# faces lying on each other are not a plate

def test_coincident_flat_sheets_read_zero_and_drive_no_refinement():
    sheets = _plate(0.0)
    assert np.nanmin(measure_from_triangles(sheets).thickness_m) == 0.0   # the raw probe
    boxes = thin_refinement_boxes(sheets, cell_m=0.0044)
    assert boxes == []
    assert boxes.coincident_triangles > 0, "the sheets must be recognised, not silently missed"


def test_a_zero_thickness_baffle_offset_in_its_plane_drives_no_refinement():
    # the exchanger's case: two sheets in ONE plane, tessellated differently (centroids apart),
    # normals opposed - the probe reads exactly 0 across a lateral offset
    baffle = np.concatenate([_sheet(0.0, flip=False), _sheet(0.0, flip=True, n=11)])
    assert thin_refinement_boxes(baffle, cell_m=0.0044) == []


def test_a_micron_reading_is_noise_not_a_plate():
    # 1 micron on a 140 mm part: float noise of coincident faces, far below any real feature
    assert thin_refinement_boxes(_plate(1e-6), cell_m=0.0044) == []


def test_two_tessellations_of_one_curved_face_touching_drive_no_refinement():
    # a baffle rim lying on the shell: the same 136 mm cylinder, tessellated twice and out of
    # phase, normals opposed. The chords disagree by up to ~0.3 mm - read as a "gap" by the
    # probe, but it is the facets' own sag, not a thickness.
    shell = _cylinder(0.136, segments=48, outward=True)
    rim = _cylinder(0.136, segments=48, outward=False, phase=math.pi / 48)
    both = np.concatenate([shell, rim])
    raw = measure_from_triangles(both).thickness_m
    assert np.isfinite(raw).any() and np.nanmin(raw[np.isfinite(raw)]) < 1e-3
    assert thin_refinement_boxes(both, cell_m=0.0044) == []


def test_a_real_thin_sleeve_on_a_curved_face_is_still_found():
    # the guard must not blind the probe to a real curved plate: a 2 mm sleeve wall
    outer = _cylinder(0.136, segments=48, outward=True)
    inner = _cylinder(0.134, segments=48, outward=False, phase=math.pi / 48)
    boxes = thin_refinement_boxes(np.concatenate([outer, inner]), cell_m=0.0044)
    assert boxes, "a 2 mm wall under a 4.4 mm cell must be detected"
    assert 0.0015 < min(b["thinnest_m"] for b in boxes) < 0.0025


def test_a_real_plate_beside_coincident_sheets_is_still_refined():
    plate = _plate(0.003)
    touching = _plate(0.0, shift=0.5)          # coincident sheets half a metre away
    boxes = thin_refinement_boxes(np.concatenate([plate, touching]), cell_m=0.0044)
    assert boxes and all(b["max"][0] < 0.3 for b in boxes), \
        "only the real plate is boxed; the touching sheets are not"
    assert min(b["thinnest_m"] for b in boxes) == pytest.approx(0.003, rel=1e-6)


@pytest.mark.parametrize("cell", [0.0, -0.004, float("nan"), float("inf")])
def test_no_planned_cell_is_no_refinement(cell):
    assert thin_refinement_boxes(_plate(0.003), cell_m=cell) == []


def test_the_result_is_still_a_plain_list_of_boxes():
    boxes = thin_refinement_boxes(_plate(0.003), cell_m=0.0044)
    assert isinstance(boxes, list) and isinstance(boxes, ThinRegions)
    assert boxes.note == "" and boxes.found == len(boxes)
    assert all({"min", "max", "level_bump", "thinnest_m"} <= set(b) for b in boxes)


# the refinement sanity cap

def test_a_refinement_the_budget_cannot_pay_for_is_cut_down_with_a_note():
    small = _plate(0.003, half=0.01)                              # a compact orifice-sized plate
    big = _plate(0.003, half=0.2, shift=1.0)                      # a big hull-like thin region
    tris = np.concatenate([small, big])
    free = thin_refinement_boxes(tris, cell_m=0.0044)
    assert sum(b["est_cells"] for b in free) > 5_000
    capped = thin_refinement_boxes(tris, cell_m=0.0044, budget_cells=5_000)
    assert capped, "the compact feature that fits must still be refined"
    assert all(b["level_bump"] == 1 for b in capped)
    assert sum(b["est_cells"] for b in capped) <= 5_000, "the cap was passed through"
    assert any(b["max"][0] < 0.5 for b in capped), "the compact plate's box is kept"
    assert capped.found > 2 * len(capped), "most of the big region is left unrefined"
    assert "left unrefined" in capped.note and "5,000 cells allowed" in capped.note


def test_nothing_fits_means_nothing_is_refined_and_the_note_says_why():
    boxes = thin_refinement_boxes(_plate(0.003), cell_m=0.0044, budget_cells=10)
    assert boxes == [] and boxes.found >= 1
    assert "left unrefined" in boxes.note
    assert boxes.thinnest_m == pytest.approx(0.003, rel=1e-6)


def test_a_level_cap_is_said_out_loud():
    boxes = thin_refinement_boxes(_plate(0.00002), cell_m=0.0044, max_level_bump=4)
    assert boxes and boxes[0]["level_bump"] == 4
    assert "4 extra level(s) instead of the" in boxes.note and "under-resolved" in boxes.note
