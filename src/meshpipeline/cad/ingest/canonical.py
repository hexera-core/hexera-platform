# Responsibility: Turn any accepted geometry file into one of the two canonical forms: a CAD solid (STEP/IGES) or a triangle surface (STL).
# Owns: the format decision by content, the pass-through of formats already canonical, the conversions, and the record of what was converted.
# Boundaries: one file in, one canonical file out, in the source's own unit; it never scales, repairs or meshes.
# Collaborates with: application/geometry_materializer.py and application/geometry_check.py (its two callers), cad/ingest/readers.py, cad/ingest/cad.py.
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.contracts.intake_formats import (
    CANONICAL_SUFFIX,
    NATIVE_UNKNOWN_TOOL,
    GeometryKind,
    format_for_key,
    format_for_suffix,
    native_format_for_suffix,
    unsupported_message,
)

#: Written beside a converted file: what it was converted from, and what the source carried.
SIDECAR_SUFFIX = ".ingest.json"


class IngestError(ValueError):
    """A file that cannot become canonical geometry. The message is a plain sentence for the user."""


@dataclass(frozen=True)
class CanonicalGeometry:
    path: Path                      # the file every consumer reads from here on
    kind: GeometryKind              # what it is: an exact CAD solid, or triangles
    source_format: str              # the format the bytes were read as (contracts key)
    converted: bool                 # False: the uploaded bytes themselves (an already-canonical format)
    regions: tuple[str, ...] = ()   # named groups/bodies the source carried, as written
    notes: tuple[str, ...] = ()
    stats: dict = field(default_factory=dict)

    def facts(self) -> dict:
        """The plain facts other workstreams read: what kind of geometry this is, and from what."""
        fmt = format_for_key(self.source_format)
        return {"geometry_kind": self.kind.value, "source_format": self.source_format,
                "source_format_label": fmt.label if fmt else self.source_format,
                "converted": self.converted, "source_regions": list(self.regions)}


def _refuse_native(sniffed: str) -> IngestError:
    from meshpipeline.cad.ingest.sniff import native_suffix

    suffix = native_suffix(sniffed) or ""
    if native_format_for_suffix(suffix) is not None:
        return IngestError(unsupported_message(suffix))
    return IngestError(NATIVE_UNKNOWN_TOOL)


def resolve_format(src: Path) -> str:
    """The format key the file IS: its content when that names it, else the suffix's format."""
    from meshpipeline.cad.ingest.sniff import native_suffix, sniff_format

    sniffed = sniff_format(src)
    if native_suffix(sniffed) is not None:
        raise _refuse_native(sniffed or "")
    if sniffed and format_for_key(sniffed) is not None:
        return sniffed
    declared = format_for_suffix(src.suffix)
    if declared is None:
        raise IngestError(unsupported_message(src.suffix.lower()))
    return declared.key


def canonicalise(src, workdir=None, *, stem: str | None = None) -> CanonicalGeometry:
    """The canonical form of `src`, written into `workdir` (default: beside it).

    STEP, IGES, VTP and a well-formed STL are returned AS THEY ARE - the bytes the user uploaded,
    so every engine that reads them today reads exactly what it read before. Everything else is
    converted: an exact CAD format to STEP, a mesh format to STL, its named groups kept as named
    STL solids. A file that turns out to be another format than its name says is read as what
    it is."""
    src = Path(src)
    workdir = Path(workdir) if workdir is not None else src.parent
    workdir.mkdir(parents=True, exist_ok=True)
    stem = stem or src.stem
    key = resolve_format(src)
    fmt = format_for_key(key)
    if fmt is None:                       # resolve_format only answers registered keys
        raise IngestError(unsupported_message(src.suffix.lower()))

    if key == "stl":
        return _canonical_stl(src, workdir, stem)
    if key == "step":
        faceted = _faceted_census(src)
        if faceted is not None:
            return _faceted_step(src, workdir / f"{stem}.stl", faceted)
    if fmt.canonical:
        path = src
        if format_for_suffix(src.suffix) is not fmt:
            # named as something else: the readers downstream dispatch on the suffix, so the
            # bytes get the name of what they are
            path = workdir / f"{stem}{fmt.suffixes[0]}"
            shutil.copy2(src, path)
        return CanonicalGeometry(path=path, kind=fmt.kind, source_format=key,
                                 converted=path != src)

    dest = workdir / f"{stem}{CANONICAL_SUFFIX[fmt.kind]}"
    if dest.resolve() == src.resolve():
        raise IngestError("internal: the canonical file would overwrite its own source")
    if fmt.kind is GeometryKind.cad:
        result = _cad_to_step(src, key, dest)
    else:
        result = _surface_to_stl(src, key, dest)
    _write_sidecar(result, declared_unit=_source_unit(src, fmt))
    return result


