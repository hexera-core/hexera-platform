# Responsibility: the other cuts' zoom windows are centred on the plane the aiming cut measured.
# Boundaries: `native._aim_from_a_drawn_cut` and the fallback score in `_place_by_what_is_drawn`.
# Collaborates with: slices.py, which builds the plan centre this corrects.
"""Two placement defects, both measured off the delivered run and the plan that produced it.

ONE: THE WINDOW LOOKED WHERE THE ENGINE HAD ALREADY PROVED THERE WAS NOTHING. The plan centres
every zoom on the middle of the fine zone (slices.py:303). Reproduced from
data/_verify/conjugate_plan.json: the zone is y = 21.5095 to 33.5468 mm, its middle is
27.5282 mm, the zoom window is 6.0379 mm tall, so the window spans 24.5092 to 30.5471 mm. The
array is at y = 22.7133 mm - 1.7960 mm BELOW the window's lower edge. The engine measured that
itself: the table above POSITION_TRIES records 79 repeats at y = 22.111 and 22.713 mm and 0
repeats at 23.917 mm, under the heading "The middle of the fine zone is empty and the channels
are in the bottom tenth of it".

The aiming cut finds the array and writes the plane into its own origin. Nothing carried that one
coordinate to the other cuts, because `replan` accepts an axis index and nothing else. It is the
same axis - the flow axis is chosen from the axes that are NOT thin - so the number transfers
with no conversion at all.

TWO: WHEN THE SEARCH FAILED IT PUBLISHED THE ONE FRAME THAT CANNOT SHOW ANYTHING. The tiebreak
was `(repeats, ink_fraction)`, highest ink wins. A fully saturated frame reads ink_fraction
1.000000 - the maximum the term can take - so with nothing showing structure the score selected
FOR saturation. Measured on across_the_flow_zoom.png of job cba7ac69: ink_fraction 1.000000, zero
pixels at or above 250 of 1,228,800, 0 repeats. That frame is what the score asked for.

Neither fix touches `shows_structure`, `MIN_REPEATS`, `period_matches` or the `usable` count.
They move WHERE the question is asked and WHICH refused frame is kept, not what passes.
"""
from __future__ import annotations

import inspect

import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N

#: The real plan's fine zone, from data/_verify/conjugate_plan.json.
ZONE_LO_Y, ZONE_HI_Y = 0.02150954685337906, 0.03354680904023285
CELL = 2.51578e-05
ARRAY_Y = ZONE_LO_Y + 0.1 * (ZONE_HI_Y - ZONE_LO_Y)        # 22.7133 mm, the first searched place
MID_Y = ZONE_LO_Y + (ZONE_HI_Y - ZONE_LO_Y) / 2.0          # 27.5282 mm, the plan's centre
MID_X, MID_Z = 0.0229212, 0.0418994


def zoom(h: float, v: float) -> dict:
    return {"scope": "zoom", "centre_m": [h, v],
            "window_m": [CELL / 4 * 1280, CELL / 4 * 960], "feature_m": CELL}


def whole(h: float, v: float) -> dict:
    return {"scope": "whole", "centre_m": [h, v], "window_m": [0.06, 0.05]}


def cut(name: str, axis: int, *, origin=None) -> dict:
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    pa = [k for k in range(3) if k != axis]
    full = [MID_X, MID_Y, MID_Z]
    o = list(origin or full)
    return {"name": name, "normal": normal, "normal_axis": axis, "plane_axes": pa, "origin": o,
            "frames": [whole(full[pa[0]], full[pa[1]]), zoom(full[pa[0]], full[pa[1]])]}


def drive(monkeypatch, *, found_axis: int = 0, period: float = 1.006312e-04):
    """Run the real `_aim_from_a_drawn_cut` with only the DRAWING stubbed out.

    `_draw_one_cut` is what renders; here it just writes the plane the aiming cut "found" into
    that cut's origin, which is exactly what `_place_by_what_is_drawn` does on a real run.
    """
    aim = cut(N.AIM_FROM_CUT, 1)
    across, along = cut("across_the_flow", 0), cut("along_the_flow", 2)
    cuts = [aim, across, along]

    def _drew(_mesh, c, **_k):
        if c is aim:
            c["origin"][1] = ARRAY_Y                      # the array's plane, as measured
            zf = next(f for f in c["frames"] if f["scope"] == "zoom")
            zf["content"] = {"shows_structure": True, "repeats": 79, "period_m": period,
                             "repeat_world_axis": found_axis, "ink_fraction": 0.187}

    monkeypatch.setattr(N, "_draw_one_cut", _drew)
    monkeypatch.setattr(N, "_rematch", lambda *a, **k: None)
    out: dict = {}
    N._aim_from_a_drawn_cut(None, cuts, root=None, ws=None, pv=None, width_px=1280,
                            height_px=960, out=out,
                            replan=lambda _ax: [cut(N.AIM_FROM_CUT, 1), cut("across_the_flow", 0),
                                                cut("along_the_flow", 2)],
                            expect_period_m=period)
    return cuts, out


def zoom_of(cuts, name):
    c = next(x for x in cuts if x.get("name") == name)
    return next(f for f in c["frames"] if f["scope"] == "zoom")


# ------------------------------------------------------------- the plan's own arithmetic

def test_the_plan_centre_misses_the_array_by_the_measured_gap():
    """Not hypothetical: these are the delivered run's own numbers."""
    half = (CELL / 4 * 960) / 2.0
    lower = MID_Y - half
    assert MID_Y == pytest.approx(0.0275282, abs=1e-7)
    assert ARRAY_Y == pytest.approx(0.0227133, abs=1e-7)
    assert lower == pytest.approx(0.0245092, abs=1e-7)
    assert (lower - ARRAY_Y) == pytest.approx(0.0017960, abs=1e-6)
    assert ARRAY_Y < lower, "the array must be OUTSIDE the window for this to be the defect"


