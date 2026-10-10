# Responsibility: Pin the external wake refinement: graded boxes behind the body along the declared flow, sized
# from the measured body, kept on the floor for a grounded body, held to its share of the cell budget (and saying
# what was cut), off when the plan says so - and rendered into the snappyHexMesh dictionary.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines import wake_region as W
from meshpipeline.engines.snappy import authoring
from meshpipeline.engines.snappy import snappy_runner as R

# a 1 m car-like body on z = 0, in a box 5 lengths up, 10 down, 5 to the side and above
BODY_MIN, BODY_MAX = [0.0, -0.2, 0.0], [1.0, 0.2, 0.3]
DOM_MIN, DOM_MAX = [-5.0, -5.2, -5.0], [11.0, 5.2, 5.3]
BIG = 50_000_000


def _plan(**kw):
    args = {"body_min": BODY_MIN, "body_max": BODY_MAX, "domain_min": DOM_MIN, "domain_max": DOM_MAX,
            "flow_axis": "+x", "base_cell": 0.2, "surface_level": 5, "budget_cells": BIG}
    args.update(kw)
    return W.plan_wake(**args)


class TestTheBoxesFollowTheFlow:
    def test_three_graded_boxes_one_level_apart_below_the_wall(self):
        p = _plan()
        assert [t.level for t in p.tiers] == [4, 3, 2]
        assert [round(t.cell_m, 6) for t in p.tiers] == [0.0125, 0.025, 0.05]

    def test_each_box_starts_at_the_trailing_extent_and_reaches_1_2_4_lengths(self):
        p = _plan()
        for t, reach in zip(p.tiers, (1.0, 2.0, 4.0)):
            assert t.box_min[0] == pytest.approx(1.0 - 0.1)       # a tenth of the body upstream
            assert t.box_max[0] == pytest.approx(1.0 + reach)
            assert t.reach_m == pytest.approx(reach)

    def test_the_boxes_nest_coarse_around_fine(self):
        p = _plan()
        for fine, coarse in zip(p.tiers, p.tiers[1:]):
            assert all(coarse.box_min[i] <= fine.box_min[i] for i in range(3))
            assert all(coarse.box_max[i] >= fine.box_max[i] for i in range(3))

    def test_the_cross_section_is_the_body_grown_modestly(self):
        t = _plan().tiers[0]
        grow = 0.1 * 0.4 + 0.05 * 1.0 + 2 * 0.0125
        assert t.box_min[1] == pytest.approx(-0.2 - grow)
        assert t.box_max[1] == pytest.approx(0.2 + grow)
        assert t.box_max[2] - BODY_MAX[2] < 0.15                  # not a slab of the whole domain

    @pytest.mark.parametrize("axis,sign_flow", [("-x", -1), ("+y", 1), ("-z", -1)])
    def test_the_declared_axis_and_sign_decide_downstream(self, axis, sign_flow):
        p = _plan(flow_axis=axis)
        i = "xyz".index(axis[1])
        t = p.tiers[-1]
        if sign_flow > 0:
            assert t.box_max[i] > BODY_MAX[i] and t.box_min[i] > BODY_MIN[i]
        else:
            assert t.box_min[i] < BODY_MIN[i] and t.box_max[i] < BODY_MAX[i]

    def test_an_undeclared_flow_goes_where_the_domain_has_most_room(self):
        p = _plan(flow_axis=None)
        assert (p.axis, p.sign) == (0, 1)
        assert p.tiers[0].box_max[0] > BODY_MAX[0]

    def test_the_boxes_are_clipped_to_the_domain(self):
        p = _plan(domain_max=[2.5, 5.2, 5.3])
        assert all(t.box_max[0] <= 2.5 for t in p.tiers)

    def test_a_grounded_body_keeps_its_wake_on_the_floor(self):
        floor = [-5.0, -5.2, 0.0]
        p = _plan(domain_min=floor, grounded=True)
        assert all(t.box_min[2] == 0.0 for t in p.tiers)

    def test_a_wing_wake_is_not_thinner_than_its_cells(self):
        p = _plan(body_min=[0.0, 0.0, -0.001], body_max=[1.0, 2.0, 0.001])
        for t in p.tiers:
            assert t.box_max[2] - t.box_min[2] > 4 * t.cell_m


