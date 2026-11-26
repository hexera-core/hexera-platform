# Responsibility: Repair the run's geometry by itself when the inspection can point at what is
#                 wrong, and hand the mesher the repaired file - or leave the run on the original.
# Owns: the decision to attempt, the attempt, and what it records about either.
# Boundaries: it repairs and promotes. It decides no job's status, delivers no artifact, and - like
#             every node before the admission gate - refuses nothing: a repair that cannot happen
#             leaves the run exactly as it was, for the gate to judge on the original.
# Collaborates with: cad/repair/triage.py (what to aim at), cad/repair/conservative.py (the bounded
#                    repair), pipeline/repair_promote.py (the staging proof and the lineage).
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import RepairGoal
from meshpipeline.pipeline import repair_goal as goal_request

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

# WHY THIS IS A NODE AND NOT AN OPERATOR TASK.
#
# A service that only tells a customer their file is broken has moved the work, not done it. The
# defects this acts on are the ones where the right answer is not a judgement: a wire with a gap, a
# curve that disagrees with its surface, an orientation that is backwards. Those have one correct
# repair, the kernel performs it, and asking a person to click a button first buys nothing.
#
# WHAT KEEPS IT HONEST is that every step can refuse and the refusal is always the same shape -
# carry on with the file the customer sent:
#
#   triage        may only recommend a repair for a defect it can POINT AT (cad/repair/triage.py).
#                 An invalid part nobody can localise, or one whose defects are questions about
#                 intent, goes to a person instead.
#   the repair    measures its own result and refuses it if it moved the geometry too far, inflated
#                 a tolerance, deleted a face or lost a solid (cad/repair/conservative.py).
#   promotion     refuses geometry that does not provably stage for the chosen engine, that is
#                 byte-identical, or that claims a different unit (pipeline/repair_promote.py).
#
# So the worst case is the behaviour of not having tried, and the original upload remains immutable
# and retrievable throughout. That is what makes doing this without asking defensible.

#: Formats the bounded repair can rewrite. An IGES file is read and written back as STEP, which
#: changes the suffix the builder stages from, so it is left for a later slice rather than
#: half-handled here.
_REPAIRABLE_SUFFIXES = {".step", ".stp"}

#: Measurements the attempt record already states as fields of ITS OWN, so carrying them inside
#: `measurements` as well would store one fact twice and let the two copies disagree.
_RECORD_FIELDS = frozenset({"refusals", "operations", "resolved", "stopped_because"})


def _forwarded(measured: Mapping) -> dict:
    """Everything the repair loop measured, minus what the record states itself.

    A DENYLIST, AND THAT IS THE POINT. This used to be a whitelist of `before`/`after`, which
    silently dropped `declared_area_mm2` and `declared_volume_mm3` the day
    `cad/repair/conservative.py` began recording them - so the receipt, with no declaration to
    subtract, told customers an approved repair had broken a limit it had actually passed
    (pipeline/repair_receipt._shape_rows). A key added to the loop's measurements now reaches the
    receipt without a change here; one disappears only by being named above, on purpose.
    """
    def _copy(value):
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, (list, tuple)):
            return [dict(v) if isinstance(v, Mapping) else v for v in value]
        return value

    return {k: _copy(v) for k, v in measured.items() if k not in _RECORD_FIELDS}


def _returned(measurements, destination: Path) -> dict:
    """The STEP this attempt returns and its hints (pipeline/repair_return.py), or {}. {} on every
    run with the meshability pass off: nothing records a `delivery_source` there."""
    from meshpipeline.pipeline.repair_return import returned
    return returned(measurements, destination)


async def _returned_untouched(source: Path, job_id, *, name: str = "repaired") -> dict:
    """A run that never got a STEP out of the repair - triage kept it away, or the repair ran past
    its clock - still RETURNS A STEP with the meshability pass live: the upload's bytes unchanged,
    and the hints sidecar when the part will not mesh at default
    (pipeline/repair_return.passthrough_without_repair). Off - the default - {} and nothing runs.
    `name` keeps a timed-out run's copy off the path its abandoned repair thread may still write.
    Off the event loop and on the provider's own clock plus a margin; never raises."""
    from meshpipeline.pipeline import repair_return
    if not repair_return.pass_live():
        return {}
    from meshpipeline.cad.repair.mesh_hints import HINTS_WALL_S
    destination = source.with_name(f"{source.stem}-{name}{source.suffix}")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(repair_return.passthrough_without_repair, source, destination),
            timeout=float(HINTS_WALL_S) + 60.0)
    except Exception as exc:  # noqa: BLE001 - the STEP is advice beside the run, never its failure
        logger.warning("node_repair_attempt: no passthrough for an untouched part (%s) - job_id=%s",
                       type(exc).__name__, job_id)
        return {}


