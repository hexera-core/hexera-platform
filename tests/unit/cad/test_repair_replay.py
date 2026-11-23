# Responsibility: Pin the agent-run replay contract - the per-region attempt budget as the LOOP
#                 applies it, the committed-package record, and `replay.replay_packages`
#                 re-applying those records with no model call and no guessed region.
# Boundaries: no kernel. The STEP read, the measure and the STEP write are doubles, and the session
#             is the real `RepairSession` over a string shape (the region-loop unit harness's own
#             idiom), so what is proved is the replay's bookkeeping and its honesty about a miss.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import (
    brep,
    caps,
    conservative,
    grind,
    operations,
    package_review,
    replay,
)
from meshpipeline.cad.repair import region_loop as RL
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.session import RepairSession

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)
_TOLERANCE_MM = 0.1
#: Two defects 100 mm apart on a 200 mm part: `decompose` merges within 1% of the diagonal, so
#: these are two regions and the fixture is only evidence of anything while they stay apart.
_BOXES = {"a": (0.0, 1.0), "b": (100.0, 101.0)}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _defect(key: str) -> EntityDefect:
    low, high = _BOXES[key]
    return EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.error,
                        entity_type="edge", entity_index={"a": 1, "b": 2}[key],
                        message=f"a short edge at {low}", fingerprint=f"edge:{key}",
                        location={"bbox_min": [low, low, low], "bbox_max": [high, high, high]})


class _Op:
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, clock: _Clock, key: str) -> None:
        self.name = f"fix_{key}"
        self._clock = clock
        self._key = key

    def apply(self, shape, targets, *, tolerance_mm):
        self._clock.now += 0.5
        return OpOutcome(shape=f"{shape}+{self._key}", evidence={})


class _Part:
    def __init__(self, *, keys: tuple[str, ...] = ("a", "b")) -> None:
        self.clock = _Clock()
        self.keys = keys
        self.ops = {key: _Op(self.clock, key) for key in ("a", "b", "nothing")}

    def scanner(self, shape):
        self.clock.now += 1.0
        return [_defect(key) for key in self.keys if f"+{key}" not in str(shape)], True

    def measurer(self, shape):
        return Measure(**{**_SOUND.to_dict(), "vertices": 40 + str(shape).count("+")})

    def session(self) -> RepairSession:
        return RepairSession("original", measurer=self.measurer, scanner=self.scanner,
                             clock=self.clock)


def _author(part: _Part, *, aim_elsewhere: tuple[int, ...] = ()):
    def author(region, defects, attempts):
        defect = defects[0]
        key = "nothing" if region.index in aim_elsewhere else defect.fingerprint.split(":")[1]
        return TargetPackage(
            region=region, intent=f"clear {defect.fingerprint}",
            plan=({"op": part.ops[key], "defect_code": DefectCode.small_edge,
                   "targets": (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                                         entity_index=defect.entity_index),)},),
            predicate=Predicate(cleared=frozenset({defect.fingerprint}),
                                reclassified=frozenset(), unchanged=frozenset(),
                                max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0),
            budget=PackageBudget(max_ops=4, timeout_s=1000.0, model_calls=0, deviation_mm=0.0))

    return author


def _counting_author(part: _Part, *, aim_elsewhere: tuple[int, ...] = ()):
    """The always-wrong author, plus a per-region tally of how often it was asked."""
    inner = _author(part, aim_elsewhere=aim_elsewhere)
    asked: dict[int, int] = {}

    def author(region, defects, attempts):
        asked[region.index] = asked.get(region.index, 0) + 1
        return inner(region, defects, attempts)

    author.asked = asked  # type: ignore[attr-defined]
    return author


def _run(part: _Part, author, **kw):
    return RL.run_regions("original", author=author, tolerance_mm=_TOLERANCE_MM,
                          budget=LoopBudget(max_ops=99, stall_limit=9, timeout_s=10_000.0),
                          session=part.session(), clock=part.clock, **kw)


@pytest.fixture(autouse=True)
def _postures(monkeypatch):
    """Every posture off by default, so each test turns on exactly what it is about."""
    for name, value in (("CAD_REPAIR_BUDGET_ATTEMPTS", "off"), ("CAD_REPAIR_REGION_LOOP", "off"),
                        ("CAD_REPAIR_INVESTIGATOR", "off"), ("CAD_REPAIR_TOURNAMENT", "off"),
                        ("CAD_REPAIR_PACKAGE_REVIEW", "off")):
        monkeypatch.setattr(rcfg, name, value, raising=False)


# --- the budget as the LOOP applies it --------------------------------------------------------


