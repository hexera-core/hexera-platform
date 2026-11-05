# Responsibility: Record a run's captured events in the durable authority.
# Boundaries: writes only when this deployment collects data, and every write is fail-open.
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

import meshpipeline.settings.policy as polcfg

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def _secret_values() -> list[tuple[str, str]]:
    out = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|DSN", k, re.I):
            out.append((v, f"[REDACTED:{k}]"))
    return out


def _redact_str(s: str) -> str:
    for v, repl in _secret_values():
        if v in s:
            s = s.replace(v, repl)
    return _TOKEN_RE.sub("[REDACTED:TOKEN]", s)


def _redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return _redact_str(obj)
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


# A capture record's LOGICAL identity, for replay.
# It answers "is this the same intended record being written again?" - NOT "does this payload
# happen to look the same?". So the payload digest is deliberately NOT part of it: including the
# digest meant a replay that produced DIFFERENT content computed a different key and was written
# as a second record, which is precisely the case worth catching. The digest is stored beside the
# key instead, so a conflicting replay is detected rather than duplicated.
# `op_id` is the producer's own occurrence identity, supplied at the emitting seam. Without one
# there is no way to tell a legitimate second record of the same kind from a replay of the first,
# so a record that does not carry one is not durable and is dropped.
def _operation_key(record: dict, op_id: str) -> str:
    attrs = record.get("attributes") or {}
    parts = [
        str(record.get("trace_id", "")),          # the job
        str(record.get("record_type", "")),
        str(record.get("name", "")),              # which event
        str(record.get("kind", "")),
        str(attrs.get("attempt", "")),            # a real retry is a DIFFERENT operation
        str(attrs.get("execution_generation", "")),
        str(op_id),                               # which occurrence, per the producer
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _record_durably(job_id: str, record: dict, payload: Any, op_id: str) -> None:
    from meshpipeline.capture import scope as _scope
    sc = _scope.current_scope()
    if sc is None or not sc.owner_id:
        logger.warning("capture: no tenant scope bound for job %s - training record dropped",
                       job_id)
        return

    from meshpipeline.persistence.repositories import capture_repository as cap
    attrs = record.get("attributes") or {}
    try:
        result = cap.record_operation(
            owner_id=sc.owner_id, job_id=job_id,
            execution_generation=_scope.current_generation(),
            op_key=_operation_key(record, op_id),
            record_type=str(record.get("record_type", "span_event")),
            name=str(record.get("name", "")), kind=str(record.get("kind", "event")),
            # Redacted on the way IN: moving the content to a database does not make a
            # token-shaped secret safe to store.
            attempt=attrs.get("attempt"), payload=_redact(payload))
    except Exception as exc:  # noqa: BLE001 - optional capture never fails a user's mesh
        logger.warning("capture: durable record failed for job %s (%s) - the mesh is unaffected",
                       job_id, exc)
        return
    if not result.trusted:
        logger.error("capture: operation %s for job %s is quarantined (%s) and will not be "
                     "exported", result.op_key[:16], job_id, result.outcome)


def add_event(job_id: str, name: str, payload: Any = None, *,
              kind: str = "event", attributes: dict | None = None) -> None:
    if not polcfg.MODES.data_collection_enabled or not job_id:
        return
    attrs = attributes or {}
    op_id = str(attrs.get("op_id", "") or "")
    if not op_id or payload is None:
        return
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "span_event",
        "trace_id": job_id,
        "name": name,
        "kind": kind,
        "attributes": attrs,
    }
    _record_durably(job_id, record, payload, op_id)
