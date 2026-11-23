# Responsibility: Declare CAD repair's switches - whether geometry may be MUTATED at all, and the
# caps a conservative repair must stay inside.
# Boundaries: settings only. Whether a particular repair is safe is decided by
# cad/repair/conservative.py measuring its own result against these; nothing here inspects a file.
from __future__ import annotations

import logging

from meshpipeline.settings.env import ConfigurationError, bool_env, optional_env

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

#: Re-trim ONE FACE from its own boundary (`operations.RebuildFace`: the face's underlying surface
#: and wires through BRepBuilderAPI_MakeFace, finished by ShapeFix_Face, spliced back into its
#: owning body), the standard answer when gmsh's 2D stage reports overlapping facets because a
#: face's trimming or parameterisation defeats the surface mesher. Off keeps the operation out of
#: the catalogue, and the rules baseline's MESH_OPS maps an overlap to `fix_shape_global` exactly
#: as before; on, `rebuild_face` is preferred and `fix_shape_global` is its fallback.
#:
#: THE GATE: NOT TO BE SET LIVE ON CUSTOMER JOBS UNTIL THE SEMANTIC PACKAGE REVIEWER
#: (CAD_REPAIR_PACKAGE_REVIEW=live) IS LIVE. A re-trim can move a feature - a face rebuilt on the
#: same surface but a different trim is a different face - and the reviewer is what replaces the
#: numeric deviation gate for such an operation (the op records its sampled deviation as evidence,
#: never as a gate). Code does not enforce the coupling; deployment posture owns it, and
#: `_warn_unreviewed_rebuild` says it out loud when the two disagree.
CAD_REPAIR_REBUILD_FACE: bool = bool_env("CAD_REPAIR_REBUILD_FACE", "false")

#: Close the gaps in ONE FACE's wires (`operations.CloseWireGaps`: ShapeFix_Wire's FixConnected at
#: the run's tolerance, then FixGaps3d on the face and on the neighbours sharing what moved), the
#: answer to gmsh's "the 1D mesh seems not to be forming a closed loop" on a face whose boundary
#: really is open, and to a kernel wire gap the narrower face fixer did not clear. Off keeps the
#: operation out of the catalogue, and the rules baseline's MESH_OPS maps an open loop to
#: `fix_face_boundary` exactly as before; on, `close_wire_gaps` is preferred for a 2D-stage open
#: loop and `fix_face_boundary` is its fallback.
#:
#: THE GATE: NOT TO BE SET LIVE ON CUSTOMER JOBS UNTIL THE SEMANTIC PACKAGE REVIEWER
#: (CAD_REPAIR_PACKAGE_REVIEW=live) IS LIVE. It is the first operation licensed to MOVE VERTICES:
#: bounded by the run's tolerance and measured (`max_vertex_move_mm`), but a moved vertex is a
#: changed part, and the reviewer is what replaces a numeric gate for it. Code does not enforce
#: the coupling; deployment posture owns it, and `_warn_unreviewed_closure` says it out loud.
CAD_REPAIR_CLOSE_WIRE_GAPS: bool = bool_env("CAD_REPAIR_CLOSE_WIRE_GAPS", "false")

#: Resolve a SUB-MESH-SIZE GAP (`gap_split.ResolveSubMeshGap`): two faces of one solid that do not
#: touch but sit closer than the default mesh's chord sag, so gmsh's triangles cross (edge through
#: a face, overlapping facets). The faces are split along their own iso-curves until a chord across
#: a patch sags less than half the gap; no surface moves, and the measured area/volume noise is
#: declared. Off keeps the operation out of the catalogue and every mapping byte for byte; on, it is
#: preferred for a clash-located gap and the code's previous choice is its fallback. It
#: re-partitions the customer's faces, so it is never auto-accepted (review_tier.REVIEWER_ONLY_OPS).
CAD_REPAIR_RESOLVE_SUB_MESH_GAP: bool = bool_env("CAD_REPAIR_RESOLVE_SUB_MESH_GAP", "false")

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


