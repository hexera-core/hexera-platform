# Responsibility: THE CFD LADDER'S LAST RUNG - when every engine the run tried failed to make a
# valid flow mesh of the surface as drawn, mesh a WRAPPED copy of it once more (cad/surface_wrap),
# with the best flow engine, and say how far the wrapped surface lies from the CAD.
# Owns: when the rung runs (wanted), which engine meshes the wrapped surface, the graph node that
# starts it, the request the run's staging reads, the delivered mesh's caveat and its words.
# Boundaries: decisions over the run's state and the engine roster; the wrap itself is
# cad/surface_wrap's, the meshing the builder's. It never alters the customer's file.
# Collaborates with: pipeline/graph.py (routes the end of a failed run here), pipeline/engine_fallback.py
# (the ranked engines, the failure classes, the time a run needs), agents/builder/attempt.py (stages
# the wrap), application/final_result.py and pipeline_run.py (the caveat), render/mesh_report.py.
"""THE WRAP RUNG (docs/RULES.md LAD-cfd-5..7).

FEA has fTetWild as the rung that always meshes (LAD-fea-9). CFD had none: a surface whose faces
cross, whose shells overlap or leave gaps, failed every engine, and the run ended with no mesh.
Commercial meshers wrap such a surface first (STAR-CCM+'s surface wrapper, Fluent Meshing's
wrap): a closed, clean skin laid over the CAD, gaps narrower than the wrap distance closed.

This rung runs only when the run would otherwise END WITH NO MESH - every regular attempt and every
switch of the ladder spent, and no earlier attempt sound (LAD-cfd-3 comes first). So it never
changes a run that delivers today. It runs once, on the best of snappyHexMesh and cfMesh that the
approved request admits (the run's own measured ranking); a user-named engine is kept when it is
one of them, and never swapped. A failure on our side (the service, the request itself, a
review) is no geometry problem, and gets no wrap.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_KEY = "cfd_wrap"
RULE_RUNG = "LAD-cfd-5"
RULE_DISTANCE = "LAD-cfd-6"
RULE_REPORT = "LAD-cfd-7"
#: The engines that mesh a wrapped surface, in the rung's own order: the two flow engines that build
#: body-fitted hex-dominant meshes with layers from any closed surface, then gmsh's tetrahedra
#: filled straight against the skin's own triangles (no remesh, no layers: the DEGRADED step that
#: cannot realistically fail on a closed, manifold, smooth skin). VMTK needs a tube.
WRAP_ENGINES = ("snappy", "cfmesh")
DEGRADED_ENGINE = "gmsh"
#: The flows the rung serves.
WRAP_FLOWS = ("external", "internal")
#: The caveat kind a delivery carries when its mesh was built on the wrapped surface.
WRAPPED_SURFACE = "wrapped_surface"

#: Failures that are not the geometry's: another surface could not change them.
_NOT_THE_GEOMETRY = frozenset({"run_infrastructure", "domain_extent", "boundary_type",
                               "contract_mismatch", "region_split", "review_incomplete"})


def record(state: Mapping) -> dict:
    rec = state.get(STATE_KEY)
    return dict(rec) if isinstance(rec, Mapping) else {}


def tried(state: Mapping) -> bool:
    """The run is on the wrap rung (or was): every attempt from it on meshes the wrapped surface,
    and a failure there ends the run."""
    return bool(record(state).get("tried"))


def flow_of_state(state: Mapping) -> str:
    from meshpipeline.engines.capability import flow_of
    return flow_of(state.get("purpose", ""))


def why_not(state: Mapping, *, now: float | None = None) -> str:
    """'' when the wrap rung runs for this failed run, else the reason it does not (for the log)."""
    import meshpipeline.settings.policy as polcfg
    if not polcfg.CFD_WRAP_ENABLED:
        return "the wrap rung is switched off"
    if tried(state):
        return "the run already meshed a wrapped surface"
    if state.get("executor_success"):
        return "the last attempt did not fail"
    if state.get("user_dispute"):
        return "a dispute rebuild keeps the mesh it disputes"
    if state.get("api_failure"):
        return "the run stopped on our side, not on the geometry"
    if state.get("builder_stop") and not _time_spent(state):
        return "the run stopped on our side, not on the geometry"
    flow = flow_of_state(state)
    if flow not in WRAP_FLOWS:
        return f"no wrap for {flow or 'an unknown flow'}"
    cause = str(state.get("executor_failure_cause") or "")
    if cause in _NOT_THE_GEOMETRY:
        return f"the failure ({cause}) is not the geometry's"
    facts = state.get("executor_failure_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    if state.get("geometry_unsuitable_reason") and set(facts.get("phases") or ()) - {"measured"}:
        return "the request asks for something these engines cannot build: a wrap changes nothing about that"
    from meshpipeline.pipeline import engine_fallback as lad
    if lad.refused_by_design(state):
        return "the engines refuse this kind of file by design: a wrap changes nothing about that"
    if not rung_steps(state):
        return "no flow engine that meshes a wrapped surface takes this request"
    # NO TIME CHECK: the rung brings its own time (CFD_WRAP_TIME_S, reserved for it), so a run whose
    # earlier attempts ran out of time still reaches it (LAD-cfd-5)
    return ""


def _time_spent(state: Mapping) -> bool:
    try:
        from meshpipeline.agents.builder.attempt import STOPPED_TIME_SPENT
    except Exception:  # noqa: BLE001 - an unreadable stop is not a time-out
        return False
    return str(state.get("builder_stop") or "") == STOPPED_TIME_SPENT


def wanted(state: Mapping) -> bool:
    try:
        why = why_not(state)
    except Exception:  # noqa: BLE001 - a question the rung cannot answer ends the run as before
        logger.warning("cfd wrap: could not tell whether the wrap rung runs", exc_info=True)
        return False
    if why:
        logger.info("cfd wrap: no wrap rung (%s) - job_id=%s", why, state.get("job_id"))
    return not why


def rung_steps(state: Mapping) -> list[str]:
    """THE RUNG'S ENGINES, in order (LAD-cfd-5): the user's own first when they named snappyHexMesh
    or cfMesh; then the two in the run's ranked order (ENG-5); then gmsh's tetrahedra against the
    skin as it is (the degraded step) - each only when it admits the whole approved request. Inside
    the rung the next engine takes over when one fails on the wrapped surface, and the delivery
    says so: the skin is clean, so the engine is the only thing left to change."""
    from meshpipeline.pipeline import engine_fallback as lad
    able = list(lad.able_engines(state))
    out: list[str] = []
    approved = lad.approved_engine(state)
    if not lad.may_switch_on_its_own(state) and approved in WRAP_ENGINES:
        out.append(approved)
    for name in [*able, *WRAP_ENGINES]:
        if name in WRAP_ENGINES and name not in out and (name in able or name == approved):
            out.append(name)
    if DEGRADED_ENGINE in able and DEGRADED_ENGINE not in out:
        out.append(DEGRADED_ENGINE)
    return out


def wrap_engine(state: Mapping) -> str:
    """The engine of the rung's step now due ('' when none is)."""
    steps = record(state).get("steps") or rung_steps(state)
    step = int(record(state).get("step", -1)) + 1
    return str(steps[step]) if 0 <= step < len(steps) else ""


