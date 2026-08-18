# Responsibility: Verify one native submission may be claimed per job generation, under the ownership fence.
# Boundaries: the claim authority and its state machine; dispatch integration is deliberately not wired yet.
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.integration import execution_ownership_support as ownership

from meshpipeline.persistence.repositories import native_submission_repository as repo

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


async def _claimed_job(prefix="claims", *, backend_execution_id: str = ""):
    return await ownership.seeded_claim(prefix, backend_execution_id=backend_execution_id)


# the canonical identity


def test_the_operation_key_is_the_job_the_generation_and_the_operation():
    job = uuid.uuid4()
    key = repo.operation_key(job, 3)
    assert key == repo.operation_key(job, 3), "the key is not stable for one operation"
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert key != repo.operation_key(job, 4), "the generation is not part of the identity"
    assert key != repo.operation_key(uuid.uuid4(), 3), "the job is not part of the identity"
    assert key != repo.operation_key(job, 3, "something_else")


def test_the_identity_excludes_everything_that_would_change_across_a_replay():
    # A replay of the SAME operation must derive the SAME key. Anything that rotates between two
    # workers - a token, a process, a random exchange id - would defeat that.
    job = uuid.uuid4()
    assert repo.operation_key(job, 1) == repo.operation_key(job, 1)
    # the signature simply has nowhere to put them, which is the point
    with pytest.raises(TypeError):
        repo.operation_key(job, 1, worker_token=uuid.uuid4())    # type: ignore[call-arg]


# the atomic claim


async def test_the_current_owner_acquires_the_operation_once(tmp_path):
    job_id, own, _s, engine_ = await _claimed_job()
    try:
        first = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                           worker_token=own.worker_token, engine="cfmesh", payload_digest=DIGEST)
        assert first.result is repo.ClaimResult.acquired and first.may_submit
        assert first.disposition is repo.Disposition.claimed

        # THE SAME token asking again is not a new permit: the operation is already claimed.
        again = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                           worker_token=own.worker_token, engine="cfmesh", payload_digest=DIGEST)
        assert again.result is repo.ClaimResult.existing_unresolved
        assert again.may_submit is False
        assert again.operation_key == first.operation_key
    finally:
        await engine_.dispose()


async def test_two_concurrent_current_callers_produce_exactly_one_permit():
    job_id, own, _s, engine_ = await _claimed_job()
    try:
        def attempt():
            return repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                              worker_token=own.worker_token, engine="cfmesh",
                              payload_digest=DIGEST)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [f.result(timeout=60) for f in [pool.submit(attempt), pool.submit(attempt)]]

        permits = [o for o in outcomes if o.may_submit]
        assert len(permits) == 1, [o.result for o in outcomes]
        loser = next(o for o in outcomes if not o.may_submit)
        assert loser.result is repo.ClaimResult.existing_unresolved
        assert len({o.operation_key for o in outcomes}) == 1
    finally:
        await engine_.dispose()


async def test_a_stale_worker_cannot_claim_or_transition():
    job_id, own, Session, engine_ = await _claimed_job()
    try:
        stale = ownership.superseded(job_id, own)
        refused = repo.claim(job_id=job_id, execution_generation=stale.execution_generation,
                             worker_token=stale.worker_token, engine="cfmesh",
                             payload_digest=DIGEST)
        assert refused.result is repo.ClaimResult.not_current_owner
        assert refused.may_submit is False
        assert repo.current(job_id=job_id,
                            execution_generation=stale.execution_generation) is None

        # the CURRENT owner claims it, and the stale worker still cannot move it
        assert repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                          worker_token=own.worker_token, engine="cfmesh",
                          payload_digest=DIGEST).may_submit
        for transition in (repo.mark_accepted, repo.reconcile_accepted):
            assert transition(job_id=job_id, execution_generation=own.execution_generation,
                              worker_token=stale.worker_token,
                              provider_reference="operations/should-not-land") is None
        assert repo.mark_failed(job_id=job_id, execution_generation=own.execution_generation,
                                worker_token=stale.worker_token, failure_class="x") is None
        assert repo.mark_indeterminate(job_id=job_id,
                                       execution_generation=own.execution_generation,
                                       worker_token=stale.worker_token) is None
        state = repo.current(job_id=job_id, execution_generation=own.execution_generation)
        assert state["disposition"] == "claimed" and state["provider_reference"] is None
    finally:
        await engine_.dispose()


async def test_the_same_identity_with_a_different_payload_or_engine_fails_closed():
    job_id, own, _s, engine_ = await _claimed_job()
    try:
        assert repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                          worker_token=own.worker_token, engine="cfmesh",
                          payload_digest=DIGEST).may_submit
        for engine_name, digest in (("cfmesh", OTHER_DIGEST), ("snappy", DIGEST)):
            outcome = repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                                 worker_token=own.worker_token, engine=engine_name,
                                 payload_digest=digest)
            assert outcome.result is repo.ClaimResult.identity_conflict, outcome
            assert outcome.may_submit is False
    finally:
        await engine_.dispose()


# the disposition state machine


