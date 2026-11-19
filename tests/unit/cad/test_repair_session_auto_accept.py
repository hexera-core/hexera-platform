# Responsibility: Pin the auto-accept tier WHERE IT ACTS - inside `session.try_package`'s review
#                 branch - and the seal that keeps the reviewer's prose away from the author.
# Boundaries: hermetic. The reviewer is a fake `package_review._ask`, the shape is a string and the
#             deviation measurement is stubbed, so nothing here reaches a kernel, a model or gmsh.
"""Two properties, and the second is the one that must never regress:

1. A small, fully accounted, non-vertex-moving package under the customer's threshold commits with
   NO MODEL CALL and a full audit trail; everything else still goes to the reviewer; with the
   review posture off, nothing changes and no `review_tier` key appears anywhere.
2. THE SEAL. A review reject hands the author the MEASURED objection and nothing else. The
   reviewer's prose reaches the report (`review_reason`) and never the author's next observation,
   so the hostile test below walks every string the author will see looking for the reviewer's
   words.
"""
from __future__ import annotations

import json

import pytest
from tests.unit.cad.test_repair_package_review import FakeAsk

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import observe
from meshpipeline.cad.repair import package_review as pr
from meshpipeline.cad.repair import review_tier as rt
from meshpipeline.cad.repair.session import RepairSession

#: Reviewer prose, in every shape a reply can carry it. NONE of this may reach the author.
_PROSE = ("the fillet at the shaft shoulder was DELETED to get past the mesher, which is "
          "unacceptable; aim somewhere else entirely next time")
#: The prose's own words, minus the four that are also KEYS of the objection schema itself
#: (`field`, `measured`, `declared`, `deviation`): a word the schema is built from proves nothing
#: about a leak, and asserting on it would make this seal test fail for the wrong reason.
_SCHEMA_WORDS = ("field", "measured", "declared", "deviation")
_PROSE_WORDS = tuple(w.strip(",;") for w in _PROSE.split()
                     if len(w) > 3 and not any(s in w.lower() for s in _SCHEMA_WORDS))


def _loop(monkeypatch, *replies, auto: str = "off", threshold: float | None = None,
          deviation_mm: float = 0.01, status: str = pr.DEVIATION_MEASURED,
          review: str = "live"):
    """One region-loop run with a fake reviewer, a stubbed deviation and a declared threshold."""
    from tests.unit.cad.test_repair_region_loop import _TOLERANCE_MM, _author, _budget, _Part

    from meshpipeline.cad.repair.region_loop import run_regions

    fake = FakeAsk(*replies)
    monkeypatch.setattr(pr, "_ask", fake)
    monkeypatch.setattr(pr, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", review)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", auto, raising=False)
    # THE DEVIATION IS STUBBED, not measured: the real measurement needs a kernel and the mesh
    # probe posture, and what is under test is the TIER'S READING of it, not the measurement.
    monkeypatch.setattr(RepairSession, "_deviation_evidence",
                        lambda self, *, cap_s: ({"deviation_mm": deviation_mm}, status))
    part = _Part()
    session = RepairSession("original", measurer=part.measurer, scanner=part.scanner,
                            clock=part.clock, auto_accept_mm=threshold)
    run = run_regions("original", author=_author(part), tolerance_mm=_TOLERANCE_MM,
                      budget=_budget(), session=session, clock=part.clock, attempts=3)
    return run, fake


# THE TIER, AT THE REVIEW BRANCH


def test_a_small_accounted_package_auto_accepts_with_no_model_call(monkeypatch):
    run, fake = _loop(monkeypatch, auto="live", threshold=0.05, deviation_mm=0.01)
    committed = [p for p in run.packages if p.committed]
    assert len(committed) == 2 and len(run.packages) == 2
    assert fake.calls == [], "the auto tier must not call the reviewer at all"
    for p in committed:
        assert p.reviewed == pr.ACCEPT
        assert p.review_tier == "auto"
        assert p.review_reason.startswith("auto-accept: ")
        assert p.review_s == 0.0
        # AUDITED: the same clipped evidence the reviewer would have been sent.
        assert isinstance(p.review_evidence, dict) and p.review_evidence
        assert p.to_dict()["review_tier"] == "auto"


