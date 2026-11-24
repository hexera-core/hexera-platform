# Responsibility: Read an IGES file the one way the product reads it: an OpenCascade IGES reader with the
# import settings that keep a spline face's boundary in one piece, so every later step works on the
# file's own edges instead of ten times as many.
# Boundaries: builds and configures the reader, and reads a file to one shape; it names no unit and
# judges nothing. cad/repair/* keeps its own reader (another owner's).
from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

#: OpenCascade's IGES import splits every B-spline curve and surface at each place it is less
#: smooth than C1 (`read.iges.bspline.continuity` 1, its default). A pre-processor's export of
#: 231 trimmed spline faces came in with 8,415 edges that way, and stitching them took 190 s;
#: read with 0 (keep each B-spline whole) it had about 800 edges and stitched in under 2 s -
#: the same 231 faces and the same area. 0 keeps the file's own curves exactly as written.
BSPLINE_CONTINUITY = 0
_CONTINUITY_KEY = "read.iges.bspline.continuity"


def is_iges(path) -> bool:
    return Path(path).suffix.lower() in (".iges", ".igs")


def iges_reader():
    """An IGESControl_Reader set to keep B-splines whole. The setting is made AFTER the reader is
    built: building the first one initialises OpenCascade's IGES parameters, and a value set before
    that is silently overwritten. Raises RuntimeError when OpenCascade does not take it."""
    from OCP.IGESControl import IGESControl_Reader
    from OCP.Interface import Interface_Static

    reader = IGESControl_Reader()
    if not Interface_Static.SetIVal_s(_CONTINUITY_KEY, BSPLINE_CONTINUITY):
        raise RuntimeError(f"OpenCascade refused {_CONTINUITY_KEY}={BSPLINE_CONTINUITY}")
    return reader


def continuity_in_force() -> int:
    """The B-spline continuity OpenCascade's IGES import uses now (for tests and logs)."""
    from OCP.Interface import Interface_Static

    return int(Interface_Static.IVal_s(_CONTINUITY_KEY))


class IgesReadError(RuntimeError):
    """The IGES file could not be read to a shape."""


def read_iges(path):
    """The shape in an IGES file, as read (unscaled, unmeshed). Raises IgesReadError when the file
    cannot be read; the shape may still be empty when the file holds nothing transferable."""
    from OCP.IFSelect import IFSelect_RetDone

    reader = iges_reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise IgesReadError(f"OpenCASCADE could not read the IGES file {Path(path).name}")
    reader.TransferRoots()
    return reader.OneShape()


@contextlib.contextmanager
def gmsh_import_path(path) -> Iterator[Path]:
    """The file gmsh should import for an upload. gmsh carries its own OpenCascade, whose IGES import splits every
    B-spline whatever this process sets (a 1,448-edge part through gmsh is 468 edges here), so an IGES is read
    here and handed to gmsh as a BREP of the same shape - in the same millimetres gmsh's own IGES import gives, so
    its Geometry.OCCScaling stages it at the same size. Any other file, or an IGES this cannot read or write, is
    yielded as it is (logged): staged exactly as before, never a new failure. The BREP lives only while gmsh
    imports it."""
    path = Path(path)
    if not is_iges(path):
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="iges_for_gmsh_") as tmp:
        brep = Path(tmp) / (path.stem + ".brep")
        try:
            from OCP.BRepTools import BRepTools

            shape = read_iges(path)
            if shape is None or shape.IsNull():
                raise IgesReadError("the IGES file holds no shape")
            if not BRepTools.Write_s(shape, str(brep)) or not brep.exists():
                raise IgesReadError("the BREP could not be written")
        except Exception as exc:  # noqa: BLE001 - gmsh imports the IGES itself, as before
            logger.warning("iges read: %s handed to gmsh as it is (%s: %s)", path.name, type(exc).__name__,
                           str(exc)[:200])
            yield path
            return
        yield brep


__all__ = ["BSPLINE_CONTINUITY", "IgesReadError", "continuity_in_force", "gmsh_import_path", "iges_reader",
           "is_iges", "read_iges"]