def _faceted_census(src: Path):
    from meshpipeline.cad.ingest.step_facets import is_faceted_step

    try:
        return is_faceted_step(src)
    except (OSError, ValueError):
        return None                  # unreadable here: OpenCASCADE has the last word, as before


def _faceted_step(src: Path, dest: Path, census) -> CanonicalGeometry:
    """A STEP that is a triangle mesh (a scan or an STL a converter wrapped as STEP) goes down
    the SURFACE road: its flat triangles become the canonical STL, exactly, in the file's own
    numbers. On the CAD road every facet would be a face - an opening candidate, a region to
    separate - and OpenCASCADE takes minutes over a hundred thousand of them."""
    from meshpipeline.cad.ingest import limits
    from meshpipeline.cad.ingest.step_facets import (
        FacetedReadError,
        read_faceted,
        read_faceted_occ,
    )
    from meshpipeline.cad.ingest.surface import (
        SurfaceError,
        SurfaceMesh,
        clean,
        stats,
        write_canonical_stl,
    )

    try:
        try:
            pts, tris = read_faceted(src)
        except FacetedReadError:
            pts, tris = read_faceted_occ(src)
        mesh = clean(SurfaceMesh(pts, tris))
    except (FacetedReadError, SurfaceError, limits.ReadLimitExceeded) as exc:
        raise IngestError(f"the faceted STEP file could not be read ({exc})") from exc
    if mesh.n_triangles > limits.MAX_TRIANGLES:
        raise IngestError(limits.too_many_triangles())
    write_canonical_stl(mesh, dest)
    result = CanonicalGeometry(
        path=dest, kind=GeometryKind.surface, source_format="step", converted=True,
        notes=(f"the STEP file is a faceted mesh ({census.describe()}), so it is read as the "
               "triangle surface it is",) + tuple(mesh.notes),
        stats=stats(mesh))
    _write_sidecar(result, declared_unit={"resolved": False, "detail": _faceted_unit_detail(src)})
    return result


def _faceted_unit_detail(src: Path) -> str:
    """Why a faceted STEP's unit is asked: the label is the converter's, not the designer's - a
    mesh has no unit to carry, so whatever the converter wrote is a default (a metre header over
    millimetre numbers makes a 300 mm aorta 300 m long)."""
    try:
        from meshpipeline.cad.unit_evidence import _step_evidence

        said = _step_evidence(src)
        label = said.unit.value if said.resolved and said.unit is not None else ""
    except Exception:  # noqa: BLE001 - the detail is a courtesy
        label = ""
    states = f" (it states {label})" if label else ""
    return (f"the STEP file is a faceted mesh: its unit label{states} was written by the "
            "program that converted it, so it is not taken as the part's unit")


def surface_to_stl(src, dest) -> CanonicalGeometry:
    """Any surface file (VTP included, which canonicalise passes through for the engine that reads
    it natively) written as the canonical STL at `dest`, for an engine that reads only STL."""
    src, dest = Path(src), Path(dest)
    key = resolve_format(src)
    fmt = format_for_key(key)
    if fmt is None or fmt.kind is not GeometryKind.surface:
        raise IngestError(f"{src.suffix or 'this file'} is not a surface")
    return _surface_to_stl(src, key, dest)


