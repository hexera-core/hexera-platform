# Responsibility: Verify "Mesh as: Auto / Shell / Solid" (SHSTAGE-2..5): the shell check run in a child process
# on small synthetic parts (a T-joint: all shell; a plate with a 30 mm boss: mixed; a block: solid), Auto's
# recommendation and its plain-words reason, what stays solid and where plates do not meet, the stage's states
# (measuring, ready, unavailable, stale), and the choice settled at Proceed - the user's pick never overridden.
# Boundaries: an in-memory object store; the real extraction (cad/midsurface) on parts that take under a second.
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.application import shell_check as sc
from meshpipeline.contracts import object_storage

SID = "5e55-1011"
CAD = {"source_format": "step", "validity": {"kind": "brep"}, "scale_to_m": 0.001}


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: s)
    return s


@pytest.fixture(scope="module")
def parts(tmp_path_factory):
    """Each synthetic part's shell check, as the worker runs it: (shell.json, overlay.json)."""
    from tests import shell_fixtures as sf

    from meshpipeline.cad import shell_preview

    root = tmp_path_factory.mktemp("shell_parts")
    out = {}
    for name, shape in (("tjoint", sf.t_joint()), ("boss", sf.plate_with_boss()), ("block", sf.chunky_block())):
        step = sf.write_step(shape, root / f"{name}.step")
        assert shell_preview.main([str(step), str(root / name), "1.0"]) == 0
        out[name] = (json.loads((root / name / "shell.json").read_text()),
                     json.loads((root / name / "overlay.json").read_text()), step)
    return out


def _ready(summary, scale=0.001):
    return {"status": sc.READY, "scale_to_m": scale, "summary": summary}


def test_the_drawing_holds_the_mid_surface_with_each_wall(parts):
    rec, drawn, _ = parts["tjoint"]
    xyz = np.frombuffer(base64.b64decode(drawn["xyz"]), dtype=np.float32).reshape(-1, 3)
    tri = np.frombuffer(base64.b64decode(drawn["tri"]), dtype=np.uint32).reshape(-1, 3)
    wall = np.frombuffer(base64.b64decode(drawn["wall"]), dtype=np.float32)
    assert len(tri) == drawn["triangles"] == len(wall) > 0 and tri.max() < len(xyz)
    # the T-joint: a 5 mm plate and a 4 mm web, every triangle carries one of them
    assert set(np.round(wall, 3)) == {4.0, 5.0} and (drawn["t_min"], drawn["t_max"]) == (4.0, 5.0)
    assert rec["bodies"][0]["verdict"] == "shell"


def test_auto_recommends_shell_for_a_part_that_is_all_plate(parts):
    rec, drawn, _ = parts["tjoint"]
    summary = sc.summarise(rec, drawn)
    assert summary["share"] >= sc.AUTO_SHELL_SHARE and summary["open_edges"] == 0
    view = sc.shell_view(_ready(summary), CAD)
    assert view["state"] == "ready" and view["recommend"] == "shell" and view["default"] == "auto"
    assert view["reason"].startswith("100% of this part is plate 4 mm to 5 mm thick, so a shell mesh needs far fewer")
    assert view["not_shelled"] == [] and view["overlay"] and view["legend_mm"] == [4.0, 5.0]
    assert sc.settle(view, "auto") == {"mesh_as": "shell", "picked": "auto", "by": "auto", "why": view["reason"]}


