# Responsibility: The v4 AI proposer - one model conversation per file that proposes parametric repair candidates.
# Owns: AIProposer (a search Generator), its two tools (`propose`, `declare_unfixable`), the system prompt and tool
#       schemas built from the op registry, the unfixable-site registry, and the route probe that runs before a wave.
# Boundaries: it PROPOSES; the search driver evaluates and the deterministic verdict decides. It makes no op call,
#             reads no geometry, prices nothing (the router prices each round) and keeps no shared state of its own
#             (spend goes through the wave ledger). It never imports a provider adapter: the model is reached only
#             through contracts.model_inference.call_repair_v4_propose, or an injected stand-in.
# Collaborates with: ops (REGISTRY, validate_call), model_ledger (the caps), sites (observation), search (Candidate).
"""The AI proposer.

WHY IT IS NOT v3's. v3's AI chose among the same thirteen kernel ops the rules used, was rewarded for making a
defect fingerprint vanish, and was checked by a cheap probe. Here the reward is the clean-STEP verdict itself
(the search driver evaluates every candidate with it), the unit of work is a failure SITE, the action space is the
parametric op library with model-chosen parameters, and after every search round the model is shown what its own
candidates did (`feedback`). The proposer owns the judgement the deterministic sweeps cannot make: overlap intent,
hole versus missing face, fix choice under the fidelity budget, ordering, and when to stop.

TRANSPORT IS STRICT AND FORGIVING AT ONCE. Every tool call is validated here with `ops.validate_call` (which never
raises) and the model hears about each problem as a structured tool result it can act on - a target an earlier op
removed comes back as "F7: removed by op 'split_angle'; it no longer exists". A malformed call costs a turn and
nothing else: it is not a refusal streak, it ends nothing, and the next turn is allowed.

COST IS ENFORCED HERE, NOT HOPED FOR. Before every model call the proposer asks the wave ledger for room (the per-
file ceiling and the shared wave cap), and after it the router's priced reply is written to that ledger. When
either limit is reached the proposer stops calling the model, returns no candidates, and reports `ai_capped`, so
the file carries on with the deterministic generators alone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from meshpipeline.cad.repair_v4.model_ledger import Admission, WaveLedger
from meshpipeline.cad.repair_v4.ops import REGISTRY, validate_call
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff
from meshpipeline.contracts.model_inference import (
    REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS,
    ModelRoundResult,
    ProposeReply,
    ToolCallRequest,
    ToolSchema,
    assistant_turn,
    tool_result_turn,
)

#: Site kinds whose fix turns on INTENT the geometry alone does not state, so a picture earns its tokens:
#: two bodies overlapping (duplicate, fit, or subtract?) and an open loop (hole, or a missing face?).
#: Every other kind is decided by the numbers in the observation.
IMAGE_KINDS = frozenset({"overlap", "open"})

PROPOSE = "propose"
DECLARE_UNFIXABLE = "declare_unfixable"

#: A conservative characters-per-token (real text runs nearer 4) and what one image is assumed to cost in tokens.
_CHARS_PER_TOKEN = 3.0
IMAGE_TOKENS = 2000
#: Refuse a call whose estimated INPUT exceeds this many tokens (at admit, before it is made).
MAX_INPUT_TOKENS = 120_000
_GROWTH = 1.25
_T = TypeVar("_T")

StopReason = Literal["wave_cap", "file_ceiling", "max_turns", "model_failure", "input_too_large"]
#: Consecutive failed model rounds (the router has already retried each) before the AI is given up on for a file.
MAX_CONSECUTIVE_FAILURES = 3

ModelCall = Callable[..., Awaitable[ProposeReply]]


# --- types ---------------------------------------------------------------------------------------------------


@dataclass
class ProposedCandidate:
    """What the proposer hands the search driver. Field-for-field the driver's `Candidate(op, targets, params,
    source, rationale)` (Task 18): a `run_stage` proposal is `op="run_stage"`, `targets=[]`,
    `params={"stage": name, "params": {...}}`. Swapped for `search.Candidate` when the two branches merge."""

    op: str
    targets: list[str]
    params: dict
    source: Literal["det", "ai"] = "ai"
    rationale: str = ""


@dataclass
class Outcome:
    """What happened to one of the proposer's candidates, for the next round's feedback. `status` is
    "applied" (evaluated: `diff` and `delta` set), "declined" / "failed" / "invalid" (`message` says why)."""

    candidate: ProposedCandidate
    status: Literal["applied", "declined", "failed", "invalid"]
    diff: Diff | None = None
    #: metric -> [before, after], e.g. {"valid": [0, 1], "panel_passes": [1, 2], "min_sicn": [0.001, 0.2]}
    delta: dict | None = None
    message: str = ""


class UnfixableRegistry:
    """Sites the AI has declared unfixable within the fidelity bound. Shared with the deterministic generators
    (construct one and pass it to both) so a declared site is not swept again for nothing."""

    def __init__(self) -> None:
        self.reasons: dict[str, str] = {}

    def declare(self, site_id: str, reason: str) -> None:
        self.reasons[site_id] = reason

    def __contains__(self, site_id: object) -> bool:
        return site_id in self.reasons

    def filter(self, sites: Sequence[Site]) -> list[Site]:
        return [s for s in sites if s.id not in self.reasons]


class NoUsableRoute(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    route: str
    detail: str = ""


# --- the static prefix: system prompt and tools ----------------------------------------------------------------


def _catalogue(stage_names: Sequence[str]) -> str:
    lines = []
    for name in sorted(REGISTRY):
        lines.append(f"- {name}: {json.dumps(REGISTRY[name].params_schema, sort_keys=True, separators=(',', ':'))}")
    if stage_names:
        lines.append("- run_stage: a whole deterministic healing stage, one of "
                     + ", ".join(sorted(stage_names)) + "; give {\"run_stage\": <name>, \"params\": {...}}")
    return "\n".join(lines)


def system_prompt(*, stage_names: Sequence[str] = ()) -> str:
    """The static system turn. Byte-identical for every file and every round with the same stage list, so the
    provider's prompt cache serves it (it is the first thing in the request, followed by the tool schemas)."""
    return f"""You repair CAD B-rep files (STEP) so that customers can mesh them in ANY mesher. You work one file at a
time, in one conversation: you see the failure sites of the current best state, propose parametric repair
operations on them, and are told what each of your candidates did to the part.

WHAT CLEAN MEANS. A file is clean only when it passes all three layers of the checker, and the checker - not you,
and not a rule of thumb - decides.
1. Valid: it re-reads after a STEP write/read round trip and BRepCheck reports every solid valid; every body is a
   closed, manifold, consistently oriented solid with positive volume; no free edges, no self-intersecting faces
   or wires, no small-edge or small-face errors; bodies do not overlap except where they share faces exactly
   (conformal contact); units are unchanged.
2. Meshable: it meshes in every mesher on a fixed panel, within the time limit, with no tolerant fallback and no
   hints sidecar, and the worst element quality (min SICN) is above 0.01.
3. Faithful: the maximum sampled surface distance from the original is at most min(0.1 mm, 0.1% of the part
   diagonal); area and volume changes stay under their limits; no body is lost and no face is lost except a
   declared sliver below the threshold. A repair that makes a mesher happy by moving the surface farther than that
   bound is a failure, not a fix.

WHAT YOU OWN. The checker finds sites; you supply the judgement deterministic sweeps cannot:
- overlap intent: are two overlapping bodies a duplicate, a fit, or one meant to be subtracted from the other?
- hole versus missing face: is an open loop a real hole to keep, or a face that should exist?
- fix choice under the deviation budget: when several fixes would work, pick the one that stays inside the
  fidelity bound, and prefer the smallest change;
- ordering and recovery: operations interact; a split can renumber faces, a sew can undo a split;
- mapping a mesher's error to the entity that causes it;
- when to stop: if a site cannot be fixed inside the bound, call {DECLARE_UNFIXABLE} with the site id and the
  reason instead of spending turns on it. Declared sites are not retried.

HOW YOU ACT. Reply only with tool calls.
- {PROPOSE}(candidates): one to a few candidates, each {{op | run_stage, targets, params, rationale}}. `targets` are
  stable entity ids such as "F41" (face), "E7" (edge), "B2" (solid); an id keeps its name across ops that only
  trim or heal it, and an id that an op removed or replaced is dead - use the ids in the latest observation.
  Candidates are evaluated for you in parallel; you will see each one's Diff and the change in the verdict next
  round. A candidate that makes the verdict worse is simply discarded; you lose a turn, never the file.
- {DECLARE_UNFIXABLE}(site_id, reason).
A malformed call costs nothing but a turn: you will get a structured error naming each problem (an unknown op, a
parameter out of bounds, a target that no longer exists) and may call again. The turns, the dollars spent on you
and the wall-clock are all budgeted and shown to you; stop when the file is clean or nothing within the bound
remains.

OPERATIONS AND THEIR PARAMETER SCHEMAS (JSON Schema; `x-targets` says what `targets` must hold):
{_catalogue(stage_names)}
"""


def tool_schemas(*, stage_names: Sequence[str] = ()) -> list[dict]:
    item_props: dict[str, Any] = {
        "op": {"type": "string", "enum": sorted(REGISTRY),
               "description": "a registered repair operation (give this OR run_stage)"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "stable entity ids the op acts on, per its x-targets"},
        "params": {"type": "object", "description": "the op's parameters, per its schema in the system prompt"},
        "rationale": {"type": "string", "description": "why this fix, in one or two sentences"},
    }
    if stage_names:
        item_props["run_stage"] = {"type": "string", "enum": sorted(stage_names),
                                   "description": "a whole healing stage (give this OR op)"}
    propose = ToolSchema(
        PROPOSE, "Propose repair candidates for the search to evaluate.",
        {"type": "object", "properties": {"candidates": {
            "type": "array", "minItems": 1,
            "items": {"type": "object", "properties": item_props, "required": ["rationale"],
                      "additionalProperties": False}}},
         "required": ["candidates"], "additionalProperties": False})
    unfixable = ToolSchema(
        DECLARE_UNFIXABLE, "Declare a site unfixable within the fidelity bound; it will not be retried.",
        {"type": "object", "properties": {"site_id": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["site_id", "reason"], "additionalProperties": False})
    return [propose.definition(), unfixable.definition()]


def cache_key_for(system: str, tools: list[dict]) -> str:
    """Names the static prefix: it changes exactly when the prompt, catalogue or tool schemas do."""
    blob = json.dumps({"system": system, "tools": tools}, sort_keys=True)
    return "crs-v4-" + hashlib.sha256(blob.encode()).hexdigest()[:24]


# --- helpers -------------------------------------------------------------------------------------------------------


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Drive a coroutine from the search driver's synchronous `propose`, whether or not a loop is running here."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _png_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _default_observe(state: Any, sites: list[Site]) -> dict:
    # Task 17's module, imported late so this one loads (and is tested) without it
    from meshpipeline.cad.repair_v4.sites import observation
    return observation(state.ws, state.verdict, sites, state.budget)


def _diff_json(diff: Diff | None) -> dict | None:
    if diff is None:
        return None
    return {"op": diff.op, "params": diff.params, "modified": diff.modified, "generated": diff.generated,
            "removed": diff.removed, "declared_added": diff.declared_added,
            "declared_removed": diff.declared_removed}


def _outcome_json(o: Outcome) -> dict:
    c = o.candidate
    return {"op": c.op, "targets": c.targets, "params": c.params, "rationale": c.rationale,
            "status": o.status, "message": o.message, "diff": _diff_json(o.diff), "delta": o.delta}


def _message_tokens(messages: Sequence[dict]) -> int:
    chars, images = 0, 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    images += 1
                else:
                    chars += len(part.get("text", ""))
        for call in m.get("tool_calls") or ():
            chars += len(call["function"]["arguments"]) + len(call["function"]["name"])
    return int(chars / _CHARS_PER_TOKEN) + images * IMAGE_TOKENS


def estimate_call_usd(messages: Sequence[dict], tools: Sequence[dict], *, prices: tuple[float, float, float],
                      output_tokens: int) -> tuple[float, int]:
    """(an UPPER BOUND on what sending `messages` will cost, the estimated input tokens).

    Input is chars/3 per token plus a flat allowance per image (history is re-sent whole, images included, so they
    are counted every call), plus the tool schemas, ALL priced at the UNCACHED rate: a cap that assumes a cache hit
    leaks one call's miss, so the cached rate is never used here. Output is reserved in full at the `output_tokens`
    the router will allow, reasoning included. Real usage cannot exceed this unless the tokenizer is far worse than
    3 chars/token. `prices` are the ROUTE'S OWN (fresh input, output, cached) per 1M tokens."""
    p_in, p_out, _cached = prices
    tokens = _message_tokens(messages) + int(len(json.dumps(tools)) / _CHARS_PER_TOKEN)
    return (tokens * p_in + output_tokens * p_out) / 1e6, tokens


# --- the proposer ----------------------------------------------------------------------------------------------------


class AIProposer:
    """A `Generator` (`propose(state, k) -> list[Candidate]`) backed by one model conversation per file.

    `state` is read for `.ws`, `.verdict` (`.clean`) and `.sites`, and handed to `observe(state, sites)` (default:
    Task 17's `observation`) to make what the model is shown. `route` is a `provider:model` from
    `contracts.model_inference.REPAIR_V4_PROPOSE_ROUTES` (see `select_route`). `file_id` and `job_id` name the file
    in the ledger and in telemetry. After each search round call `feedback()` with what became of the candidates;
    it is delivered on the next `propose`.
    """

    def __init__(self, route: str, *, max_turns: int, usd_ceiling: float, ledger: WaveLedger,
                 call: ModelCall | None = None, file_id: str = "", job_id: str = "", user_id: str = "",
                 observe: Callable[[Any, list[Site]], dict] | None = None,
                 stage_names: Sequence[str] = (), unfixable: UnfixableRegistry | None = None,
                 prices: tuple[float, float, float] | None = None,
                 output_tokens: int = REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS,
                 max_input_tokens: int = MAX_INPUT_TOKENS) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if not usd_ceiling > 0:
            raise ValueError("usd_ceiling must be positive")
        if not isinstance(ledger, WaveLedger):
            raise TypeError("a WaveLedger is required: the cap is not optional")
        self.route = route
        self.max_turns = max_turns
        self.usd_ceiling = usd_ceiling
        self.file_id = file_id
        self.job_id = job_id
        self.user_id = user_id
        self.unfixable = unfixable if unfixable is not None else UnfixableRegistry()
        self.stage_names = tuple(sorted(stage_names))
        self.turns_used = 0
        self.stop_reason: StopReason | None = None
        self._call = call
        self._ledger = ledger
        self._observe = observe or _default_observe
        # the route's OWN prices (raises UnpricedModel here, at construction, when it has none)
        if prices is None:
            from meshpipeline.contracts.model_inference import repair_v4_route_prices
            prices = repair_v4_route_prices(route)
        self._prices = prices
        self._output_tokens = output_tokens
        self._max_input_tokens = max_input_tokens
        self._last_cost: float | None = None
        self._failures = 0
        system = system_prompt(stage_names=self.stage_names)
        self._tools = tool_schemas(stage_names=self.stage_names)
        self._cache_key = cache_key_for(system, self._tools)
        self._messages: list[dict] = [{"role": "system", "content": system}]
        self._pending: list[Outcome] = []
        self._imaged: set[str] = set()

    # --- what the driver reads -----------------------------------------------------------------------------------

    @property
    def ai_capped(self) -> bool:
        """True when a spending limit - the shared wave cap or this file's own ceiling - ended the AI's part
        in this file. The file then runs deterministic-only and is marked `ai_capped`."""
        return self.stop_reason in ("wave_cap", "file_ceiling")

    @property
    def messages(self) -> list[dict]:
        return list(self._messages)

    def feedback(self, outcomes: Sequence[Outcome]) -> None:
        """What became of the candidates this proposer made, delivered with the next observation."""
        self._pending.extend(outcomes)

    # --- Generator -----------------------------------------------------------------------------------------------

    def propose(self, state: Any, k: int) -> list[ProposedCandidate]:
        return _run_sync(self._propose(state, k))

    async def _propose(self, state: Any, k: int) -> list[ProposedCandidate]:
        if self.stop_reason is not None or getattr(state.verdict, "clean", False):
            return []                                  # stop a file the moment it is clean
        if self.turns_used >= self.max_turns:
            self._stop("max_turns")
            return []
        sites = self.unfixable.filter(state.sites)
        self._messages.append(self._user_turn(state, sites))
        collected: list[ProposedCandidate] = []
        while len(collected) < k:
            if self.turns_used >= self.max_turns:
                self._stop("max_turns")
                break
            admission = self._admit()
            if admission is None or not admission.ok:
                if admission is not None:
                    assert admission.reason is not None
                    self._stop(admission.reason)
                break
            assert admission.reservation is not None
            try:
                priced = await self._ask(admission.reservation)
            except BaseException:
                self._ledger.release(admission.reservation)      # an unpriced or cancelled call spent nothing
                raise
            reply = priced.reply
            self.turns_used += 1
            if reply.failure_marker:
                # One failed round (the router has already retried it) must not end the AI for the file;
                # only MAX_CONSECUTIVE_FAILURES in a row do. The round returns what it has and the driver
                # asks again next round.
                self._ledger.release(admission.reservation)
                self._failures += 1
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    self._stop("model_failure")
                break
            self._failures = 0
            self._ledger.settle(admission.reservation, file_id=self.file_id, job_id=self.job_id,
                                provider=priced.provider, model=priced.model,
                                input_tokens=reply.input_tokens, cached_input_tokens=reply.cached_input_tokens,
                                output_tokens=reply.output_tokens, cost_usd=priced.cost_usd)
            self._last_cost = priced.cost_usd
            self._messages.append(assistant_turn(reply))
            if not reply.tool_calls:
                break                                   # the model has nothing (more) to propose this round
            room = k - len(collected)
            for call in reply.tool_calls:
                accepted, _over, result = self._handle(call, state, room)
                collected.extend(accepted)
                room -= len(accepted)
                self._messages.append(tool_result_turn(call, _json(result)))
            if collected:
                break                                   # something to evaluate; the next round brings the verdict
        return collected[:k]

    # --- one round trip ------------------------------------------------------------------------------------------

    def _admit(self) -> Admission | None:
        """Reserve this call's estimated cost, or None (and a stop) when the call is too large to make."""
        usd, tokens = estimate_call_usd(self._messages, self._tools, prices=self._prices,
                                        output_tokens=self._output_tokens)
        if tokens > self._max_input_tokens:
            self._stop("input_too_large")
            return None
        if self._last_cost is not None:
            usd = max(usd, self._last_cost * _GROWTH)
        return self._ledger.admit(self.file_id, usd, file_ceiling_usd=self.usd_ceiling)

    def _stop(self, reason: StopReason) -> None:
        """End the AI's part in this file and record why in the ledger, not just on this object."""
        if self.stop_reason == reason:
            return
        self.stop_reason = reason
        self._ledger.mark(self.file_id, stop_reason=reason, ai_capped=self.ai_capped)

    def result(self) -> dict:
        """What the driver records for this file: the AI's spend and why it stopped."""
        return {"file_id": self.file_id, "ai_capped": self.ai_capped, "stop_reason": self.stop_reason,
                "turns": self.turns_used, "spent_usd": self._ledger.file_spent_usd(self.file_id)}

    async def _ask(self, reservation: str) -> ProposeReply:
        call = self._call
        if call is None:
            from meshpipeline.contracts.model_inference import call_repair_v4_propose
            call = call_repair_v4_propose
        return await call(self._messages, self._tools, route=self.route, job_id=self.job_id,
                          user_id=self.user_id, tool_choice="auto", cache_key=self._cache_key)

    def _user_turn(self, state: Any, sites: list[Site]) -> dict:
        body: dict[str, Any] = {"observation": self._observe(state, sites),
                                "budget": {"turns_left": self.max_turns - self.turns_used,
                                           "usd_left_for_this_file": round(
                                               self.usd_ceiling - self._ledger.file_spent_usd(self.file_id), 4)}}
        if self._pending:
            body["previous_candidates"] = [_outcome_json(o) for o in self._pending]
            self._pending = []
        head = ("Round update. Your previous candidates and the current state follow as one JSON document.\n"
                if "previous_candidates" in body else "Current state follows as one JSON document.\n")
        parts: list[dict] = [{"type": "text", "text": head + _json(body)}]
        for s in sites:
            if s.kind in IMAGE_KINDS and s.view_png and s.id not in self._imaged:
                self._imaged.add(s.id)
                parts.append({"type": "text", "text": f"View of site {s.id} ({s.kind}):"})
                parts.append({"type": "image_url", "image_url": {"url": _png_data_url(s.view_png)}})
        return {"role": "user", "content": parts if len(parts) > 1 else parts[0]["text"]}

    # --- tool calls -> structured results --------------------------------------------------------------------------

    def _handle(self, call: ToolCallRequest, state: Any, room: int) -> tuple[list[ProposedCandidate], int, dict]:
        """(accepted candidates, how many valid ones were deferred for want of room, the tool result)."""
        try:
            args = json.loads(call.arguments) if call.arguments.strip() else {}
        except ValueError as exc:
            return [], 0, _failure(f"arguments are not valid JSON ({exc.__class__.__name__}: {exc})")
        if not isinstance(args, dict):
            return [], 0, _failure("arguments must be a JSON object")
        if call.name == PROPOSE:
            return self._propose_tool(args, state, room)
        if call.name == DECLARE_UNFIXABLE:
            return [], 0, self._declare_tool(args, state)
        return [], 0, _failure(f"unknown tool {call.name!r}; the tools are {PROPOSE} and {DECLARE_UNFIXABLE}")

    def _propose_tool(self, args: dict, state: Any, room: int) -> tuple[list[ProposedCandidate], int, dict]:
        items = args.get("candidates")
        extra = sorted(set(args) - {"candidates"})
        if extra:
            return [], 0, _failure(f"unknown argument(s) {extra}; {PROPOSE} takes only 'candidates'")
        if not isinstance(items, list):
            return [], 0, _failure("'candidates' must be a list of candidate objects")
        if not items:
            return [], 0, _failure("'candidates' needs at least one candidate")
        errors: list[dict] = []
        valid: list[ProposedCandidate] = []
        for i, item in enumerate(items):
            problems, candidate = self._check_candidate(item, state)
            if problems:
                errors.append({"index": i, "op": item.get("op") or item.get("run_stage")
                               if isinstance(item, dict) else None, "errors": problems})
            else:
                assert candidate is not None
                valid.append(candidate)
        accepted, deferred = valid[:max(room, 0)], max(len(valid) - max(room, 0), 0)
        result: dict[str, Any] = {"ok": not errors, "accepted": len(accepted), "deferred": deferred,
                                  "errors": errors}
        if accepted:
            result["message"] = ("accepted candidates are being evaluated; their Diffs and verdict deltas "
                                 "arrive with the next round")
        return accepted, deferred, result

    def _check_candidate(self, item: Any, state: Any) -> tuple[list[str], ProposedCandidate | None]:
        if not isinstance(item, dict):
            return ["a candidate must be an object"], None
        allowed = {"op", "run_stage", "targets", "params", "rationale"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            return [f"unknown key(s) {unknown}; a candidate has {sorted(allowed)}"], None
        op, stage = item.get("op"), item.get("run_stage")
        rationale = item.get("rationale", "")
        if not isinstance(rationale, str):
            return ["rationale: expected a string"], None
        if (op is None) == (stage is None):
            return ["give exactly one of 'op' or 'run_stage'"], None
        targets = item.get("targets", [])
        params = item.get("params", {})
        if stage is not None:
            if stage not in self.stage_names:
                return [f"unknown stage {stage!r}; known stages: {list(self.stage_names)}"], None
            if not isinstance(params, dict):
                return ["params: expected an object"], None
            return [], ProposedCandidate("run_stage", [], {"stage": stage, "params": params}, "ai", rationale)
        if not isinstance(op, str):
            return ["op: expected a string"], None
        problems = validate_call(op, targets, params, state.ws)
        if problems:
            return problems, None
        return [], ProposedCandidate(op, list(targets), dict(params), "ai", rationale)

    def _declare_tool(self, args: dict, state: Any) -> dict:
        site_id, reason = args.get("site_id"), args.get("reason")
        if not isinstance(site_id, str) or not isinstance(reason, str):
            return _failure("site_id and reason must both be strings")
        if not reason.strip():
            return _failure("a reason is required: say what bound the fix would break")
        known = {s.id for s in state.sites}
        if site_id not in known:
            return _failure(f"unknown site {site_id!r}; the sites are {sorted(known)}")
        self.unfixable.declare(site_id, reason.strip())
        return {"ok": True, "site_id": site_id,
                "message": "recorded; the site will not be shown again or swept by the deterministic generators"}


def _failure(message: str) -> dict:
    return {"ok": False, "accepted": 0, "errors": [{"index": None, "errors": [message]}]}


# --- the probe: one tool call and one image, before any paid wave ----------------------------------------------------


def _solid_png(rgb: tuple[int, int, int], size: int = 16) -> bytes:
    """A size x size solid-colour PNG, built by hand so the probe needs no imaging library."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


async def probe_route(route: str, *, call: ModelCall | None = None) -> ProbeResult:
    """The smallest possible check that `route` takes images AND calls tools: one solid-red image, one tool
    (`report_colour`), `tool_choice` required. Cents, and it must pass before a wave is paid for - a model with no
    in-repo usage (gpt-6-astra) has unproven tool-use and image support HERE. Never raises: a failure, including
    an exception, is a result."""
    if call is None:
        from meshpipeline.contracts.model_inference import call_repair_v4_propose
        call = call_repair_v4_propose
    tool = ToolSchema("report_colour", "Report the colour of the image.",
                      {"type": "object", "properties": {"colour": {"type": "string"}},
                       "required": ["colour"], "additionalProperties": False}).definition()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a connectivity probe. Answer only by calling the tool."},
        {"role": "user", "content": [
            {"type": "text", "text": "What single colour is this image? Call report_colour with its name."},
            {"type": "image_url", "image_url": {"url": _png_data_url(_solid_png((255, 0, 0)))}}]}]
    try:
        priced = await call(messages, [tool], route=route, job_id="", tool_choice="required",
                            cache_key="")
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        return ProbeResult(False, route, f"{type(exc).__name__}: {exc}")
    reply: ModelRoundResult = priced.reply
    if reply.failure_marker:
        return ProbeResult(False, route, f"provider failure: {reply.failure_marker}")
    calls = [c for c in reply.tool_calls if c.name == "report_colour"]
    if not calls:
        return ProbeResult(False, route, "the model did not call the tool")
    try:
        colour = str(json.loads(calls[0].arguments).get("colour", "")).strip().lower()
    except (ValueError, AttributeError):
        return ProbeResult(False, route, "the tool call's arguments were not a JSON object")
    if "red" not in colour:
        return ProbeResult(False, route, f"the model saw {colour!r}, not red: images are not understood")
    return ProbeResult(True, route, "tool call and image both understood")


async def select_route(routes: Sequence[str], *, call: ModelCall | None = None) -> str:
    """The first route in `routes` (strongest first) that passes `probe_route`. Raises NoUsableRoute, naming each
    failure, if none does - a wave is never started on a guess."""
    failures = []
    for route in routes:
        result = await probe_route(route, call=call)
        if result.ok:
            return route
        failures.append(f"{route}: {result.detail}")
    raise NoUsableRoute("no repair_v4_propose route passed the probe: " + "; ".join(failures))


__all__ = ["AIProposer", "DECLARE_UNFIXABLE", "IMAGE_KINDS", "MAX_CONSECUTIVE_FAILURES", "NoUsableRoute",
           "Outcome", "PROPOSE", "ProbeResult", "ProposedCandidate", "UnfixableRegistry", "cache_key_for",
           "estimate_call_usd", "probe_route", "select_route", "system_prompt", "tool_schemas"]