def next_step(state: Mapping) -> bool:
    """On the rung, the step that just failed leaves another engine to try on the same skin -
    unless the failure was ours (the service), which another engine does not change."""
    rec = record(state)
    if not rec.get("tried") or state.get("executor_success") or state.get("api_failure"):
        return False
    if str(state.get("executor_failure_cause") or "") in _NOT_THE_GEOMETRY:
        return False
    steps = list(rec.get("steps") or [])
    return int(rec.get("step", 0)) + 1 < len(steps)


def _failed_engines(state: Mapping) -> list[str]:
    from meshpipeline.pipeline import engine_fallback as lad
    return [e for e in lad.tried_engines(state) if e]


def opening_note(state: Mapping, engine: str) -> str:
    """What the user reads as the rung starts."""
    from meshpipeline.pipeline import engine_fallback as lad
    names = [lad.engine_label(e) for e in _failed_engines(state)]
    who = (f"{names[0]} could not" if len(names) == 1 else
           f"Neither {names[0]} nor {names[1]} could" if len(names) == 2 else
           f"None of {', '.join(names[:-1])} and {names[-1]} could") if names else "No engine could"
    failure = lad.classify(state)
    why = f": {failure.reason}" if failure is not None else ""
    return (f"{who} mesh the surface as drawn{why}. Last try: I am wrapping it into a clean, closed "
            f"surface that follows your CAD to within a measured distance, closing only gaps "
            f"narrower than that, and meshing the wrap with {lad.engine_label(engine)}. Your file "
            "is not changed, and the result will say how far the wrap lies from your CAD.")


