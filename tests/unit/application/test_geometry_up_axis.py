# Responsibility: Verify how the geometry check decides which way is up - the code's reading of
# the shape weighed against the model's pick from the six-way picture, the shape first and +z
# whenever that is unsure - and carries it through: onto the proposal and its notes, through the
# flow guess and the ground, into the words the intake reads, onto the stored pictures' cameras,
# and back out for the views.
# Boundaries: pure functions, a stood-in object store and a stood-in model router; no CAD, no
# rendering, no provider.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.api.v1.geometry import ConfirmIn, confirmation_message
from meshpipeline.application import geometry_check as gc
from meshpipeline.application.geometry_confirmation import body_of
from meshpipeline.cad.up_axis import turn
from meshpipeline.contracts import geometry_fields as gf
from meshpipeline.contracts import model_inference, object_storage

LEGS_AT_PLUS_Z = {"axis": "-z", "confidence": 0.8,
                  "reason": "it stands on 4 feet (wheels, struts or legs) at its +z end"}
SILENT = {"axis": None, "confidence": 0.0, "reason": "nothing in its shape says which end it stands on"}
ORDER = list(gf.UP_AXES)                      # panel A is +z, B is -z, ... as the scout draws them


def _car_facts(**over) -> dict:
    """The SAE notchback's facts as the scout stores them: a body in a flow, drawn upside down."""
    facts = {"body_kind": "single_solid", "input_kind": "solid-body", "flow": "external",
             "size_mm": [840.0, 320.0, 290.8], "confidence": {"input_kind": 0.6, "openings": 0.0},
             "notes": [], "openings": [], "flow_axis_guess": "+x", "up_evidence": dict(LEGS_AT_PLUS_Z)}
    facts.update(over)
    return facts


def _vision(**over) -> dict:
    answer = {"part": "a car body", "flow": "external", "input_kind": "solid-body", "openings": [],
              "confidence": 0.8}
    answer.update(over)
    return answer


def _picked(axis: str, conf: float) -> dict:
    return {"up_axis": axis, "up_confidence": conf, "up_from": f"in the pictures it looks the right way up with {axis} up"}


# ------------------------------------------------------------- the six-way picture question ----
def test_the_panel_the_model_picks_is_the_axis_that_panel_was_drawn_with():
    assert gc._upright_answer({"panel": "B", "confidence": 0.95}, ORDER) == {
        "up_axis": "-z", "up_confidence": 0.95, "up_panel": "B",
        "up_from": "in the pictures it looks the right way up with -z up"}
    assert gc._upright_answer({"panel": "a", "confidence": 2}, ORDER)["up_confidence"] == 1.0
    assert gc._upright_answer({"panel": "F", "confidence": 0.9}, ["-x", "+x", "-y", "+y", "-z", "+z"])["up_axis"] == "+z"


def test_none_or_a_panel_that_was_not_drawn_says_nothing():
    assert gc._upright_answer({"panel": "none", "confidence": 1.0}, ORDER) == {}
    assert gc._upright_answer({"panel": "G", "confidence": 1.0}, ORDER) == {}
    assert gc._upright_answer({"panel": "E", "confidence": 1.0}, ["+z", "-z"]) == {}
    assert gc._upright_answer({"panel": "A", "confidence": "sure"}, ORDER)["up_confidence"] == 0.0


def test_the_model_is_told_what_the_part_was_named_and_what_the_user_said():
    words = gc._up_words("car body", "the SAE notchback reference body, air flows around it")
    assert "named from its other pictures: car body" in words and "SAE notchback" in words
    assert words.endswith("Which panel shows it the right way up?")
    assert gc._up_words("", "") == "Which panel shows it the right way up?"


def test_the_question_is_only_for_things_with_an_obvious_top():
    props = gc.UP_TOOL["function"]["parameters"]["properties"]
    assert props["panel"]["enum"] == ["A", "B", "C", "D", "E", "F", "none"]
    for thing in ("a car", "a whole aircraft", "a building"):
        assert thing in gc._UP_SYSTEM
    for none in ("a wing on its own", "a nacelle", "a pipe", "a rotor", "answer none"):
        assert none in gc._UP_SYSTEM


