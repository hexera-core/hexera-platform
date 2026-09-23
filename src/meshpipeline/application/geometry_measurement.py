# Responsibility: Measure an uploaded geometry once, and store what it is against the bytes it describes.
# Owns: the decision to measure now or later, the call into the measurement package, and the stored row.
# Boundaries: it produces a description; it decides nothing about the conversation and blocks nothing.
# Collaborates with: application/geometry_materializer.py for the bytes and contracts/geometry_measurement.py for the shape.
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid as _uuid
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.geometry_measurement import (
    MEASUREMENT_SCHEMA,
    STATUS_MEASUREMENT_FAILED,
    STATUS_OK,
    STATUS_REFUSED,
    enqueue_measurement,
)
from meshpipeline.contracts.geometry_source import GeometrySourceError, GeometrySourceRef

logger = logging.getLogger(__name__)

#: EVERY PATH IN THIS MODULE FAILS OPEN. Nothing here is allowed to change what a customer sees
#: unless it succeeds: an upload whose measurement refuses, breaks, times out or cannot import its
#: package returns exactly what it returns with the whole feature switched off. That is not
#: politeness, it is the condition under which this can be merged: the measurement is new, the
#: conversation is the product, and a description is never worth an upload.

#: The purpose a file is measured for when nobody has said yet. Only two fields of the measurement
#: depend on it - the mixed-scale warnings and the cell estimates - which is exactly why the
#: measurement can run at upload, before the customer has said what the file is for.
DEFAULT_PURPOSE = "internal_cfd"

#: How long the SYNCHRONOUS path may hold an HTTP request, whatever the configured deadline is. The
#: configured deadline governs a worker, which has all day; a customer waiting on an upload does
#: not. The real bound is the size threshold in front of it - 44 corpus files measure at a median
#: of 1.11 s and a p90 of 3.91 s, and the slowest, at 55,946 faces, takes 12.32 s - and this is the
#: ceiling for the file that is not like those. Past it the request returns and the row records a
#: measurement that did not finish.
SYNCHRONOUS_DEADLINE_CEILING_S = 60.0

#: What is recorded when the measurement package is not in this image. It is a deployment fact, not
#: a fact about the file, and it is written down rather than swallowed so an operator who turned
#: the setting on learns why nothing appeared.
PACKAGE_ABSENT = ("the geometry measurement package is not installed in this image, so nothing was "
                  "opened and nothing was measured")


class _Unavailable(RuntimeError):
    """The measurement package is not here. Distinct from a measurement that ran and failed."""


def _package():
    """The measurement package, imported the first time something asks for it.

    Deliberately not a module-level import. With the setting off this function is never called, so
    an image that does not carry the distribution imports nothing, costs nothing at startup and
    behaves exactly as it did before this module existed.

    The direction of this dependency is the one rule that does not bend: the platform may import
    the measurement package, and the measurement package never imports the platform. It reads
    platform source as text and pins the names it relies on in its own test, so a change here
    fails a test there before it fails a mesh.
    """
    try:
        from geometry_agent.agent import hexera
        from geometry_agent.agent import run as agent_run
        from geometry_agent.facts import measure as agent_measure
    except Exception as exc:                       # noqa: BLE001 - absence is an outcome, not a crash
        raise _Unavailable(str(exc)) from exc
    return hexera, agent_run, agent_measure


def _agent_git_sha() -> str:
    try:
        from geometry_agent.evaluate import agent_git_sha
        return str(agent_git_sha() or "")
    except Exception:                              # noqa: BLE001 - a stamp is not worth a failure
        return ""


