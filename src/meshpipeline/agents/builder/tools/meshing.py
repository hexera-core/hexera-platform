# Responsibility: Let the builder configure, run and submit a mesh.
# Owns: the configuration palette, the mesh run, the input-contract rejection and submission.
# Boundaries: the model chooses strategy values through a declarative palette.
from __future__ import annotations

import hashlib
import json
import logging
import time as _time
from dataclasses import dataclass
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.builder.tool_context import BuilderToolContext

#: THE staged surface every engine analyses. A DERIVED workspace artefact - written upstream by the
#: engine's own tessellator - not the source geometry and not an identity. It carries no unit,
#: which is precisely why a tool that measures it must also hold the interpretation.
STAGED_SURFACE = "input.stl"

logger = logging.getLogger(__name__)


# The patch contract travels with every step of configure/run/submit, so a retry cannot quietly
# drop the boundaries the user approved.
from meshpipeline.agents.builder.tools.workspace import _confined

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "submit_mesh",
            "description": (
                "Call this when run_mesh has produced a VALID mesh (no fatal defects). "
                "Verifies the engine's declared deliverable exists and signals task completion. "
                "Returns success=true if the deliverable is present, or an error if it is not. "
                "This is the FINAL call - do not make any tool calls after submit_mesh succeeds."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_mesh",
            "description": (
                "Run the selected engine's mesher on the authored mesh spec, then validate the "
                "result. Returns cell/element count + quality + fatal defects (plus any "
                "engine-specific metrics the engine reports). Call once your mesh is authored "
                "(configure_mesh, or writing the engine's spec file). If invalid, adjust the "
                "spec and run_mesh again. When valid, submit_mesh."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

ACTIONS: dict[str, str] = {
    "configure_mesh":  "Setting the meshing strategy",
    "run_mesh":        "Generating the mesh",
    "submit_mesh":     "Submitting the mesh",
}

def submit_flag_responses_property(flags: tuple) -> dict:
    return {
        "type": "array",
        "description": (
            "One entry per region the engineer flagged, identified by its ordinal. Declare what "
            "you set out to correct and what you actually changed. 'nothing, because ...' is an "
            "acceptable answer; omitting a flag is not. The review that follows measures the mesh "
            f"at each region itself. Exactly {len(flags)} "
            f"entr{'y' if len(flags) == 1 else 'ies'} are required."),
        "items": {
            "type": "object",
            "properties": {
                "ordinal": {"type": "integer", "description": "the flag's number, as given to you"},
                "intended_correction": {"type": "string",
                                        "description": "what you set out to correct here"},
                "change_made": {"type": "string",
                                "description": "the authoring or parameter change you made"},
                "affected_region": {"type": "string",
                                    "description": "the patch or region your change affects"},
                "believed_addressed": {"type": "boolean",
                                       "description": "your own judgement - the reviewer verifies"},
            },
            "required": ["ordinal", "intended_correction", "change_made", "affected_region",
                         "believed_addressed"],
            "additionalProperties": False,
        },
    }


def submit_mesh(ctx: BuilderToolContext, args: dict | None = None) -> dict:
    from meshpipeline.engines.registry import get_spec as _get_spec
    from meshpipeline.engines.runtime import get_engine
    workspace = ctx.workspace
    _pol = _get_spec(getattr(get_engine(ctx.engine), "name", "cfmesh")).run_policy
    if not (workspace / _pol.submit_marker).exists():
        return {"success": False, "error": _pol.submit_hint}

    # THE PER-FLAG DECLARATION. Required only when an engineer actually flagged regions, so an
    # ordinary submit is unchanged. It is written into the workspace because that is what survives
    # the loop, the process and a restart - the loop's own return channel is a string.
    from meshpipeline.contracts import human_flags as HF
    if HF.expected_ordinals(ctx.user_dispute):
        responses, problems = HF.parse_flag_responses(
            (args or {}).get("flag_responses"), user_dispute=ctx.user_dispute)
        if problems:
            return {"success": False,
                    "error": "the engineer's flagged regions are not all answered: "
                             + "; ".join(problems[:8])}
        (workspace / "flag_responses.json").write_text(
            json.dumps(HF.as_dicts(responses), indent=2), encoding="utf-8")
    return {"success": True, _pol.submit_ok_key: True}


# engine-native builder tools (drive the domain's runner + its syntax library)
def _contract_patches(workspace: Path) -> list[dict]:
    from meshpipeline.engines.workspace_facts import contract_patches
    return contract_patches(workspace)


def _contract_wall_patch(workspace: Path) -> str | None:
    from meshpipeline.engines.workspace_facts import contract_wall_patch
    return contract_wall_patch(workspace)


def configure_mesh(ctx: BuilderToolContext, args: dict) -> dict:
    from meshpipeline.engines.runtime import get_engine
    workspace, engine = ctx.workspace, ctx.engine
    R = get_engine(engine)
    ename = getattr(R, "name", "")
    if not hasattr(R, "configure_mesh"):
        return {"error": f"configure_mesh is not available for the {ename!r} engine "
                         f"(it authors its mesh spec directly)."}
    ctx.require_geometry()
    gf = args.get("geometry_file", STAGED_SURFACE)
    stl = _confined(workspace, gf)
    if stl is None:
        return {"error": f"Path escape attempt blocked: {gf}"}
    if not stl.exists():
        return {"error": f"{gf} not found (CAD is tessellated to {STAGED_SURFACE} upstream)."}
    # SUBMIT STEER. A PRODUCTION-GRADE mesh already exists (a prior run_mesh judged it so) -
    # the model tends to keep re-tuning after a good mesh instead of committing.
    if (workspace / ".mesh_ok").exists():
        return {"success": True, "mesh_already_production_grade": True,
                "next": "STOP - you ALREADY have a PRODUCTION-GRADE mesh built (run_mesh judged it "
                        "so). Call submit_mesh NOW. Re-configuring only discards a good mesh."}
    # IDEMPOTENCE GUARD. configure_mesh is deterministic - identical args produce identical dicts.
    # Re-issuing the SAME args is a stuck loop that never builds; redirect to run_mesh.
    _required = get_spec_run_files(ename)
    _sig = hashlib.md5(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    _sig_file = workspace / ".configure_sig"
    _dicts_exist = all((workspace / d).exists() for d in _required)
    if _dicts_exist and _sig_file.exists() and _sig_file.read_text().strip() == _sig:
        return {"success": True, "already_configured": True,
                "next": "STOP - the mesh is ALREADY configured with these EXACT settings. "
                        "Call run_mesh NOW. Reconfigure ONLY in response to a concrete run_mesh "
                        "failure that names a value to change."}
    try:
        # The engine's palette (authoring_tool) declares its OWN strategy knobs - pull them
        # from the schema, not a hardcoded list, so the builder names no mesh vocabulary.
        from meshpipeline.engines.registry import get_spec
        _palette = get_spec(ename).authoring_tool or {}
        _keys = tuple((_palette.get("function", {}).get("parameters", {})
                       .get("properties") or {}).keys())
        strategy = args.get("strategy") or {k: args[k] for k in _keys if k in args}
        # VALIDATED PALETTE (engines-as-tools): the ENGINE checks the strategy against its
        # OWN block grammar via the shared Diagnostic protocol - reject unknown / cross-engine /
        # out-of-range knobs LOUDLY instead of silently clamping (mirrors gmsh's driver).
        # Builder stays generic: it knows no cfmesh/snappy vocabulary. Nested strategy keys
        # are surfaced so validation is not bypassed by wrapping the knobs in {"strategy": …}.
        _check = {**args, **(args["strategy"] if isinstance(args.get("strategy"), dict) else {})}
        _check.pop("strategy", None)
        _diags = get_spec(ename).validate_authoring(_check)
        _errs = [x for x in _diags if x.severity == "error"]
        if _errs:
            return {"success": False, "ok": False,
                    "diagnostics": [x.as_dict() for x in _diags],
                    "error": "configure_mesh rejected the strategy: "
                             + "; ".join(f"{x.path}: {x.message}" if x.path else x.message
                                         for x in _errs),
                    "next": "Fix the flagged strategy field(s) and call configure_mesh again."}
        # the CONTRACT wall name wins over the model's arg / the generic default - manifest
        # validation enforces it, so the produced patch must carry the contract's name.
        wall = _contract_wall_patch(workspace) or args.get("wall_patch") or "body"
        # The staged surface is metres because staging made it so; this says which conversion
        # produced it, so the engine measures physical size instead of bare numbers.
        from meshpipeline.cad.staging import staged_surface
        result = R.configure_mesh(
            workspace, geometry_file=gf, strategy=strategy, wall_patch=wall,
            contract_patches=_contract_patches(workspace), args=args,
            cell_budget=polcfg.CELL_HARD_LIMIT,
            surface=staged_surface(ctx.geometry, Path(workspace) / gf))
        if isinstance(result, dict) and result.get("success"):
            _sig_file.write_text(_sig)   # mark configured only on a real render
        return result
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def get_spec_run_files(engine: str) -> tuple:
    from meshpipeline.engines.registry import get_spec
    pol = get_spec(engine).run_policy
    return pol.required_files if pol else ()


def input_contract_rejection(spec, workspace: Path, *, geometry=None) -> str:
    from meshpipeline.cad.staging import staged_surface
    from meshpipeline.cad.surface_checks import surface_analysis_for
    stl = workspace / STAGED_SURFACE
    if not stl.exists() or geometry is None:
        return ""
    analysis = surface_analysis_for(spec, staged_surface(geometry, stl))
    if analysis is None:   # contract measures nothing here, or the probe failed → allow the build
        return ""
    return spec.geometry_unsuitable(analysis)

_RUN_MESH_FEEDBACK_RESERVE = 420   # seconds (180 was too thin live: a capped remote
                                   # round returning near the deadline + one long model
                                   # round still grazed BUILDER_LOOP_TIMEOUT - as1 job
                                   # 5f92fced, attempt 1)
_RUN_MESH_MIN_ATTEMPT = 300        # seconds


#: What a mesh run needs once it has been decided, and everything its announcement says. The
#: cap, purpose and estimate are computed ONCE here and used by both the event and the run, so
#: the two can never disagree. `refusal` set means the run was declined before the announcement
#: boundary: nothing is announced and nothing is executed.
@dataclass(frozen=True)
class PreparedMeshRun:

    engine: str = ""
    cap: int = 0
    purpose: str = ""
    history: dict | None = None
    refusal: dict | None = None


# Everything `run_mesh` decided BEFORE it announced itself. Blocking (registry lookups, file
# probes, the engine's own contract gate), so it stays off the event loop; it publishes nothing.
def prepare_mesh_run(ctx: BuilderToolContext) -> PreparedMeshRun:
    from meshpipeline.engines.registry import get_spec as _get_spec
    from meshpipeline.engines.runtime import get_engine
    workspace, engine = ctx.workspace, ctx.engine
    loop_deadline = ctx.loop_deadline
    # THE BUILD-DRIVER BOUNDARY. Past this line the engine owns preparation of its native input,
    # and it cannot do that correctly without knowing what the coordinates mean.
    ctx.require_geometry()
    R = get_engine(engine)
    ename = getattr(R, "name", "cfmesh")
    _spec = _get_spec(ename)
    _policy = _spec.run_policy
    missing = [d for d in _policy.required_files if not (workspace / d).exists()]
    if missing:
        return PreparedMeshRun(refusal={
            "success": False,
            "error": f"missing {', '.join(missing)} - run configure_mesh "
                     "first (it renders the case from your strategy)."})
    # DETERMINISTIC INPUT-CONTRACT GATE (physical impossibility, not a strategy problem).
    # Fires even if the builder never inspected the geometry - see input_contract_rejection.
    # (The node_builder pre-flight normally rejects such input before the loop even starts;
    # this is the defence-in-depth layer for a build that reached run_mesh anyway.)
    _reject = input_contract_rejection(_spec, workspace, geometry=ctx.geometry)
    if _reject:
        return PreparedMeshRun(refusal={
            "success": False, "geometry_unsuitable": True, "error": _reject,
            "guidance": "The INPUT geometry is unmeshable for this engine. STOP and report "
                        "this to the user - do NOT retry with different mesh settings, "
                        "edge lengths, layers or capping: no meshing parameter can fix a "
                        "defect in the input surface. It must be repaired or replaced."})
    # SUBMIT STEER: a PRODUCTION-GRADE mesh already exists - do NOT re-run a 25-min off-box job.
    if (workspace / ".mesh_ok").exists():
        return PreparedMeshRun(refusal={
            "success": True, "mesh_already_production_grade": True,
            "guidance": "You ALREADY built a PRODUCTION-GRADE mesh (run_mesh judged it so). "
                        "Re-running is a wasteful 25-min off-box job - call submit_mesh NOW."})
    # The cap is ENGINE-DECLARED (spec.run_policy): a draft mesher fails fast,
    # a production off-box mesher gets its legitimate 20-35 min - BUT it must FIT inside
    # the remaining builder-loop budget, or the outer loop kills the attempt mid-run and
    # the builder never receives any feedback. (Forensic finding: four multiregion attempts
    # died at exactly BUILDER_LOOP_TIMEOUT with zero remote returns - the engine's 3000s
    # run budget could never fit a 3600s loop after preamble rounds.)
    _cap = _policy.run_timeout()
    if loop_deadline is not None:
        _remaining = int(loop_deadline - _time.monotonic())
        _usable = _remaining - _RUN_MESH_FEEDBACK_RESERVE
        if _usable < _RUN_MESH_MIN_ATTEMPT:
            return PreparedMeshRun(refusal={
                "success": False,
                "error": "not_enough_loop_budget_for_remote_run",
                "remaining_loop_seconds": max(0, _remaining),
                "requested_run_timeout": _cap,
                "required_reserve_seconds": _RUN_MESH_FEEDBACK_RESERVE,
                "minimum_attempt_seconds": _RUN_MESH_MIN_ATTEMPT,
                "guidance": ("Too little builder-loop budget remains for a meaningful mesh run. "
                             "Do NOT retry run_mesh now. If the mesh strategy is already sound, "
                             "the next attempt will re-run it with a fresh budget; otherwise "
                             "REDUCE the configured work first - lower the cell budget and/or "
                             "refinement in your engine's configure palette - so the run "
                             "finishes faster."),
            })
        if _usable < _cap:
            logger.info("run_mesh: capping dispatch timeout %ds -> %ds to fit the remaining "
                        "loop budget (%ds - %ds reserve)", _cap, _usable, _remaining,
                        _RUN_MESH_FEEDBACK_RESERVE)
            _cap = _usable
    # announce the run: the declared budget it is bounded by, PLUS what similar runs
    # have actually taken (measured, engine+purpose-keyed). The UI bars elapsed against
    # the real history when we have it, and falls back to the budget - honestly labelled
    # - when we do not. We never invent a percentage.
    from meshpipeline.engines.workspace_facts import read_purpose
    _purpose = read_purpose(workspace)
    _history = None
    try:
        from meshpipeline.engines.mesh_history import estimate as _hist_estimate
        _history = _hist_estimate(engine, _purpose)
    except Exception:  # noqa: BLE001 - an absent history is honestly absent, never invented
        _history = None
    return PreparedMeshRun(engine=engine, cap=_cap, purpose=_purpose, history=_history)


# The two halves composed, WITHOUT the announcement. The announcement is an execution-owned
# event and must be authorized on the event loop, which this cannot do: it runs on a worker
# thread. `BuilderToolExecutor` therefore drives the halves itself and publishes between them.
def run_mesh(ctx: BuilderToolContext) -> dict:
    prepared = prepare_mesh_run(ctx)
    if prepared.refusal is not None:
        return prepared.refusal
    return execute_prepared_mesh_run(ctx, prepared)


# The native run and its verdict. Blocking, so it stays off the event loop. It runs only after
# the announcement above it was authorized, which is why it is a separate callable.
def execute_prepared_mesh_run(ctx: BuilderToolContext, prepared: PreparedMeshRun) -> dict:
    from meshpipeline.engines.registry import get_spec as _get_spec
    from meshpipeline.engines.runtime import get_engine
    workspace, engine = ctx.workspace, prepared.engine
    R = get_engine(engine)
    _spec = _get_spec(getattr(R, "name", "cfmesh"))
    _policy = _spec.run_policy
    _cap, _purpose = prepared.cap, prepared.purpose
    _t_mesh_start = _time.monotonic()
    # THE handoff. The engine owns preparing its native input from here, and `context`
    # is how it learns what the coordinates mean - without it the bundle would have to
    # re-infer a unit from a file that does not state one.
    res = R.run_cartesian_mesh(workspace, timeout=_cap, context=ctx)
    _mesh_seconds = _time.monotonic() - _t_mesh_start
    # RECORD the real duration of a run that actually MESHED (rc==0). A timeout or an
    # infra failure is not a data point about how long this shape takes. Best-effort.
    if res.get("rc") == 0:
        try:
            from meshpipeline.engines.mesh_history import record as _hist_record
            _hist_record(engine, _purpose, _mesh_seconds)
        except Exception:  # noqa: BLE001
            pass
    if res["timed_out"]:
        return {"success": False, "timed_out": True,
                "guidance": f"meshing exceeded {_cap // 60} min - too fine. {_policy.timeout_hint}",
                "log_tail": res["log_tail"]}
    # rc=-3 is the remote runner's INFRASTRUCTURE failure marker (adapters.mesh_execution.cloud_run_client._fail:
    # job trigger/poll/download broke) - a SYSTEM failure, not a mesh/strategy problem.
    # Without this branch it surfaced like a mesher failure and the model "fixed" a
    # correct spec in response to an outage.
    if res["rc"] == -3 and "[CLOUD_RUN_RESULT_UNCOLLECTED]" in str(res.get("log_tail") or ""):
        _rq = res.get("remote_quality") or {}
        _over_cap = "over the" in str(res.get("log_tail")) and "cap" in str(res.get("log_tail"))
        return {"success": False, "system_failure": not _over_cap, "rc": -3,
                "log_tail": res["log_tail"], "remote_quality": _rq,
                "guidance": ((f"The mesh RAN ({_rq.get('cells')} cells) but its result archive "
                              "exceeded the exchange's size cap on the way back. Reduce the cell "
                              "budget and the refinement so the delivered mesh fits, then "
                              "run_mesh again. Do NOT enlarge anything.") if _over_cap else
                             ("The mesh ran but its output could not be brought back from the "
                              "remote runner (cloud infrastructure), not your mesh spec. Call "
                              "run_mesh again unchanged; if it fails the same way again, STOP."))}
    if res["rc"] == -3:
        return {"success": False, "system_failure": True, "rc": -3,
                "log_tail": res["log_tail"],
                "guidance": ("The REMOTE MESH RUNNER failed (cloud infrastructure), not your "
                             "mesh spec. Do NOT change the configuration in response to this. "
                             "Call run_mesh again unchanged; if it fails the same way again, "
                             "STOP - the run will be retried on fresh infrastructure.")}
    q = R.check_mesh(workspace)
    fatal = q.get("fatal", [])
    ok = res["rc"] == 0 and not fatal
    out = {
        "success": ok, "rc": res["rc"], "cells": q.get("cells"),
        "mesh_ok": q.get("mesh_ok"), "fatal_defects": fatal,
        "max_non_ortho": q.get("max_non_ortho"), "max_skewness": q.get("max_skewness"),
    }
    _enricher = _spec.run_enricher
    if _enricher is not None:
        _enricher(R, workspace, res, q, out)
        ok = bool(out.get("success"))
    else:
        out["guidance"] = (_policy.ok_guidance if ok else
                           f"Not valid ({fatal or _policy.fail_label}). {_policy.fail_hint}")
    out["log_tail"] = "" if ok else res["log_tail"]
    return out
