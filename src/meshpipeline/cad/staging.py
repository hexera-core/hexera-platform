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

#: Already-triangulated surfaces: raw file coordinates in the source's own unit.
_SURFACE_SUFFIXES = {".stl"}
#: B-rep formats: read through OpenCASCADE, which normalises to ITS system unit on transfer.
_CAD_SUFFIXES = {".step", ".stp", ".iges", ".igs"}


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

    # A coordinate-state violation is OUR defect, not the user's, and it must leave this boundary
    # classified. An unclassified RuntimeError from here reaches the run entry as an unexplained
    # internal failure, which is how the metre-override refusal read to everyone who hit it.
    try:
        consumed = _consumed_state(geometry, suffix)

        if suffix in _SURFACE_SUFFIXES:
            _prepare_stl(src, dest, consumed)
        elif suffix in _CAD_SUFFIXES:
            _prepare_cad(src, dest, consumed, engine=engine)
        else:
            # vmtk's native .vtp: only its own bundle can read it, and that bundle's tessellator
            # consumes the interpretation and emits metres - the shared analysis surface and the
            # native lumen come out of one call, in one unit. Nothing further is applied here.
            _prepare_via_engine(src, dest, consumed, engine=engine)
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


def _consumed_state(geometry, suffix: str) -> PreparedCoordinates:
    interpretation = _domain_interpretation(geometry)
    if suffix in _CAD_SUFFIXES:
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
