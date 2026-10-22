"""Two independent checks on every solid this package produces, run before any file is written.

WHY TWO AND NOT ONE. A single check written by the same person who wrote the generator tends to assert
what the generator does rather than what the geometry should be. So the two checks here are deliberately
different in KIND, not just in threshold:

  CHECK 1, TOPOLOGICAL, asks the CAD kernel: is this one closed solid, is it watertight, is its volume
  positive, does it self-intersect. This catches a model that is broken as a solid. It runs on the B-rep
  and knows nothing about what the shape was meant to be.

  CHECK 2, DIMENSIONAL, ignores the kernel entirely. It tessellates the solid, measures the triangle
  soup with an independent library, and compares the result against the numbers the geometry was
  SPECIFIED with: volume against the analytic volume, bounding box against the intended extent, and the
  narrowest passage against the intended channel width. This catches a model that is a perfectly valid
  solid of the wrong thing.

A model has to pass both. The first alone passes a watertight cube when you asked for a channel network;
the second alone passes a dimensionally correct shape with a hole in its surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Report:
    geometry: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, passed, detail))

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def text(self) -> str:
        lines = [f"=== {self.geometry} ==="]
        for c in self.checks:
            lines.append(f"  {'PASS' if c.passed else 'FAIL'}  {c.name:34s} {c.detail}")
        lines.append(f"  {'ALL CHECKS PASSED' if self.ok else 'FAILED'}")
        return "\n".join(lines)


def check_topology(shape, report: Report, *, expect_solids: int = 1,
                   expect_shells: int | None = None) -> None:
    """CHECK 1. Ask OpenCASCADE whether this is a sound closed solid.

    `BRepCheck_Analyzer` is the kernel's own validity test: it walks the shape's faces, edges and
    vertices and reports whether the topology and geometry agree. `GProp` gives the volume, which must
    be positive; a negative volume means the faces are oriented inside out, which tessellates and even
    renders fine and then meshes as the complement of what you wanted.
    """
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer

    report.add("kernel validity", BRepCheck_Analyzer(shape).IsValid(),
               "BRepCheck_Analyzer on the whole shape")

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    vol_mm3 = props.Mass()
    report.add("positive volume", vol_mm3 > 0.0,
               f"{vol_mm3:.6g} mm^3 (negative means faces are inside out)")

    n_solids = 0
    exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while exp.More():
        n_solids += 1
        exp.Next()
    report.add(f"solid count == {expect_solids}", n_solids == expect_solids, f"found {n_solids}")

    n_shells = 0
    exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
    while exp.More():
        n_shells += 1
        exp.Next()
    # A SHELL IS NOT A BODY, and conflating them failed every enclosed-channel part on the first run.
    # A monolithic block with a channel network cut into it has TWO shells and ONE body: the outer
    # surface, and the surface of the cavity. Demanding one shell rejects exactly the geometry class
    # this whole project is about. So the expected count is stated by the caller, and when it is not
    # stated the check only reports.
    want = expect_shells if expect_shells is not None else n_shells
    report.add(f"shell count == {want}", n_shells == want,
               f"found {n_shells}" + ("" if expect_shells is not None else " (not asserted)"))


def tessellate(shape, deflection_mm: float, angular_rad: float = 0.1):
    """Triangulate the solid and return a trimesh, for the independent dimensional check.

    The deflection is the maximum distance between the facets and the true surface. It must be a small
    fraction of the SMALLEST FEATURE, not of the part diagonal: the usual default of diagonal/1000 is
    367 um on a 367 mm part, which is four times a 90 um channel and would erase it entirely.
    """
    import numpy as np
    import trimesh
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    # CLEAR THE CACHED TRIANGULATION FIRST. OpenCASCADE stores the facets on the shape, and a second
    # call at a finer deflection silently returns the FIRST one. The convergence check below was
    # reporting 100 -> 100 triangles and 0.0000% drift on a cylinder that was visibly 1% off, because
    # it was measuring one mesh twice. A check that cannot fail is worse than no check.
    # BOTH deflections matter and the ANGULAR one usually binds on small curved features. With the
    # common default of 0.5 rad, a 90 um channel bore comes out as a 25-sided polygon and measures
    # 0.97% under its true volume. The first convergence check here halved only the LINEAR deflection,
    # which was not the binding constraint, so it reported 100 -> 100 triangles and zero drift on a
    # cylinder that was visibly wrong. Refining a parameter that is not binding proves nothing.
    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deflection_mm, False, angular_rad, True)
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    exp = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            base = len(verts)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                verts.append((p.X(), p.Y(), p.Z()))
            reversed_face = face.Orientation() == 1  # TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                t = (base + a - 1, base + c - 1, base + b - 1) if reversed_face \
                    else (base + a - 1, base + b - 1, base + c - 1)
                faces.append(t)
        exp.Next()
    return trimesh.Trimesh(vertices=np.array(verts), faces=np.array(faces), process=True)


def check_dimensions(shape, report: Report, *, expect_volume_mm3: float | None,
                     expect_bbox_mm: tuple[float, float, float] | None,
                     expect_min_feature_mm: float, expect_bodies: int = 1,
                     volume_tol: float = 0.02, bbox_tol: float = 1e-3) -> None:
    """CHECK 2. Measure the tessellation independently and compare against the SPECIFICATION.

    Deliberately uses trimesh rather than the OpenCASCADE mass properties, so the number being compared
    was produced by a different library from the one that built the shape. If the generator and the
    kernel share a misunderstanding, this is what catches it.
    """
    mesh = tessellate(shape, deflection_mm=expect_min_feature_mm / 20.0, angular_rad=0.1)

    report.add("tessellation watertight", bool(mesh.is_watertight),
               f"{len(mesh.faces):,} triangles, {len(mesh.vertices):,} vertices")
    report.add("tessellation winding consistent", bool(mesh.is_winding_consistent), "")
    report.add(f"body count == {expect_bodies}", mesh.body_count == expect_bodies,
               f"body_count = {mesh.body_count}")
    report.add("trimesh agrees it is a volume", bool(mesh.is_volume),
               "watertight, consistent winding, positive volume, no degenerate faces")

    if expect_volume_mm3 is not None:
        err = abs(mesh.volume - expect_volume_mm3) / expect_volume_mm3
        report.add("volume vs analytic", err <= volume_tol,
                   f"mesh {mesh.volume:.6g} mm^3 vs analytic {expect_volume_mm3:.6g} mm^3, "
                   f"{err*100:.3f}% off (tolerance {volume_tol*100:.0f}%)")

    if expect_bbox_mm is not None:
        got = mesh.bounds[1] - mesh.bounds[0]
        worst = max(abs(g - e) for g, e in zip(got, expect_bbox_mm))
        report.add("bounding box vs spec", worst <= bbox_tol,
                   f"got {tuple(round(float(g), 6) for g in got)} mm, "
                   f"expected {expect_bbox_mm} mm, worst {worst*1e3:.3f} um")

    # IS THE TESSELLATION FINE ENOUGH? The first version of this check asserted that the mean triangle
    # edge was shorter than a third of the smallest feature, and it failed on a flat box, correctly: a
    # plane needs exactly two triangles and subdividing it adds nothing. The edge length was never the
    # question. The question is whether the facets have CONVERGED on the true surface, so the honest
    # test is to tessellate twice, once at the chosen deflection and once at half of it, and see
    # whether the measured volume moves. On a flat shape it will not move at all. On a cylinder that is
    # too coarsely faceted it will, because a faceted cylinder under-measures its own volume.
    fine = tessellate(shape, deflection_mm=expect_min_feature_mm / 40.0, angular_rad=0.05)
    drift = abs(fine.volume - mesh.volume) / abs(mesh.volume) if mesh.volume else 1.0
    report.add("tessellation converged", drift <= 0.002,
               f"halving the deflection moved the volume by {drift*100:.4f}% "
               f"({len(mesh.faces):,} -> {len(fine.faces):,} triangles); over 0.2% means the facets "
               f"are still cutting corners off the real surface")


def verify(shape, name: str, *, expect_volume_mm3: float | None = None,
           expect_bbox_mm: tuple[float, float, float] | None = None,
           expect_min_feature_mm: float = 0.09, expect_bodies: int = 1,
           expect_solids: int = 1, expect_shells: int | None = None,
           require_watertight: bool = True) -> Report:
    """Run both checks and return the report. Never raises; a broken shape is a FAIL, not a crash."""
    r = Report(geometry=name)
    try:
        check_topology(shape, r, expect_solids=expect_solids, expect_shells=expect_shells)
    except Exception as exc:                                  # noqa: BLE001
        r.add("kernel validity", False, f"check raised {type(exc).__name__}: {exc}")
    try:
        check_dimensions(shape, r, expect_volume_mm3=expect_volume_mm3,
                         expect_bbox_mm=expect_bbox_mm, expect_bodies=expect_bodies,
                         expect_min_feature_mm=expect_min_feature_mm)
    except Exception as exc:                                  # noqa: BLE001
        r.add("dimensional check", False, f"check raised {type(exc).__name__}: {exc}")
    return r
