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
    #: Each closed body's own volume, largest first. EMPTY MEANS NOT MEASURED, which is why
    #: `solid_change` reports `known: False` rather than guessing: an older construction, a
    #: tolerant rebuild and a stubbed measurement all arrive without it.
    #:
    #: WHY PER BODY. An aggregate count plus an aggregate volume cannot tell a loose shell
    #: CLOSING into a new body from an existing body being SPLIT in two. On 0051_00513292 a
    #: committed `sew_faces` took solids 7 -> 8 and volume +0.941 mm3 against a declared 0 - which
    #: is what a 0.94 mm3 shell closing looks like, and also what a split that moved a little
    #: looks like. One is the repair working and the other is damage.
    solid_volumes: tuple[float, ...] = ()

    def to_dict(self) -> dict:
        return {"valid": bool(self.valid), "solids": int(self.solids), "shells": int(self.shells),
                "faces": int(self.faces), "edges": int(self.edges),
                "vertices": int(self.vertices), "diagonal_mm": float(self.diagonal_mm),
                "max_tolerance_mm": float(self.max_tolerance_mm),
                "surface_area_mm2": float(self.surface_area_mm2),
                "volume_mm3": float(self.volume_mm3),
                # ONLY WHEN MEASURED, so a reader can tell "one body of 8000 mm3" from "nobody
                # looked", and a `Measure(**m.to_dict())` round trip keeps whichever it was.
                **({"solid_volumes": [float(v) for v in self.solid_volumes]}
                   if self.solid_volumes else {})}

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
    solid_volumes = _solid_volumes(shape)
    return Measure(
        valid=_is_valid(shape),
        solids=int(counts.get("solids", 0)), shells=int(counts.get("shells", 0)),
        faces=int(counts.get("faces", 0)), edges=int(counts.get("edges", 0)),
        vertices=int(counts.get("vertices", 0)),
        diagonal_mm=float(_diagonal_mm(shape)),
        max_tolerance_mm=float(_max_tolerance_mm(shape)),
        surface_area_mm2=float(extent.get("surface_area_mm2") or 0.0),
        volume_mm3=float(extent.get("volume_mm3") or 0.0),
        solid_volumes=solid_volumes)


def _solid_volumes(shape) -> tuple[float, ...]:
    """Each solid's own volume, largest first, or () when there is nothing to measure.

    ONE INTEGRAL PER BODY, on top of the whole-shape one `measure` already takes. A part with
    seven bodies pays seven cheap integrations against a `BRepCheck_Analyzer` that dominates this
    function; a shape the kernel cannot walk reports () and every reader treats that as unknown.
    """
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer

        found: list[float] = []
        walk = TopExp_Explorer(shape, TopAbs_SOLID)
        while walk.More():
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(walk.Current(), props)
            found.append(abs(float(props.Mass())))
            walk.Next()
        return tuple(sorted(found, reverse=True))
    except Exception as exc:  # noqa: BLE001 - an unmeasurable shape is unknown, never zero bodies
        logger.debug("caps: per-solid volumes not measured (%s)", exc)
        return ()


#: How close two volumes must be to be the same body. The run's own volume ratio, so what counts
#: as "conserved" here is what counts as conserved everywhere else.
def _volume_tolerance(volume: float) -> float:
    return abs(float(volume)) * float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)


def solid_change(before: Measure, after: Measure) -> dict:
    """WHICH bodies changed, not how many. Pure.

    `{known, appeared_mm3, disappeared_mm3, split}`. Bodies are matched by volume, largest first,
    within the run's own volume ratio - the only identity an aggregate measurement can offer, and
    enough to separate the three cases that matter:

      - a body that APPEARED carries volume no operation could have declared (a shell closed),
      - a body that DISAPPEARED is the loss `sanity_refusal` already refuses,
      - a body that SPLIT is one before-body whose volume is the sum of several after-bodies: its
        pieces are not new volume, they are the old body cut, and that is damage rather than
        repair (`fragment_bodies` declines its own version of this as `body_split`).

    `known: False` when either side carries no per-solid volumes. Nothing is claimed from a
    measurement nobody took.
    """
    return solid_change_of(before.solid_volumes, after.solid_volumes)


