# Responsibility: Pin the typed domain-extent evaluation: measured with the APPROVED ruler,
# tri-state (na / unmeasured / pass / miss / block), one-sided (over-delivery is never a
# caveat), floored (under half of asked, or touching the body, is a block - not a near-miss),
# and every caveat a machine-measured comparison.
from __future__ import annotations

from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents

# the heat-sink specimen: body 80x60x36 mm, flow +Y, ruler 0.06 m (user-stated),
# margins over-spec except downstream 6.67L vs 8L asked
HEAT_SINK_MANIFEST = {
    "geometry": {
        "domain_box": {"xmin": -0.4, "xmax": 0.48, "ymin": -0.4, "ymax": 0.46,
                       "zmin": -0.4, "zmax": 0.436},
        "body_bbox": {"xmin": 0.0, "xmax": 0.08, "ymin": 0.0, "ymax": 0.06,
                      "zmin": 0.0, "zmax": 0.036},
    },
}
ASKED = {"upstream": 5.0, "downstream": 8.0, "lateral": 5.0}


class TestTheHeatSinkSpecimen:
    def test_the_downstream_nearmiss_is_a_single_measured_caveat(self):
        v = evaluate_domain_extents(ASKED, 0.06, HEAT_SINK_MANIFEST)
        assert v.status == "miss"
        assert len(v.caveats) == 1
        c = v.caveats[0]
        assert c["direction"] == "downstream"
        assert c["requested"] == 8.0
        assert abs(c["measured"] - 6.67) < 0.01
        assert c["ruler_m"] == 0.06
        # NOTE: upstream/lateral measured ~6.67L against 5 asked - over-delivery, no caveat

    def test_within_tolerance_is_a_clean_pass(self):
        v = evaluate_domain_extents({"downstream": 7.0}, 0.06, HEAT_SINK_MANIFEST)
        assert v.status == "pass" and v.caveats == []

    def test_overdelivery_is_never_a_caveat(self):
        v = evaluate_domain_extents({"upstream": 5.0, "lateral": 5.0}, 0.06,
                                    HEAT_SINK_MANIFEST)
        assert v.status == "pass" and v.caveats == []


class TestTheFloor:
    def test_under_half_of_asked_is_a_block_not_a_nearmiss(self):
        # downstream measured 6.67L; asking 14 makes that under half
        v = evaluate_domain_extents({"downstream": 14.0}, 0.06, HEAT_SINK_MANIFEST)
        assert v.status == "block"
        assert "downstream" in v.detail and "half" in v.detail.lower()

    def test_a_box_touching_the_body_is_always_a_block(self):
        clipped = {"geometry": {
            "domain_box": {"xmin": 0.0, "xmax": 0.48, "ymin": -0.4, "ymax": 0.46,
                           "zmin": -0.4, "zmax": 0.436},   # xmin == body xmin
            "body_bbox": HEAT_SINK_MANIFEST["geometry"]["body_bbox"],
        }}
        v = evaluate_domain_extents({"upstream": 5.0}, 0.06, clipped)
        assert v.status == "block"


class TestMeasurementHonesty:
    def test_no_typed_request_is_not_applicable(self):
        v = evaluate_domain_extents(None, None, HEAT_SINK_MANIFEST)
        assert v.status == "na" and v.caveats == []

    def test_a_manifest_without_a_box_is_unmeasured_never_a_silent_pass(self):
        # the old gate logged 'inconclusive, passing' here - the hole that let a lost ruler
        # deliver an unmeasured mesh caveat-free
        v = evaluate_domain_extents(ASKED, 0.06, {"geometry": {}})
        assert v.status == "unmeasured"

    def test_the_typed_ruler_wins_over_whatever_the_manifest_claims(self):
        # the manifest carries the WRONG ruler (the catch-4 failure shape); measurement must
        # use the approved 0.06, not the manifest's 0.08
        m = {"geometry": {**HEAT_SINK_MANIFEST["geometry"], "reference_length": 0.08}}
        v = evaluate_domain_extents({"downstream": 8.0}, 0.06, m)
        assert v.status == "miss"
        assert abs(v.caveats[0]["measured"] - 6.67) < 0.01

    def test_vertical_is_measured_when_asked(self):
        v = evaluate_domain_extents({"vertical": 12.0}, 0.06, HEAT_SINK_MANIFEST)
        assert v.status == "miss"
        assert v.caveats[0]["direction"] == "vertical"
