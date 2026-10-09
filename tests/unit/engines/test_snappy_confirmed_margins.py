"""The far-field margins the user confirmed are the margins snappy's box is built with.

An F1 front wing confirmed at 5 upstream / 10 downstream reference lengths (flow -z) was planned
by the model at 5 / 5; the box shipped 0.645 m short of the wake room the user asked for.
"""
from meshpipeline.engines.snappy import snappy_runner as R
from meshpipeline.engines.snappy.drivers import confirmed_margins

_WING = {"bbox_min": [0.0, 0.0, 0.5225], "bbox_max": [0.213, 0.039, 0.6515], "L": 0.129}
_PLAN = {"domain_margin": {"up": 5, "down": 5, "side": 5, "vert": 5}, "n_layers": 3}
_CONFIRMED = {"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5}
_REF = 0.129


def test_the_confirmed_downstream_replaces_the_planners():
    s = confirmed_margins(_PLAN, _CONFIRMED, _REF)
    assert s["domain_margin"] == {"up": 5.0, "down": 10.0, "side": 5.0, "vert": 5.0}
    assert s["n_layers"] == 3                     # the rest of the plan is untouched
    assert _PLAN["domain_margin"]["down"] == 5    # and the planner's dict is not mutated


def test_the_box_has_ten_reference_lengths_behind_a_minus_z_wing():
    s = confirmed_margins(_PLAN, _CONFIRMED, _REF)
    dmin, dmax = R.domain_from_strategy(_WING, s, None, flow_axis="-z", ruler_m=_REF)
    # flow along -z: downstream is BELOW z_min, upstream is ABOVE z_max
    assert abs((_WING["bbox_min"][2] - dmin[2]) - 10 * _REF) < 1e-9
    assert abs((dmax[2] - _WING["bbox_max"][2]) - 5 * _REF) < 1e-9


def test_unstated_directions_keep_the_planners_sizing():
    plan = {"domain_margin": {"up": 4, "down": 12, "side": 3, "vert": 6}}
    s = confirmed_margins(plan, {"downstream": 8}, _REF)
    assert s["domain_margin"] == {"up": 4, "down": 8.0, "side": 3, "vert": 6}


def test_lateral_alone_also_sets_the_vertical_room():
    s = confirmed_margins(_PLAN, {"lateral": 7}, _REF)
    assert s["domain_margin"]["side"] == 7.0 and s["domain_margin"]["vert"] == 7.0


def test_without_a_reference_length_the_numbers_have_no_unit_and_the_plan_stands():
    assert confirmed_margins(_PLAN, _CONFIRMED, None) is _PLAN
    assert confirmed_margins(_PLAN, None, _REF) is _PLAN
    assert confirmed_margins(_PLAN, {"downstream": None, "upstream": "x"}, _REF) is _PLAN