async def test_accepted_requires_a_provider_reference_and_is_terminal():
    job_id, own, _s, engine_ = await _claimed_job()
    gen, token = own.execution_generation, own.worker_token
    try:
        assert repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                          engine="cfmesh", payload_digest=DIGEST).may_submit
        with pytest.raises(ValueError):
            repo.mark_accepted(job_id=job_id, execution_generation=gen, worker_token=token,
                               provider_reference="")
        assert repo.mark_accepted(job_id=job_id, execution_generation=gen, worker_token=token,
                                  provider_reference="operations/abc") is repo.Disposition.accepted

        # terminal: nothing moves it, and a replacement reuses it
        assert repo.mark_failed(job_id=job_id, execution_generation=gen, worker_token=token,
                                failure_class="late") is None
        assert repo.mark_indeterminate(job_id=job_id, execution_generation=gen,
                                       worker_token=token) is None
        reuse = repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                           engine="cfmesh", payload_digest=DIGEST)
        assert reuse.result is repo.ClaimResult.existing_accepted
        assert reuse.provider_reference == "operations/abc" and reuse.may_submit is False
    finally:
        await engine_.dispose()


async def test_indeterminate_never_returns_to_claimed_and_only_reconciles_forward():
    job_id, own, _s, engine_ = await _claimed_job()
    gen, token = own.execution_generation, own.worker_token
    try:
        assert repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                          engine="cfmesh", payload_digest=DIGEST).may_submit
        assert repo.mark_indeterminate(job_id=job_id, execution_generation=gen,
                                       worker_token=token) is repo.Disposition.indeterminate
        # not a permit, and not reachable back to claimed
        outcome = repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                             engine="cfmesh", payload_digest=DIGEST)
        assert outcome.result is repo.ClaimResult.existing_unresolved and not outcome.may_submit
        assert repo.current(job_id=job_id,
                            execution_generation=gen)["disposition"] == "indeterminate"
        # mark_accepted only leaves `claimed`; reconciliation is the explicit forward route
        assert repo.mark_accepted(job_id=job_id, execution_generation=gen, worker_token=token,
                                  provider_reference="operations/late") is None
        assert repo.reconcile_accepted(
            job_id=job_id, execution_generation=gen, worker_token=token,
            provider_reference="operations/found") is repo.Disposition.accepted
    finally:
        await engine_.dispose()


async def test_a_failed_operation_never_returns_to_claimed():
    job_id, own, _s, engine_ = await _claimed_job()
    gen, token = own.execution_generation, own.worker_token
    try:
        assert repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                          engine="cfmesh", payload_digest=DIGEST).may_submit
        assert repo.mark_failed(job_id=job_id, execution_generation=gen, worker_token=token,
                                failure_class="refused_by_provider") is repo.Disposition.failed
        outcome = repo.claim(job_id=job_id, execution_generation=gen, worker_token=token,
                             engine="cfmesh", payload_digest=DIGEST)
        assert outcome.result is repo.ClaimResult.existing_failed and not outcome.may_submit
        assert outcome.failure_class == "refused_by_provider"
    finally:
        await engine_.dispose()


# takeover


async def test_a_rotated_token_in_the_same_generation_sees_the_unresolved_claim():
    # THE PRE-CALL DEATH CONTROL, at the repository. Process A claims and dies before calling.
    # The replacement holds the same generation with a rotated token: it must find the claim, be
    # refused a permit, and be able to say the outcome is unknown - never fabricate success.
    # THE SAME backend execution redelivers - that is what keeps the generation and rotates only
    # the token. A different execution id would be a new run, which this control is not about.
    execution_id = f"same-exec-{uuid.uuid4().hex[:8]}"
    job_id, own_a, Session, engine_ = await _claimed_job("takeover",
                                                         backend_execution_id=execution_id)
    try:
        acquired = repo.claim(job_id=job_id, execution_generation=own_a.execution_generation,
                              worker_token=own_a.worker_token, engine="cfmesh",
                              payload_digest=DIGEST)
        assert acquired.may_submit
        fingerprint_a = repo.token_fingerprint(own_a.worker_token)

        async with Session() as db:                       # the lease expires; A is gone
            from sqlalchemy import text
            await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                                  "interval '1 second' where id = :j"), {"j": job_id})
            await db.commit()
        own_b = await ownership.claim(Session, job_id, backend_execution_id=execution_id)

        assert own_b.execution_generation == own_a.execution_generation, (
            "the takeover changed the generation - that is a new run, not a replacement")
        assert repo.token_fingerprint(own_b.worker_token) != fingerprint_a, "the token did not rotate"

        outcome = repo.claim(job_id=job_id, execution_generation=own_b.execution_generation,
                             worker_token=own_b.worker_token, engine="cfmesh",
                             payload_digest=DIGEST)
        assert outcome.result is repo.ClaimResult.existing_unresolved
        assert outcome.may_submit is False, "the replacement was given a second submission permit"
        assert outcome.provider_reference is None, "an unresolved claim reported acceptance"

        # and it may say so durably, under its own ownership
        assert repo.mark_indeterminate(
            job_id=job_id, execution_generation=own_b.execution_generation,
            worker_token=own_b.worker_token) is repo.Disposition.indeterminate
        state = repo.current(job_id=job_id, execution_generation=own_b.execution_generation)
        assert state["disposition"] == "indeterminate" and state["provider_reference"] is None
    finally:
        await engine_.dispose()