def _source_unit(src: Path, fmt) -> dict:
    """What the SOURCE states about its unit, kept beside the converted file - which carries none
    (STL) or only the converter's label (STEP from a BREP)."""
    if not fmt.declares_units:
        return {"resolved": False, "detail": f"the uploaded {fmt.label} does not record a unit"}
    from meshpipeline.cad.ingest.units import declared_unit

    try:
        evidence = declared_unit(src, fmt.key)
    except Exception:  # noqa: BLE001 - a unit that cannot be read is asked, never assumed
        evidence = None
    if evidence is None:
        return {"resolved": False, "detail": f"the {fmt.label}'s unit could not be read"}
    return {"resolved": bool(evidence.resolved),
            "unit": evidence.unit.value if evidence.unit is not None else None,
            "detail": evidence.detail}


def _cad_to_step(src: Path, key: str, dest: Path) -> CanonicalGeometry:
    from meshpipeline.cad.ingest.cad import CadReadError, brep_to_step

    if key != "brep":
        raise IngestError(f"internal: no CAD converter for {key!r}")
    try:
        stats = brep_to_step(src, dest)
    except CadReadError as exc:
        raise IngestError(str(exc)) from exc
    notes = []
    if stats.get("solids", 0) == 0:
        notes.append("the BREP file holds surfaces but no closed solid")
    return CanonicalGeometry(path=dest, kind=GeometryKind.cad, source_format=key, converted=True,
                             notes=tuple(notes), stats=stats)


def _surface_to_stl(src: Path, key: str, dest: Path) -> CanonicalGeometry:
    from meshpipeline.cad.ingest import limits
    from meshpipeline.cad.ingest.readers import read_surface
    from meshpipeline.cad.ingest.surface import SurfaceError, clean, stats, write_canonical_stl

    try:
        mesh = clean(read_surface(src, key))
    except (SurfaceError, limits.ReadLimitExceeded) as exc:
        raise IngestError(str(exc)) from exc
    if mesh.n_triangles > limits.MAX_TRIANGLES:
        raise IngestError(limits.too_many_triangles())
    regions = write_canonical_stl(mesh, dest)
    return CanonicalGeometry(path=dest, kind=GeometryKind.surface, source_format=key,
                             converted=True, regions=regions, notes=tuple(mesh.notes),
                             stats=stats(mesh))


def _canonical_stl(src: Path, workdir: Path, stem: str) -> CanonicalGeometry:
    """An STL every reader here can read is passed through untouched. Two kinds are not, and are
    rewritten: a BINARY file whose header starts with "solid" (SolidWorks writes these; the ASCII
    readers downstream take it for text and fail), and an ASCII file the line readers cannot
    follow (leading blanks, upper-case keywords)."""
    named_stl = src.suffix.lower() == ".stl"
    if _stl_reads_as_is(src):
        tidied = _tidied_stl(src, workdir / f"{stem}.canonical.stl" if named_stl
                             else workdir / f"{stem}.stl")
        if tidied is not None:
            _write_sidecar(tidied)
            return tidied
        path = src
        if not named_stl:
            path = workdir / f"{stem}.stl"       # an STL named as something else
            shutil.copy2(src, path)
        return CanonicalGeometry(path=path, kind=GeometryKind.surface, source_format="stl",
                                 converted=not named_stl, regions=_ascii_solid_names(path))
    dest = workdir / f"{stem}.canonical.stl" if named_stl else workdir / f"{stem}.stl"
    try:
        result = _surface_to_stl(src, "stl", dest)
    except IngestError:
        if not named_stl:
            raise
        # An .stl this reader cannot follow either goes on exactly as it always has: the
        # engines' own readers have the last word on it, and nothing is refused here that was
        # accepted before.
        return CanonicalGeometry(path=src, kind=GeometryKind.surface, source_format="stl",
                                 converted=False,
                                 notes=("the STL could not be checked before meshing",))
    result = CanonicalGeometry(path=result.path, kind=result.kind, source_format="stl",
                               converted=True, regions=result.regions,
                               notes=result.notes + ("the STL was rewritten so every reader "
                                                     "reads it the same way",),
                               stats=result.stats)
    _write_sidecar(result)
    return result


