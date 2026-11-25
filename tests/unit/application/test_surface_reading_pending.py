# Responsibility: Verify SURF-18 on the FEA stage: a CAD file with no solid whose surface reading is still to come
# waits (no "cannot be meshed", no way out, the shell fold says it is reading the surface); once the reading is kept it
# proceeds as a shell; a measurement that ended with no reading says what stopped it, never "coming"; a triangle file
# that is an open surface and a CAD file whose faces close see the stage they saw before.
# Boundaries: pure over hand-built facts and messages; nothing is read or measured.
from __future__ import annotations

from meshpipeline.application import fea_check, shell_check, surface_check
from meshpipeline.cad.fea_measure import FACT_KEY

OPEN_SHELL = {"clean": False, "kind": "brep", "summary": "The geometry is not clean: the surface is not closed.",
              "reasons": [{"code": "open_shell", "severity": "error", "message": "the surface is not closed"}]}
SHEET = {"kind": "surface", "why": "it holds no solid and its faces do not close", "faces": 11, "sheets": 1,
         "open_share": 0.74, "sew_tolerance_mm": 0.019, "free_edges": 14, "unmet_edges": 0, "size_mm": 149.0}
MSGS = [{"role": "user", "content": "FEA shell mesh"}]


def _facts(record: dict | None = None, **extra) -> dict:
    facts = {"source_format": "iges", "solids": 0, "scale_to_m": 0.001, "validity": OPEN_SHELL, **extra}
    if record is not None:
        facts[FACT_KEY] = record
    return facts


def _no_dead_end(words: str) -> None:
    assert "coming" not in words and "closed solids only" not in words


def test_while_the_reading_is_to_come_the_stage_waits_and_says_why():
    facts = _facts()
    blocked, kind, way_out = fea_check.blocker(facts, MSGS)
    assert (blocked, kind, way_out) == (surface_check.PENDING_WORDS, "surface_reading", "")
    assert blocked == ("This file has no closed solid, so it is being read as a surface to mesh as a shell. This can "
                       "take a few minutes; Proceed opens when it is done.")
    view = fea_check.fea_view(facts, MSGS)
    # no stop and no way out drawn: Proceed waits, the hint says why
    assert view["single_part_only"] == "" and view["way_out"] == "" and view["can_proceed"] is False
    assert view["waiting"] == surface_check.PENDING_WORDS and view["blocked_kind"] == "surface_reading"
    assert view["clean_short"] == "No closed solid - reading it as a surface"
    _no_dead_end(" ".join(str(v) for v in view.values()))


def test_while_the_reading_is_to_come_the_shell_fold_does_not_say_proceed_now():
    fold = surface_check.view(_facts(), MSGS)
    assert fold is not None and fold["state"] == "measuring" and fold["mesh_as"] == "shell" and fold["surface"]
    assert fold["label"] == "No closed solid: it is being read as a surface, to mesh as a shell."
    assert fold["measuring_words"] == "This can take a few minutes."
    assert fold["measuring_words"] != shell_check.MEASURING_WORDS and "proceed now" not in str(fold)


def test_once_the_reading_is_kept_the_surface_proceeds_as_a_shell():
    facts = _facts({"measured": True, "basis": "cad", "bodies": 1, surface_check.KEY: SHEET})
    assert fea_check.blocker(facts, MSGS) == ("", "", "")
    view = fea_check.fea_view(facts, MSGS)
    assert view["can_proceed"] is True and view["waiting"] == "" and view["single_part_only"] == ""
    fold = surface_check.view(facts, MSGS)
    assert fold["state"] == "ready" and fold["label"].startswith("This file is a surface, not a solid. It is meshed "
                                                                 "as a shell")


def test_a_measurement_with_no_reading_says_the_faces_could_not_be_read():
    facts = _facts({"measured": True, "basis": "cad", "bodies": 1})
    blocked, kind, way_out = fea_check.blocker(facts, MSGS)
    assert kind == "surface_unread"
    assert blocked == ("This file has no closed solid, and it could not be read as a surface to mesh as a shell: its "
                       "faces could not be read as one surface.")
    assert way_out == surface_check.UNREAD_WAY_OUT
    _no_dead_end(blocked + way_out)
    view = fea_check.fea_view(facts, MSGS)
    assert view["single_part_only"] == blocked and view["can_proceed"] is False and view["waiting"] == ""
    assert surface_check.view(facts, MSGS) is None


def test_a_skipped_measurement_says_what_stopped_it():
    facts = _facts({"measured": False, "reason": "the file is 300 MB, over the 200 MB measured for a single part"})
    blocked, kind, _ = fea_check.blocker(facts, MSGS)
    assert kind == "surface_unread"
    assert blocked.endswith("to mesh as a shell: the file is 300 MB, over the 200 MB measured for a single part.")
    _no_dead_end(blocked)


def test_a_check_that_stopped_before_the_measurement_does_not_wait_forever():
    blocked, kind, _ = fea_check.blocker(_facts(), MSGS, live=False)
    assert kind == "surface_unread" and blocked.endswith(": the check stopped before the surface was read.")
    assert fea_check.fea_view(_facts(), MSGS, live=False)["waiting"] == ""
    assert surface_check.view(_facts(), MSGS, live=False) is None


def test_only_a_running_check_is_live():
    assert fea_check.check_live({"status": "scouted"}) and fea_check.check_live({"status": "pending"})
    assert not fea_check.check_live({"status": "ready"}) and not fea_check.check_live({"status": "failed"})


def test_an_open_triangle_surface_keeps_its_words():
    tri = {"clean": False, "kind": "triangles", "summary": "", "reasons": [
        {"code": "open_boundary", "severity": "error", "message": "open edges"}]}
    facts = {"source_format": "stl", "solids": 1, "validity": tri}
    blocked, kind, way_out = fea_check.blocker(facts, MSGS)
    assert kind == "surface" and "shell meshes for surfaces like this are coming" in blocked
    assert way_out == fea_check.WAY_OUT_SOLID


def test_a_cad_file_whose_faces_close_waits_for_nothing():
    facts = _facts(validity={"clean": True, "kind": "brep", "summary": "", "reasons": []})
    assert fea_check.blocker(facts, MSGS) == ("", "", "")
    assert surface_check.view(facts, MSGS) is None
    assert fea_check.fea_view(facts, MSGS)["waiting"] == ""
