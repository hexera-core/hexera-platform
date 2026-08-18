# Responsibility: Carry the workspace, geometry and engine facts every builder tool needs.
# Boundaries: a passed-in context; a tool never reaches outside it for state.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meshpipeline.contracts.geometry_source import (
    GeometryInterpretationRef,
    GeometrySourceRef,
    MaterializedGeometry,
)


@dataclass(frozen=True, slots=True)
class BuilderToolContext:

    workspace: Path

    #: THE typed execution geometry for this process. None only where a run legitimately carries
    #: no geometry (a programmatic submit); every geometric tool must refuse that case rather than
    #: fall back to whatever happens to be on disk.
    geometry: MaterializedGeometry | None

    # identity the execution fence needs; not derivable from the geometry
    job_id: str = ""
    execution_id: str = ""
    execution_generation: int = 0

    # existing builder dependencies
    engine: str = ""
    mesh_fidelity: str = ""
    loop_deadline: float | None = None
    #: The engineer's flagged regions, when this build is answering a dispute. The
    #: submit tool needs them to know which declarations it must require.
    user_dispute: dict | None = None

    # derived, never stored
    # Properties rather than fields on purpose. A stored `interpretation` could be set to one
    # thing while `geometry.interpretation` said another, and the run would mesh at whichever the
    # next reader happened to consult.

    @property
    def source(self) -> GeometrySourceRef | None:
        return self.geometry.ref if self.geometry else None

    @property
    def interpretation(self) -> GeometryInterpretationRef | None:
        return self.geometry.interpretation if self.geometry else None

    @property
    def prepared(self):
        return self.geometry.prepared if self.geometry else None

    @property
    def geometry_path(self) -> str:
        return self.geometry.path if self.geometry else ""

    def require_geometry(self) -> MaterializedGeometry:
        if self.geometry is None:
            raise GeometryContextMissing(
                "this tool needs verified execution geometry, and none was provided to the "
                "builder tool context")
        return self.geometry


class GeometryContextMissing(RuntimeError):
    pass
