# Responsibility: Hold the one description of what geometry this product accepts.
# Owns: the accepted-format table, the suffix mapping, the staged filename, whether a format declares its own units, and the refusal a user reads for anything else.
# Boundaries: it answers what is accepted and what that implies; it opens no file and converts nothing.
# Collaborates with: api/v1/upload.py, agents/intake/ and every engine's staging seam.
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


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


@dataclass(frozen=True)
class NativeCadFormat:
    """A CAD tool's own file format: not readable here, but every one of them exports STEP."""
    tool: str                      # who writes it, as the user knows them; "X or Y" when shared
    suffixes: tuple[str, ...]      # lowercase, dot-prefixed
    export: str                    # how to get a STEP out of that tool, in the tool's own menu words


#: Native CAD formats. OpenCASCADE reads STEP and IGES; these need a commercial translator we do
#: not have. So a native file is not met with a bare "unsupported": the refusal names the tool and
#: says where its STEP export is, which is the way on for the user.
NATIVE_CAD_FORMATS: tuple[NativeCadFormat, ...] = (
    NativeCadFormat("SolidWorks", (".sldprt", ".sldasm"),
                    "In SolidWorks, use File > Save As > STEP AP214 (*.step)"),
    # NX saves parts AND assemblies as .prt; Creo and Solid Edge save assemblies as .asm
    NativeCadFormat("Creo or NX", (".prt",),
                    "Export a STEP from Creo or NX (Creo: File > Save As > Save a Copy, type "
                    "STEP; NX: File > Export > STEP)"),
    NativeCadFormat("Creo or Solid Edge", (".asm",),
                    "Export a STEP from Creo or Solid Edge (Creo: File > Save As > Save a Copy, "
                    "type STEP; Solid Edge: File > Save As, type STEP)"),
    NativeCadFormat("CATIA", (".catpart", ".catproduct"),
                    "In CATIA, use File > Save As, type stp"),
    NativeCadFormat("Inventor", (".ipt", ".iam"),
                    "In Inventor, use File > Export > CAD Format, type STEP (*.stp)"),
    NativeCadFormat("Parasolid", (".x_t", ".x_b"),
                    "Open it in a CAD tool that reads Parasolid (SolidWorks, NX, Solid Edge or "
                    "Onshape) and export a STEP"),
    NativeCadFormat("Rhino", (".3dm",),
                    "In Rhino, select the part and use File > Export Selected, type STEP (*.stp)"),
    NativeCadFormat("Fusion 360", (".f3d",),
                    "In Fusion 360, use File > Export, type STEP (*.stp)"),
    NativeCadFormat("Solid Edge", (".par", ".psm"),
                    "In Solid Edge, use File > Save As, type STEP (*.stp)"),
    NativeCadFormat("JT", (".jt",),
                    "Export a STEP from the CAD tool the JT came from"),
    NativeCadFormat("ACIS", (".sat", ".sab"),
                    "Open it in a CAD tool that reads ACIS (SpaceClaim, Inventor, Fusion 360 or "
                    "Onshape) and export a STEP"),
)

#: Every recognised native suffix. The picker offers these too, so picking one explains itself.
NATIVE_CAD_SUFFIXES: frozenset[str] = frozenset(
    s for n in NATIVE_CAD_FORMATS for s in n.suffixes)

# Creo numbers every save: bracket.prt.3 is the third save of bracket.prt.
_CREO_SAVE_NUMBER = re.compile(r"(\.(?:prt|asm))\.\d+$")

_WHY_STEP = "it keeps the exact surfaces and the units (STL also works, but loses the units)"


def native_format_for_suffix(suffix: str) -> NativeCadFormat | None:
    s = (suffix or "").lower()
    return next((n for n in NATIVE_CAD_FORMATS if s in n.suffixes), None)


def refusal_suffix(filename: str) -> str:
    """The suffix a refusal should talk about: the file's own, or .prt/.asm for a numbered Creo save."""
    name = (filename or "").lower()
    creo = _CREO_SAVE_NUMBER.search(name)
    return creo.group(1) if creo else PurePosixPath(name).suffix


def unsupported_message(suffix: str) -> str:
    """What a user reads when a file is refused, with the way on: which export to upload instead."""
    s = (suffix or "").lower()
    native = native_format_for_suffix(s)
    if native:
        return (f"Hexera can't open {native.tool} files ({s}). {native.export}, then upload the "
                f"STEP file: {_WHY_STEP}.")
    names = ", ".join(sorted(ACCEPTED_SUFFIXES))
    what = f"'{s}' files" if s else "a file without an extension"
    return (f"Hexera can't open {what}. Accepted geometry formats: {names}. From a CAD tool, "
            f"export a STEP file and upload that: {_WHY_STEP}.")


def capability_payload() -> dict:
    return {
        "formats": [
            {"key": f.key, "label": f.label, "suffixes": list(f.suffixes),
             "media_type": f.media_type, "declares_units": f.declares_units}
            for f in INTAKE_FORMATS
        ],
        "accept": ",".join(sorted(ACCEPTED_SUFFIXES)),
        # What the picker OFFERS: the accepted formats plus the native CAD it recognises, so a
        # SolidWorks part can be picked and the browser can say how to export it, word for word
        # what the server would say - rather than the picker hiding the file without a reason.
        "picker_accept": ",".join(sorted(ACCEPTED_SUFFIXES | NATIVE_CAD_SUFFIXES)),
        "native_cad": {s: unsupported_message(s) for s in sorted(NATIVE_CAD_SUFFIXES)},
    }
