"""Tournament repair, WIRED: K candidates gathered, rehearsed on clones, and the winner offered
to the REAL session - where review still has the last word.

Two things this file exists to pin, because both are ways the design could be quietly lost:

  1. `winner() is None` OFFERS NOTHING. Not `rank(...)[0]`, not the best loser. A tournament
     where no candidate survived has learned something, and saying so is the answer.
  2. A WIN IS A NOMINATION. The package the tournament offers goes through the region loop's
     `try_package` on the real session, so its predicate and the review tier judge it exactly as
     they judge a rules package. `test_the_winner_commits_only_through_the_real_session` runs
     both arms - the winner whose claim holds, and the winner whose claim does not - because an
     assertion about only the first would pass an implementation that committed on the clone.

Every budget/threshold assertion is a TABLE over values on BOTH sides of the constant. A one-sided
assertion has silently passed a mutated constant four times on this plan.
"""
from __future__ import annotations

import types

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import tournament as T
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.ladder import TierResult
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.region_loop import run_regions
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import COMMITTED, REVERTED, RepairSession

ORIGINAL = Measure(True, 1, 1, 6, 12, 8, 100.0, 1e-3, 600.0, 1000.0)

REGION = Region(index=0, fingerprints=frozenset({"f1"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(10.0, 10.0, 10.0), face_indices=frozenset({1}), located=True)
ELSEWHERE = Region(index=1, fingerprints=frozenset({"f9"}), bbox_min=None, bbox_max=None,
                   face_indices=frozenset(), located=False)

PREDICATE = Predicate(cleared=frozenset({"f1"}), reclassified=frozenset(),
                      unchanged=frozenset(), max_area_delta_mm2=1.0, max_volume_delta_mm3=1.0)
BUDGET = PackageBudget(max_ops=3, timeout_s=10.0, model_calls=1, deviation_mm=0.5)


def defect(fingerprint: str = "f1") -> EntityDefect:
    return EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                        entity_type="face", entity_index=1, message="x",
                        fingerprint=fingerprint,
                        location={"bbox_min": (1.0, 1.0, 1.0), "bbox_max": (2.0, 2.0, 2.0)})


DEFECTS = (defect(),)


def package(op: str) -> TargetPackage:
    """A package whose plan names `op` - so its plan_key, and therefore its identity as a
    candidate, is exactly "which operation was proposed"."""
    plan = ({"op": types.SimpleNamespace(name=op),
             "targets": (types.SimpleNamespace(fingerprint="f1", targetable=True),),
             "defect_code": DefectCode.open_shell},)
    return TargetPackage(region=REGION, intent=f"do {op}", plan=plan, predicate=PREDICATE,
                         budget=BUDGET)


class Source:
    """A candidate source as the loop sees it, plus a count of how often it was ASKED.

    The count is the whole point of the K table and of the re-call test: "no model call" is a
    claim about whether the source was consulted, and a source that returned None still cost one.
    """

    def __init__(self, op: str | None, *, raises: bool = False) -> None:
        self.op = op
        self.raises = raises
        self.asked = 0

    def __call__(self, region, defects, attempts=()):
        self.asked += 1
        if self.raises:
            raise RuntimeError("this source fell over")
        return None if self.op is None else package(self.op)


def rehearsal(cand: T.Candidate, *, eligible: bool = True, cleared=("f1",),
              revealed=(), seconds: float = 1.0, status: str = T.REHEARSED) -> T.Rehearsal:
    """A recorded rehearsal. `eligible=False` reveals a defect, which is the one ineligibility
    `score` cannot buy off, so a best-loser fallback would still rank it first."""
    return T.Rehearsal(candidate=cand, status=status, kept_ops=1,
                       tier0=TierResult("0", True, 0.01), tier1=TierResult("1", True, 0.01),
                       tier2=None, cleared=frozenset(cleared),
                       revealed=frozenset(() if eligible else ("new:1",)) | frozenset(revealed),
                       remaining=frozenset(), deviation_mm=0.01, seconds=seconds)


class Rehearsals:
    """The rehearsal seam. `verdicts` maps an operation name to whether its candidate is
    eligible; an operation not named is eligible. Records every call's shape and clock."""

    def __init__(self, verdicts: dict[str, bool] | None = None) -> None:
        self.verdicts = dict(verdicts or {})
        self.calls: list[dict] = []

    def __call__(self, candidates, *, wall_s, clock=None, limit=None, **kw):
        cands = tuple(candidates)
        self.calls.append({"keys": [c.plan_key for c in cands],
                           "ops": [c.package.plan[0]["op"].name for c in cands],
                           "wall_s": wall_s, "clock": clock, "limit": limit, **kw})
        return tuple(rehearsal(c, eligible=self.verdicts.get(c.package.plan[0]["op"].name, True))
                     for c in cands)


def build(*, k=3, shadow=False, rules="sew", one_shot="fill", investigate="fragment",
          verdicts=None, rehearsals=None, deadline_s=None, clock=None, **kw):
    """A tournament author with all three sources and the rehearsal injected.

    Returns `(author, sources, rehearsals)`. A source named `None` has nothing to propose; a
    source named `"raise"` comes apart.
    """
    def source(name):
        if name == "raise":
            return Source("x", raises=True)
        return Source(name)

    srcs = {T.RULES: source(rules), T.ONE_SHOT: source(one_shot),
            T.INVESTIGATOR: source(investigate)}
    plays = rehearsals if rehearsals is not None else Rehearsals(verdicts)
    author = T.tournament_author(
        "upload", original=ORIGINAL, k=k, shadow=shadow, deadline_s=deadline_s,
        clock=clock or (lambda: 1000.0), rules=srcs[T.RULES], one_shot=srcs[T.ONE_SHOT],
        investigate=srcs[T.INVESTIGATOR], rehearse_with=plays, **kw)
    return author, srcs, plays


def ops_of(entry: dict) -> list[str]:
    """The operations of a round's candidates, which `package()` makes readable."""
    return [c["source"] for c in entry["candidates"]]


# --- gathering ---------------------------------------------------------------------------------

def test_rules_candidate_always_enters():
    """Even with K=1, and even when the model and the investigator would have proposed too."""
    author, srcs, plays = build(k=1)
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [c["source"] for c in entry["candidates"]] == [T.RULES]
    assert plays.calls[0]["ops"] == ["sew"]


@pytest.mark.parametrize("k,sources,asked", [
    # TABLE over K on both sides of K_DEFAULT (1) and K_MAX (6). `asked` is how many times EACH
    # source was consulted, in rules / one-shot / investigator order: K bounds the SPEND, so a
    # source past K must not be called at all, never merely have its answer discarded.
    (1, [T.RULES], (1, 0, 0)),
    (2, [T.RULES, T.ONE_SHOT], (1, 1, 0)),
    (3, [T.RULES, T.ONE_SHOT, T.INVESTIGATOR], (1, 1, 1)),
    (4, [T.RULES, T.ONE_SHOT, T.INVESTIGATOR], (1, 1, 1)),
    (6, [T.RULES, T.ONE_SHOT, T.INVESTIGATOR], (1, 1, 1)),
])
def test_k_bounds_the_candidate_count(k, sources, asked):
    author, srcs, plays = build(k=k)
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [c["source"] for c in entry["candidates"]] == sources
    assert (srcs[T.RULES].asked, srcs[T.ONE_SHOT].asked, srcs[T.INVESTIGATOR].asked) == asked
    assert plays.calls[0]["limit"] == k


@pytest.mark.parametrize("k,clamped", [(0, 1), (1, 1), (2, 2), (6, 6), (7, 6), (99, 6)])
def test_an_explicit_k_is_clamped_like_the_configured_one(k, clamped):
    """TABLE over both sides of K_MIN and K_MAX: a caller cannot buy a bigger tournament than the
    configured ceiling by passing a number."""
    author, _, plays = build(k=k)
    author(REGION, DEFECTS)
    assert plays.calls[0]["limit"] == clamped


def test_k_falls_back_to_the_configured_value(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", 2, raising=False)
    author, srcs, plays = build(k=None)
    author(REGION, DEFECTS)
    assert plays.calls[0]["limit"] == 2
    assert srcs[T.INVESTIGATOR].asked == 0


def test_duplicate_plans_are_one_candidate_and_resolve_to_rules():
    """Two sources proposing the same operations proposed ONE candidate - and the rehearsal is
    paid for once, attributed to the cheaper, reproducible source."""
    author, _, plays = build(k=3, rules="sew", one_shot="sew", investigate="sew")
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [c["source"] for c in entry["candidates"]] == [T.RULES]
    assert plays.calls[0]["keys"] == [T.plan_key(package("sew"))]


def test_a_source_with_nothing_to_propose_does_not_stop_the_others():
    author, _, plays = build(k=3, rules=None)
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [c["source"] for c in entry["candidates"]] == [T.ONE_SHOT, T.INVESTIGATOR]
    assert [s["outcome"] for s in entry["sources"]][0] == "no_package"


def test_an_absent_source_is_recorded_and_nothing_is_built_behind_it(monkeypatch):
    """A source given as `None` IS ABSENT, and that is not the same as "nobody said".

    If the two were one value, silencing the one-shot author would build the real one behind it -
    and the real one makes a model call. So this asserts the round says `absent` AND that the
    router was never reached.
    """
    from meshpipeline.cad.repair import model_author as ma

    monkeypatch.setattr(ma, "_ask_sync", lambda *a, **k: pytest.fail("a model was asked"))
    author = T.tournament_author("upload", original=ORIGINAL, k=3, rules=Source("sew"),
                                 one_shot=None, investigate=None,
                                 rehearse_with=Rehearsals(), clock=lambda: 1000.0)
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [(s["source"], s["outcome"]) for s in entry["sources"]] == [
        (T.RULES, "candidate"), (T.ONE_SHOT, "absent"), (T.INVESTIGATOR, "absent")]
    assert [c["source"] for c in entry["candidates"]] == [T.RULES]


def test_source_failure_is_recorded_not_raised():
    """The investigator comes apart; rules and the one-shot still compete."""
    author, _, plays = build(k=3, investigate="raise")
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert [c["source"] for c in entry["candidates"]] == [T.RULES, T.ONE_SHOT]
    failed = [s for s in entry["sources"] if s["source"] == T.INVESTIGATOR]
    assert failed[0]["outcome"] == "failed"
    assert "RuntimeError" in failed[0]["reason"]


def test_no_source_proposed_anything_is_an_honest_ending():
    author, _, plays = build(k=3, rules=None, one_shot=None, investigate=None)
    assert author(REGION, DEFECTS) is None
    entry = author.tournaments[-1]
    assert (entry["end"], entry["offered"], entry["candidates"]) == (T.NO_CANDIDATE, None, [])
    assert plays.calls == []


def test_an_unlocated_region_costs_no_source_and_no_rehearsal():
    author, srcs, plays = build(k=3)
    assert author(ELSEWHERE, DEFECTS) is None
    assert author.tournaments[-1]["end"] == T.NOT_LOCATED
    assert (srcs[T.RULES].asked, plays.calls) == (0, [])


# --- the winner --------------------------------------------------------------------------------

def test_the_winner_is_offered_and_is_the_candidates_own_package():
    """Byte-equal plan and predicate, and the SAME object: an author that rebuilt the package
    could rebuild it differently from the one that was rehearsed."""
    author, _, plays = build(k=3, verdicts={"sew": False, "fill": True, "fragment": False})
    offered = author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert offered is not None
    assert entry["offered"] == T.plan_key(package("fill"))
    assert entry["end"] == T.OFFERED
    assert offered.plan == package("fill").plan
    assert offered.predicate == PREDICATE
    assert T.plan_key(offered) == entry["offered"]


def test_no_eligible_returns_none_and_says_why():
    """THE BEST LOSER IS NOT OFFERED. Every candidate revealed a defect, so `winner()` is None -
    and the round offers nothing even though `rank()` has a first element."""
    author, _, plays = build(k=3, verdicts={"sew": False, "fill": False, "fragment": False})
    assert author(REGION, DEFECTS) is None
    entry = author.tournaments[-1]
    assert (entry["end"], entry["offered"]) == (T.NO_ELIGIBLE, None)
    # The record still names every loser, with its score and why it lost.
    assert [c["source"] for c in entry["candidates"]] == [T.RULES, T.ONE_SHOT, T.INVESTIGATOR]
    assert all(c["eligible"] is False for c in entry["candidates"])
    assert all(isinstance(c["score"], list) for c in entry["candidates"])


def test_the_round_records_every_candidates_score_and_seconds():
    author, _, plays = build(k=3, verdicts={"sew": False})
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    first = entry["candidates"][0]
    assert set(first) == {"source", "plan_key", "status", "score", "eligible", "reason",
                          "seconds"}
    assert first["status"] == T.REHEARSED
    assert entry["seconds"] == pytest.approx(3.0)


# --- the re-call -------------------------------------------------------------------------------

def test_recall_offers_the_runner_up_rerehearsed():
    """A reverted package brings the loop back. The next eligible candidate is offered, rehearsed
    AGAIN against the session's CURRENT shape, and NO source is asked a second time."""
    author, srcs, plays = build(k=3, verdicts={"sew": False})
    first = author(REGION, DEFECTS)
    author.observe_shape("moved")
    second = author(REGION, DEFECTS, (types.SimpleNamespace(region_index=0, status=REVERTED),))

    assert T.plan_key(first) == T.plan_key(package("fill"))
    assert T.plan_key(second) == T.plan_key(package("fragment"))
    # ZERO NEW SOURCE CALLS: the ranking is cached, so a re-call spends no model call.
    assert (srcs[T.RULES].asked, srcs[T.ONE_SHOT].asked, srcs[T.INVESTIGATOR].asked) == (1, 1, 1)
    # RE-REHEARSED, and on what the session holds NOW.
    assert len(plays.calls) == 2
    assert plays.calls[0]["session_shape"] == "upload"
    assert plays.calls[1]["session_shape"] == "moved"
    # Only the candidates not yet offered are replayed - the loser of the first round included.
    assert plays.calls[1]["ops"] == ["sew", "fragment"]
    assert author.tournaments[-1]["attempt"] == 2


def test_a_recall_with_nothing_left_to_offer_is_an_honest_ending():
    author, srcs, plays = build(k=1)
    author(REGION, DEFECTS)
    author(REGION, DEFECTS, (types.SimpleNamespace(region_index=0, status=REVERTED),))
    entry = author.tournaments[-1]
    assert (entry["end"], entry["offered"]) == (T.NO_CANDIDATE, None)
    assert srcs[T.RULES].asked == 1
    assert len(plays.calls) == 1


# --- the clock ---------------------------------------------------------------------------------

def test_one_clock_is_built_and_shared_by_every_region():
    """ONE `InvestigationClock` over one deadline. Two clocks over one deadline is how a region
    starves its successors, so the rehearsal is handed the author's own clock, not a new one."""
    author, _, plays = build(k=3, deadline_s=2000.0)
    author(REGION, DEFECTS)
    author(REGION, DEFECTS, (types.SimpleNamespace(region_index=0, status=REVERTED),))
    clocks = {id(call["clock"]) for call in plays.calls}
    assert len(clocks) == 1
    assert plays.calls[0]["clock"] is author.clock
    assert author.clock.deadline_s == 2000.0


@pytest.mark.parametrize("deadline_s,runs", [
    # TABLE over both sides of "none of the run's time is left". The wall is the lesser of the
    # budget's own and this region's share of what the run has left above the reserve, so a run
    # already past its deadline rehearses nothing and says so.
    (1000.0, False), (1000.5, False), (1001.9, False), (1002.0, True), (1010.0, True),
    (None, True),
])
def test_a_region_out_of_time_rehearses_nothing(deadline_s, runs):
    author, _, plays = build(k=3, deadline_s=deadline_s)
    author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert (len(plays.calls) == 1) is runs
    assert (entry["end"] == T.NO_TIME) is (not runs)


def test_the_wall_is_the_regions_share_and_travels_into_the_rehearsal():
    author, _, plays = build(k=3, deadline_s=1000.0 + 100.0, share=0.5, reserve_s=20.0)
    author(REGION, DEFECTS)
    # 100 left, 20 reserved for the final scan, half of the rest for this region.
    assert plays.calls[0]["wall_s"] == pytest.approx(40.0)


# --- shadow ------------------------------------------------------------------------------------

def test_shadow_returns_the_rules_package():
    """The rules drive. The tournament runs, records `would_offer`, and offers nothing."""
    author, _, plays = build(k=3, shadow=True, verdicts={"sew": False})
    driven = author(REGION, DEFECTS)
    entry = author.tournaments[-1]
    assert T.plan_key(driven) == T.plan_key(package("sew"))
    assert (entry["offered"], entry["end"]) == (None, T.WOULD_OFFER)
    assert entry["would_offer"] == T.plan_key(package("fill"))


def test_shadow_with_no_winner_still_returns_the_rules_package():
    author, _, plays = build(k=3, shadow=True,
                             verdicts={"sew": False, "fill": False, "fragment": False})
    driven = author(REGION, DEFECTS)
    assert T.plan_key(driven) == T.plan_key(package("sew"))
    assert author.tournaments[-1]["end"] == T.NO_ELIGIBLE


@pytest.mark.parametrize("deadline_s,ran", [
    # TABLE over `fits_shadow`: the WHOLE wall plus the author's grace must fit above the reserve,
    # because a shadow may never cost the run it watches anything. The wall here is the cheap
    # budget's 60 s and the grace is model_author._GRACE_S (5 s).
    (1000.0, False), (1050.0, False), (1064.9, False), (1065.0, True), (1200.0, True),
])
def test_shadow_is_skipped_when_the_whole_wall_does_not_fit(deadline_s, ran):
    author, srcs, plays = build(k=3, shadow=True, deadline_s=deadline_s)
    driven = author(REGION, DEFECTS)
    # The rules package is returned WHETHER OR NOT the shadow ran: a skipped shadow is still the
    # rules run, unchanged.
    assert T.plan_key(driven) == T.plan_key(package("sew"))
    assert (len(plays.calls) == 1) is ran
    assert (author.tournaments[-1]["end"] == T.NO_TIME) is (not ran)


# --- the report --------------------------------------------------------------------------------

def test_the_report_key_is_the_plans():
    author, _, plays = build(k=3, verdicts={"sew": False})
    author(REGION, DEFECTS)
    author(ELSEWHERE, DEFECTS)
    packages = (types.SimpleNamespace(region_index=0, status=COMMITTED),)
    summary = author.tournament_report(packages)
    assert set(summary) == {"regions", "candidates", "rehearsed", "winners_by_source", "offered",
                            "committed", "seconds"}
    assert summary["regions"] == 2
    assert summary["candidates"] == 3
    assert summary["rehearsed"] == 3
    assert summary["winners_by_source"] == {T.ONE_SHOT: 1}
    assert summary["offered"] == 1
    assert summary["committed"] == 1
    assert summary["seconds"] == pytest.approx(3.0)


def test_committed_counts_only_what_the_real_session_committed():
    """A tournament win is not an acceptance, so `offered` and `committed` are different numbers
    and the report must be able to show a gap between them."""
    author, _, plays = build(k=3, verdicts={"sew": False})
    author(REGION, DEFECTS)
    reverted = (types.SimpleNamespace(region_index=0, status=REVERTED),)
    assert author.tournament_report(reverted)["offered"] == 1
    assert author.tournament_report(reverted)["committed"] == 0
    assert author.tournament_report(())["committed"] == 0


def test_the_conservative_report_block_reads_what_the_author_exposes():
    """The two names `conservative.py` reads off the author. They are read by `getattr`, so a
    rename here would silently drop the tournament from every report."""
    author, _, _ = build(k=3)
    assert isinstance(author.tournaments, list)
    assert callable(author.tournament_report)
    assert callable(author.report)
    assert author.label == T.LABEL


# --- the posture -------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("live", "live"), ("shadow", "shadow"), ("off", "off"), ("", "off"), (None, "off"),
    ("on", "off"), ("true", "off"), ("Live", "off"), ("LIVE", "off"), ("1", "off"),
])
def test_the_posture_fails_closed(monkeypatch, value, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT", value, raising=False)
    assert T.posture() == expected
    assert T.live() is (expected == "live")


@pytest.mark.parametrize("value,label,kind", [
    ("live", T.LABEL, "tournament"),
    ("shadow", "regions-rules-v1", "tournament"),
    ("off", "regions-rules-v1", "rules"),
    ("on", "regions-rules-v1", "rules"),
])
def test_select_author_consults_the_tournament_posture_first(monkeypatch, value, label, kind):
    """With the posture off, `select_author` is byte-for-byte what it was: the investigator
    branch, then today's body."""
    from meshpipeline.cad.repair import model_author as ma

    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT", value, raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "off", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    author, got = ma.select_author("upload", original=ORIGINAL)
    assert got == label
    assert (getattr(author, "tournaments", None) is not None) is (kind == "tournament")


def test_off_posture_select_author_unchanged(monkeypatch):
    """No tournament attribute anywhere on the author, so `conservative.py`'s new report block
    adds no key at all."""
    from meshpipeline.cad.repair import model_author as ma

    monkeypatch.delattr(rcfg, "CAD_REPAIR_TOURNAMENT", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "off", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    author, label = ma.select_author("upload", original=ORIGINAL)
    assert label == "regions-rules-v1"
    assert getattr(author, "tournaments", None) is None
    assert getattr(author, "tournament_report", None) is None


# --- the winner meets the real session ---------------------------------------------------------
#
# NO KERNEL. The shape is a string that records the operations applied to it and the scanner
# answers about the shape it is given, so a rollback restores the old defect list exactly as a
# real re-inspection would. What is proved here is the one thing a rehearsal CANNOT prove: the
# winner's package is executed and judged by the REAL session.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01, surface_area_mm2=1000.0,
                 volume_mm3=8000.0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Op:
    """Clears the one defect its key names - or, for `nothing`, clears nothing at all."""

    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, key: str) -> None:
        self.name = f"fix_{key}"
        self._key = key

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape=f"{shape}+{self._key}", evidence={})


