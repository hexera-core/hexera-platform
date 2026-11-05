# Responsibility: Turn verified execution geometry into the metre-normalised surface an engine stages.
# Boundaries: it stages what was approved, under the engine's declared filename; it never reinterprets the unit.
from __future__ import annotations

import shutil
from pathlib import Path

from meshpipeline.cad.normalise import AlreadyMetres
from meshpipeline.cad.prepared_surface import (
    PreparedSurface,
    SurfaceRepresentation,
)
from meshpipeline.contracts.coordinate_state import (
    CoordinateStateError,
    PreparedCoordinates,
    from_occ_transfer,
    from_source_file,
)
from meshpipeline.contracts.intake_formats import GeometryKind, geometry_kind


class UnsupportedSurfaceSource(RuntimeError):
    pass


def prepare_surface(geometry, destination, *, engine: str = "") -> PreparedSurface:
    if geometry is None:
        raise ValueError(
            "surface preparation needs verified execution geometry; a run carrying none has "
            "nothing to prepare and must stop before here")

    src = Path(geometry.path)
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    # The materialiser hands over canonical geometry (cad/ingest): a CAD solid, an STL, or a
    # surface an engine reads natively (VTP). Which one decides who tessellates.
    kind = geometry_kind(src)

    # A coordinate-state violation is OUR defect, not the user's, and it must leave this boundary
    # classified. An unclassified RuntimeError from here reaches the run entry as an unexplained
    # internal failure, which is how the metre-override refusal read to everyone who hit it.
    try:
        consumed = _consumed_state(geometry, suffix)

        if kind is GeometryKind.surface and suffix == ".stl":
            _prepare_stl(src, dest, consumed)
        elif kind is GeometryKind.cad:
            _prepare_cad(src, dest, consumed, engine=engine)
        elif suffix in _native_surface_suffixes(engine):
            # an engine's own surface format (vmtk's .vtp): that bundle's tessellator consumes
            # the interpretation and emits metres - the shared analysis surface and the native
            # lumen come out of one call, in one unit. Nothing further is applied here.
            _prepare_via_engine(src, dest, consumed, engine=engine)
        else:
            # any other surface is turned into the canonical STL first, then scaled like one
            _prepare_converted_surface(src, dest, consumed)
    except (CoordinateStateError, AlreadyMetres) as exc:
        from meshpipeline.contracts.geometry_source import GeometrySourceError
        from meshpipeline.errors import FailureClass
        raise GeometrySourceError(
            f"this geometry's coordinate state is not usable: {exc}",
            failure_class=FailureClass.INTERNAL) from exc

    return PreparedSurface(
        path=dest,
        source_id=geometry.ref.source_id,
        interpretation_id=geometry.interpretation.interpretation_id,
        consumed=consumed,
        representation=SurfaceRepresentation.stl,
    )


def _native_surface_suffixes(engine: str) -> tuple[str, ...]:
    """The surface formats an engine declares it reads itself (`native_surface_suffixes`)."""
    if not engine:
        return ()
    from meshpipeline.engines.runtime import get_engine

    try:
        return tuple(getattr(get_engine(engine), "native_surface_suffixes", ()) or ())
    except Exception:  # noqa: BLE001 - an engine that declares nothing reads nothing natively
        return ()


def _prepare_converted_surface(src: Path, dest: Path, consumed: PreparedCoordinates) -> None:
    from meshpipeline.cad.ingest import IngestError
    from meshpipeline.cad.ingest.canonical import surface_to_stl

    raw = dest.with_name(dest.stem + ".unscaled.stl")
    try:
        surface_to_stl(src, raw)
    except IngestError as exc:
        raise UnsupportedSurfaceSource(f"{src.suffix or 'this surface'} could not be read: {exc}") \
            from exc
    try:
        _prepare_stl(raw, dest, consumed)
    finally:
        if raw.resolve() != dest.resolve():
            raw.unlink(missing_ok=True)


def _consumed_state(geometry, suffix: str) -> PreparedCoordinates:
    interpretation = _domain_interpretation(geometry)
    if geometry_kind(suffix) is GeometryKind.cad:
        from meshpipeline.cad.unit_evidence import parser_applied_unit

        return from_occ_transfer(interpretation, parser_applied_unit(geometry.path))
    return from_source_file(interpretation)


def _domain_interpretation(geometry):
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )

    snapshot = geometry.interpretation
    return GeometryInterpretation(
        interpretation_id=snapshot.interpretation_id,
        owner_id=geometry.ref.owner_id,
        geometry_source_id=snapshot.geometry_source_id,
        unit=LengthUnit(snapshot.unit),
        scale_to_metres=float(snapshot.scale_to_metres),
        basis=ResolutionBasis(snapshot.basis),
        evidence=snapshot.evidence,
    )


def _prepare_stl(src: Path, dest: Path, consumed: PreparedCoordinates) -> None:
    from meshpipeline.cad.normalise import scale_stl_file

    if consumed.to_metres == 1.0:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return
    scale_stl_file(src, dest, consumed)


def _prepare_cad(src: Path, dest: Path, consumed: PreparedCoordinates, *, engine: str) -> None:
    from meshpipeline.engines.runtime import get_engine

    get_engine(engine).tessellate_to_stl(str(src), dest, prepared=consumed)


def _prepare_via_engine(src: Path, dest: Path, consumed: PreparedCoordinates, *,
                        engine: str) -> None:
    from meshpipeline.engines.runtime import get_engine

    if not engine:
        raise UnsupportedSurfaceSource(
            f"{src.suffix or 'this format'} needs an engine tessellator, and none was named")
    get_engine(engine).tessellate_to_stl(str(src), dest, prepared=consumed)


def staged_surface(geometry, destination) -> PreparedSurface:
    if geometry is None:
        raise ValueError("cannot describe a prepared surface without the execution geometry")
    dest = Path(destination)
    return PreparedSurface(
        path=dest,
        source_id=geometry.ref.source_id,
        interpretation_id=geometry.interpretation.interpretation_id,
        consumed=_consumed_state(geometry, Path(geometry.path).suffix.lower()),
        representation=SurfaceRepresentation.stl,
    )
