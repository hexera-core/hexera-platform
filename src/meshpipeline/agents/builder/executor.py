# Responsibility: Execute one builder tool call and report its result.
# Owns: the side-effecting tool roster, dispatch, and the result shape the loop consumes.
# Boundaries: the roster is the fence's authority: a tool listed here is fenced before it runs.
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.builder.context import _compress_tool_output
from meshpipeline.agents.builder.tool_context import BuilderToolContext
from meshpipeline.agents.builder.tools import (
    _dispatch_tool,
    _serialized,
    dispatch_prepared_mesh,
    get_spec_run_files,
)
from meshpipeline.agents.builder.tools.meshing import prepare_mesh_run
from meshpipeline.application import execution_fence as _fence
from meshpipeline.application.execution_publisher import StaleExecutionPublish
from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

# Tools that MUTATE the workspace, execute code, or launch the native mesher. Each is fenced on
# execution ownership before dispatch; the read-only tools are not - fencing them would add a
# database read per round and they change nothing.
SIDE_EFFECTING_TOOLS = frozenset({
    "write_file", "configure_mesh", "run_mesh", "run_python", "submit_mesh",
})

# Tools dispatched off the event loop because they block (search, native meshing, subprocesses).
_OFFLOADED_TOOLS = frozenset({
    "web_search", "geometry_report", "configure_mesh", "run_mesh", "run_python",
})


@dataclass(frozen=True)
class BuilderToolResult:

    tool: str
    accepted: bool = False           # a well-formed call the executor actually ran
    malformed: bool = False          # arguments did not parse - nothing was dispatched
    changed_domain_state: bool = False   # it mutated the workspace or produced a mesh
    content: Any = None              # the tool-result message handed back to the model
    correction: str | None = None    # a precise corrective message, when rejected
    result: dict = field(default_factory=dict)   # parsed tool output, {} when not JSON
    args: dict = field(default_factory=dict)
    terminal: bool = False           # this call completed the Builder's terminal action
    terminal_value: str = ""         # the loop's return value on a terminal call
    stop_round: bool = False         # no later call in this provider response may run
    failure_marker: str = ""         # an application-owned failure marker, when one applies


