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
from meshpipeline.agents.builder.tool_context import HANDS_OFF, BuilderToolContext

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
    from meshpipeline.engines.bucket_files import present
    if not ((workspace / _pol.submit_marker).exists() or present(workspace, _pol.submit_marker)):
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
    # WHAT THE BUILD DID WITH THE USER'S NOTES, in one plain line for the result (contracts/
    # user_notes). Best effort: kept as written, never checked, never a reason to refuse a mesh.
    if ctx.user_notes and (args or {}).get("notes_remark"):
        from meshpipeline.contracts.user_notes import record_remark
        record_remark(workspace, (args or {}).get("notes_remark"), by="builder")
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

        def _render(rules_args: dict | None = None) -> dict:
            # the AI plan (None), or the rules plan's arguments written the same way
            a = args if rules_args is None else rules_args
            g = gf if rules_args is None else STAGED_SURFACE
            return R.configure_mesh(
                workspace, geometry_file=g,
                strategy=strategy if rules_args is None else {k: a[k] for k in _keys if k in a},
                wall_patch=wall, contract_patches=_contract_patches(workspace), args=a,
                cell_budget=polcfg.CELL_HARD_LIMIT,
                surface=staged_surface(ctx.geometry, Path(workspace) / g))
        result = _render()
        if isinstance(result, dict) and result.get("success"):
            # THE AI PLAN IS NEVER WORSE THAN THE RULES PLAN (AIG-2): planned over the cell limit
            # where the rules plan is not, the rules plan is configured instead and the result says so
            from meshpipeline.engines.plan_guard import keep_no_worse
            result = keep_no_worse(engine=ename, workspace=Path(workspace), ai_strategy=strategy,
                                   ai_result=result, render=_render,
                                   limit=int(polcfg.CELL_HARD_LIMIT), job_id=ctx.job_id)
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
    #: the case this run hands the mesher (agents/builder/rerun_guard.case_digest)
    case_digest: str = ""


