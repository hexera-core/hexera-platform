# Responsibility: Verify that a shell is meshed ONLY when the user's words ask for one (SHSTAGE-2..5): plain "FEA"
# is a solid with no shell view, no shell check and nothing said; "shell", "mid-surface" and the element names are a
# shell, its check started, its mid-surface drawn; a shell asked for on a part that cannot be one (a chunky block, a
# triangle file) is a solid with a one-sentence reason. The shell check runs in a child process on small synthetic
# parts (a T-joint: all shell; a plate with a 30 mm boss: mixed; a block: solid).
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


SHELL = {"value": "shell", "by": "table"}


def test_the_drawing_holds_the_mid_surface_with_each_wall(parts):
    rec, drawn, _ = parts["tjoint"]
    xyz = np.frombuffer(base64.b64decode(drawn["xyz"]), dtype=np.float32).reshape(-1, 3)
    tri = np.frombuffer(base64.b64decode(drawn["tri"]), dtype=np.uint32).reshape(-1, 3)
    wall = np.frombuffer(base64.b64decode(drawn["wall"]), dtype=np.float32)
    assert len(tri) == drawn["triangles"] == len(wall) > 0 and tri.max() < len(xyz)
    # the T-joint: a 5 mm plate and a 4 mm web, every triangle carries one of them
    assert set(np.round(wall, 3)) == {4.0, 5.0} and (drawn["t_min"], drawn["t_max"]) == (4.0, 5.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    # no Auto: the check no longer measures a shell + solid recommendation
    assert "mixed_auto" not in rec


@pytest.mark.parametrize("words", ["FEA", "Static FEA of this bracket, 40k elements", "stress testing",
                                   "Modal analysis, solid mesh please"])
def test_plain_fea_is_a_solid_with_no_shell_view(parts, words):
    """(a) The words alone decide: plain FEA - even on a part that is all plate - is a solid, as before shell
    existed. No view, nothing settled, nothing said in the declaration."""
    rec, drawn, _ = parts["tjoint"]
    msgs = [{"role": "user", "content": words}]
    assert not sc.wants_shell({}, msgs)
    assert sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, sc.said_of({}, msgs)) is None
    settled = sc.settle(None)
    assert settled == {"mesh_as": "solid", "picked": "", "by": "", "why": ""}
    assert sc.declaration_words(settled) == ""


@pytest.mark.parametrize("words", ["FEA, shell meshing", "please do a mid-surface mesh", "midsurface model",
                                   "CQUAD4 plate elements", "Abaqus S4 shells", "Ansys SHELL181", "2D elements"])
def test_shell_words_are_a_shell(parts, words):
    """(b)/(c) "shell", "mid-surface" and the solvers' element names are a shell: the view says it, the
    mid-surface's legend is served, and Proceed settles a shell."""
    rec, drawn, _ = parts["tjoint"]
    msgs = [{"role": "user", "content": words}]
    assert sc.wants_shell({}, msgs), words
    view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, sc.said_of({}, msgs))
    assert view["mesh_as"] == "shell" and view["said_words"] == sc.SAID_SHELL and view["reason"] == ""
    assert view["label"] == sc.SHELL_LABEL and view["overlay"] and view["legend_mm"] == [4.0, 5.0]
    assert "options" not in view and "recommend" not in view and "default" not in view
    settled = sc.settle(view)
    assert settled == {"mesh_as": "shell", "picked": "shell", "by": "words", "why": sc.SAID_SHELL}
    assert sc.declaration_words(settled).startswith("Meshed as: a shell")


def test_the_latest_word_stands_and_a_later_shell_counts():
    """A shell asked for in a later message (a note after the check) is a shell; taken back later, a solid."""
    named = {"mesh_as_words": {"value": "", "by": "", "why": "no mesh-as words"}}
    first = [{"role": "user", "content": "FEA of my bracket"}]
    assert not sc.wants_shell(named, first)
    later = first + [{"role": "assistant", "content": "Shell or solid?"}, {"role": "user", "content": "make it a shell"}]
    assert sc.wants_shell(named, later)
    back = later + [{"role": "user", "content": "actually a solid mesh"}]
    assert not sc.wants_shell(named, back)
    # the naming's own reading (the model's, for a paraphrase) stands when no later message says either
    assert sc.wants_shell({"mesh_as_words": {"value": "shell", "by": "model"}}, first)


