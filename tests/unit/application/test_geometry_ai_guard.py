# Responsibility: Verify the geometry check's guard rails on the naming model: the measured reading
# comes first, and the model's answer changes it only where it is readable, surer than the code and
# not against what was measured. A bad answer never fails the naming; the code's names ship.
# Boundaries: pure functions, a stood-in object store and a stood-in model router; no CAD, no
# rendering, no provider.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.application import geometry_check as gc
from meshpipeline.contracts import model_inference, object_storage


def _pipe(**over) -> dict:
    """A pipe elbow as the scout measures it: a wall round a way through, two openings."""
    facts = {
        "body_kind": "pipe_wall", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [1448.8, 1233.4, 539.3], "seed_point_mm": [119.7, 86.7, 0.0],
        "confidence": {"input_kind": 0.6, "openings": 0.72}, "notes": [],
        "openings": [
            {"id": 1, "name": "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 466.2,
             "centroid_mm": [0.0, 0.0, 0.0], "normal": [-1, 0, 0], "confidence": 0.85},
            {"id": 2, "name": "outlet", "role": "outlet", "shape": "circle", "diameter_mm": 466.2,
             "centroid_mm": [1197.1, 867.2, 0.0], "normal": [0.36, 0.93, 0], "confidence": 0.6},
        ],
        "inside": {"rule": "INS-1", "has_inside": True, "passages": 1, "mouths_per_passage": [2],
                   "side_pockets": 0, "cell_mm": 5.0, "enclosed_mm3": 0.0},
    }
    facts.update(over)
    return facts


def _body(**over) -> dict:
    """A closed solid the scout read as a body in a flow: no inside, no way through."""
    facts = _pipe(body_kind="single_solid", input_kind="solid-body", flow="external", seed_point_mm=None,
                  inside={"rule": "INS-1", "has_inside": False, "passages": 0, "mouths_per_passage": [],
                          "side_pockets": 0, "cell_mm": 5.0, "enclosed_mm3": 0.0})
    facts.update(over)
    return facts


class _Router:
    """The model router, stood in: it answers name_geometry with the raw arguments given."""

    def __init__(self, arguments: str):
        self.arguments = arguments

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="", **_):
        return SimpleNamespace(tool_calls=[SimpleNamespace(name=tools[0]["function"]["name"],
                                                           arguments=self.arguments)])


MALFORMED = [
    {"part": "elbow", "flow": "internal", "input_kind": "body-surface", "confidence": "high",
     "openings": [{"id": 1, "name": "outlet", "role": "outlet", "confidence": "very"}]},
    {"part": "elbow", "confidence": 0.9, "openings": "1 is the inlet, 2 the outlet"},
    {"part": "elbow", "confidence": 0.9, "openings": ["inlet", None, 3, {"id": "two", "role": "inlet"}]},
    {"part": "elbow", "confidence": float("nan"), "openings": [{"id": {"n": 1}, "role": "inlet"}],
     "real_length_m": "about a metre", "flow": 7, "input_kind": ["solid-body"]},
    {"part": ["a", "list"], "openings": [{"id": 1.5, "role": "outlet", "confidence": 1.0}], "notes": 42},
    ["not", "a", "dict"],
    "just words",
    None,
]


# ------------------------------------------------------------------ a malformed answer ----
@pytest.mark.parametrize("answer", MALFORMED)
def test_a_malformed_answer_never_raises_and_the_codes_names_ship(answer, monkeypatch):
    monkeypatch.setattr(model_inference, "_router", _Router(json.dumps(answer)))
    vision = gc._name_with_vision(_pipe(), [], purpose_text="water through an elbow", session_id="s1",
                                  owner_id="o1")
    assert isinstance(vision, dict)
    p = gc._proposal(_pipe(), vision)
    assert [(o["id"], o["name"], o["role"]) for o in p["openings"]] == [(1, "inlet", "inlet"), (2, "outlet", "outlet")]
    assert (p["input_kind"], p["flow"]) == ("body-surface", "internal")
    # the raw answer, unchecked (a replay from the decision cache, an old stored record), is as harmless
    raw = gc._proposal(_pipe(), answer) if isinstance(answer, dict) else gc._proposal(_pipe(), {"error": "x"})
    assert [o["role"] for o in raw["openings"]] == ["inlet", "outlet"]


def test_an_answer_that_is_not_json_is_no_answer(monkeypatch):
    monkeypatch.setattr(model_inference, "_router", _Router("{not json"))
    vision = gc._name_with_vision(_pipe(), [], purpose_text="", session_id="s1", owner_id="o1")
    assert "error" in vision
    assert gc._proposal(_pipe(), vision)["vision_available"] is False


