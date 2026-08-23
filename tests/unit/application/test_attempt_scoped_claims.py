# Responsibility: Verify the submission identity is scoped to the planned attempt, so a re-plan can dispatch.
from __future__ import annotations

import uuid
from types import SimpleNamespace

from meshpipeline.application.native_submission import (
    ClaimingMeshExecutor,
    attempt_scoped_operation,
)
from meshpipeline.persistence.repositories.native_submission_repository import (
    NATIVE_MESH_SUBMISSION,
    ClaimOutcome,
    ClaimResult,
    Disposition,
    operation_key,
)


def test_each_attempt_names_its_own_operation_and_a_replay_names_the_same_one(tmp_path):
    # The old identity was job+generation alone: "one submission per generation". The builder
    # re-plans WITHIN a generation, so attempt 2's digest conflicted with attempt 1's claim and
    # every retry died before dispatch - each recoverable rejection became a terminal job.
    a1 = tmp_path / "generation_1" / "attempt_1"
    a2 = tmp_path / "generation_1" / "attempt_2"
    assert attempt_scoped_operation(a1) == f"{NATIVE_MESH_SUBMISSION}:attempt_1"
    assert attempt_scoped_operation(a2) == f"{NATIVE_MESH_SUBMISSION}:attempt_2"
    # deterministic: replaying the same attempt derives the same identity, and the keys differ
    # between attempts - which is exactly what lets attempt 2 claim a run of its own
    job = uuid.uuid4()
    k1 = operation_key(job, 1, attempt_scoped_operation(a1))
    k2 = operation_key(job, 1, attempt_scoped_operation(a2))
    assert k1 != k2
    assert k1 == operation_key(job, 1, attempt_scoped_operation(a1))


def test_a_workspace_that_names_no_attempt_keeps_the_generation_scoped_identity(tmp_path):
    # fail closed: anything that is not the builder's attempt_N naming behaves exactly as before
    assert attempt_scoped_operation(tmp_path / "workspace") == NATIVE_MESH_SUBMISSION
    assert attempt_scoped_operation(tmp_path / "attempt_") == NATIVE_MESH_SUBMISSION
    assert attempt_scoped_operation(tmp_path / "attempt_x") == NATIVE_MESH_SUBMISSION
    # and zero-padding normalises rather than forking the identity
    assert (attempt_scoped_operation(tmp_path / "attempt_007")
            == f"{NATIVE_MESH_SUBMISSION}:attempt_7")


def test_the_claim_and_its_settlement_name_the_same_operation(monkeypatch, tmp_path):
    # THE invariant that keeps rows from being stranded: if the claim were made under the
    # attempt-scoped name and the acceptance recorded under the bare one, the claim row would sit
    # in `claimed` forever and the NEXT replay of that attempt would read "existing unresolved".
    from meshpipeline.application import execution_fence
    from meshpipeline.persistence.repositories import native_submission_repository as claims

    ws = tmp_path / "generation_1" / "attempt_2"
    ws.mkdir(parents=True)
    (ws / "plan.json").write_text("{}")

    own = SimpleNamespace(job_id=uuid.uuid4(), execution_generation=1, worker_token="tok")
    monkeypatch.setattr(execution_fence, "current_ownership", lambda: own)

    seen: dict = {}

    def fake_claim(*, job_id, execution_generation, worker_token, engine, payload_digest,
                   semantic_operation=NATIVE_MESH_SUBMISSION):
        seen["claim_op"] = semantic_operation
        return ClaimOutcome(ClaimResult.acquired,
                            operation_key(job_id, execution_generation, semantic_operation),
                            Disposition.claimed)

    def fake_accepted(*, job_id, execution_generation, worker_token, provider_reference,
                      semantic_operation=NATIVE_MESH_SUBMISSION):
        seen["settle_op"] = semantic_operation
        return Disposition.accepted

    monkeypatch.setattr(claims, "claim", fake_claim)
    monkeypatch.setattr(claims, "mark_accepted", fake_accepted)

    class _Provider:
        def run(self, workspace, *, engine, timeout, operation_key):
            seen["exchange_key"] = operation_key
            return {"rc": 0, "provider_reference": "projects/p/locations/l/operations/x"}

    result = ClaimingMeshExecutor(_Provider()).run(ws, engine="snappy", timeout=60)

    assert result["rc"] == 0
    assert seen["claim_op"] == f"{NATIVE_MESH_SUBMISSION}:attempt_2"
    assert seen["settle_op"] == seen["claim_op"], (
        "the acceptance was recorded under a different operation than the claim")
    # and the exchange namespace is derived from the attempt-scoped key, so attempt 2's objects
    # can never overwrite attempt 1's
    assert seen["exchange_key"] == operation_key(own.job_id, 1, seen["claim_op"])
