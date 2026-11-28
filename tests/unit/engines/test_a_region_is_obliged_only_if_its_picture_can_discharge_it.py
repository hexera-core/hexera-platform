# Responsibility: a region is demanded only when its own pictures can really discharge it.
# Boundaries: `cfmesh_conjugate/spec._cuts_that_can_be_shown`.
# Collaborates with: render_adapter.expand_region_targets, which decides what can be OFFERED.
"""The obligation was built by a weaker test than the one that discharges it.

`_cuts_that_can_be_shown` named a region if any frame that spoke said `shows_structure`. The home
side discharges a region on `review_session.ResolvedCut.counts_cells`, which ends at
`wall_in_cells(bare_run_min_px, drawn_cell_px) >= WALL_CELLS_MIN`. A frame can show a perfectly
regular structure and contain no metal at all.

MEASURED, job 40e4e578's own mesh_manifest.json:

    frame                      shows_structure   bare_run_min_px   wall_cells   counts_cells
    across_the_flow   zoom     True              0.0               0.00         False
    through_the_plate zoom     True              30.0              7.50         True
    along_the_flow    zoom     False             300.0             75.00        False

So `across_the_flow` was NAMED here and refused there. `render_adapter` would not offer it,
`visual.py` found an obliged id undiscovered, and the review ended `reviewer_evidence_missing`
before the model was asked anything - a marker that gets no rerun. Jobs 40e4e578, 606af55e,
b8f09ae9 and 110daca9 all delivered a mesh, a bundle and a viewer with
`review_execution: failed_to_complete` and `reviewer_verdict: None`.

Replayed through the production path on that manifest, the obliged set is now
`['region:through_the_plate']`, which is exactly what `expand_region_targets` offers with NO
volume at all - so the refusal is unreachable in the worst case, not merely papered over by the
slab that `native._all_declared_planes` now always writes.

WHAT IS DELIBERATELY NOT CLOSED. The home side's other bars - its ink floor, cells on the best
row, row count and drawn-against-claimed pitch agreement - are re-measured from its own copy of
the PNG and cannot be predicted from the producer's manifest. The wall is the one bar whose figure
the producer publishes, so it is the one this side can honestly apply. The asymmetry is narrowed,
not eliminated, and the docstring says so.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate.spec import _cuts_that_can_be_shown
from meshpipeline.sandbox.picture_structure import WALL_CELLS_MIN, wall_in_cells

CELL_PX = 4
#: (shows_structure, bare_run_min_px) of each zoom, exactly as job 40e4e578 published them.
REAL = {
    "across_the_flow": (True, 0.0),
    "through_the_plate": (True, 30.0),
    "along_the_flow": (False, 300.0),
}


def region(name: str, *, shows: bool, bare: float, cell_px=CELL_PX, content=True) -> dict:
    zoom: dict = {"scope": "zoom", "feature_m": 2.51578e-05, "feature_pixels": cell_px,
                  "window_m": [0.0041802, 0.0031351]}
    if content:
        zoom["content"] = {"shows_structure": shows, "bare_run_min_px": bare,
                           "bare_run_px": bare}
    whole = {"scope": "whole", "window_m": [0.17751, 0.13313],
             "content": {"shows_structure": False, "bare_run_min_px": 0.0}}
    return {"name": name, "plane_axes": [0, 2], "frames": [whole, zoom]}


def manifest(*regions) -> dict:
    return {"inspection_regions": list(regions)}


# ------------------------------------------------------------------ the real plate

def test_the_plate_obliges_only_the_cut_whose_picture_holds_metal():
    """THE REGRESSION. This used to also name across_the_flow, which nothing could discharge."""
    got = _cuts_that_can_be_shown(manifest(*(region(n, shows=s, bare=b)
                                             for n, (s, b) in REAL.items())))
    assert got == ["through_the_plate"]


def test_the_structured_frame_with_no_metal_is_not_obliged():
    assert _cuts_that_can_be_shown(
        manifest(region("across_the_flow", shows=True, bare=0.0))) == []


def test_the_structured_frame_with_metal_is_obliged():
    assert wall_in_cells(30.0, CELL_PX) == 7.5 >= WALL_CELLS_MIN
    assert _cuts_that_can_be_shown(
        manifest(region("through_the_plate", shows=True, bare=30.0))) == ["through_the_plate"]


def test_an_unstructured_frame_is_not_obliged_however_much_metal():
    """along_the_flow has 75 cells of wall and shows nothing periodic. The veto still vetoes."""
    assert _cuts_that_can_be_shown(
        manifest(region("along_the_flow", shows=False, bare=300.0))) == []


def test_the_floor_is_the_readers_own_and_inclusive():
    for bare, obliged in ((2.0 * CELL_PX, True), (2.0 * CELL_PX - 0.1, False)):
        got = _cuts_that_can_be_shown(manifest(region("r", shows=True, bare=bare)))
        assert bool(got) is obliged, f"{bare} px of wall: obliged={bool(got)}"


# ------------------------------------------------------------------ silence still grants

def test_a_region_where_NO_frame_spoke_is_named_exactly_as_before():
    """An older mesher wrote windows and no measurement at all. Nothing has refused those, and
    dropping them would silently stop naming them.

    EVERY frame has to be silent for this arm, which is what `not spoke` means. A region whose
    zoom carries no content but whose whole frame says `shows_structure: False` has SPOKEN, and
    was not named before this change either - the first draft of this test built that case by
    accident and asserted the opposite.
    """
    silent = {"name": "r", "plane_axes": [0, 2], "frames": [
        {"scope": "whole", "window_m": [0.17751, 0.13313]},
        {"scope": "zoom", "window_m": [0.0041802, 0.0031351], "feature_m": 2.51578e-05}]}
    assert _cuts_that_can_be_shown(manifest(silent)) == ["r"], \
        "a region with no measurement at all stopped being named"


def test_a_region_whose_only_speaking_frame_refused_is_not_named():
    """And this is the case the first draft built: the zoom never spoke, the whole frame did and
    refused. One measured False is a veto, exactly as before."""
    mixed = {"name": "r", "plane_axes": [0, 2], "frames": [
        {"scope": "whole", "window_m": [0.17751, 0.13313],
         "content": {"shows_structure": False, "bare_run_min_px": 0.0}},
        {"scope": "zoom", "window_m": [0.0041802, 0.0031351], "feature_m": 2.51578e-05}]}
    assert _cuts_that_can_be_shown(manifest(mixed)) == []


def test_a_region_with_no_frames_is_named_exactly_as_before():
    """The geometry-derived shape, with no pictures beside it."""
    assert _cuts_that_can_be_shown({"inspection_regions": [{"name": "r"}]}) == ["r"]
    assert _cuts_that_can_be_shown({"inspection_regions": [{"name": "r", "frames": []}]}) == ["r"]


def test_an_unmeasured_cell_does_not_grant():
    """Without the drawn cell there is nothing to express the wall in. `wall_in_cells` returns 0.0
    for it, and a frame that SPOKE is judged on what it said."""
    assert _cuts_that_can_be_shown(
        manifest(region("r", shows=True, bare=30.0, cell_px=None))) == []
    assert _cuts_that_can_be_shown(
        manifest(region("r", shows=True, bare=30.0, cell_px=0))) == []


# ------------------------------------------------------------------ shape and junk

def test_a_region_with_no_name_is_skipped():
    assert _cuts_that_can_be_shown(
        {"inspection_regions": [{"frames": []}, {"name": "", "frames": []}]}) == []


@pytest.mark.parametrize("junk", [None, {}, {"inspection_regions": None},
                                  {"inspection_regions": []},
                                  {"inspection_regions": ["not a dict", 7]}])
def test_junk_names_nothing(junk):
    assert _cuts_that_can_be_shown(junk) == []


def test_any_one_frame_that_discharges_is_enough():
    """The test is `any`, over the frames that spoke - a useless whole frame beside a good zoom is
    exactly what through_the_plate is."""
    r = region("r", shows=True, bare=30.0)
    assert (r["frames"][0]["content"]["shows_structure"], r["frames"][1]["content"]
            ["shows_structure"]) == (False, True)
    assert _cuts_that_can_be_shown(manifest(r)) == ["r"]


# ------------------------------------------------------------------ the two sides agree

def test_the_obliged_set_is_offerable_with_no_volume_at_all():
    """THE POINT OF THE WHOLE CHANGE. `expand_region_targets` offers a region when the volume is
    available OR one of its frames counts cells. With the obligation drawn on the same wall bar,
    every obliged region is offerable even when the interior is gone entirely - so
    [EXPECTED_TARGET_MISSING] cannot fire on this manifest in the worst case."""
    from meshpipeline.sandbox import render_adapter as RA

    obliged = _cuts_that_can_be_shown(manifest(*(region(n, shows=s, bare=b)
                                                 for n, (s, b) in REAL.items())))

    class _R:
        def __init__(self, rid):
            self.region_id = rid

    class _C:
        def __init__(self, counts):
            self.counts_cells = counts

    # what the home side measured on those same three, through ArtifactResolver
    counts = {"across_the_flow": (False, False), "through_the_plate": (False, True),
              "along_the_flow": (False, False)}
    got = RA.expand_region_targets({n: _R(n) for n in counts}, purpose="count cells",
                                   required=True, volume_available=False,
                                   cuts={n: tuple(_C(c) for c in f) for n, f in counts.items()})
    offered = {t.target_id for t in got}
    assert {f"region:{n}" for n in obliged} <= offered, \
        f"obliged {obliged} is not offerable without a volume: offered {sorted(offered)}"
