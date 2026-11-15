# Responsibility: Declare CAD repair's switches - whether geometry may be MUTATED at all, and the
# caps a conservative repair must stay inside.
# Boundaries: settings only. Whether a particular repair is safe is decided by
# cad/repair/conservative.py measuring its own result against these; nothing here inspects a file.
from __future__ import annotations

import logging

from meshpipeline.settings.env import bool_env, optional_env

logger = logging.getLogger(__name__)

#: OFF unless a deployment turns it on, and the ONE switch that separates looking from changing.
#: Inspection runs on every mesh-intended job regardless (pipeline/repair_inspect.py); this says
#: whether a repair may write new geometry at all. It is a kill switch, not a tuning knob: with it
#: off, a repair attempt is refused before any kernel call, so a deployment that has not opted in
#: cannot mutate a customer's CAD by any path.
CAD_REPAIR_ENABLED: bool = bool_env("CAD_REPAIR_ENABLED", "false")

#: Whether the pipeline REPAIRS A FILE BY ITSELF when triage can point at what is wrong, rather
#: than only reporting it. Reachable only when CAD_REPAIR_ENABLED is on, so a deployment that has
#: not opted into geometry mutation is unaffected either way.
#:
#: ON, because an advisory that needs a person before anything is fixed is not the service this is
#: for. What makes that defensible is not optimism: the repair is bounded by the caps below, its
#: result is MEASURED and refused if it left them, the repaired geometry must provably stage for
#: the chosen engine before it replaces anything, and the original upload stays immutable and
#: retrievable. If any of those refuse, the run continues on the file the customer sent - which is
#: the same outcome as not having tried.
CAD_REPAIR_AUTONOMOUS: bool = bool_env("CAD_REPAIR_AUTONOMOUS", "true")

#: THE DEVIATION CEILING, as a fraction of the part's diagonal. A conservative repair closes gaps
#: and sews shells; it must not move the surface further than this from where the customer put it.
#: Measured after the fact and enforced by refusing the result, because an OpenCASCADE tolerance
#: is a request and not a guarantee - ShapeFix may raise a tolerance well past what it was given.
#: 0.001 is one part in a thousand of the diagonal: for a 200 mm bracket, 0.2 mm.
CAD_REPAIR_MAX_DEVIATION_RATIO: float = float(
    optional_env("CAD_REPAIR_MAX_DEVIATION_RATIO", "0.001"))

#: The largest tolerance, in millimetres, a repair may leave on any sub-shape. OpenCASCADE will
#: inflate tolerances to make a shape close; past some point "valid" means "the kernel agreed to
#: stop complaining", not "the geometry is what the customer drew".
CAD_REPAIR_MAX_TOLERANCE_MM: float = float(
    optional_env("CAD_REPAIR_MAX_TOLERANCE_MM", "0.1"))

#: Whether a repair may REMOVE faces. Off, and not a knob an operator should reach for casually:
#: deleting a face is defeaturing, which is a judgement about what the part is FOR and belongs to
#: an operator with the customer's intent in front of them - not to a conservative pass.
CAD_REPAIR_ALLOW_FACE_REMOVAL: bool = bool_env("CAD_REPAIR_ALLOW_FACE_REMOVAL", "false")

#: Whether a repair may CLOSE A HOLE by building a flat patch across it. OFF, and the reason is
#: worth reading before turning it on.
#:
#: THIS ONE INVENTS GEOMETRY. Every other conservative operation rearranges what the customer
#: drew; this adds a face that was never in their file. It exists because sewing CANNOT close a
#: hole - sewing stitches coincident boundaries, and a face that is simply missing has nothing to
#: stitch to - so the commonest reason a real part will not mesh survives every other operation.
#:
#: WHAT GUARDS IT. A flat patch across a flat loop looks determined, and across an isolated
#: opening it is. But the two rims of a THROUGH-HOLE whose wall is missing are also flat closed
#: loops, and patching them seals the hole - a part with a bolt hole handed back without one. The
#: PAIRED-RIM GUARD (`plan.paired_rim_fingerprints` over `conservative._paired_rims`) is what
#: tells the two apart: any planar loop that is not a lone face's whole outer wire and sits at the
#: other end of a straight passage from another (no steeper than 70 deg to either rim's normal) is
#: a rim of one passage - of ANY cross-section, angle or draft - and the planner then refuses
#: filling for the whole run and reports the rims for review. A check that cannot run, or that
#: cannot resolve or judge (a non-planar loop) a candidate rim, fails closed. The session caps bound how much area and volume a kept fill may
#: add; they do not see a sealed hole, so the guard - not the caps - is what protects one. Off by
#: default because it adds geometry the customer never drew; the deploy's `cad_repair` tick turns
#: it on.
CAD_REPAIR_FILL_PLANAR_HOLES: bool = bool_env("CAD_REPAIR_FILL_PLANAR_HOLES", "false")

