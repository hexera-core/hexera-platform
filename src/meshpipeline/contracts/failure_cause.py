# Responsibility: Name what actually stopped a run, and say it to the user in plain words.
# Owns: the failure-cause vocabulary, which causes another attempt can change, and each cause's sentences.
# Boundaries: pure words and rules; it measures nothing, reads no workspace and decides no status.
"""What stopped a run, said plainly.

A failed gate used to reach the user as one sentence, whatever the gate was: "The mesh did not
meet the required quality checks." Job ac1daa3e meshed a car body for 26 minutes over two attempts
and was told exactly that - but the gate that failed was a NAMING mismatch (the boundary approved
as 'car wall' was written as 'car_wall'), knowable before any mesh ran, and no quality check had
failed at all. A user who reads "quality" goes looking for a problem in their geometry that is not
there, and a retry spends another 13 minutes hitting the same wall.

So each distinct cause has its own name here, its own sentence (what failed, with the numbers and
the limit), its own next step, and a flag for whether another attempt could change the outcome.
The gate that fails names the cause (engines/gates.py) and hands over the facts; this module only
words them.

ONE VOCABULARY, THREE LEVELS, each naming a different thing and none restating another:
* the failure CATEGORY (application/final_result.FailureCategory) - the coarse, persisted outcome
  of the whole run (gate_failed, input_rejected, worker_lost, timed_out, ...);
* the failure CLASS (errors.FailureClass) - whose problem a failure is, and the one sentence for
  the categories that class owns (a refused input, a lost worker, a provider outage);
* the failure CAUSE (here) - WHICH gate failure, when the category is gate_failed; and, when the
  review of a mesh did not conclude (internal_pipeline_failure), that it did not and why - so our
  own failure still names what failed instead of "something went wrong on our side".
Where a cause and a class describe the same thing, the class's sentence is the one said: a refused
geometry is worded by errors.DOMAIN_REJECTED, never a second time here.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class FailureCause(StrEnum):
    #: A boundary the user approved is not in the mesh under that name or type. Our authoring
    #: disagreed with the approval, so it is our fault and deterministic: the same inputs write
    #: the same wrong case every time. Facts: missing, renamed {approved: written}, mistyped
    #: [{name, declared, got}], extra, missing_roles, present, before_meshing.
    CONTRACT_MISMATCH = "contract_mismatch"
    #: A boundary was in the case, but the mesher lost it: it came out with no faces (a port
    #: sealed over by cells larger than the opening). A real meshing problem. Facts: patches,
    #: detail.
    PATCH_NOT_CAPTURED = "patch_not_captured"
    #: A boundary came out with the wrong OpenFOAM type (a symmetry plane written as a plain
    #: patch). Our emission step, but it runs inside the mesher sequence, so it is not certain a
    #: new attempt repeats it. Facts: mistyped [{name, declared, want, got}].
    BOUNDARY_TYPE = "boundary_type"
    #: The cells are too badly shaped to trust (non-orthogonality, skewness, element quality,
    #: broken cells). Facts: checks [{key, label, measured, op, threshold}], fatal, unmeasured.
    MESH_QUALITY = "mesh_quality"
    #: Too few cells across the narrowest passage. Facts: cells_across, needed.
    UNDER_RESOLVED = "under_resolved"
    #: More cells than one job may build. Facts: cells, limit.
    CELL_BUDGET = "cell_budget"
    #: The far-field box around the body is not the size that was asked for. Facts: misses
    #: [{direction, requested, measured}], before_meshing.
    DOMAIN_EXTENT = "domain_extent"
    #: A trial pressure solve on the mesh did not converge.
    NOT_SOLVABLE = "not_solvable"
    #: An assembly's regions did not come out as separate meshes that meet cleanly.
    REGION_SPLIT = "region_split"
    #: The mesher stopped before it wrote a complete mesh, or the mesh's record was never
    #: written. Facts: engine.
    ENGINE_CRASHED = "engine_crashed"
    #: The input geometry was refused before any mesh was built. Facts: reason.
    GEOMETRY_REJECTED = "geometry_rejected"
    #: The review of the mesh stopped before it reached a verdict - our failure, never a statement
    #: about the mesh. Job 53bbce4b built a mesh that passed every gate and a trial solve, the
    #: review stalled, and the user was told only "something went wrong on our side". Facts:
    #: marker (which way the review stopped), validated (the executor validated the mesh first),
    #: reruns (how many times the review was already started again on the same mesh).
    REVIEW_INCOMPLETE = "review_incomplete"


#: The causes another builder attempt cannot change. A contract mismatch is authored from the
#: approval by deterministic code - the next attempt writes the same case - and a refused geometry
#: is the same file next time. Retrying either only burns another run (job ac1daa3e burned two).
_NOT_RETRYABLE = frozenset({FailureCause.CONTRACT_MISMATCH, FailureCause.GEOMETRY_REJECTED})

#: The rejection sources that are not declared gates: the engine seams the executor calls itself.
#: Declared gates name their own cause (GateSpec.cause); these are named once, here.
SEAM_CAUSES: dict[str, FailureCause] = {
    "finalize":      FailureCause.ENGINE_CRASHED,
    "domain_extent": FailureCause.DOMAIN_EXTENT,
    "solvability":   FailureCause.NOT_SOLVABLE,
    "geometry":      FailureCause.GEOMETRY_REJECTED,
}

#: Said when the retry policy skipped the remaining attempts, so the user knows the stop was a
#: decision and not a budget that ran out.
RETRY_SKIPPED_NOTE = "We did not try again: another attempt would hit the same problem."

_RUN_AGAIN = 'say "run it again" in this chat'


def as_cause(value: object) -> FailureCause | None:
    try:
        return FailureCause(str(value or ""))
    except ValueError:
        return None


def retry_can_help(cause: object, facts: Mapping | None = None) -> bool:
    """Whether another builder attempt could change this outcome. An unknown or absent cause
    answers True: when we cannot say what failed, the retry policy stays what it always was.

    One exception is carried in the facts: a contract mismatch is deterministic only where this
    system's own code writes the names from the approval (the snappy driver, the cfMesh renderer,
    and every pre-flight refusal). Where the builder model chooses them (gmsh's physical groups,
    the multi-region case's regions) or the geometry staging binds them (vmtk's openings), the
    next attempt may name them differently, so those gates mark the mismatch `retry_may_fix` and
    it keeps its retries - when in doubt, the ladder stays as it was.

    And one the other way: a mesh too coarse across its passage, whose gate worked out that even
    the cheapest rebuild reaching the floor (`rebuild_cells`, the cell count grown by the square of
    the refinement) is over the job's `cell_limit`. Another attempt can only come out too big or
    too coarse again, so it is not started."""
    c = as_cause(cause)
    if c is None:
        return True
    if (c is FailureCause.CONTRACT_MISMATCH and isinstance(facts, Mapping)
            and facts.get("retry_may_fix")):
        return True
    if c is FailureCause.UNDER_RESOLVED and rebuild_over_limit(facts):
        return False
    return c not in _NOT_RETRYABLE


def rebuild_over_limit(facts: Mapping | None) -> bool:
    """Whether an under-resolved mesh's facts say no rebuild within the cell limit reaches the
    floor. False whenever either figure is missing - when in doubt, the retry stays."""
    if not isinstance(facts, Mapping):
        return False
    need, limit = _as_number(facts.get("rebuild_cells")), _as_number(facts.get("cell_limit"))
    if need is None or limit is None:
        return False
    return limit > 0 and need > limit


# #
# the sentences
# #

def _num(v: object) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _names(names, joiner: str = "and") -> str:
    items = [f"'{n}'" for n in names]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {joiner} " + items[-1]


_OP_WORDS = {"<=": "at most", ">=": "at least", "<": "under", ">": "over", "==": "exactly"}


def _as_number(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _check_phrase(c: Mapping) -> str:
    key, label = str(c.get("key") or ""), str(c.get("label") or c.get("key") or "a quality bar")
    m, op, t = c.get("measured"), str(c.get("op") or ""), c.get("threshold")
    mf, tf = _as_number(m), _as_number(t)
    if key == "max_non_ortho" and mf is not None and tf is not None:
        return f"the worst non-orthogonality is {mf:.0f} degrees, and the limit is {tf:g} degrees"
    if key == "skew_fraction" and mf is not None and tf is not None:
        return (f"{mf * 100:.2g}% of the faces are badly skewed, and at most "
                f"{tf * 100:.2g}% may be")
    if key == "wall_faces":
        return "the body is missing from the mesh: its wall came out with no faces"
    if key in ("min_sicn", "min_quality") and mf is not None and tf is not None:
        what = "element quality (SICN)" if key == "min_sicn" else "tetrahedron quality"
        return f"the worst {what} is {mf:.3g}, and it must be at least {tf:g}"
    if m is None:
        return f"{label} was not measured"
    limit = f"{_OP_WORDS.get(op, op)} {_num(t)}".strip() if (op or t is not None) else ""
    return f"{label.rstrip('.')}: measured {_num(m)}" + (f", required {limit}" if limit else "")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def clean_reason(reason: object) -> str:
    # engine refusals open with a machine tag ("[GEOMETRY_UNSUITABLE] ...") written for the
    # builder; the user gets the words after it
    text = " ".join(str(reason or "").split())
    while text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    return text[:400].rstrip(".")


def _contract(f: Mapping) -> tuple[str, str]:
    renamed = {str(k): str(v) for k, v in dict(f.get("renamed") or {}).items()}
    missing = [str(m) for m in (f.get("missing") or []) if str(m) not in renamed]
    mistyped = [m for m in (f.get("mistyped") or []) if isinstance(m, Mapping)]
    extra = [str(e) for e in (f.get("extra") or [])]
    roles = [str(r) for r in (f.get("missing_roles") or [])]
    present = [str(p) for p in (f.get("present") or [])]
    before = bool(f.get("before_meshing"))
    noun = str(f.get("noun") or "boundary")          # a multi-region case names regions
    parts: list[str] = []
    for typed, written in renamed.items():
        parts.append(f"the boundary you approved as '{typed}' "
                     f"{'would come' if before else 'came'} out of the mesher as '{written}'")
    if missing:
        has = f" (it has {_names(present)})" if present else ""
        parts.append(f"the mesh {'would have' if before else 'has'} no {noun} called "
                     f"{_names(missing, 'or')}{has}")
    for m in mistyped:
        parts.append(f"'{m.get('name')}' {'would come' if before else 'came'} out as type "
                     f"'{m.get('got')}' where you approved '{m.get('declared')}'")
    if extra:
        parts.append(f"the mesh {'would have' if before else 'has'} {_names(extra)}, which you "
                     "did not approve")
    for r in roles:
        parts.append(f"the mesh {'would have' if before else 'has'} no {r} boundary")
    # the patch-contract launch check (engines/case_contract.py) states its own findings in plain
    # words ("the approved patch 'freestream' (farfield) is not written - the case writes ...");
    # they are said as written, the first few of them
    parts += [str(p).strip().rstrip(".") for p in (f.get("problems") or [])[:3] if str(p).strip()]
    what = (_cap("; ".join(parts)) + ", so the mesh "
            + ("would not match" if before else "does not match") + " what you approved."
            if parts else
            "A boundary you approved did not come out of the mesher under its approved name "
            "and type.")
    what += " This is our mistake, not your geometry's."
    if before:
        what += " We caught it before meshing, so no time was spent on a mesh."
    if renamed:
        typed, written = next(iter(renamed.items()))
        nxt = (f"We will fix this on our side. To go ahead now, rename '{typed}' to '{written}' "
               f"in this chat and say \"run it again\".")
    else:
        nxt = ("We will fix this on our side. Once it is fixed, say \"run it again\" in this "
               "chat - or tell me a different boundary setup to try now.")
    return what, nxt


def _quality(f: Mapping) -> tuple[str, str]:
    unmeasured = [str(u) for u in (f.get("unmeasured") or [])]
    if unmeasured:
        return (f"We could not measure {_names(unmeasured)} on the mesh, so we could not accept "
                "it. That is on our side, not your geometry's.",
                f"You can run it again: {_RUN_AGAIN}.")
    phrases: list[str] = []
    fatal = [str(x) for x in (f.get("fatal") or [])]
    if fatal:
        phrases.append(f"it has broken cells ({', '.join(fatal[:4])}) that a solver cannot use")
    phrases += [_check_phrase(c) for c in (f.get("checks") or []) if isinstance(c, Mapping)]
    if not phrases:
        phrases.append("its cells failed a quality bar this engine requires")
    return ("The mesh was built, but its cells are too badly shaped to trust: "
            + "; ".join(phrases) + ".",
            f"You can run it again ({_RUN_AGAIN}), or tell me what to change first - for "
            "example a finer mesh near sharp edges, or fewer boundary layers.")


def _touch_phrase(m: Mapping, d: object) -> tuple[str, bool]:
    """A box that touches the body, said plainly - and, when a symmetry plane is misplaced,
    where it is and where it has to be. The flag says a symmetry plane is the cause."""
    sp, sf, face = m.get("symmetry_patch"), m.get("symmetry_face"), m.get("face")
    if not sp:
        return f"the box touches the body on the {d} side", False
    if m.get("crosses"):
        return (f"the body crosses the symmetry plane '{sp}' on the {d} side, and a half model "
                "has to lie wholly on one side of its cut"), True
    if sf and sf == face:
        return (f"the symmetry plane '{sp}' was put across the flow, on the {d} side, but a "
                "half model is cut along the flow"), True
    return (f"the box touches the body on the {d} side ({face}), but the symmetry plane "
            f"'{sp}' is on the {sf} face - it has to be on the face the model was cut on"), True


def _domain(f: Mapping) -> tuple[str, str]:
    bits: list[str] = []
    misplaced = False
    for m in (f.get("misses") or []):
        if not isinstance(m, Mapping):
            continue
        d, req, meas = m.get("direction"), m.get("requested"), m.get("measured")
        if m.get("symmetry_patch") or (isinstance(meas, (int, float))
                                       and not isinstance(meas, bool) and meas <= 0):
            phrase, sym = _touch_phrase(m, d)
            bits.append(phrase)
            misplaced = misplaced or sym
        elif isinstance(req, (int, float)) and isinstance(meas, (int, float)):
            bits.append(f"{d} is {float(meas):.3g} reference lengths where you asked for "
                        f"{float(req):g}")
    what = ("The space around the body is not the size you asked for"
            + (": " + "; ".join(bits) if bits else "") + ".")
    if f.get("before_meshing"):
        what += " We caught it before meshing, so no time was spent on a mesh."
    if misplaced:
        # running it again rebuilds the same box: the out is where the plane goes
        return what, ("Tell me which face the model was cut on, so the symmetry plane goes "
                      "there, or mesh the whole model without a symmetry plane.")
    return what, (f"You can run it again ({_RUN_AGAIN}), or change the far-field sizes you "
                  "asked for.")


#: Why a review stopped, by the marker it ended with (agents/reviewer/loop_policy), in words.
_REVIEW_STOPPED = {
    "reviewer_stalled": "the reviewer stopped making progress before it reached a verdict",
    "reviewer_exhausted": "the reviewer used up its time before it reached a verdict",
    "reviewer_render_unavailable": ("the tool that draws the mesh for the reviewer was not "
                                    "available"),
    # also every pre-loop refusal: a malformed plan, a missing measurement or view or target
    "reviewer_evidence_missing": "the review did not have everything it needs to judge the mesh",
}


def review_stopped_reason(marker: object) -> str:
    """Why a review stopped without a verdict, in plain words, from the marker it ended with."""
    return _REVIEW_STOPPED.get(str(marker or "").strip(),
                               "the AI service that reviews the mesh was not available")


def _review(f: Mapping) -> tuple[str, str]:
    why = review_stopped_reason(f.get("marker"))
    reruns = f.get("reruns")
    n = int(reruns) if isinstance(reruns, int) and not isinstance(reruns, bool) else 0
    if f.get("validated"):
        what = ("Your mesh was built and passed every automatic check, but our final review of "
                f"it did not finish: {why}.")
    else:
        what = f"Our review of the mesh could not run: {why}."
    if n:
        times = {1: "once", 2: "twice"}.get(n, f"{n} times")
        what += f" We started the review again on the same mesh {times}, and it stopped again."
    what += " That is on our side, not a problem with your geometry or your request."
    return what, (f"You can run it again ({_RUN_AGAIN}) with the same request - nothing needs to "
                  "change.")


def describe(cause: object, facts: Mapping | None = None, *,
             engine: str = "") -> tuple[str, str]:
    """(what failed, what the user can do next) - each one or two plain sentences. An unknown
    cause returns two empty strings, and the caller keeps its generic headline."""
    c = as_cause(cause)
    f: Mapping = facts if isinstance(facts, Mapping) else {}
    if c is None:
        return "", ""
    if c is FailureCause.CONTRACT_MISMATCH:
        return _contract(f)
    if c is FailureCause.PATCH_NOT_CAPTURED:
        patches = [str(p) for p in (f.get("patches") or [])]
        detail = clean_reason(f.get("detail"))
        what = (f"The mesher lost the boundary {_names(patches)}: it came out with no faces, "
                "usually because it is smaller than the cells around it."
                if patches else
                (f"A boundary did not come out of the mesher: {detail}." if detail else
                 "A boundary you declared came out of the mesher with no faces."))
        return what, (f"You can run it again ({_RUN_AGAIN}), or ask for finer cells near that "
                      "boundary.")
    if c is FailureCause.BOUNDARY_TYPE:
        bits = [f"'{m.get('name')}' came out as type '{m.get('got')}', but a {m.get('declared')} "
                f"boundary must be type '{m.get('want')}'"
                for m in (f.get("mistyped") or []) if isinstance(m, Mapping)]
        what = (_cap("; ".join(bits)) if bits else
                "A boundary came out with the wrong type for the solver")
        return (what + ". This is our mistake, not your geometry's.",
                f"You can run it again ({_RUN_AGAIN}); if it happens again, we will fix it on "
                "our side.")
    if c is FailureCause.MESH_QUALITY:
        return _quality(f)
    if c is FailureCause.UNDER_RESOLVED:
        x, n = _as_number(f.get("cells_across")), f.get("needed")
        if f.get("scope") == "main":
            # the side passages the flow can go round were set aside; the main way itself fell short
            what = ("The mesh is too coarse in the passages the flow must go through"
                    + (f": about {_num(x)} cells across them at the narrowest, and they "
                       f"need at least {_num(n)}" if x is not None and n is not None else "")
                    + ".")
        else:
            what = ("The mesh is too coarse where the passage is narrowest"
                    + (f": about {_num(x)} cells across it, and it needs at least {_num(n)}"
                       if x is not None and n is not None else "") + ".")
        if rebuild_over_limit(f):
            rebuild = _as_number(f.get("rebuild_cells")) or 0.0
            allowed = _as_number(f.get("cell_limit")) or 0.0
            what += (f" A mesh fine enough would need at least about {rebuild / 1e6:.2g} million "
                     f"cells, more than the {allowed / 1e6:.2g} million one job may build.")
            return what, ("You can try another mesher, or mesh a smaller part of the geometry "
                          "where the passage is narrowest.")
        return what, f"You can run it again ({_RUN_AGAIN}), or ask for a finer mesh."
    if c is FailureCause.CELL_BUDGET:
        cells, limit = f.get("cells"), f.get("limit")
        what = ("The mesh came out too big for one job"
                + (f": {_num(int(cells))} cells, over the {_num(int(limit))}-cell limit"
                   if isinstance(cells, (int, float)) and isinstance(limit, (int, float))
                   else "") + ".")
        return what, (f"You can run it again ({_RUN_AGAIN}), or ask for less detail - for "
                      "example 'standard' instead of 'max'.")
    if c is FailureCause.DOMAIN_EXTENT:
        return _domain(f)
    if c is FailureCause.NOT_SOLVABLE:
        return ("A trial solve on the mesh did not converge, so a flow solver would not run on "
                "it.", f"You can run it again: {_RUN_AGAIN}.")
    if c is FailureCause.REGION_SPLIT:
        return ("The parts of the assembly did not come out as separate meshes that meet "
                "cleanly.",
                f"You can run it again ({_RUN_AGAIN}); if it repeats, check that the solids in "
                "the file actually touch where they should.")
    if c is FailureCause.ENGINE_CRASHED:
        who = f" ({f.get('engine') or engine})" if (f.get("engine") or engine) else ""
        return (f"The mesher{who} stopped before it finished writing the mesh, so there was no "
                "complete mesh to check. This is usually on our side, not your geometry's.",
                f"You can run it again: {_RUN_AGAIN}.")
    if c is FailureCause.REVIEW_INCOMPLETE:
        return _review(f)
    if c is FailureCause.GEOMETRY_REJECTED:
        # ONE sentence for a refused input, and errors.py owns it (FailureClass.DOMAIN_REJECTED,
        # the class the input_rejected category renders): lead, the reason, the next step.
        from meshpipeline.errors import FailureClass, user_message_for
        return (user_message_for(FailureClass.DOMAIN_REJECTED,
                                 reason=clean_reason(f.get("reason"))), "")
    return "", ""


__all__ = ["RETRY_SKIPPED_NOTE", "SEAM_CAUSES", "FailureCause", "as_cause", "clean_reason",
           "describe", "rebuild_over_limit", "retry_can_help", "review_stopped_reason"]