def _meshability_pass_posture() -> str:
    """The meshability-pass posture, FAILING CLOSED: anything but exactly live reads as off."""
    raw = optional_env("CAD_REPAIR_MESHABILITY_PASS", "off").strip().lower()
    return raw if raw == "live" else "off"


#: THE MESHABILITY PASS (region_loop._meshability_pass, cad/repair/meshability.py): before any
#: author runs, every mesh finding a registered meshability operation claims is offered that
#: operation through the ordinary `try_package`, down its rungs; and the run ALWAYS returns a STEP -
#: when nothing is kept (or the repair cannot run), the upload's bytes are written unchanged to the
#: destination, labelled `delivery_source=passthrough`, with mesh-size hints beside it when a hints
#: provider is installed. The refusal still raises, so every consumer's meaning (continue on the
#: original, job-level escalation) is unchanged. "off" is byte-for-byte today: no pass, no file
#: written on a refusal, no new measurement keys.
CAD_REPAIR_MESHABILITY_PASS: str = _meshability_pass_posture()

def kernel_test_fns_enabled() -> bool:
    """Whether the v4 kernel-worker TEST functions (echo, sleep-forever, abort, the split-op test
    ops) may register. Developer/test only, never set in production. Read at CALL time, not
    import time: spawned workers import the package from scratch and must see the environment the
    parent handed them, and a test that sets it after the settings module is imported must still
    be honoured."""
    return bool_env("HEXERA_TEST_KERNEL_FNS", "false")


def running_under_pytest() -> bool:
    """Whether this process is inside a pytest test. pytest sets PYTEST_CURRENT_TEST itself for the
    duration of each test; nothing a deployment sets. It gates the test-only constructors (the v4
    model ledger's `for_test`), so the read is fail-closed: unset OR blank is "not under pytest" and
    the test door stays shut. Read at CALL time, so a test that clears it is honoured."""
    return optional_env("PYTEST_CURRENT_TEST", "").strip() != ""


def verdict_cache_uri() -> str:
    """Where the v4 verdict cache lives when it must outlive the process: `gs://bucket/prefix` or a
    directory, from CRS_V4_VERDICT_CACHE_URI. Unset OR blank (whitespace included) returns "", which
    the caller reads as "no durable cache: one in-memory cache per process". The caller says so
    loudly, because a process-local cache looks exactly like a working one. Read at CALL time."""
    return optional_env("CRS_V4_VERDICT_CACHE_URI", "").strip()


def recipes_enabled() -> bool:
    """CAD_REPAIR_V4_RECIPES: whether the v4 search reads (and, on the dev split, writes) recipe memory
    (`repair_v4.recipes`, design 5.10). Off by default so a dev wave can A/B it. Read at CALL time."""
    return optional_env("CAD_REPAIR_V4_RECIPES", "false").strip().lower() in ("1", "true", "yes", "on")


def recipes_uri() -> str:
    """CAD_REPAIR_V4_RECIPES_URI: where recipe memory lives, `gs://bucket/prefix` (the eval bucket) or a directory.
    Unset or blank returns ""; turning the memory on without a location is refused by its caller."""
    return optional_env("CAD_REPAIR_V4_RECIPES_URI", "").strip()


ENGINES: tuple[str, ...] = ("v3", "search")


def _engine_posture_of(raw: str) -> str:
    """The repair engine named by `raw`, STRICTLY: blank is "not configured" (v3); anything that
    is not exactly a known engine, after trimming and case-folding, is refused - never coerced.

    Strict because this switch decides WHICH ENGINE REWRITES A CUSTOMER'S GEOMETRY: a typo that
    quietly fell back to v3 would hide that the operator's intent was not honoured, and one that
    quietly selected `search` would run an engine nobody asked for."""
    value = raw.strip().lower()
    if not value:
        return "v3"
    if value in ENGINES:
        return value
    raise ConfigurationError(
        f"Environment variable 'CAD_REPAIR_ENGINE' must be one of {', '.join(ENGINES)} - got {raw!r}.")