#: Close a hole whose rim is not flat with a patch that follows the surrounding surface. Separate
#: from the planar flag because it invents curvature the customer never drew, so it does not
#: inherit the planar fill's consent. Off by default; the deploy's `cad_repair` tick turns it on.
CAD_REPAIR_FILL_NONPLANAR_HOLES: bool = bool_env("CAD_REPAIR_FILL_NONPLANAR_HOLES", "false")

#: Heal self-intersecting geometry, touching only the faces the kernel's analysis names and
#: leaving the rest of the shape as it was. Off by default; the deploy's `cad_repair` tick turns
#: it on.
CAD_REPAIR_HEAL_SELF_INTERSECTIONS: bool = bool_env("CAD_REPAIR_HEAL_SELF_INTERSECTIONS", "false")

#: Split a CLOSED face - a full cylinder, sphere or torus carrying a single seam - in two
#: (`operations.SplitClosedFaces`), the standard answer when gmsh refuses a periodic surface. It
#: adds faces and moves no surface or volume, but it still rewrites the topology of the customer's
#: file, so it is off by default and a deployment opts in. Off keeps the operation out of the
#: catalogue and out of the rules baseline's MESH_OPS entirely.
CAD_REPAIR_SPLIT_CLOSED_FACES: bool = bool_env("CAD_REPAIR_SPLIT_CLOSED_FACES", "false")

#: Make two TOUCHING bodies share their interface (`operations.FragmentBodies`: a general fuse over
#: the pair), the standard answer when gmsh's volume stage finds an edge through a face because
#: two bodies of an assembly meet without a conformal interface. It splits faces and moves no
#: volume, but it rewrites how the customer's bodies are joined, so it is off by default and a
#: deployment opts in. Interpenetrating bodies are always declined. Off keeps the operation out of
#: the catalogue and out of the rules baseline's MESH_OPS entirely.
CAD_REPAIR_FRAGMENT_BODIES: bool = bool_env("CAD_REPAIR_FRAGMENT_BODIES", "false")

#: Sweep away SLIVER FACES - the sub-tolerance strips and spots a Boolean that just missed leaves
#: behind. Separate from CAD_REPAIR_ALLOW_FACE_REMOVAL on purpose: that knob waives the
#: face-removal check for EVERY operation in a run, while this one consents to a single bounded
#: operation (`operations.FixSmallFaces`) that re-measures each face it was aimed at, declares
#: exactly how many faces it removed, and is refused the moment the count or the size of what
#: actually went disagrees with that declaration. It still removes geometry the customer's file
#: carried - a 0.002 mm strip nobody could machine, but geometry - so it is off by default and a
#: deployment opts in.
CAD_REPAIR_FIX_SMALL_FACES: bool = bool_env("CAD_REPAIR_FIX_SMALL_FACES", "false")

#: THE SEARCH RADIUS the sliver sweep (`operations.FixSmallFaces`) hands
#: `ShapeFix_FixSmallFace`, as a fraction of the part's diagonal. The fixer finds a spot or a
#: strip by comparing its WIDTH against this precision, and a sliver is sliver-sized relative to
#: the PART (the localiser's criterion is an area fraction), so a fixed millimetre figure finds
#: every strip on a bracket and none on a vehicle.
#:
#: WHY THIS IS NOT CAD_REPAIR_MAX_DEVIATION_RATIO, which it used to be derived from. The old
#: reasoning was that merging a strip moves the boundary by about the strip's width, so the
#: deviation ceiling was the honest precision. MEASURED, THAT IS WRONG BY THREE ORDERS OF
#: MAGNITUDE. Running the fixer on two corpus parts at several precisions and measuring the result
#: with `caps.measure`: on a 2152 mm part at 0.005 of the diagonal the sweep removed all 6 flagged
#: slivers while the surface area moved 7.3e-06 of the part, the volume 1.6e-07, the diagonal not
#: at all, and the maximum tolerance stayed at 1e-07; on a 420 mm part, 13 of 21 slivers for
#: 3.2e-05 area and 1.3e-06 volume, again with no diagonal or tolerance movement. A PRECISION IS
#: THE RADIUS SEARCHED, NOT THE DISTANCE MOVED: removing a strip merges it into neighbours that
#: were already there, and the geometry that comes back sits far inside every cap. Tying the two
#: together only meant the sweep could never find what it exists to remove.
#:
#: THE CAPS REMAIN THE JUDGE OF ACTUAL MOVEMENT, which is why widening this is safe to try. The
#: operation does not get to decide its own result is acceptable: the same measurement showed
#: 0.02 of the diagonal removing 25 of 30 faces, inflating tolerance to 5.34 mm and leaving an
#: INVALID shape - caught by CAD_REPAIR_MAX_TOLERANCE_MM and the validity check, and rolled back.
#: Raising this ratio widens what the sweep may LOOK AT; it widens nothing it may keep.
#:
#: 0.001 is today's behaviour on the parts this was measured on and changes nothing until a
#: deployment raises it deliberately. It is settable by environment variable precisely so the
#: value can be chosen by sweeping the corpus rather than argued.
CAD_REPAIR_SLIVER_PRECISION_RATIO: float = float(
    optional_env("CAD_REPAIR_SLIVER_PRECISION_RATIO", "0.001"))