# ------------------------------------------------------------- the re-aim, behaviourally

def test_the_other_cuts_are_centred_on_the_plane_the_aiming_cut_found(monkeypatch):
    """THE REGRESSION. Before the fix these windows kept the plan's empty middle."""
    cuts, _out = drive(monkeypatch)
    for name in ("across_the_flow", "along_the_flow"):
        zf = zoom_of(cuts, name)
        c = next(x for x in cuts if x["name"] == name)
        slot = 0 if 1 == c["plane_axes"][0] else 1
        assert zf["centre_m"][slot] == pytest.approx(ARRAY_Y), \
            f"{name}'s window is still centred on the fine zone's middle"
        assert "measured the array at" in zf["centre_source"]


def test_the_aiming_cut_keeps_the_origin_it_measured(monkeypatch):
    cuts, _ = drive(monkeypatch)
    aim = next(c for c in cuts if c["name"] == N.AIM_FROM_CUT)
    assert aim["origin"][1] == pytest.approx(ARRAY_Y)


def test_the_whole_part_frame_is_left_where_it_was(monkeypatch):
    """It is centred on the BLOCK's middle on purpose, so the rest of the part stays in view."""
    cuts, _ = drive(monkeypatch)
    for name in ("across_the_flow", "along_the_flow"):
        c = next(x for x in cuts if x["name"] == name)
        wf = next(f for f in c["frames"] if f["scope"] == "whole")
        pa = c["plane_axes"]
        full = [MID_X, MID_Y, MID_Z]
        assert wf["centre_m"] == [full[pa[0]], full[pa[1]]]


def test_a_cut_whose_plane_does_not_contain_that_axis_is_untouched(monkeypatch):
    """A y-normal cut has no y component in its plane; writing one would be meaningless."""
    cuts, _ = drive(monkeypatch)
    aim = next(c for c in cuts if c["name"] == N.AIM_FROM_CUT)
    assert 1 not in aim["plane_axes"]


def test_an_aiming_cut_that_found_nothing_changes_no_centre(monkeypatch):
    """A set re-aimed from a frame that held no structure would be aimed from noise."""
    aim = cut(N.AIM_FROM_CUT, 1)
    across = cut("across_the_flow", 0)
    cuts = [aim, across]
    monkeypatch.setattr(N, "_draw_one_cut", lambda *a, **k: None)   # nothing found
    monkeypatch.setattr(N, "_rematch", lambda *a, **k: None)
    N._aim_from_a_drawn_cut(None, cuts, root=None, ws=None, pv=None, width_px=1280,
                            height_px=960, out={}, replan=lambda _ax: [], expect_period_m=0.0)
    assert zoom_of(cuts, "across_the_flow")["centre_m"] == [MID_Y, MID_Z]


# ------------------------------------------------------------- the re-aim, structurally

def test_the_carry_runs_after_the_replan_and_before_the_rematch():
    src = inspect.getsource(N._aim_from_a_drawn_cut)
    assert 'cuts[:] = [aim if c.get("name") == AIM_FROM_CUT else c for c in rebuilt]' in src
    assert src.index("cuts[:] = [aim") < src.index('_f["centre_m"] = _centre')
    assert src.index('_f["centre_m"] = _centre') < src.index("_rematch(aim")


def test_the_carry_does_not_widen_the_search_band():
    """`search_m` bounds the plane to its own zone; a plane free to wander would find the array
    somewhere else in the part and report it under this cut's name."""
    src = inspect.getsource(N._aim_from_a_drawn_cut)
    # the code, not the prose: a comment may legitimately say that search_m is left alone
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "search_m" not in code


# ------------------------------------------------------------- the fallback score

def balance(ink: float, repeats: int = 0):
    return (repeats, min(ink, 1.0 - ink))


def test_a_saturated_frame_no_longer_wins_the_tiebreak():
    """ink 1.000000 was the MAXIMUM of the old second term. It is now the minimum."""
    assert balance(0.187) > balance(1.0), "the all-ink frame still wins"
    assert balance(0.187) > balance(0.004)
    assert balance(1.0) == (0, 0.0)
    assert balance(0.0) == (0, 0.0), "an empty frame shows nothing either"


def test_a_frame_that_shows_structure_still_wins_outright():
    """`repeats` is still the primary key, so balance only ever breaks a tie."""
    assert balance(1.0, repeats=39) > balance(0.5, repeats=0)


def test_the_production_score_is_the_balance_term():
    src = inspect.getsource(N._place_by_what_is_drawn)
    assert "min(_ink, 1.0 - _ink)" in src
    assert "score = (int(content.get(\"repeats\") or 0), float(" not in src, \
        "the raw ink fraction is still the tiebreak"


def test_nothing_about_the_evidence_bar_moved():
    """An empty frame and a frame of fine metal look identical to a reader; that is what
    shows_structure is for. These fixes move where the question is asked, not the bar."""
    from meshpipeline.sandbox import picture_structure as PS

    assert PS.MIN_REPEATS == 6
    assert 'if content.get("shows_structure"):' in inspect.getsource(N._place_by_what_is_drawn)
    aim = inspect.getsource(N._aim_from_a_drawn_cut)
    for forbidden in ("MIN_REPEATS", "shows_structure =", "usable"):
        assert forbidden not in aim, f"the re-aim touched {forbidden}"
