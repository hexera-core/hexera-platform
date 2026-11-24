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
    stages = [c.params["stage"] for c in got if c.op == S.RUN_STAGE]
    assert stages == list(S.CHAIN_AFTER_DEFEATURE), stages
    # and they come FIRST, so `k` cannot drop the one follow-up the oracle measured
    assert [c.op for c in got[:3]] == [S.RUN_STAGE] * 3, [c.op for c in got[:5]]


def test_the_chain_survives_a_cap_of_one() -> None:
    gen = _gen()
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0)), 12)
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=_defeature()), 1)
    assert len(got) == 1 and got[0].op == S.RUN_STAGE


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
    assert got[0].op == "split_by_proximity", [c.op for c in got[:3]]   # first, ahead of the fair share


def test_without_a_measured_pair_the_kept_remove_small_adds_nothing() -> None:
    """B20's rule stands: a pair the kernel never measured is not guessed at, chain or no chain."""
    gen = _gen()
    bare = site("proximity", ("F1", "F2"))
    gen.propose(_state("S0", (1, 1, 0.0, 0.0, 0), sites=[bare]), 12)
    via = {"op": "remove_small", "targets": ["F1"], "params": {}, "source": "det", "rationale": "sliver"}
    got = gen.propose(_state("S1", (1, 1, 0.0, 0.0, 0), parent="S0", via=via, sites=[bare]), 12)
    assert [c for c in got if c.op == "split_by_proximity"] == []


# ---- the AI gets the same two chains, and no dev answers ----------------------------------------

def test_the_prompt_carries_both_chains_and_names_no_file() -> None:
    prompt = system_prompt(stage_names=("bodies", "condition", "shells"))
    assert CHAIN_HINTS in prompt
    for must in ("defeature", "run_stage", "remove_small", "split_by_proximity", "MEASURED"):
        assert must in CHAIN_HINTS, must
    # no file id, no sha, no split name: the dev sample's answers must not reach the model
    import re

    assert re.findall(r"[0-9a-f]{8,}", CHAIN_HINTS) == []
    for leak in ("54f1189a", "78cbab54", "bcf201b5", "cdb59a08", "dev_v2", "held-out", "heldout"):
        assert leak not in CHAIN_HINTS, leak


def test_the_prompt_stays_byte_identical_so_the_provider_cache_still_serves_it() -> None:
    a = system_prompt(stage_names=("bodies", "condition"))
    b = system_prompt(stage_names=("bodies", "condition"))
    assert a == b
