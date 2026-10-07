# Responsibility: Hold the one description of what geometry this product accepts.
# Owns: the accepted-format table, the suffix mapping, the staged filename, whether a format declares its own units, the CAD-or-surface answer every consumer asks, and the refusal a user reads for anything else.
# Boundaries: it answers what is accepted and what that implies; it opens no file and converts nothing (cad/ingest reads and converts).
# Collaborates with: api/v1/upload.py, cad/ingest/, agents/intake/ and every engine's staging seam.
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath


class GeometryKind(str, Enum):
    """The two canonical forms every accepted file becomes (cad/ingest does the converting).

    cad      an exact B-rep solid OpenCASCADE reads (STEP, IGES; BREP arrives converted to STEP)
    surface  triangles (STL; VTP stays VTP for the engine that reads it natively; every other
             mesh format arrives converted to STL, its named groups kept as named STL solids)
    """
    cad = "cad"
    surface = "surface"


@dataclass(frozen=True)
class IntakeFormat:
    key: str                       # canonical identity, stable across UI and API
    label: str                     # what a user is shown
    suffixes: tuple[str, ...]      # accepted filename suffixes, lowercase, dot-prefixed
    media_type: str                # best-known media type ("" when there is no registered one)
    declares_units: bool           # does the FORMAT itself state its physical unit?
    staged_as: str                 # the representation staged into the workspace
    kind: GeometryKind = GeometryKind.surface   # what it becomes: an exact solid or triangles
    #: True for the formats the rest of the system reads as they are; every other one is
    #: converted to its kind's canonical file before anything downstream sees it.
    canonical: bool = False


#: Ordered as the UI lists them: the most common first.
INTAKE_FORMATS: tuple[IntakeFormat, ...] = (
    IntakeFormat(
        key="stl", label="STL surface mesh", suffixes=(".stl",),
        media_type="model/stl",
        # STL is a bare triangle soup - no unit is recorded anywhere in the file.
        declares_units=False, staged_as="input.stl", canonical=True),
    IntakeFormat(
        key="step", label="STEP solid (AP203/AP214)", suffixes=(".step", ".stp"),
        media_type="model/step",
        # STEP carries a unit context, which OpenCASCADE normalises on read.
        declares_units=True, staged_as="input.step", kind=GeometryKind.cad, canonical=True),
    IntakeFormat(
        key="iges", label="IGES surface/solid", suffixes=(".iges", ".igs"),
        media_type="model/iges",
        declares_units=True, staged_as="input.iges", kind=GeometryKind.cad, canonical=True),
    IntakeFormat(
        key="vtp", label="VTK PolyData surface", suffixes=(".vtp",),
        media_type="application/vnd.vtk.polydata",
        # VTK PolyData stores raw coordinates with no unit declaration.
        declares_units=False, staged_as="input.vtp", canonical=True),
    IntakeFormat(
        key="brep", label="OpenCASCADE BREP solid", suffixes=(".brep", ".brp"),
        media_type="",
        # A BREP file is raw OpenCASCADE geometry: it records no unit at all.
        declares_units=False, staged_as="input.brep", kind=GeometryKind.cad),
    IntakeFormat(
        key="obj", label="Wavefront OBJ mesh", suffixes=(".obj",),
        media_type="model/obj", declares_units=False, staged_as="input.obj"),
    IntakeFormat(
        key="ply", label="PLY mesh", suffixes=(".ply",),
        media_type="", declares_units=False, staged_as="input.ply"),
    IntakeFormat(
        key="off", label="OFF mesh", suffixes=(".off",),
        media_type="", declares_units=False, staged_as="input.off"),
    IntakeFormat(
        key="3mf", label="3MF model", suffixes=(".3mf",),
        media_type="model/3mf",
        # Every 3MF model states its unit (the spec's default, when absent, is the millimetre).
        declares_units=True, staged_as="input.3mf"),
    IntakeFormat(
        key="glb", label="glTF binary (GLB)", suffixes=(".glb",),
        media_type="model/gltf-binary",
        # The glTF specification says metres, but nothing in a file records it, and mesh tools
        # routinely write a millimetre model's numbers unchanged (a 200 mm part would read as
        # 200 m). So the unit is asked, as for STL, rather than taken from the spec.
        declares_units=False, staged_as="input.glb"),
    IntakeFormat(
        key="gltf", label="glTF (single file, embedded data)", suffixes=(".gltf",),
        media_type="model/gltf+json", declares_units=False, staged_as="input.gltf"),
    IntakeFormat(
        key="vtk", label="VTK legacy mesh (surface or volume)", suffixes=(".vtk",),
        media_type="", declares_units=False, staged_as="input.vtk"),
    IntakeFormat(
        key="vtu", label="VTK unstructured grid (surface or volume)", suffixes=(".vtu",),
        media_type="", declares_units=False, staged_as="input.vtu"),
    IntakeFormat(
        key="msh", label="Gmsh or Fluent mesh", suffixes=(".msh",),
        media_type="", declares_units=False, staged_as="input.msh"),
    IntakeFormat(
        key="nastran", label="Nastran bulk data", suffixes=(".bdf", ".nas"),
        media_type="", declares_units=False, staged_as="input.bdf"),
    IntakeFormat(
        key="abaqus", label="Abaqus input", suffixes=(".inp",),
        media_type="", declares_units=False, staged_as="input.inp"),
    IntakeFormat(
        key="medit", label="Medit mesh", suffixes=(".mesh",),
        media_type="", declares_units=False, staged_as="input.mesh"),
    IntakeFormat(
        key="su2", label="SU2 mesh", suffixes=(".su2",),
        media_type="", declares_units=False, staged_as="input.su2"),
    IntakeFormat(
        key="3dm", label="Rhino 3DM (its meshes)", suffixes=(".3dm",),
        media_type="",
        # A Rhino model states its unit system in the file's settings.
        declares_units=True, staged_as="input.3dm"),
)

