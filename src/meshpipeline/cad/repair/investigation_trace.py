# Responsibility: READ ONE INVESTIGATION'S RECORD BACK as two things a person can use: a compact
#                 per-region OUTCOME ROW ("how it ended, and why no package"), and a redacted,
#                 size-capped PER-TURN TRACE (each tool call, its result status, each edit's
#                 kept/rolled-back verdict and reason, the conclusion, the budget).
# Owns: the outcome row's shape, the trace line shape, the redaction and the byte cap.
# Boundaries: PURE. It reads the dict the investigator hands its sink (`_Investigation.to_dict()`
#             plus `messages`) and returns plain dicts; it writes nothing, imports no kernel and
#             changes nothing the run delivers. CUSTOMER GEOMETRY DOES NOT PASS: a tool result
#             contributes its tool, ok, error and - for an edit - the verdict fields only; the
#             measured data a look or measure returned is not copied. The system prompt is not
#             copied. Model prose is clipped and secret-shaped tokens are redacted.
# Collaborates with: cad/repair/investigator.py (the record), cad/repair/investigator_tools.py
#                    (the envelope and the edit result it parses), the corpus harness and the
#                    product transcript sink (both call `outcome` and `trace`).
from __future__ import annotations

import json
import re

#: The trace's byte ceiling per region record (JSONL bytes). Past it the middle is dropped and a
#: `truncated` line says how many entries went; the `end` line always survives.
TRACE_BYTES_MAX = 64_000
#: How much of a model's prose, or a call's arguments, one trace line keeps.
TEXT_MAX = 400
#: The edit verdict fields a trace keeps. Fingerprints are hashes, never geometry.
_EDIT_KEYS = ("op", "fingerprint", "defect_code", "status", "reason", "kept", "cleared",
              "remaining", "new")

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}"                      # OpenAI / Anthropic style keys
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{8,}"           # bearer tokens
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*\.?[A-Za-z0-9_\-]*"  # JWTs
    r"|AKIA[0-9A-Z]{16}"                            # AWS access key ids
    r"|AIza[0-9A-Za-z_\-]{30,}"                     # Google API keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def redact(text: object, limit: int = TEXT_MAX) -> str:
    """`text` clipped to `limit` characters with secret-shaped tokens replaced."""
    out = _SECRET.sub("[redacted]", "" if text is None else str(text))
    return out if len(out) <= limit else out[:limit] + f"...(+{len(out) - limit} chars)"


