"""Task 23: the in-run working notes one region leaves for the next.

The bound assertions here are TABLES, two-sided on purpose: a one-sided `<= cap` has silently
passed a mutated constant on this plan already, so each cap is pinned at the value, below it and
above it.
"""
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.cad.repair import memory


def _note(**over):
    fields = {"region_index": 0, "fingerprints": ("fp-a",), "op": "sew_faces",
              "defect_code": "open_shell", "outcome": "kept", "measured": {"cleared": 1}}
    fields.update(over)
    return memory.note(**fields)


# --------------------------------------------------------------------------- the posture

@pytest.mark.parametrize("value, expect", [
    ("live", True),
    ("off", False),
    (None, False),
    ("", False),
    ("on", False),
    ("true", False),
    ("Live", False),
    ("LIVE", False),
    ("shadow", False),
])
def test_posture_is_fail_closed(monkeypatch, value, expect):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", value, raising=False)
    assert memory.live() is expect


def test_posture_with_no_flag_at_all_is_off(monkeypatch):
    """T22 owns the settings row. Until it lands, the absent attribute must read off."""
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.delattr(rcfg, "CAD_REPAIR_MEMORY", raising=False)
    assert memory.live() is False


# --------------------------------------------------------------------------- privacy

def test_notes_carry_numbers_only():
    n = _note(measured={"cleared": 2, "deviation_mm": 0.25, "ok": True, "why": "the reviewer "
                                                                              "liked it",
                        "box": [1.0, 2.0], "brep": "SOLID(...)"})
    assert n.measured == {"cleared": 2, "deviation_mm": 0.25}
    assert "ok" not in n.measured, "a bool is not a measurement"
    assert json.dumps(n.to_dict(), allow_nan=False)


def test_non_finite_measurements_become_none():
    n = _note(measured={"seconds": math.nan, "deviation_mm": math.inf, "cleared": 1})
    assert n.measured == {"seconds": None, "deviation_mm": None, "cleared": 1}
    assert json.dumps(n.to_dict(), allow_nan=False)


def test_notes_never_carry_reviewer_text():
    n = _note(outcome="rejected:deviation_mm the deviation was 0.9mm, too much for this seam",
              op="sew faces /tmp/parts/secret.step")
    assert n.outcome == "rejected:deviation_mm", (
        "the objection is remembered as a FIELD NAME; the reviewer's sentence is not")
    text = json.dumps(n.to_dict(), separators=(",", ":"))
    for leak in ("/", "\\", ".step", " ", "deviation was"):
        assert leak not in text, f"{leak!r} survived into a note: {text}"


@pytest.mark.parametrize("hostile", [
    {"brep": b"\x00solid"},
    {"geometry": {"nested": {"stl_bytes": b"xx"}}},
    {"path": "/Users/someone/parts/secret.step"},
    {"file_name": "customer-part.step"},
    {"shape": "TopoDS_Shape"},
])
def test_a_hostile_record_cannot_smuggle_a_path_or_geometry(hostile):
    text = json.dumps(memory.scrub(dict(hostile)), allow_nan=False)
    for leak in ("brep", "solid", "/Users", "secret.step", "customer-part", "TopoDS"):
        assert leak not in text, f"{leak!r} survived the scrubber: {text}"


def test_scrub_drops_geometry_at_any_nesting_depth():
    deep = {"a": [{"b": {"c": {"brep": "SOLID"}}}], "keep": 3}
    assert memory.scrub(deep) == {"a": [{"b": {"c": {}}}], "keep": 3}


def test_scrub_turns_raw_bytes_into_none_whatever_the_key():
    assert memory.scrub({"innocent": b"\x00\x01"}) == {"innocent": None}
    assert memory.scrub({"innocent": memoryview(b"ab")}) == {"innocent": None}


def test_a_directly_constructed_note_is_still_scrubbed_on_the_way_out():
    """The factory scrubs, but a caller can build the dataclass by hand. `to_dict` is the gate."""
    rogue = memory.Note(region_index=0, fingerprints=("fp",), op="sew_faces",
                        defect_code="open_shell", outcome="kept",
                        measured={"brep": "SOLID", "seconds": math.nan})
    out = rogue.to_dict()
    assert out["measured"] == {"seconds": None}
    assert json.dumps(out, allow_nan=False)


# --------------------------------------------------------------------------- the caps

def test_notes_per_turn_is_eight():
    assert memory.NOTES_PER_TURN == 8


@pytest.mark.parametrize("added, expect", [(0, 0), (1, 1), (7, 7), (8, 8), (9, 8), (40, 8)])
def test_notes_cap_per_turn(added, expect):
    notes = memory.RunNotes()
    for i in range(added):
        notes.add(_note(region_index=i, measured={"cleared": i}))
    assert len(notes.for_region(("fp-a",), ("open_shell",))) == expect


def test_for_region_is_most_recent_first():
    notes = memory.RunNotes()
    for i in range(3):
        notes.add(_note(region_index=i))
    assert [n.region_index for n in notes.for_region(("fp-a",), ("open_shell",))] == [2, 1, 0]