def test_a_string_sticker_number_is_read_as_the_number_and_a_made_up_one_is_dropped():
    clean = gc._sanitize_vision({"part": "elbow", "confidence": "0.9", "openings": [
        {"id": "2", "name": "exit", "role": "OUTLET", "confidence": 0.95},
        {"id": 9, "name": "ghost", "role": "inlet", "confidence": 1.0},
        {"id": 2.0, "name": "dup", "role": "inlet", "confidence": 1.0}]}, _pipe())
    assert clean["confidence"] == 0.9
    assert clean["openings"] == [{"id": 2, "name": "exit", "role": "outlet", "confidence": 0.95}]


def test_the_model_cannot_say_where_the_flow_direction_came_from():
    clean = gc._sanitize_vision({"part": "car", "flow_axis": "+y", "flow_from": "the user's words"}, _body())
    assert "flow_from" not in clean


class _Store:
    def __init__(self, objects: dict):
        self.objects = dict(objects)

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def download_file(self, *, object_key, destination):
        Path(destination).write_bytes(self.get_bytes(object_key=object_key))


@pytest.mark.parametrize("answer", MALFORMED[:5])
def test_a_malformed_answer_never_fails_the_naming_stage(answer, monkeypatch):
    sid = "abcdef12-aiguard"
    scouted = {"status": "scouted", "session_id": sid, "written_at": 1.0, "named": False,
               "facts": _pipe(), "proposal": gc._proposal(_pipe(), None), "snapshots": []}
    store = _Store({gc.check_object_key(sid, "scout.json"): json.dumps(scouted).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(model_inference, "_router", _Router(json.dumps(answer)))
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="water through an elbow")
    assert result["status"] == "ready"
    assert [o["role"] for o in result["proposal"]["openings"]] == ["inlet", "outlet"]


def test_a_bad_up_answer_is_no_answer(tmp_path, monkeypatch):
    sheet = tmp_path / "upright.png"
    sheet.write_bytes(b"png")
    for args in ("[1, 2]", '{"panel": "A", "confidence": NaN}', '{"panel": 3}'):
        monkeypatch.setattr(model_inference, "_router", _Router(args))
        got = gc._up_with_vision(sheet, ["+z", "-z", "+y", "-y", "+x", "-x"], part="", purpose_text="",
                                 session_id="s1", owner_id="o1")
        assert isinstance(got, dict) and got.get("up_confidence", 0.0) == 0.0
    assert gc.decide_up("external", None, {"up_axis": "-z", "up_confidence": "sure"})["up_axis"] == "+z"
    assert gc.decide_up("external", None, {"up_axis": "-z", "up_confidence": float("nan")})["up_axis"] == "+z"


# --------------------------------------------------------- the kind and flow, measured ----
def test_the_model_cannot_call_a_part_with_no_inside_a_passage():
    vision = {"part": "a pipe", "flow": "internal", "input_kind": "body-surface", "confidence": 1.0, "openings": []}
    p = gc._proposal(_body(), vision)
    assert (p["input_kind"], p["flow"]) == ("solid-body", "external")
    assert any("measured reading stands" in n for n in p["notes"])


def test_the_model_cannot_call_a_part_with_one_opening_a_passage():
    facts = _body(inside=None, openings=_pipe()["openings"][:1])
    vision = {"part": "a tank", "flow": "internal", "input_kind": "fluid-domain", "confidence": 1.0, "openings": []}
    assert gc._proposal(facts, vision)["flow"] == "external"


def test_the_model_cannot_call_a_part_with_a_way_through_a_body_in_a_flow():
    vision = {"part": "a car", "flow": "external", "input_kind": "solid-body", "confidence": 1.0, "openings": []}
    p = gc._proposal(_pipe(), vision)
    assert (p["input_kind"], p["flow"]) == ("body-surface", "internal")


def test_the_model_must_be_clearly_surer_than_the_code():
    facts = _body(inside=None)            # nothing measured against it: only the margin decides
    vision = {"part": "fluid", "flow": "internal", "input_kind": "fluid-domain", "openings": []}
    assert gc._proposal(facts, dict(vision, confidence=0.7))["flow"] == "external"     # 0.6 + 0.15 is 0.75
    assert gc._proposal(facts, dict(vision, confidence=0.8))["flow"] == "internal"


def test_a_kind_and_flow_that_disagree_with_each_other_change_nothing():
    vision = {"part": "x", "flow": "external", "input_kind": "body-surface", "confidence": 1.0, "openings": []}
    p = gc._proposal(_body(inside=None), dict(vision, flow="internal", input_kind="solid-body"))
    assert (p["input_kind"], p["flow"]) == ("solid-body", "external")


# ------------------------------------------------------------------ the openings' roles ----
def test_a_role_the_model_is_less_sure_of_than_the_code_is_ignored():
    vision = {"part": "elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 1, "name": "outlet_b", "role": "outlet", "confidence": 0.7},
                           {"id": 2, "name": "exhaust", "role": "outlet", "confidence": 0.7}]}
    by_id = {o["id"]: o for o in gc._proposal(_pipe(), vision)["openings"]}
    assert (by_id[1]["role"], by_id[1]["name"]) == ("inlet", "inlet")       # code 0.85 > 0.7: kept, name too
    assert (by_id[2]["role"], by_id[2]["name"]) == ("outlet", "exhaust")    # agrees: the name is the model's
    assert by_id[2]["confidence"] == 0.7


