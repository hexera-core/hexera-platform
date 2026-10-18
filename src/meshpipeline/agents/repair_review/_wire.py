# Responsibility: The pieces the proposer and the interpreter share - the catalogue, one bounded call, JSON parsing.
# Boundaries: it never executes anything; the review session judges every proposal under its own caps.
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

CATALOGUE: tuple[str, ...] = (
    "fix_small_edges", "fix_face_boundary", "sew_faces", "fill_planar_hole", "fix_shape_global")

#: The catalogue names that fill a hole, as strings so this layer does not import `cad.repair`.
#: Pinned to `operations.FILL_OPS` by tests/unit/agents/test_catalogue_pinned_to_operations.py.
FILL_CATALOGUE: tuple[str, ...] = ("fill_planar_hole",)

CATALOGUE_LIMITS = (
    "the repair catalogue can only merge small edges, fix a face boundary, sew faces, fill a "
    "planar hole, or run a whole-shape fix; it cannot move, redraw or delete geometry, change "
    "dimensions, or edit the part by hand")

CATALOGUE_TEXT = (
    "- fix_small_edges: merge edges too short to keep\n"
    "- fix_face_boundary: repair a face whose boundary is broken or open\n"
    "- sew_faces: stitch faces that should share an edge\n"
    "- fill_planar_hole: cap a flat hole\n"
    "- fix_shape_global: run the whole-shape healing pass")


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
