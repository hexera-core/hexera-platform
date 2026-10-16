# Responsibility: Judge a repair's result - what one operation did, and what the whole run has done
#                 to the customer's part since it arrived.
# Owns: the measurement a judgement is made on, the per-operation sanity checks, and the
#       cumulative checks against the ORIGINAL upload.
# Boundaries: it measures and refuses. It repairs nothing, holds no shape across calls, and has no
#             opinion about which operation to try next.
# Collaborates with: settings/cad_repair.py (the caps), cad/repair/session.py (which calls these
#                    before paying for a re-inspection), cad/repair/operations.py (whose evidence
#                    declares the area and volume each operation expects to add).
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace

import meshpipeline.settings.cad_repair as rcfg

logger = logging.getLogger(__name__)

# WHY THE CHECKS ARE SPLIT IN TWO.
#
# The whole-shape repair had one `_refusal(before, after)` because there was one operation. A loop
# has two different questions:
#
#   PER OPERATION - did this step damage the shape? Counts and validity only, because this runs
#   BEFORE the re-inspection and the re-inspection is what costs. A cheap check that catches a
#   destroyed solid saves a `localize_scan` on a shape nobody will keep.
#
#   CUMULATIVE - has the run as a whole moved the part further than it is allowed to? Always
#   measured against the ORIGINAL UPLOAD, never against the previous iteration. That is what makes
#   a loop as safe as a single pass: six operations each individually inside the deviation cap
#   cannot drift past it together, because nothing is ever compared to the last step.
#
# AND WHY AREA AND VOLUME ARE ACCOUNTED FOR RATHER THAN ALLOWED.
#
# The old check compared area and volume against the original with the HOLE-SPAN cap (0.75) as the
# allowance, because patching a hole legitimately moves both and nothing could say by how much.
# A 75% allowance is not a cap; it is a record of not knowing. Now each operation DECLARES the
# area and volume it expects to contribute, the comparison is against `original + declared`, and
# the residual is tight. An operation that declared 50 mm2 and moved 200 is refused - not for
# moving surface, but for not understanding what it did.


@dataclass(frozen=True, slots=True)
class Measure:
    """Everything a judgement about a shape is made from, in millimetres."""

    valid: bool
    solids: int
    shells: int
    faces: int
    edges: int
    vertices: int
    diagonal_mm: float
    max_tolerance_mm: float
    surface_area_mm2: float
    volume_mm3: float

    def to_dict(self) -> dict:
        return {"valid": bool(self.valid), "solids": int(self.solids), "shells": int(self.shells),
                "faces": int(self.faces), "edges": int(self.edges),
                "vertices": int(self.vertices), "diagonal_mm": float(self.diagonal_mm),
                "max_tolerance_mm": float(self.max_tolerance_mm),
                "surface_area_mm2": float(self.surface_area_mm2),
                "volume_mm3": float(self.volume_mm3)}

    @classmethod
    def from_dict(cls, payload: dict) -> Measure:
        """Rebuild a measurement from a recorded one, tolerating the older report's key set."""
        return cls(
            valid=bool(payload.get("valid", False)),
            solids=int(payload.get("solids", 0) or 0),
            shells=int(payload.get("shells", 0) or 0),
            faces=int(payload.get("faces", 0) or 0),
            edges=int(payload.get("edges", 0) or 0),
            vertices=int(payload.get("vertices", 0) or 0),
            diagonal_mm=float(payload.get("diagonal_mm", 0.0) or 0.0),
            max_tolerance_mm=float(payload.get("max_tolerance_mm", 0.0) or 0.0),
            surface_area_mm2=float(payload.get("surface_area_mm2", 0.0) or 0.0),
            volume_mm3=float(payload.get("volume_mm3", 0.0) or 0.0))


def measure(shape) -> Measure:
    """Measure a live shape. The only function here that touches the kernel."""
    from meshpipeline.cad.repair.conservative import (
        _area_and_volume,
        _counts,
        _diagonal_mm,
        _is_valid,
        _max_tolerance_mm,
    )

    counts = _counts(shape)
    extent = _area_and_volume(shape)
    return Measure(
        valid=_is_valid(shape),
        solids=int(counts.get("solids", 0)), shells=int(counts.get("shells", 0)),
        faces=int(counts.get("faces", 0)), edges=int(counts.get("edges", 0)),
        vertices=int(counts.get("vertices", 0)),
        diagonal_mm=float(_diagonal_mm(shape)),
        max_tolerance_mm=float(_max_tolerance_mm(shape)),
        surface_area_mm2=float(extent.get("surface_area_mm2") or 0.0),
        volume_mm3=float(extent.get("volume_mm3") or 0.0))


def sanity_refusal(before: Measure, after: Measure) -> str:
    """Did THIS operation damage the shape? Counts and validity only. "" accepts.

    Deliberately cheap: it runs before the re-inspection so that a destroyed solid costs no
    `localize_scan`.
    """
    if after.faces == 0:
        return "the operation left no faces to mesh"
    if after.solids < before.solids:
        # A SOLID IS WHAT MAKES A PART A VOLUME. Losing one turns a closed body into a bag of
        # surfaces, which most meshers either refuse or quietly mesh as something else.
        return (f"the operation lost {before.solids - after.solids} solid(s) - a closed body "
                "became loose surfaces, which is damage rather than repair")
    if after.faces < before.faces and not rcfg.CAD_REPAIR_ALLOW_FACE_REMOVAL:
        # DELETING A FACE IS DEFEATURING, a judgement about what the part is for.
        return (f"the operation removed {before.faces - after.faces} face(s); deleting geometry "
                "is an operator decision, not a conservative repair")
    if before.valid and not after.valid:
        return "the operation turned a valid shape into an invalid one"
    return ""


