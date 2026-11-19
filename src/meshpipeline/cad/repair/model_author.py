# Responsibility: Author the next target package for a region by ASKING A MODEL, through the same
#                 `author` seam the deterministic rules author fills, and refuse every reply that
#                 is not a bounded, catalogue-only claim about the region it was asked about.
# Owns: the model author's prompt, the reply schema and its validation, the per-region call
#       budget, the shadow composite (rules drive, model recorded), driver selection by posture,
#       and the record of every call (observation size, clipped reply, outcome, refusal reason).
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
from collections.abc import Callable

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


async def _ask(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """ONE bounded model call; the reply's JSON object, or None for any failure at all.

    The same call `agents/repair_review/_wire.ask` makes, made through `contracts` directly
    (cad -> contracts is allowed; cad -> agents is not). Looked up as `_ask` AT CALL TIME, so a
    test fakes it where this module looks it up.
    """
    from meshpipeline.contracts.model_inference import call_repair_review_model
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        result = await asyncio.wait_for(
            call_repair_review_model(messages, job_id=job_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - total: a failed call is no proposal, never a raise
        logger.warning("model author call failed: %s", type(exc).__name__)
        return None
    if not getattr(result, "ok", True):
        return None
    return _parse_object(getattr(result, "assistant_text", "") or "")


def _ask_sync(system: str, user: str, *, job_id: str, timeout_s: float) -> dict | None:
    """`_ask` from synchronous code: `asyncio.run` in a WORKER THREAD.

    The region loop is synchronous and itself runs off the event loop (`asyncio.to_thread` in the
    attempt node), so the model call gets a loop of its own in a thread of its own - which also
    works if a caller ever invokes the loop from a thread that already runs one. Total: any
    failure, and a thread that outlives its timeout, is None. A DAEMON thread, so one abandoned
    past its timeout never holds the process open at exit.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["reply"] = asyncio.run(_ask(system, user, job_id=job_id,
                                            timeout_s=timeout_s))
        except BaseException as exc:  # noqa: BLE001 - total: reported below, never raised
            box["error"] = exc

    worker = threading.Thread(target=run, name="model-author", daemon=True)
    worker.start()
    worker.join(timeout_s + _GRACE_S)
    if worker.is_alive():
        logger.warning("model author call outlived its %.1fs timeout; abandoned", timeout_s)
        return None
    if "error" in box:
        logger.warning("model author call failed: %s", type(box["error"]).__name__)
        return None
    reply = box.get("reply")
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


class _Refused(ValueError):
    """A reply that is not a bounded, catalogue-only claim; the message is the reason.

    `code` and `defect` are set for a PRECONDITION refusal (a named reason the run's report counts,
    in the shape `RunHistory.refuse` records); every other refusal carries the message alone."""

    def __init__(self, message: str, *, code: str = "", defect: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.defect = defect


#: What each stage-gated operation's `targets_for` requires of the defect's `measurements["stage"]`.
#: KEPT IN LOCKSTEP with operations.py (SplitClosedFaces, RebuildFace: 2d; FragmentBodies: 3d). A
#: drift means the author refuses where the session would decline - the same outcome, cheaper - or
#: (the other way) defers to the session's own decline, which is still safe.
_STAGE_REQUIRED = {"split_closed_faces": "2d", "rebuild_face": "2d", "fragment_bodies": "3d"}
#: Operations that act on a part with more than one body (the upload's solid count).
_NEEDS_MULTIPLE_BODIES = frozenset({"fragment_bodies"})


def _form(reply: object) -> tuple[str, object]:
    """Which of the three forms this reply is - EXACTLY one, nothing beside it."""
    if not isinstance(reply, dict):
        raise _Refused("reply is not a JSON object")
    forms = [k for k in ("package", "verdict", "pass") if k in reply]
    if len(forms) != 1 or len(reply) != 1:
        raise _Refused("reply is not exactly one of package, verdict or pass")
    return forms[0], reply[forms[0]]


class _Context:
    """What every reply is judged against: the region and ITS OWN defects. Cheap - no kernel."""

    def __init__(self, region: Region, defects: tuple) -> None:
        self.region = region
        # THE REGION'S OWN DEFECTS: a claim the region's scoped scan cannot verify is unprovable.
        self.mine = [d for d in defects if d.fingerprint and d.fingerprint in region.fingerprints]
        self.mine_fps = frozenset(d.fingerprint for d in self.mine)


class _Guards:
    """What a PACKAGE is judged against beyond its region. Built only for a package reply, since
    the paired-rim half can touch the kernel and a pass or a verdict needs none of it."""

    def __init__(self, defects: tuple, *, reserved: frozenset, rims: frozenset,
                 original: Measure) -> None:
        self.reserved = frozenset(reserved)
        self.held = held_fingerprints(defects, self.reserved)
        self.rims = rims
        # THE RUN'S CAPS, read exactly as rules_author reads them: a claim may never be wider.
        self.area_cap = float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) * original.surface_area_mm2
        self.volume_cap = float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO) * original.volume_mm3
        # THE ACTIVE CATALOGUE, read per call so it follows the consent flags.
        self.ops = {op.name: op for op in op_catalogue()}
        self.solids = int(original.solids)


#: Every key a package body may carry. Anything else is refused, so an attempt to smuggle a field
#: (a tolerance, `answered_held`, `reclassified`) shows up as a recorded refusal, not a silent drop.
_PACKAGE_KEYS = frozenset({"intent", "plan", "cleared", "unchanged", "max_area_delta_mm2",
                           "max_volume_delta_mm3"})


def _plan_item(item: object, ctx: _Context, guards: _Guards) -> tuple[dict, dict]:
    """One plan entry as the session's plan item, and as recorded."""
    if not isinstance(item, dict) or set(item) - {"op", "fingerprint", "defect_code"}:
        raise _Refused("a plan entry is not {op, fingerprint}")
    name, fp = item.get("op"), item.get("fingerprint")
    if not isinstance(name, str) or name not in guards.ops:
        raise _Refused(f"plan names {name!r}, which is not in the active catalogue")
    if not isinstance(fp, str) or fp not in ctx.mine_fps:
        raise _Refused(f"plan aims at {fp!r}, which is not a defect fingerprint in region "
                       f"{ctx.region.index}")
    if fp in guards.held:
        raise _Refused(f"plan aims at {fp}, which is held for review")
    if name in FILL_OPS and guards.rims:
        raise _Refused(f"plan fills {fp} while the part has a paired rim (a missing passage "
                       "wall); filling is refused for the whole run")
    on = sorted((d for d in ctx.mine if d.fingerprint == fp), key=lambda d: d.code.value)
    named = item.get("defect_code")
    if named is not None:
        on = [d for d in on if d.code.value == named]
        if not on:
            raise _Refused(f"plan names defect_code {named!r}, which {fp} does not carry")
    defect = on[0]
    if defect.code in guards.reserved:
        raise _Refused(f"plan aims at a {defect.code.value}, a code held for review")
    # OPERATION PRECONDITIONS, refused here so an invalid proposal costs no execution.
    op = guards.ops[name]
    if defect.code not in op.answers:
        raise _Refused(f"op_does_not_answer: {name} does not answer a {defect.code.value}",
                       code="op_does_not_answer", defect=fp)
    stage = _STAGE_REQUIRED.get(name)
    if stage is not None and (getattr(defect, "measurements", None) or {}).get("stage") != stage:
        raise _Refused(f"stage_precondition: {name} needs a {stage}-stage defect",
                       code="stage_precondition", defect=fp)
    if name in _NEEDS_MULTIPLE_BODIES and guards.solids <= 1:
        raise _Refused(f"needs_multiple_bodies: {name} needs more than one solid in the part "
                       f"(this one has {guards.solids})", code="needs_multiple_bodies", defect=fp)
    target = EntityRef(entity_type=str(defect.entity_type), fingerprint=fp,
                       entity_index=int(defect.entity_index),
                       ambiguous=bool(getattr(defect, "ambiguous", False)))
    # NO `tolerance_mm` and NO `answered_held`: the loop fills the run's tolerance, and an
    # engineer's exemption is never a model's to give (session.try_package).
    return ({"op": guards.ops[name], "targets": (target,), "defect_code": defect.code},
            {"op": name, "fingerprint": fp, "defect_code": defect.code.value})


def _package(body: object, ctx: _Context, guards: _Guards) -> tuple[TargetPackage, dict]:
    if not isinstance(body, dict):
        raise _Refused("package is not an object")
    extra = sorted(str(k) for k in set(body) - _PACKAGE_KEYS)
    if extra:
        raise _Refused(f"package carries keys outside the schema: {', '.join(extra)}")
    intent = body.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise _Refused("package states no intent")
    plan = body.get("plan")
    if not isinstance(plan, list) or not plan:
        raise _Refused("package has an empty plan")
    if len(plan) > int(rcfg.CAD_REPAIR_MAX_OPS):
        raise _Refused(f"plan has {len(plan)} operations, past the run's "
                       f"{int(rcfg.CAD_REPAIR_MAX_OPS)}")
    items, recorded = zip(*(_plan_item(item, ctx, guards) for item in plan), strict=True)
    cleared = _strings(body.get("cleared"))
    if cleared is None:
        raise _Refused("cleared is not a list of fingerprints")
    if set(cleared) - ctx.mine_fps:
        raise _Refused("cleared names fingerprints that are not defects in region "
                       f"{ctx.region.index}: {', '.join(sorted(set(cleared) - ctx.mine_fps))}")
    if set(cleared) & guards.held:
        raise _Refused("cleared names a fingerprint held for review")
    unchanged = _strings(body.get("unchanged", []))
    if unchanged is None:
        raise _Refused("unchanged is not a list of fingerprints")
    # THE REGION'S FINGERPRINTS, the set `session._package_refusal` checks every predicate name
    # against: a name outside it is a package the session is certain to refuse, so it is refused
    # here, where it costs no attempt.
    outside = set(unchanged) - ctx.region.fingerprints
    if outside:
        raise _Refused(f"unchanged names fingerprints outside region {ctx.region.index}: "
                       f"{', '.join(sorted(outside))}")
    if not cleared and not unchanged:
        raise _Refused("cleared and unchanged are both empty: the claim names no entity")
    bounds = {}
    for field, cap in (("max_area_delta_mm2", guards.area_cap),
                       ("max_volume_delta_mm3", guards.volume_cap)):
        value = _number(body.get(field))
        if value is None or value < 0.0:
            raise _Refused(f"{field} is not a non-negative number")
        if value > cap:
            raise _Refused(f"{field} {value:.6g} is past the run's cap {cap:.6g}")
        bounds[field] = value
    text = observe.clip_text(intent, INTENT_MAX)
    package = TargetPackage(
        region=ctx.region, intent=text, plan=tuple(items),
        predicate=Predicate(cleared=frozenset(cleared), reclassified=frozenset(),
                            unchanged=frozenset(unchanged), **bounds),
        budget=PackageBudget(max_ops=len(items), timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S),
                             model_calls=1, deviation_mm=0.0),
        rationale=f"{LABEL}: {text}")
    proposal = {"intent": text, "plan": list(recorded), "cleared": sorted(cleared),
                "unchanged": sorted(unchanged), **bounds}
    return package, proposal


def _verdict(body: object, ctx: _Context) -> dict:
    if not isinstance(body, dict):
        raise _Refused("verdict is not an object")
    named = _strings(body.get("mesher_problem"))
    if not named:
        raise _Refused("verdict names no mesher_problem fingerprints")
    if set(named) - ctx.mine_fps:
        raise _Refused("verdict names fingerprints that are not defects in region "
                       f"{ctx.region.index}")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise _Refused("verdict gives no reason")
    return {"region": int(ctx.region.index), "mesher_problem": sorted(named),
            "reason": observe.clip_text(reason, INTENT_MAX), "author": LABEL}


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

    def _rims(defects: tuple) -> frozenset:
        # THE WHOLE PART'S RIMS, WIDENED BY THE REGION'S OWN, exactly as rules_author seeds its
        # ledger and then lets the planner recompute; recorded as refusals the report carries.
        local = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()
        history = RunHistory()
        for rims in (state["part_rims"], local):
            refuse_paired_rims(history, rims)
        _keep_refusals(refusals, history.refusals)
        return frozenset(state["part_rims"]) | frozenset(local)

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
            ctx = _Context(region, tuple(defects))
            if form == "pass":
                if body is not True:
                    raise _Refused("pass is not true")
                record.update(outcome="pass", reason="")
                return None
            if form == "verdict":
                verdict = _verdict(body, ctx)
                verdicts.append(verdict)
                record.update(outcome="verdict", reason="")
                return None
            guards = _Guards(tuple(defects), reserved=held_codes, rims=_rims(tuple(defects)),
                             original=original)
            package, proposal = _package(body, ctx, guards)
        except _Refused as exc:
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
                  job_id: str = "", deadline_s: float | None = None) -> tuple[Author, str]:
    """The region loop's author for the deployment's posture, and the label the run reports.

    live + a configured router -> the model author; shadow -> the shadow composite (the rules
    drive); anything else, including live with no router, -> the rules author exactly as it was
    built before this module existed. `deadline_s` bounds the model's calls (see model_author).
    """
    mode = posture()
    if mode == "live" and _configured():
        return model_author(shape, original=original, reserved=reserved, job_id=job_id,
                            deadline_s=deadline_s), LABEL
    if mode == "shadow":
        return shadow_author(shape, original=original, reserved=reserved, job_id=job_id,
                             deadline_s=deadline_s), RULES_LABEL
    return rules_author(shape, original=original, reserved=reserved), RULES_LABEL
