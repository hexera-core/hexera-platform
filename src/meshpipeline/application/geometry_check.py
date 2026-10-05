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
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: pending -> scouted (measured, pictured, code names) -> ready (named with the user's words);
#: failed and unsupported are terminal and say why.
STATUS_PENDING, STATUS_SCOUTED, STATUS_READY = "pending", "scouted", "ready"
STATUS_FAILED, STATUS_UNSUPPORTED = "failed", "unsupported"
_CAD_SUFFIXES = (".step", ".stp", ".igs", ".iges")
_MESH_SUFFIXES = (".stl", ".obj", ".vtp")
#: THE WORKER'S LIMITS for the two steps, in seconds. Past the soft one the task is told to stop
#: and stores `failed` itself; past the hard one it is killed and stores nothing - and a worker
#: whose VM is deleted mid-task stores nothing either. The Celery tasks take these same numbers,
#: so the time box the API reads the check through (`time_boxed`) cannot drift from them.
SCOUT_SOFT_LIMIT_S, SCOUT_HARD_LIMIT_S = 900, 1200
NAMING_SOFT_LIMIT_S, NAMING_HARD_LIMIT_S = 600, 900
#: How long past its hard limit a step is still believed to be running: queue time on a busy
#: fleet, and clock skew between the API and the worker.
STALE_MARGIN_S = 120.0
#: How long the naming step waits for a scout that is still running before giving up. Under its
#: own soft limit on purpose: a wait that outlived the limit was ended by the worker with a
#: `failed` written over the scout's `pending`, instead of the request being withdrawn so the
#: next chat turn can ask again once the scout has landed.
NAMING_WAIT_S = float(NAMING_SOFT_LIMIT_S - 60)
#: The plain reasons a time-boxed check reports, and the step a retry runs again.
SCOUT_TOO_LONG = "the drawing took too long"
NAMING_TOO_LONG = "the naming step did not answer"
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
        return {"status": status, **fields}
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


def time_boxed(stored: dict | None, requested: dict | None, now: float | None = None) -> dict | None:
    """THE CHECK AS IT SHOULD BE REPORTED NOW. A scout still `pending` past its hard limit (plus
    the margin) was killed, or lost its worker: nothing will ever write its result, so it is
    reported `failed` with a plain reason and the step a retry runs. A `scouted` check whose
    naming was requested that long ago and never answered is reported the same way, with the
    scout's proposal and skin still on it so the user can fill the names in by hand. Pure, and
    the one rule every reader applies; it stores nothing, so a worker that was merely slow still
    lands its result and the next read serves it."""
    if stored is None:
        return None
    at = time.time() if now is None else now
    status = stored.get("status")
    if status == STATUS_PENDING:
        since = float(stored.get("written_at") or 0.0)
        if at - since > SCOUT_HARD_LIMIT_S + STALE_MARGIN_S:
            return {**stored, "status": STATUS_FAILED, "reason": SCOUT_TOO_LONG, "named": False,
                    "step": RETRY_SCOUT, "retry": RETRY_SCOUT, "timed_out": True}
    elif status == STATUS_SCOUTED and naming_pending(requested):
        asked = float((requested or {}).get("requested_at") or 0.0)
        if at - asked > NAMING_HARD_LIMIT_S + STALE_MARGIN_S:
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

    marked = False
    try:
        write_status(session_id, STATUS_PENDING)
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
    _store_json(check_object_key(session_id, "naming.json"),
                {"session_id": session_id, "purpose_text": purpose_text[:2000], "requested_at": time.time()})


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
    from meshpipeline.render.skin_mesh import edges_block, prepare_skin, skin_patch

    tris = np.asarray(read_stl_triangles(Path(skin_stl)), dtype=float).reshape(-1, 3, 3)
    if len(tris) == 0:
        raise RuntimeError("the part's skin has no triangles to draw")
    prepared = prepare_skin(tris)
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
            "patches": [skin_patch(prepared)], "edges": edges_block(prepared)}


