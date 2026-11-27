# Responsibility: Turn the FEA ladder's MESH-2 refusal - "the mesh failed at these faces, and a
# check had flagged the CAD there" - into ONE scoped repair of exactly those faces and ONE re-mesh.
# Boundaries: the adapter only. It never meshes, never sizes a machine and never chooses one: the
# single re-mesh is the graph's existing builder/executor pass, and the machine is the one the
# failing attempt already ran on, READ off its workspace. The repair itself is cad/repair's
# (`repair_step_file`), reached with a goal and a focus and nothing else new.
# Collaborates with: pipeline/graph.py (route_after_executor, route_after_handback),
# pipeline/repair_promote.py (the one promotion path), cad/repair/conservative.py.
"""THE MESHER HANDS ITS FAILING FACES BACK TO REPAIR, ONCE.

Repair's hardest problem is that it is guessing: it fixes what a kernel scan dislikes and hopes the
mesher agrees. MESH-2 is the one moment the mesher says exactly which faces it could not mesh AND a
geometry check agrees they are suspect (`engines/fea_driver.defect_at_failure`). That is not a
guess, so it is worth one more pass - scoped to those faces, with the meshing goal declared - and
one more mesh.

WHAT THIS IS NOT.

* NOT A LOOP. `repair_handback` on state is the latch, written the moment this node runs whatever
  it concluded, so a job gets one handback ever. A re-mesh that fails at the same faces ends the
  run on the route it would have taken before.
* NOT AN ESCALATION. The re-mesh is the graph's ordinary next pass, and the machine chooser
  receives nothing it did not receive the first time - this module reads the failing attempt's
  decision and never calls the chooser. Big/XL VM escalation is unreachable from repair by
  construction, and the plan treats that as a hard invariant rather than a preference.
* NOT A SECOND VERDICT. A handback that commits nothing ends the run on the refusal the mesher
  already wrote, with its own record attached. An honest "that did not help either" is a result;
  looping is not.
* NOT A REPAIR DECISION. Whether the faces are repairable is `cad/repair`'s to answer. This module
  decides only that the question is worth asking once, and who pays for the answer (the job's
  existing `CAD_REPAIR_TIMEOUT_S` clock).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.contracts import mesh_machine
from meshpipeline.contracts.pipeline_state import PipelineState

logger = logging.getLogger(__name__)

#: `Face_<k>`, the name gmsh gives a SINGLE PART's face - OpenCASCADE's 1-based face order, which
#: is the same order `cad/repair` indexes faces in (`regions.Region.face_indices`, built from
#: `localize`'s 1-based `TopTools_IndexedMapOfShape` walk). An assembly's faces carry their
#: sidecar names instead (`engines/gmsh/fea_adapter.face_names_of`), and those are not part faces.
FACE_NAME: Final = re.compile(r"^Face_(\d+)$")

#: Why an assembly is refused a handback. Its face names come from the assembly sidecar, not from
#: one part's kernel face order, so mapping them to indices would scope the repair to whichever
#: faces happened to share a number - a repair aimed, with confidence, at the wrong geometry.
ASSEMBLY_FACES: Final = "assembly face names are not part faces"

#: The posture's one on-value. The setting is registered by the posture task, so it is read with
#: `getattr` and defaulted: unset - every deployment that has not opted in - is off, and so is any
#: other value, which is logged rather than guessed at.
LIVE: Final = "live"

#: Why no handback was taken. A closed set: the route logs it and the record carries it, so a
#: reader never has to infer which condition failed.
NOT_LIVE: Final = "posture_off"
NOT_A_CAD_DEFECT: Final = "not_a_cad_defect_refusal"
ALREADY_HANDED_BACK: Final = "already_handed_back"
NO_FACES: Final = "no_faces_named"
#: The customer chose "Mesh only" (pipeline/repair_goal.py): no repair of any kind, this one too.
REPAIR_OFF: Final = "repair_off"

#: The gates MESH-2 can arrive under. BOTH are the same refusal: `engines/fea_driver` writes it as
#: `fea_cad_defect`, and `pipeline/executor` keeps that gate when no mesh reached the disk but
#: reports the ladder's quality gate when one did (`_driver_verdict`), merging the same
#: `cad_defect` facts in either way. A closed set rather than "any gate with the facts", so a
#: future refusal that happens to carry a `cad_defect` key cannot silently buy a handback.
MESH2_GATES: Final = ("fea_cad_defect", "fea_quality")

#: Files the bounded repair rewrites, as `repair_attempt` has it: a handback is that repair again.
_REPAIRABLE_SUFFIXES: Final = (".step", ".stp")


def posture() -> str:
    """The handback posture, fail-closed: anything but "live" is "off"."""
    raw = str(getattr(rcfg, "CAD_REPAIR_MESH_HANDBACK", "off") or "off")
    if raw not in (LIVE, "off"):
        logger.warning("CAD_REPAIR_MESH_HANDBACK=%r is not a posture; reading it as off", raw)
    return LIVE if raw == LIVE else "off"


def cad_defect_of(state: PipelineState) -> dict:
    """The MESH-2 facts the executor left on state (`{}` when the refusal was not MESH-2).

    Read off `executor_failure_facts`, which is where both arrival paths put it, and only for a
    FAILED attempt: a passing mesh clears the facts, but reading them unconditionally would let a
    stale dict from an earlier attempt buy a handback on a mesh that worked.
    """
    if state.get("executor_success") is True:
        return {}
    if str(state.get("executor_failed_gate") or "") not in MESH2_GATES:
        return {}
    facts = state.get("executor_failure_facts") or {}
    defect = facts.get("cad_defect") if isinstance(facts, dict) else None
    return dict(defect) if isinstance(defect, dict) and defect.get("faces") else {}


def focus_of(faces) -> tuple[tuple[int, ...], str]:
    """`(focus, "")` for a single part's `Face_<k>` names, or `((), reason)`.

    EVERY name must match, not merely one: a part list mixing sidecar names with `Face_k` is an
    assembly whose numbering means something else, and taking the matching subset would scope the
    repair to a guess. Sorted and de-duplicated so the focus is a set of loci, not a message order.
    """
    names = [str(f) for f in (faces or [])]
    if not names:
        return (), NO_FACES
    matched = [FACE_NAME.match(n) for n in names]
    if not all(matched):
        return (), ASSEMBLY_FACES
    indices = sorted({int(m.group(1)) for m in matched if m is not None})
    if any(i < 1 for i in indices):
        # The kernel face order is 1-based; a 0 is not face zero, it is a name we misread.
        return (), NO_FACES
    return tuple(indices), ""


def machine_of(state: PipelineState) -> dict:
    """The machine the FAILING attempt ran on: READ off its workspace, never decided here.

    `{}` means the Cloud Run mesh job, which is what no decision file has always meant
    (`contracts/mesh_machine.read`). This function is the whole of the handback's relationship
    with the machine question - it calls no chooser, states no size and passes the chooser no
    input it did not already have - which is why the re-mesh cannot come out on a bigger machine
    than the attempt that failed.
    """
    workspace = str(state.get("openfoam_workspace") or "")
    if not workspace:
        return {}
    return mesh_machine.read(workspace) or {}


def wanted(state: PipelineState) -> tuple[bool, str]:
    """Whether this refusal gets a handback, and why not when it does not.

    ALL of: the posture is live; the refusal is MESH-2; this job has had no handback yet; and
    every face named is a single part's `Face_<k>`.
    """
    if posture() != LIVE:
        return False, NOT_LIVE
    from meshpipeline.pipeline.repair_goal import repair_off
    if repair_off(state):
        return False, REPAIR_OFF
    defect = cad_defect_of(state)
    if not defect:
        return False, NOT_A_CAD_DEFECT
    if state.get("repair_handback"):
        return False, ALREADY_HANDED_BACK
    focus, why = focus_of(defect.get("faces"))
    if not focus:
        return False, why
    return True, ""


def _record(*, faces, outcome: str, committed: bool, machine: dict, report_ref: str = "",
            why: str = "", report: dict | None = None) -> dict:
    """The one shape `repair_handback` ever takes. JSON-only: it rides a LangGraph checkpoint."""
    out = {"faces": [int(f) for f in faces], "outcome": outcome, "committed": bool(committed),
           "report_ref": report_ref, "machine": dict(machine)}
    if why:
        out["why"] = why
    if report:
        out["report"] = report
    return out


async def _parked_trace(job_id: str, measured: object) -> dict | None:
    """The handback repair's investigation trace (CAD_REPAIR_INVESTIGATION_TRACE), its lines
    moved to the job's storage exactly as the attempt node's are; None when it carries none. The
    outcome table and the location are small, so they ride the record; the lines never do."""
    if not isinstance(measured, dict) or not isinstance(measured.get("investigation_trace"), dict):
        return None
    from meshpipeline.pipeline.repair_attempt import _park_trace

    await _park_trace(job_id, measured)
    return dict(measured["investigation_trace"])


async def _scoped_repair(state: PipelineState, *, focus_faces: tuple[int, ...]) -> dict:
    """Run the bounded repair on the CURRENT geometry, scoped to `focus_faces`, and promote it.

    The seam the node is tested through. Returns
    `{"committed", "outcome", "report_ref", "report", "state"}` - `state` being the promotion's
    own update, which is empty unless the repair committed.

    THE CLOCK IS THE ONE THE ATTEMPT NODE USES (`CAD_REPAIR_TIMEOUT_S`, `repair_attempt`'s
    pattern): a handback is that same bounded repair, asked a narrower question, so it may not
    invent a budget of its own. The kernel offers no interruption point, so an expired thread is
    abandoned and the run carries on - here, onto the mesher's own refusal.
    """
    import asyncio

    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
    )
    from meshpipeline.cad.repair.contracts import RepairGoal
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = str(state.get("job_id", "unknown"))
    if not rcfg.CAD_REPAIR_ENABLED:
        return {"committed": False, "outcome": "off", "report_ref": "", "state": {}}
    current = _materialized(state)
    if current is None or not current.path or not Path(current.path).exists():
        return {"committed": False, "outcome": "no_geometry", "report_ref": "", "state": {}}
    source = Path(current.path)
    if source.suffix.lower() not in _REPAIRABLE_SUFFIXES:
        return {"committed": False, "outcome": "format", "report_ref": "", "state": {}}

    # BESIDE THE GEOMETRY IT REPAIRS, with a name of its own: a resumed run re-derives the same
    # file, and the handback's output must not collide with the inbound repair's.
    destination = source.with_name(f"{source.stem}-handback{source.suffix}")
    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(repair_step_file, source, destination,
                              goal=RepairGoal.meshing, focus_faces=focus_faces, engine="v3"),
            timeout=rcfg.CAD_REPAIR_TIMEOUT_S)
    except TimeoutError:
        logger.warning("node_repair_handback: the scoped repair exceeded %.1fs - the mesher's "
                       "refusal stands - job_id=%s", rcfg.CAD_REPAIR_TIMEOUT_S, job_id)
        return {"committed": False, "outcome": "timeout", "report_ref": "", "state": {}}
    except RepairUnavailable as exc:
        # OURS, never a verdict on the file: the posture the focus needs is off, or no kernel.
        logger.info("node_repair_handback: the scoped repair could not run here (%s) - job_id=%s",
                    exc, job_id)
        return {"committed": False, "outcome": "unavailable", "report_ref": "", "state": {}}
    except RepairRefused as exc:
        refused: dict = {"refused_because": str(exc)}
        trace = await _parked_trace(job_id, exc.measurements)
        if trace is not None:
            refused["investigation_trace"] = trace
        return {"committed": False, "outcome": "refused", "report_ref": "",
                "report": refused, "state": {}}

    measured: dict = {m.name: m.value for m in report.measurements}
    parked = await _parked_trace(job_id, measured)
    _goal = measured.get("goal")
    _refusals = measured.get("refusals")
    # THE REPORT'S OWN HEADLINES, not the report: a full repair report carries entity lists and
    # survivor records, and this record rides a LangGraph checkpoint on every later node.
    summary = {"stopped_because": str(measured.get("stopped_because", "")),
               "resolved": bool(measured.get("resolved", False)),
               "operations_kept": int(measured.get("operations_kept") or 0),
               "operations_attempted": int(measured.get("operations_attempted") or 0),
               "goal": dict(_goal) if isinstance(_goal, dict) else {},
               "refusals": [dict(r) for r in _refusals if isinstance(r, dict)]
                           if isinstance(_refusals, list) else []}
    if parked is not None:
        summary["investigation_trace"] = parked

    from meshpipeline.pipeline.repair_promote import PromotionRefused, promote
    try:
        from meshpipeline.pipeline.repair_attempt import _derived_source
        repaired = _derived_source(current, destination)
        update = await asyncio.wait_for(
            asyncio.to_thread(promote, state, repaired=repaired,
                              engine=str(state.get("engine", "") or ""),
                              attempt=int(state.get("retry_count", 0) or 0)),
            timeout=rcfg.CAD_REPAIR_TIMEOUT_S)
    except TimeoutError:
        # A PROOF THAT DID NOT FINISH IS NOT A REFUSAL, and bytes nobody proved may not be meshed.
        logger.warning("node_repair_handback: the staging proof exceeded %.1fs - the mesher's "
                       "refusal stands - job_id=%s", rcfg.CAD_REPAIR_TIMEOUT_S, job_id)
        return {"committed": False, "outcome": "not_proved", "report_ref": "",
                "report": summary, "state": {}}
    except PromotionRefused as exc:
        logger.info("node_repair_handback: promotion refused (%s) - job_id=%s", exc, job_id)
        return {"committed": False, "outcome": "not_promoted", "report_ref": "",
                "report": {**summary, "not_promoted_because": str(exc)}, "state": {}}

    return {"committed": True, "outcome": "committed",
            "report_ref": f"repair_handback:{job_id}", "report": summary, "state": dict(update)}


async def node_repair_handback(state: PipelineState) -> dict:
    """One scoped repair of the faces the mesher named, then one re-mesh or an honest end.

    ALWAYS RETURNS A `repair_handback` RECORD, including when it declined: the record is the latch
    that holds the job to one handback, so a node that wrote `{}` on a refusal would hand the job
    back again on its next refusal, which is the loop this adapter exists not to be.
    """
    job_id = str(state.get("job_id", "unknown"))
    machine = machine_of(state)
    ok, why = wanted(state)
    if not ok:
        logger.info("node_repair_handback: declined (%s) - job_id=%s", why, job_id)
        return {"repair_handback": _record(faces=(), outcome="declined", committed=False,
                                           machine=machine, why=why)}

    focus, _why = focus_of(cad_defect_of(state).get("faces"))
    logger.warning("node_repair_handback: the mesh failed at %s - one scoped repair of those "
                   "faces, on %s - job_id=%s", focus,
                   machine.get("machine") or "the Cloud Run mesh job", job_id)
    try:
        outcome = await _scoped_repair(state, focus_faces=focus)
    except Exception as exc:  # noqa: BLE001 - a repair that breaks is never the file's fault
        logger.warning("node_repair_handback: the scoped repair failed (%s: %s) - the mesher's "
                       "refusal stands - job_id=%s", type(exc).__name__, exc, job_id)
        return {"repair_handback": _record(faces=focus, outcome="failed", committed=False,
                                           machine=machine, why=type(exc).__name__)}

    record = _record(faces=focus, outcome=str(outcome.get("outcome") or "no_commit"),
                     committed=bool(outcome.get("committed")), machine=machine,
                     report_ref=str(outcome.get("report_ref") or ""),
                     report=outcome.get("report") or None)
    if not record["committed"]:
        # NOTHING CHANGED, so the mesher's own refusal is still the truthful end of this run. The
        # executor's facts are left exactly as they are: the user's sentence stays the mesher's.
        logger.warning("node_repair_handback: the scoped repair committed nothing (%s) - the "
                       "mesher's refusal stands - job_id=%s", record["outcome"], job_id)
        return {"repair_handback": record}

    # THE REPAIRED GEOMETRY IS THE RUN'S GEOMETRY NOW (pipeline/repair_promote: one handle, the
    # approved one, with what it replaced recorded as lineage) - so the run goes back through the
    # admission gate, which re-measures it, and on to ONE more build and ONE more mesh. The
    # executor's verdict is cleared because it is a verdict about bytes nobody is meshing any
    # more; leaving it would let `route_after_executor` re-read the old failure after the re-mesh.
    logger.warning("node_repair_handback: the scoped repair committed - one re-mesh on %s "
                   "- job_id=%s", machine.get("machine") or "the Cloud Run mesh job", job_id)
    return {**dict(outcome.get("state") or {}),
            "repair_handback": record,
            "executor_success": False,
            "executor_failed_gate": "",
            "executor_failure_cause": "",
            "executor_failure_facts": {},
            "geometry_unsuitable_reason": ""}