def solid_change_of(before_volumes, after_volumes) -> dict:
    """`solid_change` over the two volume lists themselves, for a reader that holds the measured
    dicts rather than the `Measure`s - `package_review.evidence_of` is handed both as JSON."""
    if not before_volumes or not after_volumes:
        return {"known": False, "appeared_mm3": None, "disappeared_mm3": None, "split": ()}
    left = sorted((float(v) for v in before_volumes), reverse=True)
    right = sorted((float(v) for v in after_volumes), reverse=True)
    split: list[tuple[float, tuple[float, ...]]] = []

    # MATCHED FIRST, biggest to smallest: a body whose volume survived is the same body.
    for volume in list(left):
        match = next((v for v in right if abs(v - volume) <= _volume_tolerance(volume)), None)
        if match is not None:
            left.remove(volume)
            right.remove(match)

    # THEN THE SPLITS: an unmatched before-body whose volume is the sum of the smallest
    # unmatched after-bodies that reach it. Greedy, largest before-body first, which is the same
    # order the matching used.
    for volume in list(left):
        taken: list[float] = []
        running = 0.0
        for piece in sorted(right, reverse=True):
            if running >= volume - _volume_tolerance(volume):
                break
            taken.append(piece)
            running += piece
        if len(taken) > 1 and abs(running - volume) <= _volume_tolerance(volume):
            split.append((volume, tuple(taken)))
            left.remove(volume)
            for piece in taken:
                right.remove(piece)

    return {"known": True,
            "appeared_mm3": float(sum(right)),
            "disappeared_mm3": float(sum(left)),
            "split": tuple(split)}


