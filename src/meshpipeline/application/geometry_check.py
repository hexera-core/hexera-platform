# Responsibility: Run the geometry check for an uploaded part in two steps - the scout, which
# fetches the file, measures it, pictures it with numbered stickers and stores the skin the moment
# the upload lands; and the naming, which runs once the user has said what the part is, hands the
# pictures and their words to a vision model, and stores the proposal the user confirms.
# Boundaries: orchestration. The geometry is cad/scout (exact CAD) and cad/scout_mesh (triangle
# files), the pictures render/scout_snapshots, the model call goes through the router. This module
# sequences them and never decides for the user: whatever it stores is a proposal until the confirm
# endpoint records what the user said.
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: pending -> scouted (measured, pictured, code names) -> ready (named with the user's words);
#: failed and unsupported are terminal and say why.
STATUS_PENDING, STATUS_SCOUTED, STATUS_READY = "pending", "scouted", "ready"
STATUS_FAILED, STATUS_UNSUPPORTED = "failed", "unsupported"
#: THE WORKER'S LIMITS for the two steps, in seconds, FOR A SMALL FILE. Past the soft one the task
#: is told to stop and stores what it has itself; past the hard one it is killed and stores
#: nothing - and a worker whose VM is deleted mid-task stores nothing either.
#: NO GUESSED CLOCK BLOCKS A CLEAN FILE (jubilee_frame, a 46 MB printer frame of 145 solids, was
#: failed at 22 min by these numbers while the worker was still working on it): the limits a check
#: runs under grow with the file (`step_limits`), are passed to the task when it is queued and
#: written on the pending marker, so the time box the API reads the check through (`time_boxed`)
#: holds the check to its own numbers - and while the worker's heartbeat is fresh a check is never
#: called stuck at all. These numbers are only the backstop for a call that never returns.
SCOUT_SOFT_LIMIT_S, SCOUT_HARD_LIMIT_S = 900, 1200
NAMING_SOFT_LIMIT_S, NAMING_HARD_LIMIT_S = 600, 900
#: What each megabyte of upload adds to a step's limits (reading, tessellating and measuring a
#: STEP grow with its size: about 1 s/MB to read, minutes for an assembly of hundreds of solids).
SCOUT_S_PER_MB, NAMING_S_PER_MB = 60.0, 30.0
#: The backstop's ceiling, whatever the size: a step past it is a call that will never return.
LIMIT_CEILING_S = 6 * 3600
#: How long past its hard limit a step is still believed to be running: queue time on a busy
#: fleet, and clock skew between the API and the worker.
STALE_MARGIN_S = 120.0


def step_limits(step: str, size_bytes: float | int | None) -> tuple[int, int]:
    """(soft, hard) seconds for a scout or naming step on a file this big: the small-file limits
    plus `*_S_PER_MB` for every megabyte, hard a fixed margin past soft, under LIMIT_CEILING_S. A
    file whose size is unknown gets the small-file limits."""
    if step == RETRY_NAMING:
        soft0, hard0, per_mb = NAMING_SOFT_LIMIT_S, NAMING_HARD_LIMIT_S, NAMING_S_PER_MB
    else:
        soft0, hard0, per_mb = SCOUT_SOFT_LIMIT_S, SCOUT_HARD_LIMIT_S, SCOUT_S_PER_MB
    try:
        mb = max(0.0, float(size_bytes or 0) / 1e6)
    except (TypeError, ValueError):
        mb = 0.0
    extra = per_mb * mb
    soft = int(min(soft0 + extra, LIMIT_CEILING_S - (hard0 - soft0)))
    return soft, soft + (hard0 - soft0)


def limits_of(stored: dict | None, step: str = "scout") -> tuple[float, float]:
    """The (soft, hard) limits a stored check runs under: the ones written on its marker when it
    was queued, else the small-file limits (a marker written before the limits grew with the file)."""
    s = stored or {}
    soft0, hard0 = step_limits(step, None)
    prefix = "naming_" if step == RETRY_NAMING else ""
    try:
        soft = float(s.get(f"{prefix}soft_limit_s") or soft0)
        hard = float(s.get(f"{prefix}hard_limit_s") or hard0)
    except (TypeError, ValueError):
        soft, hard = float(soft0), float(hard0)
    return soft, hard
#: How long the naming step waits for a scout that is still running before giving up. Under its
#: own soft limit on purpose: a wait that outlived the limit was ended by the worker with a
#: `failed` written over the scout's `pending`, instead of the request being withdrawn so the
#: next chat turn can ask again once the scout has landed.
NAMING_WAIT_S = float(NAMING_SOFT_LIMIT_S - 60)
#: The plain reasons a time-boxed check reports, and the step a retry runs again.
SCOUT_TOO_LONG = "the drawing took too long"
NAMING_TOO_LONG = "the naming step did not answer"
#: ...and when the worker stopped beating mid-step (killed, or its VM went away).
SCOUT_STOPPED = "the worker drawing your part stopped"
NAMING_STOPPED = "the worker naming your part stopped"
RETRY_SCOUT, RETRY_NAMING = "scout", "naming"
#: What write_status stamps on every record; what a re-store of a record leaves out.
_STAMPED_KEYS = ("status", "written_at", "seconds", "session_id")
#: What a failure adds to a record; what a retry takes off it again.
_FAILURE_KEYS = ("reason", "retry", "step", "timed_out")


def check_object_key(session_id: str, name: str) -> str:
    """Where a session's geometry check lives in the object store: one folder per session,
    `scout.json` beside the pictures and the skin, `naming.json` once the user's words were
    handed to the model, `confirmed.json` once the user has answered."""
    return f"sessions/{session_id}/geometry_check/{name}"


def jsonable(value):
    """The payload with every numpy scalar and array turned into the plain Python it stands for.
    The mesh scout measures with numpy, and one stray numpy bool in a face record made the store
    refuse the whole check - and because the status write failed too, the stage waited forever."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is a hard dependency of the scout
        np = None
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if np is not None:
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, np.ndarray):
            return jsonable(value.tolist())
    return value


def _store_json(object_key: str, payload: dict) -> None:
    from meshpipeline.contracts.object_storage import get_object_store

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(jsonable(payload), fh)
        tmp = Path(fh.name)
    try:
        get_object_store().upload_file(local_path=tmp, object_key=object_key)
    finally:
        tmp.unlink(missing_ok=True)


def read_check(session_id: str) -> dict | None:
    """The stored check as it stands, or None before the scout was queued."""
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        return json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "scout.json")))
    except ObjectNotFound:
        return None


def write_status(session_id: str, status: str, **fields) -> None:
    """A small JSON marker so the API can tell 'not started' from 'still running'."""
    _store_json(check_object_key(session_id, "scout.json"),
                {"status": status, "session_id": session_id, "written_at": time.time(), **fields})


def _failed_record(reason: str, step: str, current: dict | None) -> dict:
    """What a failed step stores: the reason, the step, and which step a retry runs - and, for a
    naming that failed, everything the scout had stored, so the stage keeps the part and its
    labels for the user to fix by hand and a retry has facts to name."""
    record = {"reason": reason, "named": False, "step": step,
              "retry": RETRY_NAMING if step == RETRY_NAMING else RETRY_SCOUT}
    if step == RETRY_NAMING and current is not None and current.get("status") == STATUS_SCOUTED:
        record = {**{k: v for k, v in current.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS}, **record}
    return record


def store_status(session_id: str, status: str, task_step: str, **fields) -> dict:
    """`write_status` for the end of a task. A write that breaks is itself stored, as a failed
    check with the exception's class and one plain sentence, so no task ends on the status it
    started with: one stray numpy bool in a face record once made the store refuse the scouted
    result, and because nothing caught that the stage waited forever on `pending`. Returns the
    record that was stored - the failed one when the write broke - and raises nothing; when even
    the minimal record cannot be stored, the read-side time box is what tells the user."""
    try:
        write_status(session_id, status, **fields)
        # THE TASK'S RESULT IS PLAIN JSON TOO: the worker hands it to Celery's JSON encoder, and one numpy
        # integer in a fact (the clean check's body numbers, 26 Oct) ended the scout task "raised
        # unexpected: EncodeError" although its record was stored (the CFD and FEA scouts alike)
        return jsonable({"status": status, **fields})
    except Exception as exc:  # noqa: BLE001 - said and stored, never raised out of the task
        logger.exception("geometry check: could not store the %s result - session=%s", status, session_id)
        reason = f"{type(exc).__name__}: the {task_step} result could not be stored"
        try:
            write_status(session_id, STATUS_FAILED, **_failed_record(reason, task_step, read_check(session_id)))
        except Exception:  # noqa: BLE001 - the store itself is what broke
            logger.warning("geometry check: could not store the failure either - session=%s", session_id)
        return {"status": STATUS_FAILED, "reason": reason, "named": False, "step": task_step}


def naming_pending(requested: dict | None) -> bool:
    """Whether a naming is on its way: asked for, and not withdrawn."""
    return requested is not None and not requested.get("withdrawn")


def mark_naming_withdrawn(session_id: str) -> None:
    """The naming gave up waiting for the scout. The marker stays, marked withdrawn, so the chat
    asks again once the part is measured - and never holds again for a scout that is still not
    there. (`clear_naming_request` is the other withdrawal: a turn that was never stored.)"""
    current = naming_requested(session_id) or {"session_id": session_id}
    _store_json(check_object_key(session_id, "naming.json"),
                {**current, "withdrawn": True, "withdrawn_at": time.time()})


def time_boxed(stored: dict | None, requested: dict | None, now: float | None = None,
               progress: dict | None = None) -> dict | None:
    """THE CHECK AS IT SHOULD BE REPORTED NOW. A scout still `pending` whose worker has stopped
    beating (geometry_progress: the worker's heartbeat) was killed, or lost its VM: nothing will
    ever write its result, so it is reported `failed` with a plain reason and the step a retry
    runs. A `scouted` check whose naming's worker stopped the same way is reported failed too,
    with the scout's proposal and skin still on it so the user can fill the names in by hand.

    PROGRESS, NOT A CLOCK: while the worker's heartbeat is fresh the check is working, however
    long a big file takes - its own limits (written on its marker, sized to the file) end it if a
    call never returns, and the beat stops then. Without a heartbeat (no worker has picked it up,
    or a worker from before the heartbeat) the check is held to its own hard limit plus the margin.
    Pure, and the one rule every reader applies; it stores nothing, so a worker that was merely
    slow still lands its result and the next read serves it."""
    from meshpipeline.application import geometry_progress as gp

    if stored is None:
        return None
    at = time.time() if now is None else now
    status = stored.get("status")
    rec = progress if isinstance(progress, dict) else {}
    if status == STATUS_PENDING:
        since = float(stored.get("written_at") or 0.0)
        scout = rec.get("scout") if isinstance(rec.get("scout"), dict) else None
        beating = gp.beating(scout, at, marker=since) if scout is not None else None
        if beating is False:
            return {**stored, "status": STATUS_FAILED, "reason": SCOUT_STOPPED, "named": False,
                    "step": RETRY_SCOUT, "retry": RETRY_SCOUT}
        if beating is None and at - since > limits_of(stored)[1] + STALE_MARGIN_S:
            return {**stored, "status": STATUS_FAILED, "reason": SCOUT_TOO_LONG, "named": False,
                    "step": RETRY_SCOUT, "retry": RETRY_SCOUT, "timed_out": True}
    elif status == STATUS_SCOUTED and is_preview(stored):
        # A PREVIEW WHOSE SCOUT STOPPED (killed past its limit, or its VM went away) is the
        # check's result: the quick drawing stands, said so, and the user goes on with it
        scout = rec.get("scout") if isinstance(rec.get("scout"), dict) else None
        beating = gp.beating(scout, at) if scout is not None else None
        since = float(stored.get("written_at") or 0.0)
        if beating is False:
            return kept_preview(stored, SCOUT_STOPPED)
        if beating is None and at - since > limits_of(stored)[1] + STALE_MARGIN_S:
            return kept_preview(stored, SCOUT_TOO_LONG)
    elif status == STATUS_SCOUTED and naming_pending(requested):
        asked = float((requested or {}).get("requested_at") or 0.0)
        naming = rec.get("naming") if isinstance(rec.get("naming"), dict) else None
        if naming is not None and float(naming.get("picked_at") or 0.0) < asked - 30.0:
            naming = None                                 # an earlier naming's record
        beating = gp.beating(naming, at) if naming is not None else None
        if beating is False:
            return {**stored, "status": STATUS_FAILED, "reason": NAMING_STOPPED, "named": False,
                    "step": RETRY_NAMING, "retry": RETRY_NAMING}
        if beating is None and at - asked > limits_of(requested, RETRY_NAMING)[1] + STALE_MARGIN_S:
            return {**stored, "status": STATUS_FAILED, "reason": NAMING_TOO_LONG, "named": False,
                    "step": RETRY_NAMING, "retry": RETRY_NAMING, "timed_out": True}
    return stored


def retry_step(reported: dict | None) -> str | None:
    """Which step a retry runs for a check as reported: the scout for one that never measured the
    part, the naming for one that did; None when there is nothing to run again."""
    if not reported:
        return None
    status = reported.get("status")
    if status == STATUS_PENDING:
        return RETRY_SCOUT
    if status == STATUS_SCOUTED:
        return RETRY_NAMING
    if status == STATUS_FAILED:
        if reported.get("retry") in (RETRY_SCOUT, RETRY_NAMING):
            return str(reported["retry"])
        return RETRY_NAMING if reported.get("step") == RETRY_NAMING or reported.get("facts") else RETRY_SCOUT
    return None


def start_scout(session_id: str, owner_id: str, *, source: dict, interpretation: dict | None,
                where: str = "upload") -> bool:
    """Queue the scout and leave the `pending` marker - the marker BEFORE the task is published,
    so a worker that finishes first is never overwritten by a "pending" nothing would replace.
    A scout nobody will run is marked failed, never left pending. True when it was queued;
    never raises, because the upload (or the retry) stands whatever the queue does."""
    from meshpipeline.contracts.geometry_check import enqueue_scout

    # THE SAME BYTES, CHECKED BEFORE BY THE SAME OWNER (application/geometry_reuse): the check is answered
    # at once from the earlier one and nothing is queued - a re-upload never waits behind other checks
    try:
        from meshpipeline.application.geometry_reuse import reuse_scout

        reused = reuse_scout(session_id, owner_id, source, interpretation)
        if reused is not None:
            write_status(session_id, STATUS_SCOUTED, **reused)
            logger.info("geometry check answered from an earlier check of the same file (from the %s) - "
                        "session_id=%s", where, session_id)
            return True
    except Exception as exc:  # noqa: BLE001 - checked as usual
        logger.info("geometry check: no earlier check reused (%s) - session_id=%s", type(exc).__name__, session_id)

    marked = False
    try:
        # the limits this check runs under, sized to the file: the task is queued with the same
        # numbers (runtime/composition), and the time box reads them back from this marker
        size = (source or {}).get("size_bytes")
        soft, hard = step_limits(RETRY_SCOUT, size)
        write_status(session_id, STATUS_PENDING, soft_limit_s=soft, hard_limit_s=hard,
                     size_bytes=size)
        marked = True
        if not enqueue_scout(session_id=session_id, owner_id=owner_id, source=source,
                             interpretation=interpretation):
            raise RuntimeError("no worker is configured to run the geometry check")
        logger.info("geometry check queued from the %s - session_id=%s", where, session_id)
        return True
    except Exception as exc:  # noqa: BLE001 - the upload stands; the intake will ask instead
        logger.warning("geometry check could not be queued from the %s (%s: %s) - session_id=%s",
                       where, type(exc).__name__, exc, session_id)
        if marked:
            # a "pending" nobody will finish would keep the console waiting; say so instead
            try:
                write_status(session_id, STATUS_FAILED, named=False, step=RETRY_SCOUT, retry=RETRY_SCOUT,
                             reason="the geometry check could not be started; the intake will ask instead")
            except Exception:  # noqa: BLE001
                logger.warning("geometry check: could not mark the check failed - session_id=%s", session_id)
        return False


def restart_naming(session_id: str, owner_id: str, *, purpose_text: str,
                   interpretation: dict | None) -> bool:
    """Run the naming again over the scout's facts. A failed naming's record goes back to
    `scouted` (its facts, proposal and skin are still on it), the request marker is stamped
    afresh so the time box starts again, and the naming is queued. False when the record has no
    facts to name, or nothing is configured to run it."""
    from meshpipeline.contracts.geometry_check import enqueue_naming

    stored = read_check(session_id)
    if stored is None or not stored.get("facts"):
        return False
    was_failed = stored.get("status") != STATUS_SCOUTED
    if was_failed:
        # back to scouted BEFORE the task is published: a worker that starts at once must find
        # a check it names, not one it skips as failed
        write_status(session_id, STATUS_SCOUTED,
                     **{k: v for k, v in stored.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS})
    try:
        queued = enqueue_naming(session_id=session_id, owner_id=owner_id, purpose_text=purpose_text,
                                interpretation=interpretation)
    except Exception as exc:  # noqa: BLE001 - the broker's weather; the check must not be left half-way
        logger.warning("geometry naming could not be queued again (%s: %s) - session_id=%s",
                       type(exc).__name__, exc, session_id)
        queued = False
    if not queued:
        if was_failed:
            # the failure goes back as it was, so the read keeps reporting it with its way on,
            # instead of a check waiting for a naming nobody queued
            write_status(session_id, str(stored["status"]),
                         **{k: v for k, v in stored.items() if k not in _STAMPED_KEYS})
        return False
    mark_naming_requested(session_id, purpose_text)
    logger.info("geometry naming queued again - session_id=%s", session_id)
    return True


def naming_requested(session_id: str) -> dict | None:
    """The marker the API leaves when it hands the user's first answer to the naming step, so a
    second message never queues a second naming. None until then."""
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        return json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "naming.json")))
    except ObjectNotFound:
        return None


def mark_naming_requested(session_id: str, purpose_text: str) -> None:
    soft, hard = naming_limits_for(session_id)
    _store_json(check_object_key(session_id, "naming.json"),
                {"session_id": session_id, "purpose_text": purpose_text[:2000], "requested_at": time.time(),
                 "naming_soft_limit_s": soft, "naming_hard_limit_s": hard})


def checked_size_bytes(stored: dict | None) -> int | None:
    """How big the checked upload is, as its record says: the pending marker's size, else the
    scouted record's source. None when it does not say."""
    s = stored or {}
    size = s.get("size_bytes") or (s.get("source") or {}).get("size_bytes")
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def naming_limits_for(session_id: str) -> tuple[int, int]:
    """The naming's (soft, hard) limits for this session's upload, sized to its file. Never
    raises: a record that cannot be read gets the small-file limits."""
    try:
        size = checked_size_bytes(read_check(session_id))
    except Exception:  # noqa: BLE001 - the limits are a backstop; the small-file ones stand in
        size = None
    return step_limits(RETRY_NAMING, size)


def clear_naming_request(session_id: str) -> None:
    """Withdraw the marker, so a naming that could not run leaves the next chat turn free to
    ask again instead of a session that waits for a stage that never comes."""
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        get_object_store().delete_object(object_key=check_object_key(session_id, "naming.json"))
    except ObjectNotFound:
        pass
    except Exception as exc:  # noqa: BLE001 - said, not raised: the naming's own outcome matters more
        logger.warning("geometry naming: could not clear the request marker for %s (%s)", session_id, exc)


def skin_payload(skin_stl: Path) -> dict:
    """The part's skin as a CAD viewer draws it: shared vertices, smooth normals that break at
    sharp corners, and the sharp edges as lines - in the shape the mesh viewer reads (its polyMesh
    form, plus normals), so the console draws the part with the renderer it draws a delivered
    mesh with. It is marked as the input skin, never as a mesh."""
    import numpy as np

    from meshpipeline.cad.stl_io import read_stl_triangles
    from meshpipeline.render.skin_mesh import edges_block, prepare_skin, simplified, skin_patch

    tris = np.asarray(read_stl_triangles(Path(skin_stl)), dtype=float).reshape(-1, 3, 3)
    if len(tris) == 0:
        raise RuntimeError("the part's skin has no triangles to draw")
    # A BIG SKIN IS DRAWN SIMPLIFIED (a scanned car of millions of triangles): the stage turns it
    # smoothly and it is ready in seconds; the measuring reads the full skin
    drawn = simplified(tris)
    prepared = prepare_skin(drawn)
    payload = {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
               "patches": [skin_patch(prepared)], "edges": edges_block(prepared)}
    if len(drawn) < len(tris):
        payload["simplified_from"] = int(len(tris))
    return payload


# ------------------------------------------------------------------------------ the scout ----
def run_geometry_check(*, session_id: str, owner_id: str, source: dict,
                       interpretation: dict | None = None, purpose_text: str = "") -> dict:
    """The scout task body: measure, picture, store. Returns the stored result; every failure is
    stored too, as a status the console can show, because a check that silently never arrives is
    worse than one that says it could not read the part. `purpose_text` is accepted for the
    older single-step callers and ignored: naming has its own step now."""

    started = time.time()
    # THE LIVE LINE: each step is recorded as it starts, so the chat says what is happening now -
    # and the worker's heartbeat beside it, so a long check on a big file is never taken for a
    # dead one (time_boxed)
    tracker = _scout_tracker(session_id, source)
    tracker.start_heartbeat()
    try:
        return _run_scout(session_id, owner_id, source, interpretation, tracker, started)
    finally:
        tracker.stop_heartbeat()          # whatever happened, the beat stops with the task


def _run_scout(session_id: str, owner_id: str, source: dict, interpretation: dict | None,
               tracker, started: float) -> dict:
    from meshpipeline.application import geometry_progress as gp

    token = gp.track(tracker)
    preview = _Preview(session_id)
    try:
        result = _scout(session_id=session_id, owner_id=owner_id, source=source,
                        interpretation=interpretation, preview=preview)
    except _Unsupported as exc:
        result = {"status": STATUS_UNSUPPORTED, "reason": str(exc), "named": False}
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry scout failed - session=%s", session_id)
        reason = SCOUT_TOO_LONG if _out_of_time(exc) else f"{type(exc).__name__}: {str(exc)[:200]}"
        if preview.record is not None:
            # THE PART WAS ALREADY SHOWN: what failed was the full reading, never the user's way
            # on. The quick drawing stays, scouted, with the reason said, and the user confirms
            # the size and unit on it and goes on to the mesh.
            result = preview.kept(reason)
        else:
            result = {"status": STATUS_FAILED, "reason": reason,
                      "named": False, "step": RETRY_SCOUT, "retry": RETRY_SCOUT}
            if _out_of_time(exc):
                # the worker's soft limit: said as the time box says it, not as the exception's name
                result["timed_out"] = True
    finally:
        gp.untrack(token)
    result["seconds"] = round(time.time() - started, 1)
    if preview.record is not None and result.get("status") == STATUS_SCOUTED:
        moved_on = preview.moved_on()
        if moved_on is not None and moved_on.get("status") == STATUS_READY and moved_on.get("named_on_preview"):
            # the naming named the preview before this full reading landed: the full reading is
            # merged under the naming's answer, never dropped
            full = {**result, "status": STATUS_SCOUTED}
            merged = merged_onto_full({k: v for k, v in moved_on.items() if k not in _STAMPED_KEYS},
                                      {"facts": (preview.record or {}).get("facts") or {}}, full)
            stored = store_status(session_id, STATUS_READY, RETRY_NAMING, **merged)
            tracker.finish(ok=True)
            logger.info("geometry scout: the full reading merged under the naming - session=%s in %.1fs",
                        session_id, result["seconds"])
            return stored
        if moved_on is not None:
            # something else moved the check on (a failed naming the user is acting on): it is not
            # replaced under them
            tracker.finish(ok=True)
            logger.info("geometry scout: the full reading landed after the naming - kept the %s record, "
                        "session=%s in %.1fs", moved_on.get("status"), session_id, result["seconds"])
            return moved_on
    # THE WRITE IS PART OF THE TASK: a result that cannot be stored is stored as a failure, so the
    # stage never waits on a `pending` nothing will replace
    stored = store_status(session_id, result.pop("status"), RETRY_SCOUT, **result)
    tracker.finish(ok=stored.get("status") == STATUS_SCOUTED)
    if stored.get("status") == STATUS_SCOUTED and not stored.get("preview") and not stored.get("reused_from"):
        from meshpipeline.application.geometry_reuse import remember_scout

        remember_scout(session_id, owner_id, {k: v for k, v in stored.items() if k not in _STAMPED_KEYS},
                       interpretation)
    logger.info("geometry scout %s - session=%s in %.1fs", stored.get("reason") or stored["status"],
                session_id, result["seconds"])
    return stored


#: A PREVIEW is the part as first drawn, stored before it is measured: "running" while the scout
#: is still measuring it, "kept" when the full reading never came and the preview stands.
PREVIEW_RUNNING, PREVIEW_KEPT = "running", "kept"


def scout_still_running(session_id: str) -> bool:
    """The session's scout has not landed its full reading yet (nothing stored, still pending, or only the
    preview): a naming started now could only wait for it (the naming queue's yield reads this)."""
    try:
        stored = read_check(session_id)
    except Exception:  # noqa: BLE001 - the store unread: the naming runs as it always did
        return False
    return stored is None or stored.get("status") == STATUS_PENDING or is_preview(stored)


def is_preview(stored: dict | None) -> bool:
    """Whether the stored check is a preview the scout is still measuring."""
    return bool(stored) and (stored or {}).get("preview") == PREVIEW_RUNNING


class _Preview:
    """THE PART, SHOWN BEFORE IT IS MEASURED. The scout's full reading of a big assembly - the
    openings probed face by face, the clean check, the fluid inside, the pictures - takes minutes
    (jubilee_frame, 145 solids: ten minutes for the probes alone), and nothing about confirming a
    part's size and unit needs it. So the moment the part is read and its skin drawn, the skin and
    a provisional check - its size, unit, bodies and name, nothing guessed past that - are stored
    as `scouted`, marked a preview: the stage opens on it and the user can proceed. The full
    reading replaces it when it lands; a full reading that fails or runs out of time leaves the
    preview standing (`kept`), with the reason said, instead of a failed check."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.record: dict | None = None
        self.skin_key: str | None = None

    def publish(self, *, skin: Path, seed: dict, ref, fetched: Path, canonical, placed: bool) -> None:
        """Store the skin and the provisional check. Never raises: a preview that cannot be made
        leaves the scout exactly as it was without one."""
        try:
            from meshpipeline.cad.part_name import file_stem_name, part_name

            facts = preview_facts(skin, seed)
            facts.update(canonical.facts())
            facts["notes"].extend(n for n in canonical.notes if n not in facts["notes"])
            facts["part_name"] = part_name(fetched, ref.original_filename)
            facts["file_name"] = file_stem_name(ref.original_filename)
            skin_key = check_object_key(self.session_id, "skin.json")
            _store_json(skin_key, _placed_skin_payload(fetched) if placed else skin_payload(skin))
            self.skin_key = skin_key
            record = {"named": False, "facts": facts, "proposal": _proposal(facts, None), "snapshots": [],
                      "skin_key": skin_key, "source": ref.to_payload(), "preview": PREVIEW_RUNNING}
            current = read_check(self.session_id)
            if current is not None and current.get("status") != STATUS_PENDING:
                return                    # the check moved on meanwhile; the preview is not needed
            # the limits the check was queued under stay on it: the time box reads them
            record.update({k: current[k] for k in ("soft_limit_s", "hard_limit_s", "size_bytes")
                           if current is not None and current.get(k) is not None})
            write_status(self.session_id, STATUS_SCOUTED, **record)
            self.record = record
            logger.info("geometry check: preview stored (%s mm) - session=%s",
                        facts.get("size_mm"), self.session_id)
        except Exception as exc:  # noqa: BLE001 - the full reading still lands as it always did
            logger.warning("geometry check: no preview (%s: %s) - session=%s", type(exc).__name__,
                           str(exc)[:200], self.session_id)

    def kept(self, reason: str) -> dict:
        """The preview as the check's result, the full reading having failed for `reason`."""
        return kept_preview({**(self.record or {}), "status": STATUS_SCOUTED}, reason)

    def moved_on(self) -> dict | None:
        """The stored record when something other than the scout has moved the check on since the
        preview (a naming landed on it), else None."""
        try:
            current = read_check(self.session_id)
        except Exception:  # noqa: BLE001 - unreadable: the scout's result is written as usual
            return None
        if current is None or current.get("status") in (STATUS_PENDING, STATUS_SCOUTED):
            return None
        return current


def kept_preview(record: dict, reason: str) -> dict:
    """A preview record as the check's standing result: the full reading did not come (`reason`),
    so the quick drawing is what the user confirms - said on the facts and the proposal, the clean
    check said to run again at the mesh. Pure: a copy, the record given is not touched."""
    out = json.loads(json.dumps(jsonable(record)))
    said = (f"The full check did not finish ({_plain(reason)}): the part is shown from its quick "
            "drawing. Check its size and unit and press Proceed - it is measured again when it is meshed.")
    for holder in (out.setdefault("facts", {}), out.setdefault("proposal", {})):
        notes = holder.setdefault("notes", [])
        if said not in notes:
            notes.append(said)
    validity = out["facts"].get("validity")
    if isinstance(validity, dict) and validity.get("kind") == "unchecked":
        validity["summary"] = "The clean check did not finish on this file; it runs again when the part is meshed."
    out.update(status=STATUS_SCOUTED, preview=PREVIEW_KEPT, preview_reason=str(reason)[:240])
    return out


def _plain(reason: str) -> str:
    text = str(reason or "").strip()
    head, sep, rest = text.partition(": ")
    if sep and " " not in head and head[:1].isupper():
        text = rest
    return text.rstrip(" .") or "it stopped"


def preview_facts(skin: Path, seed: dict) -> dict:
    """The provisional check's facts, from the skin alone (metres) and what reading the file said
    (`seed`: the unit and its note, how it was read, how many solids): the part's real box and
    size, and nothing the full reading has not measured - no openings, no flat faces, the clean
    check still to come. Read as a solid body until the user or the full reading says otherwise,
    with no confidence in that."""
    import numpy as np

    from meshpipeline.cad.scout import length_decimals
    from meshpipeline.cad.stl_io import read_stl_triangles

    tris = np.asarray(read_stl_triangles(Path(skin)), dtype=float).reshape(-1, 3)
    if len(tris) == 0:
        raise RuntimeError("the part's skin has no triangles to measure")
    lo, hi = tris.min(axis=0), tris.max(axis=0)
    size = [float(v) for v in hi - lo]
    d = length_decimals(size)
    notes = list(seed.get("notes") or [])
    notes.append("the part is still being measured: its openings and the clean check follow")
    return {
        "body_kind": "unknown", "input_kind": "solid-body", "flow": "external",
        "solids": seed.get("solids"), "planar_faces": None,
        "bbox_min_m": [round(float(v), d + 4) for v in lo], "bbox_max_m": [round(float(v), d + 4) for v in hi],
        "size_mm": [round(v * 1000.0, d) for v in size],
        "openings": [], "seed_point_m": None, "seed_point_mm": None,
        "confidence": {"input_kind": 0.0, "openings": 0.0},
        "notes": notes, "faces": [], "holes": [],
        "read_as": seed.get("read_as") or "cad",
        "unit_assumed": bool(seed.get("unit_assumed")),
        "scale_to_m": float(seed.get("scale_to_m") or 0.001),
        "validity": {"clean": None, "kind": "unchecked", "complete": False, "reasons": [],
                     "summary": "The clean check is still running on this file."},
        "preview": True,
    }


class _Unsupported(RuntimeError):
    pass


def _out_of_time(exc: BaseException) -> bool:
    """Whether the worker's soft time limit ended the step (Celery's SoftTimeLimitExceeded, known
    by name: the application does not import the queue)."""
    return type(exc).__name__ == "SoftTimeLimitExceeded"


def _scout_tracker(session_id: str, source: dict):
    """The scout's progress record, begun: which pending marker it runs for, what kind of check
    it is, and how big the file is - so the line can say "big file (180 MB)"."""
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.contracts.intake_formats import format_for_suffix

    suffix = str((source or {}).get("suffix_hint") or "").lower()
    marker = None
    try:
        stored = read_check(session_id)
        if stored is not None and stored.get("status") == STATUS_PENDING:
            marker = stored.get("written_at")
    except Exception:  # noqa: BLE001 - without the marker the record is matched by its time
        marker = None
    fmt = format_for_suffix(suffix)
    tracker = gp.Tracker(session_id, "scout")
    tracker.begin(marker=marker, mode=str(getattr(fmt, "check_mode", "") or "") or "generic",
                  work={"size_bytes": (source or {}).get("size_bytes"), "suffix": suffix})
    return tracker


def _fetch(ref, work: Path) -> Path:
    """The uploaded bytes, brought to the worker and checked against what the upload recorded.
    The same integrity rule the job materializer applies; this needs no confirmed unit."""
    from meshpipeline.contracts.geometry_source import safe_suffix, sha256_of
    from meshpipeline.contracts.object_storage import get_object_store

    dest = work / f"geometry{safe_suffix(ref.suffix_hint)}"
    get_object_store().download_file(object_key=ref.object_key, destination=dest)
    digest, size = sha256_of(dest)
    if size != int(ref.size_bytes) or digest != ref.sha256:
        dest.unlink(missing_ok=True)
        raise RuntimeError("the retrieved geometry does not match the upload")
    return dest


def _scout(*, session_id: str, owner_id: str, source: dict, interpretation: dict | None,
           preview: _Preview | None = None) -> dict:
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.cad.ingest import IngestError, canonicalise
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
    from meshpipeline.contracts.intake_formats import (
        THERMAL_CHECK,
        GeometryKind,
        check_mode_of,
        format_for_suffix,
    )
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.render.scout_snapshots import render_snapshots

    ref = GeometrySourceRef.from_payload(source)
    suffix = (ref.suffix_hint or "").lower()
    if format_for_suffix(suffix) is None:
        raise _Unsupported("the geometry check reads the accepted geometry formats; this upload "
                           f"is a {suffix or 'nameless'} file, so the intake will ask about its openings")
    interp_ref = GeometryInterpretationRef.from_payload(interpretation) if interpretation else None

    # THE SAME BYTES, CHECKED BEFORE (application/geometry_reuse): an upload whose digest an earlier check
    # of this owner's already read, under the same unit, is answered from that check - its skin and
    # pictures copied under this session - in seconds instead of minutes
    from meshpipeline.application.geometry_reuse import reuse_scout

    gp.mark(gp.READING)
    reused = reuse_scout(session_id, owner_id, source, interpretation)
    if reused is not None:
        logger.info("geometry check: the same file was checked before (session %s) - reused, session=%s",
                    (reused.get("reused_from") or {}).get("session_id"), session_id)
        _publish_validity(session_id, ref, (reused.get("facts") or {}).get("validity") or {})
        return {**reused, "status": STATUS_SCOUTED}

    # The workspace lives exactly as long as the check. The worker runs for weeks; every upload,
    # skin and picture left behind in /tmp would stay there until the disk was full.
    with tempfile.TemporaryDirectory(prefix=f"geometry_check_{session_id[:8]}_") as tmp,             contextlib.ExitStack() as children:
        work = Path(tmp)
        # the same canonical form the job materialiser hands the engines (cad/ingest) - except a
        # thermal model, which the check reads as its PLACED parts: each part where the file puts
        # it, under its own name and colour, in milliseconds and without joining anything (the
        # joined form, when an engine needs it, is built for the run)
        fetched = _fetch(ref, work)
        try:
            canonical = canonicalise(fetched, work, stem="geometry", ecxml_form="placed")
        except IngestError as exc:
            raise _Unsupported(str(exc)) from exc
        local_path = canonical.path
        # THE CLEAN CHECK BESIDE THE SCOUT (cad/beside): it reads only the file, so it runs in a child
        # process on another core while the scout reads the part, and is collected where the scout
        # used to compute it - the same verdict, without the wait (30 s of a landing gear's 82 s)
        early_verdict = _start_clean_verdict(fetched, canonical, work)
        if early_verdict is not None:
            children.callback(early_verdict.close)      # a scout that stops takes its children with it
        # A WHOLE THERMAL MODEL (ECXML) IS SHOWN AS ONE: the file names every part, its material
        # and power, the fans, the vents and the room air, so nothing is pictured, named, turned
        # upright, measured from triangles or given an opening role - the thermal view is built
        # from the file instead
        thermal_sidecar = _thermal_sidecar(canonical.path)
        if thermal_sidecar is not None and check_mode_of(canonical.source_format) == THERMAL_CHECK:
            if early_verdict is not None:
                early_verdict.close()
            return _scout_thermal(session_id, ref, fetched, canonical, thermal_sidecar)
        placed = bool(canonical.physics.get("placed_parts"))

        def drawn(skin: Path, seed: dict) -> None:
            # THE PREVIEW, the moment the skin exists: the stage can open on it while the rest of
            # the reading runs (_Preview)
            if preview is not None:
                preview.publish(skin=skin, seed=seed, ref=ref, fetched=fetched, canonical=canonical,
                                placed=placed)

        if canonical.kind is GeometryKind.cad:
            facts, skin = _scout_exact(local_path, work, interp_ref, ref, on_skin=drawn)
        else:
            # placed parts are written in millimetres, the unit ECXML geometry is read in
            facts, skin = _scout_triangles(local_path, work, interp_ref, ref,
                                           file_scale=0.001 if placed else None, on_skin=drawn)
        # what kind of geometry this is and what it came from, for whoever decides what to offer
        facts.update(canonical.facts())
        facts.setdefault("notes", []).extend(n for n in canonical.notes if n not in facts["notes"])
        # whether the upload is CLEAN (cad/validity): the contract a mesh is promised on
        facts["validity"] = clean_verdict(fetched, canonical, started=early_verdict)
        _publish_validity(session_id, ref, facts["validity"])
        # THE PART'S NAME FROM THE FILE (cad/part_name): a STEP's own product name, else the upload's
        # file name - what every stage shows when no model names the part (an FEA part never asks one)
        from meshpipeline.cad.part_name import file_stem_name, part_name

        facts["part_name"] = part_name(fetched, ref.original_filename)
        # the upload's own file name, for a stage that names a whole assembly (application/fea_check)
        facts["file_name"] = file_stem_name(ref.original_filename)
        if facts["validity"].get("clean") is not True and facts["validity"].get("kind") != "unchecked":
            facts["notes"].append(facts["validity"]["summary"])
        # a thermal model's physics (ECXML: materials, powers, fans, vents...), kept whole beside
        # the session's check so a later solver step - or the user - has all of it
        thermal = thermal_sidecar
        if thermal is not None:
            thermal_key = check_object_key(session_id, "thermal_model.json")
            _store_json(thermal_key, thermal)
            facts.setdefault("thermal_model", {})["object_key"] = thermal_key
            # the file names its parts and which one is the air: an assembly of regions whose air
            # fills the domain box, not a body to guess about
            facts["input_kind"] = "solid-assembly"
            facts["flow"] = "internal"
            if isinstance(facts.get("confidence"), dict):
                facts["confidence"]["input_kind"] = 0.95
            declared_openings(facts, thermal)

        # the same skin, stored for the stage the user turns the part in
        gp.mark(gp.PICTURES, triangles=gp.skin_triangles(skin))
        skin_key = preview.skin_key if preview is not None and preview.skin_key else None
        if skin_key is None:
            # the same skin, stored for the stage the user turns the part in (the preview stored
            # it already when there was one: the same file, drawn once)
            skin_key = check_object_key(session_id, "skin.json")
            _store_json(skin_key, _placed_skin_payload(fetched) if placed else skin_payload(skin))
        # which way the part stands up, as far as its shape says: the code's half of the up
        # proposal, weighed against the pictures once the model has seen them
        facts["up_evidence"] = read_up_evidence(skin)
        store = get_object_store()
        snapshots = []
        # THE PICTURES ARE FOR THE MODEL AND THE CARD, never the user's way on: a part they cannot
        # be drawn for is still checked, on the stage, with the reason said
        try:
            shots = render_snapshots(skin, stickered(facts), work / "pictures")
            for s in shots:
                key = check_object_key(session_id, f"{s.name}.png")
                store.upload_file(local_path=s.path, object_key=key)
                # the camera goes with the picture: "the nose is at the left of the top picture"
                # is read through it
                snapshots.append({"name": s.name, "object_key": key, "facing": list(s.facing),
                                  "direction": list(s.direction), "up": list(s.up)})
        except Exception as exc:  # noqa: BLE001 - the stage draws the skin itself
            if _out_of_time(exc):
                raise
            logger.warning("geometry check: the pictures could not be drawn (%s: %s) - session=%s",
                           type(exc).__name__, str(exc)[:200], session_id)
            facts.setdefault("notes", []).append(
                "the pictures could not be drawn, so nothing was named from them; the part is on the stage")
        upright = _store_upright_sheet(session_id, skin, work, store)
    result = {"status": STATUS_SCOUTED, "named": False, "facts": facts, "proposal": _proposal(facts, None),
              "snapshots": snapshots, "skin_key": skin_key, "source": ref.to_payload()}
    if upright is not None:
        result["upright_sheet"] = upright
    return result


def _start_clean_verdict(uploaded: Path, canonical, work: Path):
    """The clean check, started in a child process beside the scout (cad/beside); None when it cannot
    be (the scout then judges the file itself, as before)."""
    try:
        from meshpipeline.cad.beside import Beside
        from meshpipeline.cad.validity import canonical_task

        handle = Beside("meshpipeline.cad.validity", canonical_task(uploaded, canonical), work / "clean_check",
                        name="clean_check")
        return handle if handle.proc is not None else None
    except Exception as exc:  # noqa: BLE001 - judged in-process instead
        logger.info("geometry check: the clean check could not start beside the scout (%s)", type(exc).__name__)
        return None


#: THE CLEAN VERDICT, PUBLISHED THE MOMENT IT IS KNOWN: an FEA naming waiting for the scout reads it to begin
#: the slow half of its measurement (the suspected defects' Boolean check) before the scout has landed.
EARLY_VALIDITY = "validity.json"


def _publish_validity(session_id: str, ref, verdict: dict) -> None:
    try:
        _store_json(check_object_key(session_id, EARLY_VALIDITY), {"sha256": ref.sha256, "validity": verdict})
    except Exception as exc:  # noqa: BLE001 - a courtesy to the naming; the scout carries the verdict anyway
        logger.info("geometry check: the early clean verdict was not stored (%s)", type(exc).__name__)


def early_validity(session_id: str, sha256: str) -> dict | None:
    """The clean verdict the scout published for this upload (`sha256`), before it landed; None until then."""
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    try:
        got = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, EARLY_VALIDITY)))
    except ObjectNotFound:
        return None
    except Exception:  # noqa: BLE001 - unreadable is not yet
        return None
    if not isinstance(got, dict) or got.get("sha256") != sha256 or not isinstance(got.get("validity"), dict):
        return None
    return got["validity"]