def _derived_source(original, repaired_path: Path):
    """A geometry handle for bytes WE produced, derived from the customer's upload.

    DERIVED EXECUTION GEOMETRY, NOT A CATALOGUED UPLOAD. These bytes are not a second thing the
    customer sent, so they get no row in `geometry_sources`: the source of record stays the
    immutable upload, and this handle exists for the run that is meshing the repair. Its digest is
    computed from the file on disk, because a handle claiming a digest it does not have would make
    every provenance statement built on it a lie.

    The id is deterministic in the repaired content, so re-deriving the same repair twice - a
    resumed run, a retried attempt - produces the same identity rather than a new one each time.
    """
    from meshpipeline.contracts.geometry_source import (
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.repair_promote import derived_interpretation

    digest, size = sha256_of(repaired_path)
    source_id = f"{original.ref.source_id}:repaired:{digest[:16]}"
    return MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id=source_id,
            owner_id=original.ref.owner_id,
            # Where the repaired bytes are DELIVERED from (application/repair_report_delivery.py
            # uploads them under this job). Nothing reads this to materialise - a resumed run
            # re-derives the repair from the original - so it names the delivered object rather
            # than inventing a source key that no catalogue entry backs.
            object_key=f"jobs/{original.ref.source_id}/repaired{repaired_path.suffix.lower()}",
            sha256=digest, size_bytes=size,
            original_filename=repaired_path.name,
            suffix_hint=repaired_path.suffix.lower()),
        interpretation=derived_interpretation(original, source_id=source_id),
        local_path=repaired_path)


def _declined(reason: str, *, outcome: str, route: str = "", detail: dict | None = None,
              returned: dict | None = None) -> dict:
    """Nothing was changed, and the run says why. Never an error: the gate judges the original.

    `outcome` is the same why as a closed word (no_report, off, not_autonomous, no_geometry,
    format, triage, timeout, unavailable, refused, failed, not_promoted, not_proved): the run's
    Repair check line (pipeline/repair_words.py) is read from it, never from the reason's prose."""
    record = {"attempted": False, "reason": reason, "outcome": outcome, "route": route}
    if detail:
        record["detail"] = detail
    if returned:
        # THE STEP THE CUSTOMER STILL GETS BACK (pipeline/repair_return.py): with the meshability
        # pass live, a refusal leaves the upload's bytes at the destination as a passthrough, and
        # the terminal path delivers it labelled as unchanged. Absent on every other run.
        record["returned"] = returned
    return {"repair_attempt": record}


def _mint(*, report: dict, measured: dict, source: Path,
          survivors_path: Path | None) -> dict:
    """Review items for what the run left unsure, as the state keys to merge in. Never raises: a
    lost item is logged, not fatal.

    THE NODE MINTS, THE TERMINAL PATH PERSISTS - this touches no database. Empty when review is off.
    """
    if not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        return {"repair_review_candidates": []}
    try:
        from meshpipeline.application.repair_review_minting import mint_with_overflow
        from meshpipeline.cad.repair.brep import _read_shape
        from meshpipeline.cad.repair.triage import split

        def _shape(path: Path):
            try:
                return _read_shape(path)
            except Exception:  # noqa: BLE001 - the camera falls back to a default without a shape
                return None

        original = _shape(source)
        shape = _shape(survivors_path) if survivors_path else original
        items, dropped = mint_with_overflow(
            shape=shape, report=report, repair_run_measured=measured, split=split(report),
            original_shape=original)
    except Exception:  # noqa: BLE001
        logger.warning("node_repair_attempt: review items could not be minted", exc_info=True)
        return {"repair_review_candidates": []}
    out: dict = {"repair_review_candidates": items}
    if dropped:
        out["repair_review_truncated"] = dropped
    return out


def _outcomes(state, operations, refusals, *, discarded: bool = False,
              directives=None, discarded_reason: str = "") -> list[dict]:
    """What became of each directive this run was given. Never raises; empty when review is off.

    `directives` is what the system handed its planner, including state and optional model
    guidance; it defaults to the state's own.
    """
    if directives is None:
        directives = state.get("repair_directives") or ()
    if not directives or not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        return []
    try:
        from meshpipeline.application.repair_review_minting import directive_outcomes
        if discarded_reason:
            return directive_outcomes(directives, operations, refusals, discarded=discarded,
                                      discarded_reason=discarded_reason)
        return directive_outcomes(directives, operations, refusals, discarded=discarded)
    except Exception:  # noqa: BLE001 - the corpus and the loop-closing are never load-bearing
        logger.warning("node_repair_attempt: directive outcomes could not be joined", exc_info=True)
        return []


