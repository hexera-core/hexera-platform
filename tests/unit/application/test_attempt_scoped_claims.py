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


def test_each_planned_pass_names_its_own_operation_within_an_attempt(tmp_path):
    # The attempt-level collapse re-appeared one level down: the driver runs several meshing
    # passes inside ONE attempt workspace, so pass 2's revised case conflicted with pass 1's
    # claim and every pass after the first died before dispatch ("the mesh run never started").
    from meshpipeline.contracts.mesh_execution import note_native_pass

    ws = tmp_path / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    note_native_pass(ws, 1)
    op1 = attempt_scoped_operation(ws)
    note_native_pass(ws, 2)
    op2 = attempt_scoped_operation(ws)
    assert op1 == f"{NATIVE_MESH_SUBMISSION}:attempt_1:pass_1"
    assert op2 == f"{NATIVE_MESH_SUBMISSION}:attempt_1:pass_2"
    job = uuid.uuid4()
    assert operation_key(job, 1, op1) != operation_key(job, 1, op2)
    # deterministic: a re-executed node re-derives the same identity for the same pass
    note_native_pass(ws, 2)
    assert attempt_scoped_operation(ws) == op2


def test_a_workspace_recording_no_pass_keeps_the_attempt_scoped_identity(tmp_path):
    # fail closed: no fact, an unreadable fact, or a non-numeric one behaves exactly as before
    from meshpipeline.contracts.mesh_execution import NATIVE_PASS_FACT

    ws = tmp_path / "attempt_3"
    ws.mkdir()
    assert attempt_scoped_operation(ws) == f"{NATIVE_MESH_SUBMISSION}:attempt_3"
    (ws / NATIVE_PASS_FACT).write_text("not-a-pass")
    assert attempt_scoped_operation(ws) == f"{NATIVE_MESH_SUBMISSION}:attempt_3"
    (ws / NATIVE_PASS_FACT).write_text("-2")
    assert attempt_scoped_operation(ws) == f"{NATIVE_MESH_SUBMISSION}:attempt_3"
    # and zero-padding normalises rather than forking the identity
    (ws / NATIVE_PASS_FACT).write_text("007")
    assert attempt_scoped_operation(ws) == f"{NATIVE_MESH_SUBMISSION}:attempt_3:pass_7"


class _InMemoryClaims:
    """The claim table's decision procedure, keyed exactly as the durable one keys it:
    (job, generation, semantic operation), with engine and payload digest compared, never
    identified by."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.submissions = 0

    def claim(self, *, job_id, execution_generation, worker_token, engine, payload_digest,
              semantic_operation=NATIVE_MESH_SUBMISSION):
        key = (str(job_id), int(execution_generation), semantic_operation)
        op_key = operation_key(job_id, execution_generation, semantic_operation)
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = {"engine": engine, "digest": payload_digest,
                              "disposition": Disposition.claimed, "reference": None}
            return ClaimOutcome(ClaimResult.acquired, op_key, Disposition.claimed)
        if row["engine"] != engine or row["digest"] != payload_digest:
            return ClaimOutcome(
                ClaimResult.identity_conflict, op_key, row["disposition"], row["reference"],
                detail=f"the operation is claimed for engine {row['engine']!r} with a different "
                       "payload; the same identity cannot describe two different runs")
        if row["disposition"] is Disposition.accepted:
            return ClaimOutcome(ClaimResult.existing_accepted, op_key, Disposition.accepted,
                                row["reference"])
        return ClaimOutcome(ClaimResult.existing_unresolved, op_key, row["disposition"])

    def mark_accepted(self, *, job_id, execution_generation, worker_token, provider_reference,
                      semantic_operation=NATIVE_MESH_SUBMISSION):
        self.rows[(str(job_id), int(execution_generation), semantic_operation)].update(
            disposition=Disposition.accepted, reference=provider_reference)
        return Disposition.accepted

    def install(self, monkeypatch):
        from meshpipeline.persistence.repositories import native_submission_repository as claims
        monkeypatch.setattr(claims, "claim", self.claim)
        monkeypatch.setattr(claims, "mark_accepted", self.mark_accepted)
        return self


class _CountingProvider:
    def __init__(self, claims: _InMemoryClaims) -> None:
        self._claims = claims
        self.collected = 0

    def run(self, workspace, *, engine, timeout, operation_key):
        self._claims.submissions += 1
        return {"rc": 0, "provider_reference": f"projects/p/locations/l/operations/{operation_key[:8]}"}

    def collect(self, workspace, *, engine, timeout, operation_key):
        self.collected += 1
        return {"rc": 0, "collected": True}


def test_a_revised_pass_dispatches_and_a_true_replay_disagreement_still_fails_closed(
        monkeypatch, tmp_path):
    # THE regression the incident demands, all three faces of it:
    #   (a) pass 1 accepted, pass 2 arrives with a REVISED case -> its own claim, it dispatches;
    #   (b) the SAME pass re-arrives with a DIFFERENT payload -> refused, fail closed as ever;
    #   (c) the SAME pass re-arrives with the SAME payload -> the accepted run is reused, not rerun.
    from meshpipeline.application import execution_fence
    from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE, note_native_pass

    ws = tmp_path / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    own = SimpleNamespace(job_id=uuid.uuid4(), execution_generation=1, worker_token="tok")
    monkeypatch.setattr(execution_fence, "current_ownership", lambda: own)

    claims = _InMemoryClaims().install(monkeypatch)
    provider = _CountingProvider(claims)
    executor = ClaimingMeshExecutor(provider)

    # (a) two passes of one attempt, each with a legitimately revised plan
    (ws / "plan.json").write_text('{"quality": "balanced", "max_cells": 2000000}')
    note_native_pass(ws, 1)
    assert executor.run(ws, engine="snappy", timeout=60)["rc"] == 0
    (ws / "plan.json").write_text('{"quality": "strict", "max_cells": 3000000}')
    note_native_pass(ws, 2)
    assert executor.run(ws, engine="snappy", timeout=60)["rc"] == 0, (
        "a revised pass must claim its own run, not die as a conflicting replay of pass 1")
    assert claims.submissions == 2

    # (b) same identity, same pass, different payload: nondeterminism or a real replay
    # disagreement - the refusal is the guarantee, and it must survive this fix intact
    (ws / "plan.json").write_text('{"quality": "tampered", "max_cells": 1}')
    refused = executor.run(ws, engine="snappy", timeout=60)
    assert refused["rc"] == RC_INFRASTRUCTURE
    assert "different payload" in refused["log_tail"]
    assert claims.submissions == 2, "a conflicting replay must never reach the provider"

    # (c) an exact replay of pass 2 reuses the accepted submission instead of paying twice
    (ws / "plan.json").write_text('{"quality": "strict", "max_cells": 3000000}')
    replay = executor.run(ws, engine="snappy", timeout=60)
    assert replay.get("collected") is True
    assert provider.collected == 1
    assert claims.submissions == 2, "a replay must collect, never resubmit"
