# Responsibility: Verify the far-field box is sized in the same length it is judged in.
#
# Seven corpus rotors blocked at domain_extent on every attempt: "requested 5L, mesh has 0.635L"
# in every direction, the same ratio everywhere. Not a cramped budget - two rulers. The planner's
# margins multiplied the STREAMWISE extent (a rotor's hub thickness, 0.117 m) while the gate
# judged them in the approved reference length (the diameter the user quoted, 0.924 m). One
# number, two units, a factor of eight apart. When a reference length is approved, the box is
# sized in it; a wing whose chord IS its streamwise extent is unchanged.
from __future__ import annotations

from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
from meshpipeline.engines.snappy.snappy_runner import domain_from_strategy

# rotor_like_002 as the pipeline saw it: hub 0.117 m along the +x flow, 0.924 m across
_ROTOR = {"bbox_min": [0.0, -0.462, -0.462], "bbox_max": [0.117, 0.462, 0.462], "L": 0.117,
          "extent": [0.117, 0.924, 0.924]}
_DIAMETER = 0.924
_REQUEST = {"upstream": 5.0, "downstream": 8.0, "lateral": 5.0, "vertical": 5.0}
_PLAN = {"domain_margin": {"up": 5, "down": 8, "side": 5, "vert": 5}}


def _manifest(dmin, dmax):
    n = "xyz"
    return {"geometry": {
        "domain_box": {f"{n[i]}min": dmin[i] for i in range(3)} | {f"{n[i]}max": dmax[i] for i in range(3)},
        "body_box": {f"{n[i]}min": _ROTOR["bbox_min"][i] for i in range(3)}
                    | {f"{n[i]}max": _ROTOR["bbox_max"][i] for i in range(3)}}}


def test_the_rotors_box_sized_in_the_approved_ruler_passes_the_gate_that_judges_in_it():
    dmin, dmax = domain_from_strategy(_ROTOR, _PLAN, None, flow_axis="+x", ruler_m=_DIAMETER)
    assert abs((0.0 - dmin[0]) - 5 * _DIAMETER) < 1e-9          # upstream: 5 diameters
    assert abs((dmax[0] - 0.117) - 8 * _DIAMETER) < 1e-9        # downstream: 8 diameters
    assert abs((-0.462 - dmin[1]) - 5 * _DIAMETER) < 1e-9       # lateral
    v = evaluate_domain_extents(_REQUEST, _DIAMETER, _manifest(dmin, dmax), flow_axis="+x")
    assert v.status == "pass", v.detail


def test_without_the_ruler_the_old_box_is_reproduced_and_the_gate_blocks_it():
    # the documented failure: sized in hub thickness, judged in diameters
    dmin, dmax = domain_from_strategy(_ROTOR, _PLAN, None, flow_axis="+x")
    assert abs((0.0 - dmin[0]) - 5 * 0.117) < 1e-9
    v = evaluate_domain_extents(_REQUEST, _DIAMETER, _manifest(dmin, dmax), flow_axis="+x")
    assert v.status == "block"
    assert "0.633L" in v.detail or "0.63" in v.detail, v.detail


def test_the_plans_own_reference_length_is_the_ruler_when_the_caller_passes_none():
    plan = {**_PLAN, "reference_length_m": _DIAMETER}
    dmin, _ = domain_from_strategy(_ROTOR, plan, None, flow_axis="+x")
    assert abs((0.0 - dmin[0]) - 5 * _DIAMETER) < 1e-9


def test_a_wing_whose_chord_is_its_streamwise_extent_is_unchanged():
    wing = {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [0.08, 0.06, 0.036], "L": 0.08,
            "extent": [0.08, 0.06, 0.036]}
    plan = {"domain_margin": {"up": 5, "down": 12, "side": 5, "vert": 5}}
    before = domain_from_strategy(wing, plan, None, flow_axis="+x")
    after = domain_from_strategy(wing, plan, None, flow_axis="+x", ruler_m=0.08)
    assert before == after


def test_the_legacy_no_axis_box_also_takes_the_ruler():
    dmin, _ = domain_from_strategy(_ROTOR, _PLAN, None, ruler_m=_DIAMETER)
    assert abs((0.0 - dmin[0]) - 5 * _DIAMETER) < 1e-9


def test_the_planner_is_briefed_in_the_same_unit():
    from meshpipeline.engines.snappy.planner import PLANNER_SYSTEM
    txt = PLANNER_SYSTEM
    assert "reference length" in txt and "domain_margin" in txt
    # the brief must no longer claim the ruler changes nothing about the box
    assert "changes nothing about how you size the box" not in txt
