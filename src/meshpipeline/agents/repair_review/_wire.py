# Responsibility: The pieces the proposer and the interpreter share - the catalogue, one bounded call, JSON parsing.
# Boundaries: it never executes anything; the review session judges every proposal under its own caps.
from __future__ import annotations

import asyncio
import json
import logging

from meshpipeline.cad.repair.catalogue_words import catalogue_text

logger = logging.getLogger(__name__)

#: The operation that answers a periodic-surface mesh failure, present ONLY when
#: CAD_REPAIR_SPLIT_CLOSED_FACES admits it to `operations.OPERATIONS` - the same flag, read the same
#: way, so the model side can never offer (or refuse) what the catalogue does not hold.
SPLIT_CLOSED = "split_closed_faces"
#: The operation that answers an edge-through-face mesh failure between touching bodies, present
#: ONLY when CAD_REPAIR_FRAGMENT_BODIES admits it - the same rule as SPLIT_CLOSED.
FRAGMENT = "fragment_bodies"
#: The operation that re-trims a face gmsh reports overlapping facets on, present ONLY when
#: CAD_REPAIR_REBUILD_FACE admits it - the same rule as SPLIT_CLOSED.
REBUILD = "rebuild_face"
#: The operation that closes a face's open wire by moving vertices, present ONLY when
#: CAD_REPAIR_CLOSE_WIRE_GAPS admits it - the same rule as SPLIT_CLOSED.
CLOSE_GAPS = "close_wire_gaps"


def catalogue(*, split_closed_faces: bool | None = None,
              fragment_bodies: bool | None = None,
              rebuild_face: bool | None = None,
              close_wire_gaps: bool | None = None) -> tuple[str, ...]:
    """The catalogue names for the given consent (the deployment's flags when not told). Pinned to
    `operations.catalogue` in every flag state by test_catalogue_pinned_to_operations.py."""
    import meshpipeline.settings.cad_repair as rcfg

    if split_closed_faces is None:
        split_closed_faces = bool(rcfg.CAD_REPAIR_SPLIT_CLOSED_FACES)
    if fragment_bodies is None:
        fragment_bodies = bool(rcfg.CAD_REPAIR_FRAGMENT_BODIES)
    if rebuild_face is None:
        rebuild_face = bool(rcfg.CAD_REPAIR_REBUILD_FACE)
    if close_wire_gaps is None:
        close_wire_gaps = bool(rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS)
    return ("fix_small_edges", "fix_small_faces", "fix_face_boundary", "sew_faces",
            "fill_planar_hole", "fill_nonplanar_hole", "heal_self_intersections",
            "fix_shape_global", *((SPLIT_CLOSED,) if split_closed_faces else ()),
            *((FRAGMENT,) if fragment_bodies else ()),
            *((REBUILD,) if rebuild_face else ()),
            *((CLOSE_GAPS,) if close_wire_gaps else ()))


CATALOGUE: tuple[str, ...] = catalogue()

#: The catalogue names that fill a hole, as strings so this layer does not import `cad.repair`.
#: Pinned to `operations.FILL_OPS` by tests/unit/agents/test_catalogue_pinned_to_operations.py.
FILL_CATALOGUE: tuple[str, ...] = ("fill_planar_hole", "fill_nonplanar_hole")