def cumulative_refusal(original: Measure, current: Measure, *,
                       declared_area_mm2: float, declared_volume_mm3: float) -> str:
    """Has the RUN moved the part further than it may? Measured against the upload. "" accepts."""
    # A NON-FINITE MEASUREMENT REFUSES, on either side. Every comparison below is `x > cap` or
    # `original > 0`, and NaN compares False against everything, so a NaN would skip checks or pass
    # them and the repair would be accepted on a number that was never taken. An unmeasurable
    # upload has nothing to bound the run against; an unmeasurable result is not approvable.
    unmeasured = _unmeasured(original)
    if unmeasured:
        return (f"the upload's own measurement could not be taken ({', '.join(unmeasured)} is not "
                "finite), so nothing can be bounded against it")
    unmeasured = _unmeasured(current)
    if unmeasured:
        return (f"the run's own measurement could not be taken ({', '.join(unmeasured)} is not "
                "finite), so the result cannot be approved")

    if current.max_tolerance_mm > rcfg.CAD_REPAIR_MAX_TOLERANCE_MM:
        return (f"the run left a tolerance of {current.max_tolerance_mm:.4f} mm, over the "
                f"{rcfg.CAD_REPAIR_MAX_TOLERANCE_MM} mm cap - the kernel agreed to stop "
                "complaining rather than fixing the geometry")

    if original.diagonal_mm > 0:
        moved = abs(current.diagonal_mm - original.diagonal_mm) / original.diagonal_mm
        if moved > rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO:
            return (f"the run moved the part's overall size by {moved:.5f} of its original "
                    f"diagonal, over the {rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO} cap")

    # THE BOUNDING BOX CANNOT SEE INSIDE. An internal wall moved, a cavity reshaped or a passage
    # sealed leaves the part's overall extent untouched. Area and volume move for those.
    area = _accounted(original.surface_area_mm2, current.surface_area_mm2, declared_area_mm2,
                      rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO)
    if area:
        return f"the run's surface area {area}"

    if original.solids == 0 or current.solids == 0:
        # A SHAPE WITH NO SOLID HAS NO INSIDE, so there is no volume for anything to have moved
        # within. OpenCASCADE's volume integral over an open shell is a number, not a volume:
        # it reports 799.9999999999998 mm3 for the 5-face open box of the open-shell fixture. A
        # comparison against it is meaningless in both directions, and closing that shell to its
        # true 1000 mm3 solid would read as 25% unexplained. A solid LOST is still caught by
        # `sanity_refusal`'s solid-count check, which is the case that matters. Area stays
        # compared above, because surface area is meaningful for a shell.
        logger.info("caps: volume not compared (solids %d -> %d); a shape with no solid has no "
                    "volume to move (%.3f -> %.3f mm3)", original.solids, current.solids,
                    original.volume_mm3, current.volume_mm3)
        return ""
    volume = _accounted(original.volume_mm3, current.volume_mm3, declared_volume_mm3,
                        rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)
    if volume:
        return f"the run's volume {volume}"
    return ""


def _unmeasured(m: Measure) -> list[str]:
    """The float fields of a measurement that are NaN or infinite. Counts and booleans cannot be."""
    return [name for name in ("diagonal_mm", "max_tolerance_mm", "surface_area_mm2", "volume_mm3")
            if not math.isfinite(getattr(m, name))]


def _accounted(original: float, current: float, declared: float, residual: float) -> str:
    """Is the measured change explained by what the operations said they would add?

    A ZERO ORIGINAL IS NO OPINION, not infinite change: a shape with no extent reaches here, and
    dividing by its zero is the bug rather than the finding.
    """
    if original <= 0:
        return ""
    expected = original + declared
    unexplained = abs(current - expected) / original
    if unexplained > residual:
        return (f"ended {unexplained:.4f} of the original away from what the operations "
                f"accounted for ({original:.3f} + {declared:.3f} declared = {expected:.3f}, "
                f"measured {current:.3f}), over the {residual} residual - something moved that "
                "no operation claimed")
    return ""


@dataclass(frozen=True, slots=True)
class Accounting:
    """What the operations KEPT so far said they would add. Immutable, so a rolled-back operation
    cannot leave a trace: the session only ever calls `plus` for a verdict it accepted."""

    declared_area_mm2: float = 0.0
    declared_volume_mm3: float = 0.0

    def plus(self, evidence: dict) -> Accounting:
        """Add one operation's declaration. Silence means zero - ShapeFix and sewing move no
        surface ON PURPOSE, so declaring nothing is a statement, not a gap."""
        return replace(
            self,
            declared_area_mm2=self.declared_area_mm2 + float(evidence.get("area_delta_mm2", 0.0)
                                                             or 0.0),
            declared_volume_mm3=self.declared_volume_mm3 + float(
                evidence.get("volume_delta_mm3", 0.0) or 0.0))
