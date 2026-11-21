"""Tournament repair: the candidate set, and the one rule that orders rehearsals.

Every budget/threshold assertion here is a TABLE over several values. A one-sided assertion on
this plan has three times silently passed a mutated constant, so a single `>` is not evidence.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import pathlib

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import tournament as T
from meshpipeline.cad.repair.ladder import TierResult
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region

REGION = Region(index=0, fingerprints=frozenset({"f1"}), bbox_min=None, bbox_max=None,
                located=False)
PREDICATE = Predicate(cleared=frozenset({"f1"}), reclassified=frozenset(),
                      unchanged=frozenset(), max_area_delta_mm2=1.0, max_volume_delta_mm3=1.0)
BUDGET = PackageBudget(max_ops=3, timeout_s=10.0, model_calls=1, deviation_mm=0.5)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pkg(plan, intent="fix"):
    return TargetPackage(region=REGION, intent=intent, plan=tuple(plan), predicate=PREDICATE,
                         budget=BUDGET)


def tier(name, passed, seconds=0.1):
    return TierResult(name, passed, seconds)


def reh(source=T.RULES, *, status=T.REHEARSED, kept_ops=1, tier0=True, tier1=True, tier2=True,
        cleared=("f1",), revealed=(), remaining=(), deviation_mm=0.01, seconds=1.0, plan=None,
        reason=""):
    plan = plan if plan is not None else [{"op": "sew", "fingerprint": "f1",
                                           "defect_code": "open_shell"}]
    return T.Rehearsal(
        candidate=T.candidate(source, pkg(plan)),
        status=status, kept_ops=kept_ops,
        tier0=None if tier0 is ... else tier("0", tier0),
        tier1=None if tier1 is ... else tier("1", tier1),
        tier2=None if tier2 is ... else tier("2", tier2),
        cleared=frozenset(cleared), revealed=frozenset(revealed), remaining=frozenset(remaining),
        deviation_mm=deviation_mm, seconds=seconds, reason=reason)


# --- posture ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("off", False), ("on", False), ("Live", False), ("LIVE", False),
    ("true", False), ("1", False), ("shadow", False), ("live", True),
])
def test_posture_fails_closed(monkeypatch, value, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT", value, raising=False)
    assert T.live() is expected


def test_posture_off_when_the_flag_is_absent(monkeypatch):
    """The flag is plumbed by another lane. Until it lands, the tournament is OFF."""
    monkeypatch.delattr(rcfg, "CAD_REPAIR_TOURNAMENT", raising=False)
    assert T.live() is False


@pytest.mark.parametrize("value,expected", [
    ("0", 1), ("-4", 1), ("1", 1), ("2", 2), ("3", 3), ("6", 6), ("7", 6), ("9", 6), ("99", 6),
    (4, 4), (None, 1), ("", 1), ("three", 1), ("2.9", 1),
])
def test_k_is_clamped_both_ways(monkeypatch, value, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", value, raising=False)
    assert T.k() == expected


def test_k_defaults_to_one_when_the_flag_is_absent(monkeypatch):
    """K=1 is today's behaviour: one candidate, no tournament."""
    monkeypatch.delattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", raising=False)
    assert T.k() == T.K_DEFAULT == 1


def test_an_out_of_range_k_warns(monkeypatch, caplog):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", "9", raising=False)
    with caplog.at_level("WARNING"):
        assert T.k() == 6
    assert any("9" in r.getMessage() for r in caplog.records), caplog.text


# --- candidates ------------------------------------------------------------------------------

def test_plan_key_is_the_canonical_json_sha256():
    plan = [{"fingerprint": "f1", "op": "sew", "defect_code": "open_shell"}]
    expected = sha(json.dumps(plan, sort_keys=True, separators=(",", ":")))
    assert T.plan_key(pkg(plan)) == expected


def test_key_ignores_dict_order_but_not_content():
    a = pkg([{"op": "sew", "fingerprint": "f1"}])
    b = pkg([{"fingerprint": "f1", "op": "sew"}])
    c = pkg([{"fingerprint": "f2", "op": "sew"}])
    assert T.plan_key(a) == T.plan_key(b)
    assert T.plan_key(a) != T.plan_key(c)


def test_key_ignores_intent_and_depends_only_on_the_plan():
    """Two sources proposing the same operations ARE the same candidate, whatever they call it."""
    left = T.plan_key(pkg([{"op": "sew"}], intent="a"))
    right = T.plan_key(pkg([{"op": "sew"}], intent="b"))
    assert left == right


