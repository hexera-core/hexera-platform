# Responsibility: Author the next target package for a region by ASKING A MODEL, through the same
#                 `author` seam the deterministic rules author fills, and refuse every reply that
#                 is not a bounded, catalogue-only claim about the region it was asked about.
# Owns: the model author's prompt, the reply schema and its validation, the per-region call
#       budget, the shadow composite (rules drive, model recorded), driver selection by posture,
#       and the record of every call (observation size, clipped reply, outcome, refusal reason).
# Also owns, as THE WIRE AND THE VALIDATION A SECOND AUTHOR SHARES: `converse`/`converse_sync`
#       (one bounded call carrying a whole conversation, through the worker-thread bridge),
#       `RegionContext`, `PackageGuards`, `Refusal`, `plan_entry`, `validate_package`,
#       `validate_verdict` and `rim_guard`. The private names (`_ask*`, `_Refused`, `_Context`,
#       `_Guards`, `_plan_item`, `_package`, `_verdict`) stay as this author's own bindings of
#       them, so the surface moved and the behaviour did not.
# Boundaries: it decides WHAT TO PROPOSE and nothing else, exactly like rules_author. It applies
#             nothing, measures nothing and judges nothing - the session executes the package and
#             a separate measurement judges the claim. A model reply is DATA: it can never waive a
#             cap, never carry `answered_held`, never aim at a held (reserved) code, never fill a
#             paired rim, and never name a tolerance (the loop owns that).
# Collaborates with: cad/repair/observe.py (what the model sees), contracts/model_inference.py
#                    (the one bounded model call), cad/repair/catalogue_words.py (the catalogue in
#                    words), cad/repair/rules_author.py
#                    (the guards it mirrors and the author shadow composes), cad/repair/package.py
#                    (the package it builds), cad/repair/region_loop.py (which calls it),
#                    cad/repair/conservative.py (which selects it), settings/cad_repair.py.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import observe
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.catalogue_words import catalogue_text
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.operations import FILL_OPS
from meshpipeline.cad.repair.operations import catalogue as op_catalogue
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.plan import (
    RunHistory,
    held_fingerprints,
    paired_rim_fingerprints,
    refuse_paired_rims,
)
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import LABEL as RULES_LABEL
from meshpipeline.cad.repair.rules_author import _keep_refusals, rules_author
from meshpipeline.cad.repair.session import PackageVerdict

if TYPE_CHECKING:  # the tool types are needed for annotations only; cad -> contracts stays lazy
    from meshpipeline.contracts.model_inference import ModelReply, ToolSchema

logger = logging.getLogger(__name__)

#: What this author reports itself as, beside `regions-rules-v1`, so a comparison can attribute a
#: run to the model portion of the repair surface (spec decision 6).
LABEL = "regions-model-v1"

Author = Callable[[Region, tuple, tuple[PackageVerdict, ...]], TargetPackage | None]

#: The reply as recorded: clipped, because a report carries every call.
REPLY_MAX = 600
#: Intent and reason text kept from a reply.
INTENT_MAX = 200
#: Seconds past the model call's own timeout before the bridge abandons the worker thread. The
#: call's `wait_for` is the real bound; this only catches a call that will not honour it. Also the
#: margin a SHADOW call needs left on the run's clock beyond its own timeout.
_GRACE_S = 5.0
#: The trace label every call on this route carries unless the caller names its own (an
#: investigator passes "RepairInvestigator", so its calls are attributable in the router's traces).
TRACE_NAME = "RepairReview"

AUTHOR_SYSTEM = (
    "you propose a bounded repair for ONE region of a CAD part that will not mesh, or say the "
    "geometry is sound. you are shown the region's defects (each with a fingerprint), its faces, "
    "every earlier attempt on it and why it failed, and the caps a repair must stay inside. you "
    "may only use these operations:\n"
    "{catalogue}\n"
    "reply with ONE JSON object and nothing else, in exactly one of three forms:\n"
    '1. {{"package": {{"intent": "<one sentence: what this repair is for>", "plan": [{{"op": '
    '"<operation>", "fingerprint": "<a defect fingerprint in this region>"}}], "cleared": '
    '["<the defect fingerprints this plan will make disappear>"], "unchanged": ["<fingerprints '
    'you promise it will not touch>"], "max_area_delta_mm2": <number>, "max_volume_delta_mm3": '
    "<number>}}}}\n"
    '2. {{"verdict": {{"mesher_problem": ["<defect fingerprints>"], "reason": "<why the '
    'geometry is sound and the mesher is at fault>"}}}}\n'
    '3. {{"pass": true}}\n'
    "the claim is checked by measurement after the plan runs: a defect in cleared that remains, "
    "a defect in unchanged that changes, or area or volume moving past your numbers reverts the "
    "whole plan. stay inside the caps you are shown. never repeat an attempt that already "
    "failed. never invent an operation or a fingerprint. if you are unsure, reply "
    '{{"pass": true}}.')


def system_prompt() -> str:
    """The system prompt for the deployment's ACTIVE catalogue (flag-aware, read per call)."""
    return AUTHOR_SYSTEM.format(catalogue=catalogue_text())


