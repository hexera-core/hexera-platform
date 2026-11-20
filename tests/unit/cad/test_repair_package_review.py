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


def _defects(*codes):
    """Defect doubles carrying just the code `probe_of` reads."""
    from types import SimpleNamespace

    return tuple(SimpleNamespace(code=c) for c in codes)


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


_ORIGINAL = {**_BEFORE, "solids": 2}

EXPECTED_KEYS = {
    "ops", "area_mm2", "volume_mm3", "faces", "solids",
    "diagonal_mm", "area_mm2_before", "volume_mm3_before",
    "deviation_status", "review_rejects",
    "deviation_mm", "deviation_samples", "deviation_faces", "deviation_faces_skipped",
    "mesh_before", "mesh_after",
    # WHICH ARTEFACT `mesh_after` IS. Stated whenever there is a mesh reading at all;
    # `mesh_after_in_memory` joins it only when a delivered reading exists to disagree with.
    "mesh_after_source",
    "defects_cleared", "defects_revealed", "defects_remaining",
    "author_claim",
}


def test_reviewer_evidence_is_measured_only():
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(), _DEVIATION, before, after,
                              original=_ORIGINAL, review_rejects=("a fillet face was removed",))
    assert set(evidence) == EXPECTED_KEYS
    assert evidence["mesh_after_source"] == "in_memory_only"   # no delivered probe handed in
    assert (evidence["diagonal_mm"], evidence["area_mm2_before"],
            evidence["volume_mm3_before"]) == (200.0, 1000.0, 8000.0)
    assert evidence["deviation_status"] == "measured"
    assert evidence["review_rejects"] == {"count": 1,
                                          "reasons": ["a fillet face was removed"]}
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
    assert evidence["deviation_status"] == "not measured: probe off"
    assert evidence["review_rejects"] == {"count": 0, "reasons": []}
    # No upload measure handed in: the scale reads as the hole it is.
    assert evidence["diagonal_mm"] == "unmeasured"
    # WITH THE PROBE OFF there is no mesh reading of either artefact, so neither `mesh_after` nor
    # the label that says which one it is may appear.
    assert set(evidence) == EXPECTED_KEYS - {
        "deviation_mm", "deviation_samples", "deviation_faces", "deviation_faces_skipped",
        "mesh_before", "mesh_after", "mesh_after_source"}


def test_measured_empty_deviation_is_kept_as_recorded():
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(),
                              {"deviation_s": 0.0, "deviation_samples": 0, "deviation_faces": 0,
                               "deviation_faces_skipped": 0}, before, after)
    assert "deviation_mm" not in evidence
    assert evidence["deviation_faces"] == 0
    assert evidence["deviation_status"] == "not measured: no faces"


def test_deviation_status_says_why_it_is_absent():
    assert pr._deviation_status({}) == "not measured: probe off"
    assert pr._deviation_status({"deviation_s": 0.4}) == "not measured: failed or ran out of time"
    assert pr._deviation_status({"deviation_s": 0.0, "deviation_status":
                                 "not measured: no time"}) == "not measured: no time"
    assert pr._deviation_status(_DEVIATION) == "measured"


def test_a_missing_or_non_finite_measurement_is_unmeasured_never_zero():
    delta = _delta()
    delta["after"] = {**_AFTER, "surface_area_mm2": float("nan"), "faces": None}
    delta["before"] = {k: v for k, v in _BEFORE.items() if k != "volume_mm3"}
    delta["declared_area_mm2"] = float("inf")
    before, after = _probes()
    evidence = pr.evidence_of(_package(), delta, {**_DEVIATION, "deviation_mm": float("nan")},
                              before, after, original={**_ORIGINAL, "diagonal_mm": float("inf")})
    assert evidence["area_mm2"] == {"declared": "unmeasured", "measured": "unmeasured"}
    assert evidence["volume_mm3"]["measured"] == "unmeasured"
    assert evidence["faces"] == {"before": 20, "after": "unmeasured"}
    assert evidence["deviation_mm"] == "unmeasured"
    assert evidence["diagonal_mm"] == "unmeasured"
    json.dumps(evidence, allow_nan=False)


