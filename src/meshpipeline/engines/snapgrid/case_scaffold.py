# Responsibility: Lay out the OpenFOAM case directory a snap-grid run writes into.
# Boundaries: structure only; the mesher writes the meshes, regionProperties and every region's system files.
from __future__ import annotations

from pathlib import Path


def write_case_skeleton(workspace) -> None:
    from meshpipeline.engines.snapgrid.mesher import write_case_skeleton as _skeleton

    _skeleton(Path(workspace))