@pytest.mark.parametrize("flat", [1, 2, 3, 5])
def test_off_is_todays_attempt_count_and_no_key(monkeypatch, flat):
    """FLAG-OFF IS BYTE-IDENTICAL: the flat setting governs and the report gains no key. Asserted
    over a TABLE of flat values, because a single value could be matched by a mutated tier."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_ATTEMPTS", flat)
    # The tier would say HEAVY (10) if anything were reading it, so this is two-sided.
    monkeypatch.setattr(RL.grind, "tier_of", lambda *a, **k: grind.HEAVY)
    part = _Part()
    author = _counting_author(part, aim_elsewhere=(0, 1))
    run = _run(part, author)

    assert run.attempt_budget == {}
    assert "attempt_budget" not in run.to_dict()
    assert "packages_replay" not in run.to_dict()
    assert author.asked == {0: flat, 1: flat}


@pytest.mark.parametrize(("tier", "expected"), [
    (grind.CHEAP, 3), (grind.STANDARD, 6), (grind.HEAVY, 10), (grind.UNKNOWN, 3), ("nonsense", 3),
])
def test_the_budget_governs_how_often_a_region_is_authored_for(monkeypatch, tier, expected):
    """THE POINT OF THE TASK, asserted on what the author was ASKED and not only on the record:
    a key that said 6 while the loop offered 3 would be a report about nothing."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_ATTEMPTS", 3)
    monkeypatch.setattr(RL.grind, "tier_of", lambda *a, **k: tier)
    part = _Part()
    author = _counting_author(part, aim_elsewhere=(0, 1))
    run = _run(part, author)

    assert run.attempt_budget == {"tier": tier, "attempts": expected,
                                  "rule": RL.BUDGET_RULE_TIER}
    assert run.to_dict()["attempt_budget"] == run.attempt_budget
    assert author.asked == {0: expected, 1: expected}


def test_an_explicit_attempts_argument_still_wins_in_the_loop(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setattr(RL.grind, "tier_of", lambda *a, **k: grind.HEAVY)
    part = _Part()
    author = _counting_author(part, aim_elsewhere=(0, 1))
    run = _run(part, author, attempts=2)

    assert run.attempt_budget == {"tier": "", "attempts": 2, "rule": RL.BUDGET_RULE_EXPLICIT}
    assert author.asked == {0: 2, 1: 2}


def test_the_run_budget_is_still_the_outer_bound(monkeypatch):
    """A HEAVY part does not get ten attempts if the run's operation allowance runs out first."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setattr(RL.grind, "tier_of", lambda *a, **k: grind.HEAVY)
    part = _Part()
    author = _counting_author(part)
    run = RL.run_regions("original", author=author, tolerance_mm=_TOLERANCE_MM,
                         budget=LoopBudget(max_ops=1, stall_limit=9, timeout_s=10_000.0),
                         session=part.session(), clock=part.clock)
    assert run.attempt_budget["attempts"] == 10
    assert sum(author.asked.values()) < 10
    assert run.stopped_because in ("op_budget", "escalated", "regions_exhausted")


def test_two_rejections_still_escalate_under_budget(monkeypatch):
    """THE REVIEWER'S CAP IS NOT A FUNCTION OF THE BUDGET. A HEAVY part buys ten attempts and the
    second review reject still ends the region - no retry-until-accept."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    monkeypatch.setattr(RL.grind, "tier_of", lambda *a, **k: grind.HEAVY)
    monkeypatch.setattr(package_review, "posture", lambda: "live")
    monkeypatch.setattr(package_review, "review_package_objection",
                        lambda *a, **k: (package_review.REJECT, "no", None))
    part = _Part(keys=("a",))
    author = _counting_author(part)
    run = _run(part, author)

    assert run.attempt_budget["attempts"] == 10
    # TWO attempts in the region, not ten: one revert and then the escalation.
    assert author.asked == {0: 2}
    assert run.escalated == (0,)
    assert run.stopped_because == "escalated"


# --- the committed-package record -------------------------------------------------------------


def test_replay_record_only_for_agent_runs(monkeypatch):
    """`packages_replay` IS AN AGENT-RUN KEY. A rules-only run is still replayed from its bytes,
    so it must gain no key - asserted over the table of posture combinations."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    for investigator, tournament, expected in (
            ("off", "off", False),
            ("shadow", "off", True),
            ("live", "off", True),
            ("off", "live", True),
            ("off", "shadow", False),   # a shadow tournament rehearses; it authors nothing
            ("live", "live", True)):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", investigator)
        monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT", tournament, raising=False)
        part = _Part()
        run = _run(part, _author(part))
        assert [p.committed for p in run.packages] == [True, True]
        assert bool(run.packages_replay) is expected, (investigator, tournament)
        assert ("packages_replay" in run.to_dict()) is expected
        if expected:
            assert len(run.packages_replay) == 2


def test_the_region_loop_posture_also_gates_the_record(monkeypatch):
    """Off, there is no agent-driven run to replay however the agent postures read."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live")
    part = _Part()
    assert _run(part, _author(part)).packages_replay == ()