# Everything `run_mesh` decided BEFORE it announced itself. Blocking (registry lookups, file
# probes, the engine's own contract gate), so it stays off the event loop; it publishes nothing.
def _unsafe_patch_names(workspace: Path) -> list[str]:
    """The approved patch names in the workspace that no mesher can write (none when the run
    carries no declaration)."""
    from meshpipeline.contracts.patch_names import is_mesh_safe
    path = workspace / "port_declaration.json"
    if not path.exists():
        return []
    try:
        declared = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(p.get("name")) for p in declared if isinstance(p, dict)
            and isinstance(p.get("name"), str) and p["name"].strip()
            and not is_mesh_safe(p["name"].strip())]


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
    # THE APPROVED PATCH NAMES, before any mesh runs. The intake makes every name mesh-safe, but a
    # snapshot approved before that rule, or a direct caller, can still carry one a mesher cannot
    # write - and the run then ends 26 minutes later as "patch has zero faces". The approved list
    # travels beside the contract as port_declaration.json; refuse here, in seconds, instead.
    _unsafe = _unsafe_patch_names(workspace)
    if _unsafe:
        from meshpipeline.contracts.failure_cause import FailureCause
        from meshpipeline.contracts.patch_names import mesh_safe as _mesh_safe
        from meshpipeline.engines.preflight import PreflightRefusal
        _error = ("approved patch name(s) a mesher cannot write: " + ", ".join(
            f"{n!r} (would need {_mesh_safe(n)!r})" for n in _unsafe))
        # RECORDED for the executor too (engines/preflight.py), so a loop that ends here is
        # reported as the contract mismatch it is - the names and the spelling to use, no retry -
        # rather than as a mesher that "did not finish" on an empty workspace.
        PreflightRefusal(
            gate="patch_contract", cause=FailureCause.CONTRACT_MISMATCH,
            builder_text=f"[PREFLIGHT_CONTRACT_MISMATCH] {_error}",
            facts={"missing": list(_unsafe), "renamed": {n: _mesh_safe(n) for n in _unsafe},
                   "before_meshing": True}).write(workspace)
        return PreparedMeshRun(refusal={
            "success": False, "contract_unbuildable": True,
            "error": _error,
            "guidance": "The approved patch contract itself cannot be built - no mesh setting "
                        "can fix a name. STOP and report it; do NOT run the mesher or retry."})
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
    # NO IDENTICAL RETRY: the exact case an earlier attempt of this run already ran to a verdict is
    # refused here, before the announcement - the same engine, spec and surfaces give the same
    # result back, and another attempt would only repeat it (agents/builder/rerun_guard.py).
    from meshpipeline.agents.builder import rerun_guard
    _digest = rerun_guard.case_digest(workspace,
                                      _policy.required_files if _policy is not None else ())
    _earlier = rerun_guard.earlier_identical(workspace, _digest)
    if _earlier is not None:
        logger.warning("run_mesh: the case in %s already ran in %s (%s) - refusing an identical "
                       "re-run", workspace, _earlier.get("attempt"), _earlier.get("outcome"))
        return PreparedMeshRun(refusal=rerun_guard.refusal(_earlier))
    # A GUARD CLOCK ALREADY STOPPED THIS ATTEMPT (docs/RULES.md TIME-3): the attempt ends on it and
    # is replayed as it was. A run reconfigured after it would be a lever chosen by a clock.
    if _attempt_tripped(workspace):
        return PreparedMeshRun(refusal=_GUARD_REFUSAL)
    # The cap is the run's GUARD (contracts/work_limits, TIME-2): ENGINE-DECLARED (spec.run_policy)
    # or, when the engine planned the case, set from its planned cells. A draft mesher fails fast,
    # a production off-box mesher gets its legitimate 20-35 min - BUT it must FIT inside
    # the remaining builder-loop budget, or the outer loop kills the attempt mid-run and
    # the builder never receives any feedback. (Forensic finding: four multiregion attempts
    # died at exactly BUILDER_LOOP_TIMEOUT with zero remote returns - the engine's 3000s
    # run budget could never fit a 3600s loop after preamble rounds.)
    _cap = _policy.run_timeout()
    _plan_cap = getattr(_policy, "planned_timeout", None)
    if getattr(_policy, "watched", False):
        # NO GUESSED TIME LIMIT (contracts/native_time): a watched engine's run is stopped only
        # after the stall window with no progress, or at the wall of the machine it runs on - and
        # its time is not the builder loop's (agents/loop/budget excludes native time), so the
        # loop budget never cuts it either
        from meshpipeline.contracts.native_time import wall_for
        _cap = wall_for(workspace)
        _plan_cap = None
        loop_deadline = None
    if _plan_cap is not None:
        # the engine TIMED this case when it wrote it (the cells it will make): it gets the time
        # its plan needs, not the fixed cap - and still only what the loop budget leaves
        try:
            _planned = _plan_cap(workspace)
        except Exception:  # noqa: BLE001 - a plan that cannot be read leaves the declared cap
            logger.warning("run_mesh: the engine's run plan could not be read", exc_info=True)
            _planned = None
        if _planned:
            logger.info("run_mesh: the planned case gets %ds (declared cap %ds)", _planned, _cap)
            _cap = int(_planned)
    _cite_run_guard(ctx, ename, workspace, int(_cap))
    if loop_deadline is not None:
        from meshpipeline.contracts.progress_clock import model_time
        _remaining = int(loop_deadline - model_time())
        _usable = _remaining - _RUN_MESH_FEEDBACK_RESERVE
        if _usable < _RUN_MESH_MIN_ATTEMPT:
            # THE ATTEMPT'S OWN GUARD has run out (TIME-5): a guard trip, recorded beside the case,
            # and the attempt is replayed as it was - never a cue to "reduce the work", which would
            # make the mesh depend on how long the turns before it took
            from meshpipeline.contracts.work_limits import TIME_JOB_GUARD
            from meshpipeline.engines.determinism import note_guard_trip
            note_guard_trip("builder.attempt", workspace=workspace, rule=TIME_JOB_GUARD,
                            left_s=float(max(0, _remaining)), needed_s=float(
                                _RUN_MESH_FEEDBACK_RESERVE + _RUN_MESH_MIN_ATTEMPT))
            return PreparedMeshRun(refusal={
                **_GUARD_REFUSAL,
                "error": "not_enough_loop_budget_for_remote_run",
                "remaining_loop_seconds": max(0, _remaining),
                "requested_run_timeout": _cap,
                "required_reserve_seconds": _RUN_MESH_FEEDBACK_RESERVE,
                "minimum_attempt_seconds": _RUN_MESH_MIN_ATTEMPT,
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
    return PreparedMeshRun(engine=engine, cap=_cap, purpose=_purpose, history=_history,
                           case_digest=_digest)


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
    out = _run_and_judge(ctx, prepared)
    # what this exact case came to, so a later attempt that hands the mesher the very same case
    # is refused before it starts (agents/builder/rerun_guard.py)
    from meshpipeline.agents.builder import rerun_guard
    rerun_guard.remember(ctx.workspace, prepared.case_digest, out)
    return out


def _broken_at_time_out(policy, log_tail) -> list[str]:
    """The broken cells a timed-out run was still failing to repair (the engine's own reading of
    its log, RunPolicy.timed_out_defects) - [] for a run that was only slow. Words for the record
    only: what a run was doing when its guard tripped decides nothing (TIME-3)."""
    hook = getattr(policy, "timed_out_defects", None)
    if hook is None:
        return []
    try:
        return [str(x) for x in (hook(str(log_tail or "")) or []) if str(x).strip()]
    except Exception:  # noqa: BLE001 - a log that cannot be read leaves the plain time-out
        logger.warning("run_mesh: could not read the timed-out run's log", exc_info=True)
        return []


#: What run_mesh says when a guard clock stopped this attempt (docs/RULES.md TIME-3).
_GUARD_REFUSAL = {
    "success": False, "guard_tripped": True,
    "guidance": ("A guard clock stopped this attempt's mesh run - an infrastructure stop, not a "
                 "result about the mesh or your settings. Do NOT run_mesh again and do NOT "
                 "reconfigure - end your turn now. The attempt is run again, unchanged.")}


def _cite_run_guard(ctx: BuilderToolContext, engine: str, workspace, cap: int) -> None:
    """The run's guard in its why record (TIME-2): the planned work it came from when the engine
    planned the case (its run plan's `guard`), else the engine's declared cap. Never raises."""
    import json as _json

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.contracts.work_limits import TIME_GUARD, PlannedWork
    try:
        planned = (_json.loads((workspace / "system" / "run_plan.json").read_text()) or {}
                   ).get("guard")
    except (OSError, ValueError, AttributeError):
        planned = None
    if not (isinstance(planned, dict) and planned.get("guard_s") == cap):
        planned = PlannedWork(where=engine, unit="cells", amount=None, rate=None,
                              floor_s=float(cap), ceiling_s=float(cap)).as_dict()
    dr.cite(str(getattr(ctx, "job_id", "") or ""), stage=dr.STAGE_WORK_LIMIT, decided="guard_s",
            value=cap, rule=TIME_GUARD, inputs=planned,
            words=f"{engine} run: guard {cap // 60} min"
                  + (" from its planned cells" if planned.get("amount") else
                     " (its declared cap: no planned cells)"))


def _attempt_tripped(workspace) -> bool:
    """Whether a guard clock already stopped a run of the attempt in `workspace`."""
    from meshpipeline.contracts.mesh_execution import native_runs
    from meshpipeline.contracts.work_limits import ends_attempt
    from meshpipeline.engines.determinism import guard_trips
    if any(ends_attempt(t) for t in guard_trips(workspace)):
        return True
    launched = [r for r in (native_runs(workspace) or []) if not r.get("refused_before_launch")]
    return bool(launched and launched[-1].get("timed_out"))


def _guard_verdict(ctx: BuilderToolContext, engine: str, cap: int, broken: list[str],
                   log_tail, *, stalled: bool = False) -> dict:
    """A run its guard clock stopped (TIME-3): recorded beside the case as a guard trip, said to
    the builder as the end of its turn. Not a broken mesh and not "too fine": what the run was
    doing when the clock stopped it depends on how busy the machine was. A watched run (RUN-5)
    stopped as stuck - no sign of life for the stall window - is a trip too, and says so."""
    from meshpipeline.contracts.work_limits import ends_attempt
    from meshpipeline.engines.determinism import guard_trips, note_guard_trip
    if not any(ends_attempt(t) for t in guard_trips(ctx.workspace)):
        note_guard_trip(engine, workspace=ctx.workspace, guard_s=float(cap),
                        stalled=(True if stalled else None),
                        repairing=("; ".join(broken) or None))
    out = {**_GUARD_REFUSAL, "timed_out": True, "log_tail": log_tail}
    if stalled:
        from meshpipeline.contracts.native_time import STALL_WINDOW_S
        out["stalled"] = True
        out["guidance"] = (f"The mesher made no progress for {STALL_WINDOW_S // 60} min and was "
                           f"stopped as stuck. {_GUARD_REFUSAL['guidance']}")
    return out


def _run_and_judge(ctx: BuilderToolContext, prepared: PreparedMeshRun) -> dict:
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
    _watched = bool(getattr(_policy, "watched", False))
    if _watched:
        # RUN-5 (contracts/native_time.run_off_the_wall): a watched run stopped at the Cloud Run
        # job's wall STILL WORKING runs again, the same case, on a VM - never handed back as "too
        # fine" for a limit it only met because of where it ran
        from meshpipeline.contracts.native_time import run_off_the_wall
        res = run_off_the_wall(
            workspace, lambda cap: R.run_cartesian_mesh(workspace, timeout=cap, context=ctx),
            engine=engine, job_id=str(getattr(ctx, "job_id", "") or ""))
    else:
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
    if res.get("case_contract_mismatch"):
        # REFUSED BEFORE LAUNCH (engines/case_contract.py): the written case does not build the
        # approved patches, so no mesher ran. A spec the model wrote (gmsh's groups) is its to fix;
        # a case the engine rendered from the contract is our defect, and re-configuring renders
        # the same names - the job ends as an internal error either way if it is not fixed.
        _mine = not res.get("internal_defect")
        return {"success": False, "rc": res.get("rc"), "patch_contract_mismatch": True,
                **({} if _mine else {HANDS_OFF: True}),
                "mismatch": res.get("case_contract_mismatch"),
                "guidance": ("Nothing was meshed: your spec does not build the approved patches "
                             "exactly - " + "; ".join(res.get("case_contract_mismatch") or [])
                             + ". Fix the group names/roles in your spec to match the patch "
                               "contract, then run_mesh again."
                             if _mine else
                             "Nothing was meshed: the case rendered from the patch contract does "
                             "not build the approved patches (an internal error, not your "
                             "strategy). Do NOT reconfigure - STOP and end your turn.")}
    if res.get("oversize"):
        # THE SIZE BRAKE stopped the mesher (engines/size_brake): its own count read far past
        # the case's plan. Deterministic: the same case comes out the same size every time, so it
        # must be made coarser by about that much - never re-run as it is.
        _o = res["oversize"]
        if _o.get("by_rule"):
            # THE ENGINE'S OWN SIZING RULE asks for that many cells (gmsh's passage field: 13 across
            # every passage, whatever the wall size) - no setting of this case brings it inside one job,
            # so the run moves on to the next engine (pipeline/engine_fallback), said as the size it is
            from meshpipeline.contracts.failure_cause import FailureCause
            from meshpipeline.engines.preflight import PreflightRefusal
            _words = (f"the {_o.get('stage')} would make about {int(_o.get('cells') or 0):,} cells, "
                      f"{float(_o.get('ratio') or 0):.1f} times the {int(_o.get('budget') or 0):,} one job is "
                      "sized for")
            PreflightRefusal(
                gate="finalize", cause=FailureCause.CELL_BUDGET.value,
                builder_text=f"[MESH_OVER_SIZE] {_words}",
                facts={"over_size_by_rule": True, "estimated_cells": int(_o.get("cells") or 0),
                       "cell_limit": int(_o.get("budget") or 0), "meshers_started": 1}).write(workspace)
            return {"success": False, "oversize": dict(_o), HANDS_OFF: True,
                    "guidance": (f"Not meshed: {_words}. This engine's own passage sizing asks for "
                                 "that, so a coarser size does not change it. Do NOT run_mesh again and "
                                 "do NOT reconfigure - end your turn now; the run moves on to another "
                                 "engine."),
                    "log_tail": res.get("log_tail", "")}
        return {"success": False, "oversize": dict(_o),
                "guidance": (f"Stopped before finishing: the mesh {_o.get('stage')} already had "
                             f"{int(_o.get('cells') or 0):,} cells, {float(_o.get('ratio') or 0):.1f} times "
                             f"the {int(_o.get('budget') or 0):,} it was planned for. Make the wall cells "
                             f"coarser by about {max(1.0, float(_o.get('ratio') or 1.0) / 0.9) ** 0.5:.2f}x "
                             "(or drop refinements), then configure and run_mesh again."),
                "log_tail": res.get("log_tail", "")}
    if res.get("layers_not_converging"):
        # VMTK's layers would not untangle and the plan asks for them: the run moves on to the next
        # engine (engines/vmtk/vmtk_runner LAYERS_MOVE_ON) - never this engine's layer-free fill
        from meshpipeline.contracts.failure_cause import FailureCause
        from meshpipeline.engines.preflight import PreflightRefusal
        _l = dict(res["layers_not_converging"])
        _words = str(_l.get("words") or
                     f"VMTK layers kept re-tangling ({int(_l.get('retangled') or 0)} rounds) - next engine")
        logger.warning("run_mesh: %s (%s)", _words, _l.get("next_engine"))
        PreflightRefusal(
            gate="finalize", cause=FailureCause.ENGINE_CRASHED.value,
            builder_text=f"[VMTK_LAYERS_NOT_CONVERGING] {_words}",
            facts={"layers_not_converging": True, "retangled": int(_l.get("retangled") or 0),
                   "meshers_started": 1}).write(workspace)
        return {"success": False, "layers_not_converging": _l, HANDS_OFF: True,
                "guidance": (f"Not meshed: {_words}. Do NOT run_mesh again and do NOT reconfigure - "
                             "end your turn now; the run moves on to the next engine, which builds the "
                             "layers the plan asks for."),
                "log_tail": res.get("log_tail", "")}
    if res["timed_out"] or _attempt_tripped(workspace):
        # THE RUN'S GUARD CLOCK TRIPPED - the run itself, or a step inside it (its stamp says
        # which). An infrastructure stop (TIME-3): the attempt ends and is replayed as it was.
        # a watched run's guard is the wall of the machine it ended on (RUN-5)
        from meshpipeline.contracts.native_time import wall_for
        return _guard_verdict(ctx, engine, int(wall_for(workspace)) if _watched else int(_cap),
                              _broken_at_time_out(_policy, res.get("log_tail")),
                              res.get("log_tail"), stalled=bool(res.get("stalled")))
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
    _rescue = q.get("rescue") if isinstance(q.get("rescue"), dict) else None
    if _rescue and _rescue.get("note"):
        # the mesh came out at its validity edge and the engine's own levers were tried beside it
        # (engines/cfmesh/rescue.py): the user hears what was done, in its words
        out["rescue_note"] = str(_rescue["note"])
        if ok:
            out["guidance"] = (f"{out.get('guidance') or ''} Tell the user, in these words: "
                               f"{_rescue['note']}").strip()
    if not ok and fatal and ctx.broken_mesh_moves_to:
        # A BROKEN MESH MOVES THE RUN ON (pipeline/engine_fallback.py): this engine's mesh of this
        # shape has fatal defects, and the run will move to the next engine, which delivers the
        # same approved mesh. Re-running this engine here would only spend the attempt's time
        # (cfMesh ran up to 18 minutes a try on the DrivAer cars and broke them every time).
        from meshpipeline.engines.registry import engine_label
        nxt = engine_label(ctx.broken_mesh_moves_to) or ctx.broken_mesh_moves_to
        out["moves_on_to"] = ctx.broken_mesh_moves_to
        out[HANDS_OFF] = True
        out["guidance"] = (f"Not valid ({', '.join(str(x) for x in fatal)}): this engine's mesh "
                           f"of this shape is broken. Do NOT run_mesh again and do NOT reconfigure "
                           f"- end your turn now. The run moves on to {nxt}, which builds the "
                           "same approved mesh.")
    out["log_tail"] = "" if ok else res["log_tail"]
    return out
