"""The investigator in SHADOW: the rules drive, the tool loop reads and is recorded.

THE DEFINING PROPERTY, and the reason this file exists: a shadow run changes NOTHING the run
delivers. It observes and records. It never commits a package, never alters a verdict, never
moves the deadline for the real author. `test_the_delivered_report_is_identical_whatever_the_shadow_does`
is that property as an assertion: the same part, the same rules driver, the same injected session,
run twice - once with the shadow composite around the driver and once with the bare driver - and
the two `RegionRun.to_dict()` reports must be EQUAL once every `*_s` key is stripped at every
depth. (Not just `timings`: each entry in `packages[].operations` carries its own
`apply_s`/`scan_s`/`measure_s`, which differ by microseconds between two identical runs.)

What else is held here:
  - the roster in shadow is READ_ONLY plus `conclude`, with no `edit`, and an `edit` reply is
    refused as an unknown tool rather than served;
  - no scratch clone is ever built, so there is nothing a shadow could mutate;
  - a `conclude.package` carries a `plan` (there is no edit log to be the plan), is validated
    through `validate_package` against the SESSION'S defects, and is then DISCARDED - the author
    returns None even when the model proposed a perfectly good package;
  - the whole investigation is SKIPPED unless `fits_shadow(region_wall)` - as a TABLE over real
    `InvestigationClock` deadlines, and reading `model_author._GRACE_S` live;
  - a shadow that raises does not move the run.

Hermetic: the model is a scripted fake `converse_tools_sync`, the tools are the real registry
against a `None` shape (every handler refuses, which is an answer), and the region loop walks a
double part. No kernel, no router, no network.
"""
from __future__ import annotations

import json

import pytest

# THE REGION LOOP'S OWN DOUBLES, imported rather than copied: the identity proof has to be about
# the report the LOOP delivers, and a second hand-built part double would be evidence about my
# copy of the loop's fixture instead of about the loop's. Read-only use; that file is another
# lane's.
from tests.unit.cad.test_repair_investigator import (
    DEFECTS,
    ORIGINAL,
    REGION,
    Budget,
    FakeCall,
    FakeReply,
    FakeScratch,
    _faces,
    package_body,
)
from tests.unit.cad.test_repair_region_loop import _SOUND, _TOLERANCE_MM, _author, _budget, _Part

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import investigator as inv
from meshpipeline.cad.repair import investigator_budget as ib
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair import model_author as ma
from meshpipeline.cad.repair.package import TargetPackage
from meshpipeline.cad.repair.region_loop import run_regions
from meshpipeline.cad.repair.session import REVERTED, PackageVerdict

# --- the fakes ---------------------------------------------------------------------------------

class Script:
    """A scripted `converse_tools_sync` that records the tool schemas it was handed."""

    def __init__(self, *replies, repeat=None) -> None:
        self.replies = list(replies)
        self.repeat = repeat
        self.rounds: list[dict] = []

    def __call__(self, messages, *, job_id, timeout_s, tools=(), name="", user_id=""):
        self.rounds.append({"messages": [dict(m) for m in messages], "tools": tuple(tools),
                            "timeout_s": timeout_s})
        if self.replies:
            return self.replies.pop(0), False
        return self.repeat, False

    @property
    def calls(self) -> int:
        return len(self.rounds)


def native(name: str, **args) -> FakeReply:
    return FakeReply(tool_calls=[FakeCall(name, args)])


def text(payload) -> FakeReply:
    return FakeReply(text=payload if isinstance(payload, str) else json.dumps(payload))


def conclude(**args) -> FakeReply:
    return text({"tool": "conclude", "args": args})


#: A plan in the ONE-SHOT schema, which is what a shadow conclusion must carry: there is no edit
#: log in shadow, so the plan cannot be one.
PLAN = [{"op": "fix_face_boundary", "fingerprint": "fp1"}]


@pytest.fixture
def watching(monkeypatch):
    """A shadow investigator with a scripted model and the tool calls it made recorded."""

    def install(*replies, repeat=None, **kw):
        script = Script(*replies, repeat=repeat)
        monkeypatch.setattr(ma, "converse_tools_sync", script)
        monkeypatch.setattr(ma, "_configured", lambda: True)
        seen: list[dict] = []

        def run_tool(ws, name, args, *, budget_s):
            seen.append({"name": name, "args": dict(args) if isinstance(args, dict) else args,
                         "shape": ws.shape, "scratch": getattr(ws, "scratch", None)})
            return it.ToolResult(name, True, {"observation": {}}, 0.01)

        monkeypatch.setattr(it, "run_tool", run_tool)
        author = inv.investigator(None, original=ORIGINAL, face_reader=_faces, job_id="job-1",
                                  tolerance_mm=0.1, shadow=True, **kw)
        return author, script, seen

    return install