#: WHICH ENGINE `repair_step_file` RUNS: `v3` (the conservative region-loop repair) or `search`
#: (CRS v4, cad/repair_v4, which searches over kernel operations and is scored by one checker).
#: DEFAULT `v3`, and production never sets it: v4 lives beside v3 until it beats it on the new
#: benchmark, and only then does a separate change remove v3. The default is pinned by
#: tests/unit/cad/repair_v4/test_engine_dispatch.py, which also parses the production block of
#: the deploy workflow to prove it states no value.
CAD_REPAIR_ENGINE: str = _engine_posture_of(optional_env("CAD_REPAIR_ENGINE", "v3"))

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

#: Seconds one mesh probe may take. The session hands it the lesser of this and HALF of what is
#: left before the run's deadline (session `_with_mesh`), and skips the probe outright when nothing
#: is left; the run's final whole-shape probe gets whatever remains. Defaults BELOW
#: CAD_REPAIR_TIMEOUT_S (final review I2): at the old 120 one probe that timed out could spend the
#: whole run, and the after-checks and STEP write then ran past the attempt node's clock.
CAD_REPAIR_MESH_PROBE_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_MESH_PROBE_TIMEOUT_S", "60.0"))


#: Seconds beyond one probe timeout before the region loop authors nothing: the authoring
#: reserve's extra 10 s (region_loop.final_reserve_s = probe timeout + 10) plus the 5 s the run
#: deadline is set back from CAD_REPAIR_TIMEOUT_S (conservative.DEADLINE_MARGIN_S). Mirrored
#: here rather than imported, because settings must not depend upward on cad.repair.
_AUTHORING_RESERVE_EXTRA_S = 15.0


def _warn_no_authoring_time(posture: str, probe_timeout_s: float, run_timeout_s: float) -> None:
    """Say out loud when the live probe's reserve consumes the whole run: the region loop stops
    authoring while one probe timeout plus the final-probe slack is still left, so with
    CAD_REPAIR_MESH_PROBE_TIMEOUT_S + 15 >= CAD_REPAIR_TIMEOUT_S it authors nothing, silently."""
    if posture == "live" and probe_timeout_s + _AUTHORING_RESERVE_EXTRA_S >= run_timeout_s:
        logger.warning("CAD_REPAIR_MESH_PROBE_TIMEOUT_S=%s plus the %ss final-probe reserve is not "
                       "below CAD_REPAIR_TIMEOUT_S=%s: the repair will author nothing",
                       probe_timeout_s, _AUTHORING_RESERVE_EXTRA_S, run_timeout_s)


_warn_no_authoring_time(CAD_REPAIR_MESH_PROBE, CAD_REPAIR_MESH_PROBE_TIMEOUT_S,
                        CAD_REPAIR_TIMEOUT_S)


def _tolerant_probe_posture() -> str:
    """The tier-2b posture, FAILING CLOSED: only the exact string `live` turns it on.

    `_mesh_probe_posture`'s rule, unforgiving of case and space for the same reason: this one puts
    an fTetWild run on a customer's part at the end of a repair, so `Live ` is read as the typo it
    probably is and the pass stays off.
    """
    raw = optional_env("CAD_REPAIR_TOLERANT_PROBE", "off")
    if raw not in ("live", "off"):
        # Still read as off - but said out loud, so a deployment that meant `live` finds out.
        logger.warning(
            "CAD_REPAIR_TOLERANT_PROBE=%r is neither 'live' nor 'off'; reading it as off", raw)
    return "live" if raw == "live" else "off"


