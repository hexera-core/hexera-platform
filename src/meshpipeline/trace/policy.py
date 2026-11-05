# Responsibility: Be the one gate between what the system knows and what the public page shows.
# Owns: the mode, and the projection of each payload kind under it.
# Boundaries: publication policy only. It is deliberately independent of data collection, which is retention.
from __future__ import annotations

from typing import Any, Final

from meshpipeline.trace.labels import public_label
from meshpipeline.trace.sanitizer import (
    sanitize_payload,
    scrub_text,
)

SAFE: Final = "safe"
RAW: Final = "raw"
MODES: Final[frozenset[str]] = frozenset({SAFE, RAW})

# Reasoning content is bounded harder than a generic string: it is prose, and a
# page is not a log file.
MAX_REASONING: Final = 20_000
# An inspection image the page will actually try to decode.
MAX_IMAGE_BYTES: Final = 4_000_000

# Event types this policy owns. Anything else passes through untouched - the
# Tier-1 events already carry only what the application chose to say.
TRACE_TYPES: Final[frozenset[str]] = frozenset({
    "reasoning", "tool_call", "tool_result", "screenshot",
})


def current_mode() -> str:
    # The deployment's own answer, never a caller's. If settings cannot be read at all the
    # answer is the closed mode - a projection that fails open is not a projection.
    try:
        from meshpipeline.settings import policy as _p
        return str(_p.MODES.trace_disclosure)
    except Exception:
        return SAFE


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


# reasoning


def project_reasoning(data: dict[str, Any], mode: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id":       scrub_text(str(data.get("id") or ""), limit=128),
        "agent":    scrub_text(str(data.get("agent") or ""), limit=64),
        "phase":    str(data.get("phase") or "started"),
        "status":   str(data.get("status") or "active"),
        # measured only. `duration_ms` is absent unless a clock produced it.
        "duration_ms": _int_or_none(data.get("duration_ms")),
        # REASONING tokens only. Output/total tokens are a different quantity and
        # are never relabelled into this field by the callers or by us.
        "token_count": _int_or_none(data.get("token_count")),
        "content":  None,
    }
    if mode == RAW:
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            # raw mode shows what the PROVIDER returned - scrubbed, bounded, and
            # with model identity struck out. It never shows what it did not.
            out["content"] = scrub_text(content, limit=MAX_REASONING)
    return out


# tools


def project_tool_call(data: dict[str, Any], mode: str) -> dict[str, Any]:
    name = str(data.get("tool_name") or "")
    out: dict[str, Any] = {
        "id":           scrub_text(str(data.get("id") or ""), limit=128),
        "agent":        scrub_text(str(data.get("agent") or ""), limit=64),
        "public_label": public_label(name),
        "status":       str(data.get("status") or "started"),
        "tool_name":    None,
        "arguments":    None,
    }
    if mode == RAW:
        out["tool_name"] = scrub_text(name, limit=120) or None
        out["arguments"] = sanitize_payload(data.get("arguments"))
    return out


def project_tool_result(data: dict[str, Any], mode: str) -> dict[str, Any]:
    name = str(data.get("tool_name") or "")
    out: dict[str, Any] = {
        "id":           scrub_text(str(data.get("id") or ""), limit=128),
        "tool_call_id": scrub_text(str(data.get("tool_call_id") or ""), limit=128),
        "agent":        scrub_text(str(data.get("agent") or ""), limit=64),
        "public_label": public_label(name),
        "status":       str(data.get("status") or "success"),
        "duration_ms":  _int_or_none(data.get("duration_ms")),
        "tool_name":    None,
        "result":       None,
    }
    if mode == RAW:
        out["tool_name"] = scrub_text(name, limit=120) or None
        out["result"] = sanitize_payload(data.get("result"))
    return out


# images


def project_screenshot(data: dict[str, Any], mode: str) -> dict[str, Any] | None:
    if mode != RAW:
        return None
    img = data.get("image")
    if not isinstance(img, str) or not img.strip():
        return None
    if len(img) > MAX_IMAGE_BYTES:
        # an oversized render is dropped, not truncated: half a base64 image is a
        # broken <img>, and one bad frame must not cost the events after it.
        return None
    out: dict[str, Any] = {"image": img}
    meta = data.get("meta")
    if isinstance(meta, dict):
        safe_meta = sanitize_payload(meta)
        if safe_meta:
            out["meta"] = safe_meta
    return out


# gate


def project(wire: dict[str, Any], mode: str | None = None) -> dict[str, Any] | None:
    if not isinstance(wire, dict):
        return None
    kind = wire.get("type")
    if kind not in TRACE_TYPES:
        return wire
    m = mode or current_mode()

    if kind == "screenshot":
        shot = project_screenshot(wire, m)
        if shot is None:
            return None
        return {**{k: v for k, v in wire.items() if k not in ("image", "meta")}, **shot}

    base = {k: v for k, v in wire.items()
            if k in ("type", "stage", "ts", "sequence", "seq", "event_id")}
    if kind == "reasoning":
        return {**base, **project_reasoning(wire, m)}
    if kind == "tool_call":
        return {**base, **project_tool_call(wire, m)}
    if kind == "tool_result":
        return {**base, **project_tool_result(wire, m)}
    return wire


def reasoning_id(job_id: str, agent: str, attempt: int, round_no: int) -> str:
    parts = [str(job_id or "job")[:64], str(agent or "agent")[:32],
             str(int(attempt or 0)), str(int(round_no or 0))]
    return "r:" + ":".join(parts)
