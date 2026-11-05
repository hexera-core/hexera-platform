# Responsibility: Carry a surface that is known to be in metres, and the record of how it got there.
# Boundaries: the type that makes 'already prepared' checkable instead of assumed.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from meshpipeline.contracts.coordinate_state import (
    CoordinateOrigin,
    PreparedCoordinates,
)


class SurfaceRepresentation(str, Enum):

    stl = "stl"


class SurfaceAlreadyPrepared(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedSurface:

    #: where the metre-normalised file is, right now, in this workspace
    path: Path
    #: WHICH bytes it derives from, and WHAT SIZE they were said to be - as identity, not objects,
    #: so this cannot drift into a second opinion about either
    source_id: str
    interpretation_id: str
    #: the coordinate state that was consumed to produce it - the proof of which boundary the one
    #: conversion was applied at, and therefore that it was applied once
    consumed: PreparedCoordinates
    representation: SurfaceRepresentation = SurfaceRepresentation.stl

    def __post_init__(self) -> None:
        if not str(self.path or "").strip():
            raise ValueError("a prepared surface must name a real file")
        for field in ("source_id", "interpretation_id"):
            if not str(getattr(self, field) or "").strip():
                raise ValueError(f"a prepared surface must record its {field}")

    @property
    def unit(self) -> str:
        return "m"

    @property
    def origin(self) -> CoordinateOrigin:
        return self.consumed.origin

    @property
    def scale_applied(self) -> float:
        return self.consumed.to_metres

    def require_metres(self) -> PreparedSurface:
        return self

    def refuse_second_normalisation(self) -> None:
        raise SurfaceAlreadyPrepared(
            f"{self.path.name} is already metres (converted from "
            f"{self.consumed.current_unit.value} at the {self.origin.value} boundary); "
            "normalising it again would scale it twice")


def require_metre_surface(surface, workspace, geometry_file: str):
    from meshpipeline.cad.prepared_surface import PreparedSurface

    if isinstance(surface, PreparedSurface):
        return surface
    raise ValueError(
        f"this engine needs the metre-normalised PreparedSurface for {geometry_file!r} in "
        f"{workspace}; it was given {type(surface).__name__}. Physical sizing cannot proceed on "
        "coordinates whose unit nobody has stated.")
