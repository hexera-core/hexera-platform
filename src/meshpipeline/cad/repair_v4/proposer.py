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
import re
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

#: the hard per-file model spend ceiling (spec 5.9); no proposer may be built above it
FILE_USD_CEILING = 1.00

#: The AI's part in a file ended because the model could not be reached: MAX_CONSECUTIVE_FAILURES rounds in a row
#: failed after the router's own retries (429, 5xx, timeouts, a throttle slot that never came). Infrastructure, not a
#: verdict: an evaluation row whose AI stopped for this reason is INCONCLUSIVE, never a failed repair.
MODEL_UNAVAILABLE: Literal["model_unavailable"] = "model_unavailable"
StopReason = Literal["wave_cap", "file_ceiling", "max_turns", "model_unavailable", "input_too_large",
                     "no_affordable_turn"]
#: The least output allowance a turn is reserved and requested with when the full REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS
#: does not fit the room left. Measured replies are 112 output tokens median and 263 max (lane D11, 47 turns), so
#: 1,024 is ~4x the largest seen; below it a turn is not worth asking for and the file stops `no_affordable_turn`.
MIN_OUTPUT_TOKENS = 1024
#: Consecutive failed model rounds (the router has already retried each) before the AI is given up on for a file.
MAX_CONSECUTIVE_FAILURES = 3
#: The router's failure marker for a round whose final word was a rate limit (`router._PROPOSE_ROLE:<category>`).
_RATE_LIMITED_MARKER = "repair_v4_propose:rate_limit"

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
    #: the structured reason, when the caller already knows more than the message says (a rolled-back stage knows
    #: WHICH cheap member got worse). None = parse it from `message` (`reason_of`). The message itself is never
    #: enriched: arms.py and triage.py group their tables on it.
    reason: dict | None = None


def lineage_of(state: Any) -> tuple[str, ...]:
    """The state ids from the search start down to `state` (its own last); () for a state that carries none."""
    return tuple(getattr(state, "lineage", ()) or ())


class UnfixableRegistry:
    """Sites the AI has declared unfixable within the fidelity bound. Shared with the deterministic generators
    (construct one and pass it to both) so a declared site is not swept again for nothing.

    A declaration belongs to the LINEAGE it was made in, not to the site id alone: stable ids are minted per
    lineage, so two siblings forked from one parent mint the same id for different new geometry, and a verdict on
    one must not remove the other's site. It is keyed `(state id of the declaring state, site id)` and seen by that
    state and its descendants only (`lineage` is the tuple of state ids from the start to the asking state). A
    caller with no lineage (`()`) declares and reads in one global scope, `""`."""

    def __init__(self) -> None:
        self.reasons: dict[tuple[str, str], str] = {}

    def declare(self, site_id: str, reason: str, lineage: Sequence[str] = ()) -> None:
        self.reasons[(lineage[-1] if lineage else "", site_id)] = reason

    def is_declared(self, site_id: str, lineage: Sequence[str] = ()) -> bool:
        return any((scope, site_id) in self.reasons for scope in ("", *lineage))

    def filter(self, sites: Sequence[Site], lineage: Sequence[str] = ()) -> list[Site]:
        return [s for s in sites if not self.is_declared(s.id, lineage)]


