# Responsibility: Prove the native-submission claim authority is load-bearing, against real PostgreSQL.
# Boundaries: one controlled mutation per control; the exchange and response mutations are the unit tier's.
from __future__ import annotations

import contextlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.integration import execution_ownership_support as ownership
from tests.mutation_control import prove, refuses, substitute

from meshpipeline.application import execution_fence
from meshpipeline.application.native_submission import (
    RC_INFRASTRUCTURE,
    ClaimingMeshExecutor,
    workspace_digest,
)
from meshpipeline.persistence.repositories import native_submission_repository as repo

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

ENGINE = "cfmesh"
#: Captured before any substitution, so a mutation that wraps the real behaviour calls the real
#: implementation and not itself.
_REAL_CLAIM = repo.claim
_REAL_OPERATION_KEY = repo.operation_key
_REAL_MARK_ACCEPTED = repo.mark_accepted


class _RecordingProvider:
    # The inner provider executor. It counts what would have reached Cloud Run, and hands back an
    # operation reference the way the real endpoint does.
    def __init__(self) -> None:
        self.submissions: list[str] = []
        self.collections: list[str] = []

    def run(self, workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
        self.submissions.append(operation_key)
        return {"rc": 0, "timed_out": False, "log_tail": "",
                "provider_reference":
                    _reference()}

    def collect(self, workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
        self.collections.append(operation_key)
        return {"rc": 0, "timed_out": False, "log_tail": "resumed"}


def _submit(own, workspace, provider: _RecordingProvider) -> dict:
    # THE COMPLETE PRODUCTION CALLER: the composed submission authority, the real repository and a
    # real bound ownership. Only the provider socket is a double.
    with execution_fence.execution_ownership(own):
        return ClaimingMeshExecutor(provider).run(workspace, engine=ENGINE, timeout=420)


def _reference() -> str:
    # UNIQUE PER OPERATION, as a real Cloud Run operation name is. PostgreSQL enforces that one
    # provider reference belongs to one operation, so a control that reuses a literal across its
    # own invocations manufactures a failure production could never see.
    return f"projects/p/locations/l/operations/op-{uuid.uuid4().hex[:12]}"


def _workspace(tmp_path, name: str, content: str = "the prepared bundle"):
    root = tmp_path / name
    (root / "system").mkdir(parents=True, exist_ok=True)
    (root / "system" / "meshDict").write_text(content)
    return root


class _Jobs:
    # A pool of independently claimed jobs. Every control invocation takes a fresh one, because a
    # control that ran three times against one job would be measuring its own earlier claim.
    def __init__(self) -> None:
        self._ready: list = []
        self._engines: list = []

    async def fill(self, count: int, *, prefix: str = "mut") -> None:
        for _ in range(count):
            job_id, own, Session, engine = await ownership.seeded_claim(prefix)
            self._ready.append((job_id, own, Session))
            self._engines.append(engine)

    def take(self):
        assert self._ready, "the job pool is empty; a control ran more times than expected"
        return self._ready.pop()

    async def dispose(self) -> None:
        for engine in self._engines:
            await engine.dispose()


@contextlib.contextmanager
def _without_constraints(*names: str):
    # Dropped and recreated on the disposable task database only, and restored on every exit.
    definitions = {
        "uq_native_submission_claims_identity":
            "unique (job_id, execution_generation, semantic_operation)",
        "uq_native_submission_claims_operation_key": "unique (operation_key)",
        "ck_native_submission_claims_accepted_has_reference":
            "check (disposition <> 'accepted' or "
            "(provider_reference is not null and provider_reference <> ''))",
    }
    with repo._connect() as conn, conn.cursor() as cur:
        for name in names:
            cur.execute(f"alter table native_submission_claims drop constraint {name}")
        conn.commit()
    try:
        yield
    finally:
        with repo._connect() as conn, conn.cursor() as cur:
            # The mutation's whole point was to write rows the constraint forbids, so they must
            # go before it can come back. Restoring an invariant is not optional: a drop that
            # silently failed to restore would leave every later control measuring nothing.
            cur.execute("delete from native_submission_claims a using native_submission_claims b "
                        "where a.ctid > b.ctid and a.job_id = b.job_id "
                        "and a.execution_generation = b.execution_generation "
                        "and a.semantic_operation = b.semantic_operation")
            cur.execute("delete from native_submission_claims a using native_submission_claims b "
                        "where a.ctid > b.ctid and a.operation_key = b.operation_key")
            cur.execute("update native_submission_claims set disposition = 'claimed' "
                        "where disposition = 'accepted' "
                        "and coalesce(provider_reference, '') = ''")
            for name in names:
                cur.execute(f"alter table native_submission_claims "
                            f"add constraint {name} {definitions[name]}")
            cur.execute("select conname from pg_constraint where conname = any(%s)", (list(names),))
            restored = {row[0] for row in cur.fetchall()}
            conn.commit()
        assert restored == set(names), f"a dropped constraint was not restored: {set(names) - restored}"


# 1. claim uniqueness


async def test_mutation_claim_uniqueness_removed(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="unique")

    def control() -> None:
        job_id, own, _s = jobs.take()
        digest = "a" * 64

        def attempt(_n):
            return _REAL_CLAIM(job_id=job_id, execution_generation=own.execution_generation,
                               worker_token=own.worker_token, engine=ENGINE,
                               payload_digest=digest)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        permits = [o for o in outcomes if o.may_submit]
        assert len(permits) == 1, (
            f"{len(permits)} concurrent callers reached a submission permit for one operation")

    try:
        prove("claim/1 remove claim uniqueness", control,
              lambda: _without_constraints("uq_native_submission_claims_identity",
                                           "uq_native_submission_claims_operation_key"),
              expecting="concurrent callers reached a submission permit")
    finally:
        await jobs.dispose()


# 2. an existing claimed row must not grant a permit


async def test_mutation_existing_claim_returns_acquired(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="replay")
    workspace = _workspace(tmp_path, "ws")

    def control() -> None:
        job_id, own, _s = jobs.take()
        # THE OPEN WINDOW: a worker claimed the operation and died before the provider answered,
        # so the row is `claimed` - the right to try, held by someone who may already have tried.
        held = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                          worker_token=own.worker_token, engine=ENGINE,
                          payload_digest=workspace_digest(workspace))
        assert held.may_submit
        provider = _RecordingProvider()
        result = _submit(own, workspace, provider)      # the node runs again
        assert provider.submissions == [], (
            "the replacement submitted work for an operation another invocation already claimed")
        assert result["rc"] == RC_INFRASTRUCTURE, (
            f"an unresolved claim produced a mesh verdict instead of a safe failure: {result}")

    def granting():
        def always_acquired(**kw):
            outcome = _REAL_CLAIM(**kw)
            if outcome.result is repo.ClaimResult.existing_unresolved:
                return repo.ClaimOutcome(repo.ClaimResult.acquired, outcome.operation_key,
                                         repo.Disposition.claimed)
            return outcome
        return substitute(repo, "claim", always_acquired)

    try:
        prove("claim/2 return ACQUIRED for an existing claimed row", control, granting,
              expecting="already claimed")
    finally:
        await jobs.dispose()


# 3. the generation belongs to the identity


async def test_mutation_generation_removed_from_the_operation_identity(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="newgen")

    def control() -> None:
        job_id, own, Session = jobs.take()
        digest = "b" * 64
        first = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                           worker_token=own.worker_token, engine=ENGINE, payload_digest=digest)
        assert first.may_submit
        # A GENUINELY NEW generation: a different run of the same job, entitled to submit once.
        later = own.execution_generation + 1
        with repo._connect() as conn, conn.cursor() as cur:
            cur.execute("update simulation_jobs set execution_generation = %s where id = %s",
                        (later, str(job_id)))
            conn.commit()
        fresh = repo.claim(job_id=job_id, execution_generation=later,
                           worker_token=own.worker_token, engine=ENGINE, payload_digest=digest)
        assert fresh.operation_key != first.operation_key, (
            "a new generation collided with the earlier operation's identity")
        assert fresh.may_submit, (
            f"a new generation was refused its own submission permit ({fresh.result})")

    try:
        prove("claim/3 remove the generation from the operation identity", control,
              lambda: substitute(repo, "operation_key",
                                 lambda job_id, execution_generation,
                                 semantic_operation=repo.NATIVE_MESH_SUBMISSION:
                                 _REAL_OPERATION_KEY(job_id, 0, semantic_operation)),
              expecting="collided with the earlier operation's identity")
    finally:
        await jobs.dispose()


