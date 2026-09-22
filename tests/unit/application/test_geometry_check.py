# Responsibility: Verify the geometry check's judgement-free plumbing - how the model's naming is
# merged onto the code's measurements, what the model is told, and what the user's confirmation
# becomes in the session the intake reads.
# Boundaries: pure functions only; no CAD, no rendering, no model, no database.
from __future__ import annotations

from meshpipeline.api.v1.geometry import (
    ConfirmedOpening,
    ConfirmIn,
    confirmation_message,
    patches_from,
    with_declaration,
)
from meshpipeline.application import geometry_check as gc


def _facts():
    return {
        "body_kind": "pipe_wall", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [1448.8, 1233.4, 539.3], "seed_point_mm": [119.7, 86.7, 0.0],
        "confidence": {"input_kind": 0.9, "openings": 0.72}, "notes": [],
        "openings": [
            {"id": 1, "name": "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 466.2,
             "centroid_mm": [0.0, 0.0, 0.0], "normal": [-1, 0, 0], "confidence": 0.85},
            {"id": 2, "name": "outlet", "role": "outlet", "shape": "circle", "diameter_mm": 466.2,
             "centroid_mm": [1197.1, 867.2, 0.0], "normal": [0.36, 0.93, 0], "confidence": 0.6},
        ],
    }


class _Shot:
    def __init__(self, name, facing):
        self.name, self.facing = name, facing


# ------------------------------------------------------------------------------ merging ----
def test_the_model_names_the_stickers_and_the_code_keeps_the_positions():
    vision = {"part": "pipe elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 2, "name": "inlet", "role": "inlet", "confidence": 0.8},
                           {"id": 1, "name": "outlet", "role": "outlet", "confidence": 0.8}]}
    p = gc._merge(_facts(), vision)
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[2]["role"] == "inlet" and by_id[2]["centroid_mm"] == [1197.1, 867.2, 0.0]
    assert by_id[1]["role"] == "outlet" and by_id[1]["diameter_mm"] == 466.2
    assert p["part"] == "pipe elbow" and p["vision_available"] is True


def test_a_sticker_the_model_invents_is_ignored():
    vision = {"part": "x", "flow": "internal", "input_kind": "body-surface", "confidence": 0.5,
              "openings": [{"id": 9, "name": "ghost", "role": "inlet", "confidence": 1.0}]}
    p = gc._merge(_facts(), vision)
    assert [o["id"] for o in p["openings"]] == [1, 2]
    assert [o["name"] for o in p["openings"]] == ["inlet", "outlet"]


def test_the_kind_follows_whoever_is_surer():
    facts = _facts()
    unsure = {"part": "x", "flow": "external", "input_kind": "solid-body", "confidence": 0.3, "openings": []}
    assert gc._merge(facts, unsure)["input_kind"] == "body-surface"      # the code was surer (0.9)
    sure = dict(unsure, confidence=0.95)
    assert gc._merge(facts, sure)["input_kind"] == "solid-body"


def test_without_the_model_the_codes_names_stand_and_the_user_is_told():
    p = gc._merge(_facts(), {"error": "TimeoutError"})
    assert [o["name"] for o in p["openings"]] == ["inlet", "outlet"]
    assert p["vision_available"] is False
    assert any("unavailable" in n for n in p["notes"])


def test_the_model_is_told_the_sizes_the_guesses_and_which_stickers_face_each_picture():
    text = gc._facts_text(_facts(), [_Shot("overview", [1, 2]), _Shot("opening-1", [1])], "water through an elbow")
    assert "1449 x 1233 x 539 mm" in text
    assert "1: circle opening, 466 mm across" in text and "guessed inlet" in text
    assert "The user said: water through an elbow" in text
    assert "opening-1 (stickers facing the camera: 1)" in text


# ----------------------------------------------------------------------- confirmation ----
def _confirm():
    return ConfirmIn(input_kind="body-surface", flow="internal", part="pipe elbow",
                     openings=[ConfirmedOpening(id=1, name="inlet", role="inlet", diameter_mm=466.2,
                                                centroid_mm=[0.0, 0.0, 0.0]),
                               ConfirmedOpening(id=2, name="outlet", role="outlet", width_mm=120.0,
                                                height_mm=80.0, centroid_mm=[1197.1, 867.2, 0.0])],
                     seed_point_mm=[119.7, 86.7, 0.0], size_mm=[1448.8, 1233.4, 539.3])


def test_the_confirmation_message_opens_with_the_phrase_the_intake_prompt_names():
    m = confirmation_message(_confirm())
    assert m.startswith("GEOMETRY CHECK (confirmed by the user):")
    assert "hollow inside for the fluid (pipe elbow)" in m and "flows through it" in m
    assert "inlet (inlet), 466 mm across at (0, 0, 0) mm" in m
    assert "outlet (outlet), 120 x 80 mm at (1197, 867, 0) mm" in m
    assert "A point inside the flow: (120, 87, 0) mm" in m
    assert "Part size: 1449 x 1233 x 539 mm" in m


def test_the_patches_carry_the_sizes_and_positions_the_port_binding_reads():
    patches = patches_from(_confirm())
    assert patches[0] == {"name": "inlet", "type": "inlet", "diameter_mm": 466.2, "near_mm": [0.0, 0.0, 0.0]}
    assert patches[1] == {"name": "outlet", "type": "outlet", "width_mm": 120.0, "height_mm": 80.0,
                          "near_mm": [1197.1, 867.2, 0.0]}
    assert patches[-1] == {"name": "wall", "type": "wall"}      # internal flow always has its wall


def test_a_body_in_a_flow_confirms_with_no_openings_and_no_wall_patch():
    body = ConfirmIn(input_kind="solid-body", flow="external", openings=[])
    assert patches_from(body) == []
    assert "flows around it" in confirmation_message(body)
    assert "No openings" in confirmation_message(body)


def test_a_second_confirmation_replaces_the_first_in_the_conversation():
    first = confirmation_message(_confirm())
    history = [{"role": "user", "content": "elbow, water"}, {"role": "assistant", "content": "Noted."}]
    once = with_declaration(history, first)
    assert once[:2] == history and once[-1] == {"role": "assistant", "content": first}
    second = confirmation_message(ConfirmIn(input_kind="fluid-domain", flow="internal", openings=[]))
    twice = with_declaration(once, second)
    assert [m["content"] for m in twice] == ["elbow, water", "Noted.", second]   # one declaration, the latest


# --------------------------------------------------------------------------- the workspace ----
class _Store:
    def __init__(self):
        self.written = {}

    def upload_file(self, *, local_path, object_key, **_):
        self.written[object_key] = local_path.read_bytes()


def test_the_workspace_is_removed_even_when_the_check_fails(tmp_path, monkeypatch):
    import tempfile

    from meshpipeline.contracts import object_storage

    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))     # every workspace lands here

    def _fetch_fails(ref, work):
        (work / "geometry.step").write_text("partial download")
        raise RuntimeError("the retrieved geometry does not match the upload")

    monkeypatch.setattr(gc, "_fetch", _fetch_fails)
    source = {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/elbow.step", "sha256": "0" * 64,
              "size_bytes": 10, "original_filename": "elbow.step", "suffix_hint": ".step"}
    result = gc.run_geometry_check(session_id="abcdef12-0000", owner_id="o1", source=source)
    assert result["reason"].startswith("RuntimeError")
    assert not list(tmp_path.glob("geometry_check_*"))       # nothing left behind on the worker
    assert "sessions/abcdef12-0000/geometry_check/scout.json" in store.written