def measure_local_file(path: Path, *, purpose: str = DEFAULT_PURPOSE, unit: str | None = None,
                       scale_to_metres: float | None = None, timeout_s: float | None = None,
                       source: dict | None = None) -> dict:
    """Measure one local file and return the document to store. Never raises.

    The document always carries `schema`, `status` and `reason`, because a step that cannot do its
    work writes a row saying so rather than returning an empty dict. `status` is `refused` when the
    package declined before opening the file, `measurement_failed` when it opened it and could not
    finish, and `ok` when there are measurements.
    """
    started = time.perf_counter()
    base = {"schema": MEASUREMENT_SCHEMA, "source": dict(source or {}), "measured_purpose": purpose}
    # THE PACKAGE'S OWN SWITCHES, put into the environment BEFORE the file is opened. `measure_isolated`
    # measures in a CHILD PROCESS, which inherits this environment and cannot be told anything else: a flag
    # set after the spawn would arrive too late and a flag set nowhere is a platform switch that does
    # nothing. Idempotent, and an operator's own export wins (`policy.arm_the_package`).
    polcfg.arm_the_package()

    def failed(status: str, reason: str) -> dict:
        return {**base, "status": status, "reason": reason, "plan": None,
                "seconds": round(time.perf_counter() - started, 3)}

    try:
        hexera, agent_run, agent_measure = _package()
    except _Unavailable as exc:
        logger.warning("geometry measurement: package unavailable: %s", exc)
        return failed(STATUS_REFUSED, PACKAGE_ABSENT)

    # The package's own file ceiling and its own words for refusing at it. Read from the package
    # rather than restated here, so one number moves one place.
    try:
        ceiling = int(agent_run.file_ceiling_bytes())
        size = path.stat().st_size
        if ceiling > 0 and size > ceiling:
            return failed(STATUS_REFUSED, agent_run.REFUSED_TOO_LARGE.format(
                mb=size / 1024 / 1024, cap=ceiling / 1024 / 1024, var=agent_run.MAX_FILE_MB_ENV))
    except OSError as exc:
        return failed(STATUS_MEASUREMENT_FAILED, f"the file could not be read: {exc}")

    deadline = float(timeout_s if timeout_s is not None else polcfg.GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS)
    with tempfile.TemporaryDirectory(prefix="geometry-measurement-") as cache_dir:
        try:
            if deadline > 0:
                facts = agent_measure.measure_isolated(path, purpose=purpose, cache_dir=cache_dir,
                                                       timeout_s=deadline)
            else:
                facts = agent_measure.measure(path, purpose=purpose, cache_dir=cache_dir)
        except Exception as exc:                   # noqa: BLE001 - any failure is a failed measurement
            logger.warning("geometry measurement: measurement failed: %s", exc)
            return failed(STATUS_MEASUREMENT_FAILED, _short(str(exc)))

        try:
            document = hexera.report_measured(
                facts, unit, None, purpose=purpose, scale_to_metres=scale_to_metres,
                # Nothing looked. The block is present with `not_attempted` rather than absent, so
                # a reader never has to tell "no look" from "a look that found nothing".
                look=hexera.look_block(None),
                stamp={"agent_git_sha": _agent_git_sha()})
        except Exception as exc:                   # noqa: BLE001 - a measurement that cannot be written down
            logger.warning("geometry measurement: the report could not be composed: %s", exc)
            return failed(STATUS_MEASUREMENT_FAILED, _short(str(exc)))

    # `source` becomes the upload's own record - digest, size, filename, suffix - replacing the
    # local path the measurement package names its input by. A stored document is read on another
    # machine and long after the temporary directory is gone, so a path is at best meaningless
    # there and at worst a server detail in a row a customer's conversation reads from.
    document.update(base)
    facts_dump = document.get("facts")
    if isinstance(facts_dump, dict):
        facts_dump["source_path"] = ""
    document["status"] = STATUS_OK
    document["reason"] = ""
    document["seconds"] = round(time.perf_counter() - started, 3)
    return document


def _short(text: str, limit: int = 480) -> str:
    """One sentence for a row, never a stack trace and never a path a provider message might carry."""
    one = " ".join(str(text or "").split())
    return one[:limit]