@pytest.mark.parametrize("order,kept", [
    ((T.RULES, T.ONE_SHOT, T.INVESTIGATOR), T.RULES),
    ((T.ONE_SHOT, T.RULES, T.INVESTIGATOR), T.RULES),
    ((T.INVESTIGATOR, T.ONE_SHOT, T.RULES), T.RULES),
    ((T.INVESTIGATOR, T.ONE_SHOT), T.ONE_SHOT),
    ((T.INVESTIGATOR,), T.INVESTIGATOR),
])
def test_duplicate_plans_collapse_to_the_first_source(order, kept):
    plan = [{"op": "sew", "fingerprint": "f1"}]
    out = T.dedupe([T.candidate(s, pkg(plan)) for s in order])
    assert len(out) == 1
    assert out[0].source == kept


def test_dedupe_keeps_distinct_plans_in_input_order():
    cands = [T.candidate(T.RULES, pkg([{"op": "sew"}])),
             T.candidate(T.ONE_SHOT, pkg([{"op": "fill"}])),
             T.candidate(T.INVESTIGATOR, pkg([{"op": "sew"}]))]
    out = T.dedupe(cands)
    assert [c.source for c in out] == [T.RULES, T.ONE_SHOT]


# --- the scoring rule ------------------------------------------------------------------------

def best(rehearsals):
    return T.rank(rehearsals)[0]


@pytest.mark.parametrize("cleared", [("f1",), ("f1", "f2"), ("f1", "f2", "f3", "f4", "f5")])
def test_revealing_candidate_never_beats_a_clean_one(cleared):
    """No amount of cleared defects buys a revealed one. A table, so a reordered key shows."""
    dirty = reh(T.RULES, cleared=cleared, revealed=("x",))
    clean = reh(T.ONE_SHOT, cleared=("f1",), revealed=())
    assert best([dirty, clean]) is clean
    assert best([clean, dirty]) is clean


@pytest.mark.parametrize("a,b,winner_is_a", [
    (True, None, True),     # a measured pass beats no answer
    (True, False, True),    # a measured pass beats a refusal
    (None, False, True),    # no answer outranks a refusal
    (None, True, False),
    (False, True, False),
    (False, None, False),
])
def test_no_answer_never_outranks_a_measured_pass(a, b, winner_is_a):
    ra, rb = reh(T.ONE_SHOT, tier2=a), reh(T.INVESTIGATOR, tier2=b)
    assert (best([ra, rb]) is ra) is winner_is_a
    assert (best([rb, ra]) is ra) is winner_is_a


@pytest.mark.parametrize("a,b,winner_is_a", [
    (True, None, True), (True, False, True), (None, False, True), (False, True, False),
])
def test_the_same_three_valued_order_holds_at_tier_one(a, b, winner_is_a):
    ra, rb = reh(T.ONE_SHOT, tier1=a), reh(T.INVESTIGATOR, tier1=b)
    assert (best([ra, rb]) is ra) is winner_is_a


def test_tier_two_outranks_tier_one_in_the_key():
    """A tier-2 pass with a tier-1 refusal beats a tier-1 pass with a tier-2 refusal: the rungs
    are ordered, not summed."""
    high = reh(T.ONE_SHOT, tier1=False, tier2=True)
    low = reh(T.INVESTIGATOR, tier1=True, tier2=False)
    assert best([low, high]) is high


@pytest.mark.parametrize("dev", [0.0, 0.001, 0.5, 9.0, 1e9])
def test_unmeasured_deviation_ranks_last_in_its_tier(dev):
    measured = reh(T.INVESTIGATOR, deviation_mm=dev)
    unmeasured = reh(T.ONE_SHOT, deviation_mm=None)
    assert best([unmeasured, measured]) is measured
    assert T.score(unmeasured)[6] == -math.inf


@pytest.mark.parametrize("small,large", [(0.0, 0.001), (0.01, 0.02), (0.1, 5.0), (1.0, 1.0001)])
def test_less_deviation_wins(small, large):
    a = reh(T.ONE_SHOT, deviation_mm=small)
    b = reh(T.INVESTIGATOR, deviation_mm=large)
    assert best([b, a]) is a


@pytest.mark.parametrize("few,many", [(1, 2), (2, 5), (0, 1), (3, 30)])
def test_more_cleared_wins_before_deviation_is_read(few, many):
    lean = reh(T.ONE_SHOT, cleared=tuple(f"f{i}" for i in range(many)), deviation_mm=9.0)
    tidy = reh(T.INVESTIGATOR, cleared=tuple(f"g{i}" for i in range(few)), deviation_mm=0.0)
    assert best([tidy, lean]) is lean


