# Responsibility: Verify that a review REJECTION returns a MEASURED objection - a field name from a
#                 closed tuple plus the value the SESSION measured for it - and that the reviewer's
#                 own prose can never reach the author through it.
"""Hermetic: the model call is a fake `package_review._ask` (patched where the module looks it up),
so nothing here reaches a router, a kernel or a network.

The property under test is a SEAL, not a feature: the author is told that a package was rejected by
review and, with the objection live, WHICH MEASURED FIELD the reviewer objected to and what the
session measured there. It is never told what the reviewer said. So every test below that touches a
reject asserts on the whole objection, not only on the part it cares about, and the hostile case
walks every string in the result looking for the reviewer's words.
"""
from __future__ import annotations

import json

import pytest
from tests.unit.cad.test_repair_package_review import FakeAsk

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import package_review as pr

#: Reviewer prose, in every shape a reply can carry it. NONE of this may appear in an objection.
_PROSE = "the fillet at the shaft shoulder was DELETED to get past the mesher"
_PROSE_WORDS = tuple(w for w in _PROSE.split() if len(w) > 3)

#: The session's own measurements - what an objection's `measured` must come from.
_EVIDENCE = {
    "area_mm2": {"declared": -2.0, "measured": -41.5},
    "volume_mm3": {"declared": 0.0, "measured": -1234.5},
    "faces": {"before": 20, "after": 17},
    "solids": {"before": 2, "after": 1},
    "deviation_mm": 0.75,
    "deviation_status": pr.DEVIATION_MEASURED,
    "mesh_before": ["mesh_edge_through_face"],
    "mesh_after": [],
    "defects_cleared": ["mesh_edge_through_face"],
    "defects_revealed": ["mesh_overlapping_facets"],
    "defects_remaining": [],
    # The free-text evidence keys, present exactly so the tests can prove they are unreachable.
    "ops": [{"op": "delete_face", "status": "kept", "reason": _PROSE}],
    "author_claim": _PROSE,
    "review_rejects": {"count": 1, "reasons": [_PROSE]},
}


@pytest.fixture
def live(monkeypatch):
    """The objection posture live, with a fake reviewer."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    monkeypatch.setattr(pr, "_configured", lambda: True)

    def install(*replies):
        fake = FakeAsk(*replies)
        monkeypatch.setattr(pr, "_ask", fake)
        return fake
    return install


@pytest.fixture
def off(monkeypatch):
    """The posture absent ALTOGETHER - the state of the settings module today."""
    monkeypatch.delattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", raising=False)
    monkeypatch.setattr(pr, "_configured", lambda: True)

    def install(*replies):
        fake = FakeAsk(*replies)
        monkeypatch.setattr(pr, "_ask", fake)
        return fake
    return install


def _strings(value) -> list[str]:
    """Every string anywhere in a value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (_strings(k) + _strings(v))]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


# THE SEAL


def test_objection_carries_no_reviewer_text(live):
    """A reply dense with prose - in the reason, in the objection's own keys and in the field name
    itself - yields an objection whose every string is either a name this module chose or a value
    the session measured."""
    live({"verdict": "reject", "reason": _PROSE,
          "objection": {"field": f"deviation_mm; {_PROSE}"}})
    verdict, reason, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
    assert verdict == "reject"
    # The reason still reaches the SESSION (the report, and the reviewer's own next evidence)...
    assert reason == _PROSE
    # ...and not the author, through the objection.
    assert objection == {"field": "unspecified", "measured": pr.UNMEASURED}
    blob = json.dumps(objection)
    for word in _PROSE_WORDS:
        assert word not in blob, f"reviewer word {word!r} leaked into the objection"
    allowed = set(pr.OBJECTION_FIELDS) | {"field", "measured", pr.UNMEASURED,
                                          pr.OBJECTION_UNSPECIFIED}
    assert all(s in allowed for s in _strings(objection))


def test_a_well_formed_objection_still_carries_only_names_and_numbers(live):
    live({"verdict": "reject", "reason": _PROSE, "objection": {"field": "deviation_mm"}})
    _, _, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
    assert objection == {"field": "deviation_mm", "measured": 0.75}
    assert set(objection) == {"field", "measured"}


