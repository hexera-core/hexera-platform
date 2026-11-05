# Responsibility: Pin the declared flow axis end to end: the box builder orients its margins
# along the DECLARED axis (sign included), and the extent gate measures upstream/downstream
# along that same axis - so a +Y-flow part can never again pass with its wake room on X.
from __future__ import annotations

from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents

# run 12's REAL delivered box: flow declared +Y, asked 12L downstream, planner put the big
# margin on X (16L) and only 6.67L on +Y. The old gate passed it; the axis-aware gate must not.
RUN12_MANIFEST = {
    "geometry": {
        "domain_box": {"xmin": -0.4, "xmax": 1.04, "ymin": -0.4, "ymax": 0.46,
                       "zmin": -0.4, "zmax": 0.436},
        "body_bbox": {"xmin": 0.0, "xmax": 0.08, "ymin": 0.0, "ymax": 0.06,
                      "zmin": 0.0, "zmax": 0.036},
    },
}


class TestTheGateMeasuresAlongTheDeclaredAxis:
    def test_run12s_wrong_way_box_is_caught(self):
        v = evaluate_domain_extents({"downstream": 12.0}, 0.06, RUN12_MANIFEST,
                                    flow_axis="+y")
        # (0.46 - 0.06) / 0.06 = 6.67L along +Y: above half of 12, below tolerance -> caveat
        assert v.status == "miss"
        assert v.caveats[0]["direction"] == "downstream"
        assert abs(v.caveats[0]["measured"] - 6.67) < 0.01

    def test_the_same_box_passes_when_flow_really_is_along_x(self):
        v = evaluate_domain_extents({"downstream": 12.0}, 0.06, RUN12_MANIFEST,
                                    flow_axis="+x")
        assert v.status == "pass"          # 16L on +X: over-delivered

    def test_negative_flow_swaps_the_sides(self):
        # flow -y: downstream is the ymin side (6.67L), upstream is ymax (6.67L)
        v = evaluate_domain_extents({"upstream": 5.0, "downstream": 5.0}, 0.06,
                                    RUN12_MANIFEST, flow_axis="-y")
        assert v.status == "pass"          # both 6.67 >= 5

    def test_lateral_and_vertical_are_the_remaining_axes(self):
        # flow +y: lateral = x, vertical = z. Ask a huge lateral: x margins are 6.67 and 16
        # -> min 6.67 against 20 asked = under half -> block
        v = evaluate_domain_extents({"lateral": 20.0}, 0.06, RUN12_MANIFEST, flow_axis="+y")
        assert v.status == "block"

    def test_omitting_the_axis_keeps_the_legacy_x_convention(self):
        v = evaluate_domain_extents({"downstream": 12.0}, 0.06, RUN12_MANIFEST)
        assert v.status == "pass"          # exactly the old behaviour, documented


class TestTheBoxBuilderOrientsByTheDeclaredAxis:
    ANALYSIS = {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [0.08, 0.06, 0.036], "L": 0.08,
                "extent": [0.08, 0.06, 0.036]}

    def _box(self, flow_axis):
        from meshpipeline.engines.snappy.snappy_runner import domain_from_strategy
        strategy = {"domain_margin": {"up": 5, "down": 12, "side": 5, "vert": 5}}
        return domain_from_strategy(self.ANALYSIS, strategy, None, flow_axis=flow_axis)

    def test_plus_y_flow_puts_the_wake_room_on_ymax(self):
        dmin, dmax = self._box("+y")
        ref = 0.06                                    # streamwise extent along y
        assert abs((dmax[1] - 0.06) - 12 * ref) < 1e-9   # downstream on +y
        assert abs((0.0 - dmin[1]) - 5 * ref) < 1e-9     # upstream on -y side

    def test_minus_x_flow_puts_the_wake_room_on_xmin(self):
        dmin, dmax = self._box("-x")
        ref = 0.08
        assert abs((0.0 - dmin[0]) - 12 * ref) < 1e-9    # downstream on the -x side
        assert abs((dmax[0] - 0.08) - 5 * ref) < 1e-9    # upstream on +x

    def test_no_axis_keeps_todays_box_exactly(self):
        from meshpipeline.engines.snappy.snappy_runner import domain_from_strategy
        strategy = {"domain_margin": {"up": 5, "down": 12, "side": 5, "vert": 5}}
        legacy = domain_from_strategy(self.ANALYSIS, strategy, None)
        with_kwarg = domain_from_strategy(self.ANALYSIS, strategy, None, flow_axis=None)
        assert legacy == with_kwarg


class TestIntakeRequiresTheAxisWithExtents:
    def _errs(self, **over):
        from meshpipeline.agents.intake.validation import validate_submission
        base = {
            "domain": "external aero", "engine_params": {},
            "request_txt": "A complete requirements summary. " * 8,
            "review_brief_txt": "Acceptance criteria for the mesh. " * 8,
            "mesh_engine": "snappy", "mesh_fidelity": "standard",
            "engine_source": "user_direct", "purpose": "external_cfd",
            "input_kind": "body-surface", "dimensionality": "3D",
            "patches": [{"name": "body", "type": "wall"},
                        {"name": "farfield", "type": "farfield"}],
        }
        base.update(over)
        return validate_submission(base)

    def test_extents_without_a_flow_axis_ask_for_the_direction(self):
        e = self._errs(requested_extents={"downstream": 8}, reference_length_m=0.06)
        assert any("flow_axis" in x for x in e), e

    def test_a_full_declaration_passes(self):
        e = self._errs(requested_extents={"downstream": 8}, reference_length_m=0.06,
                       flow_axis="+y")
        assert not any("flow_axis" in x or "extent" in x.lower() for x in e), e

    def test_a_bogus_axis_is_refused(self):
        e = self._errs(requested_extents={"downstream": 8}, reference_length_m=0.06,
                       flow_axis="north")
        assert any("flow_axis" in x for x in e), e
