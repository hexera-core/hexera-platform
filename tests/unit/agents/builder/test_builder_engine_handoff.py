# Responsibility: Verify a run_mesh result that hands the run on to the next engine ends the builder loop at once - no further model round, no "call submit_mesh" nudge (exhaust_manifold_007, shared dev 2 Nov: 10 minutes of "no mesh.msh" before snappy).
from __future__ import annotations

import asyncio
import types

from meshpipeline.agents.builder.loop_policy import BUILDER_NO_PROGRESS_THRESHOLD, BuilderLoopPolicy
from meshpipeline.agents.builder.tool_context import HANDS_OFF
from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.contracts.agent_loop import LoopExit, LoopLimits, LoopTally
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo, ToolCallRequest


def _executor(results: dict):
    """A builder executor that answers each tool with a fixed result."""
    async def run(tool, args, *, call_index=0):
        res = results.get(tool, {"success": True})
        return types.SimpleNamespace(malformed=False, tool=tool, args=args or {}, result=res,
                                     content=str(res), accepted=bool(res.get("success", True)),
                                     terminal=False, terminal_value=None)
    return types.SimpleNamespace(run=run, publish=None)


def _policy(results: dict) -> BuilderLoopPolicy:
    return BuilderLoopPolicy(engine="vmtk", mode="initial",
                             limits_=LoopLimits(max_rounds=60,
                                                no_progress_threshold=BUILDER_NO_PROGRESS_THRESHOLD),
                             executor=_executor(results), max_rounds=60)


def _tc(name: str, i: int = 0) -> ToolCallRequest:
    return ToolCallRequest(id=f"c-{name}-{i}", name=name, arguments="{}")


def _round(*calls, text=""):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop",
                            provider=ProviderAttemptInfo(1, "p", "m"))


def _drive(policy, script):
    asked = []
    it = iter(script)

    async def provider(**_kw):
        asked.append(1)
        try:
            return next(it)
        except StopIteration:
            # what the model did on shared dev: say there is no mesh.msh, again and again
            return _round(text="No mesh.msh exists, so there is nothing to submit.")

    def append(messages, call_id, content):
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    result = asyncio.run(run_agent_loop(driver=policy, provider_call=provider, messages=[], tools=[],
                                        job_id="job-handoff", append_tool_result=append, deadline_s=30.0))
    return result, len(asked)


_LAYERS = {"success": False, HANDS_OFF: True,
           "layers_not_converging": {"retangled": 0, "next_engine": "snappy",
                                     "words": "VMTK layer needed untangling - next engine (snappy)"},
           "guidance": "Not meshed: ... end your turn now; the run moves on to the next engine"}


def test_a_hand_off_ends_the_loop_with_that_call():
    policy = _policy({"run_mesh": _LAYERS})
    result, rounds = _drive(policy, [_round(_tc("configure_mesh")), _round(_tc("run_mesh"), _tc("list_files"))])
    assert result.exit is LoopExit.policy_abort
    assert result.payload is None
    # two model rounds, and the call after run_mesh in that round was never run
    assert rounds == 2
    assert policy.run_mesh_calls == 1 and policy.handed_off


def test_a_failed_run_that_does_not_hand_off_keeps_the_loop_going():
    policy = _policy({"run_mesh": {"success": False, "fatal_defects": ["x"], "guidance": "fix it"}})
    result, rounds = _drive(policy, [_round(_tc("run_mesh"))])
    assert result.exit is not LoopExit.policy_abort
    assert rounds > 2 and not policy.handed_off


def test_plain_text_after_a_hand_off_is_never_nudged_to_submit():
    policy = _policy({})
    policy.handed_off = True
    decision = policy.on_plaintext(LoopTally(rounds=3))
    assert decision.complete and decision.exit is LoopExit.policy_abort
    assert "submit_mesh" not in decision.message


def test_execute_marks_the_outcome_as_ending_the_loop():
    policy = _policy({"run_mesh": _LAYERS})
    out = asyncio.run(policy.execute(ToolInvocation(round_index=1, call_index=1, tool="run_mesh", parsed={})))
    assert out.ends_loop and not out.terminal


def test_every_hand_off_verdict_in_run_mesh_carries_the_key():
    # the run_mesh branches that tell the model "end your turn now; the run moves on" all hand off
    import inspect

    from meshpipeline.agents.builder.tools import meshing
    src = inspect.getsource(meshing)
    # the import + four hand-offs (case contract, over size, layers not converging, broken mesh).
    # A run its guard clock stopped is not a hand-off: the attempt is run again (TIME-3, #259)
    assert src.count("HANDS_OFF") >= 5
