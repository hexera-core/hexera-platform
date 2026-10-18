# Responsibility: Tell a STEP file that is really a triangle mesh (a scan or an STL dressed as STEP) from a real CAD B-rep, and read its triangles.
# Owns: the measured census of a STEP's faces, the faceted rule, and the exact text reader of flat polygon faces.
# Boundaries: streams the file's text; it scales nothing (triangles come back in the file's own numbers) and leaves real B-reps to OpenCASCADE.
# Collaborates with: cad/ingest/canonical.py (routes a faceted STEP to the surface path), cad/unit_evidence.py (its unit label is a converter's, not evidence).
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from meshpipeline.cad.ingest.limits import MAX_TRIANGLES, too_many_triangles

#: A faceted STEP has many faces; a real CAD part with a few flat faces stays CAD whatever they
#: look like. 100 faces is far more than a hand-modelled prism, far fewer than any scan or STL.
MIN_FACES = 100
#: ...almost every face bounded by three edges (a triangle)...
MIN_TRIANGLE_SHARE = 0.9
#: ...and (almost) no curved surface: a real B-rep carries cylinders, cones and splines.
MAX_CURVED_SHARE = 0.01

_CHUNK = 8 * 1024 * 1024
_FACE = re.compile(rb"=\s*(?:ADVANCED_FACE|FACE_SURFACE)\s*\(")
_PLANE = re.compile(rb"=\s*PLANE\s*\(")
_CURVED = re.compile(rb"\b(?:CYLINDRICAL_SURFACE|CONICAL_SURFACE|SPHERICAL_SURFACE|TOROIDAL_SURFACE|"
                     rb"DEGENERATE_TOROIDAL_SURFACE|B_SPLINE_SURFACE|BEZIER_SURFACE|"
                     rb"SURFACE_OF_REVOLUTION|SURFACE_OF_LINEAR_EXTRUSION|OFFSET_SURFACE)\s*\(")
