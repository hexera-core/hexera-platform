# Responsibility: Give a B-rep entity a name that survives a repair, so a report can say whether
#                 THIS defect went away rather than only whether the count fell.
# Owns: the fingerprint, the ref table and its collision marking, and the old->new mapping shape.
# Boundaries: pure arithmetic on boxes somebody else measured. It imports no kernel, holds no
#             shape, and never decides whether a repair worked.
# Collaborates with: cad/repair/localize.py (whose `_centroid_and_box` payload is this module's
#                    input), cad/repair/operations.py (which fills a mapping from kernel history),
#                    cad/repair/session.py (which carries the table forward across an operation).
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

# WHY AN ENTITY NEEDS A NAME AT ALL.
#
# `localize.EntityDefect.entity_index` is an index into `TopTools_IndexedMapOfShape`, which is
# stable for ONE shape and meaningless across two. ShapeFix renumbers everything it touches. So
# "edge 12 was broken, is edge 12 fixed?" is unanswerable after a repair - the edge 12 in the new
# shape may be a different edge - and the only question the old code could answer was whether the
# total count fell. A count falling is not the same as the defect being gone.
#
# WHAT THE NAME IS MADE OF: the entity's type and its bounding box, quantised. Not a centroid and
# a size, because an edge's size is a length and a face's is an area, and quantising two
# incomparable quantities with one rule gets one of them wrong. The box is what
# `localize._centroid_and_box` already computes for every defect, so this costs nothing extra.
#
# WHAT IT IS NOT: a persistent id. Two different uploads of the same part produce the same
# fingerprints only by coincidence of geometry, and nothing here claims otherwise. It is a name
# good for the length of one repair run.

#: The grid is this fraction of the part's diagonal, and each entity's box is rounded onto it.
#: THIS IS A BEST-EFFORT NAME, NOT AN IDENTITY. Rounding to a grid cannot promise that a small move
#: keeps the name: two boxes a distance d apart share their cells only when no cell edge falls
#: between them, which happens with probability about d/q per coordinate. So a sub-quantum nudge
#: USUALLY keeps the name and occasionally renames the entity. That is still enough because
#: identity across a repair is also carried by the operation's kernel mapping (`EntityMapping`),
#: and a consumer must therefore treat a fingerprint that simply vanished, with no `gone` entry
#: explaining it, as UNKNOWN rather than as FIXED.
#:
#: THE SECOND HALF OF THIS NOTE USED TO READ "a real repair tolerance is hundreds of quanta on an
#: ordinary part, so the name changes anyway", and that was the cover under which a real bug sat
#: for a whole corpus run: the grid itself was scaled off the part's CURRENT diagonal, so an
#: entity nobody touched was renamed whenever the part's extent moved at all, and the progress
#: rule read that as the defect being gone. A name that changes "anyway" is not a name. See
#: `quantum_for`, which now snaps the diagonal to an octave so an untouched entity keeps its name.
QUANTUM_FRACTION = 1e-6

#: The finest grid used when the part has no measurable extent. An empty compound and a
#: single-vertex shape both reach here, and a zero quantum divides by zero.
MIN_QUANTUM_MM = 1e-7

#: What `fate_of` returns for an entity the kernel says it deleted.
GONE = "gone"


def quantum_for(diagonal_mm: float) -> float:
    """The grid an entity's box is rounded onto, scaled to the part.

    SCALED TO THE PART'S OCTAVE, NOT TO ITS DIAGONAL, and that is the difference between a name
    and a coincidence. The grid used to be the diagonal times `QUANTUM_FRACTION`, so ANY change
    in the part's extent moved every cell boundary on the part - and a repair changes the extent
    by construction. Measured on 0059_00592781_58be5d61afe56e0fcdda08e3_step_000.step: one
    `fix_small_edges` call left all four zero-length edges at bounding boxes identical to the
    micrometre and grew the diagonal from 1822.8447 mm to 1822.9358 mm. That is 5e-5 relative,
    which at z = -877.5 mm is twenty-four cells, so all four fingerprints changed, the progress
    rule could not find the defect it had just named, and an operation that repaired NOTHING was
    kept. The bug ran the wrong way round from the one anybody feared: it never rolled a real
    repair back, it credited a non-repair.

    So the diagonal is first snapped UP to a power of two (`math.frexp` reads the exponent
    exactly, with no log and no rounding), and the grid is a fraction of that. Two scans of one
    part agree unless a repair changed its size by nearly a factor of two, which no cap permits.
    The cost is up to one octave of resolution - the grid is between 0.5 and 1 parts per million
    of the part either way - and the honest residual limit is a part whose diagonal straddles a
    power of two, where the grid still moves.
    """
    if not diagonal_mm or not math.isfinite(diagonal_mm) or diagonal_mm <= 0:
        return MIN_QUANTUM_MM
    _mantissa, exponent = math.frexp(float(diagonal_mm))
    octave = math.ldexp(1.0, exponent)
    return max(octave * QUANTUM_FRACTION, MIN_QUANTUM_MM)