def clean_verdict(uploaded: Path, canonical, started=None) -> dict:
    """THE CLEAN CONTRACT on the upload (cad/validity): clean yes or no, and every reason with
    where on the part it is. A file this calls clean is one the product promises to mesh, so a
    clean file that then fails is a product bug, never the user's. It informs and never refuses:
    a file that is not clean still goes on, with the reasons said. Never fails the scout.
    `started` is the same check begun beside the scout (_start_clean_verdict): its answer stands
    when it gave one; else the file is judged here."""
    if started is not None:
        answer = started.result()
        if isinstance(answer, dict) and "clean" in answer and "error" not in answer:
            return answer
    try:
        from meshpipeline.cad.validity import check_canonical

        return dict(check_canonical(uploaded, canonical).as_dict())
    except Exception as exc:  # noqa: BLE001 - the check is evidence; the scout must still land
        logger.warning("geometry check: the clean check could not run (%s: %s)", type(exc).__name__,
                       str(exc)[:200])
        return {"clean": None, "kind": "check_failed", "complete": False, "reasons": [],
                "summary": f"the clean check could not run ({type(exc).__name__})"}


def stickered(facts: dict) -> list[dict]:
    """The openings the pictures carry stickers for: the scout's, when it reads the fluid as flowing
    through the part. A part it reads as a body in a flow still keeps the openings it would have if
    the fluid flowed through it - so a user who says so has openings to confirm - but they are not
    drawn: the pictures (and the model naming them) see the body as before."""
    return list(facts.get("openings") or []) if facts.get("flow") != "external" else []


