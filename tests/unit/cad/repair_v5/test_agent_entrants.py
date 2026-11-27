"""v5 step 12: the agent entrant tier on fakes (no kernel, no model): when it enters, what it may propose, the caps,
and that a failed call never stops the tournament."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from tests.unit.cad.repair_v4.test_b24_seq_arm import _local, _patched
from tests.unit.cad.repair_v5.test_tournament import _WS, _apply, _defect, _volume

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v5 import agent_entrants as A
from meshpipeline.cad.repair_v5 import tournament as T

MENU = (("split_aligned",), ("defeature",))


def _reply(candidates: list[dict], cost: float = 0.01) -> Any:
    call = SimpleNamespace(name="propose_repairs", arguments=json.dumps({"candidates": candidates}))
    return SimpleNamespace(reply=SimpleNamespace(failure_marker=None, tool_calls=[call]), cost_usd=cost)


def _agent(candidates: list[dict] | Exception, *, estimate: float = 0.05, file_id: str = "f", **kw: Any) -> Any:
    calls: list = []

    async def call(messages: list, tools: list, **k: Any) -> Any:
        calls.append(messages)
        if isinstance(candidates, Exception):
            raise candidates
        return _reply(candidates)

    agent = A.AgentSource(pool=None, ledger=None, file_id=file_id, route="openai:test", call=call,
                          estimate=lambda m, t: estimate, render=False, **kw)
    return agent, calls


def _gone_if(word: str) -> Any:
    """Only a candidate whose state's name carries `word` removes the defect."""
    return lambda pool, before, after, defect, touched=None, **k: _local(gone=word in after.brep_path)


def _play(agent: Any, verify: Any, *, defect: Any = None) -> tuple[Any, T.Tournament]:
    t = T.Tournament(menu_of=lambda d: MENU, verify=verify, guard=_volume({}), sources=(*T.DEFAULT_SOURCES, agent))
    with _patched(_apply([])):
        out = t(None, _WS(), defect or _defect(), counters=seq.SeqCounters(), step=lambda line: None)
    return out, t


def test_the_agent_enters_when_no_free_entrant_wins_and_its_winner_is_kept() -> None:
    agent, calls = _agent([{"repair": "defeature", "params": {"source": "recognised"}, "why": "try the features"}])
    out, t = _play(agent, _gone_if("recognised"))
    assert len(calls) == 1 and "recognised" in out.brep_path
    won = [m for m in t.log.records if m.result == "won"]
    assert [(m.tier, m.op) for m in won] == [("agent", "defeature")]
    by = t.log.counts()["by_class"]["close_faces"]
    assert by["tiers"]["agent"] == {"entered": 1, "won": 1, "rate": 1.0}
    assert by["tiers"]["rule_seed"]["won"] == 0
    # the brief carried what the free tiers did, and the menu with its ranges
    brief = json.loads(calls[0][1]["content"])
    assert {r["repair"] for r in brief["menu"]} == {"split_aligned", "defeature"}
    assert brief["menu"][1]["tunable"]["source"] == {"enum": ["recognised", "located", "site"]}
    assert brief["already_tried"] and brief["free_entrant_won"] is False


def test_the_agent_stays_out_of_a_solved_defect_unless_it_explores() -> None:
    agent, calls = _agent([{"repair": "defeature", "params": {"source": "site"}, "why": "x"}], share=0.0)
    _, t = _play(agent, lambda *a, **k: _local())
    assert calls == [] and "agent" not in {m.tier for m in t.log.records}
    agent, calls = _agent([{"repair": "defeature", "params": {"source": "site"}, "why": "x"}], share=1.0)
    _, t = _play(agent, lambda *a, **k: _local())
    assert len(calls) == 1 and agent.events[0]["enters"] == A.ENTERS_EXPLORING
    # the seed still wins the tie
    assert next(m for m in t.log.records if m.result == "won").tier == "rule_seed"


def test_exploration_is_deterministic_and_about_a_tenth() -> None:
    ds = [_defect() for _ in range(1)]
    assert A.explores("f", ds[0]) == A.explores("f", ds[0])
    import dataclasses

    hits = sum(A.explores("file", dataclasses.replace(ds[0], id=f"S{i}")) for i in range(2000))
    assert 0.07 < hits / 2000 < 0.13