def _tool_result(message: dict) -> dict | None:
    """The envelope's `tool_result` when `message` carries one, else None."""
    content = message.get("content")
    if not isinstance(content, str) or "tool_result" not in content:
        return None
    try:
        body = json.loads(content)
    except (TypeError, ValueError):
        return None
    result = body.get("tool_result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else None


def _edit_verdict(result: dict) -> dict | None:
    if result.get("tool") != "edit" or not result.get("ok"):
        return None
    data = result.get("data") or {}
    if not isinstance(data, dict) or "status" not in data:
        return None  # a reset, or a shrunk envelope
    return {k: data[k] for k in _EDIT_KEYS if k in data}


def _edits(record: dict) -> list[dict]:
    return [v for m in record.get("messages") or () if isinstance(m, dict)
            for r in [_tool_result(m)] if r is not None
            for v in [_edit_verdict(r)] if v is not None]


def outcome(record: dict) -> dict:
    """One region's row: how it ended, what it spent, what its edits did, and WHY NO PACKAGE."""
    conclusion = record.get("conclusion") or {}
    form = str(conclusion.get("form") or "")
    end = str(record.get("end") or "")
    reason = str(conclusion.get("reason") or record.get("reason") or "")
    edits = _edits(record)
    kept = sum(1 for e in edits if e.get("kept"))
    rolled: dict[str, int] = {}
    for e in edits:
        if not e.get("kept"):
            key = str(e.get("reason") or e.get("status") or "unknown")
            rolled[key] = rolled.get(key, 0) + 1
    spent = record.get("spent") or {}
    why = ""
    if form != "package":
        parts = [end or "no ending"]
        if reason:
            parts.append(reason)
        if form and form != "package":
            parts.append(f"concluded {form}")
        if not edits:
            parts.append("no edit tried")
        elif not kept:
            parts.append("no edit kept (" + ", ".join(
                f"{k} x{n}" for k, n in sorted(rolled.items())) + ")")
        why = "; ".join(parts)
    return {"region": record.get("region"), "tier": record.get("tier", ""), "end": end,
            "form": form, "reason": redact(reason),
            "tool_calls": int(spent.get("tool_calls") or 0),
            "model_calls": int(spent.get("model_calls") or 0),
            "seconds": float(spent.get("seconds") or 0.0),
            "edits": {"tried": len(edits), "kept": kept,
                      "rolled_back": dict(sorted(rolled.items()))},
            "why_no_package": redact(why)}


def _call_args(message: dict) -> dict[str, tuple[str, str]]:
    """tool_call id -> (tool name, clipped redacted arguments) for one assistant turn."""
    out: dict[str, tuple[str, str]] = {}
    for call in message.get("tool_calls") or ():
        fn = call.get("function") or {}
        out[str(call.get("id"))] = (str(fn.get("name") or ""), redact(fn.get("arguments")))
    return out


def trace(record: dict, *, job_id: str = "", bytes_max: int = TRACE_BYTES_MAX) -> list[dict]:
    """The region's per-turn trace as JSONL-ready dicts, under `bytes_max` bytes in total."""
    region = record.get("region")
    base = {"job_id": str(job_id), "region": region}
    lines: list[dict] = []
    pending: dict[str, tuple[str, str]] = {}
    for message in record.get("messages") or ():
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            continue
        result = _tool_result(message)
        if result is not None:
            name, args = pending.pop(str(message.get("tool_call_id")),
                                     (str(result.get("tool") or ""), ""))
            line = {**base, "kind": "call", "n": result.get("call"), "tool": name,
                    "args": args, "ok": bool(result.get("ok")),
                    "error": redact(result.get("error"))}
            verdict = _edit_verdict(result)
            if verdict is not None:
                line["edit"] = verdict
            lines.append(line)
            continue
        if role == "assistant":
            pending.update(_call_args(message))
            text = message.get("content")
            if text:
                lines.append({**base, "kind": "model", "text": redact(text)})
            continue
        if role == "user" and isinstance(message.get("content"), str):
            # The driver's own turns: the opening ask, an attempt verdict, "conclude now".
            lines.append({**base, "kind": "driver", "text": redact(message["content"])})
    end = {**base, "kind": "end", "end": record.get("end", ""),
           "conclusion": {k: redact(v) if isinstance(v, str) else v
                          for k, v in (record.get("conclusion") or {}).items()
                          if k in ("form", "outcome", "reason")},
           "budget": record.get("budget") or {}, "spent": record.get("spent") or {},
           "outcome": outcome(record)}
    return _capped(lines, end, bytes_max)


def _size(line: dict) -> int:
    return len(json.dumps(line, default=str).encode()) + 1


def _capped(lines: list[dict], end: dict, bytes_max: int) -> list[dict]:
    """Keep the head and the tail; drop the middle with a visible `truncated` line."""
    total = sum(_size(ln) for ln in lines) + _size(end)
    if total <= bytes_max:
        return [*lines, end]
    room = bytes_max - _size(end) - 120  # the truncated marker
    head: list[dict] = []
    tail: list[dict] = []
    i, j = 0, len(lines) - 1
    while i <= j and room > 0:
        take_head = len(head) <= len(tail)
        line = lines[i] if take_head else lines[j]
        cost = _size(line)
        if cost > room:
            break
        room -= cost
        if take_head:
            head.append(line)
            i += 1
        else:
            tail.append(line)
            j -= 1
    dropped = len(lines) - len(head) - len(tail)
    marker = {"job_id": end["job_id"], "region": end["region"], "kind": "truncated",
              "dropped": dropped}
    return [*head, marker, *reversed(tail), end]
