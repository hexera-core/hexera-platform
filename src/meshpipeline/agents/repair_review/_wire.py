# Responsibility: The pieces the proposer and the interpreter share - the catalogue, one bounded call, JSON parsing.
# Boundaries: it never executes anything; the review session judges every proposal under its own caps.
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

CATALOGUE: tuple[str, ...] = (
    "fix_small_edges", "fix_face_boundary", "sew_faces", "fill_planar_hole", "fix_shape_global")

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