def test_only_committed_packages_are_recorded(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live")
    part = _Part()
    run = _run(part, _author(part, aim_elsewhere=(0,)))
    committed = [p for p in run.packages if p.committed]
    assert 0 < len(committed) < len(run.packages)
    assert len(run.packages_replay) == len(committed)


def test_the_record_carries_the_runs_tolerance_and_the_sorted_fingerprints(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live")
    part = _Part()
    run = _run(part, _author(part))
    record = run.packages_replay[0]

    assert set(record) == {"region_fingerprints", "plan", "predicate", "tolerance_mm",
                           "review_tier", "reviewed"}
    assert record["tolerance_mm"] == _TOLERANCE_MM
    assert record["region_fingerprints"] == sorted(record["region_fingerprints"])
    # THE LOOP'S TOLERANCE REACHED THE PLAN ITEM, which is what makes the record self-contained.
    assert [item["tolerance_mm"] for item in record["plan"]] == [_TOLERANCE_MM]
    assert record["predicate"]["cleared"] == sorted(record["predicate"]["cleared"])
    assert record["reviewed"] is None and record["review_tier"] is None   # review posture off


# --- the replay itself ------------------------------------------------------------------------


@pytest.fixture
def _no_kernel(monkeypatch):
    """The STEP read, the measure and the STEP write, doubled; the session is the real one."""
    part = _Part()
    monkeypatch.setattr(brep, "_read_shape", lambda path: "original")
    monkeypatch.setattr(caps, "measure", part.measurer)
    written: list = []
    monkeypatch.setattr(conservative, "_write_step", lambda shape, dest, **k: written.append(
        (str(shape), dest)))

    class _Session(RepairSession):
        def __init__(self, shape, **kw):
            super().__init__(shape, measurer=part.measurer, scanner=part.scanner,
                             clock=part.clock)

    monkeypatch.setattr(replay, "RepairSession", _Session)
    # A record names its operations; these doubles are the catalogue they are named from.
    monkeypatch.setattr(operations, "OPERATIONS", tuple(part.ops.values()))
    part.written = written  # type: ignore[attr-defined]
    return part


def _records(part: _Part, monkeypatch) -> list[dict]:
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live")
    run = _run(part, _author(part))
    assert len(run.packages_replay) == 2
    return [dict(r) for r in run.packages_replay]


def test_replay_reproduces_the_commits(monkeypatch, tmp_path, _no_kernel):
    records = _records(_Part(), monkeypatch)
    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)

    outcomes = [o["outcome"] for o in report.diagnostics["replay"]]
    assert outcomes == [replay.REPLAY_COMMITTED, replay.REPLAY_COMMITTED]
    values = {m.name: m.value for m in report.measurements}
    assert values["replay_records"] == 2.0
    assert values["replay_committed"] == 2.0
    assert values["replay_misses"] == 0.0
    assert report.diagnostics["valid"] is True
    # THE RESULT WAS WRITTEN, and it is the repaired shape rather than the upload.
    assert _no_kernel.written and "+a" in _no_kernel.written[0][0]


def test_replay_makes_no_model_call(monkeypatch, tmp_path, _no_kernel):
    """REVIEW IS THE RECORDED VERDICT. Both the reviewer and the author seams are patched to
    raise, so any call at all fails the test rather than quietly costing a model call."""
    records = _records(_Part(), monkeypatch)

    def _explode(*a, **k):
        raise AssertionError("the replay asked a model")

    monkeypatch.setattr(package_review, "review_package_objection", _explode)
    monkeypatch.setattr(package_review, "review_package", _explode, raising=False)
    # ... AND WITH THE REVIEW POSTURE LIVE, which is the case that would have called it.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    for record in records:
        record["reviewed"] = package_review.ACCEPT
        record["review_tier"] = "reviewer"

    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)
    assert {m.name: m.value for m in report.measurements}["replay_committed"] == 2.0
    # THE POSTURE IS PUT BACK, whatever happened inside.
    assert package_review.posture() == "live"