def _scout_thermal(session_id: str, ref, fetched: Path, canonical, sidecar: dict) -> dict:
    """The check of a whole thermal model (a format whose check_mode is "thermal"): its placed
    parts as the stage draws them, its physics kept whole beside the check, and the thermal view -
    the parts tree, totals, room air, air box, fans, vents, sources, probes, warnings and detail
    levels - built from the file (application/thermal_view). Nothing else is asked of it: no
    pictures, no picture naming, no up question, no triangle measuring, and no openings - the air
    box's sides are the file's, described in the view, never given a role."""
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.application.thermal_view import detail_levels_bounded, thermal_view
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_place import place, placed_skin
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    placement = place(read_ecxml(fetched))
    thermal_key = check_object_key(session_id, "thermal_model.json")
    _store_json(thermal_key, sidecar)
    gp.mark(gp.MEASURING)
    # the grid laid out for each detail level, in a child stopped at its time budget: a level
    # it could not size in time shows its limits, and is sized when the mesh runs
    view = thermal_view(sidecar, placement, detail=detail_levels_bounded(fetched))
    gp.mark(gp.PICTURES)
    skin_key = check_object_key(session_id, "skin.json")
    _store_json(skin_key, placed_skin(placement))
    facts = thermal_facts(view, canonical)
    facts.setdefault("thermal_model", {})["object_key"] = thermal_key
    return {"status": STATUS_SCOUTED, "named": False, "facts": facts, "proposal": _proposal(facts, None),
            "snapshots": [], "skin_key": skin_key, "source": ref.to_payload(),
            "check_mode": THERMAL_CHECK, "thermal": view}


def thermal_facts(view: dict, canonical) -> dict:
    """What the check records of a thermal model, in the shape every reader of the facts takes:
    an assembly of regions (each part, and the air), with no openings - and the file's title, the
    air box's size, and the view's warnings as the notes the form shows until the thermal panel
    replaces it."""
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    facts: dict = {
        "check_mode": THERMAL_CHECK, "read_as": "thermal model", "body_kind": "assembly",
        "input_kind": "solid-assembly", "flow": "internal", "confidence": {"input_kind": 0.95},
        "openings": [], "faces": [], "holes": [], "seed_point_mm": None,
        "size_mm": list((view.get("domain") or {}).get("size_mm") or [0.0, 0.0, 0.0]),
        # the placed parts are written in millimetres, the unit the ECXML geometry is read in
        "scale_to_m": 0.001, "unit_assumed": False,
        "title": str(view.get("title") or ""),
        "notes": [w["text"] for w in view.get("warnings") or [] if w.get("level") == "warn"],
    }
    facts.update(canonical.facts())
    return facts


def _placed_skin_payload(source: Path) -> dict:
    """A thermal model's placed parts as the stage draws them: one block per part, its own name
    and colour (cad/ingest/ecxml_place.placed_skin)."""
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_place import place, placed_skin

    return placed_skin(place(read_ecxml(source)))


def _thermal_sidecar(canonical_path: Path) -> dict | None:
    """The physics a thermal-model upload carried, as cad/ingest wrote it beside the canonical
    geometry; None for every other file."""
    from meshpipeline.cad.ingest.ecxml_build import read_ecxml_sidecar

    return read_ecxml_sidecar(canonical_path)


#: A side counts as walled off when its wall plates cover this much of it.
_WALLED = 0.99
_OPENING_ROLES = ("fan", "vent")


def declared_openings(facts: dict, thermal: dict) -> None:
    """A thermal model SAYS what each side of its solution domain is - a fan, a vent, a wall, or
    open air - so its openings are the file's, not a guess from the shape of the air box (which
    is a plain box, and reads as six openings, or none for an external flow):

    * each fan and each vent on a side is an opening, at its own rectangle, under its own name;
      a fan blowing in is an inlet, one blowing out an outlet, and a vent takes the other role;
    * a side with neither, not walled off, is an opening the size of the side, named for it;
    * a side the file's plates wall off offers nothing.

    Positions are metres: the thermal model states them in SI (JEP181A 4.1), whatever unit its
    solids are built in."""
    from meshpipeline.contracts.patch_names import mesh_safe

    patches = [p for p in thermal.get("patches") or [] if p.get("domain_face")]
    if not patches:
        return
    scale = 1.0
    fans_in = sum(1 for p in patches if p["role"] == "fan" and p.get("suggested_type") == "inlet")
    fans_out = sum(1 for p in patches if p["role"] == "fan" and p.get("suggested_type") == "outlet")
    vent_role = "inlet" if fans_out and not fans_in else "outlet"
    box = (thermal.get("domain") or {}).get("box_m") or {}
    openings, walled = [], []
    open_sides: list[str] = []          # sides the file leaves open (all or in part)
    declared_devices: list[dict] = []   # fans and vents the file puts on its domain sides
    for axis in range(3):
        for sign in ("-", "+"):
            face = sign + "xyz"[axis]
            on_face = [p for p in patches if p["domain_face"] == face]
            side = next((p for p in on_face if p["role"] == "domain_boundary"), None)
            side_area = _side_area(box, axis)
            walls = sum(float(p.get("area_m2") or 0.0) for p in on_face if p["role"] == "wall")
            devices = [p for p in on_face if p["role"] in _OPENING_ROLES]
            if devices:
                for p in devices:
                    role = p.get("suggested_type") if p["role"] == "fan" else vent_role
                    opening = _declared_opening(p, face, role, scale, mesh_safe)
                    openings.append(opening)
                    # named as the stage shows it (mesh-safe), not as the file spells it
                    declared_devices.append({"name": str(opening.get("name") or face),
                                             "role": p["role"], "flow": p.get("suggested_type") or role})
            elif side is not None and not (side_area > 0 and walls >= _WALLED * side_area):
                plates = [p for p in on_face if p["role"] in ("wall", "heat_flux")]
                if len(plates) > _MAX_SIDE_PLATES:
                    facts.setdefault("notes", []).append(
                        f"the domain's {face} side carries {len(plates)} plates, too many to read "
                        "its open part from; add its opening on the stage")
                    continue
                parts = _open_rects(side, plates) if plates else [side]
                if not parts:
                    walled.append(face)
                    continue
                open_sides.append(str(side.get("name") or face))
                if len(parts) > _MAX_SIDE_OPENINGS:
                    facts.setdefault("notes", []).append(
                        f"the plates on the domain's {face} side leave {len(parts)} separate gaps; "
                        f"the {_MAX_SIDE_OPENINGS} largest are offered - add any other on the stage")
                    parts = parts[:_MAX_SIDE_OPENINGS]
                for k, part in enumerate(parts, 1):
                    if len(parts) > 1:
                        part = {**part, "name": f"{side['name']}_{k}"}
                    opening = _declared_opening(part, face, vent_role, scale, mesh_safe)
                    opening["confidence"] = 0.6   # open air: which way it flows is the user's
                    if plates:
                        opening["notes"] = [
                            "an open part of the side: "
                            f"{', '.join(p['name'] for p in plates[:6])} close(s) the rest"]
                    openings.append(opening)
            else:
                walled.append(face)
    for i, o in enumerate(openings, 1):
        o["id"] = i
    facts["openings"] = openings
    reason = declared_openings_reason(open_sides, declared_devices, walled)
    if reason:
        facts["openings_reason"] = reason
    if walled:
        facts.setdefault("notes", []).append(
            "the thermal model closes the domain's " + ", ".join(walled)
            + " side(s) with walls, so they are not offered as openings")


def declared_openings_reason(open_sides: list[str], devices: list[dict], walled: list[str]) -> str:
    """WHY the openings are what they are, in the file's own terms - for a converter that declares
    its boundary roles (here: a thermal model's domain sides, fans and vents). Shown on the form,
    said in the declaration and carried into the brief, so the roles read as the file's and not
    as a guess: a set-top box whose six domain sides all came up "outlet" was corrected three
    times by a user who knew it had a vent and a fan inside (dev-areen, 2026-10-11) - the file
    declares every side of its solution domain open, and the stage never said so. '' when the
    file declares nothing about its sides."""
    if open_sides and not devices and not walled and len(open_sides) == 6:
        return "the file declares every side of its solution domain open"
    said: list[str] = []
    if open_sides:
        said.append(f"its solution domain open at {', '.join(open_sides)}")
    for d in devices:
        if d["role"] == "fan":
            said.append(f"{d['name']}, a fan blowing {'in' if d['flow'] == 'inlet' else 'out'}")
        else:
            said.append(f"{d['name']}, a vent")
    if walled:
        said.append(f"its {', '.join(walled)} side{'s' if len(walled) > 1 else ''} walled")
    return ("the file declares " + "; ".join(said)) if said else ""


#: Plates on one domain side the open-part reading takes (each adds two grid lines per axis).
_MAX_SIDE_PLATES = 256
#: Open rectangles offered for one side; the largest are kept, and a note says how many more.
_MAX_SIDE_OPENINGS = 8


def _open_rects(side: dict, plates: list[dict]) -> list[dict]:
    """What is left open of a domain side its plates partly close, as rectangles that tile the
    open area exactly - one for a strip, two for an L - largest first; [] when nothing is left.
    The side is cut on every plate edge into a grid; covered cells are marked plate by plate
    (one array slice each), and the open cells are merged row by row into rectangles."""
    import numpy as np

    def rect(p):
        c, s = p["centre_m"], p["size_m"]
        axes = ["xyz".index(a) for a in p["size_axes"]]
        return [(c[axes[k]] - s[k] / 2, c[axes[k]] + s[k] / 2) for k in range(2)], axes

    (su, sv), axes = rect(side)
    covers = []
    for p in plates:
        (pu, pv), paxes = rect(p)
        if paxes == axes:
            covers.append(((max(pu[0], su[0]), min(pu[1], su[1])),
                           (max(pv[0], sv[0]), min(pv[1], sv[1]))))
    us = sorted({su[0], su[1], *(v for c in covers for v in c[0] if su[0] < v < su[1])})
    vs = sorted({sv[0], sv[1], *(v for c in covers for v in c[1] if sv[0] < v < sv[1])})
    iu, iv = {v: i for i, v in enumerate(us)}, {v: j for j, v in enumerate(vs)}
    covered = np.zeros((len(vs) - 1, len(us) - 1), dtype=bool)
    for (cu0, cu1), (cv0, cv1) in covers:
        if cu1 > cu0 and cv1 > cv0:
            covered[iv[cv0]:iv[cv1], iu[cu0]:iu[cu1]] = True
    rects: list[tuple[int, int, int, int]] = []          # (i0, i1, j0, j1) in grid indices
    active: dict[tuple[int, int], int] = {}
    for j in range(covered.shape[0] + 1):
        runs: list[tuple[int, int]] = []
        if j < covered.shape[0]:
            open_row = ~covered[j]
            i = 0
            while i < len(open_row):
                if open_row[i]:
                    start = i
                    while i < len(open_row) and open_row[i]:
                        i += 1
                    runs.append((start, i))
                else:
                    i += 1
        nxt = {run: active.pop(run, j) for run in runs}
        rects += [(r[0], r[1], j0, j) for r, j0 in active.items()]
        active = nxt
    side_area = (su[1] - su[0]) * (sv[1] - sv[0])
    out = []
    for i0, i1, j0, j1 in rects:
        u0, u1, v0, v1 = us[i0], us[i1], vs[j0], vs[j1]
        if (u1 - u0) * (v1 - v0) <= 1e-6 * side_area:
            continue
        centre = list(side["centre_m"])
        centre[axes[0]], centre[axes[1]] = (u0 + u1) / 2, (v0 + v1) / 2
        out.append({**side, "centre_m": centre, "size_m": [u1 - u0, v1 - v0]})
    out.sort(key=lambda p: -p["size_m"][0] * p["size_m"][1])
    return out


def _side_area(box: dict, axis: int) -> float:
    try:
        lo, hi = box["min"], box["max"]
        a, b = (hi[i] - lo[i] for i in range(3) if i != axis)
        return float(a * b)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _declared_opening(patch: dict, face: str, role, scale: float, mesh_safe) -> dict:
    """An opening as the geometry stage draws it, from a patch the thermal model declares."""
    import math

    axis = "xyz".index(face[1])
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0 if face[0] == "+" else -1.0         # out of the air, like every opening
    centre = [float(v) * scale for v in patch["centre_m"]]
    w, h = (float(v) * scale for v in patch["size_m"])
    area = w * h
    return {"id": 0, "face": None, "name": mesh_safe(patch["name"], fallback=f"side_{face[1]}"),
            "role": role if role in ("inlet", "outlet") else "outlet", "kind": "disc",
            "shape": "rectangle", "confidence": 0.95, "centroid_m": centre,
            "centroid_mm": [c * 1000.0 for c in centre], "normal": normal, "area_m2": area,
            "area_mm2": area * 1e6, "on_extremity": True, "clear_ahead": True,
            "width_mm": w * 1000.0, "height_mm": h * 1000.0,
            "diameter_mm": math.sqrt(4.0 * area / math.pi) * 1000.0,
            "declared_by": {"object": patch.get("object"), "role": patch.get("role"),
                            "name": patch.get("name")}}


