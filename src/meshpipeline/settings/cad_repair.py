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
#: WHY IT IS NOT ON BY DEFAULT. A flat patch across a flat loop looks determined, and across an
#: isolated opening it is. But the two rims of a DRILLED THROUGH-HOLE are also flat closed loops,
#: and patching them seals the hole - turning a part with a bolt hole into a part without one,
#: which is a ruined part rather than a repaired one. Telling those two cases apart needs a
#: discriminator this code does not have yet (paired, parallel, congruent loops offset along their
#: normal are a missing tube wall, not two holes). Until it does, filling is an operator's
#: decision on evidence, not an autonomous one, and the unfilled loops are reported with their
#: reasons so that evidence exists.
CAD_REPAIR_FILL_PLANAR_HOLES: bool = bool_env("CAD_REPAIR_FILL_PLANAR_HOLES", "false")

#: The largest hole that may be patched, as a fraction of the part's diagonal measured across the
#: hole's own bounding box. Past this it is not a hole, it is a missing wall - and a flat lid over
#: a missing wall is a different part, not a repaired one.
CAD_REPAIR_MAX_HOLE_SPAN_RATIO: float = float(
    optional_env("CAD_REPAIR_MAX_HOLE_SPAN_RATIO", "0.75"))

# NO TIMEOUT SETTING HERE, deliberately. ShapeFix on a pathological shape can run for a very long
# time and a worker holding a lease is not free - but OpenCASCADE offers no interruption point, so
# a declared timeout would be a number nothing honours. The bound that exists is the run's own
# deadline (application/pipeline_budget.py). A knob that cannot be enforced is worse than its
# absence: it reads as a guarantee.