async def measure_and_store(source_id: str, owner_id: str, *,
                            purpose: str = DEFAULT_PURPOSE,
                            timeout_s: float | None = None) -> dict:
    """Fetch the bytes this source names, measure them, and store the result. Never raises.

    The return value is a summary for the caller's log, not something a customer sees.
    """
    from meshpipeline.persistence.repositories.geometry_measurement_repository import (
        GeometryMeasurementRepository,
    )
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    from meshpipeline.persistence.session import get_db

    try:
        source_uuid = _uuid.UUID(str(source_id))
    except (TypeError, ValueError):
        logger.warning("geometry measurement: malformed source id %r", source_id)
        return {"status": "skipped", "reason": "malformed source id"}

    try:
        async with get_db() as db:
            row = await GeometrySourceRepository().get_for_owner(db, source_uuid, owner_id)
            if row is None:
                return {"status": "skipped", "reason": "no such source for this owner"}
            if getattr(row, "purged_at", None) is not None:
                # Retention expiry, not corruption: the same distinction the materializer already
                # makes. There is nothing to measure and nothing is wrong.
                return {"status": "skipped", "reason": "the bytes were purged at the end of retention"}
            ref = GeometrySourceRef.from_row(row)
            unit, scale = await _interpretation_for(db, owner_id, source_uuid)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry measurement: could not read the source row: %s", exc)
        return {"status": "skipped", "reason": "the source row could not be read"}

    source_facts = {"sha256": ref.sha256, "size_bytes": ref.size_bytes,
                    "suffix_hint": ref.suffix_hint, "original_filename": ref.original_filename,
                    "source_id": ref.source_id, "owner_id": ref.owner_id}

    with tempfile.TemporaryDirectory(prefix="geometry-measurement-bytes-") as workspace:
        try:
            from meshpipeline.application.geometry_materializer import fetch_verified_bytes
            local = fetch_verified_bytes(ref, workspace=workspace, job_id=f"measure:{source_id}")
        except GeometrySourceError as exc:
            # The materializer's own sentence, which already separates retention expiry from
            # corruption from a store that could not be reached. A third vocabulary is not invented.
            document = {"schema": MEASUREMENT_SCHEMA, "status": STATUS_MEASUREMENT_FAILED,
                        "reason": _short(str(exc)), "plan": None, "source": source_facts}
        except Exception as exc:                   # noqa: BLE001
            logger.warning("geometry measurement: the bytes could not be retrieved: %s", exc)
            document = {"schema": MEASUREMENT_SCHEMA, "status": STATUS_MEASUREMENT_FAILED,
                        "reason": "the uploaded geometry could not be retrieved", "plan": None,
                        "source": source_facts}
        else:
            document = measure_local_file(Path(local), purpose=purpose, unit=unit,
                                          scale_to_metres=scale, timeout_s=timeout_s,
                                          source=source_facts)

    raw_stamp = document.get("stamp")
    stamp: dict = raw_stamp if isinstance(raw_stamp, dict) else {}
    raw_seconds = document.get("seconds")
    seconds = float(raw_seconds) if isinstance(raw_seconds, (int, float)) else None
    try:
        async with get_db() as db:
            await GeometryMeasurementRepository().record(
                db, owner_id=owner_id, geometry_source_id=source_uuid, sha256=ref.sha256,
                purpose=purpose, status=str(document.get("status") or STATUS_MEASUREMENT_FAILED),
                reason=str(document.get("reason") or ""), document=document,
                facts_schema_version=int(stamp.get("facts_schema_version") or 0),
                agent_git_sha=str(stamp.get("agent_git_sha") or ""),
                measure_seconds=seconds)
            await db.commit()
    except Exception as exc:                       # noqa: BLE001
        # A measurement nobody can read is the same as no measurement. Loud in the log, silent to
        # the customer.
        logger.warning("geometry measurement: the row could not be written - source_id=%s: %s",
                       source_id, exc)
        return {"status": "unstored", "reason": "the measurement row could not be written"}

    logger.info("geometry measurement: %s - source_id=%s sha=%s seconds=%s",
                document.get("status"), source_id, ref.sha256[:12], document.get("seconds"))
    try:
        looked = _queue_the_look(str(source_id), owner_id, str(document.get("status") or ""))
    except Exception as exc:                       # noqa: BLE001 - a description is never worth a measurement
        logger.warning("geometry look: could not be queued - source_id=%s: %s", source_id, exc)
        looked = "skipped"
    return {"status": document.get("status"), "source_id": str(source_id),
            "seconds": document.get("seconds"), "look": looked}