# --- THE DEFINING PROPERTY --------------------------------------------------------------------

def _no_seconds(value):
    """`value` with every `*_s` key removed, AT EVERY DEPTH.

    Not just `timings`: each entry in `packages[].operations` carries its own `apply_s`, `scan_s`
    and `measure_s`, and two identical runs differ in those by microseconds - so a comparison
    that stripped only the timings block would fail for a reason that is not the shadow.
    """
    if isinstance(value, dict):
        return {k: _no_seconds(v) for k, v in value.items()
                if not (str(k).endswith("_s") or str(k) == "seconds")}
    if isinstance(value, (list, tuple)):
        return [_no_seconds(v) for v in value]
    return value


def _delivered(author, part) -> dict:
    run = run_regions("original", author=author, tolerance_mm=_TOLERANCE_MM,
                      budget=_budget(), session=part.session(), clock=part.clock)
    return {"report": _no_seconds(run.to_dict()), "shape": run.shape,
            "stopped_because": run.stopped_because,
            "defects_before": [d.to_dict() for d in run.defects_before],
            "defects_after": [d.to_dict() for d in run.defects_after],
            "escalated": run.escalated}


@pytest.mark.parametrize("script_name, replies", [
    ("a shadow that concludes a package", (
        native("look"),
        conclude(package={"intent": "fix the short edge", "plan": [
            {"op": "fix_face_boundary", "fingerprint": "edge:a"}],
            "cleared": ["edge:a"], "unchanged": [],
            "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}))),
    ("a shadow that reaches a mesher-side verdict", (
        conclude(mesher_side={"mesher_problem": ["edge:a"],
                              "reason": "the geometry is sound"}),)),
    ("a shadow that escalates", (
        conclude(escalate={"reason": "this needs a person"}),)),
    ("a shadow that never concludes", ()),
    ("a shadow that asks for the edit tool it does not have", (
        native("edit", op="fix_face_boundary", fingerprint="edge:a"),)),
])
def test_the_delivered_report_is_identical_whatever_the_shadow_does(
        monkeypatch, script_name, replies):
    """THE WHOLE TASK. The run the customer gets is the RULES run, byte for byte."""
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(ma, "converse_tools_sync",
                        Script(*replies, repeat=native("look")))

    plain_part = _Part()
    plain = _delivered(_author(plain_part), plain_part)

    shadow_part = _Part()
    composite = inv.shadow_investigator(
        "original", original=_SOUND, job_id="job-1", tolerance_mm=_TOLERANCE_MM,
        rules=_author(shadow_part), budget=Budget(tool_calls=4, model_calls=6))
    watched = _delivered(composite, shadow_part)

    assert watched == plain, script_name
    # AND THE SHADOW DID RUN. An identity that holds because nothing happened is no evidence.
    assert composite.shadow_investigations, "the shadow recorded nothing at all"
    assert [r["region"] for r in composite.shadow_investigations] == [0, 1]
    assert all(r["rules_proposed"] for r in composite.shadow_investigations)
    assert all(r["investigation"] for r in composite.shadow_investigations)
    assert composite.report()["posture"] == "shadow"
    # THE ONE-SHOT SHADOW COMPOSITE'S NAME CARRIES THE SAME RECORDS, so a reader that already
    # handles `model_author.shadow_author` needs no branch on which shadow it got.
    assert composite.report()["shadow_proposals"] == composite.report()["shadow_investigations"]
    assert composite.shadow_proposals is composite.shadow_investigations


def test_the_shadow_label_is_the_rules_label():
    """The run reports the author that DROVE it. A shadow is not a second author."""
    composite = inv.shadow_investigator("original", original=_SOUND, rules=lambda *a: None)
    assert composite.label == ma.RULES_LABEL != inv.LABEL


def test_the_shadow_carries_the_driver_s_refusals_not_the_watcher_s(monkeypatch):
    """The rules drove, so what a customer is owed an explanation for is the RULES' refusals."""
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(ma, "converse_tools_sync", Script(repeat=native("look")))

    def driver(region, defects, attempts=()):
        return None

    driver.refusals = [{"defect": "fp1", "reason": "held", "detail": "the rules said so"}]
    composite = inv.shadow_investigator(None, original=ORIGINAL, rules=driver,
                                        budget=Budget(tool_calls=1, model_calls=1))
    composite(REGION, DEFECTS, ())
    assert composite.refusals == driver.refusals


def test_a_shadow_that_raises_does_not_move_the_run(monkeypatch):
    """A shadow that breaks must not cost a repair. The rules' package is still returned."""
    monkeypatch.setattr(ma, "_configured", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("the router fell over")

    monkeypatch.setattr(ma, "converse_tools_sync", boom)
    sentinel = object()
    composite = inv.shadow_investigator(None, original=ORIGINAL,
                                        rules=lambda *a, **k: sentinel)
    assert composite(REGION, DEFECTS, ()) is sentinel
    # RECORDED AS A SHADOW THAT SAID NOTHING, never silently dropped.
    [record] = composite.shadow_investigations
    assert record["rules_proposed"] is True


def test_one_record_per_region_per_attempt_and_one_investigation_continued(monkeypatch):
    """A re-call CONTINUES the region's investigation; the composite records the attempt."""
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(ma, "converse_tools_sync", Script(repeat=native("look")))
    composite = inv.shadow_investigator(None, original=ORIGINAL, face_reader=_faces,
                                        rules=lambda *a, **k: None,
                                        budget=Budget(tool_calls=1, model_calls=4))
    composite(REGION, DEFECTS, ())
    composite(REGION, DEFECTS, (PackageVerdict(region_index=0, status=REVERTED,
                                               reason="cleared fp1 remained"),))
    assert [r["attempt"] for r in composite.shadow_investigations] == [1, 2]
    # ONE investigation for the one region, updated in place - not two.
    assert [i["region"] for i in composite.investigations] == [0]


# --- the roster: read-only plus conclude, and no edit -----------------------------------------

def test_the_shadow_roster_names_the_read_only_tools_and_not_edit():
    prompt = inv.system_prompt(shadow=True)
    for spec in it.READ_ONLY:
        assert f'"{spec.name}"' in prompt, spec.name
    # NOT "the word 'edit' is absent" - a read-only tool's own description mentions the edits a
    # live posture made (`measure deviation`), and that prose is the registry's, not this
    # module's. What must be absent is the ROSTER ENTRY and the tool's description.
    for spec in it.EDITING:
        assert f'- "{spec.name}"' not in prompt, spec.name
        assert spec.describe not in prompt, spec.name
    assert "conclude" in prompt and "mesher_side" in prompt and "escalate" in prompt
    # AND THE PLAN IS ASKED FOR, because there is no edit log to be the plan.
    assert '"plan"' in prompt


def test_the_live_roster_still_declares_edit():
    """The shadow roster is a SUBTRACTION in shadow only. Live is untouched."""
    live = inv.system_prompt()
    assert it.EDIT.name in live
    assert "the plan IS the log of the edits you kept" in live


def test_the_shadow_hands_the_model_only_the_read_only_schemas(watching):
    author, script, _ = watching(repeat=conclude(escalate={"reason": "no"}))
    author(REGION, DEFECTS, ())
    handed = [schema.name for schema in script.rounds[0]["tools"]]
    # The read-only tools, then `conclude`: a shadow still has to be able to conclude, and that
    # is the driver's own tool, not one of the registry's.
    assert handed == [spec.name for spec in it.READ_ONLY] + ["conclude"]
    assert it.EDIT.name not in handed


def test_an_edit_reply_is_refused_as_an_unknown_tool(watching):
    author, script, seen = watching(
        native("edit", op="fix_face_boundary", fingerprint="fp1"),
        conclude(escalate={"reason": "I cannot edit"}),
        budget=Budget(tool_calls=4, model_calls=4))
    assert author(REGION, DEFECTS, ()) is None
    record = author.investigations[-1]
    [edit_turn, _] = record["turns"]
    assert edit_turn["tool"] == "edit"
    assert edit_turn["ok"] is False
    assert "unknown tool 'edit'" in edit_turn["error"]
    for spec in it.READ_ONLY:
        assert spec.name in edit_turn["error"]
    # AND THE HANDLER WAS NEVER REACHED. A refusal that still ran the tool is not a refusal.
    assert [row["name"] for row in seen] == []
    # IT STILL COST THE TURN, exactly as any other refused call does.
    assert record["spent"]["tool_calls"] == 1


def test_the_shadow_builds_no_scratch_clone(watching):
    """Nothing a shadow could mutate ever exists: the workspace gets the SESSION'S shape and no
    clone, and the factory is not even called."""
    built: list[object] = []

    def factory(*a, **k):
        built.append(object())
        return FakeScratch()

    author, _, seen = watching(native("look"), conclude(escalate={"reason": "done"}),
                               scratch_factory=factory,
                               budget=Budget(tool_calls=4, model_calls=4))
    current = object()
    author.observe_shape(current)
    author(REGION, DEFECTS, ())
    assert built == []
    assert [row["shape"] for row in seen] == [current]
    assert [row["scratch"] for row in seen] == [None]
    assert author.investigations[-1]["edits"] == []


# --- the conclusion: validated, and discarded -------------------------------------------------

def test_a_shadow_package_is_validated_and_discarded(watching):
    """A perfectly good package, validated through `validate_package` - and the author still
    returns None, because a shadow may not hand the loop a package to commit."""
    author, _, _ = watching(conclude(package={**package_body(), "plan": PLAN}),
                            budget=Budget(tool_calls=4, model_calls=4))
    assert author(REGION, DEFECTS, ()) is None

    record = author.investigations[-1]
    assert record["end"] == "concluded"
    assert record["conclusion"]["form"] == "package"
    assert record["conclusion"]["outcome"] == "package"
    # THE CLAIM IS RECORDED AS VALIDATED DATA, which is the whole point of a shadow arm.
    proposal = record["conclusion"]["proposal"]
    assert proposal["intent"] == package_body()["intent"]
    assert [item["op"] for item in proposal["plan"]] == ["fix_face_boundary"]
    assert proposal["cleared"] == ["fp1"]
    assert json.dumps(record)


def test_a_shadow_package_with_no_plan_is_refused(watching):
    """In shadow the plan cannot be the kept-edit log, because nothing was edited. A package
    without one claims an outcome with no stated means."""
    author, _, _ = watching(conclude(package=package_body()),
                            conclude(escalate={"reason": "then I give up"}),
                            budget=Budget(tool_calls=4, model_calls=4))
    assert author(REGION, DEFECTS, ()) is None
    record = author.investigations[-1]
    refused = record["turns"][0]
    assert refused["tool"] == "conclude" and refused["ok"] is False
    assert "plan" in refused["error"]
    assert record["end"] == "concluded"            # it then escalated, honestly


@pytest.mark.parametrize("plan, reason", [
    ([{"op": "not_an_operation", "fingerprint": "fp1"}], "catalogue"),
    ([{"op": "fix_face_boundary", "fingerprint": "fp-elsewhere"}], "region"),
    ([], "empty plan"),
])
def test_a_shadow_package_is_held_to_the_same_validation_as_a_live_one(watching, plan, reason):
    author, _, _ = watching(conclude(package={**package_body(), "plan": plan}),
                            conclude(escalate={"reason": "I cannot support a claim"}),
                            budget=Budget(tool_calls=4, model_calls=4))
    assert author(REGION, DEFECTS, ()) is None
    refused = author.investigations[-1]["turns"][0]
    assert refused["ok"] is False and reason in refused["error"]


def test_a_shadow_mesher_side_verdict_is_recorded_and_not_a_package(watching):
    author, _, _ = watching(conclude(mesher_side={"mesher_problem": ["fp1"],
                                                  "reason": "the geometry is sound"}),
                            budget=Budget(tool_calls=4, model_calls=4))
    assert author(REGION, DEFECTS, ()) is None
    assert author.verdicts[0]["mesher_problem"] == ["fp1"]


# --- the fit: a table over real deadlines -----------------------------------------------------

def test_the_fit_tables_arithmetic_is_the_arithmetic_it_claims():
    """The tables below are written in LITERAL seconds, so they can actually fail. That is only
    honest while the two numbers they were computed from are what they are, so those are pinned
    here - and this is the one place that needs changing if a lane moves them."""
    assert ma._GRACE_S == 5.0
    assert ib.InvestigationClock(1100.0, reserve_s=60.0, proof_reserve_s=0.0, share=0.5,
                                 clock=lambda: 1000.0).run_left() == 40.0


@pytest.mark.parametrize("left, wall, fits", [
    # LITERAL seconds, with the arithmetic spelled out. `region_wall` is
    # min(budget wall + proof, proof + share * (left - proof)) with proof = 1.0 and share = 0.5 (the proof
    # reserve PLUS the share above it - `share * left` alone sat below the reserve and forced a
    # conclusion before the first tool call), and `fits_shadow` needs left >= region_wall +
    # _GRACE_S (5.0). So a shadow fits from left = 11.0 upwards - BOTH SIDES of that boundary are
    # here, because a one-sided table passes a flipped comparison.
    (0.0, 100.0, False),        # wall 0.0: do not start at all, before the fit is asked
    (4.0, 100.0, False),        # wall 2.5, needs 7.5
    (10.0, 100.0, False),       # wall 5.5, needs 10.5 - one half-second short
    (10.999, 100.0, False),     # wall 5.9995, needs 10.9995
    (11.0, 100.0, True),        # wall 6.0, needs exactly 11.0
    (12.0, 100.0, True),        # wall 6.5, needs 11.5
    (20.0, 100.0, True),
    (10_000.0, 100.0, True),    # the BUDGET'S wall binds now: 100.0 + proof 1.0, needs 106.0
    (10_000.0, 0.0, False),     # a zero wall is "do not start", whatever the clock says
])
def test_a_shadow_investigation_that_cannot_fit_is_skipped_whole(monkeypatch, left, wall, fits):
    script = Script(repeat=conclude(escalate={"reason": "done"}))
    monkeypatch.setattr(ma, "converse_tools_sync", script)
    monkeypatch.setattr(ma, "_configured", lambda: True)
    now = 1000.0
    timing = ib.InvestigationClock(now + left + 60.0, reserve_s=60.0, proof_reserve_s=1.0,
                                   share=0.5, clock=lambda: now)
    author = inv.investigator(None, original=ORIGINAL, face_reader=_faces, shadow=True,
                              policy=timing, clock=lambda: now,
                              budget=Budget(tool_calls=4, model_calls=4, wall_s=wall))
    assert author(REGION, DEFECTS, ()) is None

    record = author.investigations[-1]
    assert (script.calls > 0) is fits, record
    if not fits:
        assert record["end"] == "skipped"
        assert record["spent"] == {"tool_calls": 0, "model_calls": 0, "seconds": 0.0}
        assert record["conclusion"]["outcome"] == "skipped"
        assert "time" in record["conclusion"]["reason"]
    else:
        assert record["end"] == "concluded"


def test_the_skip_reads_the_authors_grace_live(monkeypatch):
    """The margin is `model_author._GRACE_S`, READ at call time. A shadow that copied the number
    would keep running after the author's thread bridge changed its mind."""
    script = Script(repeat=conclude(escalate={"reason": "done"}))
    monkeypatch.setattr(ma, "converse_tools_sync", script)
    monkeypatch.setattr(ma, "_configured", lambda: True)

    def build():
        timing = ib.InvestigationClock(1200.0, reserve_s=0.0, proof_reserve_s=1.0, share=1.0,
                                       clock=lambda: 1000.0)
        return inv.investigator(None, original=ORIGINAL, face_reader=_faces, shadow=True,
                                policy=timing, clock=lambda: 1000.0,
                                budget=Budget(tool_calls=4, model_calls=4, wall_s=150.0))

    monkeypatch.setattr(ma, "_GRACE_S", 0.0)
    first = build()
    first(REGION, DEFECTS, ())
    assert first.investigations[-1]["end"] == "concluded"

    monkeypatch.setattr(ma, "_GRACE_S", 500.0)      # now the whole wall no longer fits
    second = build()
    second(REGION, DEFECTS, ())
    assert second.investigations[-1]["end"] == "skipped"


# --- selection -------------------------------------------------------------------------------

def test_the_shadow_posture_selects_the_composite(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "shadow", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    monkeypatch.setattr(ma, "_configured", lambda: True)
    author, label = ma.select_author(None, original=ORIGINAL, tolerance_mm=0.25)
    assert label == ma.RULES_LABEL
    assert hasattr(author, "shadow_investigations")
    assert author.investigations == []


def test_the_shadow_posture_needs_no_router_to_be_harmless(monkeypatch):
    """With the posture on and no router, the watcher skips with `no_router` and the rules still
    drive - a posture is never a way to break a run."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "shadow", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    monkeypatch.setattr(ma, "_configured", lambda: False)
    author, label = ma.select_author(None, original=ORIGINAL)
    assert label == ma.RULES_LABEL
    # THE RULES STILL PROPOSE, exactly as with the posture off.
    assert isinstance(author(REGION, DEFECTS, ()), TargetPackage)
    assert author.investigations[-1]["end"] == "no_router"
    assert author.investigations[-1]["spent"]["model_calls"] == 0


def test_the_composite_returns_the_driver_s_result_even_if_the_watcher_returns_one(monkeypatch):
    """THE COMPOSITE'S OWN CONTRACT, independently of the watcher's.

    Two separate things stop a shadow's package reaching the loop: `_conclude` discards it, and
    this composite returns the DRIVER'S result whatever the watcher gave back. Written against a
    stub watcher precisely so it tests the second one: with the real watcher, `_conclude` already
    guarantees None, so a composite that returned the watcher's result would look correct and a
    test built on the real watcher would pass a mutation of this line.
    """
    watched: list[tuple] = []
    shadow_package = object()
    rules_package = object()

    def stub_investigator(shape, **kw):
        def author(region, defects, attempts=()):
            watched.append((region.index, len(attempts)))
            return shadow_package

        author.investigations = []
        author.verdicts = []
        author.escalations = []
        author.report = lambda: {"label": inv.LABEL}
        return author

    monkeypatch.setattr(inv, "investigator", stub_investigator)
    composite = inv.shadow_investigator(None, original=ORIGINAL,
                                        rules=lambda *a, **k: rules_package)
    assert composite(REGION, DEFECTS, ()) is rules_package
    assert watched == [(0, 0)]                 # and the watcher WAS asked


def test_the_shadow_never_returns_a_package_even_when_the_rules_do(monkeypatch):
    """The package the loop gets is the DRIVER'S object, identically - not a shadow's copy."""
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(ma, "converse_tools_sync",
                        Script(conclude(package={**package_body(), "plan": PLAN}),
                               repeat=native("look")))
    made: list[object] = []

    def driver(region, defects, attempts=()):
        package = object()
        made.append(package)
        return package

    composite = inv.shadow_investigator(None, original=ORIGINAL, face_reader=_faces,
                                        rules=driver,
                                        budget=Budget(tool_calls=4, model_calls=4))
    got = composite(REGION, DEFECTS, ())
    assert got is made[0]
    assert not isinstance(got, TargetPackage)


def test_the_shadow_conclude_schema_requires_the_plan_it_handles():
    """ONE schema for two postures described the LIVE one: it forbade `plan` while the shadow
    handler refuses a package without it, so the schema and the handler disagreed."""
    live = {s.name: s for s in inv._native_schemas(False)}["conclude"]
    shadow = {s.name: s for s in inv._native_schemas(True)}["conclude"]
    live_pkg = live.parameters["properties"]["package"]
    shadow_pkg = shadow.parameters["properties"]["package"]
    assert "plan" not in live_pkg["properties"]
    assert "plan" in shadow_pkg["properties"]
    assert "plan" in shadow_pkg.get("required", ())
    assert "plan" in shadow_pkg["description"]


# --- the live posture must not reach into the shadow posture ----------------------------------
def test_a_shadow_may_still_conclude_escalate_under_the_live_posture(watching, monkeypatch):
    """A SHADOW HAS NO CLONE. It cannot try an operation, so "keep working on the copy" is advice
    it cannot take, and `escalate` is one of its two honest answers. Refusing it there turned a
    legitimate conclusion into a refusal, and three of those into a runaway."""
    import meshpipeline.settings.cad_repair as settings

    monkeypatch.setattr(settings, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "live", raising=False)
    author, _, _ = watching(repeat=conclude(escalate={"reason": "a person is needed"}))

    assert author(REGION, DEFECTS, ()) is None
    record = author.investigations[-1]
    assert record["conclusion"]["form"] == "escalate", record
    assert record["end"] == "concluded"


def test_the_shadow_schema_still_offers_escalate_under_the_live_posture(monkeypatch):
    import meshpipeline.settings.cad_repair as settings

    monkeypatch.setattr(settings, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "live", raising=False)
    offered = {s.name: s for s in inv._native_schemas(True)}["conclude"]
    assert "escalate" in offered.parameters["properties"]