async def node_cfd_wrap(state) -> dict:
    """THE WRAP RUNG STARTS (LAD-cfd-5): the next attempt meshes a wrapped copy of the surface, on
    the wrap engine, fresh. Its staging reads the request this sets (agents/builder/attempt)."""
    from meshpipeline.engines.registry import resolve_engine_params
    from meshpipeline.pipeline import engine_fallback as lad
    from meshpipeline.pipeline.engine_select import _publish

    job_id = str(state.get("job_id", "") or "unknown")
    prev = record(state)
    steps = list(prev.get("steps") or rung_steps(state))
    step = int(prev.get("step", -1)) + 1 if prev.get("tried") else 0
    if step >= len(steps):                             # the route asked first
        return {}
    engine = steps[step]
    failure = lad.classify(state)
    ladder = lad.with_attempt(state, failure)
    attempt = int(state.get("retry_count", 0) or 0) + 1
    frm = str(state.get("engine") or "")
    if failure is not None:
        ladder = lad.with_switch(ladder, attempt=attempt, frm=frm, to=engine, failure=failure,
                                 why="every engine failed on the surface as drawn: the wrap rung",
                                 changes=("meshed on a wrapped surface",))
    failed = failure.as_dict() if failure is not None else {}
    done = [dict(d) for d in (prev.get("done") or [])]
    if prev.get("tried"):
        done.append({"engine": prev.get("engine"), "reason": failed.get("reason"),
                     "cause": failed.get("cause")})
    rec: dict[str, Any] = {"tried": True, "rule": RULE_RUNG, "engine": engine, "attempt": attempt,
           "flow": flow_of_state(state),
           "after": prev.get("after") if prev.get("tried") else _failed_engines(state),
           "failure": prev.get("failure") if prev.get("tried") else failed,
           "steps": steps, "step": step, "done": done,
           "degraded": engine == DEGRADED_ENGINE}
    try:
        from meshpipeline.contracts import decision_record as dr
        dr.cite(job_id, stage="engine_ladder", decided="wrap rung", value=engine, rule=RULE_RUNG,
                inputs={"attempt": attempt, "failed_engines": rec["after"],
                        "failure": failed.get("cause")},
                words=(f"every engine failed on the surface as drawn; attempt {attempt} meshes a "
                       f"wrapped copy of it with {engine}"))
    except Exception:  # noqa: BLE001 - a record never changes the rung
        logger.debug("cfd wrap: rung not recorded", exc_info=True)
    logger.warning("node_cfd_wrap: every engine failed on the surface as drawn - attempt %d meshes "
                   "a wrapped surface with %s - job_id=%s", attempt, engine, job_id)
    note = (opening_note(state, engine) if step == 0 else
            step_note(str(prev.get("engine") or ""), engine, failed.get("reason") or ""))
    await _publish(job_id, note, f"cfd-wrap:{attempt}")
    # THE RUNG'S OWN TIME (LAD-cfd-5): every step starts with at least CFD_WRAP_TIME_S ahead of it,
    # whatever the earlier attempts spent - a run that timed out still gets its last rung
    import meshpipeline.settings.policy as polcfg
    ahead = time.time() + float(polcfg.CFD_WRAP_TIME_S)
    deadlines = {k: max(float(state.get(k) or 0.0), ahead)
                 for k in ("pipeline_deadline_epoch", "builder_deadline_epoch") if state.get(k)}
    return {
        **deadlines,
        "builder_stop": "",
        "engine": engine,
        "engine_params": resolve_engine_params(engine, dict(state.get("engine_params") or {})),
        "builder_mode": "initial",
        "builder_noop_count": 0,
        # a MEASURED refusal of the surface as drawn (it crosses itself) is what the wrap answers:
        # the wrapped surface is meshed, not refused again
        "geometry_unsuitable_reason": "",
        "classifier_result": {**dict(state.get("classifier_result") or {}),
                              "summary": (f"This attempt meshes a WRAPPED copy of the surface with "
                                          f"{lad.engine_label(engine)}: every engine failed on the "
                                          "surface as drawn. The staged surface is already closed and "
                                          "clean; plan as for any surface of this part."),
                              "error_source": "cfd_wrap"},
        "engine_ladder": ladder,
        STATE_KEY: rec,
        **lad._ladder_step(state, attempt=attempt, switch_to=engine),
    }