def _store_upright_sheet(session_id: str, skin: Path, work: Path, store) -> dict | None:
    """The six-way picture the naming asks which way is up from, stored beside the others but not
    among them: it is for that one question, never for the card or the naming. None when it could
    not be drawn - the shape's reading and the file as drawn still stand."""
    from meshpipeline.render.scout_snapshots import render_upright_sheet

    try:
        sheet = render_upright_sheet(skin, work / "pictures" / "upright.png")
        key = check_object_key(session_id, "upright.png")
        store.upload_file(local_path=sheet.path, object_key=key)
    except Exception as exc:  # noqa: BLE001 - one question fewer, never a failed scout
        logger.warning("geometry check: the six-way picture could not be drawn (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        return None
    return {"object_key": key, "order": list(sheet.order)}


def _scout_exact(local_path: Path, work: Path, interp_ref, ref, on_skin=None) -> tuple[dict, Path]:
    """A STEP or IGES file: OpenCASCADE reads real faces, so the openings come out exact.
    `on_skin(skin, seed)` is told the moment the part's skin is drawn - before it is measured -
    with what reading the file already said (the unit, the solids)."""
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.cad.fluid_volume_check import wants_fluid_volume
    from meshpipeline.cad.normalise import occ_scale_transform
    from meshpipeline.cad.scout import count_solids, read_cad, scout_cad, write_view_stl

    prepared, unit_note = _prepared_coordinates(local_path, interp_ref, ref)
    # read once: the scout and the view skin each work on their own scaled copy of it
    shape = read_cad(local_path)
    gp.mark(gp.MEASURING)
    interp = getattr(prepared, "interpretation", None)
    scale_to_m = float(interp.scale_to_metres) if interp is not None else 0.001
    # THE SKIN FIRST, sized to the part (cad/scout.view_deflection): the stage can show the part
    # while the rest of it is measured, and a skin is all a preview needs
    skin = write_view_stl(local_path, work / "skin.stl", prepared=prepared, shape=shape)
    gp.note_work(triangles=gp.skin_triangles(skin))
    if on_skin is not None:
        try:
            solids: int | None = count_solids(shape)
        except Exception:  # noqa: BLE001 - the preview says nothing of bodies it cannot count
            solids = None
        on_skin(skin, {"read_as": "cad", "unit_assumed": bool(unit_note), "scale_to_m": scale_to_m,
                       "notes": [unit_note] if unit_note else [], "solids": solids})
    # a thermal model states its openings (declared_openings, below); its many solids are not
    # searched for them
    declared = {"declared_openings": True} if _thermal_sidecar(local_path) is not None else {}
    facts = scout_cad(local_path, prepared=prepared, shape=shape, **declared).as_dict()
    # THE FLUID INSIDE THE PARTS, begun now beside the rest (cad/beside): the same child the reading
    # below waits for, so it works while the skin's holes are found
    early_fluid = None
    if not declared and wants_fluid_volume(facts):
        from meshpipeline.cad.fluid_volume_check import start_fluid_volume
        early_fluid = start_fluid_volume(local_path, work=work, kind="cad",
                                         scale=occ_scale_transform(prepared).ScaleFactor())
    try:
        return _scout_exact_rest(facts, skin, local_path, work, interp_ref, prepared, unit_note, scale_to_m,
                                 declared, early_fluid)
    finally:
        if early_fluid is not None:
            early_fluid.close()


def _scout_exact_rest(facts: dict, skin: Path, local_path: Path, work: Path, interp_ref, prepared, unit_note,
                      scale_to_m: float, declared: dict, early_fluid) -> tuple[dict, Path]:
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.cad.fluid_volume_check import read_fluid_volume, wants_fluid_volume
    from meshpipeline.cad.normalise import occ_scale_transform

    facts["read_as"] = "cad"
    facts["unit_assumed"] = bool(unit_note)
    facts["scale_to_m"] = scale_to_m
    if unit_note:
        facts["notes"].append(unit_note)
        # the unit most of a mixed file's parts state: the unit proposal's first reading
        if interp_ref is None and (mostly := _stated_mostly(local_path)) is not None:
            facts["unit_stated_mostly"] = mostly.unit.value
    gp.mark(gp.OPENINGS, triangles=gp.skin_triangles(skin))
    facts["holes"] = skin_holes(skin, facts)
    if not declared:
        map_openings(facts)
        # AN ASSEMBLY'S FLUID is the space inside its parts, closed off where it meets the outside
        # (cad/fluid_volume_check): its openings are what the user confirms, not every flat face
        if wants_fluid_volume(facts):
            read_fluid_volume(facts, local_path, work=work, kind="cad",
                              scale=occ_scale_transform(prepared).ScaleFactor(), started=early_fluid)
        read_whole_shape(facts, skin)
    return facts, skin


def read_whole_shape(facts: dict, skin: Path) -> None:
    """What the WHOLE skin says, folded into the scout's facts (cad/flow_reading): a body in a
    flow the face-by-face reading took for a passage (blades round a hub, a straight bar, a file of
    surfaces with nowhere for a fluid to go), an annular fluid body taken for a pipe wall, the
    inlet a diffuser names, and the shape's own reading of which way the flow goes past a body -
    each with its reason in the notes. A thermal model's file states all of that itself and is
    not read. Never fails the scout: a skin it cannot read leaves the facts as they were."""
    from meshpipeline.cad.flow_reading import MAX_TRIANGLES, apply_reading, read_flow, skin_triangles

    try:
        # THE BOUND, before a byte is read (read_up_evidence's): 84 bytes and 50 per triangle
        if (Path(skin).stat().st_size - 84) // 50 > MAX_TRIANGLES:
            facts["reading"] = {"read": False, "reason": "too many triangles to read quickly"}
            return
        apply_reading(facts, read_flow(skin_triangles(skin), facts))
    except Exception as exc:  # noqa: BLE001 - the face-by-face reading still stands
        logger.warning("geometry check: the whole shape could not be read (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        facts["reading"] = {"read": False, "reason": f"the shape could not be read ({type(exc).__name__})"}


def proposed_leak_closing(facts: dict) -> float | None:
    """The leak size the check proposes closing (INS-8), in metres of the skin, or None."""
    leaks = facts.get("leaks") if isinstance(facts, dict) else None
    if not isinstance(leaks, dict) or not leaks.get("found"):
        return None
    try:
        v = float(leaks.get("close_up_to_mm") or 0.0) / 1000.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def vet_openings(facts: dict, skin: Path) -> None:
    """THE PROPOSAL HELD TO THE STAGING THE MESHER WILL USE (cad/fluid_passages.stranded_openings):
    an opening the scout found that opens into no passage - a bolt hole through a flange, a slot
    through a plate, a dimple closed into a pocket - would come back from the mesher as a patch
    with no faces. It is proposed as not an opening, with the reason in the notes; the user can
    still make it one. A proposal left without an inlet takes its largest remaining opening. Run
    only when the fluid is read as flowing through the part (hold_openings_to_the_staging): a body
    in a flow keeps its openings unvetted for a flip, which the mesher's own staging still checks.
    A CAD file is staged on its B-rep, not on this skin, so only a triangle file is vetted this way."""
    from meshpipeline.application.geometry_confirmation import body_of, patches_from
    from meshpipeline.cad.fluid_passages import stranded_openings

    ops = [o for o in facts.get("openings") or [] if o.get("role") in ("inlet", "outlet")]
    if len(ops) < 2:
        return
    try:
        patches = patches_from(body_of({"flow": "internal", "input_kind": facts.get("input_kind") or "",
                                        "openings": [dict(o) for o in ops]}))
    except Exception:  # noqa: BLE001 - openings the declaration cannot be made from are not vetted
        return
    close = proposed_leak_closing(facts)
    stranded = stranded_openings(skin, patches, close_leaks=close) if close else stranded_openings(skin, patches)
    if not stranded or len(stranded) >= len(ops) - 1:
        return                   # at least two openings must stay: a vetting cannot empty a part
    for o in ops:
        if o.get("name") in stranded:
            o["role"] = "not_an_opening"
            facts.setdefault("notes", []).append(
                f"opening {o.get('id')} ({o.get('name')}) is proposed as not an opening: {stranded[o['name']]}")
    kept = [o for o in ops if o.get("role") != "not_an_opening"]
    if kept and not any(o.get("role") == "inlet" for o in kept):
        max(kept, key=lambda o: float(o.get("area_mm2") or 0.0))["role"] = "inlet"


def vet_by_map(facts: dict, imap) -> None:
    """A CAD PART'S OPENINGS HELD TO ITS MAP (INS-2): an opening the faces read whose outside - a
    few cubes out along the way the fluid would leave by it - is the part's own inside, not the
    open air, is a step inside the passage (a counterbore's floor, a socket's shoulder), not a
    mouth: proposed as not an opening, with the reason in the notes. One the map's grid is too
    coarse to see is left as it is."""
    import numpy as np

    from meshpipeline.cad.inside_space import MIN_ACROSS
    if not imap.passages():
        return
    for o in facts.get("openings") or []:
        if o.get("role") not in ("inlet", "outlet"):
            continue
        c, n = o.get("centroid_m"), o.get("normal")
        d = float(o.get("diameter_mm") or 0.0) / 1000.0
        if not c or not n or d < MIN_ACROSS * imap.h:
            continue
        c, n = np.asarray(c, dtype=float), np.asarray(n, dtype=float)
        ahead = imap.where([c + k * imap.h * n for k in (2.0, 3.0, 4.5)])
        if "out" not in ahead and "pocket" in ahead:
            o["role"] = "not_an_opening"
            facts.setdefault("notes", []).append(
                f"opening {o.get('id')} ({o.get('name')}) is proposed as not an opening: it is a step inside the "
                "passage, and the space beyond it is the part's own inside, not the outside")
    kept = [o for o in facts.get("openings") or [] if o.get("role") in ("inlet", "outlet")]
    if kept and not any(o.get("role") == "inlet" for o in kept):
        max(kept, key=lambda o: float(o.get("area_mm2") or 0.0))["role"] = "inlet"


def map_openings(facts: dict) -> None:
    """A CAD PART'S OPENINGS FROM ITS MAP (INS-2), when its faces gave fewer than two: the mouths
    the inside-space map read on its skin (`facts["holes"]`, marked `passage`), proposed as the
    openings - the largest the inlet - with a side pocket's mouths as not openings. The faces'
    exact openings always win when there are two or more of them. Never fails the scout."""
    ports = [o for o in facts.get("openings") or [] if o.get("role") in ("inlet", "outlet")]
    mouths = [h for h in facts.get("holes") or [] if h.get("passage", True)]
    if len(ports) >= 2 or len(mouths) < 2 or not (facts.get("inside") or {}).get("has_inside"):
        return
    side = [h for h in facts.get("holes") or [] if not h.get("passage", True)]
    mouths = sorted(mouths, key=lambda h: -float(h.get("area_mm2") or 0.0))
    out: list[dict] = []
    for k, h in enumerate([*mouths, *side], start=1):
        is_side = k > len(mouths)
        name = ("inlet" if k == 1 else "outlet" if len(mouths) == 2 else f"outlet_{k - 1}") if not is_side \
            else f"side_{k - len(mouths)}"
        o = {"id": k, "face": -1, "name": name, "role": "not_an_opening" if is_side else ("inlet" if k == 1 else "outlet"),
             "kind": "rim" if h.get("kind") == "rim" else "ring", "shape": h.get("shape") or "other",
             "confidence": float(h.get("confidence") or 0.6),
             "centroid_m": list(h.get("centroid_m") or []), "centroid_mm": list(h.get("centroid_mm") or []),
             "normal": list(h.get("normal") or []),
             "area_m2": round(float(h.get("area_mm2") or 0.0) * 1e-6, 9), "area_mm2": float(h.get("area_mm2") or 0.0),
             "diameter_mm": h.get("diameter_mm"), "on_extremity": True, "clear_ahead": True, "from_map": True}
        for key in ("width_mm", "height_mm", "inner_diameter_mm", "flow_area_mm2"):
            if h.get(key) is not None:
                o[key] = h[key]
        out.append(o)
    facts["openings"] = out
    facts.setdefault("notes", []).append(
        f"the part's faces gave {len(ports)} opening(s); its inside-space map found {len(mouths)} mouth(s) of its "
        "passages, proposed as the openings (measured on the drawn skin: close, not exact)")


def skin_holes(skin: Path, facts: dict | None = None) -> list[dict]:
    """The holes of the skin the stage draws, by the one definition the triangle scout uses
    (cad/open_ends: the mouths of the part's inside-space map) - what "Add an opening" snaps to.
    A STEP file's openings are measured exactly from its faces; these are for the click, on the
    same triangles the user clicks. The map's reading of the part's inside goes into `facts`
    (`inside`, INS-1), which the stage's flow question reads (INS-3). Never fails the scout: a
    skin it cannot read has no holes to snap to, and the click falls back."""
    import numpy as np

    from meshpipeline.cad.open_ends import find_holes, inside_map, surface
    from meshpipeline.cad.stl_io import read_stl_triangles

    try:
        tris = np.asarray(read_stl_triangles(Path(skin)), dtype=float).reshape(-1, 3, 3)
        mesh = surface(tris)
        found = find_holes(mesh=mesh) if mesh is not None else []
        holes = [h.as_dict() for h in found]
        if facts is not None:
            imap = inside_map(mesh) if mesh is not None else None
            facts["inside"] = imap.facts() if imap is not None else {"rule": "INS-1", "mapped": False}
            if mesh is not None:
                from meshpipeline.cad.scout_mesh import surface_health
                health = surface_health(mesh, found)
                if int(facts.get("solids") or 0) >= 1:
                    # a CAD solid is closed by its B-rep: the skin's open edges are where its
                    # tessellation's faces meet unwelded, not gaps in the part
                    health["open_edges_not_openings"] = 0
                    health["from"] = "cad solids"
                facts["surface"] = health
            if imap is not None:
                vet_by_map(facts, imap)
        return holes
    except Exception as exc:  # noqa: BLE001 - a click aid, never a reason to fail the check
        logger.warning("geometry check: the skin's holes could not be read (%s: %s)", type(exc).__name__, exc)
        return []


def _scout_triangles(local_path: Path, work: Path, interp_ref, ref,
                     file_scale: float | None = None, on_skin=None) -> tuple[dict, Path]:
    """An STL, OBJ or VTP file: triangles only, so the openings are read from loops and flat
    rings in the mesh - close, not exact - and the unit is whatever the user confirms
    (`file_scale`: the scale a converted file was written at, when its source fixes the unit).
    `on_skin(skin, seed)` is told the moment the skin is written, before the part is measured."""
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.cad.scout_mesh import scout_mesh, write_mesh_skin
    from meshpipeline.contracts.geometry_units import LengthUnit, scale_to_metres

    if interp_ref is not None:
        scale, note = float(interp_ref.scale_to_metres), ""
    elif file_scale:
        scale, note = float(file_scale), ""
    else:
        scale = scale_to_metres(LengthUnit.millimetre)
        note = "a triangle file carries no unit; sizes assume millimetres until the intake confirms"
    gp.mark(gp.MEASURING)
    # THE SKIN FIRST: the stage can show the part while it is measured (the same file either way)
    skin = write_mesh_skin(local_path, work / "skin.stl", scale_to_m=scale)
    gp.note_work(triangles=gp.skin_triangles(skin))
    if on_skin is not None:
        on_skin(skin, {"read_as": "mesh", "unit_assumed": bool(note), "scale_to_m": scale,
                       "notes": [note] if note else [], "solids": None})
    result = scout_mesh(local_path, scale_to_m=scale)
    facts = result.as_dict()
    facts.update(getattr(result, "extra", {}) or {})
    facts["read_as"] = "mesh"
    facts["unit_assumed"] = bool(note)
    facts["scale_to_m"] = scale
    if note:
        facts["notes"].append(note)
    gp.mark(gp.OPENINGS, triangles=gp.skin_triangles(skin))
    if not file_scale:
        # a thermal model's placed parts (file_scale) state their regions and openings themselves
        from meshpipeline.cad.fluid_volume_check import read_fluid_volume
        # several closed parts in one triangle file: the fluid is the space inside them, as for a
        # CAD assembly (the skin is in metres)
        read_fluid_volume(facts, skin, work=work, scale=1.0, kind="triangles")
        read_whole_shape(facts, skin)
        if facts.get("openings_from") != "fluid_volume":
            # the extracted openings bound the fluid by construction; the skin's own passages
            # (several parts' skins together) say nothing about them
            facts = hold_openings_to_the_staging(facts, local_path, skin, scale)
    return facts, skin


def hold_openings_to_the_staging(facts: dict, local_path: Path, skin: Path, scale: float) -> dict:
    """THE OPENINGS HELD TO THE MESHER'S STAGING (keep_a_stageable_proposal, then vet_openings) -
    only when the fluid is read as flowing THROUGH the part. Both stage the whole skin, and the
    first scouts it a second time: minutes on a car with a dozen wheel-arch and grille candidates
    (the Camaro on dev: 4 of the check's 6 minutes), for openings that are not even drawn on a body
    in a flow (stickered). A body read as one keeps its openings, unvetted, for a flip to "through
    it"; a flip that confirms one opening into nothing is still caught before any mesher runs, by
    the same staging (cad/fluid_passages.PassagePlan.refusal, in the snappy and cfMesh drivers),
    with the way out said: mark it as not an opening on the picture. `openings_vetted` says which."""
    if facts.get("flow") == "external":
        facts["openings_vetted"] = False
        return facts
    facts = keep_a_stageable_proposal(facts, local_path, skin, scale)
    vet_openings(facts, skin)
    facts["openings_vetted"] = True
    return facts


def _ports_of(facts: dict) -> list[dict]:
    return [o for o in facts.get("openings") or [] if o.get("role") in ("inlet", "outlet")]


def _stages(skin: Path, facts: dict) -> bool | None:
    """Whether the staging closes the fluid in with these openings (True / False), or None when it
    cannot be told (fewer than two openings, a skin too big to stage in the check's time)."""
    from meshpipeline.application.geometry_confirmation import body_of, patches_from
    from meshpipeline.cad.fluid_passages import VET_MAX_OPENINGS, VET_MAX_TRIANGLES
    from meshpipeline.cad.internal_surface import InternalSurfaceError, stage_triangles
    from meshpipeline.cad.scout_mesh import read_triangles
    from meshpipeline.engines.port_binding import BindError

    ports = _ports_of(facts)
    if len(ports) < 2 or len(ports) > VET_MAX_OPENINGS:
        return None
    try:
        if (Path(skin).stat().st_size - 84) // 50 > VET_MAX_TRIANGLES:
            return None
        patches = patches_from(body_of({"flow": "internal", "input_kind": facts.get("input_kind") or "",
                                        "openings": [dict(o) for o in ports]}))
        stage_triangles(read_triangles(Path(skin)), intake_patches=patches, surface_path=Path(skin),
                        input_kind=str(facts.get("input_kind") or ""), leak_check=False,
                        close_leaks=proposed_leak_closing(facts))
        return True
    except (InternalSurfaceError, BindError):
        return False
    except Exception:  # noqa: BLE001 - a staging that errors tells nothing either way
        return None


def keep_a_stageable_proposal(facts: dict, local_path: Path, skin: Path, scale: float) -> dict:
    """THE MAP'S OPENINGS, HELD TO THE STAGING (INS-2): when the inside-space map proposes other
    openings than the hole finder's shape rules alone, and the staging cannot close the fluid in
    with the map's while it can with the shape rules' (the map read wrong where shells overlap, or
    took a chip's inlet well for the mouth), the shape rules' reading is proposed, with the note.
    The map's reading of the part's inside (`inside`) is kept either way."""
    from meshpipeline.cad.scout_mesh import scout_mesh

    size = [float(v) for v in facts.get("size_mm") or [1.0, 1.0, 1.0]]
    step = max(1e-3 * math.sqrt(sum(v * v for v in size)), 1e-9)       # a thousandth of the part

    def key(f: dict) -> list:
        return sorted((o.get("role"), tuple(round(float(v) / step) for v in o.get("centroid_mm") or []))
                      for o in _ports_of(f))
    try:
        old = scout_mesh(local_path, scale_to_m=scale, mapped=False)
        plain = old.as_dict()
        plain.update(getattr(old, "extra", {}) or {})
    except Exception:  # noqa: BLE001 - the map's reading stands
        return facts
    if key(plain) == key(facts) or len(_ports_of(plain)) < 2:
        return facts
    if _stages(skin, facts) is not False:
        return facts
    if _stages(skin, plain) is not True:
        return facts
    kept = {k: facts[k] for k in ("inside", "surface", "read_as", "unit_assumed", "scale_to_m") if k in facts}
    out = dict(facts)
    for k in ("openings", "seed_point_m", "seed_point_mm", "body_kind", "input_kind", "flow", "confidence", "faces",
              "holes"):
        if k in plain:
            out[k] = plain[k]
    out.update(kept)
    out["notes"] = list(facts.get("notes") or []) + [
        "the openings the inside map read could not close the fluid in; the hole finder's own openings can, "
        "so they are proposed"]
    out["openings_by"] = "shape_rules"
    return out


def _prepared_coordinates(path: Path, interp_ref, ref):
    """The coordinate state the scout reads in: the confirmed unit when the upload recorded one,
    else what the file declares, else millimetres with a note - the check must not stall on a
    question the intake can still ask."""
    from meshpipeline.cad.unit_evidence import parser_applied_unit, read_declared_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )

    note = ""
    if interp_ref is not None:
        interp = GeometryInterpretation(
            interpretation_id=interp_ref.interpretation_id, owner_id=ref.owner_id,
            geometry_source_id=interp_ref.geometry_source_id, unit=LengthUnit(interp_ref.unit),
            scale_to_metres=float(interp_ref.scale_to_metres), basis=ResolutionBasis(interp_ref.basis),
            evidence=interp_ref.evidence)
    else:
        unit = None
        try:
            evidence = read_declared_unit(path)
            if evidence is not None and evidence.resolved and evidence.unit is not None:
                unit = evidence.unit
        except Exception as exc:  # noqa: BLE001 - a unit we cannot read is a note, not a failure
            logger.info("geometry check: declared unit unreadable (%s)", exc)
        if unit is None:
            # A FILE THAT MIXES UNITS reads first in the one most of its parts state (an Inventor
            # assembly: six parts in inches, one in millimetres), so the sizes shown are its real
            # ones; it is still asked, like a file that states none
            mostly = _stated_mostly(path)
            if mostly is not None:
                unit = mostly.unit
                note = f"{mostly.detail}; sizes are read as the file states them until the intake confirms"
            else:
                unit = LengthUnit.millimetre
                note = "the file does not say its unit; sizes assume millimetres until the intake confirms"
        interp = GeometryInterpretation(
            interpretation_id="unconfirmed", owner_id=ref.owner_id, geometry_source_id=ref.source_id,
            unit=unit, scale_to_metres=scale_to_metres(unit), basis=ResolutionBasis.file_declared,
            evidence="geometry check")
    return from_occ_transfer(interp, parser_applied_unit(path)), note


#: The words every assumed-unit note ends with: what a confirmed unit removes from the notes.
UNIT_ASSUMED_NOTE_END = "until the intake confirms"


def _stated_mostly(path: Path):
    """The unit most of a mixed STEP file's parts state (cad/unit_evidence.stated_mostly), or
    None; a file it cannot read is simply a file that states none."""
    from meshpipeline.cad.unit_evidence import stated_mostly

    try:
        return stated_mostly(path)
    except Exception as exc:  # noqa: BLE001 - evidence only; the check goes on without it
        logger.info("geometry check: mixed units unreadable (%s)", exc)
        return None


def read_up_evidence(skin: Path) -> dict:
    """The code's reading of which way the part stands up (cad/up_axis), from the skin the
    pictures are drawn from. A shape it cannot read says so; it never fails the scout."""
    try:
        import numpy as np

        from meshpipeline.cad.stl_io import read_stl_triangles
        from meshpipeline.cad.up_axis import read_up, too_many

        # THE BOUND, before a byte is read: a binary STL is 84 bytes and 50 per triangle (a text
        # one more), so its size caps the count - a huge skin is never loaded just to be skipped
        over = too_many((Path(skin).stat().st_size - 84) // 50)
        if over is not None:
            return over.as_dict()
        return read_up(np.asarray(read_stl_triangles(Path(skin)), dtype=float)).as_dict()
    except Exception as exc:  # noqa: BLE001 - the pictures and the user still settle it
        logger.warning("geometry check: which way is up could not be read from the shape (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        return {"axis": None, "confidence": 0.0, "reason": f"the shape could not be read ({type(exc).__name__})"}


# ----------------------------------------------------------------------------- the naming ----
@dataclass(frozen=True)
class _Shot:
    name: str
    path: Path
    facing: list
    direction: tuple | None = None      # where the camera looked, and which way was up on the
    up: tuple | None = None             # picture: what "left" and "top" of it mean in the part


def _shot_of(record: dict, path: Path) -> _Shot:
    """A stored picture, with the camera it was drawn with. A picture stored before its camera
    was recorded beside it is one of the named views, whose cameras are fixed; a close-up from
    then has none, and nothing is read through it."""
    from meshpipeline.render.scout_snapshots import global_view

    direction, up = record.get("direction"), record.get("up")
    if not (direction and up):
        direction, up = global_view(str(record.get("name"))) or (None, None)
    return _Shot(name=record["name"], path=path, facing=list(record.get("facing") or []),
                 direction=tuple(direction) if direction else None, up=tuple(up) if up else None)


def run_geometry_naming(*, session_id: str, owner_id: str, purpose_text: str,
                        interpretation: dict | None = None) -> dict:
    """The naming task body, run once the user has said what the part is. Waits for the scout
    when it is still measuring, brings the pictures back, asks the model with the user's words,
    and stores the proposal as `ready`. A scout that failed stays failed; the naming never
    invents a check that was not made. A naming that breaks is stored as `failed` where it can
    be, so the conversation stops waiting for a stage that will not come."""
    from meshpipeline.application import geometry_progress as gp

    tracker = gp.Tracker(session_id, "naming")
    tracker.begin()
    # the naming's heartbeat: an FEA measurement of a big assembly runs for minutes with nothing
    # new to say, and must not be taken for a dead worker (time_boxed)
    tracker.start_heartbeat()
    token = gp.track(tracker)
    try:
        landed = _name(session_id=session_id, owner_id=owner_id, purpose_text=purpose_text,
                       interpretation=interpretation)
        tracker.finish(ok=landed.get("status") == STATUS_READY)
        return landed
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry naming failed - session=%s", session_id)
        tracker.finish(ok=False)
        reason = NAMING_TOO_LONG if _out_of_time(exc) else f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            # a duplicate attempt may already have named the check: a late failure never
            # replaces a result the user can act on
            current = read_check(session_id)
            if current is not None and current.get("status") == STATUS_READY and current.get("named"):
                logger.info("geometry naming: a failed attempt left the ready check alone - session=%s", session_id)
                return {"status": STATUS_READY, "named": True, "superseded_failure": reason}
            # the scout's facts, proposal and skin stay on the failed record: the stage keeps
            # the part for the user to name by hand, and a retry has something to name
            write_status(session_id, STATUS_FAILED, **_failed_record(reason, RETRY_NAMING, current),
                         **({"timed_out": True} if reason == NAMING_TOO_LONG else {}))
        except Exception:  # noqa: BLE001 - the store itself may be what broke
            logger.warning("geometry naming: could not record the failure for %s", session_id)
        return {"status": STATUS_FAILED, "named": False, "reason": reason, "step": RETRY_NAMING,
                "retry": RETRY_NAMING}
    finally:
        tracker.stop_heartbeat()
        gp.untrack(token)


def _name(*, session_id: str, owner_id: str, purpose_text: str, interpretation: dict | None) -> dict:
    started = time.time()
    # AN FEA PART IS MEASURED WHILE THE SCOUT STILL RUNS (_EarlyFea): the wait below used to be idle
    early: list[_EarlyFea] = []

    def begin_early(seen: dict | None) -> None:
        # only while the scout is still measuring the part it has already shown (the preview names
        # the upload); a scout that has landed is measured from straight away, as before
        if not early and is_preview(seen):
            got = _start_early_fea(session_id, seen, purpose_text, interpretation)
            if got is not None:
                early.append(got)

    try:
        return _name_after_wait(session_id=session_id, owner_id=owner_id, purpose_text=purpose_text,
                                interpretation=interpretation, started=started,
                                stored=_wait_for_scout(session_id, on_poll=begin_early),
                                early=early[0] if early else None)
    finally:
        for e in early:
            e.stop()


def _name_after_wait(*, session_id: str, owner_id: str, purpose_text: str, interpretation: dict | None,
                     started: float, stored: dict | None, early) -> dict:
    from meshpipeline.application import geometry_progress as gp
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.object_storage import get_object_store

    if stored is None or stored.get("status") not in (STATUS_SCOUTED, STATUS_READY):
        status = (stored or {}).get("status") or "missing"
        if status in (STATUS_PENDING, "missing"):
            # the scout never finished in time: the request is marked withdrawn, so the chat
            # asks again once the part is measured - and not before, so a scout that never
            # comes cannot trap the conversation a second time
            mark_naming_withdrawn(session_id)
        logger.info("geometry naming skipped - session=%s scout status=%s", session_id, status)
        return {"status": status, "named": False}

    facts = dict(stored["facts"])
    # THE UNIT THE USER CONFIRMED, when the scout had to assume one: every millimetre in the facts
    # is re-read in the confirmed unit before the model or the user sees it.
    if facts.get("unit_assumed") and interpretation:
        interp_ref = GeometryInterpretationRef.from_payload(interpretation)
        k = float(interp_ref.scale_to_metres) / float(facts.get("scale_to_m") or 1.0)
        if abs(k - 1.0) > 1e-9:
            facts = _rescaled(facts, k)
            facts["unit_assumed"] = False
            facts["scale_to_m"] = float(interp_ref.scale_to_metres)
            facts["notes"] = [n for n in facts.get("notes", []) if UNIT_ASSUMED_NOTE_END not in n]
            # THE SKIN FOLLOWS THE FACTS: the stage places the pins at the corrected centroids on
            # the stored skin, so the skin is re-read in the same unit. The pictures show shape
            # only and stay as they are.
            _rescale_skin(session_id, k)

    gp.mark(gp.NAMING)
    if _is_thermal(facts):
        # the file names every part and says which way is up: nothing is asked of a model
        proposal = _proposal(facts, {})
        result = {k: v for k, v in stored.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS}
        result.update(named=True, facts=facts, vision={}, proposal=proposal,
                      purpose_text=purpose_text[:2000], naming_seconds=round(time.time() - started, 1))
        landed = _land_named(session_id, result, stored)
        logger.info("geometry naming %s - session=%s (a thermal model names itself)", landed["status"], session_id)
        return landed
    if _is_fea(purpose_text):
        # AN FEA PART has no openings, no flow and no far field to name: the stage shows the part,
        # its size and unit, its bodies and the clean check, all measured. No model names anything.
        # What the FEA plan sizes from is measured here, in the unit just settled: the walls and
        # the stress hot spots (FEA build step 3)
        from meshpipeline.cad.fea_measure import FACT_KEY

        facts[FACT_KEY] = fea_measurements(stored, facts, interpretation, early=early, owner_id=owner_id)
        # SHSTAGE-1: SHELL OR SOLID, when the user's words say it: the word table, the model only for a
        # paraphrase (checked against the allowed values); the stage opens on it and never overrides it
        from meshpipeline.application import mesh_as

        facts[MESH_AS_KEY] = mesh_as.as_record(mesh_as.read_mesh_as_sync(purpose_text, session_id=session_id,
                                                                          owner_id=owner_id))
        # SHSTAGE-2: THE SHELL CHECK, ONLY WHEN THE USER'S WORDS ASKED FOR A SHELL (plain FEA is a solid, and costs
        # the worker nothing): its status says "measuring" before the stage opens, and the stage never waits on it.
        # A shell asked for later is started by the stage's read (application/shell_check.start_if_asked)
        try:
            from meshpipeline.application import shell_check

            if shell_check.wants_shell(facts):
                shell_check.start(session_id, owner_id, facts=facts, interpretation=interpretation)
        except Exception as exc:  # noqa: BLE001 - no shell check: the run still makes a said shell
            logger.warning("geometry naming: the shell check was not started (%s)", type(exc).__name__)
        proposal = _proposal(facts, {})
        result = {k: v for k, v in stored.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS}
        result.update(named=True, facts=facts, vision={}, proposal=proposal,
                      purpose_text=purpose_text[:2000], naming_seconds=round(time.time() - started, 1))
        landed = _land_named(session_id, result, stored)
        logger.info("geometry naming %s - session=%s (an FEA part: nothing to name)", landed["status"], session_id)
        return landed

    with tempfile.TemporaryDirectory(prefix=f"geometry_naming_{session_id[:8]}_") as tmp:
        store = get_object_store()
        shots: list[_Shot] = []
        for s in stored.get("snapshots") or []:
            dest = Path(tmp) / f"{s['name']}.png"
            try:
                store.download_file(object_key=s["object_key"], destination=dest)
            except Exception as exc:  # noqa: BLE001 - a missing picture costs one view, not the naming
                logger.warning("geometry naming: picture %s unavailable (%s)", s["name"], exc)
                continue
            shots.append(_shot_of(s, dest))
        vision = _name_with_vision(facts, shots, purpose_text=purpose_text, session_id=session_id,
                                   owner_id=owner_id)
        # WHICH WAY IS UP, by the pictures: asked only of a body the fluid flows around, once the
        # model has said what it is - the name is what lets it place a car it cannot place cold
        if "error" not in vision and _proposal(facts, vision)["flow"] == "external":
            seen = _upright_by_pictures(stored, tmp, part=str(vision.get("part") or ""),
                                        purpose_text=purpose_text, session_id=session_id, owner_id=owner_id)
            # a question that failed is its own failure, never the naming's: under "error" it
            # would make the whole answer read as unavailable
            if "error" in seen:
                seen = {"up_error": seen["error"]}
            vision.update(seen)
    # NOTE-5 ON THE FLOW ROUTE: a surface of several named parts (or a CAD file of several solids) keeps its parts
    # for the checklist before approval, as the FEA route's measurement does
    _keep_flow_parts(session_id, stored, facts, interpretation)
    proposal = _proposal(facts, vision)
    # what the scout stored, minus the fields write_status writes itself: the status, the clock,
    # and the session id - which it also takes as its first argument
    result = {k: v for k, v in stored.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS}
    result.update(named=True, facts=facts, vision=vision, proposal=proposal,
                  purpose_text=purpose_text[:2000], naming_seconds=round(time.time() - started, 1))
    # the write is part of the step: a result the store refuses is stored as a failure over the
    # scout's record, so the stage stops waiting and keeps the part
    landed = _land_named(session_id, result, stored)
    logger.info("geometry naming %s - session=%s in %.1fs (model %s)", landed["status"], session_id,
                result["naming_seconds"], "answered" if "error" not in vision else "unavailable")
    return landed


def _land_named(session_id: str, result: dict, named_from: dict) -> dict:
    """Store the naming's `ready` record. A naming made on a PREVIEW (it waited for the full reading
    as long as its limit allowed) is merged onto the full reading when that has landed meanwhile -
    its facts (read in the naming's unit), its openings and pictures, with what the naming added -
    so the measured part is never lost under a ready record built from the quick drawing. When the
    full reading has not landed, the record says it was named on the preview, and the scout merges
    its full reading in when it lands (run_geometry_check)."""
    result = dict(result)
    on_preview = result.pop("preview", None) == PREVIEW_RUNNING or bool(result.get("named_on_preview"))
    if on_preview:
        try:
            full = read_check(session_id)
        except Exception:  # noqa: BLE001 - unreadable: the naming's record stands as it is
            full = None
        if full is not None and full.get("status") == STATUS_SCOUTED and not is_preview(full) and full.get("facts"):
            result = merged_onto_full(result, named_from, full)
        else:
            result["named_on_preview"] = True
    return store_status(session_id, STATUS_READY, RETRY_NAMING, **result)


def merged_onto_full(named: dict, named_from: dict, full: dict) -> dict:
    """A ready record the naming built on the preview `named_from`, re-made on the scout's `full`
    reading: the full facts in the unit the naming settled (`scale_to_m`), plus every fact the naming
    added (an FEA part's measurement), the proposal drawn again from them with the model's answer,
    and the full reading's pictures and openings. Pure."""
    named_facts = dict(named.get("facts") or {})
    base_facts = (named_from or {}).get("facts") or {}
    facts = dict(full.get("facts") or {})
    try:
        k = float(named_facts.get("scale_to_m") or 0.0) / float(facts.get("scale_to_m") or 0.0)
    except (TypeError, ValueError, ZeroDivisionError):
        k = 1.0
    if k > 0 and abs(k - 1.0) > 1e-9:
        facts = _rescaled(facts, k)
        facts["scale_to_m"] = named_facts.get("scale_to_m")
        facts["unit_assumed"] = named_facts.get("unit_assumed", False)
        facts["notes"] = [n for n in facts.get("notes", []) if "assume millimetres" not in n]
    facts.update({key: v for key, v in named_facts.items() if key not in base_facts})
    vision = named.get("vision")
    out = {key: v for key, v in full.items() if key not in _STAMPED_KEYS + _FAILURE_KEYS + ("preview",)}
    out.update({key: named[key] for key in ("named", "vision", "purpose_text", "naming_seconds") if key in named})
    out.update(facts=facts, proposal=_proposal(facts, vision))
    out.pop("named_on_preview", None)
    return out


#: An upload larger than this is not measured for FEA in the naming step (the record says so; the
#: plan then sizes from the part's size alone). Version 1's parts are single parts, far smaller.
FEA_MEASURE_MAX_MB = 80.0
#: SHSTAGE-1: where an FEA part's facts keep what the user's words said about meshing it as a shell or a
#: solid (application/mesh_as.as_record).
MESH_AS_KEY = "mesh_as_words"


class _TooBig(ValueError):
    """A file past FEA_MEASURE_MAX_MB."""


def fea_measurements(stored: dict, facts: dict, interpretation: dict | None, early=None,
                     owner_id: str = "") -> dict:
    """THE FEA MEASUREMENT of the stored check's upload (cad/fea_measure): bodies, the wall under
    every face and the hot spots, in millimetres under the facts' unit (`scale_to_m` says which;
    application/fea_check.in_unit re-reads it in another). A CAD file is read on its faces, in the
    coordinates the scout read it in; a triangle file on its triangles. Never fails the naming: a
    part it cannot measure says why, and the plan sizes from what the check has.

    Nothing is measured twice: `early` is the same measurement begun while the naming waited for the
    scout (_EarlyFea), used when it was made from this very upload and unit; and a measurement of the
    same bytes, unit and clean verdict this owner already has (application/geometry_reuse, keyed on the
    upload's digest) is reused whole."""
    from meshpipeline.application import geometry_reuse as reuse
    from meshpipeline.cad.fea_measure import VERSION, measure_triangles

    scale = float(facts.get("scale_to_m") or 0.001)
    started = time.time()
    validity = facts.get("validity")
    key = None
    session_id = str(stored.get("session_id") or "")
    try:
        from meshpipeline.cad.ingest import canonicalise
        from meshpipeline.cad.scout_mesh import read_triangles
        from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
        from meshpipeline.contracts.intake_formats import GeometryKind

        ref = GeometrySourceRef.from_payload(stored["source"])
        key = reuse.fea_key(ref.sha256, interpretation, scale, validity)
        known = reuse.known_fea(owner_id, ref.sha256, key) if owner_id else None
        if known is not None:
            if early is not None:
                early.stop()
            logger.info("geometry check: FEA measurement reused from an earlier check of the same file")
            from meshpipeline.application import part_matches as PM
            if known.get(PM.PARTS_KEY) and session_id:
                known = {**known, PM.PARTS_KEY: PM.copy_parts(known.get(PM.PARTS_KEY), session_id)}
            return known
        interp_ref = GeometryInterpretationRef.from_payload(interpretation) if interpretation else None
        record = None
        if early is not None and early.serves(ref, interpretation):
            try:
                record = early.record(validity)
            except _NotEarly:
                record = None
        if record is None:
            with tempfile.TemporaryDirectory(prefix="fea_measure_") as tmp:
                work = Path(tmp)
                fetched = _fetch(ref, work)
                _too_big(fetched)
                canonical = canonicalise(fetched, work, stem="geometry", ecxml_form="placed")
                if canonical.kind is GeometryKind.cad:
                    shape, record, surface = _fea_measure_cad(canonical.path, interp_ref, ref)
                    record["suspects"] = _fea_suspects(shape, record, validity)
                    _keep_parts(session_id, record, surface)
                else:
                    record = measure_triangles(read_triangles(canonical.path) * scale * 1000.0)
        else:
            _keep_parts(session_id, record, early.surface() if early is not None else None)
        record.update(measured=True, scale_to_m=scale)
    except Exception as exc:  # noqa: BLE001 - the measurement informs the plan; the check still lands
        logger.warning("geometry check: the FEA measurement did not run (%s: %s)", type(exc).__name__,
                       str(exc)[:200])
        reason = str(exc) if isinstance(exc, _TooBig) else f"the part could not be measured ({type(exc).__name__})"
        record = {"version": VERSION, "measured": False, "reason": reason, "scale_to_m": scale,
                  "seconds": round(time.time() - started, 2)}
        key = None
    logger.info("geometry check: FEA measurement %s in %.1fs - %d hot spot(s)",
                "done" if record.get("measured") else "skipped", time.time() - started,
                len(record.get("hot_spots") or []))
    if key is not None and owner_id and record.get("measured"):
        reuse.remember_fea(owner_id, str(stored["source"].get("sha256") or ""), key, record,
                           session_id=str(stored.get("session_id") or ""))
    return record


def _too_big(fetched: Path) -> None:
    size_mb = fetched.stat().st_size / 1e6
    if size_mb > FEA_MEASURE_MAX_MB:
        raise _TooBig(f"the file is {size_mb:.0f} MB, over the {FEA_MEASURE_MAX_MB:.0f} MB measured "
                      "for a single part")


def _fea_measure_cad(path: Path, interp_ref, ref):
    """(the shape, its FEA record) for a CAD file - everything but the suspected defects, which read the
    clean verdict (_fea_suspects)."""
    from meshpipeline.cad.fea_measure import measure_cad_surface
    from meshpipeline.cad.normalise import occ_scale_transform
    from meshpipeline.cad.scout import read_cad

    # the scout's own reading: the confirmed unit when there is one, else the file's
    prepared, _note = _prepared_coordinates(path, interp_ref, ref)
    mm_per_unit = 1000.0 * float(occ_scale_transform(prepared).ScaleFactor())
    shape = read_cad(path)
    record, surface = measure_cad_surface(shape, mm_per_unit=mm_per_unit)
    sheet = False
    from meshpipeline.cad.surface_model import has_solid
    if not has_solid(shape):
        # SURF-1: A FILE WITH NO SOLID is read as a surface once, here, with the measurement (so a reused
        # measurement carries the reading): a sheet to mesh as a shell, a closed skin with a gap, or faces that close
        from meshpipeline.application import surface_check

        reading = surface_check.measure(shape, mm_per_unit=mm_per_unit)
        if reading is not None:
            record[surface_check.KEY] = reading
            # (its sheets are no assembly of solids: nothing touches, nothing is bonded)
            sheet = reading.get("kind") in (surface_check.SURFACE, surface_check.GAP)
    if int(record.get("bodies") or 0) > 1 and not sheet:
        # AN ASSEMBLY: its parts, where they touch or pass into each other, and every
        # face named Part_k_Face_j - the names the delivered files use
        from meshpipeline.cad.fea_assembly import global_face_names, measure_assembly
        record = measure_assembly(shape, record, source=path, mm_per_unit=mm_per_unit)
        # the labelled surface named the same way: the parts checklist draws and measures each part on it
        names = global_face_names(shape)
        if len(names) == len(surface.names):
            surface.names = list(names)
    return shape, record, surface


def _keep_flow_parts(session_id: str, stored: dict, facts: dict, interpretation: dict | None) -> None:
    """NOTE-5 on the flow route: the parts of an upload of several named surfaces (an STL's named solids, in the
    order the mesher stages them) or several CAD solids (Part_k in OpenCASCADE's order), kept for the checklist
    (application/part_matches, its CFD route). Never fails the naming; a single part keeps nothing."""
    try:
        from meshpipeline.application import part_matches as PM
        from meshpipeline.cad.ingest import canonicalise
        from meshpipeline.contracts.geometry_source import GeometrySourceRef
        from meshpipeline.contracts.intake_formats import GeometryKind
        from meshpipeline.engines.snappy import part_levels as PL

        if not session_id or not isinstance(stored.get("source"), dict):
            return
        ref = GeometrySourceRef.from_payload(stored["source"])
        scale = float(facts.get("scale_to_m") or 0.001)
        with tempfile.TemporaryDirectory(prefix="flow_parts_") as tmp:
            work = Path(tmp)
            fetched = _fetch(ref, work)
            _too_big(fetched)
            canonical = canonicalise(fetched, work, stem="geometry", ecxml_form="placed")
            if canonical.kind is GeometryKind.cad:
                from meshpipeline.cad.fea_assembly import global_face_names, solids
                from meshpipeline.cad.fea_measure import _brep_surface
                from meshpipeline.cad.scout import read_cad
                shape = read_cad(canonical.path)
                n = len(solids(shape))
                if n < 2:
                    return
                surface = _brep_surface(shape, 1000.0 * scale)[0]
                names = global_face_names(shape)
                if len(names) != len(surface.names):
                    return
                surface.names = list(names)
                record: dict = {"part_bases": [{"id": f"Part_{k}", "name": ""} for k in range(1, n + 1)]}
            else:
                from meshpipeline.cad.stl_io import read_stl_solids
                got = {k: v for k, v in read_stl_solids(canonical.path).items() if v}
                if len(got) < 2:
                    return
                surface, record, _by_id = PL.surface_of(
                    {k: [[[c * scale for c in p] for p in t] for t in v] for k, v in got.items()})
        kept = PM.store_parts(session_id, record, surface, route=PM.ROUTE_CFD)
        if kept is not None:
            facts[PM.PARTS_KEY] = kept
    except Exception as exc:  # noqa: BLE001 - no checklist, never a failed naming
        logger.info("geometry naming: the flow parts were not kept (%s: %s)", type(exc).__name__, str(exc)[:200])


def _keep_parts(session_id: str, record: dict, surface) -> None:
    """NOTE-4/6, BEFORE APPROVAL: an assembly's parts and the areas inside a part kept for the checklist (application/part_matches) - its facts
    table and pictures, drawn on the labelled surface that exists only here. Never fails the measurement."""
    if surface is None or not session_id:
        return
    from meshpipeline.application import part_matches as PM
    kept = PM.store_parts(session_id, record, surface)
    if kept is not None:
        record[PM.PARTS_KEY] = kept


def _fea_suspects(shape, record: dict, validity) -> list:
    """MESH-1: WHERE THE CAD MAY BE BROKEN, for the stage to WARN about (never a stop): the folds the
    measurement found and the crossings the clean check found, each confirmed only when a second,
    independent measure agrees (cad/suspect_defects). Never raises."""
    try:
        from meshpipeline.cad import suspect_defects as SD
        return SD.find(shape, record, validity=validity)
    except Exception as exc:  # noqa: BLE001 - no suspects measured is no warning
        logger.warning("geometry check: suspected defects not measured (%s)", type(exc).__name__)
        return []


def _same_json(a, b) -> bool:
    return json.dumps(jsonable(a), sort_keys=True) == json.dumps(jsonable(b), sort_keys=True)


class _EarlyFea:
    """AN FEA PART'S MEASUREMENT, BEGUN WHILE THE NAMING WAITS FOR THE SCOUT (1 Nov). The naming used to
    sleep until the scout had landed and only then measure: a landing gear's 186 s after its 82 s. What it
    measures needs only the upload and the unit (the walls, the hot spots, the assembly) and - for the
    suspected defects - the clean verdict, which the scout publishes the moment it has it (EARLY_VALIDITY).
    So both start as soon as they can, in a thread of the naming's own task, and the naming collects them
    once the scout lands. Each is used only when it was made from exactly what the naming would have used
    (the same upload digest, the same unit, the same clean verdict); otherwise it is measured as before.
    A CAD file only: a triangle file is quick to measure and needs the scout's unit."""

    def __init__(self, session_id: str, source: dict, interpretation: dict | None):
        import threading

        self.session_id = session_id
        self.sha256 = str(source.get("sha256") or "")
        self.interpretation = json.loads(json.dumps(jsonable(interpretation))) if interpretation else None
        self._source = dict(source)
        self._done = threading.Event()
        self._stop = threading.Event()
        self._shape = None
        self._surface = None
        self._record: dict | None = None
        self._error: BaseException | None = None
        self._suspects: list | None = None
        self._suspects_for: object = None
        self._thread = threading.Thread(target=self._run, name=f"fea-early-{session_id[:8]}", daemon=True)
        self._thread.start()

    def serves(self, ref, interpretation: dict | None) -> bool:
        same = json.loads(json.dumps(jsonable(interpretation))) if interpretation else None
        return ref.sha256 == self.sha256 and same == self.interpretation

    def stop(self) -> None:
        self._stop.set()

    def surface(self):
        """The labelled surface the early measurement measured on (None before, or when it did not run)."""
        return self._surface

    def _run(self) -> None:
        try:
            from meshpipeline.cad.ingest import canonicalise
            from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
            from meshpipeline.contracts.intake_formats import GeometryKind

            ref = GeometrySourceRef.from_payload(self._source)
            interp_ref = GeometryInterpretationRef.from_payload(self.interpretation) if self.interpretation else None
            with tempfile.TemporaryDirectory(prefix="fea_measure_") as tmp:
                work = Path(tmp)
                fetched = _fetch(ref, work)
                _too_big(fetched)
                canonical = canonicalise(fetched, work, stem="geometry", ecxml_form="placed")
                if canonical.kind is not GeometryKind.cad:
                    raise _NotEarly("a triangle file is measured once the scout lands")
                self._shape, self._record, self._surface = _fea_measure_cad(canonical.path, interp_ref, ref)
            # THE SUSPECTED DEFECTS, as soon as the clean verdict is known
            while True:
                validity = early_validity(self.session_id, self.sha256)
                if validity is not None:
                    self._suspects = _fea_suspects(self._shape, self._record, validity)
                    self._suspects_for = validity
                    break
                if self._stop.wait(2.0):
                    break
        except BaseException as exc:  # noqa: BLE001 - said where the naming would have said it
            self._error = exc
        finally:
            self._done.set()

    def record(self, validity) -> dict:
        """The record the naming would have measured, with `validity`'s suspects. Raises what measuring
        raised (the naming says it the same way); _NotEarly when nothing was measured early."""
        self.stop()                 # no more waiting for a verdict: the scout has landed with its own
        self._done.wait()
        if isinstance(self._error, _NotEarly) or (self._error is None and self._record is None):
            raise _NotEarly("not measured early")
        if self._error is not None:
            raise self._error
        record = dict(self._record or {})
        if self._suspects is not None and _same_json(self._suspects_for, validity):
            record["suspects"] = self._suspects
        else:
            record["suspects"] = _fea_suspects(self._shape, record, validity)
        return record


class _NotEarly(RuntimeError):
    """The early measurement does not apply; the naming measures as before."""


def _start_early_fea(session_id: str, stored: dict | None, purpose_text: str,
                     interpretation: dict | None) -> _EarlyFea | None:
    """Begin an FEA part's measurement while the scout still runs: once the stored check names the
    upload (the preview carries its source) and the user's words send the part to stress analysis, and
    only for an exact CAD upload (STEP, IGES)."""
    try:
        if not stored or not isinstance(stored.get("source"), dict) or not _is_fea(purpose_text):
            return None
        suffix = str(stored["source"].get("suffix_hint") or "").lower()
        if suffix not in (".step", ".stp", ".iges", ".igs"):
            return None
        return _EarlyFea(session_id, stored["source"], interpretation)
    except Exception as exc:  # noqa: BLE001 - measured after the scout, as before
        logger.info("geometry check: the FEA measurement could not start early (%s)", type(exc).__name__)
        return None


def _wait_for_scout(session_id: str, on_poll=None) -> dict | None:
    """The stored scout, once it is no longer pending. The user's first answer can land while the
    worker is still drawing; the naming waits rather than answering from nothing. `on_poll(stored)`
    is told every record seen while waiting (the FEA measurement begins on the preview's)."""
    # as long as this naming's own soft limit allows (sized to the file), less a minute to land
    wait_s = max(NAMING_WAIT_S, float(naming_limits_for(session_id)[0] - 60))
    deadline = time.time() + wait_s
    while True:
        stored = read_check(session_id)
        if on_poll is not None:
            on_poll(stored)
        # A PREVIEW is not the measured part: the scout is still at work on it, so the naming waits
        # for the full reading - and names the preview only if that does not come in time
        if stored is not None and stored.get("status") != STATUS_PENDING and not is_preview(stored):
            return stored
        if time.time() >= deadline:
            if stored is None or stored.get("status") == STATUS_PENDING:
                # a guard (docs/RULES.md TIME-4): the step fails and is retried, never answered
                # from less
                from meshpipeline.engines.determinism import note_guard_trip
                note_guard_trip("geometry_check.naming", rule="TIME-4",
                                guard_s=float(wait_s))
            return stored
        time.sleep(2.0)


def rescaled_lengths(value, k: float):
    """Every length in a facts dict re-read by factor k: `*_mm` and `*_m` scale by k, areas by k
    squared, nested lists and dicts alike. Keys that carry no unit are left alone. The naming
    re-reads the scout's facts with it; the confirm re-reads the user's answers the same way."""
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            if key.endswith("_mm2") or key.endswith("_m2"):
                out[key] = _scale_numbers(v, k * k)
            elif key.endswith("_mm") or key.endswith("_m") or key in ("bbox_min_m", "bbox_max_m"):
                out[key] = _scale_numbers(v, k)
            else:
                out[key] = rescaled_lengths(v, k)
        return out
    if isinstance(value, list):
        return [rescaled_lengths(v, k) for v in value]
    return value


_rescaled = rescaled_lengths


def _rescale_skin(session_id: str, k: float) -> None:
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    key = check_object_key(session_id, "skin.json")
    try:
        skin = json.loads(get_object_store().get_bytes(object_key=key))
    except ObjectNotFound:
        return
    import numpy as np

    blocks = list(skin.get("patches") or [])
    if isinstance(skin.get("edges"), dict):
        blocks.append(skin["edges"])                 # the sharp edges ride on their own points
    for p in blocks:
        for name in ("positions_b64", "points_b64"):
            if p.get(name):
                arr = np.frombuffer(base64.b64decode(p[name]), dtype=np.float32) * float(k)
                p[name] = base64.b64encode(arr.astype(np.float32).tobytes()).decode("ascii")
    # THE FACTOR TRAVELS WITH THE SKIN: anything placed on it from the kernel's own millimetres (a
    # repair review's cameras) reads it, as the stage reads scale_to_m. Cumulative across rescales.
    skin["rescaled_by"] = float(skin.get("rescaled_by") or 1.0) * float(k)
    _store_json(key, skin)


def _scale_numbers(v, k: float):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(v * k, 6)
    if isinstance(v, list):
        return [_scale_numbers(x, k) for x in v]
    return v


# ------------------------------------------------------------------ naming the stickers ----
NAME_TOOL = {
    "type": "function",
    "function": {
        "name": "name_geometry",
        "description": ("Say what the pictured part is and what each numbered sticker marks. Use "
                        "only the sticker numbers you were given; never invent one."),
        "parameters": {
            "type": "object",
            "properties": {
                "part": {"type": "string", "description": "What the part is, in a few words (a pipe elbow, a manifold, a car body, a city block)."},
                "flow": {"type": "string", "enum": ["internal", "external"],
                         "description": "internal: fluid flows THROUGH the part. external: fluid flows AROUND it."},
                "input_kind": {"type": "string", "enum": ["body-surface", "fluid-domain", "solid-body"],
                               "description": "body-surface: the part's wall, with a hollow inside for the fluid. "
                                              "fluid-domain: the fluid volume itself. solid-body: a body the fluid flows around."},
                "openings": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "id": {"type": "integer", "description": "The sticker number."},
                        "name": {"type": "string", "description": "A short patch name, e.g. inlet, outlet, outlet_2."},
                        "role": {"type": "string", "enum": ["inlet", "outlet", "not_an_opening"],
                                 "description": "inlet or outlet; not_an_opening for a sticker on a bolt hole, a mounting face or anything the fluid does not pass through."},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    }, "required": ["id", "name", "role", "confidence"]},
                },
                "nose_view": {"type": "string", "enum": ["overview", "iso", "top", "front", "side", "end", "unknown"],
                              "description": "EXTERNAL flow only: the picture in which the part's NOSE (the end that meets "
                                             "the oncoming fluid) is clearest. Prefer top, front, side or end over iso."},
                "nose_side": {"type": "string", "enum": ["left", "right", "top", "bottom", "unknown"],
                              "description": "EXTERNAL flow only: where the nose sits in that picture - at its left, right, "
                                             "top or bottom edge."},
                "nose_axis": {"type": "string", "enum": ["+x", "-x", "+y", "-y", "+z", "-z", "unknown"],
                              "description": "EXTERNAL flow only: the direction from the part's middle to its NOSE - the end "
                                             "that meets the oncoming fluid (a car's nose, a wing's leading edge, a nacelle's "
                                             "intake, an aircraft's nose) - read against the axis marker in the pictures. "
                                             "unknown when no end is plainly the nose."},
                "flow_axis": {"type": "string", "enum": ["+x", "-x", "+y", "-y", "+z", "-z", "unknown"],
                              "description": "EXTERNAL flow only: ONLY what the user's own words say about the direction "
                                             "the fluid travels (\"flow along +y\"). unknown when the user did not say. "
                                             "Never read it off the pictures: the nose fields do that."},
                "real_length_m": {"type": "number", "minimum": 0,
                                  "description": "How long a real one of what this part is usually is, in METRES, "
                                                 "judged from what the part is and the user's words - NOT from the "
                                                 "sizes you were given, which are read in a unit that may be wrong "
                                                 "(a wind turbine blade: about 60-120; a car: about 4.5; a pipe "
                                                 "elbow: about 0.1-1). Leave it out when you cannot tell."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "How sure you are about the part and the flow direction overall."},
                "notes": {"type": "string", "description": "Anything the user should check, in one or two plain sentences. Say if you see an opening that has no sticker."},
            },
            "required": ["part", "flow", "input_kind", "openings", "confidence"],
        },
    },
}