class NoUsableRoute(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    route: str
    detail: str = ""


# --- the static prefix: system prompt and tools ----------------------------------------------------------------


#: What each op DOES, one line each, shown beside its schema. A schema alone gives the model parameter names and
#: bounds but not the action: without these, `make_volume` or `conformalise` is a name to guess at. Every op in the
#: registry must have one (tests/unit/cad/repair_v4/test_proposer_prompt.py), so a new op cannot ship unexplained.
OP_SUMMARIES: dict[str, str] = {
    "conformalise": "make touching/overlapping bodies share faces exactly: `fragment` splits them at each other, "
                    "`cut_by:B<n>` keeps body B<n> and cuts the overlap out of the other (at any depth; the depth is "
                    "measured and reported in the Diff). YOU choose which body yields. Needs 2+ bodies that TOUCH or "
                    "OVERLAP: on bodies already sharing faces, or apart, it changes nothing and declines no_change. "
                    "For overlap sites.",
    "fuse": "merge 2+ bodies into ONE solid (BRepAlgoAPI_Fuse): for a body inside another, two copies of one body, or "
            "halves meant to be one. Changes the body count (reported, not refused). For overlap sites.",
    "fill_loop": "close ONE free (open) loop with one new declared face, of any size; targets are the loop's edges. "
                 "Leaves faces loose: follow with make_volume to rebuild the solid. For open sites.",
    "fix_poles": "ShapeFix_Face wire repair (degenerate edges at a pole, missing seam) on the target faces. For "
                 "pole/periodic/invalid sites.",
    "make_volume": "build solid(s) from the target faces, or from every face not already in a solid when none are "
                   "given. The usual step after fill_loop/sew closes the last opening.",
    "orient_and_close": "fix shell face orientation so every solid has positive volume. For invalid/open sites.",
    "reapproximate": "re-fit the target faces' surfaces as B-splines of at most max_degree within tol_mm.",
    "rebuild_edge_as_intersection": "replace each target edge's 3D curve by the intersection of its two faces "
                                    "(edge drifted off its faces).",
    "rebuild_face": "rebuild ONE face, fixing self-intersecting wires; declined if the loop exceeds the fidelity "
                    "bound.",
    "refit_cluster": "replace 2+ adjacent faces of one shell (one closed outline) by one B-spline face within "
                     "tol_mm.",
    "defeature": "remove the target faces with OCCT's defeaturing (the neighbours are extended over the gap), "
                 "declaring each removal; `groups` says which targets form one feature (a hole, a blend) - a "
                 "feature counts as removed only when all its faces are gone. For small or thin faces where a "
                 "mesher fails, and recognised holes/blends. Never a 180-degree concave round (slot end): OCCT "
                 "cannot remove one.",
    "remove_duplicate_faces": "delete the second face of each coincident pair (declared removal).",
    "remove_small": "remove faces or edges narrower than max_width_mm, declaring each removal. For sliver sites.",
    "sew": "sew loose faces together at the tightest tolerance in tol_ladder that closes them. For open sites "
           "where faces are present but not connected.",
    "split_angle": "split the target faces so no face spans more than max_deg of angle. Moves no geometry.",
    "split_by_proximity": "split each face of each face_pair along iso-curves so a chord across a patch sags less "
                          "than target_ratio of the gap between the pair. Moves no geometry. For proximity sites.",
    "trim_unbounded": "bound the part's infinite (untrimmed) surfaces to its real extent: the part intersected with "
                      "its finite-face box enlarged by pad_fraction of the diagonal. No targets. For unbounded sites.",
    "split_closed": "split closed (periodic) faces - full cylinders, cones, spheres, tori - into open patches. "
                    "Moves no geometry.",
    "split_continuity": "split the target faces at their internal C1 (order 1) or C2 (order 2) breaks. Moves no "
                        "geometry.",
    "tighten_tolerances": "lower inflated edge/vertex/face tolerances to what the geometry is measured to satisfy "
                          "(target tolerance = target_rel x part diagonal); never below the measured deviation.",
    "to_canonical": "snap the target faces' surfaces to exact planes/cylinders/cones/spheres/tori within tol_mm.",
    "unify_same_domain": "merge coplanar/co-surface faces and collinear edges (scope all, or only the targets), "
                         "keeping the `keep` edges. The inverse of a split.",
}


def _catalogue(stage_names: Sequence[str]) -> str:
    lines = []
    for name in sorted(REGISTRY):
        schema = json.dumps(REGISTRY[name].params_schema, sort_keys=True, separators=(',', ':'))
        lines.append(f"- {name}: {OP_SUMMARIES.get(name, '(no summary)')}\n  params: {schema}")
    if stage_names:
        lines.append("- run_stage: a whole deterministic healing stage. It is one candidate, but a stage body is a "
                     "CHAIN of ops and the op budget is charged EVERY op it applies (so `shells` on a part with no "
                     "solid is sew + make_volume, two ops), whether the stage is kept or rolled back. Give "
                     "{\"run_stage\": <name>, \"params\": {...}} with only that stage's keys; its defaults:")
        for name in sorted(stage_names):
            keys = _stage_defaults(name)
            shown = "(unknown stage)" if keys is None else json.dumps(keys, sort_keys=True, separators=(',', ':'))
            lines.append(f"  {name}: {shown}")
    return "\n".join(lines)


def _stage_defaults(name: str) -> dict | None:
    """The parameters a stage reads, with their defaults (`stages.DEFAULTS`), or None for a stage it does not know."""
    from meshpipeline.cad.repair_v4.stages import DEFAULTS
    return DEFAULTS.get(name)


#: Every hard code layer 1 can raise, as the model should read it. Keyed by `verdict.valid.HARD_CODES` and checked
#: against it (tests/unit/cad/repair_v4/test_proposer.py), so a new hard code cannot reach the model unexplained.
HARD_CODE_TEXT: dict[str, str] = {
    "brep_invalid": "BRepCheck rejects a sub-shape, or the file does not read",
    "open_shell": "a solid or shell has free (unshared) edges",
    "non_manifold": "an edge is shared by more than two faces",
    "non_positive_volume": "a solid has zero or negative volume (inside out)",
    "self_intersection": "a solid's faces intersect each other",
    "body_overlap": "two bodies overlap in volume instead of sharing faces exactly (conformal contact)",
    "units_changed": "the length unit differs from the input's, or does not survive the round trip",
    "round_trip_invalid": "the written STEP does not re-read valid",
    "no_solids": "the part holds no solid at all (only faces or shells)",
    "unbounded_geometry": "infinite extent: an untrimmed surface; trim_unbounded bounds it to the part's real extent",
}


def _clean_definition() -> str:
    """WHAT CLEAN MEANS, written from the checker's own constants so the model is told what the checker enforces and
    nothing else: the hard and soft codes of layer 1 and the SICN floor and member time limit of layer 2. Since the
    owner decision of 2026-11-24 clean is VALID + MESHABLE; faithfulness is measured and reported only."""
    from meshpipeline.cad.repair_v4.verdict.assemble import MEMBER_TIMEOUT_S
    from meshpipeline.cad.repair_v4.verdict.mesh_child import MIN_SICN
    from meshpipeline.cad.repair_v4.verdict.valid import HARD_CODES, SOFT_CODES

    hard = "\n".join(f"   - {code}: {HARD_CODE_TEXT.get(code, '(undescribed)')}" for code in HARD_CODES)
    return f"""WHAT CLEAN MEANS. A file is clean when it is VALID and MESHABLE - the two layers below - and the checker, not you
and not a rule of thumb, decides.
1. Valid: it fails on any of these HARD findings (the codes you will see in `valid.hard`):
{hard}
   SOFT findings ({", ".join(SOFT_CODES)}) are reported but do NOT fail this layer: fix them only where a
   mesher fails because of them. Findings name solids and shells by position (S1, H1); ops take the stable ids.
2. Meshable: it meshes in every mesher on a fixed panel, each within {MEMBER_TIMEOUT_S:.0f} s, with no tolerant
   fallback and no hints sidecar, and the worst element quality (min SICN) is at least {MIN_SICN:g}.
FAITHFULNESS IS REPORTED, NOT REQUIRED. How far the surface moved, area, volume and body-count changes are measured
on every candidate and shown to you, but no limit applies to them: a bigger change that makes more meshers pass
ranks above a smaller one that does not. Between candidates equal on validity, meshers passing and element quality,
the one that moved the part less ranks first - so prefer the smallest change that works, and remove or fill as much
as the mesh needs."""


#: The one paragraph a proposer with recipe memory adds to its system turn (CAD_REPAIR_V4_RECIPES). Absent when
#: the memory is off, so the flag-off prompt - and its cache key - is exactly what it was.
RECIPES_PARAGRAPH = """
WHAT WORKED BEFORE. When a turn carries `what_worked_on_similar_sites`, it lists, per site, op sequences that took
sites with a similar failure signature to clean on other files. Targets there are ROLES, not ids: `site_faces`,
`site_solids`, `site_entities`, `free_loop[n]` (the n-th smallest loop of THIS site), `none`; millimetre parameters
are fractions of the diagonal (`{"rel": ...}`). Map them onto this site's ids and this part's size yourself. It is
advice from other parts, not an instruction: the checker still judges everything you propose.
"""


def system_prompt(*, stage_names: Sequence[str] = (), recipes: bool = False) -> str:
    """The static system turn. Byte-identical for every file and every round with the same stage list and recipe
    setting, so the provider's prompt cache serves it (it is the first thing in the request, then the tool schemas)."""
    return f"""You repair CAD B-rep files (STEP) so that customers can mesh them in ANY mesher. You work one file at a
time, in one conversation: you see the failure sites of the current best state, propose parametric repair
operations on them, and are told what each of your candidates did to the part.

{_clean_definition()}

WHAT YOU OWN. The checker finds sites; you supply the judgement deterministic sweeps cannot:
- overlap intent: are two overlapping bodies a duplicate, a fit, or one meant to be subtracted from the other?
- hole versus missing face: is an open loop a real hole to keep, or a face that should exist?
- fix choice: when several fixes would work, prefer the one that moves the part least - but a larger change that
  makes the part meshable beats a small one that does not;
- ordering and recovery: operations interact; a split can renumber faces, a sew can undo a split;
- mapping a mesher's error to the entity that causes it;
- when to stop: if no operation can fix a site, call {DECLARE_UNFIXABLE} with the site id and the reason instead
  of spending turns on it. Declared sites are not retried.

HOW YOU ACT. Reply only with tool calls.
- {PROPOSE}(candidates): one to a few candidates, each {{op | run_stage, targets, params, rationale}}. `targets` are
  stable entity ids such as "F41" (face), "E7" (edge), "B2" (solid body); an id keeps its name across ops that only
  trim or heal it, and an id that an op removed or replaced is dead - use the ids in the latest observation.
  Candidates are evaluated for you in parallel; you will see each one's Diff and the change in the verdict next
  round. A candidate that makes the verdict worse is simply discarded; you lose a turn, never the file.
- {DECLARE_UNFIXABLE}(site_id, reason).
A malformed call costs nothing but a turn: you will get a structured error naming each problem (an unknown op, a
parameter out of bounds, a target that no longer exists) and may call again. The turns, the dollars spent on you
and the wall-clock are all budgeted and shown to you; stop when the file is clean or nothing more can be tried.
{RECIPES_PARAGRAPH if recipes else ""}
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
        DECLARE_UNFIXABLE, "Declare a site that no operation can fix; it will not be retried.",
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


_CODE = re.compile(r"^([a-z][a-z0-9_]*): (.*)$", re.S)
_STAGE_ROLLED = re.compile(r"^stage '([^']+)' was rolled back: (.*)$", re.S)
_STAGE_NOTHING = re.compile(r"^stage '([^']+)' changed nothing(?:: (.*))?$", re.S)
_STEP = re.compile(r"^([a-z][a-z0-9_]*): (declined|failed|error): (.*)$", re.S)
_FAIL_STATUSES = ("error", "timeout", "crashed")


def reason_of(status: str, message: str) -> dict | None:
    """Why a candidate was not applied, in ONE stable shape for the proposer (and the re-prompt):
    `{"status", "code", "detail", "stage", "op"}`.

    `status` is the outcome ("declined" / "failed" / "invalid"); `code` a snake_case reason: the op's own
    leading code when it gave one (`no_change`, `invalidated`, `over_bound`, `invalid_params`, `no_intersection`,
    ...), `rolled_back` / `no_change` for a stage, the pool status (`error` / `timeout` / `crashed`) for a failure,
    `invalid_call` for a call that failed validation, and plain `declined` for an uncoded decline. `stage` and `op`
    name the stage and its inner op when a `run_stage` carried the reason. `detail` is the rest, verbatim. None for an
    applied candidate."""
    if status == "applied":
        return None
    stage = op = None
    text = message.strip()
    if status == "invalid":
        return {"status": status, "code": "invalid_call", "detail": text, "stage": None, "op": None}
    if m := _STAGE_ROLLED.match(text):
        return {"status": status, "code": "rolled_back", "detail": m.group(2), "stage": m.group(1), "op": None}
    if m := _STAGE_NOTHING.match(text):
        stage, text = m.group(1), m.group(2) or ""
        if not text:
            return {"status": status, "code": "no_change", "detail": "", "stage": stage, "op": None}
        if step := _STEP.match(text):
            op, kind, text = step.group(1), step.group(2), step.group(3)
            if kind == "error":
                return {"status": status, "code": "error", "detail": text, "stage": stage, "op": op}
    if (m := _CODE.match(text)) is not None:
        code, detail = m.group(1), m.group(2)
        if status == "failed" or code in _FAIL_STATUSES:
            return {"status": status, "code": code if code in _FAIL_STATUSES else "error", "detail": detail,
                    "stage": stage, "op": op}
        return {"status": status, "code": code, "detail": detail, "stage": stage, "op": op}
    return {"status": status, "code": "declined" if status == "declined" else "error", "detail": text,
            "stage": stage, "op": op}


def _outcome_json(o: Outcome) -> dict:
    c = o.candidate
    return {"op": c.op, "targets": c.targets, "params": c.params, "rationale": c.rationale,
            "status": o.status, "message": o.message,
            "reason": o.reason if o.reason is not None else reason_of(o.status, o.message),
            "diff": _diff_json(o.diff), "delta": o.delta}


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
                 max_input_tokens: int = MAX_INPUT_TOKENS, throttle: Any = None, recipes: Any = None) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if not (0 < usd_ceiling <= FILE_USD_CEILING):  # a chained comparison is False for inf and nan too
            raise ValueError(
                f"usd_ceiling must be finite and in (0, {FILE_USD_CEILING}] dollars per file, got {usd_ceiling!r}"
            )
        if not isinstance(ledger, WaveLedger):
            raise TypeError("a WaveLedger is required: the cap is not optional")
        if ledger.provenance not in ("shard", "test", "file"):
            raise TypeError(
                "the ledger was not built with WaveLedger.for_shard (or for_file); refusing to spend against it")
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
        #: the output allowance THIS turn was reserved with; below `output_tokens` it is also what the call requests
        self._turn_output_tokens = output_tokens
        self._last_tokens = 0
        #: the shard's `model_throttle.WaveThrottle` (None: unthrottled, e.g. one file on the engine path)
        self._throttle = throttle
        #: a `recipes.RecipeBook`: each turn shows, per open site, the id-free sequences that took similar dev
        #: sites to clean. Advice, not instruction: the search judges whatever the model proposes from it.
        self._recipes = recipes
        self._failures = 0
        #: rounds the model answered / rounds that failed after the router's retries (or never got a throttle slot)
        self.answered_rounds = 0
        self.failed_rounds = 0
        self.truncated_rounds = 0
        #: for the eval's failure triage: tool calls answered `ok: false`, the candidates they refused, and the
        #: `declare_unfixable` calls accepted from THIS proposer (the registry may be shared)
        self.tool_errors = 0
        self.candidate_errors = 0
        self.declared_unfixable = 0
        system = system_prompt(stage_names=self.stage_names, recipes=recipes is not None)
        self._tools = tool_schemas(stage_names=self.stage_names)
        self._cache_key = cache_key_for(system, self._tools)
        self._messages: list[dict] = [{"role": "system", "content": system}]
        self._pending: list[Outcome] = []
        #: (state id where the picture was shown, site id): a site id names different geometry on different
        #: branches, so "already shown" is per lineage, like the unfixable declarations
        self._imaged: set[tuple[str, str]] = set()

    # --- what the driver reads -----------------------------------------------------------------------------------

    @property
    def ai_capped(self) -> bool:
        """True when a spending limit - the shared wave cap or this file's own ceiling - ended the AI's part
        in this file. The file then runs deterministic-only and is marked `ai_capped`."""
        return self.stop_reason in ("wave_cap", "file_ceiling", "no_affordable_turn")

    @property
    def model_unavailable(self) -> bool:
        """True when the model was never really there for this file: the AI was stopped as `model_unavailable`, or
        it tried and not one round was answered (the search can end before the third failure stops it). Such a
        file ran deterministic-only, so an evaluation must not charge its outcome to the AI arm."""
        return self.stop_reason == MODEL_UNAVAILABLE or (self.failed_rounds > 0 and self.answered_rounds == 0)

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
        sites = self.unfixable.filter(state.sites, lineage_of(state))
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
            if self._throttle is not None and not self._throttle.acquire(self._last_tokens):
                # the shard's slice of the wave's rate limit never came free: a failed round, nothing was sent
                self._ledger.release(admission.reservation)
                self._failed_round()
                break
            try:
                priced = await self._ask(admission.reservation)
            except BaseException:
                self._ledger.release(admission.reservation)      # an unpriced or cancelled call spent nothing
                if self._throttle is not None:
                    self._throttle.release()
                raise
            reply = priced.reply
            if self._throttle is not None:
                self._throttle.release(rate_limited=reply.failure_marker == _RATE_LIMITED_MARKER)
            self.turns_used += 1
            if reply.failure_marker:
                # One failed round (the router has already retried it) must not end the AI for the file;
                # only MAX_CONSECUTIVE_FAILURES in a row do. The round returns what it has and the driver
                # asks again next round. A failed round was not billed, so its reservation is RELEASED, not
                # settled: the ledger holds billed usage only.
                self._ledger.release(admission.reservation)
                self._failed_round()
                break
            self._failures = 0
            self.answered_rounds += 1
            if reply.finish_reason == "length" and not reply.tool_calls:
                self.truncated_rounds += 1     # the output limit was spent on reasoning: a signal to raise it
            self._ledger.settle(admission.reservation, file_id=self.file_id, job_id=self.job_id,
                                provider=priced.provider, model=priced.model,
                                input_tokens=reply.input_tokens, cached_input_tokens=reply.cached_input_tokens,
                                output_tokens=reply.output_tokens, cost_usd=priced.cost_usd,
                                cache_write_tokens=reply.cache_write_tokens,
                                cache_write_30m_tokens=reply.cache_write_30m_tokens)
            self._last_cost = priced.cost_usd
            self._messages.append(assistant_turn(reply))
            if not reply.tool_calls:
                break                                   # the model has nothing (more) to propose this round
            room = k - len(collected)
            for call in reply.tool_calls:
                accepted, _over, result = self._handle(call, state, room)
                if result.get("ok") is False:
                    self.tool_errors += 1
                    self.candidate_errors += sum(1 for e in result.get("errors", []) if e.get("index") is not None)
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
        self._last_tokens = tokens
        self._turn_output_tokens = self._output_tokens
        admission = self._ledger.admit(self.file_id, usd, file_ceiling_usd=self.usd_ceiling)
        if admission.ok:
            return admission
        # The full allowance does not fit. Most of a reservation is the OUTPUT allowance (4,096 tokens ~ $0.20 on
        # astra) while replies are ~100-260 tokens: reserve - and request - what the room left can pay for, so a
        # file gets its turn whenever any useful turn is affordable (lane D9: a file got ZERO turns behind a
        # full-allowance reservation that did not fit its shard's remaining slice).
        p_in, p_out, _cached = self._prices
        room = self._ledger.headroom_usd(self.file_id, file_ceiling_usd=self.usd_ceiling)
        out = min(self._output_tokens, int((room - tokens * p_in / 1e6) * 1e6 / p_out)) if p_out > 0 else 0
        if out >= MIN_OUTPUT_TOKENS:
            fitted = (tokens * p_in + out * p_out) / 1e6
            if self._last_cost is not None:
                fitted = max(fitted, self._last_cost * _GROWTH)
            again = self._ledger.admit(self.file_id, fitted, file_ceiling_usd=self.usd_ceiling)
            if again.ok:
                self._turn_output_tokens = out
                return again
        if self.turns_used == 0:
            # not one turn is affordable: say so, distinctly - never a silent zero behind wave_cap / file_ceiling
            self._stop("no_affordable_turn")
            return None
        return admission

    def _failed_round(self) -> bool:
        """Count a failed model round; True (and the stop recorded) when it was the last one allowed."""
        self._failures += 1
        self.failed_rounds += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            self._stop(MODEL_UNAVAILABLE)
            return True
        return False

    def _stop(self, reason: StopReason) -> None:
        """End the AI's part in this file and record why in the ledger, not just on this object."""
        if self.stop_reason == reason:
            return
        self.stop_reason = reason
        self._ledger.mark(self.file_id, stop_reason=reason, ai_capped=self.ai_capped)

    def result(self) -> dict:
        """What the driver records for this file: the AI's spend and why it stopped."""
        return {"file_id": self.file_id, "ai_capped": self.ai_capped, "stop_reason": self.stop_reason,
                "turns": self.turns_used, "truncated_rounds": self.truncated_rounds,
                "answered_rounds": self.answered_rounds, "failed_rounds": self.failed_rounds,
                "model_unavailable": self.model_unavailable, "spent_usd": self._ledger.file_spent_usd(self.file_id),
                "tool_errors": self.tool_errors, "candidate_errors": self.candidate_errors,
                "declared_unfixable": self.declared_unfixable}

    async def _ask(self, reservation: str) -> ProposeReply:
        call = self._call
        if call is None:
            from meshpipeline.contracts.model_inference import call_repair_v4_propose
            call = call_repair_v4_propose
        extra = {} if self._turn_output_tokens >= self._output_tokens else {"max_output_tokens": self._turn_output_tokens}
        return await call(self._messages, self._tools, route=self.route, job_id=self.job_id,
                          user_id=self.user_id, tool_choice="auto", cache_key=self._cache_key, **extra)

    def _user_turn(self, state: Any, sites: list[Site]) -> dict:
        body: dict[str, Any] = {"observation": self._observe(state, sites),
                                "budget": {"turns_left": self.max_turns - self.turns_used,
                                           "usd_left_for_this_file": round(
                                               self.usd_ceiling - self._ledger.file_spent_usd(self.file_id), 4)}}
        if self._recipes is not None and len(self._recipes):
            advice = self._recipes.advice(sites)
            if advice:
                body["what_worked_on_similar_sites"] = advice
        if self._pending:
            body["previous_candidates"] = [_outcome_json(o) for o in self._pending]
            self._pending = []
        head = ("Round update. Your previous candidates and the current state follow as one JSON document.\n"
                if "previous_candidates" in body else "Current state follows as one JSON document.\n")
        parts: list[dict] = [{"type": "text", "text": head + _json(body)}]
        lineage = lineage_of(state)
        for s in sites:
            if (s.kind in IMAGE_KINDS and s.view_png
                    and not any((scope, s.id) in self._imaged for scope in ("", *lineage))):
                self._imaged.add((lineage[-1] if lineage else "", s.id))
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
            known = _stage_defaults(stage)
            # a stage merges params over its defaults, so a misspelt key would be silently ignored: refuse it here
            unknown_keys = sorted(set(params) - set(known)) if known is not None else []
            if unknown_keys:
                return [f"stage {stage!r} has no parameter(s) {unknown_keys}; its parameters: {sorted(known or {})}"], None
            if targets not in ([], None):
                return [f"run_stage takes no targets (give ids through the stage's own params); got {targets!r}"], None
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
        self.unfixable.declare(site_id, reason.strip(), lineage_of(state))
        self.declared_unfixable += 1
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


#: How many probe calls a route gets before it fails: a WRONG ANSWER (an out-of-family colour, or no tool call) is
#: asked once more, because snapshot v1's dev6 shard was refused on a single answer ("salmon") that an unchanged
#: retry passed. A provider failure or an exception is not retried here (the router already retried the call).
PROBE_ATTEMPTS = 2
#: The probe swatch's side in pixels: large enough that the model sees a solid colour, not a smudge.
PROBE_IMAGE_PX = 64
#: Colour NAMES in the red hue family. The probe asks whether the image is SEEN, not whether the model names pure
#: #FF0000 the way we would: "salmon", "crimson" and "coral" all mean it saw a red swatch. Other hues (orange,
#: yellow, green, blue, pink...) and anything that is not a colour fail.
RED_FAMILY = frozenset({
    "red", "reddish", "scarlet", "crimson", "vermilion", "vermillion", "ruby", "cherry", "carmine", "maroon",
    "salmon", "coral", "tomato", "brick", "burgundy", "rose", "rosso", "rouge", "cardinal", "cerise", "garnet",
})


def _is_red_family(answer: str) -> bool:
    import re

    return any(token in RED_FAMILY or token.startswith("red") for token in re.findall(r"[a-z]+", answer.lower()))


async def probe_route(route: str, *, call: ModelCall | None = None) -> ProbeResult:
    """The smallest possible check that `route` takes images AND calls tools: one solid-red image, one tool
    (`report_colour`), `tool_choice` required. Cents, and it must pass before a wave is paid for - a model with no
    in-repo usage (gpt-6-astra) has unproven tool-use and image support HERE. Never raises: a failure, including
    an exception, is a result.

    It passes on any colour name in the red hue family (`RED_FAMILY`), and a wrong answer gets one more call
    (`PROBE_ATTEMPTS`). Every call goes through `call`, so on a wave shard each one is metered
    (`model_route.metered_call`)."""
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
            {"type": "image_url",
             "image_url": {"url": _png_data_url(_solid_png((255, 0, 0), PROBE_IMAGE_PX))}}]}]
    last = ProbeResult(False, route, "no probe call was made")
    for _attempt in range(PROBE_ATTEMPTS):
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
            last = ProbeResult(False, route, "the model did not call the tool")
            continue
        try:
            colour = str(json.loads(calls[0].arguments).get("colour", "")).strip()
        except (ValueError, AttributeError):
            last = ProbeResult(False, route, "the tool call's arguments were not a JSON object")
            continue
        if _is_red_family(colour):
            return ProbeResult(True, route, f"tool call and image both understood (saw {colour!r})")
        last = ProbeResult(False, route, f"the model saw {colour!r}, not red: images are not understood")
    return last


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


__all__ = ["AIProposer", "DECLARE_UNFIXABLE", "IMAGE_KINDS", "MAX_CONSECUTIVE_FAILURES", "MODEL_UNAVAILABLE",
           "PROBE_ATTEMPTS", "RED_FAMILY",
           "NoUsableRoute",
           "Outcome", "PROPOSE", "ProbeResult", "ProposedCandidate", "UnfixableRegistry", "cache_key_for",
           "estimate_call_usd", "lineage_of", "probe_route", "select_route", "system_prompt", "tool_schemas"]
