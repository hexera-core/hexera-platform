# Responsibility: B21 - lane D's oracle pass 1 found the only wins neither arm reached were two-step CHAINS whose
#                 first step the arms already had. The follow-up is proposed as a continuation of a KEPT first step,
#                 ahead of the fair share so `k` cannot drop it, and the same two chains are hinted to the AI.
# Boundaries: hermetic. The det generator's rows and the prompt text; no kernel, no pool, no model.
from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4 import search as S
from meshpipeline.cad.repair_v4.proposer import CHAIN_HINTS, system_prompt
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace


def _ws() -> Workspace:
    ids = {"F1": "a", "F2": "b", "B1": "c", "B2": "d"}
    return Workspace(brep_path="/fake/a.brep", ids=ids, labels={}, history=[], original="/fake/o.brep",
                     indices={k: i for i, k in enumerate(ids, start=1)})


def site(kind: str = "self_overlap", entities: tuple[str, ...] = ("F1", "F2"), **graph: Any) -> Site:
    return Site(id="S1", kind=kind, entities=list(entities), xyz=[], meshers_failing=["gmsh_prod"],
                dist_over_h=None, graph=graph, view_png=None)


def _state(sid: str, score: tuple, *, parent: str | None = None, via: dict | None = None,
           sites: list[Site] | None = None) -> S.SearchState:
    return S.SearchState(id=sid, ws=_ws(), verdict=None, score=score, sites=sites or [site()], depth=0 if not parent
                         else 1, parent=parent, via=via)


def _gen() -> S.DetGenerator:
    return S.DetGenerator(diagonal=lambda _w: 100.0)


def _defeature(source: str = "located") -> dict:
    return {"op": "defeature", "targets": ["F1"], "params": {"source": source}, "source": "det",
            "rationale": "remove the face the mesher located"}


# ---- the chain opens only when the first step was aimed AND the verdict kept it -----------------

def test_a_kept_aimed_defeature_is_followed_by_the_three_reconditioning_stages() -> None:
    gen = _gen()
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0)), 12)                 # the parent, so its score is known
    child = _state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=_defeature())
    got = gen.propose(child, 12)
    assert [c.params["stage"] for c in got if c.op == S.RUN_STAGE] == list(S.CHAIN_AFTER_DEFEATURE)
    # B21b: the follow-ups come first, ONE row per follow-up op, in the oracle's win order for this opener
    # B21c: the hoist is ordered by DISTINCT FILES, so run_stage (12 files) leads a second defeature (11)
    head = [c.op for c in got[:len(S.CHAIN_FOLLOWUPS["defeature"])]]
    assert head[0] == S.RUN_STAGE and "defeature" in head, head


def test_the_openers_cover_every_chain_the_oracle_measured_at_six_or_more_wins() -> None:
    """B21b read `winners_by_defect.jsonl` (658 paths; 316 ungamed chain wins no arm reached). B21's two hardcoded
    chains opened 37 of those. These are the openers and follow-ups that clear six measured wins."""
    want = {
        "defeature": {"defeature", S.RUN_STAGE, "conformalise", "fuse", "split_angle"},
        "remove_small": {"split_angle", "defeature", S.RUN_STAGE, "split_by_proximity", "rebuild_face",
                         "remove_small"},
        "fuse": {"conformalise", "fuse"},
        "conformalise": {"defeature", "remove_small", "conformalise"},
        "rebuild_face": {"split_angle", S.RUN_STAGE},
        "split_angle": {S.RUN_STAGE},
    }
    for opener, follows in want.items():
        got = set(S.CHAIN_FOLLOWUPS.get(opener, ()))
        assert follows <= got, f"{opener}: missing {sorted(follows - got)}"