# ----------------------------------------------------------------- the request, the report --
def step_note(frm: str, to: str, reason: str) -> str:
    """What the user reads when the next engine takes over the wrapped surface."""
    from meshpipeline.pipeline import engine_fallback as lad
    why = f" ({reason})" if reason else ""
    if to == DEGRADED_ENGINE:
        return (f"{lad.engine_label(frm)} could not mesh the wrapped surface either{why}. Last step: "
                f"{lad.engine_label(to)} fills the same wrapped surface with tetrahedra, without wall "
                "layers - a degraded mesh, said as one beside it.")
    return (f"{lad.engine_label(frm)} could not mesh the wrapped surface{why}. Trying "
            f"{lad.engine_label(to)} on the same wrapped surface.")


def write_request(workspace: Path, state: Mapping) -> bool:
    """The wrap request beside the run's surface (cad/surface_wrap.WRAP_REQUEST_FILE), for every
    staging route to read; removed when the run is not on the rung. True when one was written."""
    from meshpipeline.cad.surface_wrap import WRAP_REQUEST_FILE
    path = Path(workspace) / WRAP_REQUEST_FILE
    if not tried(state):
        if path.exists():
            path.unlink()
        return False
    rec = record(state)
    path.write_text(json.dumps({"flow": rec.get("flow"), "engine": rec.get("engine"),
                                "rule": RULE_RUNG}), encoding="utf-8")
    return True