def test_no_threshold_means_the_reviewer_still_sees_everything(monkeypatch):
    # The posture is live and the tier is INERT: no threshold was declared.
    run, fake = _loop(monkeypatch, *({"verdict": "accept", "reason": "ok"},) * 3,
                      auto="live", threshold=None)
    assert len(fake.calls) == 2
    assert all(p.review_tier == "reviewer" for p in run.packages)


def test_an_over_threshold_package_goes_to_the_reviewer(monkeypatch):
    # A TABLE in both directions at the SESSION level too, so a mutated comparison here (not only
    # in the pure tier) is caught: same deviation, two thresholds.
    for threshold, expected_calls, expected_tier in ((0.05, 0, "auto"), (0.001, 2, "reviewer")):
        run, fake = _loop(monkeypatch, *({"verdict": "accept", "reason": "ok"},) * 3,
                          auto="live", threshold=threshold, deviation_mm=0.01)
        assert len(fake.calls) == expected_calls, threshold
        assert all(p.review_tier == expected_tier for p in run.packages), threshold


def test_an_unmeasured_deviation_goes_to_the_reviewer(monkeypatch):
    run, fake = _loop(monkeypatch, *({"verdict": "accept", "reason": "ok"},) * 3,
                      auto="live", threshold=0.05, status=pr.DEVIATION_PROBE_OFF)
    assert len(fake.calls) == 2
    assert all(p.review_tier == "reviewer" for p in run.packages)


def test_the_auto_tier_cannot_rescue_a_reviewer_reject(monkeypatch):
    # Posture live, but the tier declines (no threshold), so the reviewer's reject still stands
    # and two rejects still escalate: the tier adds a seat, it never removes one.
    from meshpipeline.cad.repair.session import REVIEW_REJECTED_REASON, REVIEW_TWICE_REASON

    run, fake = _loop(monkeypatch, *({"verdict": "reject", "reason": "no"},) * 6,
                      auto="live", threshold=None)
    assert not any(p.committed for p in run.packages)
    assert run.escalated == (0, 1)
    assert [p.reason for p in run.packages] == [REVIEW_REJECTED_REASON, REVIEW_TWICE_REASON] * 2
    assert all(p.review_tier == "reviewer" for p in run.packages)


def test_review_off_is_byte_identical_and_carries_no_tier(monkeypatch):
    run, fake = _loop(monkeypatch, auto="live", threshold=0.05, review="off")
    assert fake.calls == []
    for p in run.packages:
        assert p.committed and p.reviewed is None and p.review_tier is None
        assert p.review_reason is None and p.review_evidence is None
        assert "review_tier" not in p.to_dict() and "reviewed" not in p.to_dict()
        assert "review_objection" not in p.to_dict()


def test_an_unset_threshold_is_the_default_everywhere(monkeypatch):
    # A session nobody told, and a `run_regions` nobody told, carry no threshold at all - so the
    # tier is inert for every caller that has not adopted the seam.
    from tests.unit.cad.test_repair_region_loop import _Part

    part = _Part()
    plain = RepairSession("original", measurer=part.measurer, scanner=part.scanner)
    assert plain._auto_accept_mm is None
    told = RepairSession("original", measurer=part.measurer, scanner=part.scanner,
                         auto_accept_mm=0.25)
    assert told._auto_accept_mm == 0.25


def test_a_vertex_moving_package_is_never_auto_accepted(monkeypatch):
    """THE RULE THAT MAY NOT BE UNDONE AT THE SESSION LEVEL EITHER.

    The deploy expressions make `rebuild_face` and `close_wire_gaps` ride with the reviewer; this
    proves the tier itself refuses them, so a deploy change cannot hand them to the auto tier.
    """
    from tests.unit.cad.test_repair_region_loop import _Part

    # PINNED, not iterated from the module: an emptied `VERTEX_MOVING_OPS` would make a loop over
    # the module's own set pass vacuously, which is exactly the mutation this test must catch.
    assert rt.VERTEX_MOVING_OPS == frozenset({"close_wire_gaps", "rebuild_face"})
    for op_name in ("close_wire_gaps", "rebuild_face"):
        part = _Part()
        for op in part.ops.values():
            # The same operation, RENAMED to a vertex-moving one: nothing else about the package
            # changes, so the only thing that can send it to the reviewer is its name.
            op.name = op_name
        run, fake = _vertex_run(monkeypatch, part)
        assert len(fake.calls) == 2, op_name
        assert all(p.review_tier == "reviewer" for p in run.packages), op_name
        assert all("moves vertices" not in str(p.reason) for p in run.packages), op_name