def _queue_the_look(source_id: str, owner_id: str, status: str) -> str:
    """Hand the look to a worker, now that there is a row for it to attach to. Never raises.

    HERE, and only here. Both paths into a measurement come through `measure_and_store`, so one call
    site covers the upload that measured inline and the upload that went to a worker, and in both cases
    the row is already committed when this runs. Queueing it from the upload instead would race: the
    look would arrive at a row that does not exist yet and skip.

    Returns what happened, for the caller's log line: `off`, `queued`, `skipped` or `not_measured`.
    """
    from meshpipeline.application import geometry_vision

    if not geometry_vision.look_enabled():
        return "off"
    if status != STATUS_OK:
        # Nothing to describe. A failed measurement has no facts to label the views with and no
        # document for the words to sit beside.
        return "not_measured"
    if polcfg.GEOMETRY_SURVEY_ENABLED:
        # STEP 3 COMES AFTER STEP 1. With the survey on, the look waits for the customer to say what
        # the part is for: a look taken for the purpose assumed at upload reads an external body as
        # internal flow. `application/geometry_survey.py` queues it the moment the measurement has
        # been composed for their purpose, which is the chain's order.
        return "deferred_to_survey"
    from meshpipeline.contracts.geometry_measurement import enqueue_look
    return "queued" if enqueue_look(source_id, owner_id) else "skipped"


async def _interpretation_for(db, owner_id: str, source_uuid) -> tuple[str | None, float | None]:
    """The unit the PLATFORM recorded for these bytes, when the file declared one credibly.

    This is the only place a scale may come from. It is never taken from a model's unit call: the
    scale is a physical fact about the file and the platform owns it, and an absent interpretation
    is a legitimate, common outcome - an STL declares nothing - that the report states as an
    unknown rather than filling with a guess.
    """
    try:
        from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
            GeometryInterpretationRepository,
        )
        found = await GeometryInterpretationRepository().latest_for_source(
            db, owner_id=owner_id, geometry_source_id=source_uuid)
        if found is None:
            return None, None
        return str(found.unit.value), float(found.scale_to_metres)
    except Exception as exc:                       # noqa: BLE001 - measuring without a unit is fine
        logger.info("geometry measurement: no interpretation available: %s", exc)
        return None, None


def measure_and_store_blocking(source_id: str, owner_id: str, *,
                               purpose: str = DEFAULT_PURPOSE) -> dict:
    """The worker's entry point: the same work, on a thread with no running loop."""
    return asyncio.run(measure_and_store(source_id, owner_id, purpose=purpose))


async def on_upload(source_id: str, owner_id: str, *, size_bytes: int,
                    purpose: str = DEFAULT_PURPOSE) -> str:
    """Measure this upload now, hand it to a worker, or do nothing at all. Never raises.

    Returns what happened, for the upload's own log line: `off`, `measured`, `queued` or `skipped`.

    SMALL FILES ARE MEASURED IN THE REQUEST because the whole point is that the conversation opens
    holding the table rather than asking for what the file already answers; a measurement that
    lands after the first question is worth much less. Large ones go to a worker, because the only
    cost known before a file is opened is its size and a customer will not wait on the tail.
    """
    if not polcfg.GEOMETRY_MEASUREMENT_ENABLED:
        return "off"
    threshold = polcfg.GEOMETRY_MEASUREMENT_SYNC_MAX_MB * 1024 * 1024
    if threshold > 0 and int(size_bytes) < threshold:
        deadline = min(float(polcfg.GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS),
                       SYNCHRONOUS_DEADLINE_CEILING_S)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_run_in_thread, str(source_id), owner_id, purpose, deadline),
                timeout=deadline + 5.0)
            return "measured"
        except TimeoutError:
            logger.warning("geometry measurement: the inline measurement outlived %.0fs - "
                           "source_id=%s; the conversation is unaffected", deadline, source_id)
            return "skipped"
        except Exception as exc:                   # noqa: BLE001 - an upload is never failed for this
            logger.warning("geometry measurement: the inline measurement failed - source_id=%s: %s",
                           source_id, exc)
            return "skipped"
    return "queued" if enqueue_measurement(str(source_id), owner_id, purpose=purpose) else "skipped"


def _run_in_thread(source_id: str, owner_id: str, purpose: str, timeout_s: float) -> dict:
    # A separate loop on a worker thread, because the measurement is a native tessellation in a
    # child process and the request's own loop must stay free to serve everything else.
    return asyncio.run(measure_and_store(source_id, owner_id, purpose=purpose, timeout_s=timeout_s))