def test_the_hoist_is_ordered_by_DISTINCT_FILES_not_by_winning_paths() -> None:
    """B21c (LEAD-3's ruling on my own finding): a pattern that won 26 times on TWO files generalises worse than
    one that won 8 times on EIGHT. The whole ungamed chain set rests on 29 files, so the path count was the wrong
    key - these are the orders the file counts give."""
    # defeature: run_stage 12 files beats a second defeature's 11, though the paths say 23 against 30
    assert S.CHAIN_FOLLOWUPS["defeature"][:2] == (S.RUN_STAGE, "defeature")
    # remove_small: defeature 9 files leads split_angle's 8, though the paths say 22 against 24
    assert S.CHAIN_FOLLOWUPS["remove_small"][:3] == ("defeature", "split_angle", S.RUN_STAGE)
    # split_angle THEN run_stage is 8 files - the broadest evidence in the table - and leads its opener
    assert S.CHAIN_FOLLOWUPS["split_angle"][0] == S.RUN_STAGE
    # conformalise: remove_small's 3 files lead defeature's 2, and the 1-file conformalise THEN conformalise is LAST
    assert S.CHAIN_FOLLOWUPS["conformalise"][0] == "remove_small"
    assert S.CHAIN_FOLLOWUPS["conformalise"][-1] == "conformalise"
    # fuse's follow-ups are all 2-file rows, so the paths break the tie and conformalise (26) still leads
    assert S.CHAIN_FOLLOWUPS["fuse"][0] == "conformalise"


def test_the_hoist_is_capped_and_never_fills_itself_with_one_op() -> None:
    gen = _gen()
    bodies = site("overlap", ("F1", "F2", "B1", "B2"))
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0), sites=[bodies]), 50)
    via = {"op": "fuse", "targets": ["B1", "B2"], "params": {"fuzzy_mm": 0.0}, "source": "det", "rationale": "fuse"}
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=via, sites=[bodies]), 50)
    # the generator's own note is the record of what was hoisted, in order
    note = next(n for n in gen.notes if n.startswith("chain after a kept fuse"))
    hoisted = note.split(": ", 1)[1].split(", ")
    assert hoisted[0] == "conformalise"                                   # 26 wins, the strongest in the table
    assert len(hoisted) <= S.CHAIN_MAX_ROWS and len(hoisted) == len(set(hoisted))
    assert [c.op for c in got[:len(hoisted)]] == hoisted, [c.op for c in got[:4]]


def test_the_chain_survives_a_cap_of_one() -> None:
    """`k` cannot drop the follow-up: at a cap of ONE the single candidate is the opener's best-measured one
    (for a kept aimed defeature that is a second defeature, 30 wins in the table)."""
    gen = _gen()
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0)), 12)
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=_defeature()), 1)
    assert len(got) == 1 and got[0].op == S.CHAIN_FOLLOWUPS["defeature"][0]


def test_a_defeature_the_verdict_pushed_DOWN_opens_no_chain() -> None:
    gen = _gen()
    gen.propose(_state("S0", (1, 2, 0.3, 0.0, 0)), 12)
    worse = _state("S1", (1, 1, 0.3, 0.0, 0), parent="S0", via=_defeature())
    assert [c for c in gen.propose(worse, 12) if c.op == S.RUN_STAGE] == []


def test_the_whole_recognised_set_is_not_a_chain_opener() -> None:
    """Its oracle wins were gamed (x3.98) and it stays off in arms, so it must not open the chain either."""
    gen = _gen()
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0)), 12)
    whole = _state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=_defeature(source="recognised"))
    assert [c for c in gen.propose(whole, 12) if c.op == S.RUN_STAGE] == []


def test_the_start_state_opens_no_chain() -> None:
    gen = _gen()
    assert [c for c in gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0)), 12) if c.op == S.RUN_STAGE] == []


# ---- the second chain: a kept remove_small, then the MEASURED pairs -----------------------------

def test_a_kept_remove_small_is_followed_by_a_split_on_the_measured_pairs_only() -> None:
    measured = site("proximity", ("F1", "F2"), clashes=[{"a": "F1", "b": "F2", "touching": False, "gap_mm": 0.2}])
    gen = _gen()
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0), sites=[measured]), 12)
    via = {"op": "remove_small", "targets": ["F1"], "params": {"max_width_mm": 0.5}, "source": "det",
           "rationale": "remove the sliver"}
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=via, sites=[measured]), 12)
    splits = [c for c in got if c.op == "split_by_proximity"]
    assert splits, [c.op for c in got]
    assert all(c.params["face_pairs"] == [["F1", "F2"]] for c in splits), [c.params for c in splits]
    # B21b: the hoist is ordered by the oracle's win counts, so split_angle (24) leads and the measured-pair split
    # (14) is in the hoist behind it - both ahead of the fair share
    note = next(n for n in gen.notes if n.startswith("chain after a kept remove_small"))
    hoisted = note.split(": ", 1)[1].split(", ")
    # B21c order: defeature (9 files) leads split_angle (8), and the measured-pair split is still in the hoist
    assert hoisted[0] == "defeature" and "split_by_proximity" in hoisted, hoisted
    assert [c.op for c in got[:len(hoisted)]] == hoisted, [c.op for c in got[:4]]


