# Responsibility: Grant at most one native submission attempt per job execution generation.
# Owns: the operation identity, the atomic claim, and the disposition state machine.
# Boundaries: it decides who may call the provider; it makes no call and interprets no provider result.
# Collaborates with: simulation_jobs, read inside each statement so ownership and the write are atomic.
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

#: The one semantic native operation this product has today. Named, not implied by the table, and
#: deliberately not caller-extensible: a generic operation namespace would let a caller invent an
#: identity, and the identity is the whole guarantee.
NATIVE_MESH_SUBMISSION = "native_mesh_submission"


class Disposition(StrEnum):
    # `claimed` is the RIGHT TO TRY, never evidence the provider accepted anything.
    claimed = "claimed"
    accepted = "accepted"
    indeterminate = "indeterminate"
    failed = "failed"


class ClaimResult(StrEnum):
    acquired = "ACQUIRED"
    existing_accepted = "EXISTING_ACCEPTED"
    existing_unresolved = "EXISTING_UNRESOLVED"
    existing_failed = "EXISTING_FAILED"
    identity_conflict = "IDENTITY_CONFLICT"
    not_current_owner = "NOT_CURRENT_OWNER"


@dataclass(frozen=True)
class ClaimOutcome:

    result: ClaimResult
    operation_key: str
    disposition: Disposition | None = None
    provider_reference: str | None = None
    failure_class: str | None = None
    detail: str = ""

    @property
    def may_submit(self) -> bool:
        # ONLY the transaction that inserted the row. An existing unresolved claim is not
        # permission to try again - it is the reason not to.
        return self.result is ClaimResult.acquired


def operation_key(job_id, execution_generation: int,
                  semantic_operation: str = NATIVE_MESH_SUBMISSION) -> str:
    # THE canonical identity, from one function. It carries the job, the generation and the
    # semantic operation, and deliberately nothing else: a worker token, a process id, a random
    # exchange id, a provider name, a workspace path, the engine or the payload digest would each
    # make a replay of the SAME operation look like a different one, which is exactly the failure
    # this key exists to prevent. Engine and payload are attributes of the claimed operation and
    # are compared, not identified by.
    parts = [str(job_id), str(int(execution_generation)), semantic_operation]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _dsn() -> str:
    import meshpipeline.settings.providers as provcfg
    return provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")


def _connect():
    import psycopg
    return psycopg.connect(_dsn(), autocommit=False)


# THE ownership predicate, inside the same statement as the write. Checking ownership in one
# transaction and writing in another is the race this table exists to close: between the two, the
# claim can move. Every statement below therefore reads simulation_jobs in its own FROM/WHERE.
_CLAIM = """
insert into native_submission_claims
    (id, job_id, execution_generation, semantic_operation, operation_key,
     engine, payload_digest, disposition, claimant_fingerprint)
select %(id)s, j.id, %(gen)s, %(op)s, %(op_key)s,
       %(engine)s, %(digest)s, 'claimed', %(fingerprint)s
  from simulation_jobs j
 where j.id = %(job)s
   and j.execution_generation = %(gen)s
   and j.active_worker_token = %(token)s
-- UNQUALIFIED, deliberately. The operation key is a pure function of the identity tuple, so a
-- row that conflicts on one constraint conflicts on both; naming only the identity constraint
-- left the other free to raise instead of yielding "already claimed", which is the answer this
-- statement exists to give.
on conflict do nothing
returning id
"""

_LOOKUP = """
select disposition, engine, payload_digest, provider_reference, failure_class
  from native_submission_claims
 where job_id = %(job)s and execution_generation = %(gen)s and semantic_operation = %(op)s
"""

_IS_CURRENT = """
select 1 from simulation_jobs
 where id = %(job)s and execution_generation = %(gen)s and active_worker_token = %(token)s
"""

#: A transition is allowed only from `claimed`, except the explicit reconciliation of an
#: `indeterminate` row into `accepted`. `accepted` is terminal; `failed` never returns to
#: `claimed`; nothing returns to `claimed` at all.
_TRANSITION = """
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
   and c.disposition = any(%(from_states)s)
returning c.disposition
"""