def test_a_mixed_part_is_recommended_shell_and_solid_and_says_what_stays_solid(parts):
    """MIXED-4: a plate with a thick boss (its plate reaches far past 10 walls: MIXED-8 holds) is Auto's shell +
    solid, with mixed_run's words; the boss is listed as kept solid."""
    rec, drawn, _ = parts["boss"]
    assert rec["mixed_auto"]["ok"] is True and rec["mixed_auto"]["too_thick"] == []
    summary = sc.summarise(rec, drawn)
    assert summary["mixed_ok"] is True
    view = sc.shell_view(_ready(summary), CAD)
    assert view["recommend"] == "mixed"
    assert view["reason"].startswith("Thin walls meshed as shell (") and view["reason"].endswith(
        "1 thick region kept solid and joined to them.")
    (row,) = view["not_shelled"]
    assert row["id"] == "S1" and row["kind"] == "solid"
    assert row["words"] == ("A thick region of the part (5 faces) stays solid: it is 30 mm thick or more, too "
                            "thick for a shell.")
    assert row["at_mm"] == pytest.approx([100.0, 50.0, 22.0], abs=0.5)
    # the user's pick stands: Shell on a mixed part is a shell (its solid region said on the stage)
    assert sc.settle(view, "shell")["mesh_as"] == "shell"
    assert sc.settle(view, "solid")["mesh_as"] == "solid"
    settled = sc.settle(view, "auto")
    assert settled == {"mesh_as": "mixed", "picked": "auto", "by": "auto", "why": view["reason"]}
    assert sc.declaration_words(settled).startswith("Meshed as: shell where thin, solid where thick")


def test_a_mixed_part_whose_walls_are_too_thick_for_shells_is_recommended_solid(tmp_path):
    """MIXED-8 on the stage: the box with thick corners (4 mm walls reaching 5 to 6.7 walls: its back wall read +23%
    mixed), the stiffened panel (7.8 to 8.3 walls) and the casting (a mode -6.8%) are never offered as shell + solid;
    Auto says solid, with the reason (the casting's thin walls hold under half its metal: said first)."""
    from tests import shell_fixtures as sf
    from tests.unit.engines import test_mixed_families as fam

    from meshpipeline.cad import shell_preview

    for name, said in (("box_corners", "Walls too thick for shells: "), ("stiff_panel_mount", "Walls too thick for "
                       "shells: "), ("casting_ribs_block", "Only 42% of this part is thin wall")):
        step = sf.write_step(fam._family(name)[0], tmp_path / f"{name}.step")
        assert shell_preview.main([str(step), str(tmp_path / name), "1.0"]) == 0
        rec = json.loads((tmp_path / name / "shell.json").read_text())
        drawn = json.loads((tmp_path / name / "overlay.json").read_text())
        assert rec["mixed_auto"]["ok"] is False, name
        view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD)
        assert view["recommend"] == "solid", name
        assert view["reason"].startswith(said), (name, view["reason"])
        assert sc.settle(view, "auto")["mesh_as"] == "solid"


def test_mixed_is_never_offered_without_every_wall_s_reach():
    """MIXED-8 is read only with the reach; a summary whose check could not measure it (or a record from before it)
    is never Auto's shell + solid."""
    summary = {"bodies": [{"body": "Part_1", "verdict": "mixed"}], "share": 0.8, "t_min_mm": 2.0, "t_max_mm": 2.0,
               "solid_regions": [], "open_edges": 0, "open_places": []}
    assert sc.recommendation(summary)[0] == "solid"
    assert sc.recommendation({**summary, "mixed_ok": False, "mixed_why": "Walls too thick for shells: x."}) == (
        "solid", "Walls too thick for shells: x.")
    assert sc.recommendation({**summary, "mixed_ok": True, "mixed_why": "Thin walls meshed as shell."}) == (
        "mixed", "Thin walls meshed as shell.")


def test_a_solid_reason_without_numbers_keeps_the_plain_words():
    summary = {"bodies": [{"body": "Part_1", "verdict": "solid"}],
               "solid_regions": [{"body": "Part_1", "whole": True, "why": "no wall is thin against its size"}]}
    assert sc.recommendation(summary) == ("solid", sc.SOLID_WHY)


