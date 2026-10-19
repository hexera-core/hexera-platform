# Responsibility: Make an AI step give the same answer to the same question: key every model round by what the model was asked, keep the answer, and reuse it when asked again.
# Owns: the decision key (step, model, prompt hash, canonical inputs, owner), the cache record, the per-job list of decisions for the run record, and the router wrapper that applies them.
# Boundaries: a wrapper around the ModelRouter seam; it never changes a request, only answers one from the record. The store behind it is the object store contract.
# Collaborates with: contracts/model_inference (the seam it wraps), contracts/object_storage (where records live), runtime/composition (installs it), application/pipeline_run (copies a job's decisions into its run record).
"""THE SAME FILE GETS THE SAME DECISIONS.

The shipped models (OpenAI's Responses API, reasoning models) take neither a temperature nor a
seed, so a model step cannot be made repeatable by sampling settings. It is made repeatable by
RECORD: every model round is keyed by everything the model is shown - the step (role), the model,
a hash of the instructions and tools (the prompt version: any prompt or tool change is a new key),
the conversation itself with its run-specific noise taken out (ids, paths, timestamps, signed-URL
query strings; an image counts by the hash of its bytes), and the owner (a decision is never shared
across accounts). The answer is kept under that key.

    AI_DECISION_CACHE=off      no keys, no records (the old behaviour)
    AI_DECISION_CACHE=record   every round is keyed and recorded; the model is always asked
    AI_DECISION_CACHE=reuse    (default) a round asked before is answered from its record

A conversation is replayed round by round: when the first round is answered from the record, the
second is asked with the same history and hits its own record, and so on - so a whole intake,
build or review repeats exactly, until the inputs differ (a different mesh result, a different
user sentence), and from there the model is asked again. Only a usable answer (no failure marker,
some tool call or text) is ever kept: a provider outage is never replayed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)
from meshpipeline.settings.env import ConfigurationError, optional_env

logger = logging.getLogger(__name__)

MODES = ("off", "record", "reuse")
AI_DECISION_CACHE: str = (optional_env("AI_DECISION_CACHE", "reuse") or "reuse").strip().lower()
if AI_DECISION_CACHE not in MODES:
    raise ConfigurationError(f"AI_DECISION_CACHE must be one of {MODES}, got {AI_DECISION_CACHE!r}")

#: Bumped when the key or the record changes shape: old records then simply stop matching.
KEY_VERSION = 1
PREFIX = f"decision-cache/v{KEY_VERSION}"
#: Decisions kept per job for its run record (a builder run is at most a few hundred rounds).
_PER_JOB = 400
_JOBS = 64

# run-specific noise, replaced before hashing
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LONG_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?\b")
_TOOL_ID = re.compile(r"\b(call|fc|toolu|tool)_[A-Za-z0-9_-]{6,}\b")
_ABS_PATH = re.compile(r"(?<![\w.])/(?:[\w.@+-]+/)+([\w.@+-]+)")
_TMP_SUFFIX = re.compile(r"\b(tmp|cr_ws_|lab-|ws_)[A-Za-z0-9_]{4,}\b")
_DATA_URL = re.compile(r"data:([\w/+.-]+);base64,([A-Za-z0-9+/=]+)")


def _canon_text(text: str) -> str:
    def _data(m: re.Match) -> str:
        try:
            raw = base64.b64decode(m.group(2), validate=False)
        except (ValueError, TypeError):
            raw = m.group(2).encode()
        # "sha" glued to the digest: the long-hex rule below must not erase which picture it was
        return f"<{m.group(1)} sha{hashlib.sha256(raw).hexdigest()}>"
    t = _DATA_URL.sub(_data, text)
    t = re.sub(r"(https?://[^\s\"'?]+)\?[^\s\"']*", r"\1", t)     # signed-URL query strings
    t = _UUID.sub("<id>", t)
    t = _ISO_TIME.sub("<time>", t)
    t = _TOOL_ID.sub("<call>", t)
    t = _ABS_PATH.sub(r"<path>/\1", t)
    t = _TMP_SUFFIX.sub(r"\1<x>", t)
    return _LONG_HEX.sub("<hex>", t)


def _canon(value: Any) -> Any:
    if isinstance(value, str):
        return _canon_text(value)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                if k not in ("id", "tool_call_id")}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, float):
        return round(value, 9)
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def prompt_hash(messages: list, tools: list | None) -> str:
    """The prompt VERSION of a round: its instructions (every system turn) and its tool schemas.
    Any change to a prompt file, a prompt block or a tool's declaration gives a new hash."""
    system = [_canon(m.get("content")) for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    return _digest({"system": system, "tools": _canon(tools or [])})[:16]


def decision_key(*, step: str, model: str, messages: list, tools: list | None,
                 tool_choice: Any, owner: str) -> tuple[str, str, str]:
    """(key, prompt hash, inputs hash) of one model round."""
    ph = prompt_hash(messages, tools)
    turns = [_canon(m) for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
    ih = _digest({"turns": turns, "tool_choice": _canon(tool_choice)})[:32]
    key = _digest({"v": KEY_VERSION, "step": step, "model": model, "prompt": ph, "inputs": ih,
                   "owner": owner or ""})
    return key, ph, ih


# ----------------------------------------------------------------------- the record --------
def _to_record(r: ModelRoundResult) -> dict:
    d = asdict(r)
    d["reasoning_text"] = ""           # transport only: never kept (contracts/model_inference)
    return d


def _from_record(d: dict) -> ModelRoundResult:
    """A replayed round: the recorded answer, with NO new usage - no provider was called, so no
    tokens and no attempts are counted for this run (the record keeps the original usage)."""
    calls = tuple(ToolCallRequest(**c) for c in d.get("tool_calls") or ())
    p = d.get("provider") or {}
    prov = ProviderAttemptInfo(attempts=0, provider=str(p.get("provider") or ""), model=str(p.get("model") or ""))
    keep = {k: d[k] for k in ("assistant_text", "finish_reason") if k in d}
    return ModelRoundResult(tool_calls=calls, provider=prov, **keep)


def _usable(r: ModelRoundResult) -> bool:
    return r.ok and bool(r.tool_calls or (r.assistant_text or "").strip())


def _replayable(r: ModelRoundResult) -> bool:
    """An answer may be replayed only when it carries nothing of the run that produced it: no
    id, path, time or token (a preview token is minted per run, and a replayed one would be
    refused). Such an answer is still recorded; the model is simply asked again."""
    for c in r.tool_calls:
        if "token" in c.arguments.lower() or _canon_text(c.arguments) != c.arguments:
            return False
    return _canon_text(r.assistant_text or "") == (r.assistant_text or "")


class ObjectStoreDecisions:
    """Records in the object store: one small JSON object per key."""

    def get(self, key: str) -> dict | None:
        from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store
        try:
            raw = get_object_store().get_bytes(object_key=f"{PREFIX}/{key[:2]}/{key}.json")
        except ObjectNotFound:
            return None
        data = json.loads(raw.decode())
        return data if isinstance(data, dict) else None

    def put(self, key: str, record: dict) -> None:
        from meshpipeline.contracts.object_storage import get_object_store
        with tempfile.TemporaryDirectory(prefix="decision_") as tmp:
            p = Path(tmp) / "record.json"
            p.write_text(json.dumps(record, default=str))
            get_object_store().upload_file(local_path=p, object_key=f"{PREFIX}/{key[:2]}/{key}.json",
                                           content_type="application/json")


class MemoryDecisions:
    """Records in this process only (tests, and a process with no object store)."""

    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self._d.get(key)

    def put(self, key: str, record: dict) -> None:
        self._d[key] = record


# ------------------------------------------------------------------ the run record -----------
_LOG: OrderedDict[str, list[dict]] = OrderedDict()
_LOG_LOCK = threading.Lock()


def _log(job_id: str, entry: dict) -> None:
    if not job_id:
        return
    with _LOG_LOCK:
        rows = _LOG.setdefault(job_id, [])
        _LOG.move_to_end(job_id)
        if len(rows) < _PER_JOB:
            rows.append(entry)
        while len(_LOG) > _JOBS:
            _LOG.popitem(last=False)


def decisions_for(job_id: str) -> list[dict]:
    """Every AI decision this process made for `job_id`: step, model, prompt hash, inputs hash,
    key, whether it was asked or answered from the record, and the decision (tool names and
    arguments, or the first words of the text). The record behind each key holds the answer in
    full, so a run can be replayed (AI_DECISION_CACHE=reuse) or audited."""
    with _LOG_LOCK:
        return list(_LOG.get(job_id, []))


def _summary(r: ModelRoundResult) -> dict:
    return {"tool_calls": [{"name": c.name, "arguments": c.arguments[:2000]} for c in r.tool_calls],
            "text": (r.assistant_text or "")[:400]}


# ------------------------------------------------------------------- the wrapper -------------
class CachingModelRouter:
    """A ModelRouter that answers a round it has a record for (mode `reuse`), records every usable
    answer (`record`, `reuse`), and logs every decision for the run record. `model_of(step)` names
    the model a step's route asks, so a model change is a new key."""

    def __init__(self, inner, *, model_of: Callable[[str], str], store=None,
                 mode: str | None = None) -> None:
        self._inner = inner
        self._model_of = model_of
        self._store = store if store is not None else ObjectStoreDecisions()
        self._mode = (mode or AI_DECISION_CACHE).lower()

    async def _round(self, step: str, call, *, messages, tools, tool_choice, job_id: str,
                     user_id: str) -> ModelRoundResult:
        if self._mode == "off":
            return await call()
        if not user_id:
            # no owner, no shared record: a decision is never shared across accounts
            result = await call()
            _log(job_id, {"step": step, "source": "model (no owner: not kept)",
                          "at": round(time.time(), 1), "decision": _summary(result)})
            return result
        try:
            model = str(self._model_of(step) or "")
            key, ph, ih = decision_key(step=step, model=model, messages=messages, tools=tools,
                                       tool_choice=tool_choice, owner=user_id)
        except Exception:  # noqa: BLE001 - a key that cannot be made only means "ask the model"
            logger.warning("decision cache: no key for a %s round; asking the model", step,
                           exc_info=True)
            return await call()
        entry: dict[str, Any] = {"step": step, "model": model, "prompt": ph, "inputs": ih,
                                 "key": key[:24], "at": round(time.time(), 1)}
        if self._mode == "reuse":
            try:
                rec = self._store.get(key)
            except Exception:  # noqa: BLE001 - the record is an aid, never a dependency
                logger.warning("decision cache: lookup failed; asking the model", exc_info=True)
                rec = None
            if rec and isinstance(rec.get("result"), dict) and _replayable(_from_record(rec["result"])):
                result = _from_record(rec["result"])
                _log(job_id, {**entry, "source": "record", "decision": _summary(result)})
                return result
        result = await call()
        source = "model"
        if _usable(result):
            try:
                self._store.put(key, {"key": key, "step": step, "model": model, "prompt": ph,
                                      "inputs": ih, "stored_at": round(time.time(), 1),
                                      "result": _to_record(result)})
            except Exception:  # noqa: BLE001
                logger.warning("decision cache: could not keep the answer", exc_info=True)
                source = "model (not kept)"
        else:
            source = "model (unusable, not kept)"
        _log(job_id, {**entry, "source": source, "decision": _summary(result)})
        return result

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None, on_reasoning=None):
        return await self._round("builder", lambda: self._inner.call_builder_model(
            messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id, user_id=user_id,
            parallel_tool_calls=parallel_tool_calls, on_reasoning=on_reasoning),
            messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id, user_id=user_id)

    async def call_planner_model(self, messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None, on_reasoning=None):
        return await self._round("planner", lambda: self._inner.call_planner_model(
            messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id, user_id=user_id,
            parallel_tool_calls=parallel_tool_calls, on_reasoning=on_reasoning),
            messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id, user_id=user_id)

    async def call_intake_model(self, messages, job_id="", user_id="", name="Intake", tools=None):
        return await self._round("intake", lambda: self._inner.call_intake_model(
            messages=messages, job_id=job_id, user_id=user_id, name=name, tools=tools),
            messages=messages, tools=tools, tool_choice=name, job_id=job_id, user_id=user_id)

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="",
                                       parallel_tool_calls=None, on_reasoning=None):
        return await self._round("reviewer", lambda: self._inner.call_reviewer_with_tools(
            messages=messages, tools=tools, job_id=job_id, user_id=user_id,
            parallel_tool_calls=parallel_tool_calls, on_reasoning=on_reasoning),
            messages=messages, tools=tools, tool_choice="auto", job_id=job_id, user_id=user_id)

    async def call_summarizer_model(self, messages, job_id=""):
        # web search results are the world's, not ours: a summary of them is never replayed
        return await self._inner.call_summarizer_model(messages=messages, job_id=job_id)


__all__ = ["AI_DECISION_CACHE", "CachingModelRouter", "KEY_VERSION", "MODES", "MemoryDecisions",
           "ObjectStoreDecisions", "decision_key", "decisions_for", "prompt_hash"]