#: Every accepted suffix. The server's validation predicate, and the picker's advisory list.
ACCEPTED_SUFFIXES: frozenset[str] = frozenset(
    s for f in INTAKE_FORMATS for s in f.suffixes)

#: The file each kind is turned into, when the source is not already a canonical format.
CANONICAL_SUFFIX: dict[GeometryKind, str] = {GeometryKind.cad: ".step",
                                             GeometryKind.surface: ".stl"}


def format_for_suffix(suffix: str) -> IntakeFormat | None:
    s = (suffix or "").lower()
    return next((f for f in INTAKE_FORMATS if s in f.suffixes), None)


def format_for_key(key: str) -> IntakeFormat | None:
    return next((f for f in INTAKE_FORMATS if f.key == key), None)


def staged_name_for(suffix: str) -> str:
    fmt = format_for_suffix(suffix)
    return fmt.staged_as if fmt else "input.step"


def declares_units(suffix: str) -> bool:
    fmt = format_for_suffix(suffix)
    return bool(fmt and fmt.declares_units)


def _suffix_of(path_or_suffix) -> str:
    text = str(path_or_suffix or "").strip()
    # a bare suffix (".stl", "stl") is asked about as often as a path is
    if text and "/" not in text and "\\" not in text and text.count(".") <= 1 \
            and (text.startswith(".") or "." not in text):
        return "." + text.lstrip(".").lower()
    return PurePosixPath(text.replace("\\", "/")).suffix.lower()


def geometry_kind(path_or_suffix) -> GeometryKind | None:
    """THE one answer to "is this CAD or a surface?", for a path or a bare suffix.

    Every format-dependent decision downstream asks this instead of comparing suffixes, so a new
    format is declared once, above, and every engine treats it the same way. None for a file this
    product does not read at all."""
    fmt = format_for_suffix(_suffix_of(path_or_suffix))
    return fmt.kind if fmt else None


def is_cad(path_or_suffix) -> bool:
    return geometry_kind(path_or_suffix) is GeometryKind.cad


def is_surface(path_or_suffix) -> bool:
    return geometry_kind(path_or_suffix) is GeometryKind.surface


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
    NativeCadFormat("Fusion 360", (".f3d",),
                    "In Fusion 360, use File > Export, type STEP (*.stp)"),
    NativeCadFormat("Solid Edge", (".par", ".psm"),
                    "In Solid Edge, use File > Save As, type STEP (*.stp)"),
    NativeCadFormat("JT", (".jt",),
                    "Export a STEP from the CAD tool the JT came from"),
    NativeCadFormat("ACIS", (".sat", ".sab"),
                    "Open it in a CAD tool that reads ACIS (SpaceClaim, Inventor, Fusion 360 or "
                    "Onshape) and export a STEP"),
    NativeCadFormat("FreeCAD", (".fcstd",),
                    "In FreeCAD, select the body and use File > Export, type STEP with colors "
                    "(*.step *.stp)"),
    NativeCadFormat("SpaceClaim", (".scdoc",),
                    "In SpaceClaim, use File > Save As, type STEP (*.stp)"),
)

#: What a user reads when the bytes are a native CAD container whose tool cannot be told apart:
#: SolidWorks (before 2015), Inventor and Solid Edge all store parts in the same OLE container.
NATIVE_UNKNOWN_TOOL = ("Hexera can't open this file: it is a native CAD part (SolidWorks, Inventor "
                       "and Solid Edge save parts this way), whatever its name says. Export a STEP "
                       "from the tool that made it (File > Save As or File > Export, type STEP), "
                       "then upload the STEP file: it keeps the exact surfaces and the units (STL "
                       "also works, but loses the units).")

#: Every recognised native suffix. The picker offers these too, so picking one explains itself.
NATIVE_CAD_SUFFIXES: frozenset[str] = frozenset(
    s for n in NATIVE_CAD_FORMATS for s in n.suffixes)

#: Rhino's own export path. A .3dm IS read here, but only the triangles it stores (its meshes, and
#: the render meshes Rhino saves with each surface); a model saved without them has none to read.
RHINO_EXPORT = "In Rhino, select the part and use File > Export Selected, type STEP (*.stp)"

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
             "media_type": f.media_type, "declares_units": f.declares_units,
             "kind": f.kind.value}
            for f in INTAKE_FORMATS
        ],
        "accept": ",".join(sorted(ACCEPTED_SUFFIXES)),
        # What the picker OFFERS: the accepted formats plus the native CAD it recognises, so a
        # SolidWorks part can be picked and the browser can say how to export it, word for word
        # what the server would say - rather than the picker hiding the file without a reason.
        "picker_accept": ",".join(sorted(ACCEPTED_SUFFIXES | NATIVE_CAD_SUFFIXES)),
        "native_cad": {s: unsupported_message(s) for s in sorted(NATIVE_CAD_SUFFIXES)},
    }
