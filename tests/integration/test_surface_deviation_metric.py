# Responsibility: Verify the surface deviation metric reads near zero on itself and poorly on a large offset.
import numpy as np
import pytest

# Genuine absence -> honest skip; contamination is caught loudly by the tier conftest.
pytest.importorskip("pyvista")
import pyvista as pv  # noqa: E402

from meshpipeline.cad.surface_checks import surface_deviation  # noqa: E402

# Real runtime image (pyvista/vtk) but NO external service - see the `hermetic` marker.
pytestmark = pytest.mark.hermetic


def _sphere_stl(tmp_path, name, offset=0.0):
    s = pv.Sphere(radius=1.0, theta_resolution=80, phi_resolution=80).triangulate()
    if offset:
        s = s.copy()
        s.points = np.asarray(s.points) + np.array([offset, 0.0, 0.0])
    p = tmp_path / name
    s.save(str(p))
    return p, s


def test_surface_on_itself_deviates_near_zero(tmp_path):
    ref, s = _sphere_stl(tmp_path, "ref.stl")
    snap = tmp_path / "snap.vtp"
    s.save(str(snap))
    d = surface_deviation(snap, ref)
    assert d is not None
    assert d["mean_ratio"] < 0.05 and d["p95_ratio"] < 0.1   # sits on the CAD


def test_a_large_offset_reads_as_poor_capture(tmp_path):
    ref, _ = _sphere_stl(tmp_path, "ref.stl")
    # cell size on this sphere ~ 2*pi/80 ≈ 0.078; offset by ~one cell
    snap_path, _ = _sphere_stl(tmp_path, "snap.vtp", offset=0.08)
    d = surface_deviation(snap_path, ref)
    assert d is not None
    assert d["p95_ratio"] > 0.5   # clearly off the CAD - the 'staircased' signal
    assert d["frac_beyond_one_cell"] >= 0.0


def test_missing_inputs_return_none(tmp_path):
    assert surface_deviation(tmp_path / "nope.vtp", tmp_path / "nope.stl") is None
