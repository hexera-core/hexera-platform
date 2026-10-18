# Responsibility: Build the initial pipeline state for a run, with every declared field present.
# Boundaries: construction against the data contract; a field the contract does not declare cannot be introduced here.
from __future__ import annotations

from collections.abc import Awaitable, Callable

from meshpipeline.contracts.geometry_source import GeometryState


def make_pipeline_state(
    job_id: str,
    user_id: str,
    *,
    session_id: str = "",
    messages: list | None = None,
    # GeometryState is the TypedDict that DOCUMENTS this shape (contracts/geometry_source); a
    # bare `dict` accepted anything a caller happened to build. Widened to the union so the
    # typed value MaterializedGeometry.to_state() produces is what the annotation expects.
    geometry: GeometryState | dict | None = None,
    request_txt: str = "",
    review_brief_txt: str = "",
    domain: str = "",
    engine: str = "",
    engine_params: dict | None = None,
    intake_gate: dict | None = None,
    intake_patches: list | None = None,
    dimensionality: str = "",
    purpose: str = "",
    input_kind: str = "",
    requested_mesh_fidelity: str | None = None,
    effective_mesh_fidelity: str = "",
    mesh_fidelity_source: str = "",
    requested_extents: dict | None = None,
    reference_length_m: float | None = None,
    requirements_strict: bool = True,
    flow_axis: str | None = None,
    agent_model_configs: dict | None = None,
    user_dispute: dict | None = None,
) -> dict:
    # The chosen mesh engine is the canonical PipelineState field `engine` (intake's tool/session
    # interface calls it `mesh_engine`; there is no third spelling). Carrying it here - plus the
    # rest of the standing validated intake fields - is what lets node_intake reconstruct the
    # STANDING submission on the confirmation turn (see api/v1/chat.py::_build_intake_state).
    # engine_source is deliberately NOT carried: the data contract records it as capture-only
    # (rides the intake_complete event; no session/state carrier), and the confirmation logic
    # never consumes it.
    return {
        "messages":              messages if messages is not None else [],
        "job_id":                job_id,
        "user_id":               user_id,
        "session_id":            session_id,
        "geometry":              dict(geometry or {}),
        "domain":                domain,
        "engine":                engine,
        # who chose it (the application seeds the real value) and the fallback ladder's record;
        # an unknown source is never switched on its own
        "engine_source":         "",
        "engine_ladder":         {},
        "engine_params":         engine_params if engine_params is not None else {},
        # intake_gate: {"selection": …, "admission": …} - the confirmed engine selection and the
        # admission preview-TOKEN it backs (intake-only working key, not a graph PipelineState
        # field). The server-issued authorization consumed by submit_requirements/confirm_dispatch
        # (see agents.intake.engine_selection and agents.intake.admission_token).
        "intake_gate":           intake_gate if intake_gate is not None else {},
        "openfoam_workspace":    "",
        "executor_output":       "",
        "executor_success":      False,
        "executor_failed_gate":  "",
        "executor_failure_cause": "",
        "executor_failure_facts": {},
        "mesh_manifest":         {},
        "reviewer_result":       "",
        "reviewer_verdict":      "",
        "reviewer_feedback":     "",
        "reviewer_patch_checks":     {},
        "reviewer_axis_findings":    [],
        "reviewer_rebuild_required": False,
        "reviewer_tool_calls":   [],
        "classifier_result":     {},
        "retry_count":           0,
        "builder_mode":          "initial",
        "outcome_message":       "",
        "api_failure":           "",
        "request_txt":           request_txt,
        "review_brief_txt":      review_brief_txt,
        "intake_patches":        intake_patches if intake_patches is not None else [],
        "dimensionality":        dimensionality,
        "purpose":               purpose,
        "input_kind":            input_kind,
        "requested_extents":     dict(requested_extents) if requested_extents else None,
        "reference_length_m":    reference_length_m,
        "requirements_strict":   bool(requirements_strict),
        "flow_axis":             flow_axis,
        "requirement_caveats":   [],
        # mesh-detail preference: the user's own choice (None = never stated), the
        # deterministic operational tier, and which of the two produced it.
        "requested_mesh_fidelity": requested_mesh_fidelity,
        "effective_mesh_fidelity": effective_mesh_fidelity,
        "mesh_fidelity_source":    mesh_fidelity_source,
        "builder_noop_count":    0,
        "builder_stop":          "",
        "review_history":        [],
        "agent_model_configs":   agent_model_configs if agent_model_configs is not None else {},
        "user_dispute":          user_dispute if user_dispute is not None else {},
        # Empty until the phase that owns each one runs; never seeded from a caller, so a
        # dispute-of-a-dispute cannot start life holding an older revision's results.
        "dispute_flag_findings":  [],
        "builder_flag_responses": [],
    }


# #
# THE USER'S ENGINE IS THE RUN'S ENGINE.
# Extracted from application/pipeline_run._run_async. The product invariant:
#     An engine the USER named stays authoritative for the whole run and every continuation. The
#     pipeline never recommends, substitutes, defaults or infers one behind the user's back.
# Two halves enforce it. This one PINS the selection into the run state; node_engine_select honours
# the pin and hard-fails on a name no spec matches (an unknown pin once meant validating the mesh
# against another engine's gates). Only when nothing is pinned does selection derive a candidate
# from the declared purpose - which is why the pin is written before the graph is ever built.
# A dispute run deliberately overwrites this afterwards with the PARENT's engine: a rebuild must
# use the mesher the delivered mesh came from. That ordering is the caller's and is asserted there.
# The one exception is the fallback ladder (pipeline/engine_fallback.py), and it is narrow: an
# engine the intake PROPOSED and the user confirmed may be replaced mid-run by another that
# delivers the SAME approved mesh - said in the run's stream and its closing message, never behind
# anyone's back. A user-named engine, a dispute's engine, or one whose provenance is unknown is
# never replaced; its failure ends in an offer the user accepts in one sentence.
# #


def engine_provenance(intake_events, pinned_engine: str, *, dispute: bool = False) -> str:
    """WHO CHOSE THE ENGINE this run starts with: user_direct | suggested_confirmed | dispute |
    system.

    The approval dispatches it as an `engine_provenance` event appended to intake_events
    (agents/intake/approval.engine_provenance_event), in every data-collection mode; a dispatch
    written before that event existed still carries it on the approved submission's
    intake_complete event when collection was on. The LAST such event is the approved one: every
    approval is of the latest submission. Anything that cannot be read with certainty - no event,
    an event for another engine, an unknown word - is the user's choice, because that is the
    reading that never switches an engine on a guess."""
    if dispute:
        return "dispute"
    if not pinned_engine:
        return "system"
    for ev in reversed(list(intake_events or [])):
        if not isinstance(ev, dict) or ev.get("type") not in ("engine_provenance",
                                                               "intake_complete"):
            continue
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            break
        source = str(payload.get("engine_source") or "").strip().lower()
        same = str(payload.get("mesh_engine") or "").strip().lower() == pinned_engine
        if same and source in ("user_direct", "suggested_confirmed"):
            return source
        break
    return "user_direct"


async def pin_selected_engine(state, mesh_engine: str, *, jlog,
                              publish: Callable[[str], Awaitable[None]]) -> str:
    if not mesh_engine:
        return ""
    from meshpipeline.engines.registry import engine_names

    if mesh_engine not in engine_names():
        jlog.warning("mesh_engine %r not in catalog - resolving deterministically", mesh_engine)
        return ""
    state["engine"] = mesh_engine
    await publish(mesh_engine)
    return mesh_engine
