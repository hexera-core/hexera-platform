# Responsibility: Define the single state object every LangGraph node reads from and writes to.
# Boundaries: a typed dict of the run's facts.
# Collaborates with: pipeline/data_contract.py, which declares each field's meaning and consumers.
from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from meshpipeline.contracts.geometry_source import GeometryState

# Bump whenever PipelineState's SHAPE changes (a field added/removed/repurposed). The
# worker namespaces each job's LangGraph checkpoint by this version
# (thread_id = "<job_id>:s<version>"), so a redelivery after a deploy that changed the
# schema starts FRESH instead of resuming an incompatible checkpoint - the single biggest
# production-incident source for agent systems (state management).
STATE_SCHEMA_VERSION = 9   # v9: `dispute_flag_findings` + `builder_flag_responses` - the
                           # typed per-flag round trip for a human dispute.
                           # v7: reviewer emits `reviewer_axis_findings` (canonical typed LIST,
                           # + `reviewer_rebuild_required`; executor records
                           # `executor_failed_gate`; classifier routes on these DECLARED signals.
                           # v8: `agent_run_records` - the canonical append-only accountability
                           # history, one sanitized record per agent invocation. There is no v7
                           # reader and no migration: a redelivery after this deploy finds no
                           # checkpoint at :s8 and starts fresh, which is the designed behaviour.


class PipelineState(TypedDict):
    schema_version: int   # stamped by the worker; see STATE_SCHEMA_VERSION
    engine:   str         # mesh engine chosen pre-job by node_engine_select
    messages: Annotated[list, add_messages]
    job_id:   str
    user_id:  str

    session_id:        str

    # The VERIFIED source for this run, as MaterializedGeometry.to_state(): the durable
    # reference plus the local path this process materialised it to. The path is an
    # execution handle, never identity - a resumed process re-materialises rather than
    # trusting a path serialised by whichever container ran the previous node.
    geometry: GeometryState

    domain:     str
    engine_params: dict   # engine-NATIVE declared params (ParamSpec answers; see data_contract)

    openfoam_workspace:  str

    executor_output:    str
    executor_success:   bool
    executor_failed_gate: str          # the DECLARED gate key that rejected (classifier routes on it)
    geometry_unsuitable_reason: str    # node_geometry_admission's reject reason (unmeshable input)
    flow_topology:      str            # "internal"/"external"/"" - DERIVED from purpose (neutral fact, NOT an engine_param)
    mesh_manifest:      dict
    reviewer_result:         str
    reviewer_verdict:        str
    reviewer_feedback:       str
    reviewer_patch_checks:   dict
    reviewer_axis_findings: list # canonical typed list: one entry per composed rubric axis
                                       # {axis_key, owner, passed, finding, evidence_ids, attempt}
    reviewer_rebuild_required: bool    # reviewer says the APPROACH is wrong -> rebuild, not retry
    reviewer_tool_calls: Annotated[list, operator.add]

    # CANONICAL ACCOUNTABILITY HISTORY (v8). One sanitized AgentRunRecord per agent invocation,
    # appended - never replaced. It is HISTORY: no reader may take a previous attempt's verdict,
    # progress, eligibility deficits, evidence delta or loop tally as the current ones. The
    # current attempt's facts come from the current invocation, which builds its state fresh.
    agent_run_records: Annotated[list, operator.add]

    classifier_result: dict
    retry_count:       int
    builder_mode:      str

    outcome_message: str

    solvability_failed: bool

    api_failure: str

    request_txt:      str
    review_brief_txt: str

    intake_patches:   list
    dimensionality:   str
    purpose:          str
    input_kind:       str
    # MESH DETAIL PREFERENCE (enums.MeshFidelity: draft|standard|max). Approved intent,
    # seeded by the application and never inferred from request_txt; no node may rewrite it.
    # `requested` is None when the user never stated one - omission is not a user selection.
    requested_mesh_fidelity: str | None
    effective_mesh_fidelity: str
    mesh_fidelity_source:    str
    # THE TYPED DOMAIN REQUEST (approved intent v5): per-direction far-field multiples, the
    # metre ruler they multiply, and whether near-misses may deliver with the miss stated.
    # Seeded by the application from the approval; no node may rewrite any of them - the
    # domain gate MEASURES against these, never against prose or a re-plan's opinion.
    requested_extents:  dict | None
    reference_length_m: float | None
    requirements_strict: bool
    # the DECLARED flow direction (+x/-x/+y/-y/+z/-z, None = never stated -> legacy
    # assume-X): orients the far-field box AND the extent measurement, so the wake room
    # can never again be built or checked on the wrong side of the part
    flow_axis: str | None
    # machine-measured requirement near-misses for the CURRENT attempt (executor-owned;
    # empty means fully conforming). Deterministic code authors these; no agent may.
    requirement_caveats: list

    builder_noop_count:     int
    # Aggregate Builder budget: epoch deadline covering ALL attempts of one run. Set once on the
    # first attempt, carried across retries, never reset - so the sum of attempts cannot exceed it.
    builder_deadline_epoch: float
    # top-level pipeline budget: the ABSOLUTE epoch deadline for the WHOLE job (from created_at +
    # PIPELINE_TOTAL_TIMEOUT_SECONDS). Seeded once by the application; every child stage caps its
    # budget at what remains; never reset by a retry/rebuild/generation. 0.0 = no cap (dev).
    pipeline_deadline_epoch: float
    # the durable EXECUTION GENERATION owning this run (0 in dev/degenerate). Side effects
    # namespace on it: the attempt workspace lives under generation_<g>/ so a different-generation
    # takeover never clobbers a prior generation's files, and delivered artifacts are bound to it.
    execution_generation: int

    agent_model_configs: dict

    # USER DISPUTE of a delivered mesh: {"flags": [{x,y,z,patch,note}...], "comment": str,
    # "of_job_id": str}. Empty dict = normal run. When set, the run starts by RE-REVIEWING
    # the parent job's delivered mesh at the flagged coordinates (targeted feedback), then
    # ALWAYS rebuilds, then re-reviews the new mesh against the flagged criteria PLUS all
    # original criteria.
    user_dispute: dict

    # THE HUMAN-FLAG ROUND TRIP, typed rather than narrated. `user_dispute` is what the human
    # said; these two are what each agent did about it, carried in the same durable state so the
    # SECOND review can compare all three instead of re-reading prose.
    #   dispute_flag_findings  - the FIRST review's per-flag baseline on the parent mesh
    #   builder_flag_responses - what the builder declares it changed for each flag
    # Both are lists of plain dicts (contracts/human_flags.py owns their shape), so a checkpoint
    # and a hosted reconstruction from dispatch_payload round-trip them unchanged.
    dispute_flag_findings: list
    builder_flag_responses: list