class _Part:
    """One defect, `edge:a`, cleared by `fix_a` and by nothing else."""

    def __init__(self) -> None:
        self.clock = _Clock()
        self.scans = 0
        self.ops = {"a": _Op("a"), "nothing": _Op("nothing")}

    def defect(self):
        return EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.error,
                            entity_type="edge", entity_index=1, message="short",
                            fingerprint="edge:a",
                            location={"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [1.0, 1.0, 1.0]})

    def scanner(self, shape):
        self.scans += 1
        return ([] if "+a" in shape else [self.defect()]), True

    def measurer(self, shape):
        return Measure(**{**_SOUND.to_dict(), "vertices": 40 + str(shape).count("+")})

    def session(self) -> RepairSession:
        return RepairSession("original", measurer=self.measurer, scanner=self.scanner,
                             clock=self.clock)


def _real_package(part: _Part, region, key: str) -> TargetPackage:
    """A package aimed at `key`, claiming the region's own defect. `nothing` clears nothing, so
    the session reverts it on its own predicate - the whole point of the second arm."""
    plan = ({"op": part.ops[key], "defect_code": DefectCode.small_edge,
             "targets": (EntityRef(entity_type="edge", fingerprint="edge:a", entity_index=1),)},)
    return TargetPackage(
        region=region, intent=f"clear with {key}", plan=plan,
        predicate=Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=0.0,
                            max_volume_delta_mm3=0.0),
        budget=PackageBudget(max_ops=4, timeout_s=1000.0, model_calls=0, deviation_mm=0.0))