def test_a_solid_part_is_solid_even_when_shell_is_asked_for_with_the_reason(parts):
    rec, drawn, _ = parts["block"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, {"value": "shell", "by": "table"})
    # SHSTAGE-3b: the reason carries the measured wall: a 50 x 40 x 30 block's thinnest "wall" is 30 mm over 45 mm
    assert view["recommend"] == "solid" and view["reason"].startswith("No wall of this part is thin against its size: ")
    assert "30 mm thick" in view["reason"] and view["reason"].endswith("So it is meshed as a solid.")
    assert view["default"] == "shell" and view["said_words"] == "You asked for a shell mesh in the chat."
    assert view["not_shelled"][0]["words"].startswith("The whole part stays solid: no wall is thin against its size: ")
    assert "30 mm thick" in view["not_shelled"][0]["words"]
    settled = sc.settle(view, "shell")
    assert settled["mesh_as"] == "solid" and settled["why"] == sc.NOTHING_TO_SHELL


def test_open_edges_are_listed_marked_and_keep_auto_solid():
    summary = {"bodies": [{"body": "Part_1", "verdict": "shell"}], "share": 1.0, "t_min_mm": 2.0, "t_max_mm": 2.0,
               "solid_regions": [], "open_edges": 3, "open_places": [{"at_mm": [1, 2, 3]}, {"at_mm": [4, 5, 6]}],
               "open_at_mm": [[1, 2, 3], [4, 5, 6], [4, 5, 7]], "overlay": True}
    view = sc.shell_view(_ready(summary), CAD)
    assert view["recommend"] == "solid"
    assert view["reason"] == ("This part is plate 2 mm thick, but its mid-surfaces do not meet at 2 places yet, so a "
                              "shell mesh would have gaps there. Auto meshes it as a solid.")
    words = [r["words"] for r in view["not_shelled"]]
    assert words[0] == "The plates do not meet at 2 places: shown in red. A shell mesh has a gap there."
    assert [r["id"] for r in view["not_shelled"]] == ["", "O1", "O2"] and view["open_edges"] == 3


def test_while_measuring_the_stage_says_so_and_auto_settles_solid_at_proceed():
    started = {"status": sc.MEASURING, "started_at": time.time()}
    view = sc.shell_view(started, CAD)
    assert view["state"] == "measuring" and view["recommend"] == "" and view["measuring_words"]
    assert "Auto meshes the part as a solid" in view["early_proceed"]
    assert sc.settle(view, "auto") == {"mesh_as": "solid", "picked": "auto", "by": "auto", "why": sc.EARLY_WHY}
    # an explicit pick is settled as it is, measured or not - the run makes the shell itself
    assert sc.settle(view, "shell")["mesh_as"] == "shell"
    assert sc.settle(view, "solid")["mesh_as"] == "solid"
    # a check that never came back is read as stopped, and said
    stale = sc.shell_view({"status": sc.MEASURING, "started_at": time.time() - sc.STALE_AFTER_S - 5}, CAD)
    assert stale["state"] == "unavailable" and stale["recommend"] == "solid" and "took longer" in stale["reason"]
    # not started yet (the naming is landing): measuring
    assert sc.shell_view(None, CAD)["state"] == "measuring"


def test_a_triangle_file_cannot_be_shelled_and_says_why():
    stl = {"source_format": "stl", "validity": {"kind": "triangles"}}
    view = sc.shell_view(None, stl, {"value": "shell", "by": "table"})
    assert view["state"] == "unavailable" and not view["shell_ok"] and view["recommend"] == "solid"
    assert view["reason"] == sc.TRIANGLES_WHY and view["default"] == "auto"
    assert sc.settle(view, "shell") == {"mesh_as": "solid", "picked": "shell", "by": "user", "why": sc.TRIANGLES_WHY}


