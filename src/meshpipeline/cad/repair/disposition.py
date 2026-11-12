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
# THE COMPARISON IS EXACT EQUALITY ON THE WHOLE `measurements` DICT, with no tolerance. Two
# inspections of the same bytes are guaranteed the same report (localize_scan's contract), so an
# untouched entity measures bit-identically; any difference, however small, is a real change. A
# tolerance would be a place for a real change to hide. Comparing the whole dict also needs no
# per-code knowledge of which keys a defect carries.


def _index(defects: Iterable) -> dict[str, dict]:
    return {d.fingerprint: dict(getattr(d, "measurements", None) or {}) for d in defects}


def ledger(before: Iterable, after: Iterable, reclassified: Iterable[str] = ()) -> dict:
    """Classify every defect by what the repair did to it. All lists are sorted.

    inherent     - in both scans, measurement unchanged: the upload's own.
    introduced   - in the final scan only, or in both with a moved measurement.
    resolved     - in the upload's scan only: cleared.
    reclassified - passed through from the caller; nothing in this module reclassifies.
    """
    was, now = _index(before), _index(after)
    inherent = [fp for fp in now if fp in was and was[fp] == now[fp]]
    introduced = [fp for fp in now if fp not in was or was[fp] != now[fp]]
    resolved = [fp for fp in was if fp not in now]
    return {"inherent": sorted(inherent), "introduced": sorted(introduced),
            "resolved": sorted(resolved), "reclassified": sorted(set(reclassified))}


__all__ = ["ledger"]