_SYSTEM = (
    "You are looking at pictures of one CAD part. Yellow stickers with numbers mark the openings a "
    "measuring step found; the numbers are the only names you may use. Say what the part is, "
    "whether fluid flows through it or around it, and for every sticker which opening it is and "
    "whether fluid enters (inlet) or leaves (outlet) there - or that it is not an opening at all "
    "(a bolt hole, a mounting face). The stickers and the measuring step's guess of the kind are a "
    "machine's first reading, not facts: a closed solid with flat ends may be the water inside a "
    "pipe, or a wing, a car body, a rotor with flat hub faces. The user's own description decides "
    "what the part is and which way the fluid goes; when they say the fluid flows AROUND the part, "
    "or the part is plainly a body, answer external / solid-body and mark every sticker "
    "not_an_opening. Every picture carries an axis marker in its corner: red is +X, green +Y, "
    "blue +Z, and each picture says which way its camera looks. For a body the fluid flows "
    "AROUND, first find its NOSE - the end that meets the oncoming fluid: a car's rounded front, a "
    "wing's rounded leading edge, a nacelle's intake, an aircraft's nose. Say which picture shows "
    "it best (nose_view) and at which edge of that picture it sits (nose_side: left, right, top or "
    "bottom); the code turns that into the part's axes. Give nose_axis too, read against the marker, "
    "when you can. The fluid then travels the opposite way. Say how long a real one of what this part "
    "is usually is (real_length_m, in metres) from what it is, never from the sizes: those are read in "
    "a unit that may be wrong. If the pictures do not settle something, say so with a low "
    "confidence rather than guessing confidently. Answer by calling name_geometry."
)


