# Responsibility: Say how big an OpenCascade shape really is - the box of its own surface - and
# mesh it at a deflection that follows that size.
# Boundaries: measurement and meshing of one shape; it names no unit and decides nothing else.
from __future__ import annotations

import math

Box = tuple[float, float, float, float, float, float]

#: OpenCascade's mesher refuses a linear deflection under Precision::Confusion (1e-7 of the shape's
#: own unit) with "BRepMesh_IncrementalMesh::initParameters : invalid parameter value", and an
#: angular one under Precision::Angular. A part read at the wrong scale - an assembly whose unit is
#: missing or mixed, read as micrometres - asked for 3e-8 and crashed the geometry check. The floor
#: sits a decade above OCC's limit: a part that small is meshed a little coarser, never refused.
MIN_LINEAR_DEFLECTION = 1e-6
MIN_ANGULAR_DEFLECTION = 1e-3
#: The angular deflection used when a caller passes one that is no angle (NaN, zero, negative).
DEFAULT_ANGULAR_DEFLECTION = 0.5
#: The share of the measured diagonal a deflection takes when the caller's own is no length.
DEFAULT_DEFLECTION_FRACTION = 1.0 / 1000.0

#: A loose envelope this much wider (on the diagonal) than the surface itself means the deflection
#: it set was off by more than the meshing's own slack: the part is meshed again at its real size.
REMESH_LOOSENESS = 1.5

#: THE TESSELLATION FOLLOWS THE MODEL. A part of up to this many faces is meshed exactly as it always
#: was; past it the deflections grow with the square root of the face count (`coarsening`) and no
#: element is made smaller than the deflection (OpenCascade's MinSize) - a 3D-printer frame of
#: 40,536 faces and 809 solids made 3.1 million triangles in 4.4 minutes at the small-part settings,
#: and a few hundred thousand in seconds this way, which is all a view or a size needs.
FULL_DETAIL_FACES = 3000
#: The coarsest the angular deflection gets (radians), whatever the face count.
MAX_ANGULAR = 0.6


def face_count(shape) -> int:
    """How many faces the shape has (every placed instance counted)."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    n = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        n += 1
        exp.Next()
    return n


def coarsening(n_faces: int) -> float:
    """How much coarser than the small-part settings a model of `n_faces` faces is meshed: 1 up to
    FULL_DETAIL_FACES, then the square root of how many times past it."""
    return 1.0 if n_faces <= FULL_DETAIL_FACES else math.sqrt(n_faces / FULL_DETAIL_FACES)


def triangle_count(shape) -> int:
    """The triangles the shape's faces carry now (every placed instance counted)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    n = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(exp.Current()), TopLoc_Location())
        exp.Next()
        if tri is not None:
            n += tri.NbTriangles()
    return n


def _mesh(shape, lin, angular, min_size: bool = False) -> float:
    """Mesh at `lin` and `angular` - through incremental_mesh, which takes only numbers OpenCascade
    accepts - or, with `min_size`, with no element smaller than the deflection (a big model's view).
    Returns the linear deflection used."""
    if not min_size:
        return incremental_mesh(shape, lin, angular)
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IMeshTools import IMeshTools_Parameters
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    lin_ok = _usable(lin)
    lin = max(lin_ok if lin_ok is not None else deflection_for(diagonal(loose_box(shape)),
                                                               DEFAULT_DEFLECTION_FRACTION), MIN_LINEAR_DEFLECTION)
    ang = _usable(angular)
    ang = DEFAULT_ANGULAR_DEFLECTION if ang is None else max(ang, MIN_ANGULAR_DEFLECTION)
    params = IMeshTools_Parameters()
    params.Deflection = lin
    params.Angle = ang
    params.Relative = False
    params.InParallel = True
    params.MinSize = lin
    BRepMesh_IncrementalMesh(shape, params)
    # A FACE SMALLER THAN THE MINIMUM SIZE is left bare by the mesher (1,449 of jubilee_frame's
    # 40,536 faces: screw heads and chamfers a millimetre across) - a hole in the view. Each is meshed
    # on its own at the plain settings: a few triangles apiece.
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        exp.Next()
        if BRep_Tool.Triangulation_s(face, TopLoc_Location()) is None:
            incremental_mesh(face, lin, ang, parallel=False)
    return lin


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


