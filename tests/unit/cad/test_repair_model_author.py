"""The model author: a model proposes a target package through the region loop's `author` seam.

Hermetic: the model call is a fake `model_author._ask` (patched where the module looks it up), so these run
with no router, no network and no kernel (the observation's face reader is injected).
"""
from __future__ import annotations

import dataclasses
import json
import logging

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import catalogue_words
from meshpipeline.cad.repair import model_author as ma
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.package import TargetPackage
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import REVERTED, OpVerdict, PackageVerdict

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=10, edges=20, vertices=12,
                   diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=5000.0,
                   volume_mm3=20000.0)


def _defect(i: int, code=DefectCode.curve_inconsistency, severity=DefectSeverity.error) -> EntityDefect:
    return EntityDefect(
        code=code, severity=severity, entity_type="face", entity_index=i,
        message=f"face {i} is broken", measurements={},
        location={"bbox_min": (0.0, 0.0, float(i)), "bbox_max": (1.0, 1.0, float(i) + 1)},
        fingerprint=f"fp{i}")


REGION = Region(index=0, fingerprints=frozenset({"fp1", "fp2"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(1.0, 1.0, 3.0), face_indices=frozenset({1, 2}))
DEFECTS = (_defect(1), _defect(2))


def _faces(shape, indices):
    return {i: {"surface_type": "GeomAbs_Plane", "area_mm2": 4.0,
                "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "neighbours": 4} for i in indices}


class FakeAsk:
    """Stands in for `model_author._ask`: returns the queued replies in order and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, system, user, *, job_id, timeout_s):
        self.calls.append({"system": system, "user": user, "job_id": job_id,
                           "timeout_s": timeout_s})
        return self.replies.pop(0) if self.replies else None


@pytest.fixture
def ask(monkeypatch):
    def install(*replies):
        fake = FakeAsk(*replies)
        monkeypatch.setattr(ma, "_ask", fake)
        monkeypatch.setattr(ma, "_configured", lambda: True)
        return fake
    return install


def _package_reply(**over):
    body = {"intent": "re-fix the boundary of face 1", "plan": [{"op": "fix_face_boundary",
                                                                 "fingerprint": "fp1"}],
            "cleared": ["fp1"], "unchanged": ["fp2"],
            "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}
    body.update(over)
    return {"package": body}


def _author(**kw):
    return ma.model_author(None, original=ORIGINAL, face_reader=_faces, job_id="job-1", **kw)


def test_valid_reply_becomes_a_package(ask):
    fake = ask(_package_reply())
    author = _author()
    package = author(REGION, DEFECTS, ())
    assert isinstance(package, TargetPackage)
    assert package.region is REGION
    assert [item["op"].name for item in package.plan] == ["fix_face_boundary"]
    assert package.plan[0]["targets"][0].fingerprint == "fp1"
    assert package.plan[0]["defect_code"] is DefectCode.curve_inconsistency
    assert "tolerance_mm" not in package.plan[0]          # the loop owns the tolerance
    assert "answered_held" not in package.plan[0]
    assert package.predicate.cleared == frozenset({"fp1"})
    assert package.predicate.unchanged == frozenset({"fp2"})
    assert package.predicate.reclassified == frozenset()
    assert package.predicate.max_area_delta_mm2 == 1.0
    assert package.budget.max_ops == 1 and package.budget.model_calls == 1
    assert package.intent == "re-fix the boundary of face 1"
    assert package.rationale.startswith(ma.LABEL)
    assert fake.calls[0]["job_id"] == "job-1"
    assert fake.calls[0]["timeout_s"] == float(rcfg.CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S)
    assert "fix_face_boundary" in fake.calls[0]["system"]      # the catalogue is in the prompt
    assert json.loads(fake.calls[0]["user"])["region"]["index"] == 0


@pytest.mark.parametrize("reply, needle", [
    (_package_reply(plan=[{"op": "delete_everything", "fingerprint": "fp1"}]), "catalogue"),
    (_package_reply(plan=[{"op": "fix_face_boundary", "fingerprint": "fp9"}]), "plan"),
    (_package_reply(cleared=["fp9"]), "cleared"),
    (_package_reply(unchanged=["nowhere"]), "unchanged"),
    (_package_reply(plan=[]), "plan"),
])
def test_reply_naming_unknown_targets_is_refused(ask, reply, needle):
    ask(reply)
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    call = author.calls[-1]
    assert call["outcome"] == "refused"
    assert needle in call["reason"]


@pytest.mark.parametrize("reply", [
    None,                                                      # timeout / transport failure
    {"proposals": []},                                         # not one of the three forms
    {"pass": True, "package": _package_reply()["package"]},     # two forms at once
    {"pass": "yes"},
    {"package": "fix it"},
])
def test_timeout_or_garbage_is_none(ask, reply):
    ask(reply)
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert author.calls[-1]["outcome"] in ("no_reply", "refused")
    assert author.calls[-1]["reason"]


def test_a_hung_call_is_bounded_by_the_timeout(monkeypatch):
    import asyncio

    async def hang(system, user, *, job_id, timeout_s):
        await asyncio.sleep(30)

    monkeypatch.setattr(ma, "_ask", hang)
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ma, "_GRACE_S", 0.05)
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert author.calls[-1]["outcome"] == "no_reply"


def test_pass_is_none(ask):
    ask({"pass": True})
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert author.calls[-1]["outcome"] == "pass"


def test_shadow_records_and_executes_nothing(ask):
    ask(_package_reply(plan=[{"op": "fix_shape_global", "fingerprint": "fp2"}],
                       cleared=["fp2"], unchanged=[]))
    rules_seen = []

    def rules(region, defects, attempts=()):
        rules_seen.append(region.index)
        return "the rules package"

    author = ma.shadow_author(None, original=ORIGINAL, job_id="job-1", face_reader=_faces,
                              rules=rules)
    assert author(REGION, DEFECTS, ()) == "the rules package"     # the rules drive
    assert rules_seen == [0]
    [record] = author.shadow_proposals
    assert record["region"] == 0
    assert record["model"]["outcome"] == "package"
    assert record["model"]["proposal"]["plan"] == [{"op": "fix_shape_global",
                                                    "fingerprint": "fp2",
                                                    "defect_code": "curve_inconsistency"}]
    assert author.label == ma.RULES_LABEL


def test_shadow_records_a_refused_proposal_with_its_reason(ask):
    ask(_package_reply(cleared=["fp9"]))
    author = ma.shadow_author(None, original=ORIGINAL, job_id="", face_reader=_faces,
                              rules=lambda region, defects, attempts=(): None)
    assert author(REGION, DEFECTS, ()) is None
    [record] = author.shadow_proposals
    assert record["model"]["outcome"] == "refused"
    assert "cleared" in record["model"]["reason"]


def test_second_attempt_sees_the_first_failure(ask):
    fake = ask(_package_reply(), _package_reply())
    author = _author()
    first = author(REGION, DEFECTS, ())
    assert first is not None
    failed = PackageVerdict(
        region_index=0, status=REVERTED,
        reason="predicate failed: cleared defects still present: fp1",
        verdicts=(OpVerdict(op="fix_face_boundary", targets=("fp1",),
                            defect_code="invalid_brep", status="kept",
                            reason="the boundary was rebuilt but the face is still invalid"),))
    author(REGION, DEFECTS, (failed,))
    second = json.loads(fake.calls[1]["user"])
    assert "cleared defects still present: fp1" in json.dumps(second["attempts"])
    assert "the boundary was rebuilt" in json.dumps(second["attempts"])
    assert author.calls[1]["attempt"] == 2


def test_call_budget_is_respected(ask, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_ATTEMPTS", 2)
    fake = ask(*[_package_reply() for _ in range(5)])
    author = _author()
    for _ in range(5):
        author(REGION, DEFECTS, ())
    assert len(fake.calls) == 2
    other = Region(index=1, fingerprints=frozenset({"fp1"}), bbox_min=(0, 0, 0),
                   bbox_max=(1, 1, 1), face_indices=frozenset({1}))
    author(other, DEFECTS, ())                    # the budget is per region
    assert len(fake.calls) == 3


def test_verdict_recorded_and_returns_none(ask):
    ask({"verdict": {"mesher_problem": ["fp1"], "reason": "the geometry is sound; gmsh is not"}})
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert author.verdicts == [{"region": 0, "mesher_problem": ["fp1"],
                                "reason": "the geometry is sound; gmsh is not",
                                "author": ma.LABEL}]
    assert author.calls[-1]["outcome"] == "verdict"


def test_verdict_is_recorded_with_reason(ask):
    ask({"verdict": {"mesher_problem": ["fp1"], "reason": "sound " + "x" * 5000}})
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    (verdict,) = author.report()["verdicts"]
    assert verdict["region"] == REGION.index
    assert verdict["mesher_problem"] == ["fp1"]
    assert verdict["author"] == ma.LABEL
    assert verdict["reason"].startswith("sound ") and len(verdict["reason"]) <= ma.INTENT_MAX + 3


def test_verdict_naming_unknown_fingerprints_is_refused(ask):
    ask({"verdict": {"mesher_problem": ["fp9"], "reason": "trust me"}})
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert author.verdicts == []
    assert author.calls[-1]["outcome"] == "refused"


def test_reserved_codes_refused(ask):
    held = (_defect(1, code=DefectCode.invalid_brep), _defect(2, code=DefectCode.small_face))
    ask(_package_reply(plan=[{"op": "fix_small_faces", "fingerprint": "fp2"}],
                       cleared=["fp2"], unchanged=[]))
    author = _author(reserved=frozenset({DefectCode.small_face}))
    assert author(REGION, held, ()) is None
    assert "held" in author.calls[-1]["reason"]


def test_paired_rims_refuse_filling(ask, monkeypatch):
    rims = (_defect(1, code=DefectCode.open_shell), _defect(2, code=DefectCode.open_shell))
    monkeypatch.setattr(ma, "paired_rim_fingerprints", lambda shape, defects: frozenset(
        {"fp1", "fp2"}))
    ask(_package_reply(plan=[{"op": "fill_planar_hole", "fingerprint": "fp1"}], unchanged=[]))
    author = ma.model_author(object(), original=ORIGINAL, face_reader=_faces)
    author.observe_part(rims)
    assert author(REGION, rims, ()) is None
    assert "paired rim" in author.calls[-1]["reason"]
    assert {r["reason"] for r in author.refusals} == {"paired_rim"}


@pytest.mark.parametrize("field, value", [("max_area_delta_mm2", 1e9),
                                          ("max_volume_delta_mm3", 1e9),
                                          ("max_area_delta_mm2", -1.0),
                                          ("max_volume_delta_mm3", float("nan")),
                                          ("max_area_delta_mm2", True)])
def test_caps_exceeding_claims_refused(ask, field, value):
    ask(_package_reply(**{field: value}))
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert field in author.calls[-1]["reason"]


def test_report_carries_calls(ask):
    ask(_package_reply(), {"pass": True})
    author = _author()
    author(REGION, DEFECTS, ())
    author(REGION, DEFECTS, ())
    report = author.report()
    json.dumps(report)                                        # carried as-is by the report
    assert report["label"] == ma.LABEL
    assert [c["outcome"] for c in report["calls"]] == ["package", "pass"]
    first = report["calls"][0]
    assert first["observation_bytes"] > 0 and len(first["observation_sha256"]) == 16
    assert first["region"] == 0 and first["attempt"] == 1
    assert "fix_face_boundary" in first["reply"]
    assert len(first["reply"]) <= ma.REPLY_MAX + 3
    assert report["verdicts"] == []


def test_not_configured_fails_closed(monkeypatch):
    fake = FakeAsk(_package_reply())
    monkeypatch.setattr(ma, "_ask", fake)
    monkeypatch.setattr(ma, "_configured", lambda: False)
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert fake.calls == []
    assert author.calls[-1]["outcome"] == "not_configured"


# --- posture and driver selection -------------------------------------------------------------

def test_off_never_calls_ask(ask, monkeypatch):
    fake = ask(_package_reply())
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    sentinel = object()
    monkeypatch.setattr(ma, "rules_author", lambda shape, **kw: sentinel)
    author, label = ma.select_author(None, original=ORIGINAL)
    assert author is sentinel and label == ma.RULES_LABEL
    assert fake.calls == []


def test_live_without_a_router_is_the_rules_author(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "live")
    monkeypatch.setattr(ma, "_configured", lambda: False)
    sentinel = object()
    monkeypatch.setattr(ma, "rules_author", lambda shape, **kw: sentinel)
    author, label = ma.select_author(None, original=ORIGINAL)
    assert author is sentinel and label == ma.RULES_LABEL


def test_live_with_a_router_is_the_model_author(ask, monkeypatch):
    ask()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "live")
    author, label = ma.select_author(None, original=ORIGINAL, job_id="j")
    assert label == ma.LABEL and hasattr(author, "calls")


def test_shadow_selects_the_composite(ask, monkeypatch):
    ask()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "shadow")
    author, label = ma.select_author(None, original=ORIGINAL)
    assert label == ma.RULES_LABEL and hasattr(author, "shadow_proposals")


def test_posture_typo_reads_off_with_a_warning(monkeypatch, caplog):
    for raw, want in (("live", "live"), ("shadow", "shadow"), ("off", "off"),
                      ("Live ", "off"), ("on", "off")):
        monkeypatch.setenv("CAD_REPAIR_LLM_AUTHOR", raw)
        with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
            caplog.clear()
            assert rcfg._llm_author_posture() == want
        warned = any("CAD_REPAIR_LLM_AUTHOR" in r.getMessage() for r in caplog.records)
        assert warned is (raw not in ("live", "shadow", "off"))


def test_unknown_posture_attribute_reads_off(ask, monkeypatch):
    fake = ask(_package_reply())
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "LIVE")
    sentinel = object()
    monkeypatch.setattr(ma, "rules_author", lambda shape, **kw: sentinel)
    assert ma.posture() == "off"
    assert ma.select_author(None, original=ORIGINAL)[0] is sentinel
    assert fake.calls == []


# --- fix round 1: clocks, region-scoped unchanged, schema keys, lazy guards --------------------

class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_nearly_spent_run_makes_no_shadow_call_and_returns_the_rules_result(ask):
    fake = ask(_package_reply())
    clock = Clock(100.0)
    timeout = float(rcfg.CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S)
    author = ma.shadow_author(None, original=ORIGINAL, face_reader=_faces,
                              rules=lambda region, defects, attempts=(): "rules",
                              deadline_s=100.0 + timeout + ma._GRACE_S - 0.01, clock=clock)
    assert author(REGION, DEFECTS, ()) == "rules"
    assert fake.calls == []
    [record] = author.shadow_proposals
    assert record["model"]["outcome"] == "skipped"


def test_a_shadow_call_is_made_when_a_full_timeout_and_margin_are_left(ask):
    fake = ask({"pass": True})
    timeout = float(rcfg.CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S)
    author = ma.shadow_author(None, original=ORIGINAL, face_reader=_faces,
                              rules=lambda region, defects, attempts=(): "rules",
                              deadline_s=timeout + ma._GRACE_S + 1.0, clock=Clock(0.0))
    assert author(REGION, DEFECTS, ()) == "rules"
    assert len(fake.calls) == 1 and fake.calls[0]["timeout_s"] == timeout


def test_live_call_timeout_is_cut_to_the_time_left(ask):
    fake = ask(_package_reply())
    author = _author(deadline_s=7.5, clock=Clock(5.0))
    assert author(REGION, DEFECTS, ()) is not None
    assert fake.calls[0]["timeout_s"] == 2.5


def test_live_with_no_time_left_is_none_without_a_call(ask):
    fake = ask(_package_reply())
    author = _author(deadline_s=10.0, clock=Clock(10.0))
    assert author(REGION, DEFECTS, ()) is None
    assert fake.calls == []
    assert author.calls[-1]["outcome"] == "skipped"


def test_the_calls_max_holds_across_the_whole_run(ask, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR_CALLS_MAX", 2)
    fake = ask(*[_package_reply() for _ in range(5)])
    author = _author()
    for index in range(4):
        region = Region(index=index, fingerprints=frozenset({"fp1", "fp2"}), bbox_min=(0, 0, 0),
                        bbox_max=(1, 1, 3), face_indices=frozenset({1, 2}))
        author(region, DEFECTS, ())
    assert len(fake.calls) == 2
    assert [c["outcome"] for c in author.calls] == ["package", "package", "skipped", "skipped"]


def test_shadow_shares_the_run_calls_max(ask, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR_CALLS_MAX", 1)
    fake = ask({"pass": True}, {"pass": True})
    author = ma.shadow_author(None, original=ORIGINAL, face_reader=_faces,
                              rules=lambda region, defects, attempts=(): None)
    author(REGION, DEFECTS, ())
    author(REGION, DEFECTS, ())
    assert len(fake.calls) == 1


def test_unchanged_outside_the_region_is_refused(ask):
    ask(_package_reply(unchanged=["fp7"]))
    author = _author()
    author.observe_part(DEFECTS + (_defect(7),))         # fp7 is the part's, not the region's
    assert author(REGION, DEFECTS + (_defect(7),), ()) is None
    assert "outside region 0" in author.calls[-1]["reason"]


@pytest.mark.parametrize("key", ["tolerance_mm", "answered_held", "reclassified"])
def test_unknown_package_keys_are_refused(ask, key):
    reply = _package_reply()
    reply["package"][key] = True
    ask(reply)
    author = _author()
    assert author(REGION, DEFECTS, ()) is None
    assert key in author.calls[-1]["reason"]


def test_shadow_never_executes_a_valid_model_package_when_the_rules_have_none(ask):
    ask(_package_reply())
    author = ma.shadow_author(None, original=ORIGINAL, face_reader=_faces,
                              rules=lambda region, defects, attempts=(): None)
    assert author(REGION, DEFECTS, ()) is None
    assert author.shadow_proposals[0]["model"]["outcome"] == "package"


def test_a_pass_or_verdict_builds_no_kernel_guards(ask, monkeypatch):
    def boom(shape, defects):
        raise AssertionError("the rim check ran for a reply that is not a package")

    monkeypatch.setattr(ma, "paired_rim_fingerprints", boom)
    ask({"pass": True}, {"verdict": {"mesher_problem": ["fp1"], "reason": "sound"}})
    author = ma.model_author(object(), original=ORIGINAL, face_reader=_faces)
    assert author(REGION, DEFECTS, ()) is None
    assert author(REGION, DEFECTS, ()) is None
    assert [c["outcome"] for c in author.calls] == ["pass", "verdict"]


# FINAL REVIEW M1: a defect's `entity_index` is the CURRENT shape's, so the observation's faces
# must be read from the current shape, not the upload, once a commit has renumbered faces.


def test_the_observation_reads_faces_from_the_shape_the_loop_last_observed(ask):
    ask(_package_reply(), _package_reply())
    read: list = []

    def faces(shape, indices):
        read.append(shape)
        return _faces(shape, indices)

    author = ma.model_author("upload", original=ORIGINAL, face_reader=faces)
    author(REGION, DEFECTS, ())
    author.observe_shape("after-a-split")
    author(REGION, DEFECTS, ())
    assert read == ["upload", "after-a-split"]


def test_a_shadow_author_forwards_the_current_shape_to_its_model(ask):
    ask(_package_reply())
    read: list = []
    author = ma.shadow_author("upload", original=ORIGINAL, rules=lambda *a: None,
                              face_reader=lambda shape, idx: read.append(shape) or {})
    author.observe_shape("current")
    author(REGION, DEFECTS, ())
    assert read == ["current"]


@pytest.mark.parametrize("posture, loop, warned", [
    ("live", "off", True), ("shadow", "off", True), ("live", "live", False),
    ("shadow", "live", False), ("off", "off", False)])
def test_a_model_author_without_the_region_loop_is_warned_about(caplog, posture, loop, warned):
    # Final review M2: the author is read only by the region-scoped driver, so with the region
    # loop off a shadow or live posture is inert - said out loud, as _warn_unreviewed_baseline.
    import logging

    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        caplog.clear()
        rcfg._warn_inert_author(posture, loop)
    assert any("CAD_REPAIR_LLM_AUTHOR" in r.getMessage() for r in caplog.records) is warned


# ---- operation preconditions: refused BEFORE execution, with a named reason --------------------

def _mesh_defect(i: int, code, stage: str | None = "2d") -> EntityDefect:
    d = _defect(i, code=code)
    return dataclasses.replace(d, measurements={"stage": stage} if stage else {})


@pytest.fixture
def mesh_ops_on(monkeypatch):
    for flag in ("CAD_REPAIR_SPLIT_CLOSED_FACES", "CAD_REPAIR_FRAGMENT_BODIES",
                 "CAD_REPAIR_REBUILD_FACE"):
        monkeypatch.setattr(rcfg, flag, True)


def _refuse_case(ask, op, defect, *, solids=1, monkeypatch=None):
    ask(_package_reply(plan=[{"op": op, "fingerprint": "fp1"}], cleared=["fp1"], unchanged=[]))
    if monkeypatch is not None:
        monkeypatch.setattr(ma, "_current_solids", lambda shape: solids)
    author = ma.model_author(object(), original=ORIGINAL, face_reader=_faces, job_id="j")
    assert author(REGION, (defect, _defect(2)), ()) is None
    return author


@pytest.mark.parametrize("op, code, stage, solids, reason", [
    ("split_closed_faces", DefectCode.mesh_overlapping_facets, "2d", 1, "op_does_not_answer"),
    ("fix_face_boundary", DefectCode.mesh_periodic_surface, "2d", 1, "op_does_not_answer"),
    ("split_closed_faces", DefectCode.mesh_periodic_surface, "3d", 1, "stage_precondition"),
    ("split_closed_faces", DefectCode.mesh_periodic_surface, None, 1, "stage_precondition"),
    ("rebuild_face", DefectCode.mesh_overlapping_facets, "3d", 1, "stage_precondition"),
    ("fragment_bodies", DefectCode.mesh_edge_through_face, "2d", 2, "stage_precondition"),
    ("fragment_bodies", DefectCode.mesh_edge_through_face, "3d", 1, "needs_multiple_bodies"),
])
def test_an_operations_preconditions_refuse_before_execution(ask, mesh_ops_on, monkeypatch, op,
                                                            code, stage, solids, reason):
    author = _refuse_case(ask, op, _mesh_defect(1, code, stage), solids=solids,
                          monkeypatch=monkeypatch)
    call = author.calls[-1]
    assert call["outcome"] == "refused" and reason in call["reason"]
    assert [r["reason"] for r in author.refusals] == [reason]
    assert author.refusals[0]["defect"] == "fp1"
    assert "proposal" not in call                          # nothing was built to execute


def test_a_disabled_operation_is_refused_as_not_in_the_catalogue(ask, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", False)
    author = _refuse_case(ask, "split_closed_faces",
                          _mesh_defect(1, DefectCode.mesh_periodic_surface))
    assert "catalogue" in author.calls[-1]["reason"]


def test_preconditions_met_is_a_package(ask, mesh_ops_on, monkeypatch):
    ask(_package_reply(plan=[{"op": "fragment_bodies", "fingerprint": "fp1"}],
                       cleared=["fp1"], unchanged=[]))
    monkeypatch.setattr(ma, "_current_solids", lambda shape: 2)
    author = ma.model_author(object(), original=ORIGINAL, face_reader=_faces)
    defects = (_mesh_defect(1, DefectCode.mesh_edge_through_face, "3d"), _defect(2))
    assert isinstance(author(REGION, defects, ()), TargetPackage)


def test_an_unlocated_defect_cannot_be_named(ask):
    unlocated = dataclasses.replace(_defect(1), fingerprint="")
    ask(_package_reply(plan=[{"op": "fix_face_boundary", "fingerprint": ""}], cleared=[]))
    author = _author()
    assert author(REGION, (unlocated, _defect(2)), ()) is None
    assert author.calls[-1]["outcome"] == "refused"


def test_a_precondition_refusal_runs_nothing_and_spends_one_call(ask, mesh_ops_on):
    fake = ask(_package_reply(plan=[{"op": "split_closed_faces", "fingerprint": "fp1"}],
                              cleared=["fp1"], unchanged=[]))
    author = _author()
    assert author(REGION, (_mesh_defect(1, DefectCode.mesh_periodic_surface, "3d"),
                           _defect(2)), ()) is None
    assert len(fake.calls) == 1 and len(author.calls) == 1


def test_the_catalogue_tells_the_model_each_preconditions_in_words():
    on = catalogue_words.catalogue_text(split_closed_faces=True, fragment_bodies=True,
                                        rebuild_face=True)
    lines = dict(line[2:].split(": ", 1) for line in on.splitlines())
    assert "2D-stage" in lines["split_closed_faces"] and "located" in lines["split_closed_faces"]
    assert "2D-stage" in lines["rebuild_face"]
    assert "3D-stage" in lines["fragment_bodies"] and "more than one" in lines["fragment_bodies"]


def test_the_catalogue_with_every_flag_off_is_the_base_text_unchanged():
    off = catalogue_words.catalogue_text(split_closed_faces=False, fragment_bodies=False,
                                         rebuild_face=False)
    assert off == "\n".join(catalogue_words._BASE)
    assert "stage" not in off


def test_an_unknown_solid_count_fails_open(ask, mesh_ops_on, monkeypatch):
    # No shape, or a count that fails: nothing is refused, the session's own decline stands.
    import meshpipeline.cad.repair.conservative as cons

    def boom(shape):
        raise RuntimeError("no kernel")

    monkeypatch.setattr(cons, "_counts", boom)
    for shape in (None, object()):
        ask(_package_reply(plan=[{"op": "fragment_bodies", "fingerprint": "fp1"}],
                           cleared=["fp1"], unchanged=[]))
        author = ma.model_author(shape, original=ORIGINAL, face_reader=_faces)
        defects = (_mesh_defect(1, DefectCode.mesh_edge_through_face, "3d"), _defect(2))
        assert isinstance(author(REGION, defects, ()), TargetPackage)


def test_the_current_shapes_solids_not_the_uploads_decide(ask, mesh_ops_on, monkeypatch):
    # The upload had one solid; a fill since made two. fragment_bodies must not be refused.
    ask(_package_reply(plan=[{"op": "fragment_bodies", "fingerprint": "fp1"}],
                       cleared=["fp1"], unchanged=[]))
    monkeypatch.setattr(ma, "_current_solids", lambda shape: 2)
    author = ma.model_author(object(), original=dataclasses.replace(ORIGINAL, solids=1),
                             face_reader=_faces)
    defects = (_mesh_defect(1, DefectCode.mesh_edge_through_face, "3d"), _defect(2))
    assert isinstance(author(REGION, defects, ()), TargetPackage)


def test_an_op_answering_a_later_code_on_the_fingerprint_is_not_refused(ask):
    # fp1 carries invalid_brep (sorts before curve_inconsistency) AND curve_inconsistency;
    # fix_face_boundary answers only the second, so the plan is valid.
    two = (_defect(1, code=DefectCode.invalid_brep), _defect(1), _defect(2))
    ask(_package_reply())
    author = _author()
    package = author(REGION, two, ())
    assert isinstance(package, TargetPackage)
    assert package.plan[0]["defect_code"] is DefectCode.curve_inconsistency


@pytest.mark.parametrize("op, code, right", [
    ("split_closed_faces", DefectCode.mesh_periodic_surface, "2d"),
    ("rebuild_face", DefectCode.mesh_overlapping_facets, "2d"),
    ("fragment_bodies", DefectCode.mesh_edge_through_face, "3d"),
])
@pytest.mark.parametrize("stage", ["2d", "3d", "import", "", None])
def test_the_stage_map_matches_targets_for(mesh_ops_on, op, code, right, stage):
    # LOCKSTEP PIN: the author's map must never be STRICTER than the operation's own rule.
    from meshpipeline.cad.repair.operations import catalogue

    operation = next(o for o in catalogue() if o.name == op)
    defect = _mesh_defect(1, code, stage)
    session_accepts = bool(operation.targets_for(defect))
    author_accepts = stage == ma._STAGE_REQUIRED[op]
    assert author_accepts == session_accepts
    assert session_accepts == (stage == right)