# ------------------------------------------------------------------------------ the scout ----
def run_geometry_check(*, session_id: str, owner_id: str, source: dict,
                       interpretation: dict | None = None, purpose_text: str = "") -> dict:
    """The scout task body: measure, picture, store. Returns the stored result; every failure is
    stored too, as a status the console can show, because a check that silently never arrives is
    worse than one that says it could not read the part. `purpose_text` is accepted for the
    older single-step callers and ignored: naming has its own step now."""
    started = time.time()
    try:
        result = _scout(session_id=session_id, owner_id=owner_id, source=source,
                        interpretation=interpretation)
    except _Unsupported as exc:
        result = {"status": STATUS_UNSUPPORTED, "reason": str(exc), "named": False}
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry scout failed - session=%s", session_id)
        result = {"status": STATUS_FAILED, "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                  "named": False, "step": RETRY_SCOUT, "retry": RETRY_SCOUT}
    result["seconds"] = round(time.time() - started, 1)
    # THE WRITE IS PART OF THE TASK: a result that cannot be stored is stored as a failure, so the
    # stage never waits on a `pending` nothing will replace
    stored = store_status(session_id, result.pop("status"), RETRY_SCOUT, **result)
    logger.info("geometry scout %s - session=%s in %.1fs", stored.get("reason") or stored["status"],
                session_id, result["seconds"])
    return stored


class _Unsupported(RuntimeError):
    pass


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


def _scout(*, session_id: str, owner_id: str, source: dict, interpretation: dict | None) -> dict:
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.render.scout_snapshots import render_snapshots

    ref = GeometrySourceRef.from_payload(source)
    suffix = (ref.suffix_hint or "").lower()
    if suffix not in _CAD_SUFFIXES + _MESH_SUFFIXES:
        raise _Unsupported("the geometry check reads STEP, IGES, STL, OBJ and VTP files; this upload "
                           f"is a {suffix or 'nameless'} file, so the intake will ask about its openings")
    interp_ref = GeometryInterpretationRef.from_payload(interpretation) if interpretation else None

    # The workspace lives exactly as long as the check. The worker runs for weeks; every upload,
    # skin and picture left behind in /tmp would stay there until the disk was full.
    with tempfile.TemporaryDirectory(prefix=f"geometry_check_{session_id[:8]}_") as tmp:
        work = Path(tmp)
        local_path = _fetch(ref, work)
        if suffix in _CAD_SUFFIXES:
            facts, skin = _scout_exact(local_path, work, interp_ref, ref)
        else:
            facts, skin = _scout_triangles(local_path, work, interp_ref, ref)

        # the same skin, stored for the stage the user turns the part in
        skin_key = check_object_key(session_id, "skin.json")
        _store_json(skin_key, skin_payload(skin))
        # which way the part stands up, as far as its shape says: the code's half of the up
        # proposal, weighed against the pictures once the model has seen them
        facts["up_evidence"] = read_up_evidence(skin)
        shots = render_snapshots(skin, facts["openings"], work / "pictures")

        store = get_object_store()
        snapshots = []
        for s in shots:
            key = check_object_key(session_id, f"{s.name}.png")
            store.upload_file(local_path=s.path, object_key=key)
            # the camera goes with the picture: "the nose is at the left of the top picture" is
            # read through it
            snapshots.append({"name": s.name, "object_key": key, "facing": list(s.facing),
                              "direction": list(s.direction), "up": list(s.up)})
        upright = _store_upright_sheet(session_id, skin, work, store)
    result = {"status": STATUS_SCOUTED, "named": False, "facts": facts, "proposal": _proposal(facts, None),
              "snapshots": snapshots, "skin_key": skin_key, "source": ref.to_payload()}
    if upright is not None:
        result["upright_sheet"] = upright
    return result


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


def _scout_exact(local_path: Path, work: Path, interp_ref, ref) -> tuple[dict, Path]:
    """A STEP or IGES file: OpenCASCADE reads real faces, so the openings come out exact."""
    from meshpipeline.cad.scout import read_cad, scout_cad, write_view_stl

    prepared, unit_note = _prepared_coordinates(local_path, interp_ref, ref)
    # read once: the scout and the view skin each work on their own scaled copy of it
    shape = read_cad(local_path)
    facts = scout_cad(local_path, prepared=prepared, shape=shape).as_dict()
    facts["read_as"] = "cad"
    facts["unit_assumed"] = bool(unit_note)
    interp = getattr(prepared, "interpretation", None)
    facts["scale_to_m"] = float(interp.scale_to_metres) if interp is not None else 0.001
    if unit_note:
        facts["notes"].append(unit_note)
    skin = write_view_stl(local_path, work / "skin.stl", prepared=prepared, shape=shape)
    facts["holes"] = skin_holes(skin)
    return facts, skin


def skin_holes(skin: Path) -> list[dict]:
    """The holes of the skin the stage draws, by the one definition the triangle scout uses
    (cad/open_ends) - what "Add an opening" snaps to. A STEP file's openings are measured exactly
    from its faces; these are for the click, on the same triangles the user clicks. Never fails
    the scout: a skin it cannot read has no holes to snap to, and the click falls back."""
    import numpy as np

    from meshpipeline.cad.open_ends import find_holes
    from meshpipeline.cad.stl_io import read_stl_triangles

    try:
        tris = np.asarray(read_stl_triangles(Path(skin)), dtype=float).reshape(-1, 3, 3)
        return [h.as_dict() for h in find_holes(tris)]
    except Exception as exc:  # noqa: BLE001 - a click aid, never a reason to fail the check
        logger.warning("geometry check: the skin's holes could not be read (%s: %s)", type(exc).__name__, exc)
        return []


def _scout_triangles(local_path: Path, work: Path, interp_ref, ref) -> tuple[dict, Path]:
    """An STL, OBJ or VTP file: triangles only, so the openings are read from loops and flat
    rings in the mesh - close, not exact - and the unit is whatever the user confirms."""
    from meshpipeline.cad.scout_mesh import scout_mesh, write_mesh_skin
    from meshpipeline.contracts.geometry_units import LengthUnit, scale_to_metres

    if interp_ref is not None:
        scale, note = float(interp_ref.scale_to_metres), ""
    else:
        scale = scale_to_metres(LengthUnit.millimetre)
        note = "a triangle file carries no unit; sizes assume millimetres until the intake confirms"
    result = scout_mesh(local_path, scale_to_m=scale)
    facts = result.as_dict()
    facts.update(getattr(result, "extra", {}) or {})
    facts["read_as"] = "mesh"
    facts["unit_assumed"] = bool(note)
    facts["scale_to_m"] = scale
    if note:
        facts["notes"].append(note)
    skin = write_mesh_skin(local_path, work / "skin.stl", scale_to_m=scale)
    return facts, skin


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
            unit = LengthUnit.millimetre
            note = "the file does not say its unit; sizes assume millimetres until the intake confirms"
        interp = GeometryInterpretation(
            interpretation_id="unconfirmed", owner_id=ref.owner_id, geometry_source_id=ref.source_id,
            unit=unit, scale_to_metres=scale_to_metres(unit), basis=ResolutionBasis.file_declared,
            evidence="geometry check")
    return from_occ_transfer(interp, parser_applied_unit(path)), note


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
    try:
        return _name(session_id=session_id, owner_id=owner_id, purpose_text=purpose_text,
                     interpretation=interpretation)
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry naming failed - session=%s", session_id)
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            # a duplicate attempt may already have named the check: a late failure never
            # replaces a result the user can act on
            current = read_check(session_id)
            if current is not None and current.get("status") == STATUS_READY and current.get("named"):
                logger.info("geometry naming: a failed attempt left the ready check alone - session=%s", session_id)
                return {"status": STATUS_READY, "named": True, "superseded_failure": reason}
            # the scout's facts, proposal and skin stay on the failed record: the stage keeps
            # the part for the user to name by hand, and a retry has something to name
            write_status(session_id, STATUS_FAILED, **_failed_record(reason, RETRY_NAMING, current))
        except Exception:  # noqa: BLE001 - the store itself may be what broke
            logger.warning("geometry naming: could not record the failure for %s", session_id)
        return {"status": STATUS_FAILED, "named": False, "reason": reason, "step": RETRY_NAMING,
                "retry": RETRY_NAMING}


def _name(*, session_id: str, owner_id: str, purpose_text: str, interpretation: dict | None) -> dict:
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.object_storage import get_object_store

    started = time.time()
    stored = _wait_for_scout(session_id)
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
            facts["notes"] = [n for n in facts.get("notes", []) if "assume millimetres" not in n]
            # THE SKIN FOLLOWS THE FACTS: the stage places the pins at the corrected centroids on
            # the stored skin, so the skin is re-read in the same unit. The pictures show shape
            # only and stay as they are.
            _rescale_skin(session_id, k)

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
    proposal = _proposal(facts, vision)
    # what the scout stored, minus the fields write_status writes itself: the status, the clock,
    # and the session id - which it also takes as its first argument
    result = {k: v for k, v in stored.items() if k not in _STAMPED_KEYS + _FAILURE_KEYS}
    result.update(named=True, facts=facts, vision=vision, proposal=proposal,
                  purpose_text=purpose_text[:2000], naming_seconds=round(time.time() - started, 1))
    # the write is part of the step: a result the store refuses is stored as a failure over the
    # scout's record, so the stage stops waiting and keeps the part
    landed = store_status(session_id, STATUS_READY, RETRY_NAMING, **result)
    logger.info("geometry naming %s - session=%s in %.1fs (model %s)", landed["status"], session_id,
                result["naming_seconds"], "answered" if "error" not in vision else "unavailable")
    return landed


def _wait_for_scout(session_id: str) -> dict | None:
    """The stored scout, once it is no longer pending. The user's first answer can land while the
    worker is still drawing; the naming waits rather than answering from nothing."""
    deadline = time.time() + NAMING_WAIT_S
    while True:
        stored = read_check(session_id)
        if stored is not None and stored.get("status") != STATUS_PENDING:
            return stored
        if time.time() >= deadline:
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
    if purpose_text:
        lines.append(f"The user said: {purpose_text.strip()[:600]}")
    if facts["openings"]:
        lines.append("Stickers:")
        for o in facts["openings"]:
            size = f"{o['diameter_mm']:.0f} mm across" if o["shape"] == "circle" \
                else f"{o.get('width_mm', 0):.0f} x {o.get('height_mm', 0):.0f} mm"
            lines.append(f"  {o['id']}: {o['shape']} opening, {size}, at ({o['centroid_mm'][0]:.0f}, "
                         f"{o['centroid_mm'][1]:.0f}, {o['centroid_mm'][2]:.0f}) mm; guessed {o['role']}")
    else:
        lines.append("The measuring step found no openings (it reads this as a body in a flow).")
    if facts.get("flow") == "external" and facts.get("flow_axis_guess"):
        lines.append(f"Measuring step's guess for the flow direction: along {facts['flow_axis_guess']} "
                     "(its longest horizontal side); the sign is yours to decide from how the part faces.")
    lines.append("Axis marker in every picture: red +X, green +Y, blue +Z.")
    lines.append("Pictures, in order: " + "; ".join(
        f"{s.name} (stickers facing the camera: {', '.join(map(str, s.facing)) or 'none'})"
        + (f", camera looks along {camera_words(s.direction)}" if getattr(s, "direction", None) else "")
        for s in shots))
    return "\n".join(lines)


_AXES = ("+x", "-x", "+y", "-y", "+z", "-z")


def _flow_from_nose(answer: dict, shots=()) -> dict:
    """The fluid travels away from the nose: a nose at the -X end means flow along +x. The model
    finds a nose far more reliably than it reads an axis marker, so the surest answer is "in the top
    picture the nose is on the left": the camera of that picture says what "left" is in the part's
    axes. That settles nose_axis and flow_axis; the model's own nose_axis is the fallback."""
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
    if said in _AXES:
        answer["flow_from"] = "the user's words"       # the user's word decides; the nose only fills a silence
    elif nose in _AXES:
        answer["flow_axis"] = ("-" if nose[0] == "+" else "+") + nose[1]
        answer["flow_from"] = "the nose"
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

    panel = str(answer.get("panel") or "").strip().upper()
    if panel not in UP_PANELS or UP_PANELS.index(panel) >= len(order):
        return {}
    axis = str(order[UP_PANELS.index(panel)])
    if axis not in UP_AXES:
        return {}
    try:
        conf = max(0.0, min(1.0, float(answer.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
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
                return _upright_answer(json.loads(call.arguments), order)
        return {"error": "the model answered without picking a panel"}

    try:
        answer = asyncio.run(asyncio.wait_for(_ask(), timeout=GEOMETRY_CHECK_VISION_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - provider weather; the shape still says what it can
        logger.warning("geometry check: which way is up unavailable from the pictures (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
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
    code, vision = code or {}, vision or {}
    c_axis = code.get("axis") if code.get("axis") in UP_AXES else None
    v_axis = vision.get("up_axis") if vision.get("up_axis") in UP_AXES else None
    try:
        c_conf = float(code.get("confidence") or 0.0)
        v_conf = float(vision.get("up_confidence") or 0.0)
    except (TypeError, ValueError):
        c_conf = v_conf = 0.0
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
                return _flow_from_nose(json.loads(call.arguments), shots)
        return {"error": "the model answered without naming the stickers"}

    try:
        answer = asyncio.run(asyncio.wait_for(_ask(), timeout=GEOMETRY_CHECK_VISION_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - provider weather; the check still ships
        logger.warning("geometry check: vision naming unavailable (%s: %s)", type(exc).__name__, str(exc)[:200])
        return {"error": f"{type(exc).__name__}"}
    return answer if isinstance(answer, dict) else {"error": "malformed answer"}


def _proposal(facts: dict, vision: dict | None) -> dict:
    """What the user is shown: the code's positions and sizes, the model's names and roles where
    it gave them for a sticker that exists, and the kind and flow from whichever is surer. With
    no vision answer yet (the scout alone) the names are the code's and nothing says otherwise."""
    proposal = {
        "part": (vision or {}).get("part") or "",
        "input_kind": facts["input_kind"], "flow": facts["flow"],
        "openings": [dict(o) for o in facts["openings"]],
        "seed_point_mm": facts.get("seed_point_mm"),
        "size_mm": facts["size_mm"],
        "notes": list(facts.get("notes") or []),
        "read_as": facts.get("read_as", "cad"),
        "faces": list(facts.get("faces") or []),      # every flat face measured: what "add an opening" snaps to
        "holes": list(facts.get("holes") or []),      # every hole in the skin: what a click into one snaps to
        "named": vision is not None,
        "vision_available": bool(vision) and "error" not in (vision or {}),
    }
    if vision is None:
        return _upright(proposal, facts, None, None)
    if "error" in vision:
        proposal["notes"].append("the picture-naming step was unavailable; names are the measuring step's own")
        return _upright(proposal, facts, None, None)
    by_id = {o["id"]: o for o in proposal["openings"]}
    for named in vision.get("openings") or []:
        o = by_id.get(int(named.get("id", -1)))
        if o is None:
            continue
        if named.get("name"):
            o["name"] = str(named["name"]).strip()[:40]
        if named.get("role") in ("inlet", "outlet", "not_an_opening"):
            o["role"] = named["role"]
        o["confidence"] = round(float(named.get("confidence", o.get("confidence", 0.5))), 2)
    try:
        # how long a real one is, by the model's knowledge of what it is: what the stage checks
        # the unit against (unit_suggestion), never a size applied to anything
        real = float(vision.get("real_length_m") or 0.0)
        if real > 0:
            proposal["real_length_m"] = round(real, 6)
    except (TypeError, ValueError):
        pass
    model_conf = float(vision.get("confidence", 0.0) or 0.0)
    if model_conf >= float(facts.get("confidence", {}).get("input_kind", 0.0)):
        if vision.get("input_kind") in ("body-surface", "fluid-domain", "solid-body"):
            proposal["input_kind"] = vision["input_kind"]
        if vision.get("flow") in ("internal", "external"):
            proposal["flow"] = vision["flow"]
    if vision.get("notes"):
        proposal["notes"].append(str(vision["notes"])[:300])
    return _upright(proposal, facts, vision, vision.get("flow_axis"))


def _upright(proposal: dict, facts: dict, vision: dict | None, flow_axis) -> dict:
    """The proposal with which way is up decided (decide_up), and the external-flow facts read
    across it: the flow's guess along the longest horizontal side, and the ground only where the
    measuring step looked for it. A part drawn another way up says so in the notes."""
    from meshpipeline.contracts.geometry_fields import DEFAULT_UP, external_defaults

    up = decide_up(proposal["flow"], facts.get("up_evidence"), vision)
    proposal.update(up)
    proposal.update(external_defaults(facts, flow_axis, up["up_axis"]))
    if up["up_axis"] != DEFAULT_UP:
        proposal["notes"].append(f"Up is {up['up_axis'].upper()}: {up['up_axis_from']}.")
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
    from meshpipeline.contracts.unit_plausibility import expected_from_estimate, expected_from_words, suggest

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
    s = suggest(longest_file, in_effect, declared=interpretation is not None, expected=expected)
    return s.as_dict() if s is not None else None
