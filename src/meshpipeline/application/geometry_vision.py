# Responsibility: Look at an uploaded geometry once, and store the words against the measurement of the same bytes.
# Owns: the decision to look, the call into the measurement package's one look entry point, and the updated row.
# Boundaries: it adds a description to a row that already exists; it measures nothing and blocks nothing.
# Collaborates with: application/geometry_measurement.py for the row and contracts/geometry_measurement.py for the queue seam.
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid as _uuid
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.geometry_measurement import STATUS_OK
from meshpipeline.contracts.geometry_source import GeometrySourceError, GeometrySourceRef

logger = logging.getLogger(__name__)

#: EVERY PATH IN THIS MODULE FAILS OPEN, and it fails open in BOTH directions. A look that refuses,
#: breaks, times out, finds no provider or cannot import its package leaves the stored measurement
#: byte for byte what the measurement wrote: `look` stays `{"status": "not_attempted", ...}`, the
#: planner block keeps no look key, intake renders no look lines, and the measurement still stores and
#: still reaches the planner. And the reverse: a look that succeeds changes nothing about the
#: measurement beside it. The two are separate jobs against one row on purpose.

#: NEVER IN THE REQUEST. The measurement runs inline for a small upload, because a conversation that
#: opens holding the port table is worth a second of the customer's wait. A look is not: it is a
#: provider call of 12 to 60 seconds behind a render, and nothing downstream is blocked by its absence.
#: So it is always a queued task, for every file size, and `geometry_survey` queues it only once the
#: row it attaches to has been written AND composed for what the customer said the part is for.

#: NO 0004. The look needs no migration, and that was checked rather than assumed. Revision 0003
#: gives `geometry_measurements.document` a JSONB column, and the look already has a key in that
#: document: `report_measured` has written `look` since the measurement path shipped, as
#: `look_block(None, status=LOOK_NOT_ATTEMPTED)` - the status is PASSED and not defaulted, because
#: `look_block(None)` on its own says `failed`, which is a look that was taken and brought nothing back.
#: Turning the look on fills that key in and recomposes
#: `planner_block` beside it, both inside the same JSONB value. No new column, no new index, no new
#: constraint, and no change to what `document_for` reads or what the sha256 guard compares.
#:
#: A column was considered for `look.status` and `look.seconds`, so an operator could count looks
#: and time them in SQL without opening the JSON. It was not added: it would duplicate a value that
#: is already in the row, and two spellings of one fact is the thing that goes out of step. A
#: `document->'look'->>'status'` expression index is the cheaper answer on the day the query is
#: slow, and it is a 0004 of its own when somebody actually needs it.

#: What is recorded in the log when the package is not in this image. Same distinction the measurement
#: makes: a deployment fact, not a fact about the file.
PACKAGE_ABSENT = "the geometry measurement package is not installed in this image, so nothing looked"


def _package():
    """The look entry point and the fact schema, imported the first time something asks.

    Deliberately not a module-level import, for the same reason the measurement's is not: an image
    that carries no look never reaches this function, and it costs such an image nothing.
    """
    from geometry_agent.agent import hexera
    from geometry_agent.facts.schema import GeometryFacts
    from geometry_agent.vision.look import look_at_geometry
    return look_at_geometry, GeometryFacts, hexera


def _facts_from_document(document: dict, GeometryFacts):
    """The measurement, rebuilt from the row rather than recomputed from the file.

    This is what makes the look cheap. `report_measured` stores `facts` verbatim, so the openings are
    already there: they become the labels drawn on the views, which is how the ids the model names are
    the ids the measurement named. None when the row predates that or the dump will not validate, and
    then the renderer detects the openings itself and the look is worth slightly less.
    """
    dump = document.get("facts")
    if not isinstance(dump, dict) or not dump:
        return None
    try:
        return GeometryFacts.model_validate(dump)
    except Exception as exc:                       # noqa: BLE001 - a look without facts is still a look
        logger.info("geometry look: the stored facts could not be rebuilt (%s); "
                    "the renderer will detect the openings itself", exc)
        return None


