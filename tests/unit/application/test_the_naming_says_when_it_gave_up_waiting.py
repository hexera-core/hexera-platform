# Responsibility: Verify the naming records giving up on a preview, the one give-up that reaches a
# customer and the one that said nothing.
# Boundaries: application/geometry_check._wait_for_scout and the determinism record it writes.
"""GIVING UP ON A PREVIEW WAS THE ONE CASE THAT SAID NOTHING.

`_wait_for_scout` waits for the scout's full reading and, when its own limit runs out, names
whatever is stored. It recorded that only when the stored record was missing or still pending. The
case it did NOT record is the one that reaches a customer: a PREVIEW is stored, so the naming
builds the whole proposal from the quick drawing - `openings: []` and `confidence.input_kind: 0.0`
by construction (`preview_facts`) - and publishes it as ready.

MEASURED on the first customer assembly, 2026-10-30 (ColdPlateAssembly_CLEAN.step, 15,883,539
bytes, 6 solids, 9,226 faces), session fb5e5589:

    naming waited      1,016 s (its own soft limit 1,076 less a minute)
    the scout needed   1,303.9 s (reading 34.6, measuring 711.1, then the openings)
    so the naming      named the preview and stored it ready at 1,022.2 s
    the customer saw   a finished check, 0 openings, no reason, for 5 min 16 s

The scout's real reading - 3 openings, 52,144.56 mm3 of coolant - landed afterwards and
`merged_onto_full` put it under the naming, which is why the stored record is right now and was
wrong then. Nothing in the record said a clock had decided that, and `determinism` is the module
whose stated job is to record "every time a wall clock changed a decision".
"""
from __future__ import annotations

import pytest

from meshpipeline.application import geometry_check as GC
from meshpipeline.engines import determinism as D


def _preview_record() -> dict:
    return {"status": GC.STATUS_SCOUTED, "preview": GC.PREVIEW_RUNNING,
            "facts": {"openings": [], "confidence": {"input_kind": 0.0}, "preview": True}}


def _full_record() -> dict:
    return {"status": GC.STATUS_SCOUTED,
            "facts": {"openings": [{"id": 1}, {"id": 2}, {"id": 3}],
                      "confidence": {"input_kind": 0.7}}}


@pytest.fixture
def notes(monkeypatch):
    """Every note_time_limit the call under test writes."""
    seen: list[tuple[str, str, dict]] = []

    def _note(where: str, decision: str, **facts):
        seen.append((where, decision, facts))

    monkeypatch.setattr(D, "note_time_limit", _note)
    monkeypatch.setattr("meshpipeline.engines.determinism.note_time_limit", _note)
    return seen


def _no_wait(monkeypatch):
    """The deadline is already gone, so the wait decides on its first look."""
    monkeypatch.setattr(GC, "NAMING_WAIT_S", 0.0)
    monkeypatch.setattr(GC, "naming_limits_for", lambda _sid: (0.0, 0.0))


def test_giving_up_on_a_preview_is_recorded(monkeypatch, notes):
    """The whole point. The proposal that goes out is built from the quick drawing, so the record
    has to say a clock chose that."""
    _no_wait(monkeypatch)
    monkeypatch.setattr(GC, "read_check", lambda _sid: _preview_record())
    got = GC._wait_for_scout("s1")
    assert GC.is_preview(got), "the preview should still be returned; the naming names it"
    assert notes, "the naming gave up on a preview and recorded nothing"
    where, decision, facts = notes[-1]
    assert where == "geometry_check.naming"
    assert "preview" in decision.lower(), decision
    assert "budget_s" in facts


def test_a_missing_or_pending_record_is_still_recorded(monkeypatch, notes):
    """The two cases that were already recorded stay recorded."""
    _no_wait(monkeypatch)
    monkeypatch.setattr(GC, "read_check", lambda _sid: None)
    GC._wait_for_scout("s2")
    assert notes, "a missing record stopped being recorded"

    notes.clear()
    monkeypatch.setattr(GC, "read_check", lambda _sid: {"status": GC.STATUS_PENDING})
    GC._wait_for_scout("s3")
    assert notes, "a pending record stopped being recorded"


def test_the_full_reading_is_not_a_give_up(monkeypatch, notes):
    """When the scout's full reading is there, no clock changed anything and nothing is recorded."""
    _no_wait(monkeypatch)
    monkeypatch.setattr(GC, "read_check", lambda _sid: _full_record())
    got = GC._wait_for_scout("s4")
    assert not GC.is_preview(got)
    assert not notes, "a reading that arrived in time was recorded as a time limit"
