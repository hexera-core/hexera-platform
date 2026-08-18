# Responsibility: Declare every pipeline state field: what it means, who writes it and who reads it.
# Boundaries: the registry a node may not bypass - a field not declared here may not be added to the state.
# Collaborates with: contracts/pipeline_state.py, which is the typed shape this describes.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContractVar:
    concept: str                       # what this IS, boundary-independent
    purpose: str                       # original intent, in one sentence
    intake_field: str | None = None    # key in intake's submit/result payload
    session_column: str | None = None  # chat_sessions column
    dispatch_kwarg: str | None = None  # run_simulation kwarg / JobRequest slot
    state_field: str | None = None     # PipelineState field
    corpus: str = ""                   # "" = not captured; else WHY it earns
                                       # corpus space (training | qa)
    alias_reason: str = ""             # required iff names differ across layers


CONTRACT: tuple[ContractVar, ...] = (
    ContractVar(
        concept="job identity",
        purpose="the run's primary key across API, worker, state and corpus",
        dispatch_kwarg="job_id", state_field="job_id",
        corpus="qa: every event/sample is keyed to it",
    ),
    ContractVar(
        concept="owner identity",
        purpose="tenant scoping for quotas, artifacts and viewer access",
        dispatch_kwarg="owner_id", state_field="user_id",
        alias_reason="state predates multi-tenant naming; rename = state schema bump (deferred)",
    ),
    ContractVar(
        concept="session identity",
        purpose="links the job back to its intake conversation",
        dispatch_kwarg="session_id", state_field="session_id",
    ),
    ContractVar(
        concept="geometry unit interpretation",
        purpose="what one coordinate unit of the uploaded source means physically, confirmed by "
                "the user or verified from the file, and immutable once a job is approved under it",
        session_column="geometry_interpretation_id",
        dispatch_kwarg="geometry_interpretation",
        state_field="geometry",
        corpus="training: unit, factor and basis are recorded; no storage identity is",
        alias_reason="separate from the source because it answers a different question with a "
                     "different lifetime: the source says WHICH BYTES, this says WHAT SIZE. The "
                     "same bytes can legitimately be meshed as millimetres by one user and metres "
                     "by another, so the unit is not a property of the file",
    ),
    ContractVar(
        concept="input geometry",
        purpose="the immutable uploaded source: identified by content, materialised and "
                "verified into the executing process's own workspace",
        session_column="geometry_source_id", dispatch_kwarg="geometry_source",
        state_field="geometry",
        corpus="training: the source identity is recorded; the local path is ephemeral",
        alias_reason="the three names hold three different things and must not be unified: the "
                     "session stores a foreign key, dispatch carries the complete approved "
                     "snapshot so drift is detectable without a database, and state holds a "
                     "handle to bytes verified in THIS process. One name would imply they are "
                     "interchangeable, which is precisely the confusion the cutover removed",
    ),
    ContractVar(
        concept="user requirements",
        purpose="intake's complete natural-language requirement summary - the "
                "builder's brief",
        intake_field="request_txt", session_column="request_txt",
        dispatch_kwarg="request_txt", state_field="request_txt",
        corpus="training: the prompt side of every agent trajectory",
    ),
    ContractVar(
        concept="acceptance criteria",
        purpose="intake's qualitative brief the reviewer judges against",
        intake_field="review_brief_txt", session_column="review_brief_txt",
        dispatch_kwarg="review_brief_txt", state_field="review_brief_txt",
        corpus="training: the reviewer's evaluation target",
    ),
    ContractVar(
        concept="patch contract",
        purpose="user-agreed boundary patches {name,type} the mesh must declare",
        intake_field="patches", session_column="intake_patches",
        dispatch_kwarg="intake_patches", state_field="intake_patches",
        corpus="qa: contract the delivered mesh is validated against",
        alias_reason="intake tool arg kept terse for the model; every layer "
                     "after intake uses intake_patches",
    ),
    ContractVar(
        concept="dimensionality",
        purpose="2D / 3D geometric character, validated at intake",
        intake_field="dimensionality", session_column="dimensionality",
        dispatch_kwarg="dimensionality", state_field="dimensionality",
        corpus="training: routing/plan conditioning input",
    ),
    ContractVar(
        concept="mesh fidelity (approved)",
        purpose="the TYPED resolution target the user explicitly approved (draft/standard/max, "
                "enums.MeshFidelity). Approved INTENT in its own right - declared at intake, never "
                "inferred from request_txt prose, and bound into the approved-intent fingerprint so "
                "it cannot drift between approval and execution. It is a resolution TARGET, not a "
                "cell count: absolute sizing stays automatic and geometry-aware, and the cell budget "
                "remains a separate feasibility ceiling",
        intake_field="mesh_fidelity", session_column="requested_mesh_fidelity",
        dispatch_kwarg="requested_mesh_fidelity", state_field="requested_mesh_fidelity",
        corpus="training: routing/plan conditioning input",
        alias_reason="the intake tool arg stays `mesh_fidelity` (what the model is asked for); "
                     "every layer after intake distinguishes the user's REQUESTED tier from the "
                     "derived EFFECTIVE one, so the name gains the `requested_` qualifier",
    ),
    ContractVar(
        concept="mesh fidelity (effective)",
        purpose="the DETERMINISTIC operational mesh-detail tier actually used (draft/standard/max). "
                "Equal to the requested tier when the user chose one, otherwise the declared system "
                "default. A SOFT, engine-owned authoring input: it shifts each engine's own "
                "recommendation and nothing else - never a gate, a hard limit, a required artifact, "
                "a Reviewer criterion, or a promised cell count",
        dispatch_kwarg="effective_mesh_fidelity", state_field="effective_mesh_fidelity",
        corpus="training: authoring-recommendation conditioning input",
    ),
    ContractVar(
        concept="mesh fidelity (provenance)",
        purpose="user | default - WHICH of the two produced the effective tier. Recording it is what "
                "stops a deterministic system default being presented to the user, or stored in the "
                "approved intent, as a choice they made",
        dispatch_kwarg="mesh_fidelity_source", state_field="mesh_fidelity_source",
        corpus="qa: distinguishes user intent from a system default; not a training signal",
    ),
    ContractVar(
        concept="mesh fidelity policy version",
        purpose="the version of the three-tier default policy in force when the job was "
                "approved. Fingerprinted, so changing the default tier later "
                "cannot silently re-interpret an already-approved job - the recomputed intent stops "
                "matching and the run is refused",
        dispatch_kwarg="fidelity_policy_version",
        corpus="qa: pins the approval-time policy; not a training signal",
    ),
    ContractVar(
        concept="purpose (use case)",
        purpose="the user-declared USE CASE (structural / external_cfd / "
                "internal_cfd) - drives the boundary-role vocabulary, the "
                "compatibility gate and downstream framing; NEVER inferred from "
                "the engine (engines are physical tools)",
        intake_field="purpose", session_column="purpose",
        dispatch_kwarg="purpose", state_field="purpose",
        corpus="training: routing/plan conditioning input",
    ),
    ContractVar(
        concept="submitted geometry kind",
        purpose="what the submitted CAD represents (solid-body / fluid-domain / "
                "body-surface) - feeds the input-aware capability gate and framing",
        intake_field="input_kind", session_column="input_kind",
        dispatch_kwarg="input_kind", state_field="input_kind",
        corpus="training: routing/plan conditioning input",
    ),
    ContractVar(
        concept="task label (descriptive)",
        purpose="intake's plain-English label of geometry+task ('elbow internal "
                "flow') - the reviewer's task context (via the manifest) and the "
                "run's corpus label; NOTHING routes on it",
        intake_field="domain", session_column="domain",
        dispatch_kwarg="domain", state_field="domain",
        corpus="training: the human-readable task label of the run",
    ),
    ContractVar(
        concept="engine parameters (declared)",
        purpose="the chosen engine's OWN intake questions, answered by the "
                "user (ParamSpec keys, e.g. snappy topology) - ONE dict at "
                "every layer, never a new column per question; required at "
                "intake, defaulted at direct dispatch",
        intake_field="engine_params", session_column="engine_params",
        dispatch_kwarg="engine_params", state_field="engine_params",
        corpus="training+qa: the engine-native task declaration of the run",
    ),
    ContractVar(
        concept="intake authorization gate (engine selection + admission token)",
        purpose="the intake AUTHORIZATION gate: the confirmed engine SELECTION (no_selection → "
                "proposed_selection → confirmed_selection) plus the admission preview TOKEN it "
                "backs (canonical previewed payload + verdict + user-message revision + expiry). "
                "It STRUCTURALLY authorizes submit_requirements/confirm_dispatch - model tool "
                "arguments never do. Intake-only; not carried into the graph or dispatched - "
                "the approved snapshot is",
        session_column="intake_gate",
        corpus="qa: enforces immutable user intent at the submit/confirm boundary; not a training signal",
    ),
    ContractVar(
        concept="engine choice (user)",
        purpose="the engine the USER named or explicitly confirmed at intake - "
                "a direct user input, never inferred silently",
        intake_field="mesh_engine", session_column="mesh_engine",
        dispatch_kwarg="mesh_engine",
        corpus="training+qa: which engine the user chose",
    ),
    ContractVar(
        concept="engine choice provenance",
        purpose="whether the engine was user_direct or suggested_confirmed - "
                "audits that inferred choices are proposed, never applied",
        intake_field="engine_source",
        corpus="qa: verifies the propose-and-confirm contract; training signal "
               "for intake's fallback behaviour. Capture-only: rides the "
               "intake_complete event payload, no session/state carrier needed",
    ),
    ContractVar(
        concept="engine (resolved)",
        purpose="the engine that actually RUNS - resolution of dispute pin > "
                "user choice > declared-capability lookup > default; keys the "
                "builder prompt/tools/runner/validator downstream",
        state_field="engine",
        corpus="training+qa: engine_select_run event records chosen + source; "
               "record.json/labels.json carry it per sample",
    ),
    ContractVar(
        concept="user dispute",
        purpose="{flags, comment, of_job_id} when the user disputes a delivered "
                "mesh; triggers re-review-then-rebuild with the parent engine",
        dispatch_kwarg="user_dispute", state_field="user_dispute",
        corpus="training+qa: dispute_context event + reviewer's targeted brief",
    ),
    ContractVar(
        concept="human flag baseline findings",
        purpose="the FIRST review's per-flag determination on the parent mesh - one typed "
                "{ordinal, status, observation, explanation, measurements} per flag the user "
                "raised. Carried so the post-rebuild review compares against a recorded "
                "baseline rather than re-reading prose. Empty on a normal run",
        state_field="dispute_flag_findings",
        corpus="qa: the reviewer's own baseline per human flag",
    ),
    ContractVar(
        concept="builder flag responses",
        purpose="what the builder DECLARES it changed for each flag - {ordinal, "
                "intended_correction, change_made, affected_region, believed_addressed}. A "
                "claim, never proof: the post-rebuild review measures the result itself. "
                "Empty on a normal run",
        state_field="builder_flag_responses",
        corpus="qa: the builder's declared response per human flag",
    ),
    ContractVar(
        concept="intake event replay",
        purpose="typed intake events buffered in the API (no job exists during "
                "intake) and emitted into events.jsonl at dispatch",
        session_column="llm_metadata", dispatch_kwarg="intake_events",
        corpus="training: the intake conversation is the intake model's trajectory",
        alias_reason="column stores raw per-turn metadata; the kwarg carries "
                     "the typed subset for emission",
    ),
    ContractVar(
        concept="training sample identity",
        purpose="pre-allocated corpus sample dir for this run",
        dispatch_kwarg="sample_id",
        corpus="qa: binds job to sample; .job_id sentinel enables re-delivery reuse",
    ),
    ContractVar(
        concept="approved intake snapshot id",
        purpose="DIAGNOSTIC PROVENANCE: ties this execution to the canonical intake snapshot the "
                "user approved (agents.intake.approval). It authorizes nothing - owner/session "
                "binding was enforced at approval - carries no secret, and never influences engine "
                "behaviour. Empty for runs dispatched before the approval snapshot existed and for "
                "dispute re-runs, which inherit the parent's approved requirements",
        dispatch_kwarg="approved_snapshot_id",
        corpus="qa: joins a run to the exact user-approved configuration; not a training signal",
    ),
    ContractVar(
        concept="approved patch contract (typed, fingerprinted)",
        purpose="THE authoritative, immutable, fingerprinted set of boundaries the user approved "
                "(application.approved_patch_contract.ApprovedPatchContract). Built once at confirm "
                "time from the approval snapshot's own patches and carried verbatim through "
                "reconstruction, so the EXACT set - not merely 'some patches' - is verified at the "
                "dispatch contract and again at graph admission, before the builder. A missing, "
                "added, renamed, duplicated, merged, or re-roled boundary fails there; the engine "
                "gate then proves the delivered mesh realised it. None for a direct/internal "
                "dispatch. Never re-derived from prose, builder output, manifests, or reviewer text",
        dispatch_kwarg="approved_patch_contract",
        corpus="qa: the exact user-approved boundary contract for a run; not a training signal",
    ),
    ContractVar(
        concept="approved intent fingerprint (O-2)",
        purpose="sha256 of the COMPLETE approved execution intent - engine, purpose, input_kind, "
                "dimensionality, patches, engine_params - in the ONE canonical form the approval "
                "snapshot is fingerprinted on (agents.intake.admission_token). Set once at confirm "
                "time from the durable approved snapshot's fingerprint, carried through "
                "reconstruction into the JobRequest, and re-verified against the run's own fields at "
                "the dispatch contract and again at graph admission - so no run-determining field can "
                "drift from the approval, not just its patch subset. Empty for a direct/internal "
                "dispatch. Never re-derived from prose or manifests",
        dispatch_kwarg="approved_intent_fingerprint",
        corpus="qa: binds a run to the exact approved intent; not a training signal",
    ),
)