def _image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _facts_text(facts: dict, shots, purpose_text: str) -> str:
    from meshpipeline.render.scout_snapshots import camera_words

    lines = [f"Part size: {facts['size_mm'][0]:.0f} x {facts['size_mm'][1]:.0f} x {facts['size_mm'][2]:.0f} mm.",
             f"Measuring step's guess: {facts['input_kind']} ({facts['body_kind']}), flow {facts['flow']}."]
    lines.append("These sizes are read in the unit the file declares (or millimetres when it declares "
                 "none), which is sometimes wrong by a factor of 1000: judge real_length_m from what "
                 "the part is, not from them.")
    if facts.get("read_as") == "mesh":
        lines.append("The file is a triangle mesh, so the measurements are close, not exact.")
    if facts.get("input_kind") == "solid-assembly":
        regions = facts.get("source_regions") or []
        lines.append("The file itself declares this: an assembly of "
                     + (f"{len(regions)} regions (its parts and the air around them)" if regions
                        else "solid parts and the air around them")
                     + ", with its openings stated in the file. The kind, the flow and the stickers "
                     "are settled by the file; say what the part is.")
    if purpose_text:
        lines.append(f"The user said: {purpose_text.strip()[:600]}")
    if stickered(facts):
        lines.append("Stickers:")
        for o in stickered(facts):
            size = f"{o['diameter_mm']:.0f} mm across" if o["shape"] == "circle" \
                else f"{o.get('width_mm', 0):.0f} x {o.get('height_mm', 0):.0f} mm"
            lines.append(f"  {o['id']}: {o['shape']} opening, {size}, at ({o['centroid_mm'][0]:.0f}, "
                         f"{o['centroid_mm'][1]:.0f}, {o['centroid_mm'][2]:.0f}) mm; guessed {o['role']}")
    elif facts.get("flow") == "external":
        lines.append("The measuring step reads this as a body in a flow, so no stickers are drawn.")
    else:
        lines.append("The measuring step found no openings.")
    if facts.get("flow") == "external":
        from meshpipeline.contracts.geometry_fields import axis_from_reading
        shaped, reading = axis_from_reading(facts.get("flow_axis_reading"))
        if shaped:
            lines.append(f"Measuring step's reading of the flow direction: along {shaped[1]} ({reading.get('reason')}); "
                         "the sign is yours to decide from how the part faces.")
        elif facts.get("flow_axis_guess"):
            lines.append(f"Measuring step's guess for the flow direction: along {facts['flow_axis_guess']} "
                         "(its longest horizontal side); the sign is yours to decide from how the part faces.")
    lines.append("Axis marker in every picture: red +X, green +Y, blue +Z.")
    lines.append("Pictures, in order: " + "; ".join(
        f"{s.name} (stickers facing the camera: {', '.join(map(str, s.facing)) or 'none'})"
        + (f", camera looks along {camera_words(s.direction)}" if getattr(s, "direction", None) else "")
        for s in shots))
    return "\n".join(lines)