class _Router:
    """The model router the runtime installs, stood in: it answers with one tool call, or fails."""

    def __init__(self, arguments: dict | None = None, fail: Exception | None = None):
        self.arguments, self.fail, self.messages = arguments, fail, None

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="", **_):
        self.messages = messages
        if self.fail:
            raise self.fail
        calls = [SimpleNamespace(name=tools[0]["function"]["name"], arguments=json.dumps(self.arguments))]
        return SimpleNamespace(tool_calls=calls)


def test_the_six_way_question_goes_through_the_router_and_is_read_back(tmp_path, monkeypatch):
    sheet = tmp_path / "upright.png"
    sheet.write_bytes(b"\x89PNG not really")
    router = _Router({"panel": "B", "confidence": 0.95, "why": "the struts belong under the car"})
    monkeypatch.setattr(model_inference, "_router", router)
    got = gc._up_with_vision(sheet, ORDER, part="car body", purpose_text="the SAE car", session_id="s1", owner_id="o1")
    assert got["up_axis"] == "-z" and got["up_confidence"] == 0.95
    system, user = router.messages
    assert system["content"] == gc._UP_SYSTEM
    assert user["content"][0]["text"].startswith("The part was named from its other pictures: car body.")
    assert user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_model_that_is_down_costs_the_answer_never_the_check(tmp_path, monkeypatch):
    sheet = tmp_path / "upright.png"
    sheet.write_bytes(b"png")
    monkeypatch.setattr(model_inference, "_router", _Router(fail=RuntimeError("provider down")))
    assert gc._up_with_vision(sheet, ORDER, part="", purpose_text="", session_id="s1", owner_id="o1") == {
        "error": "RuntimeError"}


def test_the_panels_are_turned_the_way_the_console_turns_its_camera():
    """Each turn is a proper rotation taking +Z to the chosen axis - the console's own table - so
    the panel the model picks is the view the user then opens on."""
    from meshpipeline.render.scout_snapshots import signed_axis

    for axis in gf.UP_AXES:
        assert signed_axis(turn(axis, (0.0, 0.0, 1.0))) == axis
    assert turn("-z", (-1.0, -1.0, 0.8)) == (-1.0, 1.0, -0.8)
    assert turn("+y", (1.0, 2.0, 3.0)) == (1.0, 3.0, -2.0)


# --------------------------------------------------------------------------- the decision ----
def test_the_shape_and_the_pictures_agreeing_turn_the_part_surely():
    up = gc.decide_up("external", LEGS_AT_PLUS_Z, _picked("-z", 0.95))
    assert up["up_axis"] == "-z" and up["up_axis_confidence"] == 0.99
    assert up["up_axis_from"] == "it stands on 4 feet (wheels, struts or legs) at its +z end, and the pictures agree"


def test_feet_or_a_scene_outrank_a_picture_read_the_other_way():
    """The Ahmed body lying on its side, -y up: the shape finds its four stilts at its +y end, the
    model took them for a roof rack and picked +y. The measured contact decides, less sure, and
    says so."""
    stilts = {"axis": "-y", "confidence": 0.8, "reason": "it stands on 4 feet (wheels, struts or legs) at its +y end"}
    up = gc.decide_up("external", stilts, _picked("+y", 0.95))
    assert up["up_axis"] == "-y" and up["up_axis_confidence"] == 0.6
    assert "(in the pictures it looked +y up)" in up["up_axis_from"]
    assert gc.decide_up("external", LEGS_AT_PLUS_Z, None)["up_axis"] == "-z"
    assert gc.decide_up("external", LEGS_AT_PLUS_Z, {"error": "TimeoutError"})["up_axis"] == "-z"


def test_the_model_alone_turns_the_part_only_when_it_is_sure():
    assert gc.decide_up("external", SILENT, _picked("-z", 0.95))["up_axis"] == "-z"
    assert gc.decide_up("external", SILENT, _picked("-z", 0.7))["up_axis"] == "+z"
    assert gc.decide_up("external", None, {}) == {"up_axis": "+z", "up_axis_confidence": 0.0,
                                                  "up_axis_from": "as drawn: nothing says otherwise"}


def test_a_weak_shape_reading_neither_turns_the_part_nor_stops_a_sure_model():
    weak = {"axis": "-z", "confidence": 0.5, "reason": "x"}
    assert gc.decide_up("external", weak, None)["up_axis"] == "+z"
    assert gc.decide_up("external", weak, _picked("+y", 0.9))["up_axis"] == "+y"


