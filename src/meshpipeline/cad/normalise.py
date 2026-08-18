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

    if Path(src).open("rb").read(5) != b"solid":
        # BINARY: no names to carry, so VTK reads, transforms and writes it end to end.
        import pyvista as pv

        mesh = pv.read(str(src))
        mesh.points = mesh.points * factor
        mesh.save(str(dest))
        return Path(dest)

    # ASCII: solid names are PRODUCT policy - they become patch identities downstream - so the
    # named writer keeps them. VTK would round-trip the geometry perfectly and drop the names.
    from meshpipeline.cad.stl_io import read_stl_solids, write_stl_solids

    write_stl_solids(Path(dest), {
        name: [tuple(tuple(c * factor for c in v) for v in tri) for tri in tris]
        for name, tris in read_stl_solids(Path(src)).items()
    })
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