def test_the_users_words_are_the_default_and_are_never_overridden(parts):
    rec, drawn, _ = parts["tjoint"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, {"value": "solid", "by": "model"})
    assert view["recommend"] == "shell" and view["default"] == "solid"
    # nothing sent (an older console): the words decide, not Auto
    assert sc.settle(view, "") == {"mesh_as": "solid", "picked": "solid", "by": "words",
                                   "why": "You asked for a solid mesh in the chat."}
    assert sc.settle(view, "solid")["by"] == "words"
    # a click is the user's own later word
    assert sc.settle(view, "shell") == {"mesh_as": "shell", "picked": "shell", "by": "user", "why": "You picked a shell mesh."}
    assert sc.declaration_words(sc.settle(view, "shell")).startswith("Meshed as: a shell")


def test_a_unit_corrected_after_the_check_rereads_its_lengths(parts):
    rec, drawn, _ = parts["boss"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn), scale=0.001), {**CAD, "scale_to_m": 0.0254})
    assert view["scale"] == pytest.approx(25.4)
    assert view["t_min_mm"] == pytest.approx(4.0 * 25.4) and "762 mm thick or more" in view["not_shelled"][0]["words"]


def test_the_worker_runs_it_in_a_child_process_and_stores_it(parts, store, monkeypatch):
    _rec, _drawn, step = parts["tjoint"]
    monkeypatch.setattr(sc, "_cad_of", lambda stored, interpretation, work: (step, 1.0))
    store.objects[f"sessions/{SID}/geometry_check/scout.json"] = json.dumps({"status": "ready", "facts": CAD}).encode()
    assert sc.run_shell_check(session_id=SID) == {"status": "ready"}
    status = sc.read_status(SID)
    assert status["status"] == "ready" and status["summary"]["bodies"] == [{"body": "Part_1", "verdict": "shell"}]
    for name in (sc.RECORD_FILE, sc.SURFACE_FILE, sc.OVERLAY_FILE):
        assert f"sessions/{SID}/geometry_check/{name}" in store.objects
    assert sc.view_for(SID, CAD)["recommend"] == "shell"


def test_a_check_that_cannot_run_is_unavailable_and_auto_is_solid(store, monkeypatch):
    def _boom(*a):
        raise ValueError("not a CAD file")
    monkeypatch.setattr(sc, "_cad_of", _boom)
    store.objects[f"sessions/{SID}/geometry_check/scout.json"] = json.dumps({"status": "ready", "facts": CAD}).encode()
    assert sc.run_shell_check(session_id=SID) == {"status": "unavailable"}
    view = sc.view_for(SID, CAD)
    assert view["state"] == "unavailable" and view["recommend"] == "solid"
    assert view["reason"].startswith("The walls could not be measured (the part could not be read for it")


def test_start_marks_it_measuring_and_hands_it_to_the_worker(store, monkeypatch):
    from meshpipeline.contracts import geometry_check as seam

    got = []
    seam.set_shell_check_enqueuer(lambda **kw: got.append(kw))
    try:
        assert sc.start(SID, "alice", facts=CAD, interpretation=None)
    finally:
        seam.set_shell_check_enqueuer(None)
    assert got == [{"session_id": SID, "owner_id": "alice", "interpretation": None}]
    assert sc.read_status(SID)["status"] == sc.MEASURING
    # nothing set up to run it: said, never measuring forever
    assert not sc.start(SID, "alice", facts=CAD, interpretation=None)
    assert sc.read_status(SID)["status"] == sc.UNAVAILABLE
    # a triangle file is not checked
    assert not sc.start(SID, "alice", facts={"source_format": "stl"}, interpretation=None)


def test_the_confirmed_choice_is_read_into_the_run(store):
    assert sc.confirmed_mesh_as(SID) == "solid"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "shell"}).encode()
    assert sc.confirmed_mesh_as(SID) == "shell"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "mixed"}).encode()
    assert sc.confirmed_mesh_as(SID) == "mixed"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "beam"}).encode()
    assert sc.confirmed_mesh_as(SID) == "solid"
    assert sc.confirmed_mesh_as("") == "solid"
