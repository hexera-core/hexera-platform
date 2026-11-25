# Responsibility: Verify that the wall treatment said in words ("resolve the wall", "wall functions are fine") is the same ask as the y+ that builds it - met with it, never a second ask "not applied" - and that alone "resolve the wall" is the resolved-wall ask itself; a true extra ask beside them is still told.
"""m485w L5-L7 (dev-areen 975f3ed0): "water at 1 m/s, and resolve the wall - y+ around 1" got its y+ row Met and also
'Not applied: "resolve the wall" - no mesh setting of ours matches it yet', counted as "1 other thing ... could not be
applied"."""
from __future__ import annotations

import json

import pytest

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import notes_acted as NA

RESOLVED = {"fluid": "water", "fluid_words": "water", "speed_words": "1 m/s", "velocity_ms": 1.0, "wall": "resolved",
            "wall_said": True, "y_plus": 1.0}


def _read(notes: str, *, y_plus: tuple[float, str] | None = None, other: tuple[str, ...] = ()) -> dict:
    answer = {"stated_unit": "none", "sizes": [], "counts": [], "physics": [], "element_kind": [], "limits": [],
              "local": [], "layers": ([{"what": "y_plus", "value": y_plus[0], "unit": "none", "quote": y_plus[1]}]
                                      if y_plus else []),
              "other": [{"quote": q, "kind": "mesh"} for q in other]}
    req, dropped, na = MR.check(json.dumps(answer), notes, route="flow")
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model")


def _told(sec: dict) -> list[str]:
    return [w for w in MR.words(sec) if w.startswith(("Not applied", MR.NOT_MESH_LEAD))]


def _others(sec: dict) -> int:
    return max(0, len(MR.not_applied_words(sec)) - len(sec.get("dropped") or []))


@pytest.mark.parametrize("notes,yp,extra", [
    ("water at 1 m/s, and resolve the wall - y+ around 1", (1, "y+ around 1"), "resolve the wall"),
    ("y+ of 1, wall-resolved", (1, "y+ of 1"), "wall-resolved"),
    ("resolve the boundary layer; y+ about 1", (1, "y+ about 1"), "resolve the boundary layer"),
    ("no wall functions, y+ 1 please", (1, "y+ 1 please"), "no wall functions"),
])
def test_the_wall_treatment_and_its_y_plus_are_one_ask_met_together(notes, yp, extra):
    sec = NA.apply(_read(notes, y_plus=yp, other=(extra,)), notes, {"engine": "snappy", "topology": "internal",
                                                                    "conditions": RESOLVED})
    assert _told(sec) == [] and _others(sec) == 0
    assert [r["field"] for r in sec["requirements"]["rows"]] == ["layers:y_plus"]
    assert CR.summary({"rows": [{"met": True, "asked": "y+ of about 1 at the walls"}]}, not_applied=_others(sec)) == \
        "Of the 1 thing you asked for: 1 met."


def test_resolve_the_wall_alone_is_the_resolved_wall_ask():
    notes = "resolve the wall"
    sec = NA.apply(_read(notes, other=(notes,)), notes, {"engine": "snappy", "topology": "internal",
                                                         "conditions": {**RESOLVED, "wall": "functions",
                                                                        "wall_said": False}})
    assert _told(sec) == []
    assert sec["requirements"]["layers"]["y_plus"] == 1.0
    assert sec["requirements"]["physics"]["velocity_ms"] == 1.0 and sec["requirements"]["physics"]["fluid"] == "water"
    assert "From your notes: y+ of about 1 at the walls (a resolved wall)." in MR.words(sec)


def test_a_y_plus_of_1_alone_is_as_before():
    notes = "y+ of 1"
    sec = NA.apply(_read(notes, y_plus=(1, notes)), notes, {"engine": "snappy", "topology": "internal"})
    assert _told(sec) == [] and [r["field"] for r in sec["requirements"]["rows"]] == ["layers:y_plus"]


def test_wall_functions_are_fine_with_a_y_plus_of_30_is_one_ask():
    notes = "wall functions are fine, y+ 30 to 100"
    sec = NA.apply(_read(notes, y_plus=(30, "y+ 30 to 100"), other=("wall functions are fine",)), notes,
                   {"engine": "snappy", "topology": "internal"})
    assert _told(sec) == [] and _others(sec) == 0


def test_the_other_treatment_than_the_one_built_is_still_told():
    notes = "wall functions are fine, y+ of 1"
    sec = NA.apply(_read(notes, y_plus=(1, "y+ of 1"), other=("wall functions are fine",)), notes,
                   {"engine": "snappy", "topology": "internal"})
    assert _told(sec) == ['Not applied: "wall functions are fine" - no mesh setting of ours matches it yet.']


def test_a_true_extra_ask_beside_them_is_still_listed():
    notes = "resolve the wall, y+ around 1, and keep the cells cubic at the outlet"
    sec = NA.apply(_read(notes, y_plus=(1, "y+ around 1"), other=("resolve the wall", "keep the cells cubic at the outlet")),
                   notes, {"engine": "snappy", "topology": "internal", "conditions": RESOLVED})
    assert _told(sec) == ['Not applied: "keep the cells cubic at the outlet" - no mesh setting of ours matches it yet.']
    assert _others(sec) == 1


AIR = {"fluid": "air", "fluid_words": "air", "speed_words": "10 m/s", "velocity_ms": 10.0, "wall": "resolved",
       "wall_said": True, "y_plus": 1.0}


@pytest.mark.parametrize("notes,yq", [
    ("air at 10 m/s with a resolved boundary layer (y+ of 1)", "y+ of 1"),
    ("air at 10 m/s, wall-resolved, y+ about 1", "y+ about 1"),
    ("resolved boundary layer with y+ of 1 in air at 10 m/s", "y+ of 1"),
])
def test_conditions_wall_treatment_and_y_plus_in_one_breath_are_no_extra_ask(notes, yq):
    """m485w L6 (dev-areen 34689fa0): the reading set the whole sentence aside as one "other" thing; it is the
    confirmed conditions, the y+ row and the treatment that row builds - nothing more."""
    sec = _read(notes, y_plus=(1, yq), other=(notes,))
    out = NA.apply(sec, notes, {"conditions": AIR})
    assert _told(out) == [] and _others(out) == 0
    assert ((out["requirements"].get("layers") or {}).get("y_plus")) == 1


def test_one_breath_with_a_true_extra_ask_is_still_told():
    notes = "air at 10 m/s with a resolved boundary layer (y+ of 1) and cubic cells at the outlet"
    sec = _read(notes, y_plus=(1, "y+ of 1"), other=(notes,))
    out = NA.apply(sec, notes, {"conditions": AIR})
    assert _others(out) == 1


def test_the_reading_as_it_came_on_the_website_set_the_sentence_aside():
    notes = "air at 10 m/s with a resolved boundary layer (y+ of 1)"
    assert _others(_read(notes, y_plus=(1, "y+ of 1"), other=(notes,))) == 1


def test_one_breath_naming_the_other_treatment_is_not_taken_as_the_built_one():
    notes = "air at 10 m/s with wall functions (y+ of 1)"
    sec = _read(notes, y_plus=(1, "y+ of 1"), other=(notes,))
    assert NA._acts_together(notes, sec, {"conditions": AIR}, NA._covered(sec)) is False