#: Whether a repair run's finishing verdict also records WHETHER THE TOLERANT RUNG WOULD EAT THE
#: PART (tier 2b, cad/repair/tolerant_probe.py). "off" is byte-for-byte the prior behaviour: no
#: fTetWild process, and no `routing` key in the report at all. "live" adds one coarse,
#: hard-time-capped fTetWild pass, and only for a part gmsh refused at the VOLUME stage with an
#: overlap - the 59% majority the tolerant rung plausibly answers. The verdict is RECORDED ONLY:
#: nothing in this plan routes or stops on it. A posture, so a typo reads as off.
CAD_REPAIR_TOLERANT_PROBE: str = _tolerant_probe_posture()

#: Seconds one tier-2b pass may take. The finishing verdict hands it the lesser of this and what
#: is left past the final-probe slack (oracle's `routing` rule), and skips the pass outright when
#: nothing is left. 30 by default: a ROUTING ANSWER, not a mesh deliverable - tier 2b exists to
#: answer a question in seconds, and a longer cap would make it the run.
CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S", "30.0"))


def _warn_no_authoring_time_with_tolerant(posture: str, tolerant: str, probe_timeout_s: float,
                                          tolerant_timeout_s: float,
                                          run_timeout_s: float) -> None:
    """Say out loud when the two live probes' reserve consumes the whole run.

    `goal.reserve_s` holds back one tier-2 cap, the final-probe slack AND the tolerant timeout, so
    a generous tolerant cap can leave the driver nothing to author with - silently, because the
    reserve is subtracted before the first operation is tried.
    """
    if tolerant != "live":
        return
    reserve = probe_timeout_s + _AUTHORING_RESERVE_EXTRA_S + tolerant_timeout_s
    if reserve >= run_timeout_s:
        logger.warning(
            "CAD_REPAIR_MESH_PROBE_TIMEOUT_S=%s plus the %ss final-probe reserve plus "
            "CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S=%s is not below CAD_REPAIR_TIMEOUT_S=%s: the "
            "repair will author nothing", probe_timeout_s, _AUTHORING_RESERVE_EXTRA_S,
            tolerant_timeout_s, run_timeout_s)


_warn_no_authoring_time_with_tolerant(CAD_REPAIR_MESH_PROBE, CAD_REPAIR_TOLERANT_PROBE,
                                      CAD_REPAIR_MESH_PROBE_TIMEOUT_S,
                                      CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S,
                                      CAD_REPAIR_TIMEOUT_S)


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


def _package_review_posture() -> str:
    """The package-review posture, FAILING CLOSED: only the exact string `live` turns it on, and
    anything else but `off` is read as off OUT LOUD - the model author's rule, because this one
    puts a model between a customer's repaired geometry and its commit."""
    raw = optional_env("CAD_REPAIR_PACKAGE_REVIEW", "off")
    if raw not in ("live", "off"):
        logger.warning("CAD_REPAIR_PACKAGE_REVIEW=%r is neither 'live' nor 'off'; reading it "
                       "as off", raw)
    return "live" if raw == "live" else "off"


def _clone_freedom_posture() -> str:
    """The scratch-clone freedom posture, FAILING CLOSED: only `live` turns it on."""
    raw = optional_env("CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off")
    if raw not in ("live", "off"):
        logger.warning("CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS=%r is neither 'live' nor 'off'; "
                       "reading it as off", raw)
    return "live" if raw == "live" else "off"


