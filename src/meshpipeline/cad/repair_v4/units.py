# Responsibility: One vocabulary for length units, shared by the STEP reader, writer and checker.
# Owns: `canonical_unit`, the writer's unit codes and `UNBOUNDED_DIAGONAL_MM`, the one size beyond which an
#       input is unbounded rather than large.
# Boundaries: pure - no OCCT, safe in the parent process.
"""Length-unit names: STEP's own names and common spellings map to one canonical short name."""

from __future__ import annotations

_ALIASES = {
    "mm": "mm", "millimetre": "mm", "millimeter": "mm", "millimetres": "mm", "millimeters": "mm",
    "cm": "cm", "centimetre": "cm", "centimeter": "cm", "centimetres": "cm", "centimeters": "cm",
    "m": "m", "metre": "m", "meter": "m", "metres": "m", "meters": "m",
    "km": "km", "kilometre": "km", "kilometer": "km",
    "um": "um", "micrometre": "um", "micrometer": "um", "micron": "um", "microns": "um",
    "in": "inch", "inch": "inch", "inches": "inch",
    "ft": "ft", "foot": "ft", "feet": "ft",
    "mi": "mi", "mile": "mi", "miles": "mi",
    "mil": "mil", "thou": "mil",
    "uin": "uin", "microinch": "uin",
}  # fmt: skip

# The values OCCT's `write.step.unit` accepts.
WRITER_CODES = {
    "mm": "MM", "cm": "CM", "m": "M", "km": "KM", "um": "UM",
    "inch": "INCH", "ft": "FT", "mi": "MI", "mil": "MIL", "uin": "UIN",
}  # fmt: skip


#: A part whose bounding-box diagonal (mm, the BREP's own unit) exceeds this is UNBOUNDED, not large: layer 1 reports
#: `unbounded_geometry` and nothing downstream sizes, meshes or sweeps it. OCCT writes an infinite surface or curve into a
#: bounding box as +-Precision::Infinite (1e100 per side, so a diagonal of 3.46e100 mm, the 3.5e100 the corpus sample
#: measured). 1e9 mm is 1,000 km: ~3.5e3 times above a 200 m part's 2.8e5 mm diagonal, 4.5e4 times above the corpus p95
#: (22 m), above any single model a CAD file holds, and 91 orders of magnitude below the sentinel, so neither a real part
#: nor an infinite one can land on the wrong side. It is a classification boundary, not a kernel tolerance.
UNBOUNDED_DIAGONAL_MM = 1e9


def canonical_unit(name: str) -> str:
    """The canonical short name for `name`; an unknown name comes back lower-cased, unchanged."""
    cleaned = name.strip().lower()
    return _ALIASES.get(cleaned, cleaned)
