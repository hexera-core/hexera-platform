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
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

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


#: The rollback bucket for edits that ran but whose result the conversation no longer shows.
ELIDED = "unknown (result elided)"


def _is_reset(args: object) -> bool:
    """True when an edit call's (clipped) argument text asked for a reset of the clone."""
    text = str(args or "")
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return '"reset": true' in text
    return isinstance(body, dict) and body.get("reset") is True


def _elided(message: dict) -> dict | None:
    content = message.get("content")
    if not isinstance(content, str) or '"elided"' not in content:
        return None
    try:
        body = json.loads(content)
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("elided") is True else None


def outcome(record: dict) -> dict:
    """One region's row: how it ended, what it spent, what its edits did, and WHY NO PACKAGE."""
    conclusion = record.get("conclusion") or {}
    form = str(conclusion.get("form") or "")
    end = str(record.get("end") or "")
    reason = str(conclusion.get("reason") or record.get("reason") or "")
    # THE CONVERSATION IS NOT THE LEDGER. Past CONVERSATION_BYTES_MAX the driver replaces old tool
    # results with `elided` stubs, so the visible edit verdicts undercount. `turns` (one per call,
    # never elided) says how many edits RAN; `edits` (the kept-edit log) says how many were kept;
    # the visible verdicts give the reasons they can, and the rest are counted as elided.
    edits = _edits(record)
    turns = record.get("turns")
    log = record.get("edits")
    ran = sum(1 for t in (turns if isinstance(turns, list) else ()) if isinstance(t, dict)
              and t.get("tool") == "edit" and t.get("ok") and not _is_reset(t.get("args")))
    tried = max(len(edits), ran)
    kept = max(sum(1 for e in edits if e.get("kept")), len(log) if isinstance(log, list) else 0)
    rolled: dict[str, int] = {}
    for e in edits:
        if not e.get("kept"):
            key = str(e.get("reason") or e.get("status") or "unknown")
            rolled[key] = rolled.get(key, 0) + 1
    unseen = tried - kept - sum(rolled.values())
    if unseen > 0:
        rolled[ELIDED] = unseen
    spent = record.get("spent") or {}
    why = ""
    if form != "package":
        parts = [end or "no ending"]
        if reason:
            parts.append(reason)
        if form and form != "package":
            parts.append(f"concluded {form}")
        if not tried:
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
            "edits": {"tried": tried, "kept": kept,
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
        stub = _elided(message)
        if stub is not None:
            name, args = pending.pop(str(message.get("tool_call_id")),
                                     (str(stub.get("tool") or ""), ""))
            lines.append({**base, "kind": "call", "n": stub.get("call"), "tool": name,
                          "args": args, "elided": True})
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


# --- the product collector -------------------------------------------------------------------
#
# The investigator hands each region's record to its module-level `sink` (None in the product).
# A run that wants its trace opens a collector: `start()` installs `dispatch` as that sink -
# chaining whatever a harness had installed - and `stop()` puts the previous sink back once no
# run in the process is collecting. The records go to the collector bound IN THIS CONTEXT
# (a ContextVar, which `asyncio.to_thread` copies), so concurrent repairs never mix records.

_current: ContextVar[Collector | None] = ContextVar("investigation_trace_collector",
                                                    default=None)
_lock = threading.Lock()
_state: dict = {"active": 0, "previous": None}


@dataclass
class Collector:
    records: list[dict] = field(default_factory=list)
    token: object = None


def dispatch(record: dict) -> None:
    """The sink installed on the investigator while any run collects."""
    previous = _state["previous"]
    if callable(previous):
        previous(record)
    collector = _current.get()
    if collector is not None:
        collector.records.append(dict(record))


def start() -> Collector:
    from meshpipeline.cad.repair import investigator as inv

    collector = Collector()
    collector.token = _current.set(collector)
    with _lock:
        if inv.sink is not dispatch:
            _state["previous"] = inv.sink
            setattr(inv, "sink", dispatch)  # noqa: B010 - typed None in the product
        _state["active"] += 1
    return collector


def stop(collector: Collector) -> None:
    from meshpipeline.cad.repair import investigator as inv

    try:
        _current.reset(collector.token)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        _current.set(None)
    with _lock:
        _state["active"] = max(0, _state["active"] - 1)
        if _state["active"] == 0 and inv.sink is dispatch:
            setattr(inv, "sink", _state["previous"])  # noqa: B010
            _state["previous"] = None


#: The whole job's trace ceiling, shared across its regions.
JOB_TRACE_BYTES_MAX = 512_000


def summary(records: list[dict], *, job_id: str = "",
            bytes_max: int = JOB_TRACE_BYTES_MAX) -> dict:
    """The run's outcome table and its trace lines. A region's LATEST record is used: a re-call
    continues the same investigation, so its record already holds the earlier conversation."""
    latest: dict = {}
    for record in records:
        latest[record.get("region")] = record
    regions = list(latest.values())
    share = bytes_max // max(1, len(regions))
    lines = [ln for r in regions for ln in trace(r, job_id=job_id, bytes_max=share)]
    return {"outcomes": [outcome(r) for r in regions], "lines": lines,
            "truncated": any(ln.get("kind") == "truncated" for ln in lines)}


def begin_if_enabled() -> Collector | None:
    """A collector when `CAD_REPAIR_INVESTIGATION_TRACE` is on, else None (the product default)."""
    import meshpipeline.settings.cad_repair as rcfg

    if not bool(getattr(rcfg, "CAD_REPAIR_INVESTIGATION_TRACE", False)):
        return None
    return start()


def finish(collector: Collector | None, *, job_id: str = "") -> dict | None:
    """Close `collector` and return its `summary`; None when no collector was opened."""
    if collector is None:
        return None
    stop(collector)
    return summary(collector.records, job_id=job_id)


# --- the review, for audit -------------------------------------------------------------------
#
# STORED, NEVER FED BACK. What a judged package's review said, as MEASURED fields only: the
# verdict, the tier that gave it, the session's fixed revert reason and the measured objection (a
# field name from a closed tuple plus the session's own value). The reviewer's prose
# (`review_reason`) is not copied: it is the reviewer's, and a report a customer or an author can
# read must not carry it. Nothing here reaches an author's attempt log.

#: The measured blocks of `package_review.evidence_of` that may travel into a row, each named so
#: nothing arrives by being copied through. Scalars and small dicts of numbers or code names.
_EVIDENCE_MEASURED = (
    "area_mm2", "volume_mm3", "faces", "solids", "diagonal_mm", "area_mm2_before",
    "volume_mm3_before", "deviation_status", "deviation_basis", "deviation_mm",
    "deviation_samples", "deviation_faces", "deviation_faces_skipped", "removed",
    "mesh_before", "mesh_after",
    # WHICH ARTEFACT `mesh_after` IS, and the other reading when they disagree. The reviewer
    # judges the DELIVERED STEP, because a repaired shape can fail in memory and mesh once
    # exported (measured on abc 0060_00608290, three probes each way). A row that carried only
    # the authoritative number would hide exactly the disagreement these keys exist to show.
    "mesh_after_source", "mesh_after_in_memory",
    "defects_cleared", "defects_revealed", "defects_remaining",
)
#: Evidence keys that are PROSE and never travel: the author's own words for what it meant to do,
#: and the words of this region's earlier review rejects.
_EVIDENCE_PROSE = ("author_claim",)


def measured_evidence(evidence: object) -> dict:
    """The MEASURED half of the evidence a review verdict was made on. PROSE-FREE, BY NAMING.

    WHY A ROW NEEDS IT. Two gate runs at one commit judged the same operation on the same target
    and reached opposite verdicts - one reject naming `mesh_after` as still failing, one accept -
    and neither could be explained, because the row kept the verdict and dropped what it was made
    on. Without this, "the reviewer is inconsistent" and "the probe gave a different answer" look
    identical in the data, and they are very different problems.

    BUILT BY NAMING EVERY KEY, like `evidence_of` itself: an operation's own evidence dict, a raw
    reply, or any field a future caller leaves in the block cannot reach a row by default. The
    operation list keeps WHAT ran and how it ended, never why; an earlier reject is a COUNT, never
    its words. A clipped block is reported as clipped rather than as an empty measurement.
    """
    if not isinstance(evidence, dict) or not evidence:
        return {}
    if "clipped" in evidence and len(evidence) == 1:
        return {"clipped": str(evidence["clipped"])}
    out: dict = {key: evidence[key] for key in _EVIDENCE_MEASURED if key in evidence}
    if isinstance(evidence.get("ops"), list):
        out["ops"] = [{"op": str(op.get("op", "")), "status": str(op.get("status", ""))}
                      for op in evidence["ops"] if isinstance(op, dict)]
    rejects = evidence.get("review_rejects")
    if isinstance(rejects, dict):
        out["review_rejects"] = {"count": int(rejects.get("count") or 0)}
    for prose in _EVIDENCE_PROSE:
        out.pop(prose, None)
    return out


def review_of(package: dict) -> dict:
    """One judged package's review, prose-free: the verdict, and the measurements it was made on."""
    objection = package.get("review_objection")
    row = {"region": package.get("region_index"), "status": str(package.get("status") or ""),
           "reason": str(package.get("reason") or ""),
           "reviewed": str(package.get("reviewed") or ""),
           "review_tier": str(package.get("review_tier") or ""),
           "objection": dict(objection) if isinstance(objection, dict) else None}
    measured = measured_evidence(package.get("review_evidence"))
    return {**row, "evidence": measured} if measured else row


def with_reviews(traced: dict, packages: list) -> dict:
    """`traced` with each outcome row carrying its region's judged packages (`review_of`)."""
    by_region: dict = {}
    for package in packages or ():
        if isinstance(package, dict):
            by_region.setdefault(package.get("region_index"), []).append(review_of(package))
    return {**traced, "outcomes": [{**row, "packages": by_region.get(row.get("region"), [])}
                                   for row in traced.get("outcomes") or ()]}