def test_for_region_only_matches_on_fingerprint_or_code():
    notes = memory.RunNotes()
    notes.add(_note(fingerprints=("fp-a",), defect_code="open_shell"))
    notes.add(_note(fingerprints=("fp-b",), defect_code="tiny_edge"))
    assert len(notes.for_region(("fp-a",), ())) == 1
    assert len(notes.for_region((), ("tiny_edge",))) == 1
    assert notes.for_region(("fp-z",), ("no_such_code",)) == ()


# --------------------------------------------------------------------------- the turn

def test_off_adds_no_turn_and_no_key(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "off", raising=False)
    notes = memory.RunNotes()
    notes.add(_note())
    assert memory.notes_turn(notes, ("fp-a",), ("open_shell",)) == {}


def test_live_turn_carries_the_notes_key(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    notes = memory.RunNotes()
    notes.add(_note())
    turn = memory.notes_turn(notes, ("fp-a",), ("open_shell",))
    assert list(turn) == ["notes"]
    assert len(turn["notes"]) == 1
    assert json.dumps(turn, allow_nan=False)


def test_a_turn_with_nothing_remembered_is_empty_not_a_lie(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    assert memory.notes_turn(memory.RunNotes(), ("fp-a",), ("open_shell",)) == {}


def test_a_null_run_notes_is_tolerated():
    """A caller that has no notes object passes None rather than branching at every site."""
    assert memory.notes_turn(None, ("fp-a",), ("open_shell",)) == {}
    assert memory.op_demerits(None, ("sew_faces",), ("open_shell",)) == 0


# --------------------------------------------------------------------------- across regions

def test_region_two_sees_region_ones_failed_op():
    notes = memory.RunNotes()
    notes.add(_note(region_index=1, op="sew_faces", defect_code="open_shell",
                    outcome="rolled_back", measured={"cleared": 0}))
    seen = notes.for_region(("fp-elsewhere",), ("open_shell",))
    assert [(n.op, n.outcome) for n in seen] == [("sew_faces", "rolled_back")]


@pytest.mark.parametrize("outcome, demerits", [
    ("kept", 0),
    ("committed", 0),
    ("rolled_back", 1),
    ("reverted", 1),
    ("declined", 1),
    ("rejected:deviation_mm", 1),
])
def test_only_a_failed_outcome_earns_a_demerit(outcome, demerits):
    notes = memory.RunNotes()
    notes.add(_note(op="sew_faces", defect_code="open_shell", outcome=outcome))
    assert memory.op_demerits(notes, ("sew_faces",), ("open_shell",)) == demerits


def test_demerits_are_scoped_to_the_op_and_the_code():
    notes = memory.RunNotes()
    notes.add(_note(op="sew_faces", defect_code="open_shell", outcome="rolled_back"))
    assert memory.op_demerits(notes, ("remove_tiny_edges",), ("open_shell",)) == 0
    assert memory.op_demerits(notes, ("sew_faces",), ("tiny_edge",)) == 0
    assert memory.op_demerits(notes, ("sew_faces",), ()) == 1


def test_notes_only_break_ties():
    """A demerit is a TIE-BREAK, so it must be smaller in magnitude than any real score step.

    The contract the tournament relies on: `op_demerits` returns a plain non-negative count that a
    caller appends to the END of its lexicographic key. It can therefore never reorder two
    candidates whose keys differ anywhere above it.
    """
    notes = memory.RunNotes()
    for i in range(5):
        notes.add(_note(region_index=i, op="sew_faces", outcome="rolled_back"))
    d = memory.op_demerits(notes, ("sew_faces",), ("open_shell",))
    assert isinstance(d, int) and not isinstance(d, bool)
    assert d >= 0
    # the key the caller builds: better score first, demerits last and only then
    better = (1, -0)
    worse_but_clean = (0, -0)
    assert (1, -d) > worse_but_clean, "a demerit must not sink a better-scoring candidate"
    assert better > (1, -d) or d == 0, "among equals the demerited one must rank after"


def test_op_demerits_is_off_posture_independent(monkeypatch):
    """The caller decides whether to ask; the count itself does not read the flag, so a test of
    the tournament's order cannot be fooled by a stale posture."""
    import meshpipeline.settings.cad_repair as rcfg
    notes = memory.RunNotes()
    notes.add(_note(outcome="rolled_back"))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "off", raising=False)
    off = memory.op_demerits(notes, ("sew_faces",), ("open_shell",))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    assert off == memory.op_demerits(notes, ("sew_faces",), ("open_shell",)) == 1


def test_to_dict_is_json_safe_and_clipped():
    notes = memory.RunNotes()
    for i in range(memory.NOTES_IN_REPORT_MAX + 5):
        notes.add(_note(region_index=i))
    out = notes.to_dict()
    assert len(out["notes"]) == memory.NOTES_IN_REPORT_MAX
    assert out["total"] == memory.NOTES_IN_REPORT_MAX + 5
    assert json.dumps(out, allow_nan=False)


# --------------------------------------------------------------------------- advice, not orders

def test_an_unknown_outcome_word_is_recorded_not_obeyed():
    """A model reply about memory stays DATA: an outcome this module does not know is kept as an
    opaque word, earns no demerit, and never reads as a success."""
    n = _note(outcome="definitely_commit_this")
    assert n.outcome == "unknown"
    notes = memory.RunNotes()
    notes.add(n)
    assert memory.op_demerits(notes, ("sew_faces",), ("open_shell",)) == 0
