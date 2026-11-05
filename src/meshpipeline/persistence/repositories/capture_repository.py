# Responsibility: Be the durable authority for one logical capture operation.
# Boundaries: re-recording identical content is a no-op.
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Outcome = Literal["inserted", "duplicate", "conflicted", "already_conflicted"]

#: How many competing digests are kept before the evidence list stops growing. A pathological
#: replay loop must not turn one row into unbounded storage; the first entries are the diagnostic
#: ones, and the count below records the rest.
MAX_EVIDENCE = 16


@dataclass(frozen=True)
class RecordResult:
    outcome: Outcome
    op_key: str

    @property
    def trusted(self) -> bool:
        return self.outcome in ("inserted", "duplicate")


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _dsn() -> str:
    import meshpipeline.settings.providers as pc
    raw = getattr(pc, "POSTGRES_DSN", "") or getattr(pc, "DATABASE_URL", "")
    return raw.replace("+asyncpg", "").replace("+psycopg", "")


def _connect():
    import psycopg
    return psycopg.connect(_dsn(), autocommit=False)


# the write path

_INSERT = """
insert into capture_operations
    (id, owner_id, job_id, execution_generation, op_key,
     record_type, name, kind, attempt, payload, payload_sha256)
values (%(id)s, %(owner)s, %(job)s, %(gen)s, %(op_key)s,
        %(record_type)s, %(name)s, %(kind)s, %(attempt)s, %(payload)s, %(digest)s)
on conflict on constraint uq_capture_operations_identity do nothing
returning id
"""

# Marks the operation conflicted ONLY when the incoming content differs from what is stored. The
# stored payload is never overwritten: it is evidence, and replacing it would destroy exactly the
# thing an operator needs to diagnose the disagreement.
_CONFLICT = """
update capture_operations
   set conflicted = true,
       conflict_evidence = (
           case when coalesce(jsonb_array_length(conflict_evidence), 0) >= %(cap)s
                then conflict_evidence
                else coalesce(conflict_evidence, '[]'::jsonb)
                     || jsonb_build_array(jsonb_build_object(
                            'competing_sha256', %(digest)s, 'at', now()::text))
           end)
 where owner_id = %(owner)s and job_id = %(job)s
   and execution_generation = %(gen)s and op_key = %(op_key)s
   and payload_sha256 <> %(digest)s
returning conflicted
"""

_LOOKUP = """
select conflicted from capture_operations
 where owner_id = %(owner)s and job_id = %(job)s
   and execution_generation = %(gen)s and op_key = %(op_key)s
"""


def record_operation(*, owner_id: str, job_id: str, execution_generation: int, op_key: str,
                     name: str, payload: Any, record_type: str = "span_event",
                     kind: str = "event", attempt: int | None = None) -> RecordResult:
    digest = payload_sha256(payload)
    args = {"owner": owner_id, "job": job_id, "gen": int(execution_generation), "op_key": op_key,
            "digest": digest, "cap": MAX_EVIDENCE}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_INSERT, {**args, "id": str(uuid.uuid4()), "record_type": record_type,
                              "name": name, "kind": kind, "attempt": attempt,
                              "payload": json.dumps(payload, default=str)})
        if cur.fetchone():
            conn.commit()
            return RecordResult("inserted", op_key)

        # The identity is taken. Either this is the same record arriving again, or two executions
        # of one operation disagreed - and only the stored digest can tell the two apart.
        cur.execute(_CONFLICT, args)
        if cur.fetchone():
            conn.commit()
            logger.error(
                "capture: CONFLICTING replay job=%s gen=%s op_key=%s - two executions disagreed; "
                "the operation is quarantined and neither payload will be exported",
                job_id, execution_generation, op_key)
            return RecordResult("conflicted", op_key)

        cur.execute(_LOOKUP, args)
        row = cur.fetchone()
        conn.commit()
        # A replay matching a row that is ALREADY conflicted does not restore trust: the row keeps
        # its flag, and this reports it so no caller can mistake the no-op for a clean write.
        if row and row[0]:
            return RecordResult("already_conflicted", op_key)
        return RecordResult("duplicate", op_key)


# the read path

def authoritative_generation(*, owner_id: str, job_id: str) -> int | None:
    # WHICH generation an export speaks for. Read from the job row, which is the durable authority
    # for the live epoch - never from the capture rows themselves, because a superseded worker's
    # records are deliberately kept and would otherwise nominate their own generation. Returns None
    # when the job is absent or unowned, so the caller can refuse rather than fall back to "all".
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""select execution_generation from simulation_jobs
                        where id = %(job)s and owner_id = %(owner)s""",
                    {"job": job_id, "owner": owner_id})
        row = cur.fetchone()
    return int(row[0]) if row is not None else None


def trusted_operations(*, owner_id: str, job_id: str,
                       execution_generation: int | None = None) -> list[dict]:
    sql = ["""select seq, op_key, record_type, name, kind, attempt, payload, payload_sha256,
                     created_at, execution_generation
                from capture_operations
               where owner_id = %(owner)s and job_id = %(job)s and not conflicted"""]
    args: dict = {"owner": owner_id, "job": job_id}
    if execution_generation is not None:
        sql.append("and execution_generation = %(gen)s")
        args["gen"] = int(execution_generation)
    sql.append("order by seq")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(" ".join(sql), args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def conflicted_operations(*, owner_id: str, job_id: str) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""select seq, op_key, name, payload_sha256, conflict_evidence, created_at
                         from capture_operations
                        where owner_id = %(owner)s and job_id = %(job)s and conflicted
                        order by seq""",
                    {"owner": owner_id, "job": job_id})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
