# Responsibility: Read an IGES file the one way the product reads it: an OpenCascade IGES reader with the
# import settings that keep a spline face's boundary in one piece, so every later step works on the
# file's own edges instead of ten times as many.
# Boundaries: builds and configures the reader, and reads a file to one shape; it names no unit and
# judges nothing. cad/repair/* keeps its own reader (another owner's).
from __future__ import annotations

from pathlib import Path

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


__all__ = ["BSPLINE_CONTINUITY", "IgesReadError", "continuity_in_force", "iges_reader", "is_iges", "read_iges"]