def _outcome_state(outcomes: list[dict]) -> dict:
    """The outcomes the terminal path writes back onto their items (only those with an item)."""
    return {"repair_directive_outcomes": [o for o in outcomes if o.get("item_id")]} if outcomes else {}


def _not_reached(state, job_id, route: str, directives=None) -> dict:
    """A run that ended before the loop could finish owes its directives an answer too: none was
    tried to a verdict, so each is `not_reached` - logged and written back, never dropped."""
    if directives is None:
        directives = state.get("repair_directives") or ()
    if not directives or not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        return {}
    outcomes = _outcomes(state, [], [], directives=directives)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
            "route": route, "operations": [], "refusals": [], "directives": outcomes,
            "refused_because": "the run ended before the directives were tried"})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass
    return _outcome_state(outcomes)


def _not_promoted(state, job_id, route: str, directives, repair_report, why: str) -> dict:
    """A COMPLETED repair whose result was not adopted still answers its directives with what the
    operations did: a rolled-back one stays rolled back, and one the session kept is `not_reached`
    with NOT_PROMOTED, because the run meshes the original. The refusal is recorded beside them -
    never folded into "never tried". Never raises."""
    try:
        from meshpipeline.application.repair_review_minting import NOT_PROMOTED
        measured = {m.name: m.value for m in repair_report.measurements}
        attempted = [dict(op) for op in repair_report.operations]
        refusals = measured.get("refusals", []) or []
        outcomes = _outcomes(state, attempted, refusals, discarded=True, directives=directives,
                             discarded_reason=NOT_PROMOTED)
    except Exception:  # noqa: BLE001 - the corpus and the loop-closing are never load-bearing
        logger.warning("node_repair_attempt: promotion-refusal outcomes failed", exc_info=True)
        return {}
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
            "route": route, "operations": attempted, "refusals": refusals,
            "stopped_because": measured.get("stopped_because", ""),
            "planner": measured.get("planner", ""), "directives": outcomes,
            "refused_because": f"the repaired geometry was not promoted: {why}"})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass
    return _outcome_state(outcomes)


async def _triage_only(report: dict, source: Path) -> dict:
    """Items for what a person decides, when the run stays on the original and the repair's
    survivors describe a file nobody will mesh (or nothing was repaired at all)."""
    import asyncio
    return await asyncio.to_thread(_mint, report=report, measured={"survivors": []},
                                   source=source, survivors_path=None)


class _EmptyPlan:
    """What a shadow run records when the pre-pass itself failed: nothing proposed."""
    called = 0
    directives: tuple = ()
    declines: tuple = ()
    call_failures: tuple = ()


#: How long the optional trace upload may hold the run. Dev diagnostics never sit on the critical
#: path: past this the upload is abandoned (its thread may finish later) and the error recorded.
TRACE_UPLOAD_TIMEOUT_S = 10.0


def _detached(work) -> asyncio.Future:
    """`work()` on a DAEMON thread, as a future of this loop. Unlike `asyncio.to_thread`, an
    abandoned one holds nothing: neither the loop's executor shutdown nor the process's exit."""
    import threading

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def settle(error: BaseException | None) -> None:
        if not done.done():
            if error is None:
                done.set_result(None)
            else:
                done.set_exception(error)

    def run() -> None:
        error: BaseException | None = None
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the awaiting loop
            error = exc
        try:
            loop.call_soon_threadsafe(settle, error)
        except RuntimeError:
            pass  # the loop has closed: nobody is waiting any more

    threading.Thread(target=run, name="trace-upload", daemon=True).start()
    return done


async def _park_trace(job_id: str, measured: dict) -> None:
    """Move the investigation trace's per-turn `lines` out of the report and into the job's object
    storage, leaving `transcript` (where it went, or why it did not) beside the outcome table.

    CAD_REPAIR_INVESTIGATION_TRACE only: with it off the repair adds no `investigation_trace` and
    this does nothing. A failed upload is recorded, never raised - diagnostics must not cost a
    run - and the lines are dropped either way, so the report and the state stay small."""
    trace = measured.get("investigation_trace") if isinstance(measured, dict) else None
    if not isinstance(trace, dict) or not trace.get("lines"):
        return
    lines = trace.pop("lines")
    body = "".join(json.dumps({**line, "job_id": job_id}, sort_keys=True, default=str) + "\n"
                   for line in lines).encode()
    digest = hashlib.sha256(body).hexdigest()
    from meshpipeline.artifact_keys import investigation_trace_key

    key = investigation_trace_key(job_id, digest)
    transcript: dict = {"lines": len(lines), "bytes": len(body), "sha256": digest}

    def upload() -> None:
        from meshpipeline.contracts.object_storage import get_object_store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_bytes(body)
            get_object_store().upload_file(local_path=path, object_key=key,
                                           content_type="application/x-ndjson")

    try:
        await asyncio.wait_for(_detached(upload), timeout=TRACE_UPLOAD_TIMEOUT_S)
        transcript["object_key"] = key
    except Exception as exc:  # noqa: BLE001 - a diagnostic that fails must not cost a repair
        logger.warning("node_repair_attempt: investigation trace upload failed (%s) - job_id=%s",
                       type(exc).__name__, job_id)
        transcript["error"] = f"{type(exc).__name__}: {exc}"[:300]
    trace["transcript"] = transcript


