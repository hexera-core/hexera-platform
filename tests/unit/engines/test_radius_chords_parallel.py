# Responsibility: Verify the local-radius chords cast on several processes give the field one core
# gives, number for number, and that a failed worker costs speed, never the field.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import radius_chords as RC
from meshpipeline.engines.radius_field import local_radius


@pytest.fixture(scope="module")
def duct():
    """A closed, wavy-walled duct: radius varying along its length, about 5,000 wall points."""
    import pyvista as pv
    t = pv.Cylinder(radius=1.0, height=8.0, resolution=96, capping=True).triangulate()
    t = t.subdivide(2, "linear").clean()
    p = np.asarray(t.points, dtype=float).copy()
    rad = np.hypot(p[:, 1], p[:, 2])
    side = rad > 0.999
    p[side, 1:] *= (1.0 + 0.3 * np.sin(p[side, 0]))[:, None]
    faces = np.asarray(t.faces).reshape(-1, 4)[:, 1:]
    return p, faces


def _field(points, faces):
    return local_radius(points, faces, np.zeros(3), 1e-5, 4.0)


def test_many_processes_read_the_field_one_core_reads(duct, monkeypatch):
    points, faces = duct
    monkeypatch.setattr(RC, "workers_for", lambda n: 1)
    one = _field(points, faces)
    monkeypatch.setattr(RC, "workers_for", lambda n: 4)
    many = _field(points, faces)
    assert len(points) > 1000
    assert np.array_equal(one, many, equal_nan=True)
    assert np.isfinite(one).all() and one.min() > 0


def test_a_failed_worker_leaves_its_points_to_be_cast_here(duct, monkeypatch):
    points, faces = duct
    monkeypatch.setattr(RC, "workers_for", lambda n: 1)
    one = _field(points, faces)
    monkeypatch.setattr(RC, "workers_for", lambda n: 3)
    monkeypatch.setattr(RC.sys, "executable", "/bin/false")
    assert np.array_equal(one, _field(points, faces), equal_nan=True)


def test_few_points_stay_on_one_core_and_many_never_pass_the_pinned_threads(monkeypatch):
    assert RC.workers_for(RC.PARALLEL_FROM - 1) == 1
    import meshpipeline.engines.determinism as det
    monkeypatch.setattr(det, "MESH_THREADS", 3)
    monkeypatch.setattr(RC.os, "sched_getaffinity", lambda pid: set(range(64)))
    assert RC.workers_for(60_000) == 3
    monkeypatch.setattr(RC.os, "sched_getaffinity", lambda pid: {0, 1})
    assert RC.workers_for(60_000) == 2