def test_a_mixed_part_asked_as_a_shell_is_a_shell_and_says_what_is_not_in_it(parts):
    """A plate with a thick boss, asked for as a shell: a shell, the boss listed as not in it (no Auto shell +
    solid)."""
    rec, drawn, _ = parts["boss"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, SHELL)
    assert view["mesh_as"] == "shell"
    (row,) = view["not_shelled"]
    assert row["id"] == "S1" and row["kind"] == "solid"
    assert row["words"] == ("A thick region of the part (5 faces) stays solid: it is 30 mm thick or more, too "
                            "thick for a shell.")
    assert row["at_mm"] == pytest.approx([100.0, 50.0, 22.0], abs=0.5)
    assert sc.settle(view)["mesh_as"] == "shell"


def test_shell_asked_on_a_chunky_part_is_a_solid_with_the_plain_reason(parts):
    """(d) A 50 x 40 x 30 block asked for as a shell: a solid, said in one sentence with the measured wall; the
    shell's list and legend are not shown. Never a refusal."""
    rec, drawn, _ = parts["block"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn)), CAD, SHELL)
    assert view["mesh_as"] == "solid" and view["label"] == "Solid"
    assert view["reason"].startswith("You asked for a shell, but no wall of this part is thin enough for one (")
    assert "30 mm thick" in view["reason"] and view["reason"].endswith("), so it is meshed as a solid.")
    assert view["reason"].count(". ") == 0                                 # one sentence
    assert "not_shelled" not in view and "overlay" not in view and view["measuring_words"] == ""
    settled = sc.settle(view)
    assert settled == {"mesh_as": "solid", "picked": "shell", "by": "words", "why": view["reason"]}
    assert sc.declaration_words(settled) == f"Meshed as: a solid. {view['reason']}"
    # with no measured detail: the plain sentence
    plain = {"bodies": [{"body": "Part_1", "verdict": "solid"}],
             "solid_regions": [{"body": "Part_1", "whole": True, "why": "no wall is thin against its size"}]}
    assert sc.shell_view(_ready(plain), CAD, SHELL)["reason"] == sc.NOTHING_TO_SHELL


def test_open_edges_are_listed_and_marked_on_a_said_shell():
    summary = {"bodies": [{"body": "Part_1", "verdict": "shell"}], "share": 1.0, "t_min_mm": 2.0, "t_max_mm": 2.0,
               "solid_regions": [], "open_edges": 3, "open_places": [{"at_mm": [1, 2, 3]}, {"at_mm": [4, 5, 6]}],
               "open_at_mm": [[1, 2, 3], [4, 5, 6], [4, 5, 7]], "overlay": True}
    view = sc.shell_view(_ready(summary), CAD, SHELL)
    assert view["mesh_as"] == "shell"
    words = [r["words"] for r in view["not_shelled"]]
    assert words[0] == "The plates do not meet at 2 places: shown in red. A shell mesh has a gap there."
    assert [r["id"] for r in view["not_shelled"]] == ["", "O1", "O2"] and view["open_edges"] == 3


def test_proceed_while_measuring_is_still_a_shell():
    """Proceed before the check lands: the said shell is settled now, and the run makes it itself."""
    view = sc.shell_view({"status": sc.MEASURING, "started_at": time.time()}, CAD, SHELL)
    assert view["state"] == "measuring" and view["measuring_words"] == sc.MEASURING_WORDS
    assert sc.settle(view)["mesh_as"] == "shell"
    # a check that never came back is read as stopped, and said: still a shell (the run extracts it)
    stale = sc.shell_view({"status": sc.MEASURING, "started_at": time.time() - sc.STALE_AFTER_S - 5}, CAD, SHELL)
    assert stale["state"] == "unavailable" and stale["mesh_as"] == "shell" and "took longer" in stale["reason"]
    # not started yet: measuring
    assert sc.shell_view(None, CAD, SHELL)["state"] == "measuring"