@pytest.mark.parametrize("winning,status,shape_has", [
    # BOTH ARMS. A tournament win is a NOMINATION: the winner whose claim holds commits, and the
    # winner whose claim does not is REVERTED by the real session. An assertion about only the
    # first would pass an implementation that committed on its clone.
    ("a", COMMITTED, True),
    ("nothing", REVERTED, False),
])
def test_the_winner_commits_only_through_the_real_session(winning, status, shape_has):
    part = _Part()

    def rules(region, defects, attempts=()):
        return _real_package(part, region, "a")

    def one_shot(region, defects, attempts=()):
        return _real_package(part, region, "nothing")

    winner_key = T.plan_key(_real_package(part, REGION, winning))

    def plays(candidates, **kw):
        # The tournament's own judgement, scripted: exactly one candidate survives its clone.
        return tuple(rehearsal(c, eligible=c.plan_key == winner_key) for c in candidates)

    author = T.tournament_author("original", original=_SOUND, k=2, rules=rules,
                                 one_shot=one_shot, investigate=None, rehearse_with=plays,
                                 clock=part.clock)
    run = run_regions("original", author=author, tolerance_mm=0.1,
                      budget=LoopBudget(max_ops=9, stall_limit=9, timeout_s=1000.0),
                      session=part.session(), clock=part.clock, attempts=1)

    assert [p.status for p in run.packages] == [status]
    assert ("+a" in str(run.shape)) is shape_has
    # THE PACKAGE THE SESSION JUDGED IS THE ONE THE TOURNAMENT NOMINATED.
    assert author.tournaments[0]["offered"] == winner_key
    # And the report tells the two apart.
    summary = author.tournament_report(run.packages)
    assert summary["offered"] == 1
    assert summary["committed"] == (1 if status == COMMITTED else 0)


def test_a_tournament_with_no_winner_offers_the_loop_nothing():
    """`winner() is None` through the whole wiring: no package reaches the session, the region is
    left as it is, and the run says so rather than committing a best loser."""
    part = _Part()

    def rules(region, defects, attempts=()):
        return _real_package(part, region, "a")

    def plays(candidates, **kw):
        return tuple(rehearsal(c, eligible=False) for c in candidates)

    author = T.tournament_author("original", original=_SOUND, k=2, rules=rules, one_shot=None,
                                 investigate=None, rehearse_with=plays, clock=part.clock)
    run = run_regions("original", author=author, tolerance_mm=0.1,
                      budget=LoopBudget(max_ops=9, stall_limit=9, timeout_s=1000.0),
                      session=part.session(), clock=part.clock, attempts=1)

    assert run.packages == ()
    assert "+a" not in str(run.shape)
    assert author.tournaments[0]["end"] == T.NO_ELIGIBLE
    assert author.tournament_report(run.packages) == {
        "regions": 1, "candidates": 1, "rehearsed": 1, "winners_by_source": {},
        "offered": 0, "committed": 0, "seconds": pytest.approx(1.0)}