def _vertex_run(monkeypatch, part):
    from tests.unit.cad.test_repair_region_loop import _TOLERANCE_MM, _author, _budget

    from meshpipeline.cad.repair.region_loop import run_regions

    fake = FakeAsk(*({"verdict": "accept", "reason": "ok"},) * 3)
    monkeypatch.setattr(pr, "_ask", fake)
    monkeypatch.setattr(pr, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", "live", raising=False)
    monkeypatch.setattr(RepairSession, "_deviation_evidence",
                        lambda self, *, cap_s: ({"deviation_mm": 0.0}, pr.DEVIATION_MEASURED))
    session = RepairSession("original", measurer=part.measurer, scanner=part.scanner,
                            clock=part.clock, auto_accept_mm=0.05)
    return run_regions("original", author=_author(part), tolerance_mm=_TOLERANCE_MM,
                       budget=_budget(), session=session, clock=part.clock, attempts=3), fake


# THE SEAL: THE MEASURED OBJECTION CROSSES TO THE AUTHOR, THE REVIEWER'S PROSE NEVER DOES


def test_a_reject_carries_the_measured_objection_and_no_prose(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    run, _ = _loop(monkeypatch, *({"verdict": "reject", "reason": _PROSE,
                                   "objection": {"field": "area_mm2"}},) * 6,
                   auto="live", threshold=None)
    rejected = [p for p in run.packages if not p.committed]
    assert rejected
    for p in rejected:
        # The reviewer's prose IS in the report ...
        assert _PROSE in str(p.review_reason)
        # ... and the objection is MEASURED: a field name, and a value the session measured.
        assert isinstance(p.review_objection, dict)
        assert p.review_objection["field"] in pr.OBJECTION_FIELDS
        blob = json.dumps(p.review_objection)
        for word in _PROSE_WORDS:
            assert word not in blob, (word, blob)


def test_the_investigator_sees_the_objection_on_recall(monkeypatch):
    """The deferred cross-lane assertion, now implemented: what the AUTHOR is shown on its next
    turn carries `review_objection` and no substring of `review_reason`."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    run, _ = _loop(monkeypatch, *({"verdict": "reject", "reason": _PROSE,
                                   "objection": {"field": "deviation_mm"}},) * 6,
                   auto="live", threshold=None)
    attempts = [observe._attempt_of(p) for p in run.packages if not p.committed]
    assert attempts
    for attempt in attempts:
        assert "review_objection" in attempt
        assert attempt["review_objection"]["field"] in pr.OBJECTION_FIELDS
        # NOTHING ELSE FROM THE REVIEW. `review_reason` above all, but no review key at all.
        assert "review_reason" not in attempt
        assert [k for k in attempt if k.startswith("review")] == ["review_objection"]
        blob = json.dumps(attempt)
        for word in _PROSE_WORDS:
            assert word not in blob, (word, attempt)


def test_an_accepted_package_shows_the_author_no_objection(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    run, _ = _loop(monkeypatch, *({"verdict": "accept", "reason": _PROSE},) * 3,
                   auto="live", threshold=None)
    for p in run.packages:
        assert p.review_objection is None
        assert "review_objection" not in observe._attempt_of(p)
        assert "review_objection" not in p.to_dict()


def test_the_whole_observation_carries_no_reviewer_prose(monkeypatch):
    """The HOSTILE case: the whole author-facing observation, walked for the reviewer's words."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_OBJECTION", "live", raising=False)
    run, _ = _loop(monkeypatch, *({"verdict": "reject", "reason": _PROSE,
                                   "objection": {"field": "solids"}},) * 6,
                   auto="live", threshold=None)
    rejected = [p for p in run.packages if not p.committed]
    assert rejected
    blob = json.dumps([observe._attempt_of(p) for p in rejected])
    for word in _PROSE_WORDS:
        assert word not in blob, word


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