def test_a_part_the_fluid_flows_through_is_shown_as_drawn_whatever_is_read():
    """A pipe elbow has no up that changes its mesh: +z, the file as drawn."""
    up = gc.decide_up("internal", LEGS_AT_PLUS_Z, _picked("-z", 1.0))
    assert up["up_axis"] == "+z" and "flows through" in up["up_axis_from"]


# ------------------------------------------------------------------------- the proposal ----
def test_the_scout_alone_proposes_the_shapes_up_and_says_so_in_the_notes():
    p = gc._proposal(_car_facts(), None)
    assert p["up_axis"] == "-z" and p["up_axis_confidence"] == 0.8
    assert "Up is -Z: it stands on 4 feet (wheels, struts or legs) at its +z end." in p["notes"]
    assert p["flow_axis"] == "+x"                              # the longest side across -z


def test_the_named_proposal_weighs_the_pictures_too():
    p = gc._proposal(_car_facts(), _vision(**_picked("-z", 0.95)))
    assert p["up_axis"] == "-z" and p["up_axis_confidence"] == 0.99
    p = gc._proposal(_car_facts(up_evidence=SILENT), _vision(**_picked("+y", 0.9)))
    assert p["up_axis"] == "+y"
    assert p["flow_axis"] == "+x" and p["reference_length_mm"] == 840.0   # across +y: x (840) beats z (290.8)


def test_an_upright_part_has_no_note_and_keeps_its_ground_reading():
    p = gc._proposal(_car_facts(up_evidence={"axis": "+z", "confidence": 0.8, "reason": "feet at -z"},
                                grounded=True), None)
    assert p["up_axis"] == "+z" and p["grounded"] is True
    assert not any(n.startswith("Up is") for n in p["notes"])


def test_the_ground_is_not_proposed_on_a_part_drawn_another_way_up():
    """The measuring step looks for the ground under the file's lowest z; on a car drawn upside
    down that is its roof, so what it found there is no ground."""
    p = gc._proposal(_car_facts(grounded=True), None)
    assert p["up_axis"] == "-z" and p["grounded"] is False


def test_an_internal_part_is_proposed_as_drawn():
    assert gc._proposal(_car_facts(flow="internal", input_kind="body-surface"), None)["up_axis"] == "+z"


def test_a_failed_naming_still_proposes_the_shapes_up():
    p = gc._proposal(_car_facts(), {"error": "TimeoutError"})
    assert p["up_axis"] == "-z" and p["vision_available"] is False


def test_the_flow_guess_is_the_longest_side_across_the_up_axis():
    assert gf.axis_of_longest_side([840.0, 320.0, 290.8]) == "+x"
    assert gf.axis_of_longest_side([300.0, 1400.0, 4600.0], "+y") == "+z"   # a car lying on its side
    assert gf.axis_of_longest_side([1400.0, 1800.0, 4600.0], "-x") == "+z"
    assert gf.axis_of_longest_side([10.0, 10.0, 5.0], "nonsense") == "+x"      # +z, and ties go to x


# ------------------------------------------------------------------------------ the naming ----
class _Store:
    def __init__(self, objects: dict):
        self.objects = dict(objects)
        self.downloads: list[str] = []

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def download_file(self, *, object_key, destination):
        self.downloads.append(object_key)
        Path(destination).write_bytes(self.get_bytes(object_key=object_key))


def _scouted_car(sid: str) -> dict:
    sheet = gc.check_object_key(sid, "upright.png")
    return {"status": "scouted", "session_id": sid, "written_at": 1.0, "named": False,
            "facts": _car_facts(), "proposal": gc._proposal(_car_facts(), None), "snapshots": [],
            "skin_key": gc.check_object_key(sid, "skin.json"),
            "upright_sheet": {"object_key": sheet, "order": ORDER}}


def _naming(monkeypatch, sid: str, vision: dict):
    store = _Store({gc.check_object_key(sid, "scout.json"): json.dumps(_scouted_car(sid)).encode(),
                    gc.check_object_key(sid, "upright.png"): b"png"})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(gc, "_name_with_vision", lambda facts, shots, **k: dict(vision))
    asked: list[dict] = []

    def _up(sheet, order, **k):
        asked.append({"order": list(order), **k})
        return _picked("-z", 0.95)
    monkeypatch.setattr(gc, "_up_with_vision", _up)
    return store, asked