# 4. ownership is validated inside the insert

#: The claim written WITHOUT reading `simulation_jobs` - what validating ownership in a separate
#: statement amounts to once the two are no longer atomic.
_UNFENCED_CLAIM = """
insert into native_submission_claims
    (id, job_id, execution_generation, semantic_operation, operation_key,
     engine, payload_digest, disposition, claimant_fingerprint)
values (%(id)s, %(job)s, %(gen)s, %(op)s, %(op_key)s,
        %(engine)s, %(digest)s, 'claimed', %(fingerprint)s)
on conflict do nothing
returning id
"""


async def test_mutation_ownership_validated_outside_the_insert(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="fence")

    def control() -> None:
        job_id, own, _s = jobs.take()
        stale = ownership.superseded(job_id, own)
        outcome = repo.claim(job_id=job_id, execution_generation=stale.execution_generation,
                             worker_token=stale.worker_token, engine=ENGINE,
                             payload_digest="c" * 64)
        assert not outcome.may_submit, (
            "a stale worker acquired a submission permit for an operation it does not own")
        assert repo.current(job_id=job_id,
                            execution_generation=stale.execution_generation) is None, (
            "a stale worker wrote a claim row")

    try:
        prove("claim/4 validate ownership outside the claim insert", control,
              lambda: substitute(repo, "_CLAIM", _UNFENCED_CLAIM),
              expecting="acquired a submission permit for an operation it does not own")
    finally:
        await jobs.dispose()