#: The largest hole that may be patched, as a fraction of the part's diagonal measured across the
#: hole's own bounding box. Past this it is not a hole, it is a missing wall - and a flat lid over
#: a missing wall is a different part, not a repaired one.
CAD_REPAIR_MAX_HOLE_SPAN_RATIO: float = float(
    optional_env("CAD_REPAIR_MAX_HOLE_SPAN_RATIO", "0.75"))

#: Seconds inspection may spend on one file before the run goes on without it. Inspection is
#: EVIDENCE and runs on every mesh-intended job, so a pathological part must not spend a run's
#: deadline proving it is pathological. The work is handed to a thread and abandoned on expiry:
#: OpenCASCADE offers no interruption point, so the thread finishes in the background while the
#: run proceeds on the original geometry with an inconclusive report.
CAD_INSPECT_TIMEOUT_S: float = float(optional_env("CAD_INSPECT_TIMEOUT_S", "60"))

#: Files larger than this are not inspected at all. Reading a very large STEP costs seconds before
#: anything is measured, and a part that big is one an operator should look at anyway.
CAD_INSPECT_MAX_BYTES: int = int(optional_env("CAD_INSPECT_MAX_BYTES", str(128 * 1024 * 1024)))

#: Seconds the run will WAIT for a repair before going on without it. Not a limit on the kernel:
#: OpenCASCADE offers no interruption point, so the thread finishes in the background while the
#: run proceeds on the original geometry.
#:
#: An earlier version of this file argued against having this setting at all, on the grounds that
#: a timeout nothing can enforce reads as a guarantee. That was half right. What cannot be bounded
#: is the KERNEL; what can be bounded is how long a run waits on it, and that is the number that
#: matters - a pathological part must not spend a job's deadline before admission has even judged
#: the original.
CAD_REPAIR_TIMEOUT_S: float = float(optional_env("CAD_REPAIR_TIMEOUT_S", "120"))

#: Operations the loop may TRY in one run, kept or rolled back. Not a limit on how many may be
#: kept: a run that tried six and kept one did six re-inspections, and the re-inspection is what
#: costs. Six is a budget, not a derived number: it is not tied to how many operations
#: `operations.OPERATIONS` holds, which grows, and is sized to leave room for retries.
CAD_REPAIR_MAX_OPS: int = int(optional_env("CAD_REPAIR_MAX_OPS", "6"))

#: PACKAGES ONE REGION MAY BE OFFERED IN ONE RUN by the region-scoped driver, each a fresh claim
#: stated before execution and judged on its own.
#:
#: WHY MORE THAN ONE. The operation-at-a-time loop's whole advantage was that its `RunHistory`
#: PERSISTS across iterations: it tries the narrowest operation, records the pair as ineffective
#: and then proposes the next one, which is often the one that works. A region offered exactly one
#: package got one guess and no second chance, and the attempts cannot be packed into one longer
#: plan instead: a package's predicate is stated BEFORE it runs, so a plan of k speculative
#: attempts must claim all k, and one failure reverts the operations that succeeded.
#:
#: THREE, because the baseline's own stall limit is 2 - two consecutive misses and it gives up -
#: so three packages is one more chance than the loop this driver is measured against, not a
#: licence to grind. The run's CAD_REPAIR_MAX_OPS and CAD_REPAIR_TIMEOUT_S remain the outer bounds
#: and are unchanged by this: this one bounds how many CLAIMS a region may state, and a package
#: that kept nothing still charges nothing against the run's operation allowance.
CAD_REPAIR_REGION_ATTEMPTS: int = int(optional_env("CAD_REPAIR_REGION_ATTEMPTS", "3"))