class TestTheWakeLength:
    def test_the_stated_reference_length_when_it_is_the_largest(self):
        assert W.wake_scale(BODY_MIN, BODY_MAX, 0, 2.0) == 2.0

    def test_the_body_length_along_the_flow(self):
        assert W.wake_scale(BODY_MIN, BODY_MAX, 0, 0.1) == 1.0

    def test_a_disc_facing_the_flow_scales_with_its_diameter(self):
        # a rotor 57 mm along the flow, 340 x 324 mm across it
        assert W.wake_scale([0, 0, 0], [0.057, 0.34, 0.324], 0, 0.057) == pytest.approx(0.324)


class TestTheBudget:
    def test_the_estimate_stays_inside_its_share_of_the_budget(self):
        p = _plan(budget_cells=2_000_000)
        assert p.active and p.cells <= W.WAKE_BUDGET_SHARE * 2_000_000

    def test_over_budget_it_is_shortened_then_made_coarser_and_says_so(self):
        full = _plan()
        p = _plan(budget_cells=full.cells / W.WAKE_BUDGET_SHARE / 20)
        assert p.active and p.cells <= p.allowance
        assert p.changes and p.changes[0].startswith("shortened")
        assert any("coarser" in c for c in p.changes)
        assert "cell budget" in p.summary() and "it was shortened" in p.summary()
        assert p.record()["reduced"] == p.changes

    def test_no_room_at_all_drops_it_and_says_why(self):
        p = _plan(budget_cells=100)
        assert not p.active and "budget" in p.reason
        assert p.summary().startswith("no wake refinement")

    def test_a_wall_one_level_above_the_background_needs_no_wake_box(self):
        p = _plan(surface_level=1)
        assert not p.active and p.reason


class TestTheKnob:
    def test_false_turns_it_off(self):
        p = _plan(knobs=False)
        assert not p.active and p.reason == "the plan asked for none"

    def test_zero_length_turns_it_off(self):
        assert not _plan(knobs={"length": 0}).active

    def test_the_knobs_size_it(self):
        p = _plan(knobs={"length": 8, "levels_below_surface": 2, "tiers": 2})
        assert [t.level for t in p.tiers] == [3, 2]
        assert p.tiers[-1].reach_m == pytest.approx(8.0)

    def test_fewer_levels_than_tiers_still_reaches_the_whole_length(self):
        p = _plan(surface_level=2)               # only level 1 is left for the wake
        assert [t.level for t in p.tiers] == [1]
        assert p.tiers[0].reach_m == pytest.approx(4.0)

    def test_a_bad_value_keeps_its_default_in_the_renderer(self):
        assert W.wake_knobs({"tiers": 99, "length": 2})["tiers"] == W.WAKE_DEFAULTS["tiers"]

    @pytest.mark.parametrize("raw,path", [
        ("yes", "wake"), ({"lenght": 3}, "wake.lenght"), ({"length": -1}, "wake.length"),
        ({"tiers": 0}, "wake.tiers"), ({"levels_below_surface": 0}, "wake.levels_below_surface"),
        ({"enabled": "no"}, "wake.enabled"), ({"start": 3}, "wake.start")])
    def test_the_authoring_diagnostics_name_the_bad_field(self, raw, path):
        diags = authoring.validate({"wake": raw})
        assert [d.path for d in diags if d.severity == "error"] == [path]

    @pytest.mark.parametrize("raw", [True, False, {"length": 6, "levels_below_surface": 1},
                                     {"enabled": False}])
    def test_the_palette_accepts_a_good_wake(self, raw):
        assert authoring.validate({"wake": raw}) == []
        assert "wake" in authoring.AUTHORING_TOOL["function"]["parameters"]["properties"]


# the dictionary the renderer writes

ANALYSIS = {"bbox_min": BODY_MIN, "bbox_max": BODY_MAX, "L": 1.0, "extent": [1.0, 0.4, 0.3]}
STRATEGY = {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}, "max_cells": 4_000_000}
REC = {"base_cell": 0.1, "surface_level": [4, 4], "afford_level": 4, "feature_level": 5,
       "distance_bands": [(0.2, 3), (0.8, 2)], "resolve_feature_angle": 30}


def _render(tmp_path, wake, *, ground=None):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    dmin, dmax = R.domain_from_strategy(ANALYSIS, STRATEGY, flow_axis="+x", ground=bool(ground))
    summary = R.render_snappy_case(
        tmp_path, surface_name="car", feature_file="car.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=dmin, domain_max=dmax, strategy=STRATEGY,
        ground=ground, wake=wake)
    return summary, (tmp_path / "system" / "snappyHexMeshDict").read_text()