def test_without_a_measured_pair_the_kept_remove_small_adds_nothing() -> None:
    """B20's rule stands: a pair the kernel never measured is not guessed at, chain or no chain."""
    gen = _gen()
    bare = site("proximity", ("F1", "F2"))
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0), sites=[bare]), 12)
    via = {"op": "remove_small", "targets": ["F1"], "params": {}, "source": "det", "rationale": "sliver"}
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=via, sites=[bare]), 12)
    assert [c for c in got if c.op == "split_by_proximity"] == []


# ---- the AI gets the same two chains, and no dev answers ----------------------------------------

def test_the_prompt_is_keyed_on_the_tables_own_defect_families_and_names_no_file() -> None:
    prompt = system_prompt(stage_names=("bodies", "condition", "shells"))
    assert CHAIN_HINTS in prompt
    # B21b: keyed on `winners_by_defect.jsonl`'s own `label_family` values
    for family in ("overlapping_facets", "body_overlap", "self_intersection", "edge_through_face",
                   "plc_intersection", "open_shell", "brep_invalid", "no_volume"):
        assert family in CHAIN_HINTS, family
    for must in ("defeature", "run_stage", "remove_small", "split_by_proximity", "fuse THEN conformalise"):
        assert must in CHAIN_HINTS, must
    # B21c: the hint counts FILES, and says so, including that the biggest pattern rests on two of them
    assert "DIFFERENT FILES" in CHAIN_HINTS and "8 files" in CHAIN_HINTS
    assert "2 files, but 26 wins" in CHAIN_HINTS and "29 files" in CHAIN_HINTS
    assert "(21)" not in CHAIN_HINTS, "path counts must not be quoted as if they were file counts"
    # no file id, no sha, no split name: the dev sample's answers must not reach the model
    import re

    assert re.findall(r"[0-9a-f]{8,}", CHAIN_HINTS) == []
    for leak in ("54f1189a", "78cbab54", "bcf201b5", "cdb59a08", "dev_v2", "held-out", "heldout"):
        assert leak not in CHAIN_HINTS, leak


def test_the_prompt_stays_byte_identical_so_the_provider_cache_still_serves_it() -> None:
    a = system_prompt(stage_names=("bodies", "condition"))
    b = system_prompt(stage_names=("bodies", "condition"))
    assert a == b


# ---- the k_propose knob: present, default UNCHANGED ---------------------------------------------

def test_k_propose_defaults_to_twelve_and_only_a_positive_integer_moves_it() -> None:
    """LEAD-3 asked for the knob pre-built with the default unchanged, so a comparison run needs no code change.
    A typo must not silently make every search one candidate wide."""
    assert S.K_PROPOSE_DEFAULT == 12
    assert S.k_propose_setting("") == 12
    assert S.k_propose_setting(None if False else "  6 ") == 6
    assert S.k_propose_setting("24") == 24
    for bad in ("0", "-3", "nine", "6.5", " "):
        assert S.k_propose_setting(bad) == 12, bad


def test_the_env_var_is_read_but_an_explicit_argument_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("CAD_REPAIR_V4_K_PROPOSE", "3")
    assert S.k_propose_setting() == 3
    seen: list[int] = []

    class Count:
        def propose(self, state: Any, k: int) -> list:
            seen.append(k)
            return []

    start = _ws()

    class _J:
        def judge(self) -> S.Judge:
            return S.Judge(cheap=lambda *a: _VV(), full=lambda *a: _VV(), score=lambda v: (1, 0, 0.0, 0.0, 0),
                           sites=lambda *a: [site()])

    class _VV:
        clean = False
        inconclusive = False

    S.search(None, start, start.original, [Count()], S.Budget(wall_s=50, ops=5, model_calls=0, model_usd=0.0),
             workers=1, judge=_J().judge(), patience=1)
    assert seen and seen[0] == 3, seen                      # the env var reached the generator
    seen.clear()
    S.search(None, start, start.original, [Count()], S.Budget(wall_s=50, ops=5, model_calls=0, model_usd=0.0),
             workers=1, judge=_J().judge(), patience=1, k_propose=9)
    assert seen and seen[0] == 9, seen                      # an explicit argument still wins