# corpus event registry: every event type + which corpus purpose it serves
EVENT_TYPES: dict[str, str] = {
    "intake_turn":        "training: one intake conversation turn",
    "intake_complete":    "training: intake's full structured handoff (incl. "
                          "engine_source provenance)",
    "web_search":         "training+qa: the search sub-agent's query, provider, "
                          "distilled answer and usage",
    "engine_select_run":  "qa: which engine ran and WHY (user/dispute/internal/"
                          "default) - deterministic, no model",
    "geometry_admission": "qa: input-contract admission verdict - when an engine's "
                          "measured input contract (e.g. self-intersection) rejected the "
                          "geometry before any build; deterministic, no model",
    "planner_run":        "training: the planner LLM's system, input, raw "
                          "response, parsed plan and usage",
    "builder_attempt":    "training: the builder's full trajectory per attempt",
    "builder_truncation_recovery":
                          "qa: context-pruning interventions during a build",
    "executor_run":       "qa: ground-truth mesh outcome (success, quality, logs)",
    "classifier_run":     "training: failure-classification input/output",
    "reviewer_run":       "training: the reviewer's visual trajectory + verdict",
    "final_result_built": "qa: the durable final_result facts + the message "
                          "rendered from them (deterministic, no model call)",
    "dispute_context":    "qa: provenance of dispute rebuilds (parent, flags)",
}


