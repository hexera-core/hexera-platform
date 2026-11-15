# Responsibility: Verify the semantic reviewer - the owner's replacement for numeric deviation gates
#                 on the customer repair path - reads ONLY measured evidence, treats the author's
#                 intent as a claim, and turns every no-answer into a named reject.
"""Hermetic: the model call is a fake `package_review._ask` (patched where the module looks it up),
so nothing here reaches a router, a kernel or a network."""
from __future__ import annotations

import json
import logging

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import package_review as pr
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region

_BEFORE = {"valid": True, "solids": 2, "shells": 2, "faces": 20, "edges": 60, "vertices": 40,
           "diagonal_mm": 200.0, "max_tolerance_mm": 0.01, "surface_area_mm2": 1000.0,
           "volume_mm3": 8000.0}
_AFTER = {**_BEFORE, "solids": 2, "faces": 22, "surface_area_mm2": 1000.5, "volume_mm3": 8000.0}

#: The author's claim, and text a reviewer must NEVER see: a raw model reply, an observation dump
#: and an operation's own free-form evidence.
_INTENT = "fragment the two touching bodies so they share one interface"
_RAW_REPLY = "RAW-MODEL-REPLY-TEXT"
_OBSERVATION = "OBSERVATION-DUMP-TEXT"
_OP_EVIDENCE = "OP-EVIDENCE-TEXT"


def _defect(fp: str, code: DefectCode) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                        entity_index=1, message="x", fingerprint=fp, location={})


def _package() -> TargetPackage:
    region = Region(index=3, fingerprints=frozenset({"f1", "f2"}), bbox_min=(0.0, 0.0, 0.0),
                    bbox_max=(1.0, 1.0, 1.0), face_indices=frozenset({1}), located=True)
    return TargetPackage(
        region=region, intent=_INTENT,
        plan=({"op": object(), "targets": (EntityRef("face", "f1", 1),),
               "defect_code": DefectCode.mesh_edge_through_face},),
        predicate=Predicate(cleared=frozenset({"f1"}), reclassified=frozenset(),
                            unchanged=frozenset({"f2"}), max_area_delta_mm2=1.0,
                            max_volume_delta_mm3=1.0),
        budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=1, deviation_mm=0.0),
        rationale=f"regions-model-v1: {_INTENT} {_RAW_REPLY}")


def _delta() -> dict:
    return {
        "verdicts": [
            {"op": "fragment_bodies", "targets": ["f1"], "defect_code": "mesh_edge_through_face",
             "status": "kept", "reason": "", "basis": "kernel",
             "evidence": {"note": _OP_EVIDENCE, "area_delta_mm2": 0.5},
             "delta": {"before": _BEFORE, "after": _AFTER,
                       "revealed": [{"fingerprint": "f9", "code": "mesh_overlapping_facets"}]},
             "apply_s": 1.0, "scan_s": 1.0, "measure_s": 0.1},
            {"op": "fix_small_faces", "targets": ["f2"], "defect_code": "small_face",
             "status": "rolled_back", "reason": "target_remains", "evidence": {},
             "delta": {}, "apply_s": 0.1, "scan_s": 0.1, "measure_s": 0.1}],
        "before": _BEFORE, "after": _AFTER,
        "declared_area_mm2": 0.5, "declared_volume_mm3": 0.0,
        # Things a careless caller might hand over that must not survive into the evidence.
        "reply": _RAW_REPLY, "observation": _OBSERVATION,
    }


_DEVIATION = {"deviation_mm": 0.02, "deviation_samples": 40, "deviation_faces": 2,
              "deviation_faces_skipped": 0, "deviation_s": 0.4}


def _probes(*, mesh: bool = True) -> tuple[dict, dict]:
    before = pr.probe_of((_defect("f1", DefectCode.mesh_edge_through_face),
                          _defect("f2", DefectCode.small_face)), mesh_probed=mesh)
    after = pr.probe_of((_defect("f2", DefectCode.small_face),
                         _defect("f9", DefectCode.mesh_overlapping_facets)), mesh_probed=mesh)
    return before, after


