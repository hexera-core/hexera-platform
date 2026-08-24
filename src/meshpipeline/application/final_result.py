# Responsibility: Decide a run's one terminal status and the truthful public account of it.
# Owns: the terminal-status and failure-category vocabularies, the merge of durable facts, and the user-facing message.
# Boundaries: it reports.
from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from meshpipeline.application.artifact_policy import required_output_classes
from meshpipeline.contracts.review_outcome import ReviewExecution, ReviewVerdict

# v5: `requirement_caveats` - machine-measured requirement near-misses delivered WITH the
# mesh (empty on every fully conforming run). v4 records predate the field and are read in a
# DOCUMENTED compat window with caveats=[] - which is true by construction, because a v4 run
# either conformed fully or was refused outright.
FINAL_RESULT_SCHEMA_VERSION = 5
_READABLE_SCHEMA_VERSIONS = frozenset({4, 5})


class TerminalStatus(str, enum.Enum):
    succeeded = "succeeded"
    failed = "failed"
    # CANCELLATION IS CURRENTLY UNSUPPORTED. There is no JobStatus.cancelled, no cancel endpoint,
    # no cooperative-cancellation path, and nothing emits a cancelled outcome - so the taxonomy below
    # carries NO `cancelled` category (its presence implied a capability that does not exist).
    # A future cooperative-cancellation feature is a separately-scoped design, not incidental residue.
    # `timed_out` IS a real, reachable failure (the top-level pipeline deadline,: a run that
    # exhausts its logical budget fails with the `timed_out` category - it is not a distinct status.


class FailureCategory(str, enum.Enum):
    incompatible_requirements = "incompatible_requirements"
    authoring_failed = "authoring_failed"
    native_execution_failed = "native_execution_failed"
    required_output_missing = "required_output_missing"
    gate_failed = "gate_failed"
    review_rejected = "review_rejected"
    attempts_exhausted = "attempts_exhausted"
    delivery_failed = "delivery_failed"
    timed_out = "timed_out" # top-level pipeline-deadline exhaustion
    internal_pipeline_failure = "internal_pipeline_failure"
    # NOTE: no `cancelled` - cancellation is currently unsupported (see TerminalStatus).


# category → (headline, whether a mesh deliverable MAY exist, retry-sensible, user-change-needed)
_CATEGORY_META: dict[FailureCategory, tuple[str, bool, bool, bool]] = {
    FailureCategory.incompatible_requirements: (
        "The requested setup cannot be meshed as specified.", False, False, True),
    FailureCategory.authoring_failed: (
        "The mesh specification could not be prepared.", False, True, False),
    FailureCategory.native_execution_failed: (
        "The mesh could not be generated for this geometry.", False, True, False),
    FailureCategory.required_output_missing: (
        "The mesh run finished without producing the required output.", False, True, False),
    FailureCategory.gate_failed: (
        "The mesh did not meet the required quality checks.", True, True, False),
    FailureCategory.review_rejected: (
        "The mesh did not pass review.", True, True, False),
    FailureCategory.attempts_exhausted: (
        "A valid mesh could not be produced within the allowed attempts.", True, True, False),
    FailureCategory.delivery_failed: (
        "The mesh was built but could not be stored for download.", False, True, False),
    FailureCategory.timed_out: (
        "The job ran out of time before completing.", False, True, False),
    FailureCategory.internal_pipeline_failure: (
        "Something went wrong on our side while running the job.", False, True, False),
}