@pytest.mark.parametrize("few,many", [(1, 2), (1, 7), (2, 3)])
def test_fewer_kept_ops_wins(few, many):
    a = reh(T.ONE_SHOT, kept_ops=few)
    b = reh(T.INVESTIGATOR, kept_ops=many)
    assert best([b, a]) is a


@pytest.mark.parametrize("other", [T.ONE_SHOT, T.INVESTIGATOR])
def test_tie_goes_to_rules(other):
    r = reh(T.RULES, plan=[{"op": "sew"}])
    o = reh(other, plan=[{"op": "fill"}])
    assert best([o, r]) is r


def test_one_shot_beats_the_investigator_on_a_tie():
    a = reh(T.ONE_SHOT, plan=[{"op": "sew"}])
    b = reh(T.INVESTIGATOR, plan=[{"op": "fill"}])
    assert best([b, a]) is a


@pytest.mark.parametrize("status", [T.NOT_REHEARSED, T.FAILED])
def test_a_rehearsed_candidate_outranks_an_unrehearsed_one_however_good_it_looks(status):
    ghost = reh(T.RULES, status=status, cleared=("a", "b", "c"), deviation_mm=0.0, kept_ops=1)
    real = reh(T.INVESTIGATOR, status=T.REHEARSED, cleared=("a",), deviation_mm=5.0, kept_ops=9)
    assert best([ghost, real]) is real


def test_tier0_outranks_everything_below_it():
    failed0 = reh(T.RULES, tier0=False, cleared=("a", "b"), deviation_mm=0.0)
    passed0 = reh(T.INVESTIGATOR, tier0=True, cleared=("a",), deviation_mm=5.0)
    assert best([failed0, passed0]) is passed0


# --- eligibility and honest endings ----------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"status": T.NOT_REHEARSED},
    {"status": T.FAILED},
    {"tier0": False},
    {"tier0": ...},            # tier 0 never asked
    {"revealed": ("x",)},
    {"cleared": ()},
])
def test_ineligible_is_never_offered(kwargs):
    assert T.eligible(reh(**kwargs)) is False
    assert T.winner([reh(**kwargs)]) is None


def test_eligible_is_true_only_for_a_clean_rehearsed_clearing_candidate():
    assert T.eligible(reh()) is True
    assert T.winner([reh()]) is not None


def test_a_tournament_with_no_survivor_says_so():
    """An honest ending stays first-class: no candidate is dressed up as a winner."""
    losers = [reh(T.RULES, tier0=False), reh(T.ONE_SHOT, revealed=("x",)),
              reh(T.INVESTIGATOR, status=T.FAILED)]
    assert T.winner(losers) is None
    assert len(T.rank(losers)) == 3          # ... and nothing is dropped from the record


def test_the_winner_is_the_best_eligible_not_the_best_overall():
    pretty = reh(T.RULES, revealed=("x",), cleared=("a", "b", "c"), deviation_mm=0.0)
    modest = reh(T.ONE_SHOT, cleared=("a",), deviation_mm=1.0)
    assert T.winner([pretty, modest]) is modest


# --- purity ----------------------------------------------------------------------------------

def test_rank_is_pure_and_stable():
    items = [reh(T.INVESTIGATOR, plan=[{"op": "a"}]), reh(T.ONE_SHOT, plan=[{"op": "b"}]),
             reh(T.RULES, plan=[{"op": "c"}])]
    snapshot = list(items)
    first, second = T.rank(items), T.rank(items)
    assert [id(x) for x in first] == [id(x) for x in second]
    assert items == snapshot                    # the input list is untouched
    assert sorted(id(x) for x in first) == sorted(id(x) for x in snapshot)


def test_rank_is_stable_among_equals():
    """Equal scores keep input order, so a tournament is reproducible run to run."""
    a = reh(T.RULES, plan=[{"op": "a"}])
    b = reh(T.RULES, plan=[{"op": "b"}])
    assert T.score(a) == T.score(b)
    assert [id(x) for x in T.rank([a, b])] == [id(a), id(b)]
    assert [id(x) for x in T.rank([b, a])] == [id(b), id(a)]


def test_the_module_touches_no_kernel_and_no_mesher():
    """Pure. A scoring rule that imported OCP would make every score cost a kernel load.

    Read off the IMPORTS, deferred ones included, not off the text: a comment saying "no OCP"
    must not be what makes this pass.
    """
    tree = ast.parse(pathlib.Path(T.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    for banned in ("OCP", "gmsh", "fea_tetwild", "mesh_probe", "subprocess", "multiprocessing",
                   "meshpipeline.agents", "meshpipeline.pipeline"):
        assert not any(n == banned or n.startswith(banned + ".") for n in imported), banned