def sanity_refusal(before: Measure, after: Measure, *,
                   declared_faces_removed: int = 0) -> str:
    """Did THIS operation damage the shape? Counts and validity only. "" accepts.

    Deliberately cheap: it runs before the re-inspection so that a destroyed solid costs no
    `localize_scan`.

    `declared_faces_removed` is the face-count twin of the area declaration: the sliver sweep
    (`operations.FixSmallFaces`) states how many faces it removed, having verified each one is
    debris, and exactly that many fewer faces is accepted. One face beyond the declaration is
    still refused - accounted for, never allowed - and silence still means zero, so every other
    operation keeps the prior rule to the byte.
    """
    if after.faces == 0:
        return "the operation left no faces to mesh"
    if after.solids < before.solids:
        # A SOLID IS WHAT MAKES A PART A VOLUME. Losing one turns a closed body into a bag of
        # surfaces, which most meshers either refuse or quietly mesh as something else.
        return (f"the operation lost {before.solids - after.solids} solid(s) - a closed body "
                "became loose surfaces, which is damage rather than repair")
    removed = before.faces - after.faces
    if (removed > max(0, int(declared_faces_removed))
            and not rcfg.CAD_REPAIR_ALLOW_FACE_REMOVAL):
        # DELETING A FACE IS DEFEATURING, a judgement about what the part is for - unless an
        # operation licensed to sweep slivers declared exactly this removal.
        return (f"the operation removed {removed} face(s), "
                f"{max(0, int(declared_faces_removed))} declared; deleting geometry "
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

    # An upload can already exceed our tolerance policy. Repair may improve that body, but it may
    # not use an existing bad tolerance as permission to widen it further. Sound uploads retain
    # the configured absolute cap; out-of-policy uploads retain their measured starting ceiling.
    tolerance_ceiling = (max(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM, original.max_tolerance_mm)
                         if rcfg.CAD_REPAIR_MAX_TOLERANCE_MM > 0 else 0.0)
    if current.max_tolerance_mm > tolerance_ceiling:
        return (f"the run left a tolerance of {current.max_tolerance_mm:.4f} mm, over the "
                f"{tolerance_ceiling} mm effective cap - the kernel agreed to stop "
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

    if volume_incomparable(original, current):
        # A BODY THAT CLOSED IS NEW VOLUME NOBODY COULD HAVE PREDICTED. The first two cases are a
        # shape with no inside at all; the third is the MIXED ASSEMBLY `sew_faces` is built for -
        # one solid body plus one that arrived as loose faces, sewn closed and promoted, so the
        # count goes 1 -> 2 and the new body's volume appears. No operation can declare it:
        # whether a shell closed is the kernel's answer, not a prediction. Judging it against the
        # original refused a legitimate repair. A solid LOST is still caught by `sanity_refusal`'s
        # solid-count check, which is the direction that matters. Area stays compared above,
        # because surface area is meaningful for a shell.
        #
        # ON THE NO-SOLID CASES SPECIFICALLY: OpenCASCADE's volume integral over an open shell is
        # a number, not a volume - it reports 799.9999999999998 mm3 for the 5-face open box of the
        # open-shell fixture. A comparison against it is meaningless in both directions, and
        # closing that shell to its true 1000 mm3 solid would read as 25% unexplained.
        logger.info("caps: volume not compared (solids %d -> %d); no inside to measure, or a body "
                    "closed that nobody could have declared (%.3f -> %.3f mm3)",
                    original.solids, current.solids, original.volume_mm3, current.volume_mm3)
        # WHAT IS STILL BOUNDED, absolutely, from the CURRENT measurement alone (never a delta
        # against the original, whose integral is meaningless for a shell). A closed body has
        # strictly positive volume, and no shape whose bounding-box diagonal is d exceeds
        # d**3 / (3 * sqrt(3)) - the cube. That catches an integral gone wrong by orders of
        # magnitude without inventing a baseline the geometry cannot support.
        if current.solids > 0:
            if current.volume_mm3 <= 0:
                return (f"the run closed {current.solids} solid(s) but their volume measured "
                        f"{current.volume_mm3:.3f} mm3; a closed body has positive volume")
            ceiling = current.diagonal_mm ** 3 / (3 * math.sqrt(3))
            if current.volume_mm3 > ceiling:
                return (f"the run's volume {current.volume_mm3:.3f} mm3 is impossible for a body "
                        f"with a {current.diagonal_mm:.3f} mm bounding-box diagonal (at most "
                        f"{ceiling:.3f} mm3, the cube)")
        return ""
    volume = _accounted(original.volume_mm3, current.volume_mm3, declared_volume_mm3,
                        rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)
    if volume:
        return f"the run's volume {volume}"
    return ""


def volume_incomparable(original: Measure, current: Measure) -> bool:
    """Is a before-vs-after volume comparison meaningless for this pair of measurements?

    ONE PLACE, because `cumulative_refusal` decides whether to compare,
    `volume_accountability` reports whether it did, and `package.evaluate` asks the same question
    about one package - PUBLIC for that third reader, which is outside this module. Two hand-written copies of a three-way
    condition drift, and the drift is silent in the worst direction: a report that says the
    volume was judged against a run in which it was not.

    The three cases, and why none of them supports a comparison, are argued at the use site in
    `cumulative_refusal` - an open shell's volume integral is a number and not a volume, and a
    body that CLOSED is volume no operation could have declared.
    """
    return (original.solids == 0 or current.solids == 0
            or current.solids > original.solids)


def volume_accountability(original: Measure, current: Measure) -> dict:
    """Was the before-vs-after volume comparison made, and if not, why. For the report."""
    if volume_incomparable(original, current):
        return {"compared": False,
                "reason": ("the upload had no solid (an open shell's volume integral is not a "
                           "volume) or a body closed that no operation could have declared; only "
                           "an absolute plausibility bound was applied")}
    return {"compared": True, "reason": "solid count unchanged or lower; volume judged against "
                                        "the original plus declared"}


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
