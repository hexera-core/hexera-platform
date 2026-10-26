"""OpenFOAM is not run on a case nobody has looked at.

The platform scans a case's dictionaries before handing them to OpenFOAM, because a dictionary can
name a file to read or a command to run. Every other OpenFOAM bundle does it at the top of its
runner: cfmesh/native.py:105, cfmesh/cfmesh_runner.py:148, snappy/native.py:34 and
snappy_multiregion/native.py:79.

The conjugate path did not. It scanned only when `check_mesh` ran, which is AFTER cartesianMesh,
topoSet and splitMeshRegions have each executed - so the guard was reporting on dictionaries that
had already been handed to OpenFOAM three times. Its own docstring claimed otherwise. Nothing
anywhere pinned "a foam invocation is preceded by a scan", so the engine row could have landed
green with the guard missing, and an engine row is exactly the change that makes a CUSTOMER'S
workspace the argument.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Just enough for build() to get as far as the stages."""
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    lo, hi = (0.0, 0.0, 0.0), (0.002, 0.010, 0.001)
    (tmp_path / "constant" / "triSurface" / "block.stl").write_text(C.block_surface_stl(lo, hi))
    (tmp_path / "constant" / "triSurface" / "fluid.stl").write_text(C.block_surface_stl(lo, hi))
    return tmp_path


def test_a_rejected_case_stops_before_the_mesher_runs(workspace, monkeypatch):
    ran: list[str] = []

    def never(ws, script, log, **kw):
        ran.append(script)
        return 0, 0.1

    monkeypatch.setattr(C, "_foam", never)
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "meshDict names a file outside the case")

    with pytest.raises(C.ConjugateError) as caught:
        C.build(workspace, background_cell_m=0.0002)

    assert ran == [], f"OpenFOAM was run anyway: {ran}"
    assert "rejected before any stage ran" in str(caught.value)
    assert "outside the case" in str(caught.value), "the reason has to reach the user"


def test_a_clean_case_reaches_the_mesher(workspace, monkeypatch):
    """The other half, so the guard cannot pass by refusing everything."""
    ran: list[str] = []

    def record(ws, script, log, **kw):
        ran.append(script)
        return 1, 0.1                      # fail the first stage: this test is about reaching it

    monkeypatch.setattr(C, "_foam", record)
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "")

    with pytest.raises(C.ConjugateError):
        C.build(workspace, background_cell_m=0.0002)
    assert ran[:1] == ["cartesianMesh"], f"the mesher should have been reached, got {ran}"


def test_the_scan_is_the_shared_one(monkeypatch):
    # Not a copy of its own: the bundle re-exports the guard from sandbox, as the others do.
    import meshpipeline.sandbox.foam_case_guard as guard
    from meshpipeline.engines.cfmesh_conjugate import foam_exec
    assert foam_exec.scan_case_dicts is guard.scan_case_dicts
    assert C.scan_case_dicts is guard.scan_case_dicts


def test_every_openfoam_bundle_scans_before_it_runs():
    """The rule this engine was the exception to, written down so it stays a rule.

    Each bundle that drives OpenFOAM must call scan_case_dicts somewhere it can refuse from -
    which, for the conjugate path, means build() and not only check_mesh().
    """
    import inspect

    from meshpipeline.engines.cfmesh import native as cfmesh_native
    from meshpipeline.engines.snappy import native as snappy_native
    from meshpipeline.engines.snappy_multiregion import native as mr_native

    for mod in (cfmesh_native, snappy_native, mr_native):
        assert "scan_case_dicts" in inspect.getsource(mod), mod.__name__
    assert "scan_case_dicts" in inspect.getsource(C.build), (
        "conjugate.build must scan the case itself; check_mesh runs too late to guard the stages")
