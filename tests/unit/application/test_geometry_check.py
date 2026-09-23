# Responsibility: Verify the geometry check's judgement-free plumbing - how the model's naming is
# merged onto the code's measurements, what the model is told, and what the user's confirmation
# becomes in the session the intake reads.
# Boundaries: pure functions only; no CAD, no rendering, no model, no database.
from __future__ import annotations

from pathlib import Path

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


# ------------------------------------------------------------------------------ the skin ----
class _Scout:
    def __init__(self, facts):
        self._facts = facts

    def as_dict(self):
        return self._facts


def test_the_check_stores_the_skin_the_viewer_draws(tmp_path, monkeypatch):
    """The stage renders the same skin the pictures were drawn from, in the shape the mesh
    viewer already reads, and the stored result says the skin is there."""
    import json
    import tempfile

    import meshpipeline.cad.scout as scout_mod
    import meshpipeline.render.scout_snapshots as snaps_mod
    from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary
    from meshpipeline.contracts import object_storage

    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(gc, "_fetch", lambda ref, work: (work / "geometry.step").write_text("step") or work / "geometry.step")
    monkeypatch.setattr(gc, "_prepared_coordinates", lambda path, interp_ref, ref: (None, ""))
    monkeypatch.setattr(gc, "_name_with_vision", lambda *a, **k: {"error": "no model in this test"})
    monkeypatch.setattr(scout_mod, "scout_cad", lambda path, *, prepared: _Scout(_facts()))

    def _skin(path, dest, *, prepared):
        write_stl_binary(Path(dest), _box_triangles((0, 0, 0), (1, 1, 1)))
        return Path(dest)
    monkeypatch.setattr(scout_mod, "write_view_stl", _skin)
    monkeypatch.setattr(snaps_mod, "render_snapshots", lambda skin, openings, out: [])

    source = {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/elbow.step", "sha256": "0" * 64,
              "size_bytes": 4, "original_filename": "elbow.step", "suffix_hint": ".step"}
    monkeypatch.setattr(gc, "_name_with_vision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the scout must not call the model")))
    result = gc.run_geometry_check(session_id="abcdef12-1111", owner_id="o1", source=source)
    assert result.get("reason") is None, result
    assert result["status"] == "scouted" and result["named"] is False
    skin = json.loads(store.written["sessions/abcdef12-1111/geometry_check/skin.json"])
    assert skin["kind"] == "stl" and skin["is_mesh"] is False and skin["mesh_units"] == "m"
    assert skin["patches"][0]["name"] == "skin" and skin["patches"][0]["tri_count"] == 12
    assert "positions_b64" in skin["patches"][0]
    stored = json.loads(store.written["sessions/abcdef12-1111/geometry_check/scout.json"])
    assert stored["skin_key"] == "sessions/abcdef12-1111/geometry_check/skin.json"
    assert stored["proposal"]["openings"][0]["centroid_mm"] == [0.0, 0.0, 0.0]


# ------------------------------------------------------------------------------ the naming ----
class _NamingStore(_Store):
    """A store that also answers reads, so the naming can find the scout it waits for."""

    def __init__(self, objects: dict):
        super().__init__()
        self.objects = dict(objects)

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()
        self.written[object_key] = self.objects[object_key]

    def get_bytes(self, *, object_key):
        from meshpipeline.contracts.object_storage import ObjectNotFound
        if object_key not in self.objects:
            raise ObjectNotFound(object_key)
        return self.objects[object_key]

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)
        self.written["deleted:" + object_key] = b""


def _scouted(session_id: str, *, unit_assumed: bool, scale: float) -> dict:
    facts = dict(_facts(), unit_assumed=unit_assumed, scale_to_m=scale, read_as="mesh")
    return {"status": "scouted", "named": False, "facts": facts, "proposal": {},
            "snapshots": [], "skin_key": f"sessions/{session_id}/geometry_check/skin.json"}


