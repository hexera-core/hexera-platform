# Responsibility: Bring geometry to metres, whatever form it arrived in.
# Boundaries: scaling only, applied once at a declared boundary.
from __future__ import annotations

from pathlib import Path

from meshpipeline.contracts.coordinate_state import PreparedCoordinates


class AlreadyMetres(RuntimeError):
    pass


def _factor(prepared: PreparedCoordinates) -> float:
    from meshpipeline.contracts.coordinate_state import OCC_OUTPUT_UNIT

    if prepared.already_normalised and prepared.current_unit is not OCC_OUTPUT_UNIT:
        raise AlreadyMetres(
            f"post-transfer coordinates are {OCC_OUTPUT_UNIT.value}, not "
            f"{prepared.current_unit.value} - this state has already been converted")
    return prepared.to_metres


def scale_stl_file(src: Path, dest: Path, prepared: PreparedCoordinates) -> Path:
    factor = prepared.to_metres
    if factor == 1.0:
        import shutil
        shutil.copy2(src, dest)          # already metres: keep the user's exact bytes
        return Path(dest)

    from meshpipeline.cad.stl_io import (
        StlReadError,
        is_ascii_stl,
        read_binary_corners,
        read_stl_solids,
        write_stl_binary,
        write_stl_solids,
    )

    # Binary or ASCII is read from the bytes, never from the header's first word: a binary STL
    # whose header says "solid ..." is common, and so is an ASCII file opening on a bare "solid".
    if not is_ascii_stl(Path(src)):
        # BINARY: no names to carry. Every record is scaled and written back as binary.
        corners = read_binary_corners(Path(src)) * factor
        if not len(corners):
            raise StlReadError(f"{Path(src).name} holds no triangles to scale")
        write_stl_binary(Path(dest), corners)
        return Path(dest)

    # ASCII: solid names are PRODUCT policy - they become patch identities downstream - so the
    # named writer keeps them. VTK would round-trip the geometry perfectly and drop the names.
    solids = {
        name: [tuple(tuple(c * factor for c in v) for v in tri) for tri in tris]
        for name, tris in read_stl_solids(Path(src)).items()
    }
    if not any(solids.values()):
        # never hand the mesher an empty surface: it would fail later with no word of why
        raise StlReadError(f"{Path(src).name} holds no triangles to scale")
    write_stl_solids(Path(dest), solids)
    return Path(dest)


def occ_scale_transform(prepared: PreparedCoordinates):
    from OCP.gp import gp_Trsf

    trsf = gp_Trsf()
    trsf.SetScaleFactor(_factor(prepared))
    return trsf


def scale_polydata_file(src: Path, dest: Path, prepared: PreparedCoordinates) -> Path:
    import pyvista as pv

    factor = prepared.to_metres
    mesh = pv.read(str(src))
    if factor != 1.0:
        mesh.points = mesh.points * factor
    mesh.save(str(dest))
    return Path(dest)