#: Consecutive operations that were not kept before the loop gives up. A planner that has proposed
#: two things in a row that did not work has run out of ideas the rules can express, and further
#: attempts spend a job's deadline rediscovering that.
CAD_REPAIR_STALL_LIMIT: int = int(optional_env("CAD_REPAIR_STALL_LIMIT", "2"))

#: How far the part's surface area may end up from what the operations ACCOUNTED FOR, as a
#: fraction of the original.
#:
#: WHY THIS IS NOT THE OLD ALLOWANCE. The whole-shape repair compared area and volume against the
#: original with the hole-span cap (0.75) as the allowance, because patching a hole legitimately
#: moves both and there was no way to say by how much. An operation that patches a hole now
#: DECLARES the area it added, so the comparison is against `original + declared` and the residual
#: can be tight. A 75% allowance on a measurement nobody predicted is not a cap; it is a record of
#: not knowing.
CAD_REPAIR_MAX_AREA_CHANGE_RATIO: float = float(
    optional_env("CAD_REPAIR_MAX_AREA_CHANGE_RATIO", "0.02"))

#: The same residual, for volume - applied only when the solid count did not RISE. Volume that
#: appears because a body closed is ACCEPTED AND RECORDED rather than compared, and `caps` skips
#: the comparison in three cases: the original had no solid, the result has none, or the result has
#: MORE than the original. The third is the mixed assembly - one solid body beside one sewn closed
#: and promoted - where the new body's volume is real, undeclarable (whether a shell closed is the
#: kernel's answer) and not a fraction of anything. A solid LOST is caught by the per-operation
#: solid-count check instead.
CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO: float = float(
    optional_env("CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", "0.02"))

#: Whether an UNSURE repair defect is put to an engineer in the console. OFF, and while it is off
#: the review mints nothing and no run parks: an unsure defect is reported exactly as it was before
#: this slice existed. Turning it on is what lets a run wait on a person, so it is a deployment's
#: decision and not a default.
CAD_REPAIR_REVIEW_ENABLED: bool = bool_env("CAD_REPAIR_REVIEW_ENABLED", "false")

#: How many unsure defects one review may put to a model before the rest go to the engineer
#: unexamined. A bound on spend per review, not a quality threshold: the sample is what the model
#: looks at, and it is taken in the order the defects were found.
CAD_REPAIR_REVIEW_SAMPLE_SIZE: int = int(optional_env("CAD_REPAIR_REVIEW_SAMPLE_SIZE", "3"))

#: The most model calls one review may make, across the sample. A hard ceiling on the review's
#: cost, independent of how many defects the sample holds.
CAD_REPAIR_REVIEW_LLM_CALLS_MAX: int = int(optional_env("CAD_REPAIR_REVIEW_LLM_CALLS_MAX", "4"))

#: Seconds one review model call may take before it is abandoned and its defect goes to the
#: engineer as unexamined. A model that has not answered in this long is not going to answer in
#: time for a job's deadline.
CAD_REPAIR_REVIEW_LLM_TIMEOUT_S: float = float(optional_env("CAD_REPAIR_REVIEW_LLM_TIMEOUT_S", "20.0"))


def _planner_posture() -> str:
    """The planner posture, FAILING CLOSED: anything but exactly shadow or live reads as off."""
    raw = optional_env("CAD_REPAIR_LLM_PLANNER", "off").strip().lower()
    return raw if raw in ("shadow", "live") else "off"


#: Optional model guidance inside CAD Repair System. "off" makes no model call and writes no
#: shadow row. "shadow" records guidance beside the system's executed plan without acting on it.
#: "live" lets that guidance enter the same planner and answer to the same caps and measurements
#: as every other directive. An unknown
#: value reads as "off": a typo in a deployment must not put a model in charge of geometry.
CAD_REPAIR_LLM_PLANNER: str = _planner_posture()

#: The most model calls one repair run may make while directing it. A hard ceiling on spend.
CAD_REPAIR_LLM_PLANNER_CALLS_MAX: int = int(optional_env("CAD_REPAIR_LLM_PLANNER_CALLS_MAX", "2"))

#: Seconds one model call may take before the planner continues with its bounded fallback.
CAD_REPAIR_LLM_PLANNER_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_LLM_PLANNER_TIMEOUT_S", "20.0"))