_AXES = ("+x", "-x", "+y", "-y", "+z", "-z")

#: A signed axis written out: "+y", "-X", "x+", "positive z", "minus y". Not one followed by "up"
#: ("+z up" says which way is up, not where the fluid goes).
_SIGNED_AXIS = re.compile(
    r"(?<![a-z0-9_])([+\-−])\s?([xyz])(?![a-z0-9_])(?!\s*(?:is\s+)?up\b)"
    r"|(?<![a-z0-9_])([xyz])([+\-−])(?![a-z0-9_])(?!\s*(?:is\s+)?up\b)"
    r"|\b(positive|plus|negative|minus)\s+([xyz])\b(?!\s*(?:is\s+)?up\b)")
#: An axis named without a sign, as a direction: "along x", "in the y direction", "z-direction".
_PLAIN_AXIS = re.compile(r"\balong\s+(?:the\s+)?([xyz])\b|\b([xyz])[\s\-]?direction\b")


def user_stated_flow_axes(purpose_text: str) -> set[str]:
    """The flow directions the user's own words state, read by rule: every signed axis written
    out, and "+a" for an axis named as a direction without a sign. What the model says the user
    said is only the user's words when it is one of these."""
    text = str(purpose_text or "").lower()
    found: set[str] = set()
    for m in _SIGNED_AXIS.finditer(text):
        sign, axis = (m.group(1), m.group(2)) if m.group(2) else (m.group(4), m.group(3)) if m.group(3) \
            else ("+" if m.group(5) in ("positive", "plus") else "-", m.group(6))
        found.add(("+" if sign == "+" else "-") + axis)
    for m in _PLAIN_AXIS.finditer(text):
        found.add("+" + (m.group(1) or m.group(2)))
    return found


def _flow_from_nose(answer: dict, shots=(), purpose_text: str = "") -> dict:
    """The fluid travels away from the nose: a nose at the -X end means flow along +x. The model
    finds a nose far more reliably than it reads an axis marker, so the surest answer is "in the top
    picture the nose is on the left": the camera of that picture says what "left" is in the part's
    axes. That settles nose_axis and flow_axis; the model's own nose_axis is the fallback.

    The model's flow_axis is the user's words only when the user's words state it
    (user_stated_flow_axes); else it is the model's own guess, which the nose replaces and the
    shape's reading weighs (weigh_flow_axis) like a nose."""
    from meshpipeline.render.scout_snapshots import screen_axes, signed_axis

    view = str(answer.get("nose_view") or "").lower()
    side = str(answer.get("nose_side") or "").lower()
    shot = next((s for s in shots if s.name == view), None)
    if shot is not None and side in ("left", "right", "top", "bottom") and getattr(shot, "up", None):
        right, up = screen_axes(shot.direction, shot.up)
        v = {"right": right, "left": tuple(-c for c in right), "top": up, "bottom": tuple(-c for c in up)}[side]
        answer["nose_axis"] = signed_axis(v)
        answer["nose_from"] = f"{side} of the {view} picture"
    nose = str(answer.get("nose_axis") or "").lower()
    said = str(answer.get("flow_axis") or "").lower()
    if said in _AXES and said in user_stated_flow_axes(purpose_text):
        answer["flow_from"] = "the user's words"       # the user's word decides; the nose only fills a silence
    elif nose in _AXES:
        answer["flow_axis"] = ("-" if nose[0] == "+" else "+") + nose[1]
        answer["flow_from"] = "the nose"
    elif said in _AXES:
        answer["flow_from"] = "the pictures"           # the model's own guess: weighed like a nose
    return answer


# ----------------------------------------------------------------------- which way is up ----
#: THE ONE QUESTION the model is asked about which way is up, over one picture: the part six
#: times, each panel turned so a different axis points up, the way the console's Up control turns
#: the camera (render/scout_snapshots.render_upright_sheet). Asked only of a body in a flow, and
#: only for things with an obvious top: asked of a wing on its own or a nacelle, the model picked
#: a panel with 0.9 confidence anyway.
UP_PANELS = ("A", "B", "C", "D", "E", "F")
UP_TOOL = {
    "type": "function",
    "function": {
        "name": "pick_upright",
        "description": "Say which panel shows the part the right way up, or none.",
        "parameters": {
            "type": "object",
            "properties": {
                "panel": {"type": "string", "enum": [*UP_PANELS, "none"],
                          "description": "The panel that shows the part the right way up; none when it has no "
                                         "right way up or you cannot tell."},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "why": {"type": "string", "description": "One plain sentence: what shows it."},
            },
            "required": ["panel", "confidence"],
        },
    },
}

_UP_SYSTEM = (
    "The picture shows one CAD part six times, in panels A to F, each turned a different way up. "
    "Only if the part is a thing with an obvious top and bottom - a car, truck, bus, train, motorbike, "
    "a whole aircraft with its fuselage, a ship, a building or a group of buildings - say which panel "
    "shows it the right way up: its top (roof, canopy, tail fin, deck) towards the top of the panel, "
    "and what it rests on (wheels, mounting struts or stilts, landing gear, a base) at the bottom. For "
    "anything else - a wing on its own, a nacelle or pod, a pipe or fitting, a duct, a rotor or "
    "propeller, a plain block, a teardrop, a nose cone - answer none: it has no right way up. If you "
    "cannot tell, answer none. Answer by calling pick_upright."
)


def _up_words(part: str, purpose_text: str) -> str:
    """What the model is told beside the six-way picture: what the part was named from its other
    pictures, and what the user said. Asked cold, the model called the SAE car body "a CAD part"
    and would not place it; told it is a car body, it picked the right panel."""
    lines = []
    if part:
        lines.append(f"The part was named from its other pictures: {part.strip()[:80]}.")
    if purpose_text:
        lines.append(f"The user said: {purpose_text.strip()[:600]}")
    lines.append("Which panel shows it the right way up?")
    return "\n".join(lines)


def _upright_answer(answer: dict, order) -> dict:
    """The model's panel as an up axis - {up_axis, up_confidence, up_from} - or {} for none."""
    from meshpipeline.contracts.geometry_fields import UP_AXES

    if not isinstance(answer, dict):
        return {"error": "malformed answer"}
    panel = answer.get("panel")
    panel = panel.strip().upper() if isinstance(panel, str) else ""
    order = list(order) if isinstance(order, (list, tuple)) else []
    if panel not in UP_PANELS or UP_PANELS.index(panel) >= len(order):
        return {}
    axis = str(order[UP_PANELS.index(panel)])
    if axis not in UP_AXES:
        return {}
    conf = _unit_number(answer.get("confidence")) or 0.0
    return {"up_axis": axis, "up_confidence": round(conf, 2), "up_panel": panel,
            "up_from": f"in the pictures it looks the right way up with {axis} up"}


