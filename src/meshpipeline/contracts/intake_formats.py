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


#: WHAT TO SEND IN PLACE OF A FORMAT THE MEASUREMENT CANNOT OPEN, by suffix.
#:
#: This is NOT a second accepted-format list and it decides nothing about admission at upload. It
#: answers one question: when the measurement reports that it cannot open a file's format at all -
#: `contracts/geometry_measurement.STATUS_UNSUPPORTED_FORMAT` - what does the customer do about it?
#:
#: .vtp is here because it is the one accepted format the measurement cannot read. The package reads
#: .stl/.obj/.ply/.off/.glb/.gltf through trimesh and .step/.stp/.iges/.igs through OpenCASCADE
#: (`geometry_agent.facts.load`); VTK PolyData is readable only inside the mesh engine's own bundle,
#: which is a different image from the ones that measure. So a .vtp upload used to be admitted and
#: then silently measured by nothing: no port table, no questions, no look, no plan, and no sentence
#: to the customer either. IT IS STILL ADMITTED - vmtk takes a .vtp natively and staging it loses
#: nothing - and it is now REFUSED AT ADMISSION with this sentence, before any builder runs.
#:
#: NOTHING IS ADDED HERE ON A GUESS. The generic sentence below names the formats that are both
#: accepted and measurable and stops there, because telling a customer to convert to something this
#: product has not measured would be worse than telling them nothing.
SEND_INSTEAD: dict[str, str] = {
    ".vtp": "Export the same surface as .stl and send that: it meshes identically on the vmtk "
            "engine, which takes either, and an .stl is measured, looked at and surveyed.",
}


def send_instead(suffix: str) -> str:
    """What to send in place of this format, in a sentence a customer can act on.

    Always a sentence. A refusal with no instruction is a wall, and this is reached only where the
    product has already decided it cannot measure what it was given.
    """
    s = (suffix or "").lower()
    if s in SEND_INSTEAD:
        return SEND_INSTEAD[s]
    return (f"Send the geometry as one of {', '.join(sorted(ACCEPTED_SUFFIXES - set(SEND_INSTEAD)))} "
            f"instead: those are the formats this product can measure.")


def capability_payload() -> dict:
    return {
        "formats": [
            {"key": f.key, "label": f.label, "suffixes": list(f.suffixes),
             "media_type": f.media_type, "declares_units": f.declares_units}
            for f in INTAKE_FORMATS
        ],
        "accept": ",".join(sorted(ACCEPTED_SUFFIXES)),
    }
