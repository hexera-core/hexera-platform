# Responsibility: Hold the one description of what geometry this product accepts.
# Owns: the accepted-format table, the suffix mapping, the staged filename, and whether a format declares its own units.
# Boundaries: it answers what is accepted and what that implies; it opens no file and converts nothing.
# Collaborates with: api/v1/upload.py, agents/intake/ and every engine's staging seam.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntakeFormat:
    key: str                       # canonical identity, stable across UI and API
    label: str                     # what a user is shown
    suffixes: tuple[str, ...]      # accepted filename suffixes, lowercase, dot-prefixed
    media_type: str                # best-known media type ("" when there is no registered one)
    declares_units: bool           # does the FORMAT itself state its physical unit?
    staged_as: str                 # the representation staged into the workspace


#: Ordered as the UI lists them: the most common first.
INTAKE_FORMATS: tuple[IntakeFormat, ...] = (
    IntakeFormat(
        key="stl", label="STL surface mesh", suffixes=(".stl",),
        media_type="model/stl",
        # STL is a bare triangle soup - no unit is recorded anywhere in the file.
        declares_units=False, staged_as="input.stl"),
    IntakeFormat(
        key="step", label="STEP solid (AP203/AP214)", suffixes=(".step", ".stp"),
        media_type="model/step",
        # STEP carries a unit context, which OpenCASCADE normalises on read.
        declares_units=True, staged_as="input.step"),
    IntakeFormat(
        key="iges", label="IGES surface/solid", suffixes=(".iges", ".igs"),
        media_type="model/iges",
        declares_units=True, staged_as="input.iges"),
    IntakeFormat(
        key="vtp", label="VTK PolyData surface", suffixes=(".vtp",),
        media_type="application/vnd.vtk.polydata",
        # VTK PolyData stores raw coordinates with no unit declaration.
        declares_units=False, staged_as="input.vtp"),
)

#: Every accepted suffix. The server's validation predicate, and the picker's advisory list.
ACCEPTED_SUFFIXES: frozenset[str] = frozenset(
    s for f in INTAKE_FORMATS for s in f.suffixes)


def format_for_suffix(suffix: str) -> IntakeFormat | None:
    s = (suffix or "").lower()
    return next((f for f in INTAKE_FORMATS if s in f.suffixes), None)


def staged_name_for(suffix: str) -> str:
    fmt = format_for_suffix(suffix)
    return fmt.staged_as if fmt else "input.step"


def declares_units(suffix: str) -> bool:
    fmt = format_for_suffix(suffix)
    return bool(fmt and fmt.declares_units)


def unsupported_message(suffix: str) -> str:
    names = ", ".join(sorted(ACCEPTED_SUFFIXES))
    return f"Unsupported file type '{suffix}'. Accepted geometry formats: {names}."


def capability_payload() -> dict:
    return {
        "formats": [
            {"key": f.key, "label": f.label, "suffixes": list(f.suffixes),
             "media_type": f.media_type, "declares_units": f.declares_units}
            for f in INTAKE_FORMATS
        ],
        "accept": ",".join(sorted(ACCEPTED_SUFFIXES)),
    }
