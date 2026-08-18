# Responsibility: Define the closed public event vocabulary the browser is allowed to receive.
# Owns: the event types, the stage sequence, the tone set and the typed event constructors.
# Boundaries: vocabulary and construction only - no transport.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

# the pipeline stages a user sees, in order
STAGES: Final[tuple[str, ...]] = (
    "intake", "engine_select", "geometry_admission", "builder",
    "executor", "classifier", "reviewer", "outcome",
)

# the closed set of event types
STAGE:      Final = "stage"       # a stage became active
ATTEMPT:    Final = "attempt"     # a rebuild opened attempt N of M
NOTE:       Final = "note"        # a line written by a human, for a human
CHECK:      Final = "check"       # a validation result + what it proves
ACTION:     Final = "action"      # an agent took a step (a tool call), named in plain words
SEARCH:     Final = "search"      # an agent looked something up
SCREENSHOT: Final = "screenshot"  # the reviewer's view of the mesh
FILE:       Final = "file"        # a file the run produced, by name and size only
REASONING:  Final = "reasoning"   # an agent thought: ACTIVITY in safe mode, content in raw
RATIONALE:  Final = "rationale"   # the APPLICATION's own conclusion, both modes
TOOL_CALL:  Final = "tool_call"   # a tool was invoked
TOOL_RESULT: Final = "tool_result"  # …and what came back
MESHING:    Final = "meshing"     # the mesher started, with the engine's declared budget
MESHED:     Final = "meshed"      # the mesher finished, with the cell count
VERDICT:    Final = "verdict"     # the reviewer's decision
CLOSING:    Final = "closing"     # the run's final word to the user

EVENT_TYPES: Final[frozenset[str]] = frozenset({
    STAGE, ATTEMPT, NOTE, CHECK, ACTION, SEARCH, FILE,
    SCREENSHOT, MESHING, MESHED, VERDICT, CLOSING,
    REASONING, RATIONALE, TOOL_CALL, TOOL_RESULT,
})

# A reasoning operation's lifecycle. One id, one card, three possible ends.
REASONING_PHASES: Final[frozenset[str]] = frozenset({"started", "completed", "failed"})
TOOL_CALL_STATUSES: Final[frozenset[str]] = frozenset({"started", "blocked"})
TOOL_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"success", "failure", "blocked"})

# What a FILE event may say happened. Closed: "wrote something" is not an account,
# and a verb nobody defined cannot be rendered honestly.
FILE_OPERATIONS: Final[frozenset[str]] = frozenset({
    "created", "updated", "generated", "packaged",
})

# CHAIN-OF-THOUGHT: was "NOT AN EVENT", now governed by PUBLIC_TRACE_MODE.
# The original decision was right for a single mode: the first live run put raw
# deliberation on screen - "The read_file is omitting content. Let me try listing
# the directory…" - which names a tool, shows a dead end, and is the model talking
# to ITSELF. Publishing that unconditionally was the wrong default and still is.
# What changed is that "publish" stopped being one question. A deployment now
# declares who its page is for:
#   safe (default)  reasoning is ACTIVITY. The event carries the fact that
#                   thinking happened, its measured duration, and its reported
#                   reasoning-token count. `content` is null and no reasoning
#                   text is ever WRITTEN to the public stream.
#   raw             reasoning is CONTENT, for a deployment that has explicitly
#                   acknowledged showing its workings - still scrubbed of
#                   secrets and of model/provider identity.
# The projection lives in ONE place (meshpipeline.trace.policy), not in the
# agents, and it runs again at egress so a raw backlog cannot be replayed to a
# safe reader. The agent's outward voice (msg.content -> NOTE) is unchanged, and
# reasoning is still captured for training regardless of what the page shows.

# a NOTE's tone. Not a log level - a log level says who should care (operator vs user);
# a tone says how the user should read it.
TONES: Final[frozenset[str]] = frozenset({"info", "warn", "error"})


@dataclass(frozen=True, slots=True)
class UiEvent:
    type:  str
    stage: str = ""
    data:  dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"{self.type!r} is not a user-facing event type")
        if self.stage and self.stage not in STAGES:
            raise ValueError(f"{self.stage!r} is not a pipeline stage")

    def wire(self) -> dict[str, Any]:
        return {"type": self.type, "stage": self.stage,
                "ts": datetime.now(UTC).isoformat(), **self.data}


# constructors: the ONLY way to make an event
def stage(name: str) -> UiEvent:
    return UiEvent(STAGE, name)


def attempt(stage_: str, n: int, of: int) -> UiEvent:
    return UiEvent(ATTEMPT, stage_, {"n": int(n), "of": int(of)})


def note(stage_: str, text: str, tone: str = "info") -> UiEvent:
    if tone not in TONES:
        raise ValueError(f"{tone!r} is not a tone")
    return UiEvent(NOTE, stage_, {"text": str(text), "tone": tone})