def test_a_triangle_file_asked_as_a_shell_is_a_solid_and_says_why():
    stl = {"source_format": "stl", "validity": {"kind": "triangles"}}
    view = sc.shell_view(None, stl, SHELL)
    assert view["state"] == "unavailable" and view["mesh_as"] == "solid" and view["reason"] == sc.TRIANGLES_WHY
    assert sc.settle(view) == {"mesh_as": "solid", "picked": "shell", "by": "words", "why": sc.TRIANGLES_WHY}


def test_a_unit_corrected_after_the_check_rereads_its_lengths(parts):
    rec, drawn, _ = parts["boss"]
    view = sc.shell_view(_ready(sc.summarise(rec, drawn), scale=0.001), {**CAD, "scale_to_m": 0.0254}, SHELL)
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
    assert sc.view_for(SID, CAD) is None                                  # nothing said: no shell view
    shelled = sc.view_for(SID, CAD, [{"role": "user", "content": "shell mesh"}])
    assert shelled["mesh_as"] == "shell" and shelled["overlay"]


def test_a_check_that_cannot_run_is_unavailable_and_the_shell_is_still_meshed(store, monkeypatch):
    def _boom(*a):
        raise ValueError("not a CAD file")
    monkeypatch.setattr(sc, "_cad_of", _boom)
    store.objects[f"sessions/{SID}/geometry_check/scout.json"] = json.dumps({"status": "ready", "facts": CAD}).encode()
    assert sc.run_shell_check(session_id=SID) == {"status": "unavailable"}
    view = sc.view_for(SID, CAD, [{"role": "user", "content": "shell mesh"}])
    assert view["state"] == "unavailable" and view["mesh_as"] == "shell"
    assert view["reason"].startswith("The mid-surface could not be drawn here (the part could not be read for it")


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


def test_the_check_starts_only_when_a_shell_was_asked_for(store):
    """(a)/(b) No worker time on a plain FEA part: the check starts only for a said shell, once, and only after the
    FEA naming stored its reading - a shell asked for after the naming starts it then."""
    from meshpipeline.contracts import geometry_check as seam

    got = []
    seam.set_shell_check_enqueuer(lambda **kw: got.append(kw) or True)
    named = {**CAD, "mesh_as_words": {"value": "", "by": "", "why": "no mesh-as words"}}
    try:
        assert not sc.start_if_asked(SID, "alice", facts=named, messages=[{"role": "user", "content": "FEA"}])
        assert got == [] and sc.read_status(SID) is None
        # before the naming: the naming itself starts it
        assert not sc.start_if_asked(SID, "alice", facts=CAD, messages=[{"role": "user", "content": "shell"}])
        later = [{"role": "user", "content": "FEA"}, {"role": "user", "content": "mesh it as a shell please"}]
        assert sc.start_if_asked(SID, "alice", facts=named, messages=later)
        assert len(got) == 1 and sc.read_status(SID)["status"] == sc.MEASURING
        # once
        assert not sc.start_if_asked(SID, "alice", facts=named, messages=later) and len(got) == 1
    finally:
        seam.set_shell_check_enqueuer(None)


def test_the_naming_starts_the_check_only_for_a_said_shell():
    """The FEA naming's call site: shell_check.start only behind wants_shell (geometry_check)."""
    import inspect

    from meshpipeline.application import geometry_check as gc

    src = inspect.getsource(gc)
    i = src.index("shell_check.start(session_id, owner_id")
    assert "if shell_check.wants_shell(facts):" in src[i - 200:i]


