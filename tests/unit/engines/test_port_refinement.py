# The corpus's largest real failure cluster (21 episodes: mini_housing, manifold,
# diffuser_plenum, tee_wye) died at the manifest gate with a zero-face port: the base cell
# is sized from the INLET bore, ports were refined one level COARSER than the wall, and a
# small side port ended up with cells bigger than its own opening - castellation sealed it.
# These tests pin the fix: per-port levels sized from each port's own diameter, volume
# refinement bands around raised ports, rim features at least as fine as the port surface,
# and one extra level for a port that sealed on the previous attempt in the same workspace.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.snappy.snappy_runner import (
    PORT_MIN_CELLS_ACROSS,
    _port_levels,
    render_internal_case,
)


def test_large_port_keeps_the_default_level():
    # bore-sized port: base_cell derived from it already gives plenty of cells across
    lv = _port_levels(base_cell=0.01, default_level=2, smin=3,
                      port_sizes={"inlet": 0.15})
    assert lv["inlet"] == 2


def test_small_port_level_rises_until_cells_fit_across_its_diameter():
    # 6mm port against a 10mm base cell: needs base/2^lvl <= 6mm/6 = 1mm -> lvl 4
    lv = _port_levels(base_cell=0.01, default_level=2, smin=6,
                      port_sizes={"tiny": 0.006})
    assert lv["tiny"] == 4
    cell_at_port = 0.01 / (2 ** lv["tiny"])
    assert cell_at_port * PORT_MIN_CELLS_ACROSS <= 0.006 + 1e-12


def test_pinhole_is_clamped_and_never_detonates_the_budget():
    lv = _port_levels(base_cell=0.01, default_level=2, smin=3,
                      port_sizes={"pinhole": 1e-5})
    assert lv["pinhole"] == 3 + 4          # smin + 4 ceiling


def test_previously_sealed_port_gets_one_more_level():
    base = _port_levels(base_cell=0.01, default_level=2, smin=6,
                        port_sizes={"p": 0.006})
    again = _port_levels(base_cell=0.01, default_level=2, smin=6,
                         port_sizes={"p": 0.006}, sealed_before={"p"})
    assert again["p"] == base["p"] + 1


def test_missing_or_zero_diameter_keeps_default_and_never_raises():
    lv = _port_levels(base_cell=0.01, default_level=2, smin=3,
                      port_sizes={"unknown": 0.0})
    assert lv["unknown"] == 2


def _render(tmp_path: Path, **kw):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet", "outlet_2": "outlet_2"}
    feats = {k: f"{k}.eMesh" for k in names}
    return ws, render_internal_case(
        ws, names=names, features=feats, interior_point=(0, 0, 0),
        bbox_min=(-0.1, -0.1, -0.1), bbox_max=(0.1, 0.1, 0.1),
        base_cell=0.01, surface_level=2, feature_level=3, n_layers=3,
        wall_key="wall", **kw)


def test_render_authors_per_port_levels_and_a_refinement_band(tmp_path):
    ws, summary = _render(tmp_path,
                          port_sizes={"inlet": 0.15, "outlet_2": 0.006})
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    # the big inlet keeps the default port level (smin-1 = 1); the small port rises to 4
    assert "inlet { level (1 1); patchInfo { type patch; } }" in text
    assert "outlet_2 { level (4 4); patchInfo { type patch; } }" in text
    # a distance-mode refinement band exists around the RAISED port only
    assert "outlet_2 { mode distance;" in text
    assert "inlet { mode distance;" not in text
    # the raised port's rim features snap at least as finely as its surface
    assert '{ file "outlet_2.eMesh"; level 4; }' in text
    # and the decision is visible in the summary the manifest carries
    assert summary["port_levels"] == {"inlet": 1, "outlet_2": 4}


def test_surplus_correction_steps_levels_down_when_the_clamp_refines_the_background(tmp_path):
    # The autopsied detonation: a 0.3 m part, planner surface_level 7 -> base_cell
    # inflated to 0.25 m; the min-8 division clamp makes the ACTUAL background finer
    # (~0.16 m), and keeping level 7 over-refined everything - every cell in the real
    # failure ended at level 8 and the budget died before the fluid volume was covered.
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet"}
    feats = {k: f"{k}.eMesh" for k in names}
    summary = render_internal_case(
        ws, names=names, features=feats, interior_point=(0, 0, 0),
        bbox_min=(-0.15, -0.15, -0.15), bbox_max=(0.15, 0.15, 0.15),
        base_cell=0.25, surface_level=7, feature_level=8, n_layers=3,
        wall_key="wall")
    assert summary["surface_level"] == [6, 6]          # 7 - surplus(1)
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    # the near-wall band is a few WALL cells deep, not most of the domain: the old
    # 3*base_cell rule authored a 0.75 m band here
    import re
    band = float(re.search(r"wall \{ mode distance; levels \(\((\S+) ", text).group(1))
    assert band < 0.05, f"near-wall band {band} m still domain-scale"


def test_no_clamp_means_no_correction(tmp_path):
    # a well-proportioned case: divisions land unclamped, levels pass through untouched
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet"}
    feats = {k: f"{k}.eMesh" for k in names}
    summary = render_internal_case(
        ws, names=names, features=feats, interior_point=(0, 0, 0),
        bbox_min=(-0.1, -0.1, -0.1), bbox_max=(0.1, 0.1, 0.1),
        base_cell=0.01, surface_level=2, feature_level=3, n_layers=3,
        wall_key="wall")
    assert summary["surface_level"] == [2, 2]


def test_seed_bubble_guarantees_a_fine_unambiguous_seed_cell(tmp_path):
    # The autopsied leak: the seed's cell was a BACKGROUND cell (~170 mm) that straddled
    # an outlet disc 15 mm away, so region-keep flooded outside and every submerged port
    # got zero faces. The renderer must author a refinement sphere at the seed.
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet"}
    feats = {k: f"{k}.eMesh" for k in names}
    render_internal_case(
        ws, names=names, features=feats, interior_point=(0.01, -0.03, 0.09),
        bbox_min=(-0.15, -0.15, -0.15), bbox_max=(0.15, 0.15, 0.15),
        base_cell=0.25, surface_level=7, feature_level=8, n_layers=3,
        wall_key="wall")
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "seedZone { type searchableSphere; centre (0.01 -0.03 0.09);" in text
    assert "seedZone { mode inside; levels ((1e15 6)); }" in text  # refined to wall level


def test_render_without_port_sizes_is_unchanged_legacy_behaviour(tmp_path):
    ws, summary = _render(tmp_path)
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "inlet { level (1 1); patchInfo { type patch; } }" in text
    assert "outlet_2 { level (1 1); patchInfo { type patch; } }" in text
    assert "mode distance" in text          # the wall near-band remains
    assert text.count("mode distance") == 1  # ... and is the ONLY distance region
    assert summary["port_levels"] == {}