# 5. transitions carry the same ownership predicate

_UNFENCED_TRANSITION = """
update native_submission_claims c
   set disposition = %(to)s,
       provider_reference = coalesce(%(reference)s, c.provider_reference),
       failure_class = coalesce(%(failure)s, c.failure_class),
       updated_at = now()
 where c.job_id = %(job)s and c.execution_generation = %(gen)s
   and c.semantic_operation = %(op)s
   and c.disposition = any(%(from_states)s)
returning c.disposition
"""


async def test_mutation_transitions_lose_the_ownership_predicate(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="stale")

    def control() -> None:
        job_id, own, _s = jobs.take()
        acquired = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                              worker_token=own.worker_token, engine=ENGINE,
                              payload_digest="d" * 64)
        assert acquired.may_submit
        stale = ownership.superseded(job_id, own)
        for name, call in (
            ("accepted", lambda: repo.mark_accepted(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=stale.worker_token,
                provider_reference=_reference())),
            ("failed", lambda: repo.mark_failed(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=stale.worker_token, failure_class="forged")),
            ("indeterminate", lambda: repo.mark_indeterminate(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=stale.worker_token, failure_class="forged")),
            ("reconciled", lambda: repo.reconcile_accepted(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=stale.worker_token,
                provider_reference=_reference())),
        ):
            assert call() is None, f"a superseded worker moved the claim to {name}"
        row = repo.current(job_id=job_id, execution_generation=own.execution_generation)
        assert row["disposition"] == "claimed" and not row["provider_reference"], row

    try:
        prove("claim/5 remove the ownership predicate from disposition transitions", control,
              lambda: substitute(repo, "_TRANSITION", _UNFENCED_TRANSITION),
              expecting="a superseded worker moved the claim to")
    finally:
        await jobs.dispose()