_LOOP = re.compile(rb"=\s*(?:EDGE_LOOP|POLY_LOOP)\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_FACETED_BREP = re.compile(rb"=\s*FACETED_BREP\s*\(")
_ASSEMBLY = re.compile(rb"\b(?:NEXT_ASSEMBLY_USAGE_OCCURRENCE|MAPPED_ITEM|ITEM_DEFINED_TRANSFORMATION)\s*\(")


@dataclass(frozen=True)
class StepCensus:
    faces: int = 0
    planes: int = 0
    curved: int = 0
    loops: int = 0
    triangle_loops: int = 0
    faceted_brep: bool = False
    assembly: bool = False

    @property
    def faceted(self) -> bool:
        """A mesh in STEP clothing: many faces, nearly all flat triangles, no curved surface
        (or the STEP's own FACETED_BREP, which is a mesh by definition)."""
        if self.faces < MIN_FACES or self.curved > MAX_CURVED_SHARE * self.faces:
            return False
        if self.faceted_brep:
            return True
        return self.loops > 0 and self.triangle_loops >= MIN_TRIANGLE_SHARE * self.loops

    def describe(self) -> str:
        return (f"{self.faces:,} faces, {self.triangle_loops:,} of them triangles, "
                f"{self.curved:,} curved surfaces")


def _chunks(path: Path):
    """The file in whole entities: every chunk ends at a ';', so no entity is split."""
    tail = b""
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                if tail:
                    yield tail
                return
            data = tail + block
            cut = data.rfind(b";")
            if cut < 0:
                tail = data
                continue
            yield data[:cut + 1]
            tail = data[cut + 1:]


def census(path) -> StepCensus:
    faces = planes = curved = loops = tri = 0
    faceted = assembly = False
    for chunk in _chunks(Path(path)):
        faces += len(_FACE.findall(chunk))
        planes += len(_PLANE.findall(chunk))
        curved += len(_CURVED.findall(chunk))
        for refs in _LOOP.findall(chunk):
            loops += 1
            tri += refs.count(b"#") == 3
        faceted = faceted or bool(_FACETED_BREP.search(chunk))
        assembly = assembly or bool(_ASSEMBLY.search(chunk))
    return StepCensus(faces, planes, curved, loops, tri, faceted, assembly)


def is_faceted_step(path) -> StepCensus | None:
    """The census when the STEP file is a faceted mesh, else None."""
    c = census(path)
    return c if c.faceted else None


# the exact reader: flat polygon faces, straight from the text

_POINT = re.compile(rb"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)\s*\)\s*;")
_POLY_LOOP = re.compile(rb"#(\d+)\s*=\s*POLY_LOOP\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_EDGE_LOOP = re.compile(rb"#(\d+)\s*=\s*EDGE_LOOP\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_ORIENTED = re.compile(rb"#(\d+)\s*=\s*ORIENTED_EDGE\s*\(\s*'[^']*'\s*,\s*\*\s*,\s*\*\s*,\s*#(\d+)\s*,"
                       rb"\s*\.([TF])\.\s*\)")
_EDGE_CURVE = re.compile(rb"#(\d+)\s*=\s*EDGE_CURVE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)")
_VERTEX = re.compile(rb"#(\d+)\s*=\s*VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")
_BOUND = re.compile(rb"#(\d+)\s*=\s*FACE_(OUTER_)?BOUND\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*\.([TF])\.\s*\)")
_FACES = re.compile(rb"#(\d+)\s*=\s*(?:FACE_SURFACE|ADVANCED_FACE)\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)")
_REF = re.compile(rb"#(\d+)")


class FacetedReadError(ValueError):
    """The text reader cannot follow this faceted file (an assembly, a face with holes)."""


def read_faceted(path) -> tuple[np.ndarray, np.ndarray]:
    """(points, triangles) of a faceted STEP in the FILE's own numbers: each flat face's outer
    loop, in its own order (reversed where its bound says so), fanned into triangles. The facing
    of neighbours is made consistent afterwards by surface.tidy."""
    points: dict[int, tuple[float, float, float]] = {}
    poly: dict[int, list[int]] = {}
    edge_loop: dict[int, list[int]] = {}
    oriented: dict[int, tuple[int, bool]] = {}
    edge: dict[int, tuple[int, int]] = {}
    vertex: dict[int, int] = {}
    bound: dict[int, tuple[int, bool, bool]] = {}
    faces: list[list[int]] = []
    for chunk in _chunks(Path(path)):
        if _ASSEMBLY.search(chunk):
            raise FacetedReadError("the faceted STEP is an assembly; its placements need OCC")
        for m in _POINT.finditer(chunk):
            xyz = m.group(2).split(b",")
            if len(xyz) >= 3:
                points[int(m.group(1))] = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        for m in _POLY_LOOP.finditer(chunk):
            poly[int(m.group(1))] = [int(r) for r in _REF.findall(m.group(2))]
        for m in _EDGE_LOOP.finditer(chunk):
            edge_loop[int(m.group(1))] = [int(r) for r in _REF.findall(m.group(2))]
        for m in _ORIENTED.finditer(chunk):
            oriented[int(m.group(1))] = (int(m.group(2)), m.group(3) == b"T")
        for m in _EDGE_CURVE.finditer(chunk):
            edge[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
        for m in _VERTEX.finditer(chunk):
            vertex[int(m.group(1))] = int(m.group(2))
        for m in _BOUND.finditer(chunk):
            bound[int(m.group(1))] = (int(m.group(3)), m.group(4) == b"T", bool(m.group(2)))
        for m in _FACES.finditer(chunk):
            faces.append([int(r) for r in _REF.findall(m.group(2))])

    def loop_points(loop_id: int) -> list[int]:
        if loop_id in poly:
            return poly[loop_id]
        ring = []
        for oe in edge_loop.get(loop_id, []):
            e_id, forward = oriented[oe]
            start, end = edge[e_id]
            ring.append(vertex[start if forward else end])
        return ring

    index: dict[int, int] = {}
    tris: list[tuple[int, int, int]] = []
    for bounds in faces:
        if len(bounds) != 1:
            raise FacetedReadError("a faceted face has holes; the text reader takes flat polygons")
        b = bound.get(bounds[0])
        if b is None:
            raise FacetedReadError("a face's bound is not one the text reader knows")
        loop_id, same_way, _outer = b
        try:
            ring = loop_points(loop_id)
        except KeyError as exc:
            raise FacetedReadError("a face's loop refers to an entity the reader did not find") \
                from exc
        if not same_way:
            ring = ring[::-1]
        ids = []
        for p in ring:
            if p not in index:
                index[p] = len(index)
            ids.append(index[p])
        for k in range(1, len(ids) - 1):
            tris.append((ids[0], ids[k], ids[k + 1]))
        if len(tris) > MAX_TRIANGLES:
            from meshpipeline.cad.ingest.surface import SurfaceError

            raise SurfaceError(too_many_triangles())
    if not tris:
        raise FacetedReadError("the faceted STEP has no polygon faces")
    try:
        pts = np.array([points[p] for p in index], dtype=np.float64)
    except KeyError as exc:
        raise FacetedReadError("a face corner is not a point in the file") from exc
    return pts, np.asarray(tris, dtype=np.int64)


def read_faceted_occ(path) -> tuple[np.ndarray, np.ndarray]:
    """The same triangles through OpenCASCADE, for a faceted STEP the text reader cannot follow
    (an assembly with placements). Slow on big files; OCC's millimetres are turned back into the
    file's own numbers so the canonical surface carries them like any mesh file."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.ingest.cad import read_step
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.geometry_units import scale_to_metres

    shape = read_step(Path(path))
    BRepMesh_IncrementalMesh(shape, 1e-3, False, 0.5, True)
    to_file = 1e-3 / scale_to_metres(parser_applied_unit(path))   # OCC mm -> file units
    pts_all: list[np.ndarray] = []
    tris_all: list[np.ndarray] = []
    offset = 0
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            nodes = np.array([[p.X(), p.Y(), p.Z()] for p in
                              (tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1))])
            t = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)]) - 1
            if face.Orientation() == TopAbs_REVERSED:
                t = t[:, ::-1]
            pts_all.append(nodes * to_file)
            tris_all.append(t + offset)
            offset += len(nodes)
        ex.Next()
    if not tris_all:
        raise FacetedReadError("OpenCASCADE found no faces in the faceted STEP")
    return np.vstack(pts_all), np.vstack(tris_all).astype(np.int64)