def posture() -> str:
    """The model-author posture as the settings hold it now; anything but shadow/live is off."""
    value = str(getattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off") or "off")
    return value if value in ("shadow", "live") else "off"


def _configured() -> bool:
    """Is a model router configured? No router -> no model, fail closed."""
    from meshpipeline.contracts import model_inference
    return model_inference._router is not None


def _parse_object(text: str) -> dict | None:
    """The reply's JSON object. COPIED from `agents/repair_review/_wire.parse_object` - cad may not
    import agents, and ten lines are cheaper than a new shared layer."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def converse(messages: list[dict], *, job_id: str, timeout_s: float,
                   name: str = TRACE_NAME) -> tuple[str, dict | None]:
    """ONE bounded model call carrying a WHOLE conversation; its raw text and its JSON object.

    The same call `agents/repair_review/_wire.ask` makes, made through `contracts` directly
    (cad -> contracts is allowed; cad -> agents is not). The route takes a full `Conversation`, so
    a multi-turn author (an investigator's tool loop) is this same call with a longer list - which
    is why the TEXT comes back too: a tool loop has to append the assistant turn it just got.

    TOTAL: a raised call, a timeout and `ok=False` are all `("", None)`, never a raise. A reply
    with no JSON object keeps its text and parses to None, so a caller can record what was said.
    """
    from meshpipeline.contracts.model_inference import call_repair_review_model
    try:
        result = await asyncio.wait_for(
            call_repair_review_model(list(messages), job_id=job_id, name=name), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - total: a failed call is no proposal, never a raise
        logger.warning("model author call failed: %s", type(exc).__name__)
        return "", None
    if not getattr(result, "ok", True):
        return "", None
    text = getattr(result, "assistant_text", "") or ""
    return text, _parse_object(text)


async def _ask(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """`converse` as the two-message call THIS author makes; the reply's JSON object, or None.

    Looked up as `_ask` AT CALL TIME by `_ask_sync`, so a test fakes the author's model call where
    this module looks it up.
    """
    _, reply = await converse([{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                              job_id=job_id, timeout_s=timeout_s)
    return reply


def _bounded_in_thread(start: Callable, *, timeout_s: float, failure):
    """Run `start()` - a coroutine factory - with `asyncio.run` in a WORKER THREAD.

    The region loop is synchronous and itself runs off the event loop (`asyncio.to_thread` in the
    attempt node), so the model call gets a loop of its own in a thread of its own - which also
    works if a caller ever invokes the loop from a thread that already runs one. Total: any
    failure, and a thread that outlives its timeout, is `failure`. A DAEMON thread, so one
    abandoned past its timeout never holds the process open at exit.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["reply"] = asyncio.run(start())
        except BaseException as exc:  # noqa: BLE001 - total: reported below, never raised
            box["error"] = exc

    # THE CALLER'S CONTEXT CROSSES THE THREAD (a Thread starts with an empty one), so a call made
    # under a tournament's telemetry scope is still labelled with it.
    import contextvars

    context = contextvars.copy_context()
    worker = threading.Thread(target=lambda: context.run(run), name="model-author", daemon=True)
    worker.start()
    worker.join(timeout_s + _GRACE_S)
    if worker.is_alive():
        logger.warning("model author call outlived its %.1fs timeout; abandoned", timeout_s)
        return failure
    if "error" in box:
        logger.warning("model author call failed: %s", type(box["error"]).__name__)
        return failure
    return box.get("reply", failure)


def converse_sync(messages: list[dict], *, job_id: str, timeout_s: float,
                  name: str = TRACE_NAME) -> tuple[str, dict | None]:
    """`converse` from synchronous code, through the worker-thread bridge. Total: `("", None)`."""
    out = _bounded_in_thread(
        lambda: converse(list(messages), job_id=job_id, timeout_s=timeout_s, name=name),
        timeout_s=timeout_s, failure=("", None))
    if not (isinstance(out, tuple) and len(out) == 2):
        return "", None
    text, reply = out
    return (text if isinstance(text, str) else ""), (reply if isinstance(reply, dict) else None)


async def converse_tools(messages: list[dict], *, job_id: str, timeout_s: float,
                         tools: Sequence[ToolSchema], name: str = TRACE_NAME,
                         user_id: str = "") -> tuple[ModelReply | None, bool]:
    """ONE bounded model call carrying a whole conversation AND native tool schemas.

    `(reply, tools_unsupported)`. A SIBLING of `converse`, never a replacement: `converse` (and
    `_ask` under it) is the seam the one-shot author's suites fake, and it stays exactly as it was
    - asked with the three arguments it always was, with no tools constructed anywhere near it.

    `ToolsUnsupported` IS A CAPABILITY ANSWER, NOT A FAILURE: `(None, True)` tells the caller to
    use the JSON-in-text protocol instead, and it must not be charged against the model-call
    budget, because no round happened. Every other failure - a raise, a timeout, `ok=False` - is
    `(None, False)`, the same total contract `converse` keeps, and IS charged.

    A reply may carry MANY tool calls (`reply.tool_calls`), so a caller's tool budget counts
    `len(reply.tool_calls)` rather than rounds. The caller threads the round back with
    `assistant_turn(reply)` plus one `tool_result_turn` per call, and asks again with the longer
    list.
    """
    from meshpipeline.contracts.model_inference import ToolsUnsupported, call_repair_review_model
    try:
        result = await asyncio.wait_for(
            call_repair_review_model(list(messages), job_id=job_id, name=name,
                                     tools=list(tools), user_id=user_id), timeout=timeout_s)
    except ToolsUnsupported:
        logger.info("the repair-review route cannot call tools natively; falling back")
        return None, True
    except Exception as exc:  # noqa: BLE001 - total: a failed round is no proposal, never a raise
        logger.warning("model author tool call failed: %s", type(exc).__name__)
        return None, False
    if not getattr(result, "ok", True):
        return None, False
    return result, False


def converse_tools_sync(messages: list[dict], *, job_id: str, timeout_s: float,
                        tools: Sequence[ToolSchema], name: str = TRACE_NAME,
                        user_id: str = "") -> tuple[ModelReply | None, bool]:
    """`converse_tools` from synchronous code, through the same worker-thread bridge.

    Total: `(None, False)`. An abandoned thread is a charged failure, not a capability answer -
    the router may well support tools; this call simply never came back.
    """
    out = _bounded_in_thread(
        lambda: converse_tools(list(messages), job_id=job_id, timeout_s=timeout_s,
                               tools=tools, name=name, user_id=user_id),
        timeout_s=timeout_s, failure=(None, False))
    if not (isinstance(out, tuple) and len(out) == 2):
        return None, False
    reply, unsupported = out
    return reply, bool(unsupported)


def _ask_sync(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """THIS author's two-message call from synchronous code. Total: None.

    Bridged through `_ask` rather than through `converse_sync`, so `_ask` stays the ONE seam a test
    fakes for the whole two-message path.
    """
    reply = _bounded_in_thread(lambda: _ask(system, user, job_id=job_id, timeout_s=timeout_s),
                               timeout_s=timeout_s, failure=None)
    return reply if isinstance(reply, dict) else None


def _clip_reply(reply: object) -> str | None:
    if reply is None:
        return None
    try:
        text = json.dumps(reply, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(reply)
    return observe.clip_text(text, REPLY_MAX)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(dict.fromkeys(value))


class Refusal(ValueError):
    """A reply that is not a bounded, catalogue-only claim; the message is the reason.

    `code` and `defect` are set for a PRECONDITION refusal (a named reason the run's report counts,
    in the shape `RunHistory.refuse` records); every other refusal carries the message alone."""

    def __init__(self, message: str, *, code: str = "", defect: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.defect = defect


#: The private name this module was written with, kept so every existing caller and test reads the
#: same surface. The public name is what a SECOND author (the investigator) consumes.
_Refused = Refusal


#: What each stage-gated operation's `targets_for` requires of the defect's `measurements["stage"]`.
#: KEPT IN LOCKSTEP with operations.py (SplitClosedFaces, RebuildFace: 2d; FragmentBodies: 3d), and
#: THE LOCKSTEP TEST (test_the_stage_map_matches_targets_for) is what keeps that true: refusing here
#: is only "the same outcome, cheaper" while this map is NOT STRICTER than `targets_for`; a stricter
#: map silently loses a repair the session would have accepted. A looser map is safe (the session
#: declines). DATA: `mesh_probe._face_defect` / `_unlocated` set `measurements["stage"]` on EVERY
#: mesh defect (mesh_probe.py ~L346-347, value "2d"/"3d"/"import" or "" when unknown), so a missing
#: or other stage is evidence `targets_for` itself declines - refused here to match it.
_STAGE_REQUIRED = {"split_closed_faces": "2d", "rebuild_face": "2d", "fragment_bodies": "3d"}
#: The same rule for an operation whose stage gate depends on the CODE it is aimed at:
#: CloseWireGaps needs a 2d stage for `mesh_open_loop` and none for `wire_gap` (a kernel code,
#: which carries no stage) - so a per-operation entry would be STRICTER than `targets_for`.
_STAGE_REQUIRED_FOR_CODE = {("close_wire_gaps", "mesh_open_loop"): "2d"}


def _stage_required(name: str, code: str) -> str | None:
    """The stage `name`'s `targets_for` requires for a defect of `code`, or None for no rule."""
    return _STAGE_REQUIRED.get(name) or _STAGE_REQUIRED_FOR_CODE.get((name, code))
#: Operations that act on a part with more than one body (the CURRENT shape's solid count).
_NEEDS_MULTIPLE_BODIES = frozenset({"fragment_bodies"})


def _current_solids(shape) -> int | None:
    """The shape's solid count, or None when it cannot be known (fail open). Kernel read, so it is
    only called for an operation that needs it."""
    if shape is None:
        return None
    try:
        from meshpipeline.cad.repair.conservative import _counts
        solids = _counts(shape).get("solids")  # a MISSING key is unavailable, never 0
        return None if solids is None else int(solids)
    except Exception:  # noqa: BLE001 - total: an unknown count refuses nothing
        return None


def _form(reply: object) -> tuple[str, object]:
    """Which of the three forms this reply is - EXACTLY one, nothing beside it."""
    if not isinstance(reply, dict):
        raise Refusal("reply is not a JSON object")
    forms = [k for k in ("package", "verdict", "pass") if k in reply]
    if len(forms) != 1 or len(reply) != 1:
        raise Refusal("reply is not exactly one of package, verdict or pass")
    return forms[0], reply[forms[0]]


class RegionContext:
    """What every reply is judged against: the region and ITS OWN defects. Cheap - no kernel."""

    def __init__(self, region: Region, defects: tuple) -> None:
        self.region = region
        # THE REGION'S OWN DEFECTS: a claim the region's scoped scan cannot verify is unprovable.
        self.mine = [d for d in defects if d.fingerprint and d.fingerprint in region.fingerprints]
        self.mine_fps = frozenset(d.fingerprint for d in self.mine)


class PackageGuards:
    """What a PACKAGE is judged against beyond its region. Built only for a package reply, since
    the paired-rim half can touch the kernel and a pass or a verdict needs none of it."""

    def __init__(self, defects: tuple, *, reserved: frozenset, rims: frozenset,
                 original: Measure, solids_of: Callable[[], int | None] = lambda: None,
                 clone: bool = False) -> None:
        self.reserved = frozenset(reserved)
        self.held = held_fingerprints(defects, self.reserved)
        self.rims = rims
        # THE RUN'S CAPS, read exactly as rules_author reads them: a claim may never be wider.
        self.area_cap = float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) * original.surface_area_mm2
        self.volume_cap = float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO) * original.volume_mm3
        # THE ACTIVE CATALOGUE, read per call so it follows the consent flags - except on a
        # DISPOSABLE CLONE under `CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS`, which is offered every
        # operation the catalogue can build. The four consent flags say what a DEPLOYMENT may
        # DELIVER; on a copy they were deciding what an investigation is allowed to learn, and a
        # deployment that has not consented to an operation still cannot commit one, because
        # delivery reads this same catalogue with `clone` false.
        self.ops = {op.name: op for op in self._catalogue(clone)}
        self.solids_of = solids_of


    @staticmethod
    def _catalogue(clone: bool) -> tuple:
        """The operations these guards admit. The deployment's own, or - for a clone under the
        live posture - every operation `operations.catalogue` can build."""
        if not clone or getattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off") != "live":
            return tuple(op_catalogue())
        return tuple(op_catalogue(split_closed_faces=True, fragment_bodies=True,
                                 rebuild_face=True, close_wire_gaps=True,
                                 resolve_sub_mesh_gap=True))


#: Every key a package body may carry. Anything else is refused, so an attempt to smuggle a field
#: (a tolerance, `answered_held`, `reclassified`) shows up as a recorded refusal, not a silent drop.
_PACKAGE_KEYS = frozenset({"intent", "plan", "cleared", "unchanged", "max_area_delta_mm2",
                           "max_volume_delta_mm3"})


def plan_entry(item: object, ctx: RegionContext, guards: PackageGuards, *,
               clone: bool = False, cured_on_clone: bool = False) -> tuple[dict, dict]:
    """One plan entry as the session's plan item, and as recorded.

    `clone` says this entry will run on a DISPOSABLE SCRATCH COPY, never delivered. It drops the
    two rules that exist to keep a PROPOSAL sensible rather than to keep a part safe: the
    catalogue's "does this operation answer that code" table, and an operation's stage
    precondition. Measured on wave 1, those two refused 74 and 27 clone edits - an investigation
    was being argued out of trying things on a copy that cannot ship. Everything else still
    holds, here and at delivery: a fingerprint must be a defect of this region, held geometry is
    untouchable, and a paired rim may not be filled.

    `cured_on_clone` is DELIVERY-side and waives ONLY the answer table: the clone recorded that
    the part meshes after these edits, a measured result the advisory table cannot outweigh. The
    stage precondition and every safety rule above still apply.
    """
    if not isinstance(item, dict) or set(item) - {"op", "fingerprint", "defect_code"}:
        raise Refusal("a plan entry is not {op, fingerprint}")
    name, fp = item.get("op"), item.get("fingerprint")
    if not isinstance(name, str) or name not in guards.ops:
        raise Refusal(f"plan names {name!r}, which is not in the active catalogue")
    if not isinstance(fp, str) or fp not in ctx.mine_fps:
        raise Refusal(f"plan aims at {fp!r}, which is not a defect fingerprint in region "
                       f"{ctx.region.index}")
    if fp in guards.held:
        raise Refusal(f"plan aims at {fp}, which is held for review")
    if name in FILL_OPS and guards.rims:
        raise Refusal(f"plan fills {fp} while the part has a paired rim (a missing passage "
                       "wall); filling is refused for the whole run")
    op = guards.ops[name]
    on = sorted((d for d in ctx.mine if d.fingerprint == fp), key=lambda d: d.code.value)
    named = item.get("defect_code")
    if named is not None:
        on = [d for d in on if d.code.value == named]
        if not on:
            raise Refusal(f"plan names defect_code {named!r}, which {fp} does not carry")
    # A fingerprint can carry several codes: aim at the ones THIS operation answers, and refuse
    # only when that leaves nothing (never because another code sorts first).
    answered = [d for d in on if d.code in op.answers]
    if not answered and not clone and not cured_on_clone:
        raise Refusal(f"op_does_not_answer: {name} does not answer a "
                       f"{on[0].code.value}", code="op_does_not_answer", defect=fp)
    # ON A CLONE an unanswered aim is still ATTEMPTED, against the code that sorts first: the
    # answer table is the catalogue's advice about what is worth proposing, and a copy is where
    # advice is allowed to be wrong.
    defect = (answered or on)[0]
    if defect.code in guards.reserved:
        raise Refusal(f"plan aims at a {defect.code.value}, a code held for review")
    # OPERATION PRECONDITIONS, refused here so an invalid proposal costs no execution.
    stage = None if clone else _stage_required(name, defect.code.value)
    if stage is not None and (getattr(defect, "measurements", None) or {}).get("stage") != stage:
        raise Refusal(f"stage_precondition: {name} needs a {stage}-stage defect",
                       code="stage_precondition", defect=fp)
    if name in _NEEDS_MULTIPLE_BODIES:
        # THE CURRENT SHAPE'S COUNT, not the upload's (a fill can create a solid mid-run). Unknown
        # (no shape, or the count failed) FAILS OPEN: the session's own decline stands.
        solids = guards.solids_of()
        if solids is not None and solids <= 1:
            raise Refusal(f"needs_multiple_bodies: {name} needs more than one solid in the part "
                           f"(this one has {solids})", code="needs_multiple_bodies", defect=fp)
    target = EntityRef(entity_type=str(defect.entity_type), fingerprint=fp,
                       entity_index=int(defect.entity_index),
                       ambiguous=bool(getattr(defect, "ambiguous", False)))
    # NO `tolerance_mm` and NO `answered_held`: the loop fills the run's tolerance, and an
    # engineer's exemption is never a model's to give (session.try_package).
    return ({"op": guards.ops[name], "targets": (target,), "defect_code": defect.code},
            {"op": name, "fingerprint": fp, "defect_code": defect.code.value})


def validate_package(body: object, ctx: RegionContext, guards: PackageGuards, *, label: str,
                     model_calls: int = 1,
                     cured_on_clone: bool = False) -> tuple[TargetPackage, dict]:
    """The package a reply claims, validated, and the same claim as recorded.

    `label` goes in the rationale and `model_calls` in the budget, so a SECOND author reports
    itself rather than this one (an investigator spends many calls on one package). Everything
    else is the run's own policy, read per call.

    `cured_on_clone` is the clone's record that the part meshes after this plan's edits
    (`session.MESHES_AFTER_TARGET_REMAINS`). It lifts the op-answers-defect table and nothing
    else: no op-answers-defect gate sits between a measured clean result and delivery.
    """
    if not isinstance(body, dict):
        raise Refusal("package is not an object")
    extra = sorted(str(k) for k in set(body) - _PACKAGE_KEYS)
    if extra:
        raise Refusal(f"package carries keys outside the schema: {', '.join(extra)}")
    intent = body.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise Refusal("package states no intent")
    plan = body.get("plan")
    if not isinstance(plan, list) or not plan:
        raise Refusal("package has an empty plan")
    if len(plan) > int(rcfg.CAD_REPAIR_MAX_OPS):
        raise Refusal(f"plan has {len(plan)} operations, past the run's "
                       f"{int(rcfg.CAD_REPAIR_MAX_OPS)}")
    items, recorded = zip(*(plan_entry(item, ctx, guards, cured_on_clone=cured_on_clone)
                          for item in plan), strict=True)
    cleared = _strings(body.get("cleared"))
    if cleared is None:
        raise Refusal("cleared is not a list of fingerprints")
    if set(cleared) - ctx.mine_fps:
        raise Refusal("cleared names fingerprints that are not defects in region "
                       f"{ctx.region.index}: {', '.join(sorted(set(cleared) - ctx.mine_fps))}")
    if set(cleared) & guards.held:
        raise Refusal("cleared names a fingerprint held for review")
    unchanged = _strings(body.get("unchanged", []))
    if unchanged is None:
        raise Refusal("unchanged is not a list of fingerprints")
    # THE REGION'S FINGERPRINTS, the set `session._package_refusal` checks every predicate name
    # against: a name outside it is a package the session is certain to refuse, so it is refused
    # here, where it costs no attempt.
    outside = set(unchanged) - ctx.region.fingerprints
    if outside:
        raise Refusal(f"unchanged names fingerprints outside region {ctx.region.index}: "
                       f"{', '.join(sorted(outside))}")
    if not cleared and not unchanged:
        raise Refusal("cleared and unchanged are both empty: the claim names no entity")
    bounds = {}
    for field, cap in (("max_area_delta_mm2", guards.area_cap),
                       ("max_volume_delta_mm3", guards.volume_cap)):
        value = _number(body.get(field))
        if value is None or value < 0.0:
            raise Refusal(f"{field} is not a non-negative number")
        if value > cap:
            raise Refusal(f"{field} {value:.6g} is past the run's cap {cap:.6g}")
        bounds[field] = value
    text = observe.clip_text(intent, INTENT_MAX)
    package = TargetPackage(
        region=ctx.region, intent=text, plan=tuple(items),
        predicate=Predicate(cleared=frozenset(cleared), reclassified=frozenset(),
                            unchanged=frozenset(unchanged), **bounds),
        budget=PackageBudget(max_ops=len(items), timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S),
                             model_calls=int(model_calls), deviation_mm=0.0),
        rationale=f"{label}: {text}")
    proposal = {"intent": text, "plan": list(recorded), "cleared": sorted(cleared),
                "unchanged": sorted(unchanged), **bounds}
    return package, proposal


def validate_verdict(body: object, ctx: RegionContext, *, label: str) -> dict:
    """The "the geometry is sound, the mesher is at fault" verdict a reply claims, validated.

    `label` is the author that reached it, recorded as `author`."""
    if not isinstance(body, dict):
        raise Refusal("verdict is not an object")
    named = _strings(body.get("mesher_problem"))
    if not named:
        raise Refusal("verdict names no mesher_problem fingerprints")
    if set(named) - ctx.mine_fps:
        raise Refusal("verdict names fingerprints that are not defects in region "
                       f"{ctx.region.index}")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise Refusal("verdict gives no reason")
    return {"region": int(ctx.region.index), "mesher_problem": sorted(named),
            "reason": observe.clip_text(reason, INTENT_MAX), "author": label}


def rim_guard(shape, part_rims: frozenset, defects: tuple, refusals: list) -> frozenset:
    """The paired rims filling is refused for, with every one recorded into `refusals`.

    THE WHOLE PART'S RIMS, WIDENED BY THE REGION'S OWN, exactly as rules_author seeds its ledger
    and then lets the planner recompute; recorded as refusals the report carries.
    """
    local = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()
    history = RunHistory()
    for rims in (frozenset(part_rims), local):
        refuse_paired_rims(history, rims)
    _keep_refusals(refusals, history.refusals)
    return frozenset(part_rims) | frozenset(local)


def _package(body: object, ctx: RegionContext,
             guards: PackageGuards) -> tuple[TargetPackage, dict]:
    """`validate_package` as THIS author makes it: its own label, one model call per package."""
    return validate_package(body, ctx, guards, label=LABEL, model_calls=1)


def _verdict(body: object, ctx: RegionContext) -> dict:
    """`validate_verdict` as THIS author makes it: its own label."""
    return validate_verdict(body, ctx, label=LABEL)


_Context = RegionContext
_Guards = PackageGuards
_plan_item = plan_entry


def model_author(shape, *, original: Measure, reserved: frozenset = frozenset(),
                 job_id: str = "", face_reader: observe.FaceReader | None = None,
                 deadline_s: float | None = None, clock: Callable[[], float] = time.monotonic,
                 shadow: bool = False) -> Author:
    """Build the model `author` the region loop calls: `(region, defects, attempts) -> package`.

    ONE CALL PER AUTHOR CALL. No router, a timeout, garbage, a refused (malformed or invalid)
    reply, a `pass` and a `verdict` all return None - and a None ENDS THE REGION'S ATTEMPTS, as the
    rules author's None does (region_loop). A malformed reply is therefore NOT retried, neither
    within the call nor by the loop: the region is left as it is and the final scan reports it.

    THE CLOCKS, because a model call spends the repair run's own time:
      - at most CAD_REPAIR_REGION_ATTEMPTS calls per region, and at most
        CAD_REPAIR_LLM_AUTHOR_CALLS_MAX across the whole run (this factory is per run);
      - `deadline_s` is an ABSOLUTE `clock()` reading the run must finish model work by
        (conservative passes start + CAD_REPAIR_TIMEOUT_S - 5). Each call's timeout is the lesser
        of CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S and the time left; with none left, None without a call;
      - `shadow=True` is stricter: a shadow call is skipped unless a FULL timeout plus a margin is
        left, because a shadow must never move the outcome of the run it watches.
    A skipped call is recorded (outcome `skipped`) so the report says why the model was not asked.

    WHAT IT EXPOSES, read off the callable like rules_author's attributes (the seam stays
    `(region, defects, attempts)`):
      - `calls`: one record per call - region, attempt, observation bytes and hash, the clipped
        reply, the outcome (package / verdict / pass / refused / no_reply / not_configured /
        skipped) and the refusal reason; a validated package's `proposal` too.
      - `verdicts`: every accepted "the geometry is sound, the mesher is at fault" verdict.
      - `refusals`: the run-level paired-rim refusals, in the shape rules_author reports them.
      - `observe_part`: the whole part's defects, once per run (the paired-rim guard).
      - `observe_shape`: the session's CURRENT shape, before every call (region_loop).
      - `report()`: all of the above as one JSON-safe dict, for the repair report.

    `shape` IS THE UPLOAD, as for rules_author, and the paired-rim check is read from it. THE
    OBSERVATION'S FACES ARE READ FROM THE CURRENT SHAPE (`observe_shape`, final review M1): a
    defect's `entity_index` is the current shape's, and once a split or fragment commit renumbers
    faces, reading the upload by that index would show the model the wrong faces' type, area and
    bbox. Until the loop says otherwise the current shape is the upload.
    """
    calls: list[dict] = []
    verdicts: list[dict] = []
    refusals: list[dict] = []
    asked: dict[int, int] = {}
    state: dict = {"part_rims": frozenset(), "shape": shape}
    held_codes = frozenset(reserved)

    def observe_part(part_defects: tuple) -> None:
        state["part_rims"] = (paired_rim_fingerprints(shape, tuple(part_defects))
                              if shape is not None else frozenset())

    def observe_shape(current) -> None:
        state["shape"] = current

    def _skip_reason(timeout: float) -> str:
        if sum(asked.values()) >= max(0, int(rcfg.CAD_REPAIR_LLM_AUTHOR_CALLS_MAX)):
            return "the run's model calls are spent (CAD_REPAIR_LLM_AUTHOR_CALLS_MAX)"
        if deadline_s is None:
            return ""
        left = deadline_s - clock()
        if left <= 0.0:
            return "the run's time is spent"
        if shadow and left < timeout + _GRACE_S:
            return "too little of the run's time is left for a shadow call"
        return ""

    def author(region: Region, defects: tuple,
               attempts: tuple[PackageVerdict, ...] = ()) -> TargetPackage | None:
        if not region.located:
            # Not a locus (rules_author says why); no call is spent on it.
            return None
        if asked.get(region.index, 0) >= max(1, int(rcfg.CAD_REPAIR_REGION_ATTEMPTS)):
            return None
        record: dict = {"region": int(region.index), "attempt": len(attempts) + 1}
        calls.append(record)
        if not _configured():
            record.update(outcome="not_configured", reason="no model router is configured")
            return None
        timeout = float(rcfg.CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S)
        skip = _skip_reason(timeout)
        if skip:
            record.update(outcome="skipped", reason=skip)
            return None
        if deadline_s is not None:
            timeout = min(timeout, deadline_s - clock())
        asked[region.index] = asked.get(region.index, 0) + 1
        obs = observe.observation_of(region, tuple(defects), tuple(attempts), state["shape"],
                                     original=original, face_reader=face_reader)
        user = json.dumps(obs, sort_keys=True, allow_nan=False)
        record.update(observation_bytes=len(user.encode()),
                      observation_sha256=hashlib.sha256(user.encode()).hexdigest()[:16])
        reply = _ask_sync(system_prompt(), user, job_id=job_id, timeout_s=timeout)
        record["reply"] = _clip_reply(reply)
        if reply is None:
            record.update(outcome="no_reply",
                          reason="the model call failed, timed out or returned no JSON object")
            return None
        try:
            form, body = _form(reply)
            ctx = RegionContext(region, tuple(defects))
            if form == "pass":
                if body is not True:
                    raise Refusal("pass is not true")
                record.update(outcome="pass", reason="")
                return None
            if form == "verdict":
                verdict = _verdict(body, ctx)
                verdicts.append(verdict)
                record.update(outcome="verdict", reason="")
                return None
            guards = PackageGuards(
                tuple(defects), reserved=held_codes,
                rims=rim_guard(shape, state["part_rims"], tuple(defects), refusals),
                original=original, solids_of=lambda: _current_solids(state["shape"]))
            package, proposal = _package(body, ctx, guards)
        except Refusal as exc:
            record.update(outcome="refused", reason=str(exc))
            if exc.code:
                _keep_refusals(refusals, [{"defect": exc.defect, "reason": exc.code,
                                           "detail": str(exc)}])
            return None
        record.update(outcome="package", reason="", proposal=proposal)
        return package

    def report() -> dict:
        return {"label": LABEL, "calls": [dict(c) for c in calls],
                "verdicts": [dict(v) for v in verdicts]}

    author.calls = calls  # type: ignore[attr-defined]
    author.verdicts = verdicts  # type: ignore[attr-defined]
    author.refusals = refusals  # type: ignore[attr-defined]
    author.observe_part = observe_part  # type: ignore[attr-defined]
    author.observe_shape = observe_shape  # type: ignore[attr-defined]
    author.report = report  # type: ignore[attr-defined]
    author.label = LABEL  # type: ignore[attr-defined]
    return author


def shadow_author(shape, *, original: Measure, reserved: frozenset = frozenset(),
                  job_id: str = "", face_reader: observe.FaceReader | None = None,
                  rules: Author | None = None, deadline_s: float | None = None,
                  clock: Callable[[], float] = time.monotonic) -> Author:
    """The rules author DRIVES; the model is asked beside it and its proposal recorded.

    Composes rules_author's callable (never reimplements it) and returns exactly what it returns,
    so the run is the rules run. Nothing of the model's is executed: its package is built only to
    be validated and is discarded. Each author call appends one `shadow_proposals` record - the
    region, the attempt, whether the rules proposed, and the model's call record (validated or
    refused, with the reason). `refusals` are the rules author's, since the rules drove.

    THE SHADOW CANNOT MOVE THE OUTCOME: the inner model author runs with `shadow=True`, so it
    makes no call unless a full call timeout plus a margin is left before `deadline_s`, and it
    shares the run-wide CAD_REPAIR_LLM_AUTHOR_CALLS_MAX.
    """
    driver = rules if rules is not None else rules_author(shape, original=original,
                                                          reserved=reserved)
    model = model_author(shape, original=original, reserved=reserved, job_id=job_id,
                         face_reader=face_reader, deadline_s=deadline_s, clock=clock,
                         shadow=True)
    proposals: list[dict] = []

    def observe_part(part_defects: tuple) -> None:
        for each in (driver, model):
            seen = getattr(each, "observe_part", None)
            if callable(seen):
                seen(part_defects)

    def author(region: Region, defects: tuple,
               attempts: tuple[PackageVerdict, ...] = ()) -> TargetPackage | None:
        package = driver(region, defects, attempts)
        before = len(model.calls)  # type: ignore[attr-defined]
        try:
            model(region, defects, attempts)
        except Exception as exc:  # noqa: BLE001 - a shadow that breaks must not move the run
            logger.warning("model author shadow failed: %s", type(exc).__name__)
        made = model.calls[before:]  # type: ignore[attr-defined]
        proposals.append({"region": int(region.index), "attempt": len(attempts) + 1,
                          "rules_proposed": package is not None,
                          "model": dict(made[-1]) if made else None})
        return package

    def report() -> dict:
        return {**model.report(), "posture": "shadow",  # type: ignore[attr-defined]
                "shadow_proposals": [dict(p) for p in proposals]}

    author.calls = model.calls  # type: ignore[attr-defined]
    author.verdicts = model.verdicts  # type: ignore[attr-defined]
    author.shadow_proposals = proposals  # type: ignore[attr-defined]
    author.refusals = getattr(driver, "refusals", [])  # type: ignore[attr-defined]
    author.observe_part = observe_part  # type: ignore[attr-defined]
    author.observe_shape = model.observe_shape  # type: ignore[attr-defined]
    author.report = report  # type: ignore[attr-defined]
    author.label = RULES_LABEL  # type: ignore[attr-defined]
    return author


def select_author(shape, *, original: Measure, reserved: frozenset = frozenset(),
                  job_id: str = "", deadline_s: float | None = None,
                  tolerance_mm: float = 0.0) -> tuple[Author, str]:
    """The region loop's author for the deployment's posture, and the label the run reports.

    THE INVESTIGATOR POSTURE IS CONSULTED FIRST (`CAD_REPAIR_INVESTIGATOR`), because a bounded
    tool loop and a one-shot call are two postures of the same seam and a deployment that asked
    for the loop must not get the one-shot author instead. Off - the default - falls through to
    the body below untouched, so with the posture unset this function is byte-for-byte what it
    was. `tolerance_mm` is the run's tolerance, which only the investigator needs (its edits run
    on a scratch session, and `try_op` takes one).

    Then: live + a configured router -> the model author; shadow -> the shadow composite (the
    rules drive); anything else, including live with no router, -> the rules author exactly as it
    was built before this module existed. `deadline_s` bounds the model's calls (see
    model_author).
    """
    from meshpipeline.cad.repair import investigator as inv
    from meshpipeline.cad.repair import tournament as tn

    # THE TOURNAMENT POSTURE IS CONSULTED FIRST (`CAD_REPAIR_TOURNAMENT`), because a tournament is
    # not a FOURTH author beside these - it is the one that CONSUMES them, gathering the rules
    # author's, the one-shot's and the investigator's packages and offering the one that survived
    # its clone. A deployment that asked for it must not get one of its own sources instead. Off -
    # the default - falls through untouched, so with the posture unset the rest of this function
    # is byte-for-byte what it was. A tournament NEEDS NO ROUTER: the rules author alone makes a
    # tournament of one, which is exactly today's behaviour at K=1.
    bracket = tn.posture()
    if bracket in ("live", "shadow"):
        return tn.tournament_author(shape, original=original, reserved=reserved, job_id=job_id,
                                    tolerance_mm=tolerance_mm, deadline_s=deadline_s,
                                    shadow=bracket == "shadow"), (
            tn.LABEL if bracket == "live" else RULES_LABEL)

    stance = inv.posture()
    if stance == "live" and _configured():
        return inv.investigator(shape, original=original, reserved=reserved, job_id=job_id,
                                tolerance_mm=tolerance_mm, deadline_s=deadline_s), inv.LABEL
    if stance == "shadow":
        # SEAM (Task 12): the investigator's shadow composite - the rules drive, the loop is
        # recorded beside them. Until it exists, a shadow posture falls through to the body below
        # rather than inventing a composite here, so nothing of the run moves.
        composite = getattr(inv, "shadow_investigator", None)
        if callable(composite):
            return composite(shape, original=original, reserved=reserved, job_id=job_id,
                             tolerance_mm=tolerance_mm, deadline_s=deadline_s), RULES_LABEL
    mode = posture()
    if mode == "live" and _configured():
        return model_author(shape, original=original, reserved=reserved, job_id=job_id,
                            deadline_s=deadline_s), LABEL
    if mode == "shadow":
        return shadow_author(shape, original=original, reserved=reserved, job_id=job_id,
                             deadline_s=deadline_s), RULES_LABEL
    return rules_author(shape, original=original, reserved=reserved), RULES_LABEL