def test_the_naming_rescales_the_facts_and_the_skin_to_the_confirmed_unit(monkeypatch):
    import base64
    import json
    import struct

    from meshpipeline.contracts import object_storage

    sid = "abcdef12-2222"
    stored = _scouted(sid, unit_assumed=True, scale=0.001)
    skin = {"kind": "stl", "patches": [{"name": "skin", "positions_b64":
            base64.b64encode(struct.pack("<9f", *[0.0, 0.0, 0.0, 0.001, 0.0, 0.0, 0.0, 0.001, 0.0])).decode()}]}
    store = _NamingStore({f"sessions/{sid}/geometry_check/scout.json": json.dumps(stored).encode(),
                          f"sessions/{sid}/geometry_check/skin.json": json.dumps(skin).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(gc, "_name_with_vision", lambda facts, shots, **k: {
        "part": "pipe elbow", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
        "openings": [{"id": 1, "name": "water_in", "role": "inlet", "confidence": 0.9}], "_size": facts["size_mm"]})
    metres = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "m", "scale_to_metres": 1.0,
              "basis": "user_confirmed", "evidence": "test"}
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="water through the elbow",
                                    interpretation=metres)
    assert result["status"] == "ready" and result["named"] is True
    assert result["facts"]["size_mm"] == [1448800.0, 1233400.0, 539300.0]      # a thousand times larger
    assert result["facts"]["openings"][0]["centroid_mm"] == [0.0, 0.0, 0.0]
    assert result["facts"]["openings"][1]["diameter_mm"] == 466200.0
    assert result["proposal"]["openings"][0]["name"] == "water_in"
    assert result["facts"]["unit_assumed"] is False
    new_skin = json.loads(store.objects[f"sessions/{sid}/geometry_check/skin.json"])
    pts = struct.unpack("<9f", base64.b64decode(new_skin["patches"][0]["positions_b64"]))
    assert abs(pts[3] - 1.0) < 1e-6 and abs(pts[7] - 1.0) < 1e-6              # the skin moved with the facts


def test_a_naming_that_finds_no_scout_in_time_withdraws_its_request(monkeypatch):
    import json

    from meshpipeline.contracts import object_storage

    sid = "abcdef12-3333"
    store = _NamingStore({f"sessions/{sid}/geometry_check/scout.json": json.dumps({"status": "pending"}).encode(),
                          f"sessions/{sid}/geometry_check/naming.json": b"{}"})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(gc, "NAMING_WAIT_S", 0.0)
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="a pipe")
    assert result == {"status": "pending", "named": False}
    assert f"deleted:sessions/{sid}/geometry_check/naming.json" in store.written
    assert f"sessions/{sid}/geometry_check/naming.json" not in store.objects


def test_a_late_naming_failure_never_replaces_a_ready_check(monkeypatch):
    import json

    from meshpipeline.contracts import object_storage

    sid = "abcdef12-4444"
    ready = {"status": "ready", "named": True, "proposal": {"part": "elbow"}, "facts": {}}
    store = _NamingStore({f"sessions/{sid}/geometry_check/scout.json": json.dumps(ready).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(gc, "_name", lambda **k: (_ for _ in ()).throw(RuntimeError("provider down")))
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="an elbow")
    assert result["status"] == "ready" and result["named"] is True
    assert json.loads(store.objects[f"sessions/{sid}/geometry_check/scout.json"])["status"] == "ready"
    assert not any(k.endswith("scout.json") for k in store.written)      # nothing overwritten


def test_a_naming_failure_on_an_unnamed_check_is_recorded(monkeypatch):
    import json

    from meshpipeline.contracts import object_storage

    sid = "abcdef12-5555"
    store = _NamingStore({f"sessions/{sid}/geometry_check/scout.json": json.dumps({"status": "scouted"}).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(gc, "_name", lambda **k: (_ for _ in ()).throw(RuntimeError("provider down")))
    result = gc.run_geometry_naming(session_id=sid, owner_id="o1", purpose_text="an elbow")
    assert result["status"] == "failed" and "provider down" in result["reason"]
    assert json.loads(store.written[f"sessions/{sid}/geometry_check/scout.json"])["status"] == "failed"