def _accepts(func, name: str) -> bool:
    """Does `func` take a keyword called `name`? A LANE SEAM, READ RATHER THAN ASSUMED.

    The auto-accept threshold has to be handed to `cad/repair/conservative.repair_step_file`,
    which is where the region driver is built and therefore the only place it can reach
    `region_loop.run_regions`. That file is owned by another task in flight. Asking the callee
    what it accepts means this side can land first, complete, with the hop closing by itself when
    the parameter arrives - instead of a TypeError on every repair in between.

    Never raises: an unreadable signature is answered "no", which is today's call.
    """
    import inspect
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # a C function, or no signature to read
        return False


def _posture() -> str:
    """The LLM planner posture, read at call time; anything but shadow/live is off."""
    posture = str(getattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "off") or "off")
    return posture if posture in ("shadow", "live") else "off"


async def _prepass(report: dict, job_id: str):
    """The LLM planner's plan for this report, or None. Total.

    RUNS ON THE EVENT LOOP, BEFORE the kernel's `wait_for`: the planner's own per-call timeouts
    bound it, so a slow model never spends the kernel's clock. The planner module is imported only
    here, so with the posture off nothing of it is touched.
    """
    try:
        from collections.abc import Mapping

        from meshpipeline.agents.repair_review import _wire
        from meshpipeline.agents.repair_review.planner import llm_plan
        nested = report.get("report")
        inner = dict(nested) if isinstance(nested, Mapping) else report
        entities = [e for e in (inner.get("entities") or ()) if isinstance(e, dict)]
        story = _wire.story_of([{"defect_code": e.get("code")} for e in entities])
        return await llm_plan(inner, story=story, job_id=job_id)
    except Exception:  # noqa: BLE001 - a planner that breaks leaves the run as it would have been
        logger.warning("node_repair_attempt: the LLM planner pre-pass failed", exc_info=True)
        return None


def _shadow_row(job_id: str, plan, outcome: str, *, operations=(), stopped_because: str = "",
                resolved=None, refusals=()) -> None:
    """THE ONE SHADOW WRITER, called from every exit of the node past the pre-pass.

    A shadow run pairs optional model guidance with CAD Repair System's executed plan on the same
    part, so it is logged once whichever way the
    loop ended; `not_executed` says the loop never produced a verdict. A no-op when there is no
    shadow plan. Never raises.
    """
    if plan is None:
        return
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("planner_shadow", op_id="review:shadow", payload={
            "outcome": outcome, "called": plan.called,
            "proposals": [dict(d) for d in plan.directives],
            "declines": list(plan.declines), "call_failures": list(plan.call_failures),
            "operations": [dict(o) for o in operations], "refusals": list(refusals),
            "stopped_because": stopped_because, "resolved": resolved})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass


async def node_repair_attempt(state: PipelineState) -> dict:
    """Repair the geometry if the evidence supports it, and hand on whichever file should be meshed.

    THE RUN SAYS WHAT HAPPENED. Whichever way the attempt ended, the Repair check lane the
    inspection opened gets its one line (pipeline/repair_words.py), read from what was recorded -
    and, where there is detail worth a record, the receipt beside it: what was kept, what was
    undone, what was REFUSED and why (pipeline/repair_receipt.py)."""
    from meshpipeline.pipeline import repair_receipt, repair_words

    job_id = str(state.get("job_id", "unknown"))
    out = await _attempt(state)
    await repair_words.asay(job_id, repair_words.repair_check_words(state, out))
    await repair_receipt.apublish(job_id, repair_receipt.repair_receipt_rows(state, out))
    return out