class TestTheDictionary:
    def test_the_boxes_are_geometry_and_refinement_regions(self, tmp_path):
        summary, d = _render(tmp_path, W.WakeRequest(flow_axis="+x"))
        n = len(summary["wake"]["tiers"])
        assert n == 3
        for k, t in enumerate(summary["wake"]["tiers"]):
            assert f"wakeTier{k} {{ type searchableBox;" in d
            assert f"wakeTier{k} {{ mode inside; levels ((1e15 {t['level']})); }}" in d
        # every wake level sits below the wall's
        assert max(t["level"] for t in summary["wake"]["tiers"]) < summary["surface_level"][1]
        assert json.loads((tmp_path / R.WAKE_FACT).read_text()) == summary["wake"]
        assert "wake refined" in summary["wake_note"]

    def test_no_request_writes_the_historical_dictionary(self, tmp_path):
        summary, d = _render(tmp_path, None)
        assert "wakeTier" not in d and "wake" not in summary
        assert not (tmp_path / R.WAKE_FACT).exists()

    def test_a_declined_wake_draws_no_box_and_records_that(self, tmp_path):
        _render(tmp_path, W.WakeRequest(flow_axis="+x"))
        summary, d = _render(tmp_path, W.WakeRequest(knobs=False, flow_axis="+x"))
        assert "wakeTier" not in d
        assert summary["wake"]["enabled"] is False
        assert json.loads((tmp_path / R.WAKE_FACT).read_text())["tiers"] == []

    def test_a_grounded_car_wake_sits_on_the_floor(self, tmp_path):
        summary, _d = _render(tmp_path, W.WakeRequest(flow_axis="+x"), ground="ground")
        assert all(t["box_min"][2] == pytest.approx(0.0) for t in summary["wake"]["tiers"])


class TestTheDriverKeepsTheKnob:
    def test_a_revised_plan_that_drops_the_wake_inherits_it(self, tmp_path):
        from meshpipeline.engines.snappy.drivers import _inherit_durable_plan_fields
        (tmp_path / "attempt_1").mkdir()
        (tmp_path / "attempt_1" / ".last_plan.json").write_text(json.dumps({"wake": False}))
        (tmp_path / "attempt_2").mkdir()
        out = _inherit_durable_plan_fields({"n_layers": 3}, tmp_path / "attempt_2")
        assert out["wake"] is False


class TestTheBodyKeepsItsShare:
    def test_the_body_estimate_caps_what_the_wake_may_spend(self):
        p = _plan(budget_cells=4_000_000, body_cells=3_800_000)
        assert p.allowance == pytest.approx(200_000)
        assert p.cells <= 200_000
        assert "the body itself needs about 3.8 M" in p.summary()

    def test_a_body_that_fills_the_budget_gets_no_wake_and_is_told_why(self):
        p = _plan(budget_cells=2_000_000, body_cells=2_400_000)
        assert not p.active
        assert "raise max_cells" in p.reason

    def test_the_castellated_estimate_counts_background_surface_and_bands(self):
        est = R.castellated_estimate(area=1.0, divisions=[10, 10, 10], base_cell=1.0,
                                     surface_level=2, near=(0.5, 2), far=(1.0, 1))
        surface = R.CAST_SURFACE_CELLS * 1.0 / 0.25 ** 2
        bands = R.CAST_BAND_GRADING * (0.5 / 0.25 ** 3 + 0.5 / 0.5 ** 3)
        assert est == pytest.approx(1000 + surface + bands)

    def test_no_wall_area_no_estimate(self):
        assert R.castellated_estimate(area=None, divisions=[1, 1, 1], base_cell=1.0,
                                      surface_level=1, near=(1, 1), far=(1, 1)) is None


class TestTheReviewerIsTold:
    def test_an_authored_wake_is_stated_with_its_reach_and_cells(self, tmp_path):
        from meshpipeline.agents.reviewer.context import wake_refinement_line
        summary, _d = _render(tmp_path, W.WakeRequest(flow_axis="+x"))
        line = wake_refinement_line(summary["wake"])
        assert "3 graded boxes" in line and "+x" in line and "AUTHORED" in line

    def test_no_wake_is_stated_with_the_reason(self):
        from meshpipeline.agents.reviewer.context import wake_refinement_line
        line = wake_refinement_line({"enabled": False, "tiers": [], "reason": "the plan asked for none"})
        assert line.startswith("Wake refinement: NONE") and "asked for none" in line