def test_the_naming_asks_which_way_is_up_of_a_body_in_a_flow_with_its_name(monkeypatch):
    sid = "abcdef12-7777"
    store, asked = _naming(monkeypatch, sid, _vision(part="car body"))
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="the SAE car, air around it")
    assert result["status"] == "ready"
    assert asked == [{"order": ORDER, "part": "car body", "purpose_text": "the SAE car, air around it",
                      "session_id": sid, "owner_id": "o1"}]
    assert gc.check_object_key(sid, "upright.png") in store.downloads
    assert result["proposal"]["up_axis"] == "-z" and result["proposal"]["up_axis_confidence"] == 0.99
    assert result["vision"]["up_axis"] == "-z"


def test_the_naming_never_asks_it_of_a_part_the_fluid_flows_through(monkeypatch):
    sid = "abcdef12-8888"
    _, asked = _naming(monkeypatch, sid, _vision(part="pipe elbow", flow="internal", input_kind="body-surface",
                                                 confidence=0.95))
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="water through it")
    assert asked == [] and result["proposal"]["up_axis"] == "+z"


# ------------------------------------------------------------ the words the intake reads ----
def test_a_grounded_part_drawn_another_way_up_is_declared_free_in_the_flow_and_says_why():
    body = ConfirmIn(input_kind="solid-body", flow="external", part="a car", flow_axis="+x",
                     reference_length_mm=840.0, grounded=True, up_axis="-z")
    m = confirmation_message(body)
    assert "drawn with -z up" in m and "meshed free in the flow" in m
    assert "no ground patch is declared" in m and "re-exporting the file with +z up" in m
    assert "wall patch named ground" not in m


def test_a_grounded_upright_part_still_stands_on_the_ground():
    for up in ("+z", None):
        m = confirmation_message(ConfirmIn(input_kind="solid-body", flow="external", flow_axis="+x",
                                           grounded=True, up_axis=up))
        assert "wall patch named ground" in m and "free in the flow" not in m


def test_the_confirm_model_and_the_registry_carry_up():
    assert "up_axis" in gf.FIELD_KEYS
    spec = next(s for s in gf.form_spec() if s["key"] == "up_axis")
    assert [o[0] for o in spec["options"]] == list(gf.UP_AXES) and spec["applies"] == "both"
    assert ConfirmIn(input_kind="solid-body", flow="external", up_axis="-y").up_axis == "-y"
    with pytest.raises(ValueError):
        ConfirmIn(input_kind="solid-body", flow="external", up_axis="down")
    assert body_of({"input_kind": "solid-body", "flow": "external"}).up_axis is None


# -------------------------------------------------------------------- the pictures' cameras ----
def test_a_stored_picture_keeps_its_camera_and_an_old_one_gets_its_named_views():
    """The nose ("at the left of the top picture") is read through the picture's camera. The
    worker stored only the picture's name, so in production the naming never had the camera and
    fell back on the model's own reading of the axis marker."""
    new = gc._shot_of({"name": "front", "object_key": "k", "facing": [1], "direction": [0, 1, 0], "up": [0, 0, 1]},
                      Path("front.png"))
    assert new.direction == (0, 1, 0) and new.up == (0, 0, 1) and new.facing == [1]
    old = gc._shot_of({"name": "top", "object_key": "k", "facing": []}, Path("top.png"))
    assert old.direction == (0.0, 0.0, -1.0) and old.up == (0.0, 1.0, 0.0)
    closeup = gc._shot_of({"name": "opening-3", "object_key": "k", "facing": [3]}, Path("o.png"))
    assert closeup.direction is None and closeup.up is None
    # through it the nose settles the flow, and the model is told where each picture looks
    a = gc._flow_from_nose({"nose_view": "top", "nose_side": "left"}, [old])
    assert a["flow_axis"] == "+x" and a["nose_from"] == "left of the top picture"
    text = gc._facts_text(_car_facts(), [old], "")
    assert "top (stickers facing the camera: none), camera looks along -Z (from above)" in text