def test_the_recorded_evidence_is_clipped_with_a_stated_marker():
    small = {"author_claim": "x"}
    assert pr.clip_evidence(small) == small
    big = {"author_claim": "y" * 10_000}
    clipped = pr.clip_evidence(big)
    assert set(clipped) == {"clipped", "head"}
    assert "clipped to 4000 of" in clipped["clipped"]
    assert len(clipped["head"].encode()) <= 4000


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
    assert "unmeasured" in pr.REVIEW_SYSTEM
    assert "deviation was not measured, reject it unless" in pr.REVIEW_SYSTEM
    assert "review_rejects" in pr.REVIEW_SYSTEM
    assert "from the upload as repair received it" in pr.REVIEW_SYSTEM
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


def test_review_live_on_the_baseline_driver_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        caplog.clear()
        rcfg._warn_unreviewed_baseline("live", "off")
        assert any("WITHOUT the package review" in r.getMessage() for r in caplog.records)
        caplog.clear()
        rcfg._warn_unreviewed_baseline("live", "live")
        rcfg._warn_unreviewed_baseline("off", "off")
        assert not caplog.records


def test_unknown_posture_attribute_reads_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "LIVE")
    assert pr.posture() == "off"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    assert pr.posture() == "live"


def test_the_review_settings_default_off_and_twenty_seconds():
    assert rcfg.CAD_REPAIR_PACKAGE_REVIEW == "off"
    assert rcfg.CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S == 20.0


# THE LOOP-LEVEL INVARIANT: posture live => every COMMITTED package was reviewed and accepted.


def _loop(monkeypatch, *replies):
    from tests.unit.cad.test_repair_region_loop import _author, _Part, _run

    fake = FakeAsk(*replies)
    monkeypatch.setattr(pr, "_ask", fake)
    monkeypatch.setattr(pr, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    part = _Part()
    asked: list[int] = []
    inner = _author(part)

    def author(region, defects, attempts):
        asked.append(int(region.index))
        return inner(region, defects, attempts)

    return _run(part, author, attempts=3), fake, asked


def test_live_review_every_committed_package_was_accepted(monkeypatch):
    run, fake, _ = _loop(monkeypatch, {"verdict": "reject", "reason": "r1"},
                         {"verdict": "accept", "reason": "ok"},
                         {"verdict": "accept", "reason": "ok"})
    committed = [p for p in run.packages if p.committed]
    assert committed and len(fake.calls) == 3
    assert all(p.reviewed == "accept" for p in committed)
    assert [p.status for p in run.packages] == ["reverted", "committed", "committed"]


def test_live_review_a_region_rejected_twice_is_escalated_and_not_authored_again(monkeypatch):
    run, fake, asked = _loop(monkeypatch, *({"verdict": "reject", "reason": "no"},) * 6)
    assert not any(p.committed for p in run.packages)
    assert run.escalated == (0, 1)
    # Two attempts per region - the reject and the one retry - never the third the bound allows.
    assert asked == [0, 0, 1, 1] and len(fake.calls) == 4
    assert [p.reason for p in run.packages] == [
        "review: rejected by review", "review: rejected by review twice"] * 2


# DEBRIS VERSUS REAL GEOMETRY (LEAD ruling 2026-11-20, under the owner-approved design: "only
# sliver debris may be removed, counted and capped; never delete real geometry to get past the
# mesher"). The reviewer keeps the rule, is told the permitted case, and is shown the facts that
# tell the two apart.


def test_the_prompt_keeps_the_rule_and_states_the_permitted_debris_case():
    text = pr.REVIEW_SYSTEM
    # (1) the rule, about REAL geometry
    assert "reject a repair that deletes or collapses REAL geometry" in text
    assert "a feature, a wall, a body" in text
    # (2) the permitted case and every condition on it
    assert "removing sliver or degenerate DEBRIS faces is acceptable when ALL of these hold" in text
    for condition in ("the removal is declared (removed)", "area_fraction is negligible",
                      "deviation_mm is negligible relative to diagonal_mm", "deviation_basis",
                      "solids after equal solids before"):
        assert condition in text, condition
    # unsure stays reject
    assert "if you are unsure, reject" in text


def _removing_delta(*, faces=4, area=0.48387, status="kept"):
    delta = _delta()
    delta["verdicts"].append(
        {"op": "fix_small_faces", "targets": ["f2"], "defect_code": "small_face",
         "status": status, "reason": "", "basis": "geometric",
         "evidence": {"faces_removed": faces, "area_removed_mm2": area,
                      "removed_fingerprints": ["face:x"]},
         "delta": {}, "apply_s": 0.1, "scan_s": 0.1, "measure_s": 0.1})
    return delta


def test_the_reviewer_is_shown_the_facts_that_tell_debris_from_real_geometry():
    before, after = _probes()
    deviation = {**_DEVIATION, "deviation_basis": "adjacent_to_removed"}
    evidence = pr.evidence_of(_package(), _removing_delta(), deviation, before, after,
                              original=_ORIGINAL)
    assert evidence["removed"] == {"faces": 4, "area_mm2": pytest.approx(0.48387),
                                   "area_fraction": pytest.approx(0.48387 / 1000.0)}
    assert evidence["deviation_basis"] == "adjacent_to_removed"
    assert evidence["deviation_mm"] == 0.02
    assert evidence["solids"] == {"before": 2, "after": 2}
    assert evidence["diagonal_mm"] == 200.0
    json.dumps(evidence, allow_nan=False)
    # Both are fields an objection may name, so a reject can point at them.
    assert {"removed", "deviation_basis"} <= set(pr.OBJECTION_FIELDS)


def test_a_rolled_back_removal_or_none_is_not_shown_as_removed():
    before, after = _probes()
    assert "removed" not in pr.evidence_of(_package(), _delta(), _DEVIATION, before, after)
    rolled = _removing_delta(status="rolled_back")
    assert "removed" not in pr.evidence_of(_package(), rolled, _DEVIATION, before, after)


def test_an_undeclared_area_or_scale_reads_unmeasured_never_zero():
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _removing_delta(area=None), _DEVIATION, before, after,
                              original=_ORIGINAL)
    assert evidence["removed"] == {"faces": 4, "area_mm2": "unmeasured",
                                   "area_fraction": "unmeasured"}
    no_scale = pr.evidence_of(_package(), _removing_delta(), _DEVIATION, before, after)
    assert no_scale["removed"]["area_fraction"] == "unmeasured"