#: What is stored when the configured reader has no key in this environment. Said, not swallowed, so an
#: operator who switched the look on learns why nothing looked.
NO_READER = ("the configured reader ({provider}, {model}) has no key in this environment, so nothing looked; "
             "the look never falls through to another provider")


def reader():
    """The vision client the look uses: GEOMETRY_VISION_PROVIDER at GEOMETRY_VISION_MODEL, or None.

    NAMED, NEVER DISCOVERED. The package's `auto` provider takes Anthropic, then OpenAI, then
    DeepInfra, and this platform's own template carries only DeepInfra, so leaving it to `auto` reads
    every part with a model the look was never measured with. `with_model` is the package's own way to
    point one provider's client at a named model without reading the environment a second time.
    """
    from geometry_agent.vision import client as vision_client

    chosen = vision_client.vision_client_from_env(polcfg.GEOMETRY_VISION_PROVIDER or "off")
    if chosen is None:
        return None
    model = str(polcfg.GEOMETRY_VISION_MODEL or "").strip()
    return vision_client.with_model(chosen, model) if model else chosen


def look_at_local_file(path: Path, document: dict, *, timeout_s: float | None = None,
                       purpose: str | None = None, representation: str | None = None) -> dict | None:
    """Look at one local file and return the block to store, or None when nothing looked. Never raises.

    None means the caller writes nothing and the row keeps the measurement's own `look`. A block means
    the caller stores it, whatever its status: a failed look recorded as failed is worth more than a
    row that cannot tell a look that broke from a look that never ran.

    `purpose` and `representation` are what the customer's words decided, from the survey, when the
    survey is on. Without them the look is taken for the purpose the measurement assumed at upload,
    which is what it always was. The package's own note is why they matter: without the
    representation every part reads as internal flow and an external body comes back with invented
    ports.
    """
    try:
        look_at_geometry, GeometryFacts, hexera = _package()
    except Exception as exc:                       # noqa: BLE001 - absence is an outcome, not a crash
        logger.warning("geometry look: %s: %s", PACKAGE_ABSENT, exc)
        return None
    try:
        client = reader()
    except Exception as exc:                       # noqa: BLE001 - a reader that cannot be built did not look
        client = None
        logger.warning("geometry look: the reader could not be built (%s)", exc)
    if client is None:
        # Said in the LOG as well as on the row. A reader that cannot be BUILT warns above; a reader that
        # was never configured used to return this block in silence, so a dev deployment looking at
        # nothing had to be diagnosed from stored rows rather than from its own output. The sentence comes
        # from the one function that answers the question, so the log, the row and the deploy preflight
        # cannot disagree about why.
        logger.warning("geometry look: nothing looked: %s",
                       polcfg.vision_reader_has_no_key() or "the reader was not configured")
        # The same word the measurement's own block uses, read from the one module that owns it: a
        # reader that has to tell "nothing looked" from "the look failed" cannot do it if the two
        # halves of this platform spell the first one differently.
        from meshpipeline.application.geometry_measurement import LOOK_NOT_ATTEMPTED

        return hexera.look_block(None, status=LOOK_NOT_ATTEMPTED, reason=NO_READER.format(
            provider=polcfg.GEOMETRY_VISION_PROVIDER, model=polcfg.GEOMETRY_VISION_MODEL))

    deadline = float(timeout_s if timeout_s is not None else polcfg.GEOMETRY_VISION_TIMEOUT_SECONDS)
    facts = _facts_from_document(document, GeometryFacts)
    # A directory of this task's own, passed as an argument rather than set as an environment
    # variable: two looks in one worker process share one environment and cannot use it to mean two
    # directories, and a caller that sets and unsets it around a call is racing itself. It is
    # temporary because a worker's disk is not where a render belongs for thirty days, and what a
    # second job on the same bytes reads is the stored look, not a cached picture.
    with tempfile.TemporaryDirectory(prefix="geometry-look-cache-") as cache_dir:
        try:
            return look_at_geometry(path, facts, client=client, deadline_s=deadline,
                                    cache_root_dir=cache_dir,
                                    purpose=str(purpose or document.get("measured_purpose")
                                                or document.get("purpose") or "internal_cfd"),
                                    representation=representation or None)
        except Exception as exc:                   # noqa: BLE001 - the entry point promises not to, belt and braces
            logger.warning("geometry look: the look raised, which it is not supposed to: %s", exc)
            return None