def check(stage_: str, statement: str, *, ok: bool) -> UiEvent:
    return UiEvent(CHECK, stage_, {"statement": str(statement), "ok": bool(ok)})


def action(stage_: str, actions: list[str]) -> UiEvent:
    return UiEvent(ACTION, stage_, {"actions": [str(a) for a in actions]})


def search(stage_: str, query: str) -> UiEvent:
    return UiEvent(SEARCH, stage_, {"query": str(query)})


def screenshot(stage_: str, image_b64: str) -> UiEvent:
    return UiEvent(SCREENSHOT, stage_, {"image": image_b64})


def file(stage_: str, display_path: str, byte_count: int,
         operation: str = "created") -> UiEvent:
    if operation not in FILE_OPERATIONS:
        raise ValueError(f"{operation!r} is not a file operation")
    rel = str(display_path).replace("\\", "/").strip().lstrip("/")
    parts = [seg for seg in rel.split("/") if seg not in ("", ".", "..")]
    if not parts:
        raise ValueError("a file event needs a workspace-relative path")
    n = int(byte_count)
    if n < 0:
        raise ValueError("byte_count cannot be negative")
    return UiEvent(FILE, stage_, {"display_path": "/".join(parts),
                                  "byte_count": n, "operation": operation,
                                  "status": "ok"})


# trace
# Every one of these is PROJECTED by meshpipeline.trace.policy before it becomes a
# UiEvent. The projection is what decides whether reasoning text, a real tool name,
# its arguments or an inspection image may appear - and it reads the mode from
# deployment settings, never from a caller. A producer therefore cannot publish
# more than its deployment allows even by passing more.


def reasoning(stage_: str, rid: str, agent: str, phase: str, *,
              duration_ms: int | None = None, token_count: int | None = None,
              content: str | None = None, status: str = "active") -> UiEvent:
    if phase not in REASONING_PHASES:
        raise ValueError(f"{phase!r} is not a reasoning phase")
    from meshpipeline.trace.policy import project_reasoning
    return UiEvent(REASONING, stage_, project_reasoning(
        {"id": rid, "agent": agent, "phase": phase, "status": status,
         "duration_ms": duration_ms, "token_count": token_count,
         "content": content}, _mode()))


def rationale(stage_: str, agent: str, conclusion: str, because: str = "") -> UiEvent:
    from meshpipeline.trace.sanitizer import scrub_text
    return UiEvent(RATIONALE, stage_, {
        "agent": scrub_text(str(agent), limit=64),
        "conclusion": scrub_text(str(conclusion), limit=600),
        "because": scrub_text(str(because), limit=1200)})


def tool_call(stage_: str, cid: str, agent: str, tool_name: str,
              arguments: object = None, status: str = "started") -> UiEvent:
    if status not in TOOL_CALL_STATUSES:
        raise ValueError(f"{status!r} is not a tool-call status")
    from meshpipeline.trace.policy import project_tool_call
    return UiEvent(TOOL_CALL, stage_, project_tool_call(
        {"id": cid, "agent": agent, "tool_name": tool_name,
         "arguments": arguments, "status": status}, _mode()))


def tool_result(stage_: str, rid: str, call_id: str, agent: str, tool_name: str,
                result: object = None, status: str = "success",
                duration_ms: int | None = None) -> UiEvent:
    if status not in TOOL_RESULT_STATUSES:
        raise ValueError(f"{status!r} is not a tool-result status")
    from meshpipeline.trace.policy import project_tool_result
    return UiEvent(TOOL_RESULT, stage_, project_tool_result(
        {"id": rid, "tool_call_id": call_id, "agent": agent,
         "tool_name": tool_name, "result": result, "status": status,
         "duration_ms": duration_ms}, _mode()))


def _mode() -> str:
    from meshpipeline.trace.policy import current_mode
    return current_mode()


def meshing(stage_: str, engine: str, budget_s: int, history: dict | None = None) -> UiEvent:
    data: dict = {"engine": str(engine), "budget_s": int(budget_s)}
    if history:
        data["history"] = history
    return UiEvent(MESHING, stage_, data)


def meshed(stage_: str, cells: int | None = None) -> UiEvent:
    return UiEvent(MESHED, stage_, {"cells": int(cells) if cells else None})


def verdict(stage_: str, verdict_: str, summary: str = "") -> UiEvent:
    return UiEvent(VERDICT, stage_, {"verdict": str(verdict_), "summary": str(summary)})


def closing(text: str, event_id: str = "") -> UiEvent:
    data: dict[str, Any] = {"text": str(text)}
    if event_id:
        data["event_id"] = str(event_id)
    return UiEvent(CLOSING, "", data)
