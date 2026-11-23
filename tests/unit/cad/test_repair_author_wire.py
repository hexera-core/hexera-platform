"""The author wire and validation, as the surface a SECOND author (the investigator) consumes.

`test_repair_model_author.py` pins the model author's behaviour and stays unchanged; this file pins
the extracted names it is now built from: `converse`/`converse_sync` (one bounded call carrying a
WHOLE conversation), `RegionContext`/`PackageGuards`/`Refusal`, `plan_entry`, `validate_package` and
`validate_verdict` with `label` and `model_calls` as parameters, and `rim_guard`.

Hermetic: the model call is a fake `contracts.model_inference.call_repair_review_model`, so these
run with no router, no network and no kernel.
"""
from __future__ import annotations

import asyncio

import pytest

import meshpipeline.contracts.model_inference as mi
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import model_author as ma
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.regions import Region

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=10, edges=20, vertices=12,
                   diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=5000.0,
                   volume_mm3=20000.0)


def _defect(i: int) -> EntityDefect:
    return EntityDefect(
        code=DefectCode.curve_inconsistency, severity=DefectSeverity.error, entity_type="face",
        entity_index=i, message=f"face {i} is broken", measurements={},
        location={"bbox_min": (0.0, 0.0, float(i)), "bbox_max": (1.0, 1.0, float(i) + 1)},
        fingerprint=f"fp{i}")