#: Whether an investigation may TRY an operation on its scratch clone that the delivery path would
#: refuse, and whether the walk puts the regions that actually stop the mesh first.
#:
#: Measured on wave 1: 538 of 623 regions escalated, 385 of those without a single tool call, and
#: 73 of 153 stated escalation reasons said the scoped checks pass while the WHOLE PART still
#: fails - the investigation was being stopped from trying things on a copy that cannot ship, and
#: was spending its clock on regions that were never the reason the part failed.
#:
#: "off" is byte-for-byte the prior behaviour. "live" changes the CLONE and the WALK ORDER only:
#: the clone's guards drop the review-reserved codes and the paired-rim fill refusal, and regions
#: holding a mesh-located defect are walked first with that defect as their primary target.
#:
#: IT MOVES NO DELIVERY GATE. The real session's caps, validity and solid-count checks, the
#: paired-rim guard, the held-for-review guard, the new-defect and revealed rules, the delivered
#: STEP keep rule and package review all judge a package exactly as they do today - a clone is a
#: copy, and nothing reaches a customer's geometry without passing them.
CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS: str = _clone_freedom_posture()


#: Whether a separate model reviews every package that passed its predicate BEFORE the commit
#: becomes durable (cad/repair/package_review.py) - the owner's semantic review, in place of any
#: numeric "barely moved" gate. "off" is byte-for-byte the prior behaviour: no call, no new key on
#: a verdict. "live" hands the reviewer the package's MEASURED evidence (ops kept and rolled back,
#: declared vs measured area/volume, counts, deviation, mesh verdicts, defects cleared/revealed/
#: remaining) with the author's intent labelled a claim; a reject - or any no-answer, including
#: no router and no time left - undoes the package exactly as a failed predicate does. Needs
#: nothing else on; without the mesh probe the evidence simply lacks mesh and deviation fields.
CAD_REPAIR_PACKAGE_REVIEW: str = _package_review_posture()


def _warn_unreviewed_baseline(review: str, region_loop: str) -> None:
    """Say out loud when review is live but the driver it lives in is not: the review sits in
    `RepairSession.try_package`, which only the region-scoped driver calls, so with
    CAD_REPAIR_REGION_LOOP off the operation-at-a-time baseline commits every repair unreviewed."""
    if review == "live" and region_loop != "live":
        logger.warning("CAD_REPAIR_PACKAGE_REVIEW=live but CAD_REPAIR_REGION_LOOP=%r: the "
                       "operation-at-a-time driver commits repairs WITHOUT the package review",
                       region_loop)


_warn_unreviewed_baseline(CAD_REPAIR_PACKAGE_REVIEW, CAD_REPAIR_REGION_LOOP)


def _warn_inert_author(author: str, region_loop: str) -> None:
    """Say out loud when a model author is asked for but the driver it lives in is not: the author
    is read only by the region-scoped driver, so with CAD_REPAIR_REGION_LOOP off a shadow or live
    CAD_REPAIR_LLM_AUTHOR is silently inert (the `_warn_unreviewed_baseline` rule)."""
    if author != "off" and region_loop != "live":
        logger.warning("CAD_REPAIR_LLM_AUTHOR=%s but CAD_REPAIR_REGION_LOOP=%r: the model author "
                       "is never asked - only the region-scoped driver reads it", author,
                       region_loop)


_warn_inert_author(CAD_REPAIR_LLM_AUTHOR, CAD_REPAIR_REGION_LOOP)


def _warn_unreviewed_rebuild(rebuild_face: bool, review: str) -> None:
    """Say out loud when `rebuild_face` is admitted but the package reviewer is not live: the
    reviewer is the gate this operation ships behind (see CAD_REPAIR_REBUILD_FACE), and without it
    a re-trim that moved a feature is judged by nothing semantic. A warning, not a refusal - the
    coupling is the deployment posture's to own, and benchmarks run the op unreviewed on purpose."""
    if rebuild_face and review != "live":
        logger.warning("CAD_REPAIR_REBUILD_FACE is on but CAD_REPAIR_PACKAGE_REVIEW=%r: rebuild_face "
                       "must not run on customer jobs until the package reviewer is live", review)


_warn_unreviewed_rebuild(CAD_REPAIR_REBUILD_FACE, CAD_REPAIR_PACKAGE_REVIEW)


