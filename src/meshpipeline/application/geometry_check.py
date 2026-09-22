# Responsibility: Run the geometry check for an uploaded part - fetch it, propose its openings and
# kind, picture it with numbered stickers, let a vision model say what the numbers are, and store
# the result where the API serves it and the user confirms it.
# Boundaries: orchestration. The geometry is cad/scout, the pictures render/scout_snapshots, the
# model call goes through the router. This module sequences them and never decides for the user:
# whatever it stores is a proposal until the confirm endpoint records what the user said.
from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATUS_PENDING, STATUS_READY, STATUS_FAILED, STATUS_UNSUPPORTED = "pending", "ready", "failed", "unsupported"
_CAD_SUFFIXES = (".step", ".stp", ".igs", ".iges")


def check_object_key(session_id: str, name: str) -> str:
    """Where a session's geometry check lives in the object store: one folder per session,
    `scout.json` beside the pictures, `confirmed.json` once the user has answered."""
    return f"sessions/{session_id}/geometry_check/{name}"


def write_status(session_id: str, status: str, **fields) -> None:
    """A small JSON marker so the API can tell 'not started' from 'still running'."""
    from meshpipeline.contracts.object_storage import get_object_store

    payload = {"status": status, "session_id": session_id, "written_at": time.time(), **fields}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        tmp = Path(fh.name)
    try:
        get_object_store().upload_file(local_path=tmp, object_key=check_object_key(session_id, "scout.json"))
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- the check ----
def run_geometry_check(*, session_id: str, owner_id: str, source: dict,
                       interpretation: dict | None = None, purpose_text: str = "") -> dict:
    """The worker task body. Returns the stored result; every failure is stored too, as a status
    the console can show, because a check that silently never arrives is worse than one that
    says it could not read the part."""
    started = time.time()
    try:
        result = _check(session_id=session_id, owner_id=owner_id, source=source,
                        interpretation=interpretation, purpose_text=purpose_text)
    except _Unsupported as exc:
        result = {"status": STATUS_UNSUPPORTED, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the user is told, in one sentence, and the intake carries on
        logger.exception("geometry check failed - session=%s", session_id)
        result = {"status": STATUS_FAILED, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    result["seconds"] = round(time.time() - started, 1)
    write_status(session_id, result.pop("status"), **result)
    logger.info("geometry check %s - session=%s in %.1fs", result.get("reason") or "ready",
                session_id, result["seconds"])
    return result


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


def _check(*, session_id: str, owner_id: str, source: dict, interpretation: dict | None,
           purpose_text: str) -> dict:
    from meshpipeline.cad.scout import scout_cad, write_view_stl
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.render.scout_snapshots import render_snapshots

    ref = GeometrySourceRef.from_payload(source)
    suffix = (ref.suffix_hint or "").lower()
    if suffix not in _CAD_SUFFIXES:
        raise _Unsupported("the geometry check reads CAD files (STEP or IGES); this upload is a "
                           f"{suffix or 'surface'} file, so the intake will ask about its openings")
    interp_ref = GeometryInterpretationRef.from_payload(interpretation) if interpretation else None

    work = Path(tempfile.mkdtemp(prefix=f"geometry_check_{session_id[:8]}_"))
    local_path = _fetch(ref, work)
    prepared, unit_note = _prepared_coordinates(local_path, interp_ref, ref)

    facts = scout_cad(local_path, prepared=prepared).as_dict()
    if unit_note:
        facts["notes"].append(unit_note)

    pictures = work / "pictures"
    skin = write_view_stl(local_path, work / "skin.stl", prepared=prepared)
    shots = render_snapshots(skin, facts["openings"], pictures)

    vision = _name_with_vision(facts, shots, purpose_text=purpose_text, session_id=session_id,
                               owner_id=owner_id)
    proposal = _merge(facts, vision)

    store = get_object_store()
    snapshots = []
    for s in shots:
        key = check_object_key(session_id, f"{s.name}.png")
        store.upload_file(local_path=s.path, object_key=key)
        snapshots.append({"name": s.name, "object_key": key, "facing": list(s.facing)})
    return {"status": STATUS_READY, "facts": facts, "vision": vision, "proposal": proposal,
            "snapshots": snapshots, "source": ref.to_payload()}


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
                "part": {"type": "string", "description": "What the part is, in a few words (a pipe elbow, a manifold, a car body)."},
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
                        "role": {"type": "string", "enum": ["inlet", "outlet"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    }, "required": ["id", "name", "role", "confidence"]},
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "How sure you are about the part and the flow direction overall."},
                "notes": {"type": "string", "description": "Anything the user should check, in one or two plain sentences."},
            },
            "required": ["part", "flow", "input_kind", "openings", "confidence"],
        },
    },
}

_SYSTEM = (
    "You are looking at pictures of one CAD part. Yellow stickers with numbers mark the openings a "
    "measuring step found; the numbers are the only names you may use. Say what the part is, "
    "whether fluid flows through it or around it, and for every sticker which opening it is and "
    "whether fluid enters (inlet) or leaves (outlet) there. Use the user's own description when "
    "one is given - it decides the flow direction. If the pictures do not settle something, say "
    "so with a low confidence rather than guessing confidently. Answer by calling name_geometry."
)


def _image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _facts_text(facts: dict, shots, purpose_text: str) -> str:
    lines = [f"Part size: {facts['size_mm'][0]:.0f} x {facts['size_mm'][1]:.0f} x {facts['size_mm'][2]:.0f} mm.",
             f"Measuring step's guess: {facts['input_kind']} ({facts['body_kind']}), flow {facts['flow']}."]
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


def _merge(facts: dict, vision: dict) -> dict:
    """What the user is shown: the code's positions and sizes, the model's names and roles where
    it gave them for a sticker that exists, and the kind and flow from whichever is surer."""
    proposal = {
        "part": vision.get("part") or "",
        "input_kind": facts["input_kind"], "flow": facts["flow"],
        "openings": [dict(o) for o in facts["openings"]],
        "seed_point_mm": facts.get("seed_point_mm"),
        "size_mm": facts["size_mm"],
        "notes": list(facts.get("notes") or []),
        "vision_available": "error" not in vision,
    }
    if "error" in vision:
        proposal["notes"].append("the picture-naming step was unavailable; names are the measuring step's own")
        return proposal
    by_id = {o["id"]: o for o in proposal["openings"]}
    for named in vision.get("openings") or []:
        o = by_id.get(int(named.get("id", -1)))
        if o is None:
            continue
        if named.get("name"):
            o["name"] = str(named["name"]).strip()[:40]
        if named.get("role") in ("inlet", "outlet"):
            o["role"] = named["role"]
        o["confidence"] = round(float(named.get("confidence", o.get("confidence", 0.5))), 2)
    model_conf = float(vision.get("confidence", 0.0) or 0.0)
    if model_conf >= float(facts.get("confidence", {}).get("input_kind", 0.0)):
        if vision.get("input_kind") in ("body-surface", "fluid-domain", "solid-body"):
            proposal["input_kind"] = vision["input_kind"]
        if vision.get("flow") in ("internal", "external"):
            proposal["flow"] = vision["flow"]
    if vision.get("notes"):
        proposal["notes"].append(str(vision["notes"])[:300])
    return proposal