#: Above these an uploaded STL is passed on unchecked, as it always was: the safe cleanup reads
#: every triangle, and a file this large is better left to the engines than held in memory twice.
_TIDY_MAX_BINARY_TRIANGLES = 5_000_000
_TIDY_MAX_ASCII_BYTES = 300 * 1024 * 1024


def _tidied_stl(src: Path, dest: Path) -> CanonicalGeometry | None:
    """The uploaded STL after the safe cleanup (surface.tidy) - or None when the cleanup changes
    nothing, so a clean file stays the very bytes the user uploaded. An ASCII file keeps its
    solids under their own names; a binary one stays binary."""
    import struct

    from meshpipeline.cad.ingest.surface import (
        SurfaceError,
        read_stl,
        safe_solid_name,
        stats,
        tidy,
        write_ascii_solids,
        write_binary_stl,
    )

    size = src.stat().st_size
    with src.open("rb") as fh:
        head = fh.read(84)
    binary = len(head) == 84 and 84 + 50 * struct.unpack("<I", head[80:84])[0] == size
    if binary and (size - 84) // 50 > _TIDY_MAX_BINARY_TRIANGLES:
        return None
    if not binary and size > _TIDY_MAX_ASCII_BYTES:
        return None
    try:
        mesh, report = tidy(read_stl(src))
    except (SurfaceError, ValueError, MemoryError):
        return None
    if not report.changed:
        return None
    corners = mesh.corners()
    if binary:
        write_binary_stl(dest, corners)
        regions: tuple[str, ...] = ()
    else:
        solids: dict = {}
        for gi in sorted(set(mesh.group.tolist())):
            raw = mesh.names[gi] if 0 <= gi < len(mesh.names) else ""
            name = safe_solid_name(raw) or "surface"
            prev = solids.get(name)
            part = corners[mesh.group == gi]
            solids[name] = part if prev is None else np.vstack([prev, part])
        write_ascii_solids(dest, solids)
        regions = tuple(n for n in solids) if len(solids) > 1 else ()
    return CanonicalGeometry(path=dest, kind=GeometryKind.surface, source_format="stl",
                             converted=True, regions=regions,
                             notes=tuple(report.notes()), stats=stats(mesh))


def _ascii_solid_names(path: Path) -> tuple[str, ...]:
    """The distinct, meaningful solid names of an ASCII STL - () for binary or a single solid."""
    from meshpipeline.cad.regions import _meaningful

    names: list[str] = []
    with path.open("rb") as fh:
        if fh.read(5) != b"solid":
            return ()
        fh.seek(0)
        for line in fh:
            if line.startswith(b"solid"):
                name = line[5:].decode("latin-1").strip()
                if _meaningful(name) and name not in names:
                    names.append(name)
    return tuple(names) if len(names) > 1 else ()


def _stl_reads_as_is(path: Path) -> bool:
    import struct

    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(4096)
    if size >= 84 and len(head) >= 84:
        n = struct.unpack("<I", head[80:84])[0]
        if n > 0 and 84 + 50 * n == size:
            return head[:5] != b"solid"
    # ASCII: the line readers want "solid" first and lower-case keywords
    return head[:5] == b"solid" and b"facet" in head and b"vertex" in head


def _write_sidecar(result: CanonicalGeometry, *, declared_unit: dict | None = None) -> None:
    record = asdict(result)
    record["path"] = result.path.name
    record["kind"] = result.kind.value
    record["declared_unit"] = declared_unit or {
        "resolved": False, "detail": "the uploaded STL does not record a unit"}
    Path(str(result.path) + SIDECAR_SUFFIX).write_text(json.dumps(record, indent=1, default=str))


def read_sidecar(path) -> dict | None:
    """What the canonical file at `path` was converted from, or None for an uploaded original."""
    side = Path(str(path) + SIDECAR_SUFFIX)
    if not side.is_file():
        return None
    try:
        return json.loads(side.read_text())
    except (OSError, ValueError):
        return None
