# Responsibility: Run the engine and evaluate its declared gates.
# Boundaries: execution and measurement; it forms no quality opinion beyond the declared bars and renders nothing.
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.settings.policy as polcfg
from meshpipeline.application.execution_publisher import execution_publisher
from meshpipeline.capture.logger import TrainingLogger
from meshpipeline.engines.runtime import get_engine

logger = logging.getLogger(__name__)

# PipelineState is the inter-node contract (defined in graph.py). Imported under
# TYPE_CHECKING only - annotations are strings here, so this adds typing/IDE support
# without a runtime import (which would cycle: graph imports these node modules).
if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


async def node_executor(state: PipelineState) -> dict:
    job_id         = state.get("job_id", "unknown")
    workspace      = state.get("openfoam_workspace", "")
    _domain        = state.get("domain", "")

    # FENCE - before native output is validated and gate results are ACCEPTED as this run's
    # evidence. A fenced worker's mesh must never satisfy the current generation's gates.
    from meshpipeline.application import execution_fence as _fence
    await _fence.assert_current_owner("executor gate acceptance")

    # THE execution publisher for this node. It runs inside the graph, under the claim
    # taken before the graph started, so every event it publishes is ownership-checked.
    _pub = execution_publisher(job_id, agent="executor")
    # This node runs once per builder retry, so WHICH retry is what makes one announcement
    # distinct from the next. A replay of the same retry re-derives the same number.
    _n = int(state.get("retry_count", 0) or 0)
    await _pub.astage(op_id=f"check:{_n}")
    await _pub.anote("Checking the mesh", op_id=f"check:{_n}")

    # The builder's deterministic input-contract PRE-FLIGHT already rejected the geometry as
    # physically unmeshable (e.g. a self-intersecting surface for a fill engine) - no mesh
    # was built. Surface that reason as THE failure instead of running finalize on an empty
    # workspace (which would report a generic "no mesh" and lose the real cause).
    _geom_reject = state.get("geometry_unsuitable_reason", "")
    if _geom_reject:
        logger.info("Executor: geometry rejected up front - job_id=%s: %s", job_id, _geom_reject)
        await _pub.acheck("Your geometry can be meshed by this engine", ok=False)
        return {
            "executor_success":     False,
            "executor_output":      _geom_reject,
            "executor_failed_gate": "geometry",
        }

    if not workspace:
        logger.warning("Executor: no workspace in state - job_id=%s", job_id)
        return {
            "executor_success": False,
            "executor_output":  "[EXECUTOR_ERROR] No workspace path in state",
        }

    # cfMesh-native: the Builder already built constant/polyMesh via run_mesh.
    # The executor validates it + writes the manifest/review-mesh; no meshing here.
        # Flow regime is the neutral `flow_topology` fact (derived from the purpose), NOT an
    # engine_param - engine_params stays engine-native only.
    _flow_topology = state.get("flow_topology", "") or ""
    _internal = _flow_topology == "internal"
    # internal jobs bypassing intake (programmatic submits) can arrive with no domain label;
    # give the reviewer a non-empty flow context rather than an empty field.
    _mfst_domain = _domain or ("internal flow" if _internal else "")
    # ENGINE-ADAPTER seam: each engine declares its own finalize (flow engines →
    # each flow bundle's finalize clone; gmsh → its own finalize).
    result = await asyncio.to_thread(
        get_engine(state.get("engine", "")).finalize, workspace,
        state.get("intake_patches", []) or [], state.get("engine", ""), _mfst_domain,
        _internal, state.get("engine_params", {}) or {}, _flow_topology)

    executor_success = result["success"]
    solvability_failed = False
    requirement_caveats: list = []
    contract_failed = False
    # WHICH rejection source spoke. The classifier looks the section up from this key
    # (a declared GateSpec, or one of the non-gate seams) instead of regexing the prose.
    failed_gate = "" if executor_success else "finalize"
    executor_output  = result.get("output", "")
    mesh_manifest: dict = {}
    solvability_metrics: dict = {}  # residual/iters/n_cells/info

    if executor_success:
        # The ENGINE-DECLARED blocking gates (spec.gates): the executor iterates
        # whatever the catalog row declares - no per-engine branches here. Crash
        # semantics (gate crash = SystemFailure, never a mesh rejection) live in
        # run_gates, written once.
        from meshpipeline.engines.gates import GateCtx, run_gates
        from meshpipeline.engines.registry import get_spec
        _ctx = GateCtx(
            workspace=Path(workspace),
            engine=state.get("engine", ""),
            domain=state.get("domain", ""),
            intake_patches=state.get("intake_patches", []) or [],
            engine_params=state.get("engine_params", {}) or {},
        )
        _spec = get_spec(state.get("engine", ""))
        _proves = {g.key: (g.proves or g.key) for g in _spec.gates}

        _gate_results: list[tuple[str, bool]] = []

        def _report_gate(key: str, ok: bool, _fb: str) -> None:
            # RECORDS what the gate proved; it does not publish. `run_gates` wraps this callback
            # in `except Exception`, which would swallow a lost claim, so publication happens
            # below where the refusal can propagate. The order is the gate order either way.
            _gate_results.append((key, ok))

        _gates_ok, _gate_key, _gate_feedback = run_gates(
            _spec.gates, _ctx, on_result=_report_gate)
        for _key, _ok in _gate_results:
            # Report what the gate PROVES, in the engineer's language - never the internal key.
            # This is a TRUST surface: the user is being shown the checks their mesh survived,
            # so it must read as evidence. The statement is ENGINE-declared (GateSpec.proves).
            await _pub.acheck(_proves.get(_key, _key), ok=_ok)
        if not _gates_ok:
            executor_success = False
            failed_gate = _gate_key
            contract_failed = (_gate_key == "patch_contract")
            executor_output += f"\n{_gate_feedback}"
            logger.warning(
                "Executor: gate %s REJECTED mesh - job_id=%s - %s",
                _gate_key, job_id, _gate_feedback.splitlines()[0][:200],
            )
            await _pub.acheck(_proves.get(_gate_key, _gate_key), ok=False)

        # Manifest for state/reviewer context - loaded whenever the manifest
        # itself was valid (a patch_contract rejection still ships the manifest
        # to the classifier, exactly as before).
        if _gates_ok or _gate_key != "manifest_valid":
            try:
                with open(Path(workspace) / "mesh_manifest.json") as _mf:
                    mesh_manifest = json.load(_mf)
            except Exception as exc:
                logger.error(
                    "Executor: mesh_manifest.json passed validation but failed to load - "
                    "reviewer will have no manifest context - job_id=%s: %s",
                    job_id, exc,
                )
                executor_output += "\n[MANIFEST_LOAD_ERROR] manifest validated but could not be loaded"

        if executor_success:
            _domain_check_failed = False
            if executor_success and not contract_failed and mesh_manifest and polcfg.DOMAIN_EXTENT_GATE_ENABLED:
                _typed_req = state.get("requested_extents")
                _typed_ruler = state.get("reference_length_m")
                if _typed_req and _typed_ruler:
                    # TYPED path (approved intent v5): measured with the APPROVED ruler,
                    # tri-state, one-sided, floored. A gate CRASH here records "unmeasured"
                    # rather than passing - an unmeasured requirement can neither pass nor
                    # be delivered with a caveat.
                    from meshpipeline.engines.domain_extent_gate import (
                        ExtentVerdict,
                        evaluate_domain_extents,
                    )
                    try:
                        _v = evaluate_domain_extents(_typed_req, _typed_ruler, mesh_manifest,
                                                     flow_axis=state.get("flow_axis"))
                    except Exception:  # noqa: BLE001
                        logger.exception("Executor: typed extent evaluation crashed - "
                                         "job_id=%s", job_id)
                        _v = ExtentVerdict(
                            "unmeasured", [],
                            "[DOMAIN_EXTENT_UNMEASURED] the extent evaluation itself failed - "
                            "an unmeasured requirement can neither pass nor carry a caveat")
                    if _v.status == "miss" and not state.get("requirements_strict"):
                        # a NEAR-miss under a non-strict approval: record the measured
                        # caveats and CONTINUE - solvability and every later check still
                        # run, and the ladder still gets its chance to fix the box (the
                        # classifier routes caveated attempts while retries remain).
                        # kind discriminator: this author site produces domain-extent
                        # caveats; stamping it keeps kind-less dicts a strictly legacy shape
                        requirement_caveats = [{**dict(c), "kind": "domain_extent"}
                                               for c in _v.caveats]
                        executor_output += f"\n{_v.detail}"
                        logger.warning(
                            "Executor: domain-extent NEAR-MISS (caveats recorded, run "
                            "continues) - job_id=%s - %s", job_id, _v.detail[:160])
                        _domain_check_failed = True
                    elif _v.status in ("miss", "block", "unmeasured"):
                        executor_success = False
                        failed_gate = "domain_extent"
                        contract_failed = True
                        executor_output += f"\n{_v.detail}"
                        logger.warning(
                            "Executor: domain-extent gate REJECTED mesh (%s) - job_id=%s - %s",
                            _v.status, job_id, _v.detail[:160])
                        _domain_check_failed = True
                else:
                    try:
                        # LEGACY prose path (pre-v5 approvals): the engine seam parses the
                        # request text; strict blocking, unchanged. Applicability is decided
                        # by the CASE/ARTIFACT - it no-ops unless the request declares
                        # far-field extents AND the manifest records a domain box.
                        _ext = get_engine(state.get("engine", "")).check_domain_extents(
                            state.get("request_txt", ""), mesh_manifest)
                        if _ext is not None and not _ext[0]:
                            executor_success = False
                            failed_gate = "domain_extent"
                            contract_failed = True
                            executor_output += f"\n{_ext[1]}"
                            logger.warning(
                                "Executor: domain-extent gate REJECTED mesh - job_id=%s - %s",
                                job_id, _ext[1][:160],
                            )
                            _domain_check_failed = True
                    except Exception as _de_exc:
                        # Fail closed on an unexpected gate error would risk false rejects on a
                        # best-effort check; log loudly and pass (extents are advisory-derived).
                        logger.exception("Executor: domain-extent gate errored (passing) - job_id=%s: %s", job_id, _de_exc)
            if _domain_check_failed:
                # ONE publication site for every path through the domain gate - the pipeline
                # execution closure counts sites, and truth does not need three copies
                await _pub.acheck("The far-field domain is the size you asked for", ok=False)

            if (
                executor_success
                and not contract_failed
                and mesh_manifest
                and polcfg.SOLVABILITY_GATE_ENABLED
            ):
                try:
                    # ENGINE-OWNED seam (like finalize/gates above): each engine
                    # declares its own solvability check. OpenFOAM engines assemble
                    # + solve the FV pressure-Poisson operator; gmsh returns None
                    # because its SICN>0 gate already IS its solvability guarantee.
                    _solv = get_engine(state.get("engine", "")).check_solvability
                    await _pub.anote("Trying a solver run on it", op_id=f"solvability:{_n}")
                    _result = await asyncio.to_thread(
                        _solv, Path(workspace), solvability_metrics,
                    )
                    if _result is None:
                        logger.info("Executor: no dedicated solvability stage for this "
                                    "engine (covered by its gates) - job_id=%s", job_id)
                    elif not _result[0]:
                        executor_success = False
                        failed_gate = "solvability"
                        solvability_failed = True
                        executor_output += f"\n{_result[1]}"
                        logger.warning(
                            "Executor: solvability gate REJECTED mesh - job_id=%s: %s",
                            job_id, _result[1][:200],
                        )
                        await _pub.acheck("The solver cannot run this mesh - the pressure "
                                   "equation does not solve", ok=False)
                    else:
                        logger.info("Executor: solvability gate PASSED - job_id=%s", job_id)
                        await _pub.acheck("A solver run on this mesh converges - the pressure "
                                   "equation assembles and solves", ok=True)
                except Exception as _sv_exc:
                    # A CRASH in the solvability check (checkMesh subprocess can't run,
                    # OOM in the AMG solve, etc.) is a SYSTEM failure, not a verdict on
                    # the mesh. Classify it (RESOURCE vs INTERNAL) and raise so it
                    # routes to the system-failure sink instead of rejecting the mesh.
                    from meshpipeline.errors import classify_exception
                    logger.exception("Executor: solvability gate CRASHED - job_id=%s", job_id)
                    raise classify_exception(_sv_exc, "solvability_gate")


    logger.info(
        "Executor: success=%s job_id=%s output_len=%d",
        executor_success, job_id, len(executor_output),
    )
    if executor_success:
        await _pub.anote("Every check passed - sending the mesh for review", op_id=f"outcome:{_n}")
    else:
        # The executor does NOT announce a rebuild - whether one happens is the ROUTER's
        # call (route_after_executor), not the executor's, and predicting it here printed
        # "Rebuilding - attempt 5 of 4" on the final try where the loop actually gives up.
        # If a retry does happen, the builder opens it with its own `attempt N of M`
        # event; if the loop is exhausted, the outcome turn explains the failure. Either
        # way the user already saw the failed CHECK above. (executor_output is the repair
        # instruction addressed to the builder, an LLM - deliberately never published.)
        await _pub.anote("The mesh did not pass its checks", op_id=f"outcome:{_n}")

    _attempt_log = ""
    if workspace:
        _log_path = Path(workspace) / "attempt_log.txt"
        if _log_path.exists():
            try:
                _attempt_log = _log_path.read_text(encoding="utf-8")
            except Exception:
                pass

    TrainingLogger(job_id).log("executor_run", op_id=f"executor:{_n}", payload={
        "success":    executor_success,
        "failed_gate": "" if executor_success else failed_gate,
        "output_len": len(executor_output),
        "workspace":  workspace,
        "executor_stdouts":      result.get("stdout", ""),
        "executor_stderrs":      result.get("stderr", ""),
        "attempt_log_snapshots": _attempt_log,
        "mesh_manifest":         mesh_manifest,
        "solvability_metrics":   solvability_metrics,
    })
    return {
        "executor_success":     executor_success,
        "executor_output":      executor_output,
        "executor_failed_gate": "" if executor_success else failed_gate,
        "mesh_manifest":        mesh_manifest,
        "solvability_failed":   solvability_failed,
        # machine-measured requirement near-misses for THIS attempt ([] when none): the
        # delivery surfaces state them and the selection machinery reads them; nothing
        # downstream may add to or waive them
        "requirement_caveats":  requirement_caveats,
    }