def deflection_for(size: float, fraction: float) -> float:
    """A linear deflection of `fraction` of a measured `size` (a diagonal), never below the floor
    OpenCascade accepts. A size that is no size (zero, negative, NaN, infinite) gets the floor:
    the deflection never depends on the unit being known."""
    try:
        lin = float(size) * float(fraction)
    except (TypeError, ValueError):
        return MIN_LINEAR_DEFLECTION
    if not math.isfinite(lin) or lin < MIN_LINEAR_DEFLECTION:
        return MIN_LINEAR_DEFLECTION
    return lin


def _usable(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0.0 else None


def incremental_mesh(shape, linear_deflection, angular_deflection, *, relative: bool = False,
                     parallel: bool = True) -> float:
    """BRepMesh_IncrementalMesh with parameters OpenCascade will accept, whatever the unit state:
    every mesh call on the intake, check and stage paths goes through here. A linear deflection
    that is no length (zero, negative, NaN, infinite) is taken from the shape's MEASURED size
    instead; a tiny one is raised to MIN_LINEAR_DEFLECTION; an angle that is no angle becomes
    DEFAULT_ANGULAR_DEFLECTION. Returns the linear deflection the shape was meshed at."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    lin = _usable(linear_deflection)
    if lin is None:
        lin = deflection_for(diagonal(loose_box(shape)), DEFAULT_DEFLECTION_FRACTION)
    lin = max(lin, MIN_LINEAR_DEFLECTION)
    ang = _usable(angular_deflection)
    ang = DEFAULT_ANGULAR_DEFLECTION if ang is None else max(ang, MIN_ANGULAR_DEFLECTION)
    BRepMesh_IncrementalMesh(shape, lin, bool(relative), ang, bool(parallel))
    return lin


def mesh_to_size(shape, fraction: float, angular_deflection: float, *,
                 linear_deflection: float | None = None, min_size: bool = False) -> tuple[Box, float]:
    """Mesh the shape at `fraction` of its real diagonal (or at an explicit linear deflection) and
    return (its real box - the box of the meshed surface, the linear deflection it was meshed at).

    The size is not known before a mesh exists, and OpenCascade's only free box is the loose
    envelope. So: mesh at the envelope's size (what was always done - a file whose envelope is
    honest meshes exactly as before), measure the surface, and only when the envelope was
    REMESH_LOOSENESS or more too big mesh again at the real size, then measure once more. The
    returned box is never the envelope unless nothing could be meshed at all. `min_size`: no
    element smaller than the linear deflection (a big model's view, never a small part's)."""
    envelope = loose_box(shape)
    d_env = diagonal(envelope)
    # an explicit deflection that is no length is no request: the part's size decides
    explicit = _usable(linear_deflection) if linear_deflection is not None else None
    lin = explicit if explicit is not None else deflection_for(d_env, fraction)
    lin = _mesh(shape, lin, angular_deflection, min_size)
    box = surface_box(shape)
    if box is None:
        return envelope, lin
    d_real = diagonal(box)
    if explicit is None and d_real > 0.0 and d_env > REMESH_LOOSENESS * d_real:
        lin = _mesh(shape, deflection_for(d_real, fraction), angular_deflection, min_size)
        box = surface_box(shape) or box
    return box, lin


def mesh_for_model(shape, fraction: float, angular_deflection: float) -> tuple[Box, float]:
    """mesh_to_size at the settings the model's size calls for: exactly `fraction` and
    `angular_deflection` for a part of up to FULL_DETAIL_FACES faces, coarser by `coarsening` past
    it, with no element under the deflection (FULL_DETAIL_FACES)."""
    k = coarsening(face_count(shape))
    if k <= 1.0:
        return mesh_to_size(shape, fraction, angular_deflection)
    return mesh_to_size(shape, fraction * k, min(angular_deflection * k, max(MAX_ANGULAR, angular_deflection)),
                        min_size=True)


def remesh(shape, lin: float, angular_deflection: float) -> None:
    """Throw the shape's triangles away and mesh it again (OpenCascade keeps a finer mesh it already
    has), with no element under the deflection."""
    from OCP.BRepTools import BRepTools

    BRepTools.Clean_s(shape)
    _mesh(shape, lin, angular_deflection, True)


__all__ = ["DEFAULT_ANGULAR_DEFLECTION", "DEFAULT_DEFLECTION_FRACTION", "FULL_DETAIL_FACES", "MAX_ANGULAR",
           "MIN_ANGULAR_DEFLECTION", "MIN_LINEAR_DEFLECTION", "REMESH_LOOSENESS", "coarsening", "deflection_for",
           "diagonal", "face_count", "incremental_mesh", "loose_box", "mesh_for_model", "mesh_to_size", "remesh",
           "surface_box", "triangle_count"]
