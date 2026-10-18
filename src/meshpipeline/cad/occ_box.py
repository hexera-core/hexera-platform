# Responsibility: Say how big an OpenCascade shape really is - the box of its own surface - and
# mesh it at a deflection that follows that size.
# Boundaries: measurement and meshing of one shape; it names no unit and decides nothing else.
from __future__ import annotations

import math

Box = tuple[float, float, float, float, float, float]

#: A loose envelope this much wider (on the diagonal) than the surface itself means the deflection
#: it set was off by more than the meshing's own slack: the part is meshed again at its real size.
REMESH_LOOSENESS = 1.5


def loose_box(shape) -> Box:
    """OpenCascade's fast envelope (BRepBndLib.Add): it encloses every B-spline control point and
    every tolerance, so it is never too small - and can be several times too big. A Toyota Supra
    STEP 56.9 x 132.2 x 37.0 mm across read 570 x 492 x 401 mm this way. Fit only where a
    guaranteed envelope is wanted, never as the part's size."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return tuple(float(v) for v in box.Get())  # type: ignore[return-value]


def surface_box(shape) -> Box | None:
    """The box of the shape's faces, read off the points its own triangulation puts on them, or
    None when it has no faces. Exact to within the mesh's deflection (the surface between two nodes
    may bulge past them by at most that), and never larger than the part. Only faces count: an
    edge or vertex outside every face (a construction or annotation line) is not part of the
    surface that gets meshed.

    A face the mesher left without triangles is not dropped: it adds its own exact box
    (AddOptimal on that face alone - slow per face, but such faces are rare; over the face's whole
    parameter range, so it can overshoot a trimmed face a little, never undershoot), so a failed
    face can never make the part read smaller than it is - and a shape no face of which meshed
    still reads its faces' exact box, not the loose envelope."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    box = Bnd_Box()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        exp.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None and tri.NbNodes() > 0:
            # the triangulation's own node range, in C++, placed by the face's location (accurate:
            # every node is moved, not the corners of the untransformed range)
            tri.MinMax(box, loc.Transformation(), True)
        else:
            BRepBndLib.AddOptimal_s(face, box, False, False)
    if box.IsVoid():
        return None
    return tuple(float(v) for v in box.Get())  # type: ignore[return-value]


def diagonal(box: Box) -> float:
    return math.sqrt((box[3] - box[0]) ** 2 + (box[4] - box[1]) ** 2 + (box[5] - box[2]) ** 2)


def mesh_to_size(shape, fraction: float, angular_deflection: float, *,
                 linear_deflection: float | None = None) -> tuple[Box, float]:
    """Mesh the shape at `fraction` of its real diagonal (or at an explicit linear deflection) and
    return (its real box - the box of the meshed surface, the linear deflection it was meshed at).

    The size is not known before a mesh exists, and OpenCascade's only free box is the loose
    envelope. So: mesh at the envelope's size (what was always done - a file whose envelope is
    honest meshes exactly as before), measure the surface, and only when the envelope was
    REMESH_LOOSENESS or more too big mesh again at the real size, then measure once more. The
    returned box is never the envelope unless nothing could be meshed at all."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    envelope = loose_box(shape)
    d_env = diagonal(envelope)
    lin = linear_deflection if linear_deflection is not None else max(d_env * fraction, 1e-12)
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
    box = surface_box(shape)
    if box is None:
        return envelope, lin
    d_real = diagonal(box)
    if linear_deflection is None and d_real > 0.0 and d_env > REMESH_LOOSENESS * d_real:
        lin = max(d_real * fraction, 1e-12)
        BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
        box = surface_box(shape) or box
    return box, lin


__all__ = ["REMESH_LOOSENESS", "diagonal", "loose_box", "mesh_to_size", "surface_box"]