def catalogue_limits(*, rebuild_face: bool | None = None,
                     close_wire_gaps: bool | None = None) -> str:
    """What the catalogue can NOT do, in words, for the given consent (CAD_REPAIR_REBUILD_FACE
    and CAD_REPAIR_CLOSE_WIRE_GAPS read when not told). Flags off is the prior text, byte for byte;
    on, it also says a face the mesher reports overlapping facets on may be re-trimmed, and an
    open wire's loose vertices may be moved together by at most the run's tolerance - the
    admitted ops that redraw a face or move a vertex, so the limits must not claim otherwise."""
    import meshpipeline.settings.cad_repair as rcfg

    if rebuild_face is None:
        rebuild_face = bool(rcfg.CAD_REPAIR_REBUILD_FACE)
    if close_wire_gaps is None:
        close_wire_gaps = bool(rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS)
    rebuild = (", re-trim one face from its own boundary on the same surface when the mesher "
               "reports overlapping facets on it" if rebuild_face else "")
    if close_wire_gaps:
        rebuild += (", close a face's open wire by moving its loose vertices together by at most "
                    "the run's tolerance")
    return (
        "the repair catalogue can only merge small edges, sweep away a few measured sliver faces, "
        "fix a face boundary, sew faces, fill a planar hole, cap a small curved hole with a "
        "constrained patch, re-trim only the faces the kernel says cross each other"
        f"{rebuild}, or run a whole-shape fix; beyond those slivers it cannot move, redraw or "
        "delete geometry, change dimensions, or edit the part by hand")


CATALOGUE_LIMITS = catalogue_limits()

#: `catalogue_text` LIVES IN `cad/repair/catalogue_words.py` (the model author in `cad/repair` needs it
#: and cad may not import agents); re-exported here so this module's callers are untouched.
CATALOGUE_TEXT = catalogue_text()


def configured() -> bool:
    from meshpipeline.contracts import model_inference
    return model_inference._router is not None


async def ask(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """One bounded model call; the reply's JSON object, or None for any failure at all."""
    from meshpipeline.contracts.model_inference import call_repair_review_model
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        result = await asyncio.wait_for(
            call_repair_review_model(messages, job_id=job_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - total: a failed call is a decline, never a raise
        logger.warning("repair review model call failed: %s", type(exc).__name__)
        return None
    if not getattr(result, "ok", True):
        return None
    return parse_object(getattr(result, "assistant_text", "") or "")


def parse_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


MEASURE_KEYS_MAX = 12
TEXT_MAX = 300


def clip_text(value: object, limit: int = TEXT_MAX) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def clip_measurements(measurements: dict | None) -> dict:
    """The measurements, bounded: at most MEASURE_KEYS_MAX keys, long values clipped."""
    out: dict = {}
    for key, value in list((measurements or {}).items())[:MEASURE_KEYS_MAX]:
        out[str(key)[:60]] = value if isinstance(value, (int, float, bool)) else clip_text(value, 80)
    return out


def tried_of(members: list[dict]) -> list[str]:
    return sorted({str(t) for m in members
                   for t in ((m.get("measurements") or {}).get("tried") or [])})[:10]


def story_of(items: list[dict], stopped_because: str = "") -> dict:
    """A short story of the part: how many defects of each code are waiting, and why the run stopped."""
    counts: dict[str, int] = {}
    for item in items:
        code = item.get("defect_code")
        if code:
            counts[str(code)] = counts.get(str(code), 0) + 1
    story: dict = {"defects_by_code": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:10])}
    if stopped_because:
        story["run_stopped_because"] = clip_text(stopped_because, 60)
    return story


def trim_rationale(text: object, limit: int = 200) -> str:
    out = " ".join(str(text or "").split())
    if len(out) <= limit:
        return out
    cut = out[:limit]
    head = cut.rsplit(" ", 1)[0] if " " in cut and out[limit] != " " else cut
    return head.rstrip(" ,;:-")


DEFECTS_MAX = 40


def clip_defects(entities: list[dict] | tuple[dict, ...] | None, limit: int = DEFECTS_MAX) -> list[dict]:
    """The scan's located defects, bounded: at most `limit` entries, each with clipped text."""
    out: list[dict] = []
    for e in list(entities or ())[:limit]:
        if not isinstance(e, dict):
            continue
        out.append({"fingerprint": str(e.get("fingerprint") or ""), "code": str(e.get("code") or ""),
                    "entity": f"{e.get('entity_type')}#{e.get('entity_index')}",
                    "severity": e.get("severity"), "region": e.get("region"),
                    "message": clip_text(e.get("message")),
                    "measurements": clip_measurements(e.get("measurements"))})
    return out