# 6. the payload identity is immutable


async def test_mutation_changed_payload_digest_ignored(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="payload")
    first_ws = _workspace(tmp_path, "one", "the bundle that was uploaded")
    other_ws = _workspace(tmp_path, "two", "a completely different bundle")

    def control() -> None:
        job_id, own, _s = jobs.take()
        provider = _RecordingProvider()
        _submit(own, first_ws, provider)
        assert len(provider.submissions) == 1
        uploaded = repo.current(job_id=job_id,
                                execution_generation=own.execution_generation)["payload_digest"]
        # the same claimed operation, replayed with DIFFERENT workspace content
        result = _submit(own, other_ws, provider)
        assert result["rc"] == RC_INFRASTRUCTURE, (
            "one operation identity accepted a second, different bundle")
        assert provider.collections == [], (
            "a differing payload was resumed as though it were the accepted work")
        assert repo.current(job_id=job_id, execution_generation=own.execution_generation)[
            "payload_digest"] == uploaded, "the stored payload identity was overwritten"

    def ignoring():
        def digest_blind(**kw):
            outcome = _REAL_CLAIM(**kw)
            if outcome.result is repo.ClaimResult.identity_conflict:
                return repo.ClaimOutcome(repo.ClaimResult.existing_accepted,
                                         outcome.operation_key, outcome.disposition,
                                         outcome.provider_reference, outcome.failure_class)
            return outcome
        return substitute(repo, "claim", digest_blind)

    try:
        prove("claim/6 ignore a changed payload digest", control, ignoring,
              expecting="accepted a second, different bundle")
    finally:
        await jobs.dispose()


# 7. accepted means evidence


#: `mark_accepted` with its application guard removed - the transition alone, exactly what the
#: repository would do if `accepted` no longer required evidence.
def _reference_free_mark_accepted(*, job_id, execution_generation, worker_token,
                                  provider_reference):
    return repo._transition(job_id=job_id, execution_generation=execution_generation,
                            worker_token=worker_token, to=repo.Disposition.accepted,
                            from_states=("claimed",), reference=provider_reference)


@contextlib.contextmanager
def _accepted_needs_no_evidence():
    # ONE invariant, enforced in two places. Removing either alone leaves the other refusing -
    # asserted directly below - so the mutation that kills the control must remove both.
    with substitute(repo, "mark_accepted", _reference_free_mark_accepted), \
            _without_constraints("ck_native_submission_claims_accepted_has_reference"):
        yield


async def test_mutation_accepted_without_a_provider_reference(tmp_path):
    jobs = _Jobs()
    await jobs.fill(5, prefix="evidence")

    def _claimed():
        job_id, own, _s = jobs.take()
        acquired = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                              worker_token=own.worker_token, engine=ENGINE,
                              payload_digest="e" * 64)
        assert acquired.may_submit
        return job_id, own

    def control() -> None:
        job_id, own = _claimed()
        refuses(Exception, lambda: repo.mark_accepted(
            job_id=job_id, execution_generation=own.execution_generation,
            worker_token=own.worker_token, provider_reference=""),
            "an acceptance was recorded with no provider operation reference")
        row = repo.current(job_id=job_id, execution_generation=own.execution_generation)
        assert row["disposition"] == "claimed", row
        # and the reuse path therefore cannot report a fabricated acceptance
        replay = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                            worker_token=own.worker_token, engine=ENGINE,
                            payload_digest="e" * 64)
        assert replay.result is repo.ClaimResult.existing_unresolved, replay
        assert not replay.provider_reference

    try:
        # the database alone still refuses, with the application guard removed
        job_id, own = _claimed()
        with substitute(repo, "mark_accepted", _reference_free_mark_accepted):
            refuses(Exception, lambda: repo.mark_accepted(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=own.worker_token, provider_reference=""),
                "the database recorded an acceptance with no provider reference")
        # and the application guard alone still refuses, with the check constraint dropped
        job_id, own = _claimed()
        with _without_constraints("ck_native_submission_claims_accepted_has_reference"):
            refuses(ValueError, lambda: repo.mark_accepted(
                job_id=job_id, execution_generation=own.execution_generation,
                worker_token=own.worker_token, provider_reference=""),
                "the application guard recorded an acceptance with no provider reference")

        prove("claim/7 allow accepted without a provider operation reference", control,
              _accepted_needs_no_evidence,
              expecting="an acceptance was recorded with no provider operation reference")
    finally:
        await jobs.dispose()