def _warn_unreviewed_closure(close_wire_gaps: bool, review: str) -> None:
    """Say out loud when `close_wire_gaps` is admitted but the package reviewer is not live: the
    reviewer is the gate this vertex-moving operation ships behind (see
    CAD_REPAIR_CLOSE_WIRE_GAPS). A warning, not a refusal - `_warn_unreviewed_rebuild`'s rule."""
    if close_wire_gaps and review != "live":
        logger.warning("CAD_REPAIR_CLOSE_WIRE_GAPS is on but CAD_REPAIR_PACKAGE_REVIEW=%r: "
                       "close_wire_gaps moves vertices and must not run on customer jobs until the "
                       "package reviewer is live", review)


_warn_unreviewed_closure(CAD_REPAIR_CLOSE_WIRE_GAPS, CAD_REPAIR_PACKAGE_REVIEW)

#: Seconds one package-review call may take; cut further to what is left of the run's time. A
#: call that does not answer in time is a reject.
CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S", "20.0"))

def _investigator_posture() -> str:
    """The investigator's posture, FAILING CLOSED: only the exact strings `shadow` and `live` turn
    it on, and anything else but `off` is read as off OUT LOUD - `_llm_author_posture`'s rule,
    because this one puts a model in a TOOL LOOP over a customer's geometry, with its own compute
    budget, where the plain model author got one bounded call."""
    raw = optional_env("CAD_REPAIR_INVESTIGATOR", "off")
    if raw not in ("live", "shadow", "off"):
        logger.warning("CAD_REPAIR_INVESTIGATOR=%r is not 'live', 'shadow' or 'off'; reading it "
                       "as off", raw)
    return raw if raw in ("live", "shadow") else "off"


#: Whether the region loop's author is the bounded INVESTIGATOR (cad/repair/investigator.py) rather
#: than the single-shot model author. "off" is byte-for-byte the prior behaviour: the investigator
#: modules are never imported and `select_author` is called exactly as now. "shadow" runs an
#: investigation beside the driving author and records it, executing nothing of it, and only when
#: the whole investigation fits in the time left. "live" lets the investigation author the region's
#: target package - still a claim the session proves under the same caps, and still reviewed.
#: Read only by the region-scoped driver (CAD_REPAIR_REGION_LOOP=live).
CAD_REPAIR_INVESTIGATOR: str = _investigator_posture()

#: Persist each investigation's per-turn trace (tool calls, edit kept/rolled-back with reason,
#: conclusion, budget) and a per-region outcome table ("why no package") into the repair report,
#: and the trace as JSONL beside the job's artifacts. Dev diagnostics: off in production, on in
#: shared dev and crsv3. Off is byte-for-byte the prior behaviour. Redacted and size-capped.
CAD_REPAIR_INVESTIGATION_TRACE: bool = bool_env("CAD_REPAIR_INVESTIGATION_TRACE", "false")

#: CRS v3 postures read RAW here and validated by their ONE consumer, which fails closed (anything
#: but the exact strings it accepts reads as off, out loud). Each defaults to "off", byte-for-byte
#: the prior behaviour. Before these rows existed every consumer's `getattr(rcfg, NAME, "off")`
#: default answered, so the deploy path's exports were silently inert.
#: The tournament (cad/repair/tournament.py): off, shadow or live.
CAD_REPAIR_TOURNAMENT: str = optional_env("CAD_REPAIR_TOURNAMENT", "off")
#: Candidates the tournament rehearses per region; tournament.py clamps it and falls back to K=1.
CAD_REPAIR_TOURNAMENT_K: str = optional_env("CAD_REPAIR_TOURNAMENT_K", "1")
#: Fingerprint-keyed outcome memory (cad/repair/memory.py): off or live.
CAD_REPAIR_MEMORY: str = optional_env("CAD_REPAIR_MEMORY", "off")
#: Whether a review rejection returns its measured objection (cad/repair/package_review.py).
CAD_REPAIR_REVIEW_OBJECTION: str = optional_env("CAD_REPAIR_REVIEW_OBJECTION", "off")
#: Whether a mesher failure hands its failing faces back to repair (pipeline/repair_handback.py).
CAD_REPAIR_MESH_HANDBACK: str = optional_env("CAD_REPAIR_MESH_HANDBACK", "off")
#: Whether an operator may run a job as standalone repair (pipeline/repair_goal.py).
CAD_REPAIR_STANDALONE_JOBS: str = optional_env("CAD_REPAIR_STANDALONE_JOBS", "off")