def test_the_confirmed_choice_is_read_into_the_run(store):
    assert sc.confirmed_mesh_as(SID) == "solid"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "shell"}).encode()
    assert sc.confirmed_mesh_as(SID) == "shell"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "mixed"}).encode()
    assert sc.confirmed_mesh_as(SID) == "mixed"
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "beam"}).encode()
    assert sc.confirmed_mesh_as(SID) == "solid"
    assert sc.confirmed_mesh_as("") == "solid"
    # a shell asked for in a request-card note after Proceed is a shell...
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps({"fea_mesh_as": "solid"}).encode()
    assert sc.confirmed_mesh_as(SID, [{"text": "mesh it as a shell"}]) == "shell"
    assert sc.confirmed_mesh_as(SID, [{"text": "40k elements"}]) == "solid"
    # ...unless the stage already found the shell asked for could not be made
    store.objects[f"sessions/{SID}/geometry_check/confirmed.json"] = json.dumps(
        {"fea_mesh_as": "solid", "fea_mesh_as_picked": "shell (words)"}).encode()
    assert sc.confirmed_mesh_as(SID, [{"text": "mesh it as a shell"}]) == "solid"


@pytest.mark.parametrize("settled,row", [("shell", "Shell (mid-surface, plate elements)"),
                                         ("mixed", "Shell + solid (thin walls shell, thick regions solid)"),
                                         ("solid", None)])
def test_the_request_card_says_a_shell_or_shell_and_solid(settled, row):
    """SHSTAGE-5 on the request card: the settled choice read back from the declaration's own head; a solid (the
    default) adds no row."""
    from types import SimpleNamespace

    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    from meshpipeline.application.intake_brief import build_brief

    said = sc.declaration_words({"mesh_as": settled, "why": "Because."})
    session = SimpleNamespace(request_txt="Modal analysis of this hopper.", purpose="structural", mesh_engine="gmsh",
                              messages=[{"role": "user", "content": "Modal analysis of this hopper."},
                                        {"role": "assistant", "content": f"{CONFIRMED_MARK} a solid part. {said}"}],
                              user_notes=[], intake_submitted=False)
    brief = build_brief(session)
    assert brief.get("mesh_as") == row
    assert sc.card_mesh_as("") == "" and sc.card_mesh_as("Meshed as: a solid.") == ""


@pytest.mark.parametrize("settled,words", [
    ("shell", "Shell: linear plates (4-node quads, a few 3-node triangles)"),
    ("mixed", "Thin walls: linear plates (4-node quads, a few 3-node triangles); thick regions and the solid mesh "
              "made beside it: element order 2"),
    ("solid", None)])
def test_the_request_cards_engine_settings_are_true_to_a_shell(settled, words):
    """A shell run's plates are linear (CQUAD4 / S4 / SHELL181): the card never says "element order: 2" as if they
    were quadratic - and no solid is made beside a shell (SHSTAGE-7), so no order is said for it. A solid run keeps
    the settings as they are."""
    from types import SimpleNamespace

    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    from meshpipeline.application.intake_brief import build_brief

    said = sc.declaration_words({"mesh_as": settled, "why": "Because."})
    session = SimpleNamespace(request_txt="Static analysis, as a shell.", purpose="structural", mesh_engine="gmsh",
                              engine_params={"element_order": "2"},
                              messages=[{"role": "user", "content": "Static analysis, as a shell."},
                                        {"role": "assistant", "content": f"{CONFIRMED_MARK} a solid part. {said}"}],
                              user_notes=[], intake_submitted=False)
    brief = build_brief(session)
    assert brief.get("engine_params_label") == words
    assert brief["engine_params"] == {"element_order": "2"}       # the settings themselves are unchanged
    assert sc.card_engine_settings(sc.CARD_LABELS["shell"], {"element_order": 2, "optimize": "netgen"}) == (
        f"{sc.SHELL_ELEMENTS}; optimize: netgen")
    assert sc.card_engine_settings(sc.CARD_LABELS["shell"], {}) == sc.SHELL_ELEMENTS


def test_two_small_faces_far_apart_are_a_length_not_a_wall_thickness():
    """A bar's two end caps pair across its length (255 mm apart, 25 mm wide): that is not a wall, so no
    "255 mm thick" is said for the region that stays solid."""
    from meshpipeline.application.shell_check import _is_wall
    assert _is_wall({"thickness_mm": 5.0, "width_mm": 80.0})
    assert not _is_wall({"thickness_mm": 255.0, "width_mm": 25.0})
    assert _is_wall({"thickness_mm": 30.0})          # no width measured: a wall, as before
    assert not _is_wall({"width_mm": 25.0})