REGION = Region(index=0, fingerprints=frozenset({"fp1", "fp2"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(1.0, 1.0, 3.0), face_indices=frozenset({1, 2}))
DEFECTS = (_defect(1), _defect(2))

CONVERSATION = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"}, {"role": "user", "content": "u2"}]


class _Result:
    def __init__(self, text: str, ok: bool = True) -> None:
        self.assistant_text = text
        self.ok = ok


class FakeCall:
    """Stands in for `contracts.model_inference.call_repair_review_model`."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, messages, job_id="", name="RepairReview"):
        self.calls.append({"messages": messages, "job_id": job_id, "name": name})
        value = self.results.pop(0) if self.results else _Result("")
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def router(monkeypatch):
    def install(*results):
        fake = FakeCall(*results)
        monkeypatch.setattr(mi, "call_repair_review_model", fake)
        return fake
    return install


# --- the wire ---------------------------------------------------------------------------------

def test_converse_sync_sends_the_whole_conversation(router):
    fake = router(_Result('{"pass": true}'))
    text, parsed = ma.converse_sync(CONVERSATION, job_id="job-1", timeout_s=5.0)
    assert parsed == {"pass": True}
    assert text == '{"pass": true}'
    assert len(fake.calls) == 1
    assert fake.calls[0]["messages"] == CONVERSATION          # all four turns, in order
    assert fake.calls[0]["job_id"] == "job-1"


def test_converse_sync_names_the_trace(router):
    fake = router(_Result("{}"))
    ma.converse_sync(CONVERSATION, job_id="j", timeout_s=5.0, name="RepairInvestigator")
    assert fake.calls[0]["name"] == "RepairInvestigator"
    fake2 = router(_Result("{}"))
    ma.converse_sync(CONVERSATION, job_id="j", timeout_s=5.0)
    assert fake2.calls[0]["name"] == "RepairReview"           # the author's own default


@pytest.mark.parametrize("result", [RuntimeError("boom"), _Result("{}", ok=False)])
def test_converse_sync_is_total_on_a_failed_call(router, result):
    router(result)
    assert ma.converse_sync(CONVERSATION, job_id="j", timeout_s=5.0) == ("", None)


def test_converse_sync_keeps_the_text_when_there_is_no_json(router):
    router(_Result("sorry, no."))
    text, parsed = ma.converse_sync(CONVERSATION, job_id="j", timeout_s=5.0)
    assert parsed is None and text == "sorry, no."


def test_converse_sync_is_total_on_a_hung_call(monkeypatch):
    async def hang(messages, job_id="", name="RepairReview"):
        await asyncio.sleep(30)

    monkeypatch.setattr(mi, "call_repair_review_model", hang)
    monkeypatch.setattr(ma, "_GRACE_S", 0.05)
    assert ma.converse_sync(CONVERSATION, job_id="j", timeout_s=0.05) == ("", None)


def test_ask_sync_is_a_two_message_wrapper(router):
    fake = router(_Result('{"pass": true}'))
    assert ma._ask_sync("sys", "usr", job_id="j", timeout_s=5.0) == {"pass": True}
    assert fake.calls[0]["messages"] == [{"role": "system", "content": "sys"},
                                         {"role": "user", "content": "usr"}]


# --- validation, with the label and the call count as parameters -------------------------------

def _ctx_and_guards():
    ctx = ma.RegionContext(REGION, DEFECTS)
    guards = ma.PackageGuards(DEFECTS, reserved=frozenset(), rims=frozenset(), original=ORIGINAL)
    return ctx, guards


BODY = {"intent": "re-fix the boundary of face 1",
        "plan": [{"op": "fix_face_boundary", "fingerprint": "fp1"}],
        "cleared": ["fp1"], "unchanged": ["fp2"],
        "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}


def test_validate_package_label_and_calls_are_parameters():
    ctx, guards = _ctx_and_guards()
    package, proposal = ma.validate_package(dict(BODY), ctx, guards,
                                            label="regions-investigator-v1", model_calls=7)
    assert package.rationale.startswith("regions-investigator-v1: ")
    assert package.budget.model_calls == 7
    assert proposal["plan"] == [{"op": "fix_face_boundary", "fingerprint": "fp1",
                                 "defect_code": DefectCode.curve_inconsistency.value}]


def test_validate_package_defaults_to_one_model_call():
    ctx, guards = _ctx_and_guards()
    package, _ = ma.validate_package(dict(BODY), ctx, guards, label="x")
    assert package.budget.model_calls == 1


def test_validate_verdict_label_is_a_parameter():
    ctx, _ = _ctx_and_guards()
    verdict = ma.validate_verdict({"mesher_problem": ["fp1"], "reason": "sound"}, ctx,
                                  label="regions-investigator-v1")
    assert verdict["author"] == "regions-investigator-v1"
    assert verdict["mesher_problem"] == ["fp1"] and verdict["region"] == 0


def test_plan_entry_refuses_with_a_refusal():
    ctx, guards = _ctx_and_guards()
    with pytest.raises(ma.Refusal):
        ma.plan_entry({"op": "fix_face_boundary", "fingerprint": "nowhere"}, ctx, guards)


def test_rim_guard_records_the_parts_rims_as_refusals():
    refusals: list[dict] = []
    rims = ma.rim_guard(None, frozenset({"fp1"}), DEFECTS, refusals)
    assert rims == frozenset({"fp1"})
    assert [r["defect"] for r in refusals] == ["fp1"]
    assert refusals[0]["reason"]


def test_rim_guard_with_no_rims_records_nothing():
    refusals: list[dict] = []
    assert ma.rim_guard(None, frozenset(), DEFECTS, refusals) == frozenset()
    assert refusals == []


# --- the private names the existing suite and the region loop still use ------------------------

def test_private_aliases_still_resolve():
    assert ma._Refused is ma.Refusal
    assert ma._Context is ma.RegionContext
    assert ma._Guards is ma.PackageGuards
    assert ma._plan_item is ma.plan_entry
    ctx, guards = _ctx_and_guards()
    package, _ = ma._package(dict(BODY), ctx, guards)
    assert package.rationale.startswith(f"{ma.LABEL}: ")       # the author's own label
    assert package.budget.model_calls == 1
    assert ma._verdict({"mesher_problem": ["fp1"], "reason": "sound"}, ctx)["author"] == ma.LABEL


def test_the_run_timeout_still_bounds_the_package(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 123.0)
    ctx, guards = _ctx_and_guards()
    package, _ = ma.validate_package(dict(BODY), ctx, guards, label="x")
    assert package.budget.timeout_s == 123.0