# PipelineState intent registry: every field, its purpose. The suite parses
# graph.PipelineState and requires EXACT name agreement - a field added there
# without a registered purpose (or vice versa) fails. Supersedes count-only
# drift tripwires with named intent.
STATE_FIELDS: dict[str, str] = {
    "schema_version":        "state schema version stamped by the worker (STATE_SCHEMA_VERSION)",
    "engine":                "the RESOLVED engine that runs (see 'engine (resolved)' above)",
    "messages":              "LangGraph message accumulator for the intake node",
    "job_id":                "job identity (see contract)",
    "user_id":               "owner identity (see contract; alias of owner_id)",
    "session_id":            "session identity (see contract)",
    "geometry":              "verified input geometry handle (see contract)",
    "domain":                "descriptive task label (see contract)",
    "engine_params":         "engine-native declared parameters (see contract)",
    "openfoam_workspace":    "the attempt workspace directory the builder/executor share",
    "executor_failed_gate":  "the declared gate key that rejected the mesh (drives classification)",
    "geometry_unsuitable_reason": "node_geometry_admission's reject reason: the input surface is "
                                  "unmeshable for the engine (e.g. self-intersecting) - routes to "
                                  "the executor short-circuit and the outcome turn",
    "flow_topology":         "internal/external flow regime DERIVED from the purpose - a neutral "
                             "fact read by the executor/runners, NOT an engine-native param",
    "executor_output":       "executor stdout/diagnostics for the classifier and reviewer",
    "executor_success":      "ground-truth mesh gate: did the executor validate a mesh",
    "mesh_manifest":         "the engine's manifest of the built mesh (reviewer navigation, quality)",
    "reviewer_result":       "reviewer raw result blob",
    "reviewer_verdict":      "PASS/FAIL verdict driving delivery vs retry",
    "reviewer_feedback":     "reviewer's targeted feedback for the next attempt",
    "reviewer_patch_checks": "reviewer per-patch check results",
    "reviewer_axis_findings": "canonical typed LIST (F-8): one entry per composed rubric axis {axis_key, owner, passed, finding, evidence_ids, attempt}; classifier extracts failing axes where passed is False",
    "reviewer_rebuild_required": "reviewer judged the approach wrong -> builder rebuilds from scratch",
    "reviewer_tool_calls":   "accumulated reviewer tool-call records (capture + limits)",
    "agent_run_records":     "append-only canonical agent accountability history; one "
                             "sanitized run record per agent invocation, never read as "
                             "the CURRENT attempt's verdict, progress or deficits",
    "classifier_result":     "failure classification driving the retry brief",
    "retry_count":           "attempt counter for retry budget and capture grouping",
    "builder_mode":          "initial vs retry build behaviour switch",
    "outcome_message":       "application pre-composed user-facing message (domain-gate "
                             "rejection / blameless system-failure note); never model prose",
    "solvability_failed":    "cheap solvability gate outcome (deterministic guardrail)",
    "api_failure":           "provider failure marker for honest failure taxonomy",
    "request_txt":           "user requirements (see contract)",
    "review_brief_txt":      "acceptance criteria (see contract)",
    "intake_patches":        "patch contract (see contract)",
    "dimensionality":        "dimensionality (see contract)",
    "purpose":               "purpose / use case (see contract)",
    "input_kind":            "submitted geometry kind (see contract)",
    "requested_mesh_fidelity": "the mesh-detail tier the USER chose (draft/standard/max), or None when they never stated one - omission is never a user selection",
    "effective_mesh_fidelity": "the deterministic operational tier actually used (draft/standard/max); soft engine-owned authoring input only, never a gate or a cell target",
    "mesh_fidelity_source":    "user | default - which of the two produced the effective tier, so a system default is never presented as a user choice",
    "builder_noop_count":    "no-op builder rounds counter (retry-storm guard)",
    "builder_deadline_epoch": "aggregate Builder wall-clock deadline (epoch) across ALL attempts of one run (B-4); set once, carried, never reset",
    "pipeline_deadline_epoch": "TOP-LEVEL pipeline wall-clock deadline (epoch) for the WHOLE job (O-4, from the durable pipeline_deadline_at anchored once at execution start); child budgets cap at what remains; never reset",
    "execution_generation":  "O-3 durable execution generation owning this run; side effects namespace on it (workspace generation_<g>/, artifact binding) so a superseded generation never clobbers the current one",
    "agent_model_configs":   "model/temperature provenance per agent for the corpus record",
    "user_dispute":          "user dispute (see contract)",
    "dispute_flag_findings":  "first-review per-flag baseline (see contract)",
    "builder_flag_responses": "builder per-flag declared response (see contract)",
}