# A FACE SWAP IS NOT DEBRIS (Greptile PRRT_kwDOUFq_Ps657Wye): RebuildFace declares
# faces_removed=1 AND faces_added=1, with no area - one face out, one in.


def _rebuild_delta():
    delta = _delta()
    delta["verdicts"].append(
        {"op": "rebuild_face", "targets": ["f3"], "defect_code": "mesh_overlapping_facets",
         "status": "kept", "reason": "", "basis": "kernel",
         # THE REAL DECLARATION, read off operations.RebuildFace.apply
         "evidence": {"faces_removed": 1, "faces_added": 1},
         "delta": {}, "apply_s": 0.1, "scan_s": 0.1, "measure_s": 0.1})
    return delta


def test_a_rebuild_alone_is_not_shown_as_removed():
    before, after = _probes()
    assert "removed" not in pr.evidence_of(_package(), _rebuild_delta(), _DEVIATION, before,
                                           after, original=_ORIGINAL)


def test_a_rebuild_beside_a_sweep_does_not_hide_the_sweeps_known_area():
    before, after = _probes()
    delta = _rebuild_delta()
    delta["verdicts"] += _removing_delta()["verdicts"][-1:]
    evidence = pr.evidence_of(_package(), delta, _DEVIATION, before, after, original=_ORIGINAL)
    assert evidence["removed"] == {"faces": 4, "area_mm2": pytest.approx(0.48387),
                                   "area_fraction": pytest.approx(0.48387 / 1000.0)}


def test_the_real_rebuild_declaration_is_a_swap():
    import inspect

    from meshpipeline.cad.repair.operations import RebuildFace

    source = inspect.getsource(RebuildFace.apply)
    assert '"faces_removed": 1, "faces_added": 1' in source
    assert pr.net_faces_removed({"faces_removed": 1, "faces_added": 1}) == 0
    assert not pr.removes_surface({"faces_removed": 1, "faces_added": 1})


# --- mesh_after is the DELIVERED artefact; the in-memory reading is kept beside it -------------
# Measured in the pipeline image on abc 0060_00608290, three probes each and every one identical:
# the repaired shape IN MEMORY fails with 5 x mesh_edge_through_face, and the SAME geometry written
# to STEP and read back MESHES. So a reviewer shown the in-memory probe refuses repairs whose
# delivered file meshes - correct on its evidence, wrong about the product. The owner's headline is
# a repaired STEP that gmsh meshes, and lane E's keep rule already judges cures on the delivered
# STEP, so review must not judge a different object.