EXPECTED_KEYS = {
    "ops", "area_mm2", "volume_mm3", "faces", "solids",
    "deviation_mm", "deviation_samples", "deviation_faces", "deviation_faces_skipped",
    "mesh_before", "mesh_after",
    "defects_cleared", "defects_revealed", "defects_remaining",
    "author_claim",
}


def test_reviewer_evidence_is_measured_only():
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(), _DEVIATION, before, after)
    assert set(evidence) == EXPECTED_KEYS
    assert evidence["author_claim"] == _INTENT
    assert evidence["ops"] == [
        {"op": "fragment_bodies", "status": "kept", "reason": ""},
        {"op": "fix_small_faces", "status": "rolled_back", "reason": "target_remains"}]
    assert evidence["area_mm2"] == {"declared": 0.5, "measured": pytest.approx(0.5)}
    assert evidence["volume_mm3"] == {"declared": 0.0, "measured": 0.0}
    assert evidence["faces"] == {"before": 20, "after": 22}
    assert evidence["solids"] == {"before": 2, "after": 2}
    assert evidence["deviation_mm"] == 0.02 and evidence["deviation_samples"] == 40
    assert evidence["mesh_before"] == ["mesh_edge_through_face"]
    assert evidence["mesh_after"] == ["mesh_overlapping_facets"]
    assert evidence["defects_cleared"] == ["mesh_edge_through_face"]
    assert evidence["defects_revealed"] == ["mesh_overlapping_facets"]
    assert evidence["defects_remaining"] == ["small_face"]
    # THE AUTHOR'S TEXT APPEARS ONCE, AND ONLY AS A CLAIM.
    text = json.dumps({k: v for k, v in evidence.items() if k != "author_claim"})
    assert _INTENT not in text
    json.dumps(evidence, allow_nan=False)


def test_evidence_without_the_probe_lacks_mesh_and_deviation_fields():
    before, after = _probes(mesh=False)
    evidence = pr.evidence_of(_package(), _delta(), {}, before, after)
    assert set(evidence) == EXPECTED_KEYS - {
        "deviation_mm", "deviation_samples", "deviation_faces", "deviation_faces_skipped",
        "mesh_before", "mesh_after"}


def test_measured_empty_deviation_is_kept_as_recorded():
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(),
                              {"deviation_s": 0.0, "deviation_samples": 0, "deviation_faces": 0,
                               "deviation_faces_skipped": 0}, before, after)
    assert "deviation_mm" not in evidence
    assert evidence["deviation_faces"] == 0


def test_reviewer_never_sees_raw_replies_or_observations(review_ask):
    fake = review_ask({"verdict": "accept", "reason": "design kept"})
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(), _DEVIATION, before, after)
    pr.review_package(evidence, timeout_s=5.0)
    sent = fake.calls[0]["system"] + fake.calls[0]["user"]
    for secret in (_RAW_REPLY, _OBSERVATION, _OP_EVIDENCE, "regions-model-v1"):
        assert secret not in sent
    assert json.loads(fake.calls[0]["user"]) == evidence


# THE VERDICT


