# Responsibility: One vocabulary for length units, shared by the STEP reader, writer and checker.
# Owns: `canonical_unit` and the writer's unit codes.
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


def canonical_unit(name: str) -> str:
    """The canonical short name for `name`; an unknown name comes back lower-cased, unchanged."""
    cleaned = name.strip().lower()
    return _ALIASES.get(cleaned, cleaned)