def claim(*, job_id, execution_generation: int, worker_token, engine: str, payload_digest: str,
          semantic_operation: str = NATIVE_MESH_SUBMISSION) -> ClaimOutcome:
    key = operation_key(job_id, execution_generation, semantic_operation)
    args = {"job": str(job_id), "gen": int(execution_generation), "op": semantic_operation,
            "op_key": key, "token": str(worker_token)}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_CLAIM, {**args, "id": str(uuid.uuid4()), "engine": engine,
                             "digest": payload_digest,
                             "fingerprint": token_fingerprint(worker_token)})
        if cur.fetchone():
            conn.commit()
            return ClaimOutcome(ClaimResult.acquired, key, Disposition.claimed)

        # Nothing was inserted. Either this worker does not own the job, or the operation is
        # already claimed - and only the database can say which.
        cur.execute(_IS_CURRENT, args)
        if cur.fetchone() is None:
            conn.rollback()
            return ClaimOutcome(ClaimResult.not_current_owner, key,
                                detail="the job's current generation and token are not this "
                                       "worker's; a superseded worker may not submit")
        cur.execute(_LOOKUP, args)
        row = cur.fetchone()
        conn.commit()
        if row is None:                          # pragma: no cover - insert lost with no row
            return ClaimOutcome(ClaimResult.not_current_owner, key,
                                detail="the claim was refused and no operation exists")
        disposition, stored_engine, stored_digest, reference, failure = row
        if stored_engine != engine or stored_digest != payload_digest:
            return ClaimOutcome(
                ClaimResult.identity_conflict, key, Disposition(disposition), reference, failure,
                detail=f"the operation is claimed for engine {stored_engine!r} with a different "
                       "payload; the same identity cannot describe two different runs")
        if disposition == Disposition.accepted:
            return ClaimOutcome(ClaimResult.existing_accepted, key, Disposition.accepted,
                                reference, failure)
        if disposition == Disposition.failed:
            return ClaimOutcome(ClaimResult.existing_failed, key, Disposition.failed,
                                reference, failure)
        return ClaimOutcome(ClaimResult.existing_unresolved, key, Disposition(disposition),
                            reference, failure,
                            detail="an earlier invocation holds this operation and its outcome "
                                   "is not durably known; it must not be submitted again")


def token_fingerprint(worker_token) -> str:
    # The project's safe representation: a raw worker token is never stored, logged or published.
    return hashlib.sha256(str(worker_token).encode()).hexdigest()[:32]


def _transition(*, job_id, execution_generation: int, worker_token, to: Disposition,
                from_states: tuple[str, ...], reference: str | None = None,
                failure_class: str | None = None,
                semantic_operation: str = NATIVE_MESH_SUBMISSION) -> Disposition | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_TRANSITION, {
            "job": str(job_id), "gen": int(execution_generation), "op": semantic_operation,
            "token": str(worker_token), "to": str(to), "from_states": list(from_states),
            "reference": reference, "failure": failure_class})
        row = cur.fetchone()
        conn.commit()
    return Disposition(row[0]) if row else None


def mark_accepted(*, job_id, execution_generation: int, worker_token,
                  provider_reference: str,
                  semantic_operation: str = NATIVE_MESH_SUBMISSION) -> Disposition | None:
    # ACCEPTED MEANS EVIDENCE. The database refuses the state without a reference, so a caller
    # cannot record an acceptance it cannot point at.
    if not provider_reference:
        raise ValueError("accepted requires the provider's operation reference")
    return _transition(job_id=job_id, execution_generation=execution_generation,
                       worker_token=worker_token, to=Disposition.accepted,
                       from_states=("claimed",), reference=provider_reference,
                       semantic_operation=semantic_operation)


def mark_indeterminate(*, job_id, execution_generation: int, worker_token,
                       failure_class: str = "acknowledgement_lost",
                       semantic_operation: str = NATIVE_MESH_SUBMISSION) -> Disposition | None:
    # The honest state for a call that may have been accepted. Reachable only from `claimed`: an
    # accepted operation is settled, and a failed one was definitively answered.
    return _transition(job_id=job_id, execution_generation=execution_generation,
                       worker_token=worker_token, to=Disposition.indeterminate,
                       from_states=("claimed",), failure_class=failure_class,
                       semantic_operation=semantic_operation)


def mark_failed(*, job_id, execution_generation: int, worker_token,
                failure_class: str,
                semantic_operation: str = NATIVE_MESH_SUBMISSION) -> Disposition | None:
    return _transition(job_id=job_id, execution_generation=execution_generation,
                       worker_token=worker_token, to=Disposition.failed,
                       from_states=("claimed",), failure_class=failure_class,
                       semantic_operation=semantic_operation)


def reconcile_accepted(*, job_id, execution_generation: int, worker_token,
                       provider_reference: str,
                       semantic_operation: str = NATIVE_MESH_SUBMISSION) -> Disposition | None:
    # The ONLY route out of `indeterminate`, and only into `accepted`: it takes a provider
    # reference, so it can never be used to decide that nothing was submitted.
    if not provider_reference:
        raise ValueError("reconciliation requires the provider's operation reference")
    return _transition(job_id=job_id, execution_generation=execution_generation,
                       worker_token=worker_token, to=Disposition.accepted,
                       from_states=("indeterminate",), reference=provider_reference,
                       semantic_operation=semantic_operation)


def current(*, job_id, execution_generation: int,
            semantic_operation: str = NATIVE_MESH_SUBMISSION) -> dict | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_LOOKUP, {"job": str(job_id), "gen": int(execution_generation),
                              "op": semantic_operation})
        row = cur.fetchone()
    if row is None:
        return None
    return {"disposition": row[0], "engine": row[1], "payload_digest": row[2],
            "provider_reference": row[3], "failure_class": row[4]}