def read_request(workspace: Path) -> dict | None:
    from meshpipeline.cad.surface_wrap import WRAP_REQUEST_FILE
    path = Path(workspace) / WRAP_REQUEST_FILE
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def read_report(workspace: Path | str | None) -> dict | None:
    from meshpipeline.cad.surface_wrap import WRAP_REPORT_FILE
    if not workspace:
        return None
    try:
        rec = json.loads((Path(workspace) / WRAP_REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def delivered(state: Mapping) -> dict | None:
    """The wrap the delivered mesh was built on: the rung's record and the staging's measured
    report, or None when the delivered mesh is not a wrapped one."""
    if not tried(state):
        return None
    rep = read_report(state.get("openfoam_workspace"))
    if not rep:
        return None
    return {**record(state), "report": rep}


# ------------------------------------------------------------------------------ the words ----
def caveat_of(wrap: Mapping | None, *, executor_success: bool, failed_gate: str) -> dict | None:
    """THE one predicate for the wrapped-surface caveat (application/final_result): None unless the
    executor validated a mesh built on the wrap, with no gate failed."""
    if not wrap or not executor_success or failed_gate:
        return None
    rep = wrap.get("report") if isinstance(wrap.get("report"), Mapping) else {}
    if not rep:
        return None
    keep = ("distance_m", "cube_m", "max_m", "p95_m", "max_on_cad_m", "p95_on_cad_m",
            "bridged_spots", "spots", "cad_to_skin_max_m", "smoothed_faces", "dry_faces",
            "patches", "words", "openings_moved")
    return {"kind": WRAPPED_SURFACE, "rule": RULE_REPORT, "engine": wrap.get("engine"),
            "after": list(wrap.get("after") or []),
            "done": [dict(d) for d in (wrap.get("done") or []) if isinstance(d, Mapping)],
            "degraded": bool(wrap.get("degraded")),
            "reason": str((wrap.get("failure") or {}).get("reason") or ""),
            **{k: rep.get(k) for k in keep if rep.get(k) is not None}}


def _mm(m: Any) -> str:
    from meshpipeline.cad.surface_wrap import _mm as mm
    try:
        return mm(float(m))
    except (TypeError, ValueError):
        return "an unmeasured distance"


def caveat_lines(c: Mapping) -> list[str]:
    """The delivery's lines for a mesh built on the wrapped surface, in plain words."""
    from meshpipeline.pipeline import engine_fallback as lad
    words = str(c.get("words") or "").strip() or "Meshed on a wrapped surface"
    lines = [f"  - {words[0].upper()}{words[1:]}."]
    names = [lad.engine_label(str(e)) for e in (c.get("after") or []) if e]
    reason = str(c.get("reason") or "").strip()
    if names:
        lines.append(f"    Why: {', '.join(names)} could not mesh the surface as drawn"
                     + (f" ({reason})" if reason else "") + ".")
    gap = 2.0 * float(c.get("distance_m") or 0.0)
    if gap > 0:
        lines.append(f"    The wrap closed only gaps narrower than {_mm(gap)} and left out faces the "
                     "flow cannot reach (faces that cross, doubled faces, the inside of overlapping "
                     "shells).")
    for s in list(c.get("spots") or [])[:3]:
        if not isinstance(s, Mapping):
            continue
        at = ", ".join(f"{float(v) * 1000:.4g}" for v in (s.get("centre_m") or []))
        lines.append(f"    Bridged at ({at}) mm on {s.get('patch')}: the wrap lies up to "
                     f"{_mm(s.get('max_m'))} from your CAD there.")
    more = int(c.get("bridged_spots") or 0) - min(3, len(c.get("spots") or []))
    if more > 0:
        lines.append(f"    ...and {more} more bridged spot{'s' if more != 1 else ''}, listed in the "
                     "mesh report.")
    for m in c.get("openings_moved") or []:
        if isinstance(m, Mapping) and m.get("opening"):
            lines.append(f"    {m['opening']}: its vessel narrows below what the wrap can draw, so the "
                         f"opening is placed where the vessel still is, {_mm(m.get('moved_m'))} from "
                         "where it is drawn.")
    for d in c.get("done") or []:
        if isinstance(d, Mapping) and d.get("engine"):
            lines.append(f"    {lad.engine_label(str(d['engine']))} could not mesh the wrapped surface"
                         + (f" ({d.get('reason')})" if d.get("reason") else "") + "; "
                         + f"{lad.engine_label(str(c.get('engine') or ''))} took over.")
    if c.get("degraded"):
        lines.append("    DEGRADED: this is a tetrahedral mesh without wall layers, the rung's last "
                     "step. Use it for a first look; near-wall results (shear, heat transfer) need "
                     "the layered mesh a fixed CAD gives.")
    lines.append("    Every face of the wrap is named after the nearest face of your CAD, so your "
                 "boundaries keep their names.")
    return lines


WRAPPED_SURFACE_CLOSING = (
    "The mesh passed every automatic check. It follows the wrap, not your exact CAD: for a mesh on "
    "your exact surface, close the gaps and remove the crossing faces in the CAD, then run it again.")


__all__ = ["DEGRADED_ENGINE", "RULE_DISTANCE", "RULE_REPORT", "RULE_RUNG", "STATE_KEY",
           "WRAPPED_SURFACE", "WRAPPED_SURFACE_CLOSING", "WRAP_ENGINES", "caveat_lines", "caveat_of",
           "delivered", "next_step", "node_cfd_wrap", "opening_note", "read_report", "read_request",
           "record", "rung_steps", "step_note", "tried", "wanted", "why_not", "wrap_engine",
           "write_request"]