def test_the_free_text_evidence_keys_are_not_objectionable(live):
    """`ops`, `author_claim` and `review_rejects` are evidence - and are NOT in OBJECTION_FIELDS, so
    naming one is as unknown as naming nonsense. This is the second lock on the seal: even an
    objection field drawn from the evidence cannot be a free-text one."""
    for field in ("ops", "author_claim", "review_rejects"):
        assert field in _EVIDENCE and field not in pr.OBJECTION_FIELDS
        live({"verdict": "reject", "reason": "r", "objection": {"field": field}})
        _, _, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
        assert objection == {"field": "unspecified", "measured": pr.UNMEASURED}
        assert _PROSE not in json.dumps(objection)


def test_objection_value_is_the_sessions_measurement(live):
    """A reviewer that cites its own number hands the author the SESSION'S number."""
    live({"verdict": "reject", "reason": "the volume fell by 3 mm3",
          "objection": {"field": "volume_mm3"}})
    _, _, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
    assert objection == {"field": "volume_mm3",
                         "measured": {"declared": 0.0, "measured": -1234.5}}


def test_measured_is_never_read_from_the_reply_whatever_the_reply_says():
    """`_objection_of` directly, with a reply that offers its own `measured` everywhere it could:
    the value still comes from the evidence. Pinned at this level because the strict
    `{"field"}`-only schema would otherwise be the only thing stopping it, and a later task that
    widens the schema must not quietly open this door."""
    reply = {"verdict": "reject", "reason": _PROSE, "measured": _PROSE,
             "objection": {"field": "deviation_mm"}}
    assert pr._objection_of(reply, _EVIDENCE) == {"field": "deviation_mm", "measured": 0.75}
    assert _PROSE not in json.dumps(pr._objection_of(reply, _EVIDENCE))


def test_a_field_the_evidence_does_not_carry_is_unmeasured_never_zero(live):
    live({"verdict": "reject", "reason": "r", "objection": {"field": "deviation_faces"}})
    assert "deviation_faces" not in _EVIDENCE
    _, _, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
    assert objection == {"field": "deviation_faces", "measured": pr.UNMEASURED}


@pytest.mark.parametrize("raw", [
    {"field": "no_such_field"},
    {"field": "deviation_mm", "note": "and the fillet"},   # an extra key
    {"field": 7},
    {"field": None},
    {},
    "deviation_mm",
    ["deviation_mm"],
    None,
])
def test_unknown_field_is_unspecified(live, raw):
    live({"verdict": "reject", "reason": "r", "objection": raw})
    _, _, objection = pr.review_package_objection(_EVIDENCE, timeout_s=5.0)
    assert objection == {"field": "unspecified", "measured": pr.UNMEASURED}


def test_an_accept_has_nothing_to_object_to(live):
    live({"verdict": "accept", "reason": "nothing was removed"})
    assert pr.review_package_objection(_EVIDENCE, timeout_s=5.0) == (
        "accept", "nothing was removed", None)


def test_every_no_answer_reject_carries_the_unnamed_objection(live, monkeypatch):
    """A no-answer named no field, so it must not invent one - and must not be None either: the
    author is still owed the fact that review rejected it."""
    unnamed = {"field": "unspecified", "measured": pr.UNMEASURED}
    monkeypatch.setattr(pr, "_configured", lambda: False)
    assert pr.review_package_objection({}, timeout_s=5.0) == (
        "reject", pr.NOT_CONFIGURED, unnamed)
    monkeypatch.setattr(pr, "_configured", lambda: True)
    live(None)
    assert pr.review_package_objection({}, timeout_s=5.0) == ("reject", pr.NO_REPLY, unnamed)
    live()
    assert pr.review_package_objection({}, timeout_s=5.0, deadline_s=10.0,
                                       clock=lambda: 11.0) == ("reject", pr.NO_TIME, unnamed)


def test_the_objection_is_bounded(live):
    """One oversized evidence field cannot become the author's whole next observation."""
    big = {**_EVIDENCE, "defects_cleared": [f"mesh_code_{i}" for i in range(200)],
           "deviation_status": "x" * 500}
    live({"verdict": "reject", "reason": "r", "objection": {"field": "defects_cleared"}})
    _, _, objection = pr.review_package_objection(big, timeout_s=5.0)
    assert len(objection["measured"]) == pr.OBJECTION_ITEMS_MAX
    live({"verdict": "reject", "reason": "r", "objection": {"field": "deviation_status"}})
    _, _, objection = pr.review_package_objection(big, timeout_s=5.0)
    assert len(objection["measured"]) == pr.OBJECTION_TEXT_MAX


