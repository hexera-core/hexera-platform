# Responsibility: Be the one way tests obtain a capture authority and seed it.
# Boundaries: an in-memory stand-in honouring the durable authority's contract, including how it reconciles a conflict.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from meshpipeline.persistence.repositories.capture_repository import (
    RecordResult,
    payload_sha256,
)


@dataclass
class _Row:
    seq: int
    owner_id: str
    job_id: str
    execution_generation: int
    op_key: str
    record_type: str
    name: str
    kind: str
    attempt: int | None
    payload: dict
    payload_sha256: str
    created_at: datetime
    conflicted: bool = False
    conflict_evidence: list = field(default_factory=list)


class InMemoryAuthority:

    #: The epoch the job row would report. None means "whatever epoch this fixture's records were
    #: written with", which is the production invariant: a run's capture rows carry the same
    #: generation the job row holds. A takeover test sets it explicitly to something else.
    live_generation: int | None = None


    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int, str], _Row] = {}
        self._next_seq = 1

    # write
    def record_operation(self, *, owner_id: str, job_id: str, execution_generation: int,
                         op_key: str, name: str, payload, record_type: str = "span_event",
                         kind: str = "event", attempt: int | None = None) -> RecordResult:
        digest = payload_sha256(payload)
        key = (owner_id, str(job_id), int(execution_generation), op_key)
        row = self._rows.get(key)
        if row is None:
            self._rows[key] = _Row(
                seq=self._next_seq, owner_id=owner_id, job_id=str(job_id),
                execution_generation=int(execution_generation), op_key=op_key,
                record_type=record_type, name=name, kind=kind, attempt=attempt,
                payload=payload, payload_sha256=digest, created_at=datetime.now(UTC))
            self._next_seq += 1
            return RecordResult("inserted", op_key)
        if row.payload_sha256 != digest:
            row.conflicted = True
            row.conflict_evidence.append({"competing_sha256": digest,
                                          "at": datetime.now(UTC).isoformat()})
            return RecordResult("conflicted", op_key)
        # content matches - but a row already marked conflicted never regains trust
        return RecordResult("already_conflicted" if row.conflicted else "duplicate", op_key)

    # read
    def trusted_operations(self, *, owner_id: str, job_id: str,
                           execution_generation: int | None = None) -> list[dict]:
        rows = [r for r in self._rows.values()
                if r.owner_id == owner_id and r.job_id == str(job_id) and not r.conflicted
                and (execution_generation is None
                     or r.execution_generation == int(execution_generation))]
        return [self._as_dict(r) for r in sorted(rows, key=lambda r: r.seq)]

    def conflicted_operations(self, *, owner_id: str, job_id: str) -> list[dict]:
        rows = [r for r in self._rows.values()
                if r.owner_id == owner_id and r.job_id == str(job_id) and r.conflicted]
        return [{"seq": r.seq, "op_key": r.op_key, "name": r.name,
                 "payload_sha256": r.payload_sha256,
                 "conflict_evidence": r.conflict_evidence, "created_at": r.created_at}
                for r in sorted(rows, key=lambda r: r.seq)]

    @staticmethod
    def _as_dict(r: _Row) -> dict:
        return {"seq": r.seq, "op_key": r.op_key, "record_type": r.record_type, "name": r.name,
                "kind": r.kind, "attempt": r.attempt, "payload": r.payload,
                "payload_sha256": r.payload_sha256, "created_at": r.created_at,
                "execution_generation": r.execution_generation}

    # convenience for seeding a test's starting state
    def seed(self, owner_id: str, job_id: str, records: list[dict], *, generation: int = 1) -> None:
        for i, rec in enumerate(records):
            self.record_operation(
                owner_id=owner_id, job_id=job_id, execution_generation=generation,
                op_key=rec.get("op_key") or f"seed:{i}", name=rec["name"],
                record_type=rec.get("record_type", "span_event"),
                attempt=(rec.get("attributes") or {}).get("attempt"),
                payload=rec.get("payload", {}))

    def authoritative_generation(self, *, owner_id: str, job_id: str) -> int | None:
        # Stands in for the job row. Production reads the job, never the capture rows, because a
        # superseded worker would otherwise nominate itself; this double has no job table, so with
        # no epoch declared it reports the one its records were written with - the same invariant,
        # expressed with what the fixture has. A takeover test declares `live_generation` instead.
        if self.live_generation is not None:
            return int(self.live_generation)
        gens = {r.execution_generation for r in self._rows.values()
                if r.owner_id == owner_id and r.job_id == str(job_id)}
        return max(gens) if gens else None

    def install(self, monkeypatch) -> InMemoryAuthority:
        import meshpipeline.persistence.repositories.capture_repository as cap
        for fn in ("record_operation", "trusted_operations", "conflicted_operations",
                   "authoritative_generation"):
            monkeypatch.setattr(cap, fn, getattr(self, fn))
        return self


def seed_events(authority, owner_id: str, job_id: str, events: list[dict], *,
                generation: int = 0) -> None:
    for i, e in enumerate(events):
        authority.record_operation(
            owner_id=owner_id, job_id=job_id, execution_generation=generation,
            op_key=f"{e['event_type']}:{i}", name=e["event_type"],
            record_type="span_event", kind="event",
            attempt=e.get("attempt"), payload=e.get("payload", {}))


#: The tenant every unit-tier capture test files under. One constant, so a test cannot
#: accidentally assert isolation against a typo instead of a real foreign owner.
CAPTURE_OWNER = "owner-training-tests"
FOREIGN_OWNER = "owner-someone-else"


def seed_conflict(authority, owner_id: str, job_id: str, *, op_key: str = "disputed",
                  name: str = "final_result_built", first=None, second=None,
                  generation: int = 0) -> None:
    for payload in (first if first is not None else {"verdict": "PASS"},
                    second if second is not None else {"verdict": "FAIL"}):
        authority.record_operation(owner_id=owner_id, job_id=job_id,
                                   execution_generation=generation, op_key=op_key,
                                   name=name, payload=payload)


def export_projection(authority, owner_id: str, job_id: str,
                      execution_generation: int | None = None) -> list[dict]:
    return [
        {"schema_version": 2, "record_type": row["record_type"], "trace_id": job_id,
         "seq": row["seq"], "span_id": f'{row["seq"]:016x}', "parent_span_id": None,
         "ts": row["created_at"].isoformat(), "name": row["name"],
         "kind": row["kind"], "component": "event",
         "attributes": ({"attempt": row["attempt"]} if row["attempt"] is not None else {}),
         "execution_generation": row["execution_generation"],
         "payload": row["payload"], "payload_sha256": row["payload_sha256"]}
        for row in authority.trusted_operations(
            owner_id=owner_id, job_id=job_id, execution_generation=execution_generation)
    ]