def fingerprint_of(entity_type: str, box: dict, *, quantum: float) -> str:
    """A short, stable name for one entity, or "" when there is nothing to name it by.

    AN EMPTY STRING IS A REAL ANSWER. `localize._centroid_and_box` returns `{}` for an entity with
    no measurable extent, and a fingerprint invented from that would match every other
    unmeasurable entity - so an operation could aim at one and "fix" another. Such a defect is
    UNTARGETABLE, and the planner reports skipping it.
    """
    low, high = box.get("bbox_min"), box.get("bbox_max")
    if not low or not high or len(low) != 3 or len(high) != 3:
        return ""
    coords = [float(v) for v in (*low, *high)]
    if not all(math.isfinite(v) for v in coords):
        return ""
    q = quantum if quantum > 0 else MIN_QUANTUM_MM
    cells = [int(round(v / q)) for v in coords]
    digest = hashlib.blake2s(
        f"{entity_type}|{'|'.join(str(c) for c in cells)}".encode(), digest_size=8).hexdigest()
    return f"{entity_type}:{digest}"


@dataclass(frozen=True, slots=True)
class EntityRef:
    """One entity, named so that an operation can aim at it and a later scan can look for it."""

    entity_type: str
    fingerprint: str
    #: Index into the CURRENT shape's indexed map. How the kernel is actually handed this entity;
    #: valid only for the shape the ref was built from.
    entity_index: int = 0
    #: Another entity shares this one's box, so the fingerprint cannot tell them apart. A verdict
    #: about an ambiguous target may not claim the kernel confirmed anything about it.
    ambiguous: bool = False

    @property
    def targetable(self) -> bool:
        return bool(self.fingerprint)


@dataclass(frozen=True, slots=True)
class EntityMapping:
    """What one operation did to the entities it touched, as the kernel reported it.

    `basis` is the honesty of the whole mapping: `kernel` when the operation had a history API to
    read (`ShapeBuild_ReShape`, `BRepBuilderAPI_Sewing`), `geometric` when it had none and the
    caller must fall back to comparing boxes. A training signal that cannot tell those apart
    overstates its own confidence, which is why it travels with the mapping rather than being
    inferred later.
    """

    moved: dict[str, str] = field(default_factory=dict)
    gone: frozenset[str] = frozenset()
    basis: str = "geometric"

    @classmethod
    def empty(cls, basis: str = "geometric") -> EntityMapping:
        return cls(moved={}, gone=frozenset(), basis=basis)

    def fate_of(self, fingerprint: str) -> tuple[str, str]:
        """What became of one entity: `(GONE, "")`, `("moved", new)`, or `("unknown", same)`.

        UNKNOWN IS THE COMMON CASE and not a gap. An operation's history records what it CHANGED,
        and most of a repaired shape is untouched, so an entity the mapping never mentions is one
        the operation did not touch.
        """
        if fingerprint in self.gone:
            return GONE, ""
        if fingerprint in self.moved:
            return "moved", self.moved[fingerprint]
        return "unknown", fingerprint


def refs_for(boxes: list[tuple[str, int, dict]],
             *, diagonal_mm: float) -> tuple[EntityRef, ...]:
    """Name every entity in a list of `(entity_type, entity_index, box)`, marking collisions.

    TWO ENTITIES CAN SHARE A BOX: a duplicated face, two coincident edges a bad export left
    behind. They get distinct keys so the table stays addressable, and both are marked
    `ambiguous` - because what the fingerprint cannot distinguish, no verdict may claim it did.

    ONE ENTITY LISTED TWICE IS NOT THAT CASE. The localiser reports a defect per code, so the same
    `entity_index` arrives here more than once with the same box, and treating that as a collision
    was a bug with teeth: the suffixed `edge:abc#0` / `edge:abc#1` are names the NEXT scan will not
    produce (its survivor is plain `edge:abc`), so the progress rule looked for a defect it could
    never find and kept an operation that had repaired nothing. A collision is therefore judged on
    the INDEX as well as the box: same index plus same box is one entity reported twice.
    """
    quantum = quantum_for(diagonal_mm)
    named = [(kind, index, fingerprint_of(kind, box, quantum=quantum))
             for kind, index, box in boxes]

    #: Which distinct entities each fingerprint names. More than one is a real collision.
    owners: dict[str, set[tuple[str, int]]] = {}
    for kind, index, fingerprint in named:
        if fingerprint:
            owners.setdefault(fingerprint, set()).add((kind, int(index)))

    out: list[EntityRef] = []
    #: The suffix a genuinely colliding entity gets, kept PER ENTITY so the same entity listed
    #: twice keeps one key rather than being handed two.
    assigned: dict[tuple[str, int, str], str] = {}
    used: dict[str, int] = {}
    for kind, index, fingerprint in named:
        collided = bool(fingerprint) and len(owners.get(fingerprint, ())) > 1
        key = fingerprint
        if collided:
            entity = (kind, int(index), fingerprint)
            if entity in assigned:
                out.append(EntityRef(entity_type=kind, fingerprint=assigned[entity],
                                     entity_index=int(index), ambiguous=True))
                continue
            nth = used.get(fingerprint, 0)
            used[fingerprint] = nth + 1
            key = f"{fingerprint}#{nth}"
            assigned[entity] = key
        out.append(EntityRef(entity_type=kind, fingerprint=key, entity_index=int(index),
                             ambiguous=collided))
    return tuple(out)