# THE POSTURE


def test_posture_off_ignores_the_objection_key(off):
    """FLAG OFF IS TODAY, BYTE FOR BYTE. The schema is still EXACTLY {verdict, reason}, so a reply
    carrying an objection is the malformed reject it is today, and no objection comes back."""
    off({"verdict": "accept", "reason": "ok", "objection": {"field": "deviation_mm"}})
    assert pr.review_package_objection(_EVIDENCE, timeout_s=5.0) == (
        "reject", "malformed reply: not exactly {verdict, reason}", None)
    off({"verdict": "reject", "reason": _PROSE})
    assert pr.review_package_objection(_EVIDENCE, timeout_s=5.0) == ("reject", _PROSE, None)
    assert pr.objection_posture() == "off"


@pytest.mark.parametrize("raw", ["off", "LIVE", "Live", "", "shadow", None])
def test_any_posture_but_live_reads_off(monkeypatch, raw):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", raw, raising=False)
    assert pr.objection_posture() == "off"


def test_review_package_keeps_its_pre_objection_signature(live):
    """The existing caller (session.py `_review`) is untouched by this task."""
    live({"verdict": "reject", "reason": _PROSE, "objection": {"field": "deviation_mm"}})
    assert pr.review_package(_EVIDENCE, timeout_s=5.0) == ("reject", _PROSE)


def test_objection_fields_is_a_closed_tuple_of_measured_evidence_keys():
    assert isinstance(pr.OBJECTION_FIELDS, tuple)
    assert len(set(pr.OBJECTION_FIELDS)) == len(pr.OBJECTION_FIELDS)
    assert pr.OBJECTION_UNSPECIFIED not in pr.OBJECTION_FIELDS
    assert set(pr.OBJECTION_FIELDS) == {
        "area_mm2", "volume_mm3", "faces", "solids", "deviation_mm", "deviation_samples",
        "deviation_faces", "deviation_faces_skipped", "deviation_status", "mesh_before",
        "mesh_after", "defects_cleared", "defects_revealed", "defects_remaining"}


def test_every_objection_field_is_a_key_evidence_of_can_produce(monkeypatch):
    """OBJECTION_FIELDS is read off `evidence_of`'s own output, not written beside it."""
    from tests.unit.cad.test_repair_package_review import _BEFORE, _delta, _package

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live", raising=False)
    evidence = pr.evidence_of(
        _package(), _delta(),
        {"deviation_mm": 0.5, "deviation_samples": 9, "deviation_faces": 2,
         "deviation_faces_skipped": 0},
        {"codes": ["mesh_edge_through_face"], "mesh_kinds": ["mesh_edge_through_face"]},
        {"codes": ["mesh_overlapping_facets"], "mesh_kinds": ["mesh_overlapping_facets"]},
        original=_BEFORE)
    missing = [f for f in pr.OBJECTION_FIELDS if f not in evidence]
    assert not missing, f"not evidence_of keys: {missing}"


# TWO REJECTIONS STILL ESCALATE - with the objection live, the loop is unchanged.


def test_two_rejections_still_escalate(monkeypatch):
    from tests.unit.cad.test_repair_package_review import _loop

    from meshpipeline.cad.repair.session import REVIEW_REJECTED_REASON, REVIEW_TWICE_REASON

    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    run, fake, asked = _loop(monkeypatch, *(
        {"verdict": "reject", "reason": _PROSE, "objection": {"field": "deviation_mm"}},) * 6)
    assert not any(p.committed for p in run.packages)
    assert run.escalated == (0, 1)
    assert asked == [0, 0, 1, 1] and len(fake.calls) == 4
    assert [p.reason for p in run.packages] == [
        REVIEW_REJECTED_REASON, REVIEW_TWICE_REASON] * 2
    # And the GENERIC reason is still all the author's attempt log carries today.
    assert all(_PROSE not in str(p.reason) for p in run.packages)


@pytest.mark.skip(reason="cross-lane seam: session.PackageVerdict.review_objection and "
                         "observe._attempt_of are owned by another lane this round. This module "
                         "exports review_package_objection(); when the session adopts it, assert "
                         "here that the investigator's re-call turn carries review_objection and "
                         "no substring of review_reason.")
def test_investigator_sees_the_objection_on_recall():
    raise AssertionError("unimplemented: see skip reason")