class FakeAsk:
    """Stands in for `package_review._ask`: the queued replies in order, every call recorded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, system, user, *, job_id, timeout_s):
        self.calls.append({"system": system, "user": user, "job_id": job_id,
                           "timeout_s": timeout_s})
        return self.replies.pop(0) if self.replies else None


@pytest.fixture
def review_ask(monkeypatch):
    def install(*replies):
        fake = FakeAsk(*replies)
        monkeypatch.setattr(pr, "_ask", fake)
        monkeypatch.setattr(pr, "_configured", lambda: True)
        return fake
    return install


def test_accept_is_returned_with_its_reason(review_ask):
    review_ask({"verdict": "accept", "reason": "the interface is shared, nothing removed"})
    assert pr.review_package({"author_claim": "x"}, timeout_s=5.0) == (
        "accept", "the interface is shared, nothing removed")


def test_a_reject_carries_the_reviewers_reason(review_ask):
    review_ask({"verdict": "reject", "reason": "a fillet face was deleted"})
    assert pr.review_package({}, timeout_s=5.0) == ("reject", "a fillet face was deleted")


def test_reviewer_no_answer_rejects(review_ask, monkeypatch):
    # No router.
    monkeypatch.setattr(pr, "_configured", lambda: False)
    verdict, reason = pr.review_package({}, timeout_s=5.0)
    assert verdict == "reject" and "no model router" in reason
    # No reply (a failed or timed-out call, or a reply with no JSON object).
    review_ask(None)
    verdict, reason = pr.review_package({}, timeout_s=5.0)
    assert verdict == "reject" and "no reply" in reason
    # A reply in the wrong shape.
    review_ask({"verdict": "accept"})
    verdict, reason = pr.review_package({}, timeout_s=5.0)
    assert verdict == "reject" and "malformed" in reason
    review_ask({"verdict": "accept", "reason": "ok", "confidence": 0.9})
    verdict, reason = pr.review_package({}, timeout_s=5.0)
    assert verdict == "reject" and "malformed" in reason
    # The deadline already passed: no call at all.
    fake = review_ask({"verdict": "accept", "reason": "ok"})
    verdict, reason = pr.review_package({}, timeout_s=5.0, deadline_s=10.0, clock=lambda: 11.0)
    assert (verdict, reason) == ("reject", "no time left to review")
    assert fake.calls == []


def test_a_hung_review_is_bounded_and_rejects(monkeypatch):
    import asyncio

    async def hang(system, user, *, job_id, timeout_s):
        await asyncio.sleep(30)

    monkeypatch.setattr(pr, "_ask", hang)
    monkeypatch.setattr(pr, "_configured", lambda: True)
    monkeypatch.setattr(pr, "_GRACE_S", 0.05)
    verdict, reason = pr.review_package({}, timeout_s=0.05)
    assert verdict == "reject" and "no reply" in reason


def test_unsure_or_unknown_verdict_rejects(review_ask):
    for value in ("unsure", "ACCEPT", "maybe", True, None):
        review_ask({"verdict": value, "reason": "hmm"})
        verdict, reason = pr.review_package({}, timeout_s=5.0)
        assert verdict == "reject"
        assert "not accept or reject" in reason


def test_the_timeout_is_cut_to_the_time_left(review_ask):
    fake = review_ask({"verdict": "accept", "reason": "ok"})
    pr.review_package({}, timeout_s=20.0, deadline_s=103.0, clock=lambda: 100.0)
    assert fake.calls[0]["timeout_s"] == pytest.approx(3.0)


def test_the_prompt_says_unsure_is_reject_and_names_the_claim():
    assert "reject" in pr.REVIEW_SYSTEM and "unsure" in pr.REVIEW_SYSTEM
    assert "author_claim" in pr.REVIEW_SYSTEM
    assert '"verdict"' in pr.REVIEW_SYSTEM


# THE POSTURE


def test_posture_typo_reads_off_with_a_warning(monkeypatch, caplog):
    for raw, want in (("live", "live"), ("off", "off"), ("Live", "off"), ("on", "off"),
                      ("shadow", "off")):
        monkeypatch.setenv("CAD_REPAIR_PACKAGE_REVIEW", raw)
        with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
            caplog.clear()
            assert rcfg._package_review_posture() == want
        warned = any("CAD_REPAIR_PACKAGE_REVIEW" in r.getMessage() for r in caplog.records)
        assert warned is (raw not in ("live", "off"))


def test_unknown_posture_attribute_reads_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "LIVE")
    assert pr.posture() == "off"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    assert pr.posture() == "live"


def test_the_review_settings_default_off_and_twenty_seconds():
    assert rcfg.CAD_REPAIR_PACKAGE_REVIEW == "off"
    assert rcfg.CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S == 20.0