async def test_a_new_generation_derives_a_distinct_identity_and_may_submit_once():
    from meshpipeline.persistence.lease import LeaseRepository

    job_id, own_a, Session, engine_ = await _claimed_job("newgen")
    try:
        assert repo.claim(job_id=job_id, execution_generation=own_a.execution_generation,
                          worker_token=own_a.worker_token, engine="cfmesh",
                          payload_digest=DIGEST).may_submit
        async with Session() as db:
            from sqlalchemy import text
            await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                                  "interval '1 second' where id = :j"), {"j": job_id})
            await db.commit()
        async with Session() as db:               # a DIFFERENT backend execution
            _r, own_b = await LeaseRepository().claim_execution(
                db, job_id, worker_token=uuid.uuid4(), backend="test",
                backend_execution_id=f"other-{uuid.uuid4().hex[:8]}")
            await db.commit()

        assert own_b.execution_generation > own_a.execution_generation
        fresh = repo.claim(job_id=job_id, execution_generation=own_b.execution_generation,
                           worker_token=own_b.worker_token, engine="cfmesh",
                           payload_digest=DIGEST)
        assert fresh.may_submit, "a genuinely new generation was denied its own submission"
        assert fresh.operation_key != repo.operation_key(job_id, own_a.execution_generation)
        # the earlier generation's row is untouched
        assert repo.current(job_id=job_id,
                            execution_generation=own_a.execution_generation)["disposition"] == "claimed"
    finally:
        await engine_.dispose()


async def test_two_jobs_never_collide():
    a_job, a_own, _s1, a_engine = await _claimed_job("job-a")
    b_job, b_own, _s2, b_engine = await _claimed_job("job-b")
    try:
        for job_id, own in ((a_job, a_own), (b_job, b_own)):
            assert repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                              worker_token=own.worker_token, engine="cfmesh",
                              payload_digest=DIGEST).may_submit
        assert repo.operation_key(a_job, a_own.execution_generation) != \
            repo.operation_key(b_job, b_own.execution_generation)
    finally:
        await a_engine.dispose()
        await b_engine.dispose()


async def test_one_provider_reference_cannot_belong_to_two_operations():
    import psycopg

    a_job, a_own, _s1, a_engine = await _claimed_job("ref-a")
    b_job, b_own, _s2, b_engine = await _claimed_job("ref-b")
    try:
        for job_id, own in ((a_job, a_own), (b_job, b_own)):
            repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                       worker_token=own.worker_token, engine="cfmesh", payload_digest=DIGEST)
        assert repo.mark_accepted(job_id=a_job, execution_generation=a_own.execution_generation,
                                  worker_token=a_own.worker_token,
                                  provider_reference="operations/shared") is repo.Disposition.accepted
        with pytest.raises(psycopg.errors.UniqueViolation):
            repo.mark_accepted(job_id=b_job, execution_generation=b_own.execution_generation,
                               worker_token=b_own.worker_token,
                               provider_reference="operations/shared")
    finally:
        await a_engine.dispose()
        await b_engine.dispose()


async def test_no_raw_worker_token_is_stored():
    job_id, own, _s, engine_ = await _claimed_job("fingerprint")
    try:
        repo.claim(job_id=job_id, execution_generation=own.execution_generation,
                   worker_token=own.worker_token, engine="cfmesh", payload_digest=DIGEST)
        import psycopg

        import meshpipeline.settings.providers as provcfg
        dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute("select claimant_fingerprint from native_submission_claims "
                        "where job_id = %s", (str(job_id),))
            stored = cur.fetchone()[0]
        assert str(own.worker_token) not in stored
        assert stored == repo.token_fingerprint(own.worker_token) and len(stored) == 32
    finally:
        await engine_.dispose()


# the deliberate non-integration guard


async def test_the_submission_path_now_runs_through_the_claim_authority():
    # THE CUTOVER: the claim, the deterministic namespace and the provider reference moved
    # together, because any one of them alone is worse than none.
    import inspect

    from meshpipeline.adapters.mesh_execution import cloud_run_client
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    from meshpipeline.runtime.composition import build_mesh_executor

    composed = build_mesh_executor()
    assert isinstance(composed, ClaimingMeshExecutor), (
        "the provider executor is reachable without passing the submission authority")
    assert isinstance(composed._inner, cloud_run_client.CloudRunMeshExecutor)

    exchange = inspect.getsource(cloud_run_client._exchange)
    assert "coordinates_for(operation_key)" in exchange and "uuid" not in exchange
    # the authority records what the adapter reports, and nothing else does
    authority = inspect.getsource(ClaimingMeshExecutor.run)
    assert "mark_accepted" in authority and "mark_indeterminate" in authority
    assert "native_submission_repository" not in inspect.getsource(cloud_run_client)