def test_the_scout_stores_each_pictures_camera_the_six_way_picture_and_the_shapes_reading(tmp_path, monkeypatch):
    import tempfile

    import meshpipeline.cad.scout as scout_mod
    import meshpipeline.render.scout_snapshots as snaps_mod
    from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary

    store = _Store({})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(gc, "_fetch", lambda ref, work: (work / "geometry.step").write_text("step") or work / "geometry.step")
    monkeypatch.setattr(gc, "_prepared_coordinates", lambda path, interp_ref, ref: (None, ""))
    facts = _car_facts()
    facts.pop("up_evidence")
    monkeypatch.setattr(scout_mod, "scout_cad", lambda path, *, prepared: SimpleNamespace(as_dict=lambda: dict(facts, notes=[])))

    def _skin(path, dest, *, prepared):
        write_stl_binary(Path(dest), _box_triangles((0, 0, 0), (1, 1, 1)))
        return Path(dest)
    monkeypatch.setattr(scout_mod, "write_view_stl", _skin)

    def _pictures(skin, openings, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "front.png").write_bytes(b"png")
        return [snaps_mod.Snapshot(name="front", path=Path(out) / "front.png", direction=(0.0, 1.0, 0.0),
                                   facing=[], up=(0.0, 0.0, 1.0))]
    monkeypatch.setattr(snaps_mod, "render_snapshots", _pictures)

    def _sheet(skin, out_path, **_):
        Path(out_path).write_bytes(b"sheet")
        return snaps_mod.UprightSheet(path=Path(out_path), order=tuple(ORDER))
    monkeypatch.setattr(snaps_mod, "render_upright_sheet", _sheet)
    source = {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/car.step", "sha256": "0" * 64,
              "size_bytes": 4, "original_filename": "car.step", "suffix_hint": ".step"}
    result = gc.run_geometry_check(session_id="abcdef12-2222", owner_id="o1", source=source)
    assert result.get("reason") is None, result
    stored = json.loads(store.objects["sessions/abcdef12-2222/geometry_check/scout.json"])
    assert stored["snapshots"][0]["direction"] == [0.0, 1.0, 0.0] and stored["snapshots"][0]["up"] == [0.0, 0.0, 1.0]
    # the six-way picture is stored beside the others, not among them
    assert stored["upright_sheet"] == {"object_key": "sessions/abcdef12-2222/geometry_check/upright.png", "order": ORDER}
    assert store.objects["sessions/abcdef12-2222/geometry_check/upright.png"] == b"sheet"
    assert [s["name"] for s in stored["snapshots"]] == ["front"]
    # a plain box has nothing to stand on: the shape's reading says so, and the part is shown as drawn
    assert stored["facts"]["up_evidence"]["axis"] is None
    assert stored["proposal"]["up_axis"] == "+z"


def test_a_six_way_picture_that_cannot_be_drawn_never_fails_the_scout(tmp_path, monkeypatch):
    import meshpipeline.render.scout_snapshots as snaps_mod

    def _broken(skin, out_path, **_):
        raise RuntimeError("no OpenGL")
    monkeypatch.setattr(snaps_mod, "render_upright_sheet", _broken)
    assert gc._store_upright_sheet("s1", tmp_path / "skin.stl", tmp_path, _Store({})) is None


def test_a_shape_that_cannot_be_read_never_fails_the_scout(tmp_path):
    reading = gc.read_up_evidence(tmp_path / "missing.stl")
    assert reading["axis"] is None and "could not be read" in reading["reason"]


# --------------------------------------------------------------------- back out for the views ----
def _stored(monkeypatch, **objects) -> None:
    store = _Store({gc.check_object_key("s1", name): json.dumps(v).encode() for name, v in objects.items()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)


def test_the_views_open_with_what_the_user_confirmed_else_what_the_check_proposed(monkeypatch):
    _stored(monkeypatch, **{"scout.json": {"proposal": {"up_axis": "-z"}}, "confirmed.json": {"up_axis": "+y"}})
    assert gc.stored_up_axis("s1") == "+y"
    assert gc.stored_up_axis("s1", confirmed=False) == "-z"
    _stored(monkeypatch, **{"scout.json": {"proposal": {"up_axis": "-z"}}, "confirmed.json": {"up_axis": None}})
    assert gc.stored_up_axis("s1") == "-z"
    _stored(monkeypatch, **{"scout.json": {"status": "pending"}})
    assert gc.stored_up_axis("s1") is None
    _stored(monkeypatch)
    assert gc.stored_up_axis("s1") is None