def _region_loop_posture() -> str:
    """The region-loop posture, FAILING CLOSED: anything but exactly live reads as off."""
    raw = optional_env("CAD_REPAIR_REGION_LOOP", "off").strip().lower()
    return raw if raw == "live" else "off"


#: Which DRIVER repairs a part: the operation-at-a-time loop, or the region-scoped one. "off" is
#: byte-for-byte the prior behaviour. "live" decomposes the part into regions and gives each one a
#: target package - a locus, a plan, and a claim stated before execution that a region-scoped
#: re-inspection then checks - so a region that cannot be repaired within its own budget is queued
#: for a person while the regions that committed still ship.
#:
#: A POSTURE RATHER THAN A BOOLEAN, for the reason CAD_REPAIR_LLM_PLANNER is one: an unknown value
#: reads as "off", so a typo in a deployment changes the driver of a customer's geometry to
#: nothing at all. The operation-at-a-time loop is retained whatever this says - it is the
#: benchmark baseline an improvement is attributed against, and the corpus runner reaches it.
CAD_REPAIR_REGION_LOOP: str = _region_loop_posture()

#: The confidence below which the model's plan is not acted on and the rule planner's choice stands.
CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE: float = float(
    optional_env("CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE", "0.5"))


def _mesh_probe_posture() -> str:
    """The mesh-probe posture, FAILING CLOSED: only the exact string `live` turns it on.

    STRICTER THAN `_region_loop_posture`, which forgives case and surrounding space: this one
    puts a gmsh run on every scan of a customer's part, so `Live ` or `LIVE` is read as the typo
    it probably is and the probe stays off.
    """
    raw = optional_env("CAD_REPAIR_MESH_PROBE", "off")
    if raw not in ("live", "off"):
        # Still read as off - but said out loud, so a deployment that meant `live` finds out.
        logger.warning("CAD_REPAIR_MESH_PROBE=%r is neither 'live' nor 'off'; reading it as off",
                       raw)
    return "live" if raw == "live" else "off"


#: Whether a repair scan also asks gmsh WHY the part will not mesh. "off" is byte-for-byte the
#: prior behaviour: the probe is never called. "live" appends the probe's defects
#: (cad/repair/mesh_probe.py) after the kernel scan's on every scan the session makes - the first,
#: each operation's re-inspection, and a package's scoped proof - so an operation can be aimed at
#: a mesh failure and judged by the same progress rule as any other defect. A posture, so a typo
#: reads as off.
CAD_REPAIR_MESH_PROBE: str = _mesh_probe_posture()

#: Seconds one mesh probe may take. The session hands it the lesser of this and what is left of
#: the run's time, and skips the probe outright when nothing is left.
CAD_REPAIR_MESH_PROBE_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_MESH_PROBE_TIMEOUT_S", "120.0"))


def _llm_author_posture() -> str:
    """The model-author posture, FAILING CLOSED: only the exact strings `shadow` and `live` turn it
    on, and anything else but `off` is read as off OUT LOUD - the mesh probe's rule, because this
    one puts a model in the author's seat for a customer's geometry."""
    raw = optional_env("CAD_REPAIR_LLM_AUTHOR", "off")
    if raw not in ("live", "shadow", "off"):
        logger.warning("CAD_REPAIR_LLM_AUTHOR=%r is not 'live', 'shadow' or 'off'; reading it "
                       "as off", raw)
    return raw if raw in ("live", "shadow") else "off"


#: Who authors the region loop's target packages (cad/repair/model_author.py). "off" is
#: byte-for-byte the prior behaviour: the deterministic rules author, no model call. "shadow"
#: keeps the rules author driving and asks the model beside it, recording its proposal (validated
#: or refused, with the reason) and executing nothing of it. "live" lets the model author the
#: packages - each still a claim the session proves under the same caps - and only when a model
#: router is configured; without one the rules author drives. Read only by the region-scoped
#: driver (CAD_REPAIR_REGION_LOOP=live).
CAD_REPAIR_LLM_AUTHOR: str = _llm_author_posture()

#: Seconds one model-author call may take before it counts as no proposal for that attempt.
CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S", "20.0"))

#: The most model-author calls one repair run may make, shadow or live - a hard ceiling on spend
#: and on the run time model calls can take, beside the per-region CAD_REPAIR_REGION_ATTEMPTS.
CAD_REPAIR_LLM_AUTHOR_CALLS_MAX: int = int(optional_env("CAD_REPAIR_LLM_AUTHOR_CALLS_MAX", "6"))
