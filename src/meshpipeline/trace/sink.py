# Responsibility: Carry public trace events for work that happens before a job exists.
# Boundaries: a buffer for the pre-job window; once a job exists its own stream takes over.
from __future__ import annotations

from typing import Any

import meshpipeline.events as E


class PublicTraceSink:

    def __init__(self, stage: str = "intake", agent: str = "intake") -> None:
        self.stage = stage
        self.agent = agent
        self.events: list[dict[str, Any]] = []

    # the JobPublisher trace surface
    def reasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                  token_count: int | None = None, content: str | None = None,
                  status: str = "active") -> None:
        self._add(lambda: E.reasoning(self.stage, rid, self.agent, phase,
                                      duration_ms=duration_ms, token_count=token_count,
                                      content=content, status=status))

    def tool_call(self, cid: str, tool_name: str, arguments: Any = None,
                  status: str = "started") -> None:
        self._add(lambda: E.tool_call(self.stage, cid, self.agent, tool_name,
                                      arguments, status))

    def tool_result(self, rid: str, call_id: str, tool_name: str, result: Any = None,
                    status: str = "success", duration_ms: int | None = None) -> None:
        self._add(lambda: E.tool_result(self.stage, rid, call_id, self.agent, tool_name,
                                        result, status, duration_ms))

    def rationale(self, conclusion: str, because: str = "") -> None:
        self._add(lambda: E.rationale(self.stage, self.agent, conclusion, because))

    # transport
    def _add(self, build) -> None:
        try:
            # the constructors already apply the mode's projection, so what lands
            # in `events` is publishable as-is
            self.events.append(build().wire())
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "public trace: a pre-job event could not be built; the turn is unaffected")

    def drain(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def session_events(self) -> list[dict[str, Any]]:
        return [{"type": "public_trace", "payload": e} for e in self.drain()]


def project_all(events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from meshpipeline.trace.policy import project
    out: list[dict[str, Any]] = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        wire = e.get("payload") if e.get("type") == "public_trace" else e
        if not isinstance(wire, dict) or wire.get("type") not in E.EVENT_TYPES:
            continue
        projected = project(wire)
        if projected is not None:
            out.append(projected)
    return out
