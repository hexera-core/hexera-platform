# Responsibility: Declare CAD repair's switches - whether geometry may be MUTATED at all, and the
# caps a conservative repair must stay inside.
# Boundaries: settings only. Whether a particular repair is safe is decided by
# cad/repair/conservative.py measuring its own result against these; nothing here inspects a file.
from __future__ import annotations

from meshpipeline.settings.env import bool_env, optional_env

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
#: cannot resolve a candidate rim's free wire, fails closed. The session caps bound how much area and volume a kept fill may
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
CAD_INSPECT_TIMEOUT_S: float = float(optional_env("CAD_INSPECT_TIMEOUT_S", "25"))

#: Files larger than this are not inspected at all. Reading a very large STEP costs seconds before
#: anything is measured, and a part that big is one an operator should look at anyway.
CAD_INSPECT_MAX_BYTES: int = int(optional_env("CAD_INSPECT_MAX_BYTES", str(80 * 1024 * 1024)))

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
#: costs. The catalogue is FIVE operations (`operations.OPERATIONS`), so six is every operation
#: once plus one spare attempt - which is a reasonable budget rather than a derived number, and
#: saying so is more honest than the arithmetic this comment used to claim.
CAD_REPAIR_MAX_OPS: int = int(optional_env("CAD_REPAIR_MAX_OPS", "6"))

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


#: Where the model sits in the repair loop's planner seat. "off" is byte-for-byte the prior
#: behaviour: the rule planner alone, no model call, no shadow row. "shadow" asks the model and
#: records what it would have done beside what the rules did, without acting on it. "live" lets its
#: plan act, inside the same caps and measurements every operation already answers to. An unknown
#: value reads as "off": a typo in a deployment must not put a model in charge of geometry.
CAD_REPAIR_LLM_PLANNER: str = _planner_posture()

#: The most planner model calls one repair run may make. A hard ceiling on spend per run.
CAD_REPAIR_LLM_PLANNER_CALLS_MAX: int = int(optional_env("CAD_REPAIR_LLM_PLANNER_CALLS_MAX", "2"))

#: Seconds one planner model call may take before the rule planner's choice stands.
CAD_REPAIR_LLM_PLANNER_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_LLM_PLANNER_TIMEOUT_S", "20.0"))

#: The confidence below which the model's plan is not acted on and the rule planner's choice stands.
CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE: float = float(
    optional_env("CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE", "0.5"))
