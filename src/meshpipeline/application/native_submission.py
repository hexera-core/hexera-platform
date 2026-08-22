# Responsibility: Let at most one worker submit a native mesh run, and say honestly what became of it.
# Owns: the ownership precondition, the claim decision, and the disposition recorded for its outcome.
# Boundaries: it decides and records; the provider call itself belongs to the executor it wraps.
# Collaborates with: execution_fence.py for the ownership a claim needs, and native_submission_repository.py to hold it.
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE

logger = logging.getLogger(__name__)



@runtime_checkable
class ProviderMeshExecutor(Protocol):
    # The INNER contract: the same call, plus the operation identity, because the provider
    # exchange must land in a namespace a replacement worker can re-derive.
    def run(self, workspace: Any, *, engine: str, timeout: int,
            operation_key: str) -> dict: ...


def workspace_digest(workspace: Any) -> str:
    # THE BUNDLE, not the path: sorted relative names and their content hashes. Stored with the
    # claim so a replay that would upload different content is refused as an identity conflict
    # rather than quietly overwriting the accepted operation's input.
    root = Path(workspace)
    parts = [
        f"{path.relative_to(root)}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in sorted(p for p in root.rglob("*") if p.is_file())
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _infrastructure_failure(engine: str, detail: str) -> dict:
    logger.error("native submission unresolved for %s: %s", engine, detail)
    return {"rc": RC_INFRASTRUCTURE, "timed_out": False,
            "log_tail": f"[CLOUD_RUN_FAILED] {engine}: {detail}"}


class ClaimingMeshExecutor:
    # THE SUBMISSION AUTHORITY, wrapped around whichever provider executor is composed. It holds
    # the one rule the crash window needs: a worker may contact the provider only if it created
    # the claim. A replacement that finds an existing claim never submits again - it reuses what
    # is durably known, or says plainly that the outcome is not known.

    def __init__(self, inner: ProviderMeshExecutor) -> None:
        self._inner = inner

    def run(self, workspace: Any, *, engine: str, timeout: int) -> dict:
        from meshpipeline.application import execution_fence as fence
        from meshpipeline.persistence.repositories import (
            native_submission_repository as claims,
        )

        own = fence.current_ownership()
        if own is None:
            # FAIL CLOSED. Without ownership there is no operation identity, so there is no claim
            # to make this submission at-most-once and no namespace of its own to exchange
            # through - every unowned run would share one set of object keys and overwrite the
            # next. The composition root installs this authority around the Cloud Run executor and
            # nothing else, so reaching here means a caller escaped the ownership binding, which
            # is a fault to report rather than a mesh to launch.
            return _infrastructure_failure(
                engine, "no execution ownership is bound, so this run cannot claim an operation "
                        "identity; it will not be submitted")

        digest = workspace_digest(workspace)
        outcome = claims.claim(job_id=own.job_id, execution_generation=own.execution_generation,
                               worker_token=own.worker_token, engine=engine,
                               payload_digest=digest)

        if outcome.result is claims.ClaimResult.existing_accepted:
            # ALREADY SUBMITTED, durably. The provider is working under the same deterministic
            # coordinates, so continue reading from them rather than starting a second run.
            logger.info("native submission already accepted for job %s generation %s - reusing "
                        "operation %s", own.job_id, own.execution_generation,
                        outcome.provider_reference)
            collect = getattr(self._inner, "collect", None)
            if collect is None:
                return _infrastructure_failure(
                    engine, "a previous worker's submission was accepted and this executor "
                            "cannot collect its result without submitting again")
            return collect(workspace, engine=engine, timeout=timeout,
                           operation_key=outcome.operation_key)

        if outcome.result is claims.ClaimResult.existing_failed:
            return _infrastructure_failure(
                engine, f"a previous worker's submission failed definitively "
                        f"({outcome.failure_class})")

        if outcome.result is not claims.ClaimResult.acquired:
            # `claimed`, `indeterminate`, an identity conflict, or a superseded worker. None of
            # them may submit, and none of them may report success: the honest answer is that the
            # outcome is not known here.
            return _infrastructure_failure(engine, outcome.detail or str(outcome.result))

        try:
            result = self._inner.run(workspace, engine=engine, timeout=timeout,
                                     operation_key=outcome.operation_key)
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            if _is_indeterminate(exc):
                # The request may have reached the provider. Recording that is the whole point:
                # a replacement must not decide for itself that nothing was submitted.
                claims.mark_indeterminate(
                    job_id=own.job_id, execution_generation=own.execution_generation,
                    worker_token=own.worker_token, failure_class=type(exc).__name__)
                return _infrastructure_failure(
                    engine, "the submission may have been accepted and its acknowledgement was "
                            "lost; it will not be resubmitted automatically")
            claims.mark_failed(job_id=own.job_id,
                               execution_generation=own.execution_generation,
                               worker_token=own.worker_token,
                               failure_class=type(exc).__name__)
            raise
        else:
            reference = str(result.get("provider_reference", "") or "")
            if reference:
                claims.mark_accepted(
                    job_id=own.job_id, execution_generation=own.execution_generation,
                    worker_token=own.worker_token, provider_reference=reference)
            else:
                # The executor completed without naming an operation - a local runner, or a
                # provider whose reference we could not read. Neither may be recorded as accepted.
                claims.mark_indeterminate(
                    job_id=own.job_id, execution_generation=own.execution_generation,
                    worker_token=own.worker_token, failure_class="no_provider_reference")
            return result


def _is_indeterminate(exc: BaseException) -> bool:
    from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate
    return isinstance(exc, SubmissionIndeterminate)