def test_mesh_after_is_the_delivered_probe_and_the_in_memory_one_is_labelled():
    evidence = pr.evidence_of(
        _package(), _delta(), {},
        pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True),
        pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True),
        delivered=pr.probe_of((), mesh_probed=True))

    assert evidence["mesh_after"] == []                        # the delivered file meshes
    assert evidence["mesh_after_in_memory"] == ["mesh_edge_through_face"]
    assert evidence["mesh_after_source"] == "delivered"


def test_the_in_memory_reading_stands_alone_when_the_delivered_probe_could_not_run():
    """No time left, or an export that failed: the evidence says which reading it is, so an
    unmeasured delivered probe can never be mistaken for a passing one."""
    evidence = pr.evidence_of(
        _package(), _delta(), {},
        pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True),
        pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True),
        delivered=None)

    assert evidence["mesh_after"] == ["mesh_edge_through_face"]
    assert evidence["mesh_after_source"] == "in_memory_only"
    assert "mesh_after_in_memory" not in evidence


def test_a_delivered_failure_is_reported_even_when_the_memory_shape_passed():
    """THE REVERSE, which matters more: a repair that meshes in memory and FAILS as delivered is
    not a repair, and the authoritative reading must say so."""
    evidence = pr.evidence_of(
        _package(), _delta(), {},
        pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True),
        pr.probe_of((), mesh_probed=True),
        delivered=pr.probe_of(_defects("mesh_edge_through_face"), mesh_probed=True))

    assert evidence["mesh_after"] == ["mesh_edge_through_face"]
    assert evidence["mesh_after_in_memory"] == []
    assert evidence["mesh_after_source"] == "delivered"


def test_the_delivered_case_pins_its_own_key_set():
    """The same guard as `test_reviewer_evidence_is_measured_only`, for the path that HAS a
    delivered reading: exactly one extra key, and nothing else arrives with it."""
    before, after = _probes()
    evidence = pr.evidence_of(_package(), _delta(), _DEVIATION, before, after,
                              original=_ORIGINAL, review_rejects=("a fillet face was removed",),
                              delivered=pr.probe_of((), mesh_probed=True))

    assert set(evidence) == EXPECTED_KEYS | {"mesh_after_in_memory"}


# --- WHICH bodies changed, as evidence the reviewer reads --------------------------------------

def test_the_reviewer_is_told_which_bodies_appeared_or_split():
    """MEASURED ON 0051_00513292. A committed `sew_faces` took solids 7 -> 8 while the aggregate
    volume moved +0.942 mm3, and the real event was a 3601 mm3 body becoming closed - the
    aggregate understated it by a factor of about 3800, because an open shell's faces already
    contributed a NUMBER to the volume integral rather than a volume. A reviewer shown only the
    count and the aggregate cannot tell that from a body being cut in two.
    """
    before = {**_BEFORE, "solids": 1, "solid_volumes": [8000.0]}
    after = {**_AFTER, "solids": 2, "solid_volumes": [8000.0, 3601.17]}
    probe_before, probe_after = _probes()

    evidence = pr.evidence_of(_package(), {**_delta(), "before": before, "after": after},
                              _DEVIATION, probe_before, probe_after, original=_ORIGINAL)

    assert set(evidence) == EXPECTED_KEYS | {
        "solids_appeared_mm3", "solids_disappeared_mm3", "solids_split"}
    assert evidence["solids_appeared_mm3"] == pytest.approx(3601.17)
    assert evidence["solids_disappeared_mm3"] == pytest.approx(0.0)
    assert evidence["solids_split"] == 0


def test_a_split_body_is_named_to_the_reviewer_as_one():
    before = {**_BEFORE, "solids": 1, "solid_volumes": [8000.0]}
    after = {**_AFTER, "solids": 2, "solid_volumes": [5000.0, 3000.0]}
    probe_before, probe_after = _probes()

    evidence = pr.evidence_of(_package(), {**_delta(), "before": before, "after": after},
                              _DEVIATION, probe_before, probe_after, original=_ORIGINAL)

    assert evidence["solids_split"] == 1
    assert evidence["solids_appeared_mm3"] == pytest.approx(0.0), (
        "a split body's pieces are the old body cut, not new volume")


def test_without_per_solid_volumes_the_reviewer_is_told_nothing_about_bodies():
    """The module's own rule: a measurement nobody took is absent, never 0 - which here would
    read as "no body appeared and none split"."""
    probe_before, probe_after = _probes()

    evidence = pr.evidence_of(_package(), _delta(), _DEVIATION, probe_before, probe_after,
                              original=_ORIGINAL)

    assert "solids_appeared_mm3" not in evidence
    assert "solids_split" not in evidence