def attach_look(document: dict, look: dict) -> dict:
    """The stored document with the look in it, and the planner's block recomposed around it.

    The block is recomposed rather than patched because `planner_block` is the package's own single
    definition of what a planner may read, and spelling any part of it a second time here is the thing
    `contracts/geometry_agent_block.py` exists to refuse. A package that cannot recompose it leaves the
    block the measurement wrote, which carries no look, which is what a planner sees today.
    """
    updated = dict(document)
    updated["look"] = look
    try:
        from geometry_agent.agent.hexera import planner_block
        recomposed = planner_block(updated)
    except Exception as exc:                       # noqa: BLE001 - never worth losing the look over
        logger.warning("geometry look: the planner block could not be recomposed (%s); "
                       "the stored block keeps the measurement's own", exc)
        return updated
    if isinstance(recomposed, dict) and recomposed:
        updated["planner_block"] = recomposed
    return updated


async def look_and_store(source_id: str, owner_id: str, *, timeout_s: float | None = None) -> dict:
    """Fetch the bytes this source names, look at them, and store the words in the measurement row.

    Never raises. The return value is a summary for the caller's log, not something a customer sees.
    """
    started = time.perf_counter()
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
        logger.warning("geometry look: malformed source id %r", source_id)
        return {"status": "skipped", "reason": "malformed source id"}

    try:
        async with get_db() as db:
            row = await GeometryMeasurementRepository().for_source(
                db, owner_id=owner_id, geometry_source_id=source_uuid)
            if row is None:
                # The measurement has not been written yet, or it was never attempted. Either way there
                # is nothing to attach words to, and a look with no measurement beside it is not a
                # thing this product has.
                return {"status": "skipped", "reason": "no measurement row for this source"}
            document = dict(row.document or {})
            purpose, status, reason = str(row.purpose or ""), str(row.status or ""), str(row.reason or "")
            sha256 = str(row.sha256 or "")
            facts_schema_version = int(row.facts_schema_version or 0)
            agent_git_sha = str(row.agent_git_sha or "")
            measure_seconds = row.measure_seconds
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry look: could not read the measurement row: %s", exc)
        return {"status": "skipped", "reason": "the measurement row could not be read"}

    if document.get("status") != STATUS_OK:
        return {"status": "skipped", "reason": "the measurement did not succeed, so there is nothing to describe"}

    # ALREADY LOOKED, and this is checked before anything else is read. It is the whole of "a second
    # job on the same file pays nothing": the look is stored with the measurement and keyed the same
    # way, so a re-queued task, a retry or a second job against the same upload stops at this line,
    # having read one row and fetched no bytes, rendered nothing and called no provider.
    stored = document.get("look")
    if isinstance(stored, dict) and stored.get("status") == "ok":
        return {"status": "cached", "source_id": str(source_id)}

    try:
        async with get_db() as db:
            source_row = await GeometrySourceRepository().get_for_owner(db, source_uuid, owner_id)
            if source_row is None:
                return {"status": "skipped", "reason": "no such source for this owner"}
            if getattr(source_row, "purged_at", None) is not None:
                return {"status": "skipped", "reason": "the bytes were purged at the end of retention"}
            ref = GeometrySourceRef.from_row(source_row)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry look: could not read the source row: %s", exc)
        return {"status": "skipped", "reason": "the source row could not be read"}

    # THE LOOK'S PURPOSE, NOT THE ROW'S. `purpose` above is what the facts in this row were measured
    # for, and it is written back unchanged below. Reusing the name for the survey's purpose wrote
    # `None[:32]` into the row with the survey off, so every look was paid for and then lost as
    # `unstored`, and with the survey on it relabelled facts measured for one purpose as another.
    look_purpose, representation = await _composed_for(owner_id, source_id, sha256)
    with tempfile.TemporaryDirectory(prefix="geometry-look-bytes-") as workspace:
        try:
            from meshpipeline.application.geometry_materializer import fetch_verified_bytes
            local = fetch_verified_bytes(ref, workspace=workspace, job_id=f"look:{source_id}")
        except GeometrySourceError as exc:
            logger.info("geometry look: the bytes are not available (%s)", exc)
            return {"status": "skipped", "reason": "the uploaded geometry could not be retrieved"}
        except Exception as exc:                   # noqa: BLE001
            logger.warning("geometry look: the bytes could not be retrieved: %s", exc)
            return {"status": "skipped", "reason": "the uploaded geometry could not be retrieved"}
        look = look_at_local_file(Path(local), document, timeout_s=timeout_s, purpose=look_purpose,
                                  representation=representation)

    if look is None:
        return {"status": "skipped", "reason": PACKAGE_ABSENT}

    updated = attach_look(document, look)
    try:
        async with get_db() as db:
            await GeometryMeasurementRepository().record(
                db, owner_id=owner_id, geometry_source_id=source_uuid, sha256=sha256,
                purpose=purpose, status=status, reason=reason, document=updated,
                facts_schema_version=facts_schema_version, agent_git_sha=agent_git_sha,
                measure_seconds=measure_seconds)
            await db.commit()
    except Exception as exc:                       # noqa: BLE001
        # A look nobody can read is the same as no look. The measurement row is untouched.
        logger.warning("geometry look: the row could not be updated - source_id=%s: %s", source_id, exc)
        return {"status": "unstored", "reason": "the look could not be written"}

    seconds = round(time.perf_counter() - started, 3)
    logger.info("geometry look: %s - source_id=%s sha=%s model=%s seconds=%s",
                look.get("status"), source_id, sha256[:12], look.get("model"), seconds)
    # WHATEVER THE LOOK DID, and not only when it landed. Step 4 has to see what step 3 saw: the questions only
    # the look can raise (a mouth the measurement did not find) are composed in now, with every answer already
    # given kept. And a look that FAILED is composed in for the opposite reason: the survey row records the
    # look's state, the builder's block says which of the four states it was, and gated on `ok` the row kept
    # saying `not_attempted` about a part whose look had broken. A failed look is not an absent one.
    from meshpipeline.application import geometry_survey
    await geometry_survey.recompose_after_look(str(source_id), owner_id, updated)
    return {"status": str(look.get("status") or "failed"), "source_id": str(source_id),
            "model": str(look.get("model") or ""), "seconds": seconds,
            "look_seconds": look.get("seconds")}


async def _composed_for(owner_id: str, source_id: str, sha256: str) -> tuple[str | None, str | None]:
    """The purpose and representation the customer's words decided, when the survey composed them.

    (None, None) before a survey has been composed, which is a look taken with nothing said about the
    part yet.
    """
    try:
        from meshpipeline.application import geometry_survey
        state = await geometry_survey.load(owner_id, str(source_id), sha256=sha256)
    except Exception:                              # noqa: BLE001 - a look is taken either way
        return None, None
    composed = (state or {}).get("composed_for") or {}
    return (str(composed.get("purpose") or "") or None,
            str(composed.get("representation") or "") or None)


def look_and_store_blocking(source_id: str, owner_id: str) -> dict:
    """The worker's entry point: the same work, on a thread with no running loop."""
    return asyncio.run(look_and_store(source_id, owner_id))


__all__ = ["NO_READER", "PACKAGE_ABSENT", "attach_look", "look_and_store", "look_and_store_blocking",
           "look_at_local_file", "reader"]
