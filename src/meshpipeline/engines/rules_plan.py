# Responsibility: Build an attempt on a model-led engine (cfMesh, VMTK, gmsh CFD, snappy multi-region) by the rules alone, with the same tools and gates the model uses, so a busy, failed or wandering builder model never fails a run.
# Owns: the rules plan's tool sequence per engine (AIG-1), the BUILDER_AI switch, and when an attempt falls back to it.
# Boundaries: answers builder rounds in place of the model; every tool call still goes through the builder's executor, its validation and every gate. It invents no size: each engine's own defaults and staged reading decide them.
# Collaborates with: agents/builder/invoke.py (runs it when the model gave no mesh), agents/builder/loop.py (the loop it drives), engines/<engine>/authoring.py (the defaults it leaves in force), pipeline/engine_fallback.layer_request (the layers the user asked for).
"""THE RULES BUILD WHEN THE AI DOES NOT (docs/RULES.md AIG-1).

On cfMesh, VMTK, gmsh (CFD) and snappy multi-region the builder is a model that calls tools. When
that model is down, busy, answers nothing usable, or ends its rounds without a mesh, the attempt is
not failed and not retried: the same attempt is built by this rules plan instead - the same tools,
the same validation, the same gates - and the run says so.

The rules plan per engine (the tool calls a careful builder makes, no model judgement):
  cfmesh             configure_mesh(n_layers = the count the request asks for, else the engine's
                     standard) -> run_mesh -> submit_mesh. The far field comes from the approved
                     extents, the sizes from the engine's own defaults.
  vmtk               geometry_report -> configure_mesh({}) when the engine staged the ports itself;
                     else seeds from the open profiles (first = inlet, the rest outlets) ->
                     run_mesh -> submit_mesh.
  gmsh               geometry_report -> write_file gmsh_spec.json (first order, the engine's size
                     factor, no groups: the engine binds each declared port to its face) -> run_mesh
                     -> submit_mesh.
  snappy_multiregion geometry_report -> configure_mesh(regions = the report's suggested regions)
                     -> run_mesh -> submit_mesh.

BUILDER_AI=off builds every such attempt this way (the rules-only baseline).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.settings.env import optional_env

logger = logging.getLogger(__name__)

#: BUILDER_AI=off: model-led engines are built by the rules plan, no model asked (AIG-1).
BUILDER_AI: bool = (optional_env("BUILDER_AI", "on") or "on").strip().lower() not in ("off", "false", "0", "no")

#: The engines whose builder is a model calling tools, and so have a rules plan here.
ENGINES = ("cfmesh", "vmtk", "gmsh", "snappy_multiregion")

RULE = "AIG-1"

#: gmsh's plan: the lab-proven defaults (first order, size factor of the diagonal, curvature points)
GMSH_SPEC = {"element_order": 1, "size": {"mode": "factor", "value": 0.04}, "curvature_nodes": 24,
             "optimize": True, "groups": []}


def covers(engine: str) -> bool:
    return str(engine or "") in ENGINES


def _tool_results(messages) -> list[tuple[str, dict]]:
    """(tool name, parsed result) for every tool result in the conversation, oldest first."""
    names: dict[str, str] = {}
    out: list[tuple[str, dict]] = []
    for m in messages or ():
        if not isinstance(m, Mapping):
            continue
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                names[str((tc or {}).get("id", ""))] = str(fn.get("name", ""))
        elif m.get("role") == "tool":
            c = m.get("content")
            try:
                d = json.loads(c) if isinstance(c, str) else (c or {})
            except ValueError:
                d = {"raw": str(c)[:2000]}
            out.append((names.get(str(m.get("tool_call_id", "")), str(m.get("name", ""))),
                        d if isinstance(d, dict) else {"value": d}))
    return out


def requested_layers(state: Mapping[str, Any]) -> int | None:
    """The layer count the request asks for (the product's own reader); None when it names none."""
    try:
        from meshpipeline.pipeline import engine_fallback as ef
        lr = ef.layer_request(str(state.get("request_txt") or ""), str(state.get("review_brief_txt") or ""))
        return int(lr.count) if lr.count else None
    except Exception:  # noqa: BLE001 - no reading: the engine's standard count
        return None


def _vmtk_args(report: Mapping) -> dict:
    if report.get("staged_ports"):
        return {}                         # the engine staged the seeds and the sizing itself
    profiles = [p for p in report.get("open_profiles") or [] if isinstance(p, Mapping) and "index" in p]
    if len(profiles) >= 2:
        return {"source_ids": [profiles[0]["index"]], "target_ids": [p["index"] for p in profiles[1:]]}
    return {}


class RulesBuilder:
    """A provider call that answers each builder round with the rules plan's next tool call."""

    def __init__(self, engine: str, state: Mapping[str, Any] | None = None) -> None:
        self.engine = str(engine or "")
        self.state = dict(state or {})
        self.rounds = 0

    @staticmethod
    def _result(calls=(), text: str = "") -> ModelRoundResult:
        tcs = tuple(ToolCallRequest(id=f"call_rules{uuid.uuid4().hex[:10]}", name=n, arguments=json.dumps(a))
                    for n, a in calls)
        return ModelRoundResult(tool_calls=tcs, assistant_text=text,
                                finish_reason="tool_calls" if tcs else "stop")

    def next_round(self, messages) -> ModelRoundResult:
        self.rounds += 1
        done = _tool_results(messages)
        names = [n for n, _ in done]
        last = done[-1] if done else ("", {})
        if "submit_mesh" in names:
            return self._result(text="Submitted by the rules plan." if last[1].get("success", True)
                                else "The rules plan could not submit this mesh.")
        if "run_mesh" in names or last[1].get("mesh_already_production_grade"):
            # a run_mesh ran (or one before this plan already built a production-grade mesh):
            # submit it - submit_mesh and the gates judge it, never this plan
            return self._result([("submit_mesh", {})])
        if last[0] in ("configure_mesh", "write_file") and last[1].get("success") is False:
            return self._result(text=f"The rules plan's {last[0]} was refused: "
                                     f"{str(last[1].get('error') or '')[:300]}")
        e = self.engine
        if e == "cfmesh":
            if "configure_mesh" not in names:
                asked = requested_layers(self.state)
                return self._result([("configure_mesh", {} if asked is None else {"n_layers": asked})])
            return self._result([("run_mesh", {})])
        if "geometry_report" not in names:
            return self._result([("geometry_report", {})])
        report = dict(done[names.index("geometry_report")][1])
        if report.get("error"):
            return self._result(text=f"The rules plan stops: geometry_report failed ({report['error']}).")
        if e == "gmsh":
            if "write_file" not in names:
                return self._result([("write_file", {"path": "gmsh_spec.json",
                                                     "content": json.dumps(GMSH_SPEC, indent=1)})])
            return self._result([("run_mesh", {})])
        if e in ("vmtk", "snappy_multiregion"):
            if "configure_mesh" not in names:
                if e == "vmtk":
                    args = _vmtk_args(report)
                else:
                    regions = report.get("suggested_regions")
                    if not regions:
                        return self._result(text="The rules plan stops: the report suggests no regions.")
                    args = {"regions": regions}
                return self._result([("configure_mesh", args)])
            return self._result([("run_mesh", {})])
        return self._result(text=f"There is no rules plan for the {e} engine.")

    async def __call__(self, *, messages, tools=None, job_id="", user_id="", tool_choice="auto",
                       on_reasoning=None) -> ModelRoundResult:
        return self.next_round(messages)


__all__ = ["BUILDER_AI", "ENGINES", "GMSH_SPEC", "RULE", "RulesBuilder", "covers", "requested_layers"]