def test_off_menu_out_of_range_untunable_repeated_and_extra_proposals_are_rejected_and_logged() -> None:
    agent, _ = _agent([
        {"repair": "fuse", "params": {}, "why": "not on the menu"},
        {"repair": "split_aligned", "params": {"segments": 7}, "why": "not in the enum"},
        {"repair": "defeature", "params": {"groups": [[1]]}, "why": "not tunable"},
        {"repair": "defeature", "params": {"source": "site"}, "why": "a fourth"},
    ])
    _, t = _play(agent, _gone_if("nothing"))
    rejected = [m for m in t.log.records if m.result == "rejected"]
    assert [m.tier for m in rejected] == ["agent"] * 4
    reasons = " | ".join(m.reason for m in rejected)
    assert "not on the class menu" in reasons and "outside" in reasons and "not tunable" in reasons
    assert "over the agent's 3" in reasons
    assert all(m.heat == "aim" and not m.applied for m in rejected)


def test_a_repeat_of_a_free_entrant_is_rejected_not_rerun() -> None:
    agent, _ = _agent([{"repair": "split_aligned", "params": {}, "why": "the seed again"}])
    _, t = _play(agent, _gone_if("nothing"))
    [m] = [m for m in t.log.records if m.tier == "agent"]
    assert m.result == "rejected" and "earlier tier" in m.reason


def test_the_defect_cap_refuses_a_call_before_it_is_sent() -> None:
    agent, calls = _agent([{"repair": "defeature", "params": {"source": "site"}, "why": "x"}], estimate=0.2)
    _play(agent, _gone_if("nothing"))
    assert calls == [] and agent.events[0]["outcome"] == "refused" and "defect cap" in agent.events[0]["why"]


def test_spend_on_one_defect_counts_against_its_cap_in_later_rounds() -> None:
    agent, calls = _agent([{"repair": "defeature", "params": {"source": "site"}, "why": "x"}], estimate=0.08)
    agent._by_defect[A.defect_key(_defect())] = 0.1
    _play(agent, _gone_if("nothing"))
    assert calls == [] and agent.events[0]["outcome"] == "refused"


def test_a_failed_or_refused_call_never_raises_and_the_tournament_goes_on() -> None:
    class LedgerRefused(RuntimeError):
        pass

    for exc, outcome in ((RuntimeError("provider down"), "error"), (LedgerRefused("file_ceiling"), "refused")):
        agent, _ = _agent(exc)
        out, t = _play(agent, _gone_if("nothing"))
        assert out.labels["q"] == 0 and agent.events[0]["outcome"] == outcome
        assert "agent" not in {m.tier for m in t.log.records}


def test_metered_call_lowers_the_output_allowance_and_the_reservation() -> None:
    import asyncio

    from meshpipeline.cad.repair_v4 import model_route
    from meshpipeline.contracts import model_inference as MI

    seen: dict = {}

    class Ledger:
        def admit(self, file_id: str, est: float, *, file_ceiling_usd: float) -> Any:
            seen["est"] = est
            return SimpleNamespace(ok=True, reservation="r")

        def release(self, r: str) -> None:
            pass

        def settle(self, r: str, **kw: Any) -> None:
            seen["settled"] = kw["cost_usd"]

    async def fake(messages: list, tools: list, **kw: Any) -> Any:
        seen["max_output_tokens"] = kw.get("max_output_tokens")
        reply = SimpleNamespace(failure_marker=None, input_tokens=1, cached_input_tokens=0, output_tokens=1,
                                cache_write_tokens=0, cache_write_30m_tokens=0)
        return SimpleNamespace(reply=reply, provider="p", model="m", cost_usd=0.001)

    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MI, "call_repair_v4_propose", fake)
        mp.setattr(MI, "repair_v4_route_prices", lambda route: (10.0, 50.0, 1.0))
        call = model_route.metered_call(Ledger(), "f", 1.0, kind="v5_agent", max_output_tokens=1500)
        asyncio.run(call([{"role": "user", "content": "x"}], [], route="openai:test"))
    assert seen["max_output_tokens"] == 1500 and seen["est"] < 0.08 and seen["settled"] == 0.001


def test_the_v5_arm_describes_each_unknown_once_per_file_on_the_files_ledger() -> None:
    import dataclasses
    import sys
    from pathlib import Path

    import pytest
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools/validation/v4_eval"))
    import arms

    from meshpipeline.cad.repair_v5 import describe_ai

    asked: list = []

    def describe(defects: list, **kw: Any) -> list:
        asked.append([d.id for d in defects if d.cls == "unknown"])
        assert kw["file_id"] == "sha" and kw["cap_usd"] == 1.0
        return [dataclasses.replace(d, ai={"description": "a gap"}) if d.cls == "unknown" else d for d in defects]

    unknown, known = _defect(cls="unknown"), _defect()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(describe_ai, "describe_unknown", describe)
        d = arms._V5Describe(object(), "sha", "openai:test", 1.0)
        first, again = d([unknown, known]), d([unknown, known])
    assert asked == [["S@F1"]]
    assert first[0].ai == again[0].ai == {"description": "a gap"} and again[1].ai is None