#: Seconds ONE investigator model call may take. Cut further to what is left of the region's wall
#: and of the run (cad/repair/investigator_budget.InvestigationClock.left), so a generous value
#: here cannot outlive the run.
CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S: float = float(
    optional_env("CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S", "20.0"))

#: The most investigator model calls one repair RUN may make, across every region, shadow or live -
#: a hard ceiling on spend above the per-region budgets, which bound one region each. A capability
#: refusal that falls back to the JSON transport is not charged against it.
CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX: int = int(
    optional_env("CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX", "48"))

#: The fraction of the time left above the final reserve that ONE region's investigation may take.
#: At the 0.5 default the first region can never leave the second with nothing, and the budget's
#: own wall still caps it.
CAD_REPAIR_INVESTIGATOR_REGION_SHARE: float = float(
    optional_env("CAD_REPAIR_INVESTIGATOR_REGION_SHARE", "0.5"))


def _investigator_images_posture() -> str:
    """The `look` tool's image posture, FAILING CLOSED: only the exact string `live` turns it on.

    Parsed here so the inventory lists it once, and NOT plumbed through the deploy path: nothing
    has ever sent an image on the repair-review route, so it stays off everywhere until Task 14's
    measured router check passes.
    """
    raw = optional_env("CAD_REPAIR_INVESTIGATOR_IMAGES", "off")
    if raw not in ("live", "off"):
        logger.warning("CAD_REPAIR_INVESTIGATOR_IMAGES=%r is neither 'live' nor 'off'; reading it "
                       "as off", raw)
    return "live" if raw == "live" else "off"


#: Whether the investigator's `look` tool may carry a RENDERED IMAGE as well as its text
#: observation. "off" is text-first: an observation plus the review camera's numbers. "live" is
#: Task 14's and needs a measured check that the repair-review route accepts images at all.
CAD_REPAIR_INVESTIGATOR_IMAGES: str = _investigator_images_posture()


def _warn_inert_investigator(investigator: str, region_loop: str) -> None:
    """Say out loud when an investigator is asked for but the driver it lives in is not: the author
    seam is read only by the region-scoped driver, so with CAD_REPAIR_REGION_LOOP off a shadow or
    live CAD_REPAIR_INVESTIGATOR is silently inert (the `_warn_inert_author` rule)."""
    if investigator != "off" and region_loop != "live":
        logger.warning("CAD_REPAIR_INVESTIGATOR=%s but CAD_REPAIR_REGION_LOOP=%r: the "
                       "investigator is never asked - only the region-scoped driver reads it",
                       investigator, region_loop)


_warn_inert_investigator(CAD_REPAIR_INVESTIGATOR, CAD_REPAIR_REGION_LOOP)


def _auto_accept_posture() -> str:
    """The auto-accept posture, FAILING CLOSED: only the exact string `live` turns it on.

    `_tolerant_probe_posture`'s rule, and unforgiving for a stronger reason than any other
    posture here: this one lets a package COMMIT WITHOUT A REVIEWER, so `Live ` is read as the
    typo it probably is and every package keeps its seat in front of the model.
    """
    raw = optional_env("CAD_REPAIR_AUTO_ACCEPT", "off")
    if raw not in ("live", "off"):
        logger.warning("CAD_REPAIR_AUTO_ACCEPT=%r is neither 'live' nor 'off'; reading it as off",
                       raw)
    return "live" if raw == "live" else "off"