# mesh-manifest key registry: the schema-2.1 contract written ONCE by
# openfoam/manifest.write_manifest and consumed by the executor (validation),
# reviewer (context + navigation), viewer endpoints, worker (dispute seeding)
# and exporter. Dot-paths; a trailing ".*" marks a dynamic mapping keyed by
# patch/criterion names. The suite enforces BOTH directions: every emitted
# path is registered, and every key any consumer reads must be one the writer
# actually emits - ghost reads and dead registry rows both fail.
MANIFEST_KEYS: dict[str, str] = {
    "schema_version":        "manifest schema version (2.1)",
    "mesh_mode":             "engine that built the mesh (dispute rebuilds pin to it)",
    "mesh_written":          "polyMesh owner file exists - the mesh physically exists",
    "cell_count":            "executor-measured cell total (executor-only concept)",
    "mesh_units":            "length unit for every coordinate in the manifest/viewer",
    "engine_params":         "declared engine-native params the mesh was built under",
    "engine_params.*":       "one entry per ParamSpec key of the engine",
    "flow_topology":         "internal/external flow regime the mesh was built for (neutral "
                             "purpose-derived fact; dispute rebuilds recover it from here)",
    "domain":                "declared domain label at build time (reviewer context)",
    "quality_criteria":      "evidence-backed report card (see children)",
    "quality_criteria.engine": "engine whose criteria registry was applied",
    "quality_criteria.production_grade": "all gating criteria passed (None = not evaluable)",
    "quality_criteria.criteria": "per-criterion rows: key/label/gating/ok_when/"
                                 "measured/passed/rationale/evidence_url",
    "geometry":              "measured + requested extents (see children)",
    "geometry.box_xmin":     "actual meshed extent (octree-padded) - info/reference",
    "geometry.box_xmax":     "actual meshed extent",
    "geometry.box_ymin":     "actual meshed extent",
    "geometry.box_ymax":     "actual meshed extent",
    "geometry.box_zmin":     "actual meshed extent",
    "geometry.box_zmax":     "actual meshed extent",
    "geometry.domain_box":   "REQUESTED far-field box - what the A1 extent gate measures",
    "geometry.body_box":     "body bounding box - reviewer scale guidance + viewer",
    "geometry.chord":        "streamwise body extent - unit of 'Nc upstream' requests",
    "patches":               "patch → entity list (engine bookkeeping)",
    "patches.*":             "entities per patch name",
    "patch_types":           "patch → role (wall/inlet/outlet/farfield/symmetry/empty)",
    "patch_types.*":         "role per patch name",
    "patch_face_counts":     "patch → boundary face count (contract validation)",
    "patch_face_counts.*":   "face count per patch name",
    "quality":               "engine quality probe (opaque engine dict: fatal, "
                             "skew_fraction, max_non_ortho, layer_coverage, cells, regions…)",
    "quality.*":             "engine-specific quality metrics",
    "mesh_paths":            "deliverable file paths (see children)",
    "mesh_paths.surface":    "surface preview mesh path",
    "mesh_paths.volume":     "volume mesh path (foamToVTK export)",
    "inspection_regions":    "system-derived slice/nearwall windows the reviewer steps through",
    "inspection_regions.name":     "region identifier the reviewer calls inspect_region with",
    "inspection_regions.kind":     "slice | nearwall",
    "inspection_regions.normal":   "slice plane normal",
    "inspection_regions.origin":   "slice origin (body centre)",
    "inspection_regions.clip_box": "clip extents framing the region",
    "validation":            "uniform structural-validity summary (see children)",
    "validation.has_wall":   "a wall patch exists",
    "validation.has_inflow": "inlet or farfield exists",
    "validation.has_outflow": "outlet or farfield exists",
    "validation.patch_validation": "patch → has faces (dynamic per patch name)",
    "validation.patch_validation.*": "per-patch face presence",
    "validation.body_has_faces": "the wall/body actually has boundary faces",
    "validation.volume_exists":  "polyMesh owner exists (volume mesh present)",
    "validation.warnings":   "non-fatal validity notes",
}


def dispatch_kwargs() -> set[str]:
    return {v.dispatch_kwarg for v in CONTRACT if v.dispatch_kwarg}


def session_columns() -> set[str]:
    return {v.session_column for v in CONTRACT if v.session_column}