# 8. the disposition machine is monotonic

_UNORDERED_TRANSITION = """
update native_submission_claims c
   set disposition = %(to)s,
       provider_reference = coalesce(%(reference)s, c.provider_reference),
       failure_class = coalesce(%(failure)s, c.failure_class),
       updated_at = now()
  from simulation_jobs j
 where c.job_id = j.id
   and c.job_id = %(job)s and c.execution_generation = %(gen)s
   and c.semantic_operation = %(op)s
   and j.execution_generation = %(gen)s and j.active_worker_token = %(token)s
returning c.disposition
"""


async def test_mutation_indeterminate_may_return_to_claimed(tmp_path):
    jobs = _Jobs()
    await jobs.fill(3, prefix="monotonic")

    def control() -> None:
        job_id, own, _s = jobs.take()
        gen = own.execution_generation
        acquired = repo.claim(job_id=job_id, execution_generation=gen,
                              worker_token=own.worker_token, engine=ENGINE,
                              payload_digest="f" * 64)
        assert acquired.may_submit
        assert repo.mark_indeterminate(job_id=job_id, execution_generation=gen,
                                       worker_token=own.worker_token) is \
            repo.Disposition.indeterminate

        # AN AMBIGUOUS SUBMISSION STAYS AMBIGUOUS. The state it would have to pass through to
        # become retryable is `claimed`, and nothing returns there: `mark_accepted`,
        # `mark_failed` and `mark_indeterminate` all move only from `claimed`.
        for name, call in (
            ("accepted", lambda: repo.mark_accepted(
                job_id=job_id, execution_generation=gen, worker_token=own.worker_token,
                provider_reference=_reference())),
            ("failed", lambda: repo.mark_failed(
                job_id=job_id, execution_generation=gen, worker_token=own.worker_token,
                failure_class="assumed_not_submitted")),
        ):
            assert call() is None, (
                f"an ambiguous submission was moved to {name} without reconciliation, so it "
                "became retryable")
        assert repo.current(job_id=job_id,
                            execution_generation=gen)["disposition"] == "indeterminate"
        # the only route out demands evidence, and `accepted` is then terminal
        refuses(ValueError, lambda: repo.reconcile_accepted(
            job_id=job_id, execution_generation=gen, worker_token=own.worker_token,
            provider_reference=""),
            "an indeterminate operation was reconciled with no provider reference")
        assert repo.reconcile_accepted(
            job_id=job_id, execution_generation=gen, worker_token=own.worker_token,
            provider_reference=_reference()) is \
            repo.Disposition.accepted
        assert repo.mark_indeterminate(job_id=job_id, execution_generation=gen,
                                       worker_token=own.worker_token) is None, (
            "an accepted operation was reverted, so its acceptance is not terminal")

    try:
        prove("claim/8 allow indeterminate to leave its state without reconciliation", control,
              lambda: substitute(repo, "_TRANSITION", _UNORDERED_TRANSITION),
              expecting="without reconciliation, so it became retryable")
    finally:
        await jobs.dispose()
