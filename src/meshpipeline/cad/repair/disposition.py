# Responsibility: Say, defect by defect, whether a repair caused what survives it or whether it
#                 arrived with the upload.
# Owns: the comparison rule (fingerprint AND measurement) and the sorted ledger it produces.
# Boundaries: pure arithmetic over two already-located defect lists. It imports no kernel, scans
#             nothing, and edits nothing. It invents no reclassification: that comes from a caller.
# Collaborates with: cad/repair/localize.py (whose defects it reads), cad/repair/region_loop.py
#                    (whose `RegionRun` holds the two scans), cad/repair/conservative.py (which
#                    reports the ledger as `measurements["disposition"]`).
from __future__ import annotations

from collections.abc import Iterable

# WHY THE FINGERPRINT ALONE IS NOT ENOUGH.
#
# Fingerprints are content-derived, so an entity the repair reshaped can keep its name. If "same
# name" meant "inherent", a repair could quietly move an edge and the report would call the result
# the customer's own geometry. A defect is INHERENT only when its fingerprint AND its measurements
# are both unchanged; a surviving fingerprint whose measurements moved was touched, so it is
# INTRODUCED (the defect now present is not the one that arrived).
#
# THE COMPARISON IS EXACT EQUALITY, with no tolerance, over every measurement EXCEPT the drop-list
# below. Two inspections of the same bytes are guaranteed the same report (localize_scan's
# contract), so an untouched entity measures bit-identically; a tolerance would be a place for a
# real change to hide.
#
# THE DROP-LIST IS KEYS THAT ARE NOT THE ENTITY'S OWN CONTENT:
#   boundary_edges, crossing_face_indices - raw 1-based entity indices. ShapeFix renumbers
#       everything it touches, so a survivor's indices change when the repair works NEXT to it.
#   relative_size - a ratio to the part's bounding diagonal; it drifts when the box moves.
# The intrinsic absolute measures (boundary_length, length, area, ...) stay compared.
#
# WHY A DROP-LIST AND NOT AN ALLOWLIST OF CONTENT KEYS - DO NOT FLIP THIS. An allowlist fails
# toward NOT comparing a key it does not know, which yields a false `inherent`: the function
# excusing a repair that did touch something, which cannot be seen or recovered from. A drop-list
# fails toward comparing a new key, which yields a false `introduced`: wrongly blaming ourselves,
# which is visible and recoverable. For a function whose purpose is trust, only the second is
# acceptable.
NON_CONTENT_KEYS = frozenset({"boundary_edges", "crossing_face_indices", "relative_size"})


def _content(measurements) -> dict:
    return {k: v for k, v in dict(measurements or {}).items() if k not in NON_CONTENT_KEYS}


def _scan(defects: Iterable) -> tuple[dict[str, dict], set[str], int]:
    """(content by fingerprint, fingerprints seen more than once, defects with no fingerprint)."""
    by_fp: dict[str, dict] = {}
    repeated: set[str] = set()
    unlocated = 0
    for d in defects:
        fp = d.fingerprint
        if not fp:
            unlocated += 1
        elif fp in by_fp:
            repeated.add(fp)
        else:
            by_fp[fp] = _content(getattr(d, "measurements", None))
    return by_fp, repeated, unlocated


def ledger(before: Iterable, after: Iterable, reclassified: Iterable[str] = ()) -> dict:
    """Classify every attributable defect by what the repair did to it. All lists are sorted.

    inherent     - in both scans, content measurements unchanged: the upload's own.
    introduced   - in the final scan only, or in both with a moved measurement.
    resolved     - in the upload's scan only: cleared.
    reclassified - passed through from the caller; nothing in this module reclassifies.
    ambiguous    - a fingerprint shared by two or more defects in either scan
                   (`EntityDefect.ambiguous`). They cannot be told apart, so neither "the
                   customer's" nor "ours" would be a finding; they are in no other bucket.
    unlocated    - a COUNT of defects (both scans) with an empty fingerprint, i.e. no measurable
                   extent. An entity with no name cannot be attributed in either direction.
    """
    was, was_dup, was_blank = _scan(before)
    now, now_dup, now_blank = _scan(after)
    ambiguous = was_dup | now_dup
    inherent = [fp for fp in now if fp in was and was[fp] == now[fp]]
    introduced = [fp for fp in now if fp not in was or was[fp] != now[fp]]
    resolved = [fp for fp in was if fp not in now]
    keep = lambda fps: sorted(fp for fp in fps if fp not in ambiguous)  # noqa: E731
    return {"inherent": keep(inherent), "introduced": keep(introduced),
            "resolved": keep(resolved), "reclassified": sorted(set(reclassified)),
            "ambiguous": sorted(ambiguous), "unlocated": was_blank + now_blank}


__all__ = ["NON_CONTENT_KEYS", "ledger"]
