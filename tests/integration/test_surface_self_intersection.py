# Responsibility: Verify crossing triangles are flagged, adjacent ones are not, and admission acts on the finding.
import numpy as np
import pytest

pytest.importorskip("pyvista")
pytest.importorskip("vtkmodules")

import pyvista as pv  # noqa: E402
from tests._geometry_support import prepared_surface  # noqa: E402

from meshpipeline.cad.surface_checks import self_intersects, surface_analysis_for  # noqa: E402
from meshpipeline.engines.admission import AdmissionEvidence  # noqa: E402
from meshpipeline.engines.registry import get_spec  # noqa: E402

# Real runtime image (pyvista/vtk) but NO external service - see the `hermetic` marker.
pytestmark = pytest.mark.hermetic


def _save(mesh, tmp_path, name):
    p = tmp_path / name
    mesh.extract_surface().triangulate().save(str(p))
    return p


def test_clean_closed_surfaces_are_not_flagged(tmp_path):
    for name, m in (("sphere.stl", pv.Sphere(theta_resolution=60, phi_resolution=60)),
                    ("torus.stl", pv.ParametricTorus())):
        assert self_intersects(_save(m, tmp_path, name)) is False, f"{name} wrongly flagged"


def test_two_triangles_that_cross_are_flagged(tmp_path):
    pts = np.array([
        [0, 0, 0], [2, 0, 0], [0, 2, 0],            # triangle A in z=0
        [0.5, 0.5, -1], [0.5, 0.5, 1], [1.5, 0.5, 0],  # triangle B pierces A
    ], dtype=float)
    faces = np.hstack([[3, 0, 1, 2], [3, 3, 4, 5]])
    surf = pv.PolyData(pts, faces)
    p = tmp_path / "cross.vtp"
    surf.save(str(p))
    assert self_intersects(p) is True


def test_two_disjoint_triangles_are_not_flagged(tmp_path):
    pts = np.array([
        [0, 0, 0], [1, 0, 0], [0, 1, 0],
        [5, 5, 5], [6, 5, 5], [5, 6, 5],
    ], dtype=float)
    faces = np.hstack([[3, 0, 1, 2], [3, 3, 4, 5]])
    surf = pv.PolyData(pts, faces)
    p = tmp_path / "disjoint.vtp"
    surf.save(str(p))
    assert self_intersects(p) is False


def test_adjacent_triangles_sharing_an_edge_are_not_a_crossing(tmp_path):
    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    faces = np.hstack([[3, 0, 1, 2], [3, 0, 2, 3]])
    surf = pv.PolyData(pts, faces)
    p = tmp_path / "quad.vtp"
    surf.save(str(p))
    assert self_intersects(p) is False


# the admission REJECTION, driven by the real predicate (kills self_intersects -> False)

def _two_overlapping_spheres(tmp_path):
    a = pv.Sphere(radius=1.0, center=(0, 0, 0), theta_resolution=24, phi_resolution=24)
    b = pv.Sphere(radius=1.0, center=(1.2, 0, 0), theta_resolution=24, phi_resolution=24)
    p = tmp_path / "overlapping_spheres.stl"
    (a + b).extract_surface().triangulate().save(str(p))
    return p


def _one_clean_sphere(tmp_path):
    p = tmp_path / "clean_sphere.stl"
    pv.Sphere(theta_resolution=40, phi_resolution=40).extract_surface().triangulate().save(str(p))
    return p


def _admit_codes(analysis):
    spec = get_spec("vmtk")
    ev = AdmissionEvidence(engine="vmtk", purpose="internal_cfd", input_kind="surface",
                           dimensionality="3D", surface_analysis=analysis)
    return {r.code: r for r in spec.admit(ev)}


def test_self_intersecting_surface_is_rejected_by_vmtk_admission(tmp_path):
    p = _two_overlapping_spheres(tmp_path)
    # These fixtures are written directly in metres; the contract makes that an
    # explicit claim rather than an assumption the reader has to make.
    analysis = surface_analysis_for(get_spec("vmtk"), prepared_surface(p))
    assert analysis is not None, "surface_analysis_for deferred on a valid (if defective) surface"
    assert analysis.get("self_intersecting") is True

    codes = _admit_codes(analysis)
    assert "geometry_unsuitable" in codes, \
        f"vmtk admitted a self-intersecting lumen; rejections were {sorted(codes)}"
    assert "self-intersect" in codes["geometry_unsuitable"].message
    assert codes["geometry_unsuitable"].phase == "measured"


def test_clean_surface_is_admitted_by_vmtk_measured_phase(tmp_path):
    p = _one_clean_sphere(tmp_path)
    # These fixtures are written directly in metres; the contract makes that an
    # explicit claim rather than an assumption the reader has to make.
    analysis = surface_analysis_for(get_spec("vmtk"), prepared_surface(p))
    assert analysis is not None
    assert analysis.get("self_intersecting") is False
    assert "geometry_unsuitable" not in _admit_codes(analysis)
