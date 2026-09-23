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
#: How long the naming step waits for a scout that is still running before giving up - the
#: scout's own hard limit, so a slow queue is waited out rather than abandoned.
NAMING_WAIT_S = 900.0


def check_object_key(session_id: str, name: str) -> str:
    """Where a session's geometry check lives in the object store: one folder per session,
    `scout.json` beside the pictures and the skin, `naming.json` once the user's words were
    handed to the model, `confirmed.json` once the user has answered."""
    return f"sessions/{session_id}/geometry_check/{name}"


def _store_json(object_key: str, payload: dict) -> None:
    from meshpipeline.contracts.object_storage import get_object_store

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
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
    """The part's skin in the shape the mesh viewer already reads (render/viewer_pack's STL
    form), so the console draws the part with the renderer it draws a delivered mesh with. It is
    marked as the input skin, never as a mesh."""
    from meshpipeline.cad.stl_io import read_stl_triangles
    from meshpipeline.render.viewer_pack import stl_response

    resp = stl_response({"skin": read_stl_triangles(Path(skin_stl))}, {}, "m")
    if resp is None:
        raise RuntimeError("the part's skin has no triangles to draw")
    resp["is_mesh"] = False
    resp["cell_count"] = 0
    return resp


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
        result = {"status": STATUS_UNSUPPORTED, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry scout failed - session=%s", session_id)
        result = {"status": STATUS_FAILED, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    result["seconds"] = round(time.time() - started, 1)
    status = result.pop("status")
    write_status(session_id, status, **result)
    logger.info("geometry scout %s - session=%s in %.1fs", result.get("reason") or status,
                session_id, result["seconds"])
    return {"status": status, **result}


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
        shots = render_snapshots(skin, facts["openings"], work / "pictures")

        store = get_object_store()
        snapshots = []
        for s in shots:
            key = check_object_key(session_id, f"{s.name}.png")
            store.upload_file(local_path=s.path, object_key=key)
            snapshots.append({"name": s.name, "object_key": key, "facing": list(s.facing)})
    return {"status": STATUS_SCOUTED, "named": False, "facts": facts, "proposal": _proposal(facts, None),
            "snapshots": snapshots, "skin_key": skin_key, "source": ref.to_payload()}


def _scout_exact(local_path: Path, work: Path, interp_ref, ref) -> tuple[dict, Path]:
    """A STEP or IGES file: OpenCASCADE reads real faces, so the openings come out exact."""
    from meshpipeline.cad.scout import scout_cad, write_view_stl

    prepared, unit_note = _prepared_coordinates(local_path, interp_ref, ref)
    facts = scout_cad(local_path, prepared=prepared).as_dict()
    facts["read_as"] = "cad"
    facts["unit_assumed"] = bool(unit_note)
    interp = getattr(prepared, "interpretation", None)
    facts["scale_to_m"] = float(interp.scale_to_metres) if interp is not None else 0.001
    if unit_note:
        facts["notes"].append(unit_note)
    skin = write_view_stl(local_path, work / "skin.stl", prepared=prepared)
    return facts, skin


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


# ----------------------------------------------------------------------------- the naming ----
@dataclass(frozen=True)
class _Shot:
    name: str
    path: Path
    facing: list


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
            write_status(session_id, STATUS_FAILED, reason=reason, named=False)
        except Exception:  # noqa: BLE001 - the store itself may be what broke
            logger.warning("geometry naming: could not record the failure for %s", session_id)
        return {"status": STATUS_FAILED, "named": False, "reason": reason}


def _name(*, session_id: str, owner_id: str, purpose_text: str, interpretation: dict | None) -> dict:
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.object_storage import get_object_store

    started = time.time()
    stored = _wait_for_scout(session_id)
    if stored is None or stored.get("status") not in (STATUS_SCOUTED, STATUS_READY):
        status = (stored or {}).get("status") or "missing"
        if status in (STATUS_PENDING, "missing"):
            # the scout never finished in time: withdraw the request, so the next chat turn may
            # ask again once it has, instead of a conversation that waits forever
            clear_naming_request(session_id)
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
            shots.append(_Shot(name=s["name"], path=dest, facing=list(s.get("facing") or [])))
        vision = _name_with_vision(facts, shots, purpose_text=purpose_text, session_id=session_id,
                                   owner_id=owner_id)
    proposal = _proposal(facts, vision)
    result = {k: v for k, v in stored.items() if k not in ("status", "written_at", "seconds")}
    result.update(named=True, facts=facts, vision=vision, proposal=proposal,
                  purpose_text=purpose_text[:2000], naming_seconds=round(time.time() - started, 1))
    write_status(session_id, STATUS_READY, **result)
    logger.info("geometry naming ready - session=%s in %.1fs (model %s)", session_id,
                result["naming_seconds"], "answered" if "error" not in vision else "unavailable")
    return {"status": STATUS_READY, **result}


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


def _rescaled(value, k: float):
    """Every length in a facts dict re-read by factor k: `*_mm` and `*_m` scale by k, areas by k
    squared, nested lists and dicts alike. Keys that carry no unit are left alone."""
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            if key.endswith("_mm2") or key.endswith("_m2"):
                out[key] = _scale_numbers(v, k * k)
            elif key.endswith("_mm") or key.endswith("_m") or key in ("bbox_min_m", "bbox_max_m"):
                out[key] = _scale_numbers(v, k)
            else:
                out[key] = _rescaled(v, k)
        return out
    if isinstance(value, list):
        return [_rescaled(v, k) for v in value]
    return value


def _rescale_skin(session_id: str, k: float) -> None:
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

    key = check_object_key(session_id, "skin.json")
    try:
        skin = json.loads(get_object_store().get_bytes(object_key=key))
    except ObjectNotFound:
        return
    import numpy as np

    for p in skin.get("patches") or []:
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
                "flow_axis": {"type": "string", "enum": ["+x", "-x", "+y", "-y", "+z", "-z", "unknown"],
                              "description": "EXTERNAL flow only: the direction the fluid travels past the part, read from "
                                             "how it faces (a car's nose, a wing's leading edge, the side a building would "
                                             "meet the wind) and from the user's words. unknown when nothing settles it."},
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
    "(a bolt hole, a mounting face). For a body the fluid flows AROUND, say which way the fluid "
    "travels from how the part faces. The user's own description, when given, decides the flow "
    "direction and what the part is. If the pictures do not settle something, say so with a low "
    "confidence rather than guessing confidently. Answer by calling name_geometry."
)


def _image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _facts_text(facts: dict, shots, purpose_text: str) -> str:
    lines = [f"Part size: {facts['size_mm'][0]:.0f} x {facts['size_mm'][1]:.0f} x {facts['size_mm'][2]:.0f} mm.",
             f"Measuring step's guess: {facts['input_kind']} ({facts['body_kind']}), flow {facts['flow']}."]
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
    lines.append("Pictures, in order: " + "; ".join(
        f"{s.name} (stickers facing the camera: {', '.join(map(str, s.facing)) or 'none'})" for s in shots))
    return "\n".join(lines)


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
                return json.loads(call.arguments)
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
    from meshpipeline.contracts.geometry_fields import external_defaults

    proposal = {
        "part": (vision or {}).get("part") or "",
        "input_kind": facts["input_kind"], "flow": facts["flow"],
        "openings": [dict(o) for o in facts["openings"]],
        "seed_point_mm": facts.get("seed_point_mm"),
        "size_mm": facts["size_mm"],
        "notes": list(facts.get("notes") or []),
        "read_as": facts.get("read_as", "cad"),
        "named": vision is not None,
        "vision_available": bool(vision) and "error" not in (vision or {}),
    }
    if vision is None:
        proposal.update(external_defaults(facts, None))
        return proposal
    if "error" in vision:
        proposal["notes"].append("the picture-naming step was unavailable; names are the measuring step's own")
        proposal.update(external_defaults(facts, None))
        return proposal
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
    model_conf = float(vision.get("confidence", 0.0) or 0.0)
    if model_conf >= float(facts.get("confidence", {}).get("input_kind", 0.0)):
        if vision.get("input_kind") in ("body-surface", "fluid-domain", "solid-body"):
            proposal["input_kind"] = vision["input_kind"]
        if vision.get("flow") in ("internal", "external"):
            proposal["flow"] = vision["flow"]
    proposal.update(external_defaults(facts, vision.get("flow_axis")))
    if vision.get("notes"):
        proposal["notes"].append(str(vision["notes"])[:300])
    return proposal


# kept for callers and tests that knew the check by its first name
_merge = _proposal