def test_a_swap_that_would_leave_two_inlets_keeps_the_measured_roles():
    vision = {"part": "elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 2, "name": "inlet_2", "role": "inlet", "confidence": 0.7}]}
    p = gc._proposal(_pipe(), vision)                                     # code 0.6 < 0.7, but no outlet left
    assert [o["role"] for o in p["openings"]] == ["inlet", "outlet"]


def test_a_part_the_fluid_flows_through_never_loses_its_inlet_and_outlet():
    vision = {"part": "elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 1, "name": "bolt", "role": "not_an_opening", "confidence": 0.99},
                           {"id": 2, "name": "bolt_2", "role": "not_an_opening", "confidence": 0.99}]}
    p = gc._proposal(_pipe(), vision)
    assert [(o["name"], o["role"]) for o in p["openings"]] == [("inlet", "inlet"), ("outlet", "outlet")]
    assert any("measured roles stand" in n for n in p["notes"])


def test_a_surer_model_still_names_and_swaps_the_openings():
    vision = {"part": "elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 1, "name": "outlet", "role": "outlet", "confidence": 0.9},
                           {"id": 2, "name": "inlet", "role": "inlet", "confidence": 0.9}]}
    by_id = {o["id"]: o for o in gc._proposal(_pipe(), vision)["openings"]}
    assert (by_id[1]["role"], by_id[2]["role"]) == ("outlet", "inlet")


def test_a_body_in_a_flow_may_have_every_sticker_set_aside():
    vision = {"part": "a car", "flow": "external", "input_kind": "solid-body", "confidence": 0.9,
              "openings": [{"id": 1, "name": "x", "role": "not_an_opening", "confidence": 0.9},
                           {"id": 2, "name": "y", "role": "not_an_opening", "confidence": 0.9}]}
    p = gc._proposal(_body(), vision)
    assert [o["role"] for o in p["openings"]] == ["not_an_opening", "not_an_opening"]


# ----------------------------------------------------------- the flow direction's source ----
class _View:
    def __init__(self, name, direction, up=(0.0, 0.0, 1.0)):
        self.name, self.direction, self.up, self.facing = name, direction, up, []


def test_a_direction_the_user_never_stated_is_not_the_users_words():
    a = gc._flow_from_nose({"flow_axis": "+y"}, [], "a car body, air flows around it")
    assert a["flow_axis"] == "+y" and a["flow_from"] == "the pictures"
    # the nose, when there is one, says more than a direction nobody stated
    top = _View("top", (0.0, 0.0, -1.0), up=(0.0, 1.0, 0.0))
    a = gc._flow_from_nose({"nose_view": "top", "nose_side": "left", "flow_axis": "+y"}, [top], "a car body")
    assert a["flow_axis"] == "+x" and a["flow_from"] == "the nose"


def test_a_direction_the_user_did_state_is_their_words():
    for words in ("flow along +y", "the air comes in the +Y direction", "positive y", "along y",
                  "y-direction flow", "flow towards Y+"):
        assert gc._flow_from_nose({"flow_axis": "+y"}, [], words)["flow_from"] == "the user's words", words
    assert gc._flow_from_nose({"flow_axis": "-y"}, [], "along y")["flow_from"] == "the pictures"
    assert gc._flow_from_nose({"flow_axis": "+z"}, [], "+z is up")["flow_from"] == "the pictures"
    assert gc.user_stated_flow_axes("flow along -X, +z up") == {"-x"}


def test_a_made_up_direction_is_weighed_against_a_sure_shape_axis():
    facts = _body(inside=None, openings=[], flow_axis_guess="+x",
                  flow_axis_reading={"rule": "along", "axes": ["x"], "confidence": 0.9,
                                     "reason": "the blades turn about x"})
    vision = {"part": "a rotor", "flow": "external", "input_kind": "solid-body", "confidence": 0.9,
              "openings": [], "flow_axis": "+y", "flow_from": "the pictures"}
    p = gc._proposal(facts, vision)
    assert p["flow_axis"][1] == "x"
    said = dict(vision, flow_from="the user's words")
    assert gc._proposal(facts, said)["flow_axis"] == "+y"