def _up_with_vision(sheet: Path, order, *, part: str, purpose_text: str, session_id: str,
                    owner_id: str) -> dict:
    """The model's reading of which way is up from the six-way picture - {up_axis, up_confidence,
    up_from}, {} when it says the part has none, or {error} - never raised: the shape's reading
    and the file as drawn are always there underneath."""
    from meshpipeline.settings.geometry_check import GEOMETRY_CHECK_VISION_TIMEOUT_S

    async def _ask():
        from meshpipeline.contracts import model_inference as llm_router

        content = [{"type": "text", "text": _up_words(part, purpose_text)}, _image_part(sheet)]
        messages = [{"role": "system", "content": _UP_SYSTEM}, {"role": "user", "content": content}]
        result = await llm_router.call_reviewer_with_tools(messages, [UP_TOOL], job_id=session_id,
                                                           user_id=owner_id)
        for call in result.tool_calls:
            if call.name == "pick_upright":
                try:
                    return _upright_answer(json.loads(call.arguments), order)
                except Exception:  # noqa: BLE001 - a bad answer is no answer; the shape still says what it can
                    return {"error": "malformed answer"}
        return {"error": "the model answered without picking a panel"}

    try:
        answer = asyncio.run(asyncio.wait_for(_ask(), timeout=GEOMETRY_CHECK_VISION_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - provider weather; the shape still says what it can
        logger.warning("geometry check: which way is up unavailable from the pictures (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        _note_vision_timeout(exc, "the up axis was not read from the pictures")
        return {"error": f"{type(exc).__name__}"}
    return answer if isinstance(answer, dict) else {"error": "malformed answer"}


def _upright_by_pictures(stored: dict, tmp, *, part: str, purpose_text: str, session_id: str,
                         owner_id: str) -> dict:
    """Bring the scout's six-way picture back and ask it; {} when the scout drew none."""
    from meshpipeline.contracts.object_storage import get_object_store

    sheet = stored.get("upright_sheet") or {}
    if not sheet.get("object_key"):
        return {}
    dest = Path(tmp) / "upright.png"
    try:
        get_object_store().download_file(object_key=sheet["object_key"], destination=dest)
    except Exception as exc:  # noqa: BLE001 - one picture fewer, never a failed naming
        logger.warning("geometry naming: the six-way picture is unavailable (%s)", exc)
        return {}
    return _up_with_vision(dest, sheet.get("order") or [], part=part, purpose_text=purpose_text,
                           session_id=session_id, owner_id=owner_id)


#: How sure a reading must be to turn a part. The shape's are 0.75 (a scene) and 0.8 (feet). The
#: model, asked about a body the shape says nothing about, must be surer: it names a panel with
#: confidence where a person would hesitate.
CODE_TURNS_AT, MODEL_TURNS_AT = 0.6, 0.8


def decide_up(flow: str, code: dict | None, vision: dict | None) -> dict:
    """WHICH WAY IS UP, decided by the code from both readings: the shape's (cad/up_axis) and the
    pictures' (_up_with_vision). They agree: that, surer. The shape found feet or a scene: that -
    a measured contact outranks a picture read (the shape's reading was never wrong on 89 corpus
    parts turned all six ways; the model took the Ahmed body's stilts for a roof rack). The shape is
    silent and the model is sure: the model. Anything else: +z, the file as drawn - never a guess.
    A part the fluid flows through keeps +z: which way is up changes nothing about its mesh."""
    from meshpipeline.contracts.geometry_fields import DEFAULT_UP, UP_AXES

    def said(axis, conf, words):
        return {"up_axis": axis, "up_axis_confidence": round(float(conf), 2), "up_axis_from": words}

    if flow != "external":
        return said(DEFAULT_UP, 0.0, "as drawn: the fluid flows through the part")
    code = code if isinstance(code, dict) else {}
    vision = vision if isinstance(vision, dict) else {}
    c_axis = code.get("axis") if code.get("axis") in UP_AXES else None
    v_axis = vision.get("up_axis") if vision.get("up_axis") in UP_AXES else None
    # each reading's sureness on its own: a bad number from the model costs the model's reading,
    # never the shape's
    c_conf = _unit_number(code.get("confidence")) or 0.0
    v_conf = _unit_number(vision.get("up_confidence")) or 0.0
    shape = str(code.get("reason") or "")
    if c_axis and v_axis == c_axis:
        return said(c_axis, 1.0 - (1.0 - c_conf) * (1.0 - v_conf), f"{shape}, and the pictures agree")
    if c_axis and c_conf >= CODE_TURNS_AT:
        if v_axis:
            return said(c_axis, 0.75 * c_conf, f"{shape} (in the pictures it looked {v_axis} up)")
        return said(c_axis, c_conf, shape)
    if v_axis and v_conf >= MODEL_TURNS_AT:
        return said(v_axis, v_conf, str(vision.get("up_from") or f"it looks the right way up with {v_axis} up"))
    return said(DEFAULT_UP, 0.0, "as drawn: nothing says otherwise")


def _name_with_vision(facts: dict, shots, *, purpose_text: str, session_id: str, owner_id: str) -> dict:
    """The vision model's naming, or an empty dict with a note when it could not be had. The
    check never fails on this step: the code's own names are always there underneath."""
    from meshpipeline.settings.geometry_check import GEOMETRY_CHECK_VISION_TIMEOUT_S

    async def _ask():
        # the neutral contract the agents call through; the runtime injects the real router
        from meshpipeline.contracts import model_inference as llm_router

        content = [{"type": "text", "text": _facts_text(facts, shots, purpose_text)}]
        content += [_image_part(s.path) for s in shots]
        messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}]
        result = await llm_router.call_reviewer_with_tools(messages, [NAME_TOOL], job_id=session_id,
                                                           user_id=owner_id)
        for call in result.tool_calls:
            if call.name == "name_geometry":
                # THE ANSWER IS CHECKED BEFORE ANYTHING READS IT: a field the schema does not allow is
                # dropped, and an answer that cannot be read at all is no answer - the code's own
                # names ship. A replayed answer (the decision cache) passes the same check.
                try:
                    answer = _sanitize_vision(json.loads(call.arguments), facts)
                except Exception:  # noqa: BLE001 - not even JSON: no answer
                    answer = {"error": "malformed answer"}
                return answer if "error" in answer else _flow_from_nose(answer, shots, purpose_text)
        return {"error": "the model answered without naming the stickers"}

    try:
        answer = asyncio.run(asyncio.wait_for(_ask(), timeout=GEOMETRY_CHECK_VISION_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - provider weather; the check still ships
        logger.warning("geometry check: vision naming unavailable (%s: %s)", type(exc).__name__, str(exc)[:200])
        _note_vision_timeout(exc, "the openings were not named from the pictures")
        return {"error": f"{type(exc).__name__}"}
    return answer if isinstance(answer, dict) else {"error": "malformed answer"}


def _note_vision_timeout(exc: BaseException, what: str) -> None:
    # a guard (docs/RULES.md TIME-4): the same path as a provider outage - the code's own reading
    # stands and the user confirms the proposal; never a different answer chosen by the clock
    if isinstance(exc, TimeoutError):
        from meshpipeline.engines.determinism import note_guard_trip
        from meshpipeline.settings.geometry_check import GEOMETRY_CHECK_VISION_TIMEOUT_S
        note_guard_trip("geometry_check.vision", rule="TIME-4", step=what,
                        guard_s=float(GEOMETRY_CHECK_VISION_TIMEOUT_S))


# ------------------------------------------------------- the model's answer, guarded ----
#: How much surer than the measuring step the model must say it is before its kind and flow
#: replace the measured ones: a model names a kind with confidence where the shape hesitates.
MODEL_KIND_MARGIN = 0.15
_KINDS = ("body-surface", "fluid-domain", "solid-body")
_FLOW_OF_KIND = {"body-surface": "internal", "fluid-domain": "internal", "solid-body": "external"}
_ROLES = ("inlet", "outlet", "not_an_opening")
_NOSE_VIEWS = ("overview", "iso", "top", "front", "side", "end", "unknown")
_NOSE_SIDES = ("left", "right", "top", "bottom", "unknown")
_FLOW_FROM = ("the user's words", "the nose", "the pictures")
MALFORMED = "malformed answer"


def _unit_number(v) -> float | None:
    """A sureness from outside the code: a finite number in [0, 1], else None."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, x)) if math.isfinite(x) else None


def _opening_id(v) -> int | None:
    """A sticker number as a whole number, else None ("3" and 3.0 are 3; "three", 2.5 are not)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if math.isfinite(v) and v.is_integer() else None
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _word(v, allowed) -> str | None:
    w = v.strip().lower() if isinstance(v, str) else None
    return w if w in allowed else None


def _sanitize_vision(vision, facts: dict | None = None, *, derived: bool = False) -> dict:
    """The naming model's answer with only what its schema allows: every word checked against its
    list, every sureness a number in [0, 1], openings only as dicts naming a measured sticker. A
    field that fails is dropped; an answer that cannot be read at all is `{"error": MALFORMED}`, so
    the code's own names ship. `derived` keeps the fields the code itself adds after the model
    answers (where the flow direction came from, which way is up). Never raises."""
    try:
        return _sanitized(vision, facts if isinstance(facts, dict) else {}, derived)
    except Exception as exc:  # noqa: BLE001 - whatever the model sent, the rules stand
        logger.warning("geometry check: the naming answer could not be read (%s)", type(exc).__name__)
        return {"error": MALFORMED}


def _sanitized(vision, facts: dict, derived: bool) -> dict:
    from meshpipeline.contracts.geometry_fields import UP_AXES

    if not isinstance(vision, dict):
        return {"error": MALFORMED}
    if not vision:
        return {}
    if "error" in vision:
        return {"error": str(vision["error"])[:200]}
    out: dict = {}
    if isinstance(vision.get("part"), str) and vision["part"].strip():
        out["part"] = vision["part"].strip()[:120]
    for key, allowed in (("flow", ("internal", "external")), ("input_kind", _KINDS),
                         ("nose_view", _NOSE_VIEWS), ("nose_side", _NOSE_SIDES),
                         ("nose_axis", (*_AXES, "unknown")), ("flow_axis", (*_AXES, "unknown"))):
        if (w := _word(vision.get(key), allowed)) is not None:
            out[key] = w
    if (conf := _unit_number(vision.get("confidence"))) is not None:
        out["confidence"] = conf
    real = vision.get("real_length_m")
    if isinstance(real, (int, float)) and not isinstance(real, bool) and math.isfinite(real) and 0 < real < 1e6:
        out["real_length_m"] = float(real)
    if isinstance(vision.get("notes"), str) and vision["notes"].strip():
        out["notes"] = vision["notes"].strip()[:300]
    measured = {_opening_id(o.get("id")) for o in facts.get("openings") or [] if isinstance(o, dict)}
    named: list[dict] = []
    raw = vision.get("openings")
    for item in raw if isinstance(raw, list) else []:
        oid = _opening_id(item.get("id")) if isinstance(item, dict) else None
        if oid is None or oid not in measured or any(n["id"] == oid for n in named):
            continue
        entry: dict = {"id": oid}
        if isinstance(item.get("name"), str) and item["name"].strip():
            entry["name"] = item["name"].strip()[:40]
        if (role := _word(item.get("role"), _ROLES)) is not None:
            entry["role"] = role
        if (c := _unit_number(item.get("confidence"))) is not None:
            entry["confidence"] = c
        named.append(entry)
    if named or isinstance(raw, list):
        out["openings"] = named
    if derived:
        if (w := _word(vision.get("flow_from"), _FLOW_FROM)) is not None:
            out["flow_from"] = w
        if (w := _word(vision.get("up_axis"), UP_AXES)) is not None:
            out["up_axis"] = w
        if (c := _unit_number(vision.get("up_confidence"))) is not None:
            out["up_confidence"] = c
        if isinstance(vision.get("up_panel"), str) and vision["up_panel"] in UP_PANELS:
            out["up_panel"] = vision["up_panel"]
        for key in ("nose_from", "up_from", "up_error"):
            if isinstance(vision.get(key), str):
                out[key] = vision[key][:200]
    return out or {"error": MALFORMED}


def _inside_contradicts(facts: dict, kind: str, flow: str) -> str:
    """Why a kind and flow go against what was measured - the openings and the part's inside-space
    map (INS-1) - in plain words, else "". Fluid flowing through needs two openings at least, and a
    wall around a way through (or a closed volume, for the fluid itself); fluid flowing around never
    fits a part the map shows a passage through."""
    inside: dict = facts["inside"] if isinstance(facts.get("inside"), dict) else {}
    mapped = bool(inside) and inside.get("mapped") is not False
    if flow == "internal":
        if len([o for o in facts.get("openings") or [] if isinstance(o, dict)]) < 2:
            return "the measuring step found fewer than two openings for the fluid to pass"
        if mapped and not inside.get("has_inside"):
            if kind == "body-surface":
                return "the part's inside-space map shows no way through it"
            if "enclosed_mm3" in inside and float(inside.get("enclosed_mm3") or 0.0) <= 0.0:
                return "the part's inside-space map shows no volume for the fluid"
    if flow == "external" and mapped and inside.get("has_inside"):
        return "the part's inside-space map shows a passage through it"
    return ""


def _model_kind_flow(facts: dict, vision: dict) -> tuple[tuple[str, str] | None, str]:
    """The model's kind and flow, when they may replace the measured ones: they agree with each
    other, the model is surer than the code by MODEL_KIND_MARGIN, and they contradict nothing
    measured. Returns ((kind, flow) or None, a note for the user when a reading was set aside)."""
    said_kind, said_flow = vision.get("input_kind"), vision.get("flow")
    code_kind, code_flow = str(facts.get("input_kind") or ""), str(facts.get("flow") or "")
    flow = str(said_flow or _FLOW_OF_KIND.get(str(said_kind), ""))
    kind = str(said_kind or (code_kind if _FLOW_OF_KIND.get(code_kind) == flow else ""))
    if not flow or _FLOW_OF_KIND.get(kind) != flow or (kind, flow) == (code_kind, code_flow):
        return None, ""
    code_conf = _unit_number((facts.get("confidence") or {}).get("input_kind")) or 0.0
    if (vision.get("confidence") or 0.0) < code_conf + MODEL_KIND_MARGIN:
        return None, ""
    why = _inside_contradicts(facts, kind, flow)
    if why:
        words = "through it" if flow == "internal" else "around it"
        return None, (f"The pictures suggested the fluid flows {words}, but {why}, "
                      "so the measured reading stands.")
    return (kind, flow), ""


def _has_port_pair(openings) -> bool:
    roles = {o.get("role") for o in openings}
    return "inlet" in roles and "outlet" in roles


def _apply_vision(proposal: dict, facts: dict, vision: dict, declared_kind: bool) -> None:
    """The model's answer laid over the measured proposal, each change only where it is allowed:
    the kind and flow (_model_kind_flow), and each opening's role and name where the model is surer
    than the code about that opening - and never so that a part the fluid flows through is left
    without an inlet and an outlet."""
    if not declared_kind:
        kind_flow, note = _model_kind_flow(facts, vision)
        if kind_flow is not None:
            proposal["input_kind"], proposal["flow"] = kind_flow
        if note:
            proposal["notes"].append(note)
    before = [dict(o) for o in proposal["openings"]]
    by_id = {o["id"]: o for o in proposal["openings"]}
    for named in vision.get("openings") or []:
        o = by_id.get(named["id"])
        if o is None or o.get("declared_by"):
            continue
        role, conf = named.get("role"), named.get("confidence")
        code_conf = _unit_number(o.get("confidence"))
        code_conf = 0.5 if code_conf is None else code_conf
        if role is not None and role != o.get("role"):
            if conf is None or conf <= code_conf:
                continue          # the code was as sure: its role, and the name that goes with it, stand
            o["role"], o["confidence"] = role, round(conf, 2)
        elif conf is not None:
            o["confidence"] = round(max(conf, code_conf), 2)
        if named.get("name"):
            o["name"] = named["name"]
    if proposal["flow"] == "internal" and not _has_port_pair(proposal["openings"]) and _has_port_pair(before):
        proposal["openings"] = before
        proposal["notes"].append("The pictures' roles for the openings would leave no inlet and outlet, "
                                 "so the measured roles stand.")


def _proposal(facts: dict, vision: dict | None) -> dict:
    """What the user is shown: the code's positions and sizes, the model's names and roles where
    it gave them for a sticker that exists and is surer of, and the kind and flow from the model
    only where it is clearly surer and contradicts nothing measured. With no vision answer yet (the
    scout alone) the names are the code's and nothing says otherwise. Nothing the model sent can
    make this raise: an answer it cannot read leaves the code's own proposal."""
    if _is_thermal(facts):
        return _thermal_proposal(facts, vision)
    if vision is not None:
        vision = _sanitize_vision(vision, facts, derived=True)
    proposal = {
        "part": (vision or {}).get("part") or str(facts.get("part_name") or ""),
        "input_kind": facts["input_kind"], "flow": facts["flow"],
        "openings": [dict(o) for o in facts["openings"]],
        "seed_point_mm": facts.get("seed_point_mm"),
        "size_mm": facts["size_mm"],
        "notes": list(facts.get("notes") or []),
        # why the openings are what they are, when the file itself declares them (declared_openings)
        "openings_reason": str(facts.get("openings_reason") or ""),
        "read_as": facts.get("read_as", "cad"),
        "faces": list(facts.get("faces") or []),      # every flat face measured: what "add an opening" snaps to
        "holes": list(facts.get("holes") or []),      # every hole in the skin: what a click into one snaps to
        "named": vision is not None,
        "vision_available": bool(vision) and "error" not in (vision or {}),
    }
    # THE PART'S INSIDE (INS-1) and the stage's question when "through it" cannot be meshed as
    # proposed (INS-3): asked only if the user says the fluid flows through it
    if isinstance(facts.get("inside"), dict):
        from meshpipeline.application.geometry_confirmation import flow_question
        proposal["inside"] = dict(facts["inside"])
        if isinstance(facts.get("surface"), dict):
            proposal["surface"] = dict(facts["surface"])
        proposal["flow_question"] = flow_question(facts["inside"], proposal["openings"], facts.get("surface"))
    # THE LEAKS (INS-8): the openings above assume them closed; the stage proposes closing every one
    # up to the size proposed here, a switch the user can turn off or a size they can change
    if isinstance(facts.get("leaks"), dict) and facts["leaks"].get("found"):
        proposal["leaks"] = dict(facts["leaks"])
    if vision is None:
        return _upright(proposal, facts, None, None)
    if "error" in vision:
        proposal["notes"].append("the picture-naming step was unavailable; names are the measuring step's own")
        return _upright(proposal, facts, None, None)
    # WHAT THE FILE ITSELF DECLARES IS NOT THE PICTURES' TO OVERRULE. A thermal model (ECXML) names
    # its parts and says which one is the air, and which sides of its domain are open, fans or
    # vents: the scout read that as an assembly of regions with its openings. The model, shown a
    # box, called the set-top box "a solid body the fluid flows around" and marked its six open
    # sides "not an opening", and the chat then refused the multi-region mesher the file is for.
    # The model still names the part; the kind, the flow and the declared openings stay the file's.
    declared_kind = facts.get("input_kind") == "solid-assembly"
    try:
        _apply_vision(proposal, facts, vision, declared_kind)
        # how long a real one is, by the model's knowledge of what it is: what the stage checks
        # the unit against (unit_suggestion), never a size applied to anything
        if vision.get("real_length_m"):
            proposal["real_length_m"] = round(float(vision["real_length_m"]), 6)
        if vision.get("notes") and not declared_kind:
            proposal["notes"].append(vision["notes"])
        # the stage's flow question reads the roles and the flow as now proposed
        if isinstance(facts.get("inside"), dict):
            from meshpipeline.application.geometry_confirmation import flow_question
            proposal["flow_question"] = flow_question(facts["inside"], proposal["openings"], facts.get("surface"))
        return _upright(proposal, facts, vision, vision.get("flow_axis"))
    except Exception as exc:  # noqa: BLE001 - the model's answer never fails the check: the rules stand
        logger.warning("geometry check: the naming answer was set aside (%s: %s)", type(exc).__name__, str(exc)[:200])
        return _proposal(facts, {"error": MALFORMED})


def _upright(proposal: dict, facts: dict, vision: dict | None, flow_axis) -> dict:
    """The proposal with which way is up decided (decide_up), and the external-flow facts read
    across it: the flow's direction - the user's words, else the shape's own reading or the nose
    the pictures show (weigh_flow_axis), else the longest horizontal side - and the ground only
    where the measuring step looked for it. A part drawn another way up says so in the notes, and
    a body in a flow says where its direction came from and how sure that is."""
    from meshpipeline.contracts.geometry_fields import DEFAULT_UP, external_defaults

    up = decide_up(proposal["flow"], facts.get("up_evidence"), vision)
    proposal.update(up)
    flow_axis, overruled = weigh_flow_axis(facts, vision, flow_axis, up["up_axis"])
    proposal.update(external_defaults(facts, flow_axis, up["up_axis"]))
    from_nose = proposal.get("flow_axis_guessed") is False and (vision or {}).get("flow_from") != "the user's words"
    if from_nose:
        proposal.update(nose_sureness(facts, proposal["flow_axis"], up["up_axis"]))
        if (vision or {}).get("flow_from") == "the pictures":
            # the model named a direction the user never stated: as sure as a nose, and said so
            proposal["flow_axis_from"] = str(proposal["flow_axis_from"]).replace(
                "the nose in the pictures", "the picture-naming step's guess")
    if overruled:
        proposal["flow_axis_guessed"] = True
    if up["up_axis"] != DEFAULT_UP:
        proposal["notes"].append(f"Up is {up['up_axis'].upper()}: {up['up_axis_from']}.")
    if proposal["flow"] == "external" and (proposal.get("flow_axis_guessed") or from_nose):
        proposal["notes"].append(direction_note(proposal, overruled))
    if facts.get("unit_assumed"):
        # A FILE THAT STATES NO UNIT gets a proposed one, read from what the part is (its shape
        # and flow, and the naming's estimate once there is one) - shown with how sure it is and
        # the other reading, for the user to confirm. Never applied: the sizes stay as measured.
        inferred = unit_inference(proposal, facts)
        if inferred is not None:
            proposal["unit_inference"] = inferred
    return proposal


#: How sure a nose read off the pictures is as the flow direction on its own.
NOSE_CONFIDENCE = 0.6


def nose_sureness(facts: dict, axis: str, up_axis: str) -> dict:
    """Where a direction from the pictures' nose came from and how sure it is: surer when the
    shape's own reading runs along the same axis (the nose then only gives the sign)."""
    from meshpipeline.contracts.geometry_fields import axis_from_reading

    shaped, reading = axis_from_reading(facts.get("flow_axis_reading"), up_axis)
    if shaped and shaped[1] == axis[1]:
        sure = max(NOSE_CONFIDENCE, float(reading.get("confidence") or 0.0))
        return {"flow_axis_confidence": round(sure, 2),
                "flow_axis_from": f"the nose in the pictures, along the shape's own axis ({reading.get('reason')})"}
    return {"flow_axis_confidence": NOSE_CONFIDENCE, "flow_axis_from": "the nose in the pictures"}


#: How sure the shape's reading of the flow direction must be to stand against the nose the
#: pictures show (a rotor's axis, a body of revolution's, a mirror plane are; a cut face, a
#: straight bar's span and the longest side are not - there the nose speaks).
SHAPE_OUTRANKS_NOSE = 0.7
#: Below this, the direction is said to be a guess the user should check on the picture.
DIRECTION_SURE = 0.6


def weigh_flow_axis(facts: dict, vision: dict | None, flow_axis, up_axis: str) -> tuple[str | None, str]:
    """THE FLOW DIRECTION, weighed: the user's own words decide. A nose read off the pictures
    gives the sign along the shape's axis, but where the shape is sure of another axis - blades
    round a hub turn the flow along their axis, whichever end the model took for a nose - the
    shape's axis stands. Returns the axis to propose (None: the shape's or the longest side's) and
    what was overruled, if anything."""
    from meshpipeline.contracts.geometry_fields import AXES, axis_from_reading

    if flow_axis not in AXES or (vision or {}).get("flow_from") == "the user's words":
        return flow_axis, ""
    # EVERY reading is weighed, not only the first: a car's wheels (an unsure sign, so first and
    # under the bar) and its mirror plane (sure) are two readings, and either can rule the nose out
    readings = facts.get("flow_axis_reading")
    vehicle = ((facts.get("reading") or {}).get("vehicle") or {}).get("axis")
    for one in ([readings] if isinstance(readings, dict) else readings or []):
        shaped, reading = axis_from_reading([one], up_axis)
        if not shaped or shaped[1] == flow_axis[1]:
            continue
        # A VEHICLE'S LENGTH IS MEASURED (cad/flow_reading.on_wheels): a nose lies at one end of
        # it, so a "nose" across it was read off a picture that shows the vehicle end-on. Only
        # which end leads is unsure, and that is the nose's to say when it lies along the length.
        along_vehicle = reading.get("rule") == "along" and vehicle in (reading.get("axes") or [])
        if along_vehicle or float(reading.get("confidence") or 0.0) >= SHAPE_OUTRANKS_NOSE:
            return None, f"the pictures' nose said {flow_axis}"
    return flow_axis, ""


def direction_note(proposal: dict, overruled: str = "") -> str:
    """The flow direction as the user reads it on the form: where it came from and how sure."""
    axis = proposal.get("flow_axis")
    why = str(proposal.get("flow_axis_from") or "")
    sure = float(proposal.get("flow_axis_confidence") or 0.0)
    words = f"The flow is proposed along {axis}: {why}"
    if overruled:
        words += f" ({overruled}; the shape's axis is surer)"
    return words + (". Please confirm it on the picture." if sure < DIRECTION_SURE else ".")



def unit_inference(proposal: dict, facts: dict | None = None, words: str = "") -> dict | None:
    """The unit a file that states none most likely is in (contracts/unit_plausibility.infer_unit),
    as a dict, or None when the part has no measured size. What the part is comes, best first,
    from the naming model's estimate of a real one's length, then the words for it (its name, the
    user's own words), then the measured shape and the flow. The file's numbers are read back
    through the scale the proposal was measured under."""
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.unit_plausibility import (
        FORMAT_UNIT,
        expected_from_estimate,
        expected_from_words,
        infer_unit,
        shape_kind,
    )

    size, scale = proposal.get("size_mm"), proposal.get("scale_to_m") or (facts or {}).get("scale_to_m")
    try:
        if not size or len(size) != 3:
            return None
        longest_file = max(float(v) for v in size) * 0.001 / float(scale or 0.001)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    part = str(proposal.get("part") or "")
    expected = (expected_from_estimate(part, proposal.get("real_length_m"))
                or expected_from_words(f"{part}\n{words or ''}"))
    # an FEA part (its measurement is in the facts) has no flow: it reads as a part, never as "a
    # body in an external flow"
    flow = "" if (facts or {}).get("fea_measure") else str(proposal.get("flow") or "")
    shape = shape_kind(flow, size, grounded=bool(proposal.get("grounded")), up_axis=proposal.get("up_axis"))
    fmt = str((facts or {}).get("source_format") or "").lower().lstrip(".")
    try:
        stated = LengthUnit((facts or {}).get("unit_stated_mostly") or "")
    except ValueError:
        stated = None
    inferred = infer_unit(longest_file, expected=expected, shape=shape, format_unit=FORMAT_UNIT.get(fmt),
                          stated=stated)
    return inferred.as_dict() if inferred is not None else None


def is_fea_purpose(purpose_text: str) -> bool:
    """Whether the user's words send the part down the FEA path (the progress line's words follow)."""
    return _is_fea(purpose_text)


def _is_fea(purpose_text: str) -> bool:
    """Whether the user's words send the part down the FEA path (application/purpose_map). The
    latest answer speaks first when it settles the purpose alone - it may be the answer to the
    purpose question - else everything said, read together, as the stage reads it."""
    from meshpipeline.application.purpose_map import ROUTE_FEA, read_purpose

    lines = [ln for ln in str(purpose_text or "").splitlines() if ln.strip()]
    last = read_purpose(lines[-1]) if lines else None
    reading = last if last is not None and last.settled else read_purpose(purpose_text)
    return reading.route == ROUTE_FEA


def _is_thermal(facts: dict | None) -> bool:
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    return (facts or {}).get("check_mode") == THERMAL_CHECK


def _thermal_proposal(facts: dict, vision: dict | None) -> dict:
    """A thermal model's proposal: the file's title, an assembly of regions, no openings, and
    the file's own up (+z, which every thermal tool takes) - what the form shows until the
    thermal panel replaces it. Nothing in it is a guess for the user to correct."""
    from meshpipeline.contracts.geometry_fields import DEFAULT_UP, external_defaults
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    proposal = {
        "part": str(facts.get("title") or ""), "input_kind": "solid-assembly", "flow": "internal",
        "openings": [], "seed_point_mm": None, "size_mm": facts["size_mm"],
        "notes": list(facts.get("notes") or []), "openings_reason": "",
        "read_as": facts.get("read_as", "thermal model"), "faces": [], "holes": [],
        "named": vision is not None, "vision_available": False, "check_mode": THERMAL_CHECK,
        "up_axis": DEFAULT_UP, "up_axis_confidence": 1.0, "up_axis_from": "as the file draws it",
    }
    proposal.update(external_defaults(facts, None, DEFAULT_UP))
    return proposal


# kept for callers and tests that knew the check by its first name
_merge = _proposal


def stored_up_axis(session_id: str, *, confirmed: bool = True) -> str | None:
    """Which way is up for a session's part, for the views that draw it: what the user confirmed
    on the geometry check, else what the check proposed (only that, with `confirmed=False`); None
    when neither says. Display only - it turns the camera, never the file or the mesh."""
    from meshpipeline.contracts.geometry_fields import UP_AXES
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    store = get_object_store()
    for name in ("confirmed.json", "scout.json") if confirmed else ("scout.json",):
        try:
            stored = json.loads(store.get_bytes(object_key=check_object_key(session_id, name)))
        except ObjectNotFound:
            continue
        if not isinstance(stored, dict):
            continue
        axis = stored.get("up_axis") if name == "confirmed.json" else (stored.get("proposal") or {}).get("up_axis")
        if axis in UP_AXES:
            return str(axis)
    return None


def unit_suggestion(proposal: dict, interpretation: dict | None, words: str = "") -> dict | None:
    """The other unit to show beside the part's size on the stage, or None when the part is
    believable in the unit in effect - or the user has already named the unit, which settles it.

    The part's longest side in the file's own numbers comes from the proposal's size, read back
    through the scale it was measured under. What the part is comes, best first, from the naming
    model's estimate of how long a real one is, else from the words for it (the model's name for
    the part, the user's own words). A declared unit is only doubted on those words or on a wild
    size; a unit nothing declared (a triangle file) is only a reading, and yields to either."""
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.unit_plausibility import (
        EXTERNAL_BODY,
        expected_from_estimate,
        expected_from_words,
        shape_kind,
        suggest,
    )

    if interpretation and str(interpretation.get("basis")) == "user_confirmed":
        return None
    size, scale = proposal.get("size_mm"), proposal.get("scale_to_m")
    try:
        if not size or len(size) != 3 or not scale:
            return None
        longest_file = max(float(v) for v in size) * 0.001 / float(scale)
        in_effect = LengthUnit(str(interpretation["unit"])) if interpretation else LengthUnit.millimetre
    except (TypeError, ValueError, KeyError):
        return None
    part = str(proposal.get("part") or "")
    expected = (expected_from_estimate(part, proposal.get("real_length_m"))
                or expected_from_words(f"{part}\n{words or ''}"))
    if expected is None and interpretation is None:
        # nothing names the part and the file states no unit: what its SHAPE and FLOW say it is
        # (a car-like body on the ground, a flat wing-like body, a passage) - a 4.7-unit car is
        # 4.7 m, not 4.7 mm. A declared unit is not doubted on the shape alone.
        shape = shape_kind(str(proposal.get("flow") or ""), size,
                           grounded=bool(proposal.get("grounded")), up_axis=proposal.get("up_axis"))
        expected = shape if shape is not EXTERNAL_BODY else None
    s = suggest(longest_file, in_effect, declared=interpretation is not None, expected=expected)
    return s.as_dict() if s is not None else None