async def _attempt(state: PipelineState) -> dict:
    import asyncio

    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = state.get("job_id", "unknown")
    report = dict(state.get("repair_report") or {})
    if not report:
        return _declined("this run carries no inspection to act on", outcome="no_report")
    if not rcfg.CAD_REPAIR_ENABLED:
        return _declined("CAD repair is switched off in this deployment", outcome="off")
    if not rcfg.CAD_REPAIR_AUTONOMOUS:
        return _declined("automatic repair is switched off; the report stands for an operator",
                         outcome="not_autonomous")

    original = _materialized(state)
    if original is None or not original.path or not Path(original.path).exists():
        return _declined("there is no materialised geometry to repair", outcome="no_geometry")

    source = Path(original.path)
    if source.suffix.lower() not in _REPAIRABLE_SUFFIXES:
        return _declined(f"the bounded repair does not rewrite {source.suffix.lower()} files",
                         outcome="format")

    engine = str(state.get("engine", "") or "")
    advice = recommend(repair_status=str(state.get("repair_status", "") or ""),
                       report=report, target_engine=engine)
    if advice.route != ROUTE_CONSERVATIVE_REPAIR:
        # Triage declined to recommend an automatic repair: abstained, or routed this to a person
        # or to the customer. Either way it is not ours to attempt.
        # A DIRECTED RE-RUN STILL ANSWERS EVERY DIRECTIVE: none was tried, so each is not_reached.
        return {**_declined(f"triage routed this to {advice.route}", outcome="triage", route=advice.route,
                            detail={"reasons": list(advice.reasons),
                                    "abstain_reason": advice.abstain_reason},
                            returned=await _returned_untouched(source, job_id)),
                **_not_reached(state, job_id, advice.route)}

    # THE REPAIRED FILE LIVES IN THE JOB'S OWN WORKSPACE, beside the materialised original, so it
    # survives for the builder that stages it. A resumed run re-materialises the original into the
    # same place and re-derives the repair, which is why the derived identity is deterministic.
    destination = source.with_name(f"{source.stem}-repaired{source.suffix}")

    # OPTIONAL MODEL GUIDANCE. OFF TOUCHES NOTHING. SHADOW asks and only records; LIVE adds its
    # proposals after the engineer's and replay directives, which keep their place. All of them
    # enter the same RepairPlanner and the kernel judges each directive exactly the same way.
    posture = _posture()
    directives = tuple(state.get("repair_directives") or ())
    shadow_plan = None
    if posture != "off":
        plan = await _prepass(report, job_id)
        if posture == "shadow":
            shadow_plan = plan if plan is not None else _EmptyPlan()
        elif plan is not None:
            directives = directives + tuple(plan.directives)

    # WHAT THIS RUN IS FINISHING FOR, and the only threshold it may widen anything with
    # (pipeline/repair_goal.py). `None` is EVERY RUN TODAY: no goal declared, or the
    # `cad-repair-standalone-jobs` row not live - and `repair_step_file`'s `goal` default is None,
    # so passing it explicitly below changes not one byte of such a run.
    asked = goal_request.from_state(state)
    goal = None if asked is None else asked.goal
    threshold_mm = None if asked is None else asked.auto_accept_mm
    standalone = goal is RepairGoal.standalone

    from meshpipeline.cad.repair.conservative import (
        RESULT_V5_KEY,
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
        repair_timeout_s,
        status_for,
    )
    # THE THRESHOLD CROSSES ONE SEAM THIS MODULE DOES NOT OWN. `repair_step_file` is where the
    # region driver is constructed, so it is the only place `auto_accept_mm` can be handed to
    # `region_loop.run_regions` - and that file is another task's. The keyword is therefore passed
    # only when the callee ACCEPTS it: the hop goes live the moment that parameter lands, and
    # until then the run is the run it is today rather than one that crashed on a TypeError.
    extra: dict = {}
    if threshold_mm is not None and _accepts(repair_step_file, "auto_accept_mm"):
        extra["auto_accept_mm"] = threshold_mm
    # CRS v5 BETA: the repair's plain progress lines go onto the job's live stream as it runs
    # (pipeline/repair_progress.py), and its clock is v5's file wall plus v3's (the automatic fallback's).
    timeout_s = repair_timeout_s()
    relay = None
    if rcfg.CAD_REPAIR_ENGINE == "v5" and _accepts(repair_step_file, "on_progress"):
        from meshpipeline.pipeline.repair_progress import ProgressRelay
        relay = ProgressRelay(str(job_id)).start()
        extra["on_progress"] = relay.push
    try:
        # OFF THE EVENT LOOP AND ON A CLOCK. ShapeFix on a real part is seconds of CPU with no
        # await points, and on a pathological one it can run past the job's remaining deadline -
        # delaying a mesh that was viable all along. The thread is abandoned on expiry (the kernel
        # offers no interruption point) and the run proceeds on the original.
        try:
            repair_report = await asyncio.wait_for(
                asyncio.to_thread(repair_step_file, source, destination,
                                  directives=directives, goal=goal, **extra),
                timeout=timeout_s)
        finally:
            if relay is not None:
                await relay.stop()
        for item in repair_report.measurements:
            if item.name == "investigation_trace" and isinstance(item.value, dict):
                await _park_trace(str(job_id), {"investigation_trace": item.value})
    except TimeoutError:
        logger.warning("node_repair_attempt: the repair exceeded %.1fs - continuing on the "
                       "original - job_id=%s", timeout_s, job_id)
        from meshpipeline.engines.determinism import note_time_limit
        note_time_limit("cad_repair", "the repair ran past its time limit: the original file "
                        "is meshed", budget_s=float(timeout_s))
        _shadow_row(job_id, shadow_plan, "not_executed")
        return {**_declined(f"the repair exceeded {timeout_s:.0f}s",
                            outcome="timeout", route=advice.route,
                            returned=await _returned_untouched(source, job_id, name="returned")),
                **await _triage_only(report, source),
                **_not_reached(state, job_id, advice.route, directives)}
    except RepairUnavailable as exc:
        # OUR limitation, never a verdict on the file.
        _shadow_row(job_id, shadow_plan, "not_executed")
        return {**_declined(f"the repair could not run here: {exc}", outcome="unavailable",
                            route=advice.route,
                            returned=_returned(getattr(exc, "measurements", None), destination)),
                **await _triage_only(report, source),
                **_not_reached(state, job_id, advice.route, directives)}
    except RepairRefused as exc:
        # The kernel produced something and the caps declined it. The customer's file is unharmed
        # and this is the evidence an operator needs to decide what to do instead.
        logger.info("node_repair_attempt: repair refused (%s) - continuing on the original "
                    "- job_id=%s", exc, job_id)
        await _park_trace(str(job_id), exc.measurements)
        outcomes = _outcomes(state, exc.measurements.get("operations", []),
                             exc.measurements.get("refusals", []),
                             discarded=bool(exc.measurements.get("result_discarded")),
                             directives=directives)
        _shadow_row(job_id, shadow_plan, "refused",
                    operations=exc.measurements.get("operations", []),
                    stopped_because=str(exc.measurements.get("stopped_because", "")),
                    refusals=exc.measurements.get("refusals", []))
        try:
            from meshpipeline.capture.logger import TrainingLogger
            # A DECLINE IS STILL A TRAINING ROW, and often the most useful one. A run that kept
            # nothing is refused before anything is written, so the success path below never runs
            # - and an attempted-and-rolled-back sequence is exactly what the model-directed
            # portion needs to learn from. The attempts travel in the refusal's own measurements.
            TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
                "route": advice.route, "targets": [dict(t) for t in advice.targets],
                "operations": [dict(op) for op in exc.measurements.get("operations", [])],
                "stopped_because": exc.measurements.get("stopped_because", ""),
                "refusals": exc.measurements.get("refusals", []),
                "planner": exc.measurements.get("planner", ""),
                "refused_because": str(exc),
                "directives": outcomes,
                "measurements": dict(exc.measurements)})
        except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
            pass
        minted = await asyncio.to_thread(
            _mint, report=report, measured=dict(exc.measurements), source=source,
            survivors_path=None)
        declined = _declined(f"the repair was refused: {exc}", outcome="refused", route=advice.route,
                             detail={"measurements": exc.measurements},
                             returned=_returned(exc.measurements, destination))
        if exc.measurements.get(RESULT_V5_KEY):
            # THE BETA CONTRACT (repair_v5/result.py), where the API and the UI read it, refused or not
            declined["repair_attempt"][RESULT_V5_KEY] = exc.measurements[RESULT_V5_KEY]
            declined["repair_attempt"]["engine"] = exc.measurements.get("engine", "")
        return {**declined, **minted, **_outcome_state(outcomes)}
    except Exception as exc:  # noqa: BLE001 - a repair that breaks is never the file's fault
        _shadow_row(job_id, shadow_plan, "not_executed")
        logger.warning("node_repair_attempt: the repair failed (%s) - continuing on the original "
                       "- job_id=%s", type(exc).__name__, job_id)
        return {**_declined(f"the repair failed: {type(exc).__name__}", outcome="failed",
                            route=advice.route,
                            returned=_returned(getattr(exc, "measurements", None), destination)),
                **await _triage_only(report, source),
                **_not_reached(state, job_id, advice.route, directives)}

    # NO PROMOTION FOR A STANDALONE REPAIR JOB. Promotion exists to prove the repaired bytes
    # STAGE FOR A MESHER (pipeline/repair_promote.py) and to make them the geometry this run
    # meshes - and a standalone job meshes nothing: its verdict is the kernel plus a tier-2
    # score, with the product mesher not involved (spec amendment 1). So the repaired file is
    # handed back as itself, and the customer's upload is not replaced in a run that will
    # never build from either of them.
    update: dict = {}
    if not standalone:
        from meshpipeline.pipeline.repair_promote import PromotionRefused, promote
        try:
            # HASHING THE WRITTEN FILE CAN FAIL TOO - it may be unreadable, truncated, or gone. That
            # is our problem, not the customer's, and like every other refusal here it leaves the run
            # on the original rather than failing a mesh that was viable.
            repaired = _derived_source(original, destination)
            # OFF THE EVENT LOOP AND ON A CLOCK, like the repair itself: the staging proof is a
            # tessellation, and run here synchronously it held the job's heartbeat and cancellation
            # checks for as long as it took. The thread is abandoned on expiry, as the repair's is,
            # and the run stays on the original.
            try:
                update = await asyncio.wait_for(
                    asyncio.to_thread(promote, state, repaired=repaired, engine=engine,
                                      attempt=int(state.get("retry_count", 0) or 0)),
                    timeout=rcfg.CAD_REPAIR_TIMEOUT_S)
            except TimeoutError:
                # A PROOF THAT DID NOT FINISH IS NOT A REFUSAL. `PromotionRefused` means the geometry
                # was judged and found unusable; a clock of OURS running out judged nothing, and
                # reporting it as a refusal told a customer their sound repair "failed our checks" -
                # the Formula car, where the proof's own engine staging was still drawing a report
                # picture nobody meshes (engines/gmsh/gmsh_runner.STAGE_EXTRAS_TIMEOUT_S). The run
                # still stays on the original, because bytes we never proved may not be promoted.
                logger.warning("node_repair_attempt: the staging proof exceeded %.1fs - continuing on "
                               "the original - job_id=%s", rcfg.CAD_REPAIR_TIMEOUT_S, job_id)
                from meshpipeline.engines.determinism import note_time_limit
                note_time_limit("cad_repair_promote", "the staging proof ran past its time limit: the "
                                "original file is meshed",
                                budget_s=float(rcfg.CAD_REPAIR_TIMEOUT_S))
                _shadow_row(job_id, shadow_plan, "not_executed")
                why = (f"the staging proof did not finish within "
                       f"{rcfg.CAD_REPAIR_TIMEOUT_S:.0f}s, so the repair was not proved")
                return {**_declined(why, outcome="not_proved", route=advice.route),
                        **await _triage_only(report, source),
                        **_not_promoted(state, job_id, advice.route, directives, repair_report, why)}
        except PromotionRefused as exc:
            # It repaired, but the result is not something the mesher can use - so the run stays on the
            # original and the repair becomes evidence rather than geometry.
            logger.info("node_repair_attempt: promotion refused (%s) - continuing on the original "
                        "- job_id=%s", exc, job_id)
            _shadow_row(job_id, shadow_plan, "not_executed")
            # THE RUN STAYS ON THE ORIGINAL, so only what a person decides is minted: the repair's
            # survivors describe a file nobody will mesh.
            return {**_declined(f"the repaired geometry was not promoted: {exc}", outcome="not_promoted",
                                route=advice.route),
                    **await _triage_only(report, source),
                    **_not_promoted(state, job_id, advice.route, directives, repair_report, str(exc))}
        except Exception as exc:  # noqa: BLE001
            logger.warning("node_repair_attempt: the repaired file could not be adopted (%s: %s) - "
                           "continuing on the original - job_id=%s", type(exc).__name__, exc, job_id)
            _shadow_row(job_id, shadow_plan, "not_executed")
            return {**_declined(f"the repaired file could not be adopted: {type(exc).__name__}",
                                outcome="not_promoted", route=advice.route),
                    **await _triage_only(report, source),
                    **_not_promoted(state, job_id, advice.route, directives, repair_report,
                                    type(exc).__name__)}

    measured = {m.name: m.value for m in repair_report.measurements}
    attempted = [dict(op) for op in repair_report.operations]
    outcomes = _outcomes(state, attempted, measured.get("refusals", []), directives=directives)
    _shadow_row(job_id, shadow_plan, "executed", operations=attempted,
                stopped_because=str(measured.get("stopped_because", "")),
                resolved=bool(measured.get("resolved", False)),
                refusals=measured.get("refusals", []))
    logger.info("node_repair_attempt: repaired and promoted for engine=%s - stopped=%s "
                "- kept=%s/%s - job_id=%s", engine, measured.get("stopped_because"),
                measured.get("operations_kept"), measured.get("operations_attempted"), job_id)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        # THE SLICE 8 CORPUS. Every attempt, kept or rolled back, with the defect it aimed at and
        # the basis on which that was judged. A rolled-back operation is as much a training
        # example as a kept one - more, arguably - which is why the whole sequence is logged
        # rather than only what worked. This starts filling the day the deterministic loop is
        # enabled, which is the reason it had to go in first.
        TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
            "route": advice.route, "targets": [dict(t) for t in advice.targets],
            "operations": attempted,
            "stopped_because": measured.get("stopped_because", ""),
            "refusals": measured.get("refusals", []),
            "planner": measured.get("planner", ""),
            "directives": outcomes,
            "report": repair_report.to_dict(),
            "lineage": update.get("repair_lineage", {})})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass

    minted = await asyncio.to_thread(
        _mint, report=report,
        measured={**measured, "operations": attempted,
                  "survivors": [dict(e) for e in repair_report.entities]},
        source=source, survivors_path=destination)

    # WHAT THE GOAL ADDS TO THE RECORD, and only when one was declared - so a run with no goal
    # produces the record it produced before this field existed, key for key.
    goal_record: dict = {}
    if goal is not None:
        goal_record["goal"] = goal.value
        # THE THRESHOLD AS ASKED, recorded whether or not the seam below carries it yet: a reader
        # judging an auto-accepted package needs the number the customer declared, and a run whose
        # threshold never reached the session must be distinguishable from one that declared none.
        goal_record["auto_accept_mm"] = threshold_mm
        if threshold_mm is not None and "auto_accept_mm" not in extra:
            # THE SEAM, STATED. `repair_step_file` does not take the threshold yet, so the
            # auto-accept tier stayed inert for this run - said plainly rather than implied by a
            # tier that never fired.
            goal_record["auto_accept_reached_session"] = False
    if standalone:
        # THE DELIVERABLE. Nothing promoted it (see above), so the ending node is the only thing
        # that can name these bytes for the terminal path to hand back.
        goal_record["repaired_path"] = str(destination)
    returned_step = _returned(measured, destination)
    if returned_step:
        # THE RETURNED STEP AND ITS HINTS SIDECAR (pipeline/repair_return.py), so the terminal
        # path can upload the sidecar beside the repaired file and the mesh staging can apply it.
        goal_record["returned"] = returned_step

    # `repair_status` IS NOT TOUCHED. It is the inspection's verdict on the file the customer
    # SENT, and `repair_report` beside it is that inspection - so overwriting it with the repair's
    # own verdict produced a pairing that read "repaired" next to the original defect list.
    return {
        **update,
        **minted,
        **_outcome_state(outcomes),
        "repair_attempt": {
            "attempted": True,
            # NOT A STATEMENT THAT THE RUN SUCCEEDED. `status_for` (cad/repair/conservative.py)
            # returns "repaired" unless the report's DEFECTS include an error or fatal entry - it
            # looks at no caps and at no warnings, so warnings left behind still read "repaired",
            # even beside `resolved: False`. Read `resolved` for whether the loop succeeded. Not to
            # be confused with the top-level `repair_status`, the inspection's verdict on the
            # customer's file.
            "repair_status": status_for(repair_report).value,
            # TAKEN FROM THE LOOP, NOT INFERRED. It used to be derived from `holes_left`, a key
            # the report no longer carries - which made it read True for any run with no warning
            # defect, including a run the loop had explicitly reported as a failure.
            "resolved": bool(measured.get("resolved", False)),
            # WHY THE LOOP STOPPED. "Every error cleared" and "I ran out of operations" leave the
            # same geometry and mean opposite things to the operator reading this.
            "stopped_because": measured.get("stopped_because", ""),
            "route": advice.route,
            # EVERY ATTEMPT, with its targets, verdict, reason and measured delta.
            "operations": attempted,
            # WHAT THE REPAIR AIMED AT, kept with what it did: a reviewer approving this reads the
            # defects that justified it beside the operations that answered them.
            "targets": [dict(t) for t in advice.targets],
            # WHAT WAS DELIBERATELY NOT ATTEMPTED AND WHY - the rims of a missing bore wall above
            # all, which used to be a silent skip inside the fill executor.
            "refusals": measured.get("refusals", []),
            # THE MEASURED SHAPE CHANGE, before and after - and everything else the loop
            # measured, above all what the kept operations DECLARED they would add. The caps
            # already refused anything past them, so this is not a check: it is the figure the
            # receipt states beside the cap it stayed inside, and the receipt cannot separate
            # accounted-for change from unexplained change without the declarations
            # (pipeline/repair_receipt._shape_rows). See `_forwarded` for why this is not a
            # whitelist. The refused path forwards the same dict whole, in `detail.measurements`.
            "measurements": _forwarded(measured),
            "defects_remaining": [dict(e) for e in repair_report.entities],
            "summary": repair_report.summary,
            # CRS v5 BETA: which engine ran, and the result contract (repair_v5/result.py) the API and the UI read.
            # Absent on a v3-only or search run, so those records are unchanged key for key.
            **({"engine": repair_report.diagnostics.get("engine", ""),
                RESULT_V5_KEY: repair_report.diagnostics[RESULT_V5_KEY]}
               if repair_report.diagnostics.get(RESULT_V5_KEY) else {}),
            **goal_record,
        },
    }