#: Whether a SMALL, FULLY ACCOUNTED package may be accepted without a model reviewer, audited
#: (cad/repair/review_tier.py). "off" is byte-for-byte the prior behaviour: every package that
#: passed its predicate goes to the reviewer. "live" is still INERT without a customer deviation
#: threshold on the run, and never applies to the vertex-moving operations (`rebuild_face`,
#: `close_wire_gaps`) at any threshold - the reviewer's seat is spent on the large moves, which is
#: the whole point of the tier. A posture, so a typo reads as off.
CAD_REPAIR_AUTO_ACCEPT: str = _auto_accept_posture()


def _warn_inert_auto_accept(auto_accept: str, review: str) -> None:
    """Say out loud when auto-accept is live but the review it tiers is not: the tier decision is
    read only inside the live review branch, so with CAD_REPAIR_PACKAGE_REVIEW off a live
    CAD_REPAIR_AUTO_ACCEPT is silently inert (the `_warn_inert_investigator` rule)."""
    if auto_accept == "live" and review != "live":
        logger.warning("CAD_REPAIR_AUTO_ACCEPT=live but CAD_REPAIR_PACKAGE_REVIEW=%r: the tier "
                       "is never consulted - it only tiers a review that is running", review)


_warn_inert_auto_accept(CAD_REPAIR_AUTO_ACCEPT, CAD_REPAIR_PACKAGE_REVIEW)


def _budget_attempts_posture() -> str:
    """The budget-driven-attempts posture, FAILING CLOSED: only the exact string `live` turns it
    on.

    `_auto_accept_posture`'s rule. Off is byte-for-byte the committed behaviour - every region
    gets CAD_REPAIR_REGION_ATTEMPTS, whatever the part costs - so a typo must not quietly hand a
    heavy part ten attempts.
    """
    raw = optional_env("CAD_REPAIR_BUDGET_ATTEMPTS", "off")
    if raw not in ("live", "off"):
        logger.warning("CAD_REPAIR_BUDGET_ATTEMPTS=%r is neither 'live' nor 'off'; reading it as "
                       "off", raw)
    return "live" if raw == "live" else "off"


#: Whether a region's PACKAGE ALLOWANCE comes from the part's expected mesh cost
#: (`cad/repair/grind.ATTEMPTS`, keyed by `grind.tier_of`) instead of the one flat
#: CAD_REPAIR_REGION_ATTEMPTS. "off" is byte-for-byte the prior behaviour, down to the report
#: gaining no key. "live" reads the tier from the run's FIRST whole-shape scan and the upload's own
#: face and solid counts; an unknown tier reads as the cheapest budget, and an `attempts` argument
#: a caller states explicitly still wins. The run's CAD_REPAIR_MAX_OPS, its deadline, each
#: package's own budget and the two-rejections escalation remain the outer bounds. A posture, so a
#: typo reads as off.
CAD_REPAIR_BUDGET_ATTEMPTS: str = _budget_attempts_posture()


def _warn_inert_budget_attempts(budget_attempts: str, region_loop: str) -> None:
    """Say out loud when the budget is live but the driver that reads it is not: the allowance is
    read only by the region-scoped driver, so with CAD_REPAIR_REGION_LOOP off a live
    CAD_REPAIR_BUDGET_ATTEMPTS is silently inert (the `_warn_inert_author` rule)."""
    if budget_attempts == "live" and region_loop != "live":
        logger.warning("CAD_REPAIR_BUDGET_ATTEMPTS=live but CAD_REPAIR_REGION_LOOP=%r: the "
                       "per-region allowance is read only by the region-scoped driver, so it is "
                       "never consulted", region_loop)


_warn_inert_budget_attempts(CAD_REPAIR_BUDGET_ATTEMPTS, CAD_REPAIR_REGION_LOOP)