@dataclass(frozen=True)
class FinalResult:
    schema_version: int
    job_id: str
    owner_id: str
    status: TerminalStatus
    engine: str = ""
    purpose: str = ""
    dimensionality: str = ""
    requested_mesh_fidelity: str | None = None   # the user's own choice; None = never stated
    effective_mesh_fidelity: str = ""            # the deterministic operational tier
    mesh_fidelity_source: str = ""               # user | default
    fidelity_policy_version: str = ""
    approved_snapshot_id: str = ""
    executor_success: bool = False
    # SPLIT (v4): the verdict is about the MESH and exists only when the review concluded; the
    # execution state is about the REVIEW and always exists. The single `review` field they
    # replace could not tell "we rejected your mesh" from "we could not judge it".
    reviewer_verdict: ReviewVerdict | None = None
    review_execution: ReviewExecution = ReviewExecution.not_reached
    failed_gate: str = ""
    patch_contract_ok: bool | None = None
    outcome_code: str = ""                       # "success" or a FailureCategory value
    failure_category: str | None = None
    attempts: int = 0
    attempts_max: int = 0
    required_ready: bool = False
    delivered_types: list[str] = field(default_factory=list)   # logical ready-artifact types
    # machine-measured requirement near-misses delivered WITH the mesh; authored only by the
    # executor's typed gate, stated verbatim on every surface, waivable by nobody
    requirement_caveats: list = field(default_factory=list)
    optional_warnings: list[str] = field(default_factory=list)
    missing_outputs: list[str] = field(default_factory=list)
    finalized_at: str = ""

    def __post_init__(self) -> None:
        # THE invariant, enforced at construction so no path can violate it.
        concluded = self.review_execution is ReviewExecution.completed
        if (self.reviewer_verdict is not None) != concluded:
            raise ValueError(
                "reviewer_verdict must be present exactly when review_execution == completed; "
                f"got verdict={self.reviewer_verdict!r} execution={self.review_execution!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["reviewer_verdict"] = None if self.reviewer_verdict is None else self.reviewer_verdict.value
        d["requirement_caveats"] = [dict(c) for c in (self.requirement_caveats or [])]
        d["review_execution"] = self.review_execution.value
        return d

    @staticmethod
    def from_dict(d: dict) -> FinalResult:
        v = d.get("schema_version")
        if v not in _READABLE_SCHEMA_VERSIONS:
            raise ValueError(f"final_result schema_version {v!r} is not readable "
                             f"(accepted: {sorted(_READABLE_SCHEMA_VERSIONS)}) "
                             "- refusing to render it")
        return FinalResult(
            schema_version=v, job_id=str(d["job_id"]), owner_id=str(d["owner_id"]),
            status=TerminalStatus(d["status"]), engine=d.get("engine", ""),
            purpose=d.get("purpose", ""), dimensionality=d.get("dimensionality", ""),
            requested_mesh_fidelity=d.get("requested_mesh_fidelity"),
            effective_mesh_fidelity=d.get("effective_mesh_fidelity", ""),
            mesh_fidelity_source=d.get("mesh_fidelity_source", ""),
            fidelity_policy_version=d.get("fidelity_policy_version", ""),
            approved_snapshot_id=d.get("approved_snapshot_id", ""),
            executor_success=bool(d.get("executor_success", False)),
            reviewer_verdict=(None if d.get("reviewer_verdict") is None
                              else ReviewVerdict(d["reviewer_verdict"])),
            requirement_caveats=list(d.get("requirement_caveats") or []),
            review_execution=ReviewExecution(d.get("review_execution", "not_reached")),
            failed_gate=d.get("failed_gate", ""),
            patch_contract_ok=d.get("patch_contract_ok"), outcome_code=d.get("outcome_code", ""),
            failure_category=d.get("failure_category"), attempts=int(d.get("attempts", 0)),
            attempts_max=int(d.get("attempts_max", 0)),
            required_ready=bool(d.get("required_ready", False)),
            delivered_types=list(d.get("delivered_types", [])),
            optional_warnings=list(d.get("optional_warnings", [])),
            missing_outputs=list(d.get("missing_outputs", [])),
            finalized_at=d.get("finalized_at", ""))


# WHAT A CRASH STILL KNOWS.
# A late exception - the Reviewer failing while building its evidence, say - unwinds the graph
# and leaves no returned state. The crash path used to answer that by declaring the run's facts
# unknown: engine "", executor_success False, attempts 0. But those facts were not unknown, they
# were merely not in scope. The approved intent is fingerprint-verified on the dispatch payload,
# and every stage that completed wrote its result into graph state the fenced checkpointer had
# already persisted.
# So a crash reports what is DURABLE, and only defaults what genuinely is not. It never infers
# success: an absent checkpoint means executor_success stays False, because "we did not observe
# it" and "it did not happen" must fail the same conservative way.
def merge_durable_facts(*, approved: Mapping[str, Any] | None = None,
                        checkpoint_state: Mapping[str, Any] | None = None,
                        db_attempt: int | None = None) -> dict:
    a = approved or {}
    s = checkpoint_state or {}

    def _s(mapping, key, default=""):
        v = mapping.get(key, default)
        return v if isinstance(v, str) else default

    # ATTEMPTS: the checkpoint's retry_count is the run's own counter; the job row's
    # current_attempt is the durable fallback. Never negative, never invented.
    attempts = s.get("retry_count")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = db_attempt if isinstance(db_attempt, int) and db_attempt >= 0 else 0

    return {
        # approved intent - fingerprint-verified before dispatch, so it is as authoritative
        # after a crash as before one
        "engine": _s(s, "engine") or _s(a, "mesh_engine") or _s(a, "engine"),
        "purpose": _s(s, "purpose") or _s(a, "purpose"),
        "dimensionality": _s(s, "dimensionality") or _s(a, "dimensionality"),
        "approved_snapshot_id": _s(a, "approved_snapshot_id"),
        # stage outcomes - only ever what a completed stage actually recorded
        "executor_success": bool(s.get("executor_success", False)),
        "failed_gate": _s(s, "executor_failed_gate"),
        "reviewer_verdict": _s(s, "reviewer_verdict"),
        "attempts": int(attempts),
    }


def build_final_result(*, job_id: str, owner_id: str, status: TerminalStatus, engine: str,
                       purpose: str, dimensionality: str, approved_snapshot_id: str,
                       requested_mesh_fidelity: str | None = None,
                       effective_mesh_fidelity: str = "",
                       mesh_fidelity_source: str = "",
                       fidelity_policy_version: str = "",
                       executor_success: bool, reviewer_verdict: str, failed_gate: str,
                       api_failure: str, attempts: int, attempts_max: int,
                       required_ready: bool, delivered_types: list[str],
                       optional_warnings: list[str],
                       pipeline_timed_out: bool = False,
                       requirement_caveats: list | None = None) -> FinalResult:
    _verdict = (reviewer_verdict or "").strip().upper()

    # REVIEW-COMPLETION INVARIANT.
    # `reviewer_verdict` is written by exactly one place: run_unified_review returns a verdict
    # ONLY from an Eligibility.PASS/FAIL decision. Every other exit - deadline exhausted, a
    # submission refused as ungrounded or missing an axis, missing evidence, provider down -
    # returns api_failure and NO verdict. So `api_failure` set at terminal time proves the final
    # attempt produced no eligible verdict, and any verdict still in state was RETAINED from an
    # earlier attempt (the graph carries reviewer_verdict forward across retries).
    # A retained verdict must never be reported as this run's judgement of the mesh.
    _reviewer_marker = (api_failure or "").startswith("reviewer_")
    if api_failure and (_reviewer_marker or _verdict in ("PASS", "FAIL")):
        # The review RAN and did not conclude: either a reviewer-owned failure marker, or a
        # verdict retained from an earlier attempt that this one did not reproduce. Either way
        # there is no verdict for THIS attempt - and a retained one must never be reported as
        # this run's judgement of the mesh.
        execution, verdict = ReviewExecution.failed_to_complete, None
    elif _verdict and (failed_gate or not executor_success):
        # A failed hard gate routes straight back to authoring without reaching the reviewer, and
        # an executor that never validated a mesh has nothing to review. The verdict present is
        # retained from an earlier attempt, of either polarity.
        execution, verdict = ReviewExecution.not_reached, None
    elif _verdict == "FAIL":
        # Everything above is excluded: the executor validated a mesh, no gate failed, nothing
        # failed on our side. Eligibility accepted this attempt and the application derived FAIL.
        execution, verdict = ReviewExecution.completed, ReviewVerdict.failed
    elif _verdict == "PASS":
        execution, verdict = ReviewExecution.completed, ReviewVerdict.passed
    else:
        execution, verdict = ReviewExecution.not_reached, None

    patch_ok: bool | None = None
    if failed_gate:
        patch_ok = failed_gate != "patch_contract"
    elif executor_success:
        patch_ok = True

    if status == TerminalStatus.succeeded:
        return FinalResult(
            requirement_caveats=list(requirement_caveats or []),
            schema_version=FINAL_RESULT_SCHEMA_VERSION, job_id=job_id, owner_id=owner_id,
            status=status, engine=engine, purpose=purpose, dimensionality=dimensionality,
            requested_mesh_fidelity=requested_mesh_fidelity,
            effective_mesh_fidelity=str(effective_mesh_fidelity or ""),
            mesh_fidelity_source=str(mesh_fidelity_source or ""),
            fidelity_policy_version=str(fidelity_policy_version or ""),
            approved_snapshot_id=approved_snapshot_id, executor_success=True,
            reviewer_verdict=verdict, review_execution=execution,
            failed_gate="", patch_contract_ok=True, outcome_code="success", failure_category=None,
            attempts=attempts, attempts_max=attempts_max, required_ready=required_ready,
            delivered_types=list(delivered_types), optional_warnings=list(optional_warnings),
            missing_outputs=[], finalized_at=datetime.now(UTC).isoformat())

 # FAILURE precedence (most specific, product-meaningful cause first)
    cat = _derive_failure_category(executor_success=executor_success, verdict=verdict,
                                   execution=execution,
                                   failed_gate=failed_gate, api_failure=api_failure,
                                   required_ready=required_ready, attempts=attempts,
                                   attempts_max=attempts_max,
                                   pipeline_timed_out=pipeline_timed_out)
    return FinalResult(
        schema_version=FINAL_RESULT_SCHEMA_VERSION, job_id=job_id, owner_id=owner_id, status=status,
        engine=engine, purpose=purpose, dimensionality=dimensionality,
        requested_mesh_fidelity=requested_mesh_fidelity,
        effective_mesh_fidelity=str(effective_mesh_fidelity or ""),
        mesh_fidelity_source=str(mesh_fidelity_source or ""),
        fidelity_policy_version=str(fidelity_policy_version or ""),
        approved_snapshot_id=approved_snapshot_id, executor_success=executor_success,
        reviewer_verdict=verdict, review_execution=execution,
        failed_gate=failed_gate, patch_contract_ok=patch_ok, outcome_code=cat.value,
        failure_category=cat.value, attempts=attempts, attempts_max=attempts_max,
        required_ready=False, delivered_types=[], optional_warnings=list(optional_warnings),
        # Derived from the ONE artifact policy, never named here: a literal list silently
        # disagreed with the policy the moment a second required class was added.
        missing_outputs=list(required_output_classes(engine)),
        finalized_at=datetime.now(UTC).isoformat())


def _derive_failure_category(*, executor_success: bool, verdict: ReviewVerdict | None,
                             execution: ReviewExecution, failed_gate: str,
                             api_failure: str, required_ready: bool, attempts: int,
                             attempts_max: int,
                             pipeline_timed_out: bool = False) -> FailureCategory:
    # 1. THE REVIEW DID NOT CONCLUDE. Our side failed, so no judgement about the user's mesh is
    #    available to report. This outranks every product-meaningful cause below, because each of
    #    those asserts something we did not establish. The one thing that still outranks it is a
    #    top-level timeout, which is the more specific and more actionable reason the review had
    #    no budget left to conclude in.
    if execution is ReviewExecution.failed_to_complete and not pipeline_timed_out:
        return FailureCategory.internal_pipeline_failure
    # 2. A technical gate the mesh ACTUALLY failed. Checked before the review because a failed
    #    hard gate short-circuits back to authoring without reaching the reviewer, so any verdict
    #    present alongside it is retained from an earlier attempt, not a judgement of this mesh.
    if failed_gate:
        return FailureCategory.gate_failed
    # 3. An ELIGIBLE reviewer rejection. Reachable only when the review concluded - see the
    #    review-completion invariant in build_final_result.
    # ONLY (completed, failed) may be reported as a quality rejection.
    if execution is ReviewExecution.completed and verdict is ReviewVerdict.failed:
        return FailureCategory.review_rejected
    # 4. Execution succeeded and the mesh was validated, but the required deliverable never stored.
    if executor_success and verdict is ReviewVerdict.passed and not required_ready:
        return FailureCategory.delivery_failed
    # the run exhausted its TOP-LEVEL pipeline budget mid-flight. This outranks the generic
    # causes below (a provider error or an unvalidated mesh is usually a CONSEQUENCE of running out of
    # time, not the reason), but never the specific product causes above - a mesh the reviewer
    # actually rejected, or a gate it actually failed, is reported as what it was.
    if pipeline_timed_out:
        return FailureCategory.timed_out
    if api_failure:
        return FailureCategory.internal_pipeline_failure
    if not executor_success:
        # never validated a mesh within the budget
        return (FailureCategory.attempts_exhausted if attempts_max and attempts >= attempts_max
                else FailureCategory.native_execution_failed)
    return FailureCategory.internal_pipeline_failure


def _render_fidelity(fr: FinalResult) -> str:
    # The mesh-detail line renders the effective tier with its provenance; a record with no tier
    # renders nothing rather than inventing one.
    from meshpipeline.pipeline.enums import render_fidelity_line
    if fr.effective_mesh_fidelity and fr.mesh_fidelity_source:
        return render_fidelity_line(effective=fr.effective_mesh_fidelity,
                                    source=fr.mesh_fidelity_source)
    return ""


def render_message(fr: FinalResult) -> str:
    lines: list[str] = []
    if fr.status == TerminalStatus.succeeded:
        if fr.requirement_caveats:
            # the caveats come FIRST - before any success language - so a skimmed message
            # still reads them
            lines.append("Delivered with stated deviations from your request:")
            for c in fr.requirement_caveats:
                lines.append(
                    f"  - {c.get('direction')} margin: requested {c.get('requested'):g}, "
                    f"delivered {c.get('measured'):g} reference-lengths "
                    f"(1 reference-length = {c.get('ruler_m'):g} m)")
            lines.append("Every mesh-quality check passed; only the margins above fell short "
                         "of the request. Rebuild with relaxed constraints if they matter for "
                         "your analysis.")
        lines.append("Mesh generation completed successfully.")
        if fr.engine:
            lines.append(f"Engine: {fr.engine}")
        _fid = _render_fidelity(fr)
        if _fid:
            lines.append(_fid)
        lines.append("Required deliverable: ready to download"
                     if fr.required_ready else
                     "Required deliverable: prepared")
        if fr.reviewer_verdict is ReviewVerdict.passed:
            lines.append("Review: passed")
        if fr.optional_warnings:
            lines.append("Note: an optional preview could not be prepared, but your mesh is ready.")
        return "\n".join(lines)

    cat = FailureCategory(fr.failure_category) if fr.failure_category else \
        FailureCategory.internal_pipeline_failure
    headline, _may_have_mesh, retry_ok, user_change = _CATEGORY_META[cat]
    lines.append("Mesh generation did not complete successfully.")
    lines.append(headline)
    lines.append("No downloadable mesh deliverable is available.")
    if cat == FailureCategory.delivery_failed:
        lines[-1] = ("The mesh was built and reviewed, but the required output could not be stored, "
                     "so no download is available. This is our fault, not your geometry's.")
    if cat == FailureCategory.attempts_exhausted and fr.attempts_max:
        lines.append(f"Attempts used: {fr.attempts}/{fr.attempts_max}.")
    if user_change:
        lines.append("A change to the request is needed before this can be meshed.")
    elif retry_ok:
        lines.append("You can try running the job again.")
    return "\n".join(lines)


# #
# TERMINAL STATUS DERIVATION - what status a finished run is entitled to.
# Extracted from application/pipeline_run._run_async. The rules are policy, not sequencing: each one
# exists because a specific way of over-claiming success was possible, and each is now stated once
# where the verdict is owned rather than inline in the orchestrator.
# ON CANCELLATION: there is no cancelled terminal status (see TerminalStatus). `asyncio.CancelledError`
# is a BaseException, so it is not caught by the run's `except Exception` and propagates untouched -
# a cancelled run therefore never derives ANY status here, which is exactly why it cannot decay into
# a generic failure. That property is asserted rather than assumed.
# #

@dataclass(frozen=True)
class RunOutcome:

    api_failure: str = ""
    reviewer_verdict: str = ""
    executor_success: bool = False
    retry_count: int = 0

    @classmethod
    def from_graph_state(cls, state: Mapping) -> RunOutcome:
        raw = state.get("api_failure", "") or ""
        if raw.startswith("<<API_FAILURE:") and raw.endswith(">>"):
            raw = raw[len("<<API_FAILURE:"):-2]
        return cls(api_failure=raw,
                   reviewer_verdict=state.get("reviewer_verdict", "") or "",
                   executor_success=bool(state.get("executor_success", False)),
                   retry_count=int(state.get("retry_count", 0) or 0))


@dataclass(frozen=True)
class StatusDecision:

    status: Any                     #: JobStatus - imported lazily to keep this module import-light
    failed_reason: Any | None       #: FailedReason, or None on success

    @property
    def succeeded(self) -> bool:
        return self.failed_reason is None and getattr(self.status, "value", "") == "succeeded"


def derive_terminal_status(outcome: RunOutcome, *, job_id: str, jlog) -> StatusDecision:
    from meshpipeline.persistence.models import FailedReason, JobStatus

    if outcome.api_failure:
        # SYSTEM failure (a dependency could not do its job). Classify it for the precise DB reason
        # plus a dead-letter record, so it is inspectable rather than a bare 'failed'.
        from meshpipeline.errors import (
            classify_api_failure,
            failed_reason_for,
            record_dead_letter,
        )
        fc = classify_api_failure(outcome.api_failure)
        try:
            reason = FailedReason(failed_reason_for(fc))
        except ValueError:
            reason = FailedReason.api_failure
        dep = outcome.api_failure.split(":")[0] if ":" in outcome.api_failure else outcome.api_failure
        record_dead_letter(job_id, fc, dep, outcome.api_failure,
                           extra={"verdict": outcome.reviewer_verdict,
                                  "retry_count": outcome.retry_count})
        try:
            from meshpipeline.metrics import failure as _mfail
            _mfail(fc.value, dep)
        except Exception:                          # noqa: BLE001 - metrics never change a verdict
            pass
        return StatusDecision(JobStatus.failed, reason)

    if outcome.reviewer_verdict == "PASS" and outcome.executor_success:
        # Success requires BOTH a reviewer PASS *and* the executor having actually validated THIS
        # mesh (contract + manifest + gates). A PASS alone is not enough - without the
        # executor_success guard a mesh that failed every gate but reached the reviewer (a degraded
        # text-only review) could ship. This is the last line of defence: never deliver an
        # un-validated mesh.
        return StatusDecision(JobStatus.succeeded, None)

    return StatusDecision(
        JobStatus.failed,
        FailedReason.reviewer_rejected if outcome.executor_success else FailedReason.mesh_generation)


def apply_delivery(decision: StatusDecision, delivery) -> StatusDecision:
    from meshpipeline.persistence.models import JobStatus

    if delivery is None or delivery.succeeded:
        return decision
    return StatusDecision(JobStatus.failed, delivery.failed_reason)