class BuilderToolExecutor:

    def __init__(self, *, context: BuilderToolContext,
                 publish: ExecutionEventPublisher | None = None,
                 tool_calls_out: list | None = None) -> None:
        # ONE typed context, not a spread of loose fields. The workspace, the verified execution
        # geometry, and the identity the fence needs all travel together, so a tool cannot be
        # handed a workspace whose geometry it then has to guess at.
        self._context = context
        self._workspace = context.workspace
        self._job_id = context.job_id
        self._publish = publish
        self._tool_calls_out = tool_calls_out
        self._round_index = 0
        self._pub_seq = 0
        # WHICH mesh dispatch this is within the node run, on the same replay-identity rule as
        # `_pub_seq`: the executor is rebuilt when the node re-runs, so a replay recounts from one
        # and republishes under the identity the first run used.
        self._mesh_seq = 0

    def capture_reasoning(self, reasoning: str) -> None:
        if not reasoning:
            return
        try:
            with open(self._workspace / "attempt_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n=== Builder reasoning (round {self._round_index}) ===\n"
                        f"{reasoning}\n")
        except Exception:  # noqa: BLE001
            pass

    def announce(self, tools: list[str]) -> None:
        return

    async def run(self, tool: str, args: dict | None, *, call_index: int) -> BuilderToolResult:
        if args is None:
            # MALFORMED. Nothing is dispatched and nothing is inferred: a tool that never ran
            # produced no result, so no progression may be built on one.
            return BuilderToolResult(
                tool=tool, malformed=True, accepted=False,
                content=(f"[SYSTEM] The arguments for {tool} were not valid JSON, so NOTHING was "
                         "executed and no result exists. Call the tool again with well-formed "
                         "JSON arguments. Do not assume the action succeeded."),
                correction=f"malformed arguments for {tool}")

        # 1. PRE-DISPATCH FENCE. A worker whose lease was taken over must not start new native
        #    work or mutate a workspace a newer generation now owns.
        if tool in SIDE_EFFECTING_TOOLS:
            await _fence.assert_current_owner(f"builder tool {tool}")

        # 2. DISPATCH. Engine-owned execution, protected paths and schema validation all live
        #    inside the tool implementations themselves.
        if tool == "run_mesh":
            # The mesh run ANNOUNCES itself, and that event is execution-owned: authorizing it
            # is an async database check, which the worker thread running the mesher cannot do.
            # So the run is driven in two offloaded halves with the announcement between them,
            # on this thread, where the ownership check can be awaited.
            raw = await self._run_announced_mesh()
        elif tool in _OFFLOADED_TOOLS:
            raw = await asyncio.to_thread(_dispatch_tool, self._context, tool, args)
        else:
            raw = _dispatch_tool(self._context, tool, args)

        # 3. POST-DISPATCH FENCE, before the native output is accepted into the loop's reasoning
        #    (and therefore into state and evidence). Nothing below this line may run for a
        #    superseded worker: no event, no accepted result, no progression, no later call.
        if tool == "run_mesh":
            await _fence.assert_current_owner("accept native run_mesh output")

        parsed: dict = {}
        try:
            _p = json.loads(raw)
            parsed = _p if isinstance(_p, dict) else {}
        except Exception:  # noqa: BLE001 - non-JSON tool output is simply not a mesh step
            parsed = {}

        # 4. EVENT PUBLICATION - only ever after the fence that authorises it.
        await self._publish_for(tool, args, parsed)

        # 5. TERMINAL: a successful submit_mesh ends the attempt.
        compressed = _compress_tool_output(tool, args, raw,
                                           protect=get_spec_run_files(self._context.engine))
        if tool == "submit_mesh" and parsed.get("success"):
            self._record(tool, args, raw, compressed)
            logger.info("Builder [%s]: submit_mesh succeeded - polyMesh confirmed, exiting loop",
                        self._job_id)
            return BuilderToolResult(
                tool=tool, accepted=True, changed_domain_state=True, content=compressed,
                result=parsed, args=args, terminal=True, terminal_value="submit_mesh:success",
                stop_round=True)

        self._record(tool, args, raw, compressed)
        self._append_attempt_log(tool, raw)
        return BuilderToolResult(
            tool=tool, accepted=True, content=compressed, result=parsed, args=args,
            changed_domain_state=tool in SIDE_EFFECTING_TOOLS)

    async def _run_announced_mesh(self) -> str:
        prepared = await asyncio.to_thread(prepare_mesh_run, self._context)
        if prepared.refusal is not None:
            # Declined before the announcement boundary, exactly as before: the model is told
            # why, nothing is published and the mesher is never submitted.
            return _serialized("run_mesh", self._context.job_id, prepared.refusal)
        if self._publish is not None:
            # The announcement the run used to make for itself: same engine, same cap, same
            # measured estimate, published before the mesher starts. A lost claim raises here
            # and the run below is never submitted.
            self._mesh_seq += 1
            await self._publish.ameshing(prepared.engine, prepared.cap, prepared.history,
                                         op_id=f"meshing:{self._mesh_seq}")
        return await asyncio.to_thread(dispatch_prepared_mesh, self._context, prepared)

    # The typed publisher this executor was given. A collaborator that needs to publish
    # asks for it here rather than reaching for a private attribute: `getattr` returns
    # whatever happens to be there, and a wrong guess fails silently at the call.
    @property
    def publish(self) -> ExecutionEventPublisher | None:
        return self._publish

    # internals
    async def _publish_for(self, tool: str, args: dict, parsed: dict) -> None:
        if not self._publish:
            return
        # WHICH accepted tool call this is within the node run. The executor is rebuilt when the
        # node re-runs, so a replay counts from one again and lands on the same identities.
        self._pub_seq += 1
        _n = self._pub_seq
        try:
            if tool == "run_mesh":
                # closes the meshing progress bar the run_mesh tool opened
                await self._publish.ameshed(parsed.get("cells"), op_id=f"meshed:{_n}")
                if not parsed.get("success"):
                    await self._publish.awarn(
                        "The mesh came back with defects - reworking it",
                        op_id=f"defects:{_n}")
            elif tool == "write_file":
                # only a write that ACTUALLY landed: the failure shape carries `error`
                # and no `written`, so a blocked or escaped path emits nothing here.
                _rel = parsed.get("written")
                if _rel:
                    await self._publish.afile(str(_rel), int(parsed.get("bytes") or 0),
                                              str(parsed.get("operation") or "created"),
                                              op_id=f"file:{_n}")
            elif tool == "web_search":
                # Surface the SEARCH in the live feed - query only. The distilled result is
                # intentionally NOT published.
                await self._publish.asearch(
                    (args.get("query") or "").strip()[:160].replace("\n", " "))
        except StaleExecutionPublish:
            # A lost claim is not an observability blip: this generation must stop, not carry on
            # running tools for a job a newer one owns.
            raise
        except Exception:  # noqa: BLE001
            pass

    def _record(self, tool: str, args: dict, raw: str, compressed: Any) -> None:
        if self._tool_calls_out is None:
            return
        stored = args
        if tool == "write_file":
            content = args.get("content", "")
            if len(content) > 20_000:
                stored = {**args, "content": content[:20_000], "_truncated": True}
        self._tool_calls_out.append({
            "call_num": len(self._tool_calls_out) + 1, "tool": tool, "args": stored,
            "raw_output": raw, "compressed_output": compressed})

    def _append_attempt_log(self, tool: str, raw: str) -> None:
        try:
            with open(self._workspace / "attempt_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n--- Tool: {tool} ---\n")
                f.write(raw[:4000])
                f.write("\n")
        except Exception:  # noqa: BLE001
            pass


__all__ = ["SIDE_EFFECTING_TOOLS", "BuilderToolExecutor", "BuilderToolResult"]