def test_replay_restores_the_review_posture_even_on_a_failure(monkeypatch, tmp_path, _no_kernel):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "live")
    monkeypatch.setattr(caps, "measure", lambda shape: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", [])
    assert package_review.posture() == "live"


def test_replay_miss_is_recorded_not_guessed(monkeypatch, tmp_path, _no_kernel):
    """A RECORD WHOSE REGION IS NOT IN THIS DECOMPOSITION IS A MISS, not the nearest region."""
    records = _records(_Part(), monkeypatch)
    records[0]["region_fingerprints"] = ["edge:ghost"]

    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)
    outcomes = [o["outcome"] for o in report.diagnostics["replay"]]
    assert outcomes == [replay.REPLAY_MISS, replay.REPLAY_COMMITTED]
    assert report.diagnostics["replay"][0]["region_fingerprints"] == ["edge:ghost"]
    assert {m.name: m.value for m in report.measurements}["replay_misses"] == 1.0


@pytest.mark.parametrize("extra", [["edge:a", "edge:b"], [], ["edge:a", "edge:ghost"]])
def test_a_partially_overlapping_region_is_also_a_miss(monkeypatch, tmp_path, _no_kernel, extra):
    """EXACT MATCH ONLY. A decomposition that merged two of the original's regions overlaps both
    records, and applying either would aim a claim at geometry it never claimed."""
    records = _records(_Part(), monkeypatch)
    records[0]["region_fingerprints"] = extra
    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)
    assert report.diagnostics["replay"][0]["outcome"] == replay.REPLAY_MISS


@pytest.mark.parametrize(("reviewed", "expected"), [
    (None, replay.REPLAY_COMMITTED),            # no reviewer was in force: replayable
    ("accept", replay.REPLAY_COMMITTED),
    ("reject", replay.REPLAY_NOT_ACCEPTED),
    ("", replay.REPLAY_NOT_ACCEPTED),
    ("Accept", replay.REPLAY_NOT_ACCEPTED),     # fail-closed on anything but the exact verdict
])
def test_the_recorded_verdict_decides_whether_a_record_is_replayed(
        monkeypatch, tmp_path, _no_kernel, reviewed, expected):
    records = _records(_Part(), monkeypatch)
    records[0]["reviewed"] = reviewed
    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)
    assert report.diagnostics["replay"][0]["outcome"] == expected


def test_an_empty_record_list_is_an_honest_no_op(monkeypatch, tmp_path, _no_kernel):
    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", [])
    assert report.diagnostics["replay"] == []
    assert {m.name: m.value for m in report.measurements}["replay_committed"] == 0.0


def test_a_record_survives_the_json_report_and_still_replays(monkeypatch, tmp_path, _no_kernel):
    """PR #494 review (region_loop.py:503). The record reaches a caller through the delivered
    JSON report, so it must be plain data: an operation by NAME and its targets as fields, not a
    live op and `EntityRef`s that `default=str` flattens into strings nobody can apply."""
    import json

    part = _Part()
    records = json.loads(json.dumps(_records(part, monkeypatch), default=str))
    report = replay.replay_packages(tmp_path / "in.step", tmp_path / "out.step", records)

    assert [o["outcome"] for o in report.diagnostics["replay"]] == [
        replay.REPLAY_COMMITTED, replay.REPLAY_COMMITTED]


def test_a_committed_gap_prepass_package_is_recorded_first(monkeypatch):
    """Greptile PRRT_kwDOUFq_Ps66QAIz: pre-pass packages joined `packages` but never `replays`, so
    every pre-pass split was lost on replay. A committed one is recorded, before the walk's."""
    import dataclasses

    from meshpipeline.cad.repair.session import PackageVerdict

    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live")
    part = _Part()
    author = _author(part)
    seen: dict = {}

    def prepass(work, regions, **kw):
        region = regions[0]
        package = author(region, [d for d in work.defects()
                                  if d.fingerprint in region.fingerprints], ())
        package = dataclasses.replace(package, intent="the pre-pass's split")
        seen["region"] = sorted(region.fingerprints)
        return [(region.index, package, PackageVerdict(region_index=region.index,
                                                        status="reverted", reason="x")),
                (region.index, package, PackageVerdict(region_index=region.index,
                                                        status="committed"))], []

    monkeypatch.setattr(RL, "_meshability_pass", prepass)
    run = _run(part, author)
    records = [dict(r) for r in run.packages_replay]
    assert records and records[0]["region_fingerprints"] == seen["region"]
    assert sum(1 for r in records if r["region_fingerprints"] == seen["region"]) >= 1
    assert len(records) == 1 + sum(1 for p in run.packages[2:] if p.committed)
