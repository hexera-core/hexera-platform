# Responsibility: Verify INS-10 - an inlet or outlet the intake model sends without a location takes the location the geometry check measured and the user confirmed (the same at the preview and the submission), so a run switched to snappyHexMesh is never asked "the rough location of each opening" the stage already has; and NOTE-13's ai_remark_kept, which keeps an AI step's own remark off a result whose asks the rules decided.
# Boundaries: pure over plain data and a temporary workspace; no store, no model.
from __future__ import annotations

import json

from meshpipeline.agents.intake.validation import _validate_internal_ports
from meshpipeline.application.geometry_confirmation import with_locations
from meshpipeline.engines import mesh_requirements as MR

# bend_elbow_004 as the stage confirmed it: two openings of one size
LOCATED = [{"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0]},
           {"name": "outlet", "type": "outlet", "near_mm": [657.0, 660.0, 0.0]}]


def _ports(**extra):
    return [{"name": "inlet", "type": "inlet", "diameter_mm": 214.0, **extra},
            {"name": "outlet", "type": "outlet", "diameter_mm": 214.0},
            {"name": "wall", "type": "wall"}]


def test_two_ports_of_one_size_take_the_measured_locations_and_are_not_asked():
    sent = _ports()
    assert any("rough location" in e for e in _validate_internal_ports(sent))          # the question it asked
    got = with_locations(sent, LOCATED)
    assert [p.get("near_mm") for p in got] == [[0.0, 0.0, 0.0], [657.0, 660.0, 0.0], None]
    assert _validate_internal_ports(got) == []
    # the same ports in, the same out: the preview's token binds the submission
    assert with_locations(_ports(), LOCATED) == got


def test_a_location_sent_is_kept_and_nothing_is_filled_without_a_confirmation():
    own = _ports(near_mm=[1.0, 2.0, 3.0])
    assert with_locations(own, LOCATED)[0]["near_mm"] == [1.0, 2.0, 3.0]
    sent = _ports()
    assert with_locations(sent, []) is sent


def test_a_renamed_port_takes_the_only_opening_of_its_role_and_never_a_guess():
    renamed = [{"name": "p_1", "type": "inlet", "diameter_mm": 214.0},
               {"name": "p_2", "type": "outlet", "diameter_mm": 214.0}]
    got = with_locations(renamed, LOCATED)
    assert got[0]["near_mm"] == [0.0, 0.0, 0.0] and got[1]["near_mm"] == [657.0, 660.0, 0.0]
    # two inlets sent, one confirmed: which one it is cannot be told - nothing filled
    two = [{"name": "a", "type": "inlet", "diameter_mm": 50.0}, {"name": "b", "type": "inlet", "diameter_mm": 50.0}]
    assert all("near_mm" not in p for p in with_locations(two, LOCATED))
    # a name that is another role's opening is never given its location
    crossed = [{"name": "outlet", "type": "inlet", "diameter_mm": 214.0}]
    assert "near_mm" not in with_locations(crossed, LOCATED)[0]


def test_an_ai_remark_is_kept_only_when_the_rules_decided_no_ask(tmp_path):
    assert MR.ai_remark_kept(tmp_path) is True                                       # no reading: as before
    answer = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
              "element_kind": [], "limits": [], "other": [],
              "controls": [{"key": "cells_across", "value": 20, "choice": "", "unit": "none", "where": "",
                            "strict": False, "quote": "at least 20 cells across the bore"}]}
    req, dropped, na = MR.check(json.dumps(answer), "at least 20 cells across the bore", route="flow")
    MR.write(tmp_path, MR.section("used", requirements=req, dropped=dropped, not_applied=na))
    assert MR.decided_asks(MR.read(tmp_path)) and MR.ai_remark_kept(tmp_path) is False


def test_a_centroid_at_negative_zero_fingerprints_as_the_database_keeps_it():
    """The approved intent is fingerprinted before it is stored as JSONB, which keeps no -0.0: a port at (657, 660, -0)
    read back as 0 and the run was refused at dispatch (m483 round 10). The sign of a zero is no part of the intent."""
    from meshpipeline.agents.intake import admission_token as at
    assert at.fingerprint({"patches": [{"near_mm": [657.0, 660.0, -0.0]}]}) ==         at.fingerprint({"patches": [{"near_mm": [657.0, 660.0, 0.0]}]})
    assert at.fingerprint({"x": [1.0]}) != at.fingerprint({"x": [-1.0]})
