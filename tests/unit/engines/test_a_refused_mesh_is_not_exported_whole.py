"""A big mesh its own gate refused does not get a second copy of every cell written beside it.

WHY THIS EXISTS, measured on 2026-10-29. A 336,666,582-cell conjugate mesh finished, and its own
verdict was already written:

    fluid is in 5 disconnected piece(s), expected 2. Islands mean the cells are too coarse to hold
    the passage together; this mesh will not solve even though checkMesh calls it OK.

`foamToVTK -constant -noFaceZones -allRegions` was then started on it anyway, with 168 GB left on
the disk, and had to be killed by hand to stop it taking the box's filesystem with it. The export
is a second copy of every cell; at that size it is hundreds of gigabytes, and no review was ever
going to open it, because the mesh had been refused.

THE ALWAYS-EXPORT RULE IS STILL RIGHT AT THE SIZES IT WAS WRITTEN FOR, and native.py says why:
"ALWAYS, on every run, and that is deliberate. The volume artifact is declared optional -
requiring it would end a review before the model was asked, on a mesh that passed every gate - so
the way the region slices actually materialise is by the volume being THERE." A 10M-cell refusal
exports in seconds and a reviewer may yet be asked about it. A 300M one cannot be.

So BOTH conditions: refused AND big. Either alone still exports.

THE SURFACE IS ALWAYS BUILT. It is small, it is a required render artifact, and a reader of a
refusal wants to see what was refused.
"""
import inspect

import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N


def test_the_bar_is_both_refused_and_big():
    src = inspect.getsource(N._review_aids)
    assert "refused and int(cells) > VOLUME_SKIP_ABOVE_CELLS" in src, (
        "a passing mesh of any size still exports, and so does a small refusal")


def test_the_size_is_named_rather_than_written_into_the_condition():
    assert N.VOLUME_SKIP_ABOVE_CELLS == 40_000_000


@pytest.mark.parametrize("refused, cells, skips", [
    (True, 336_666_582, True),      # the run this exists for
    (True, 100_000_000, True),
    (True, 40_000_001, True),
    (True, 40_000_000, False),      # at the bar, not over it
    (True, 10_000_000, False),      # a small refusal still exports: a reviewer may be asked
    (False, 336_666_582, False),    # a mesh that PASSED always exports, whatever its size
    (False, 10_000_000, False),
])
def test_which_runs_skip_it(tmp_path, refused, cells, skips, monkeypatch):
    """Exercised through `_review_aids` itself rather than a re-implementation of its condition."""
    called: list = []

    def _no_export(*a, **k):
        called.append(k)
        return "VTK"

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk", _no_export)
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.review_surface.build_region_msh",
        lambda ws: ({}, None))

    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=refused, cells=cells)
    if skips:
        assert called == [], "the export must not have been started"
        assert out["volume"] is None
        assert "volume_skipped" in out
        assert f"{cells:,}" in out["volume_skipped"]
    else:
        assert called, "the export has to run"
        assert "volume_skipped" not in out


def test_the_skip_is_said_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.review_surface.build_region_msh",
        lambda ws: ({}, None))
    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=True, cells=336_666_582)
    said = out["volume_skipped"]
    assert "refused" in said
    assert "second copy of every cell" in said
    assert "no review will open it" in said


def test_a_skipped_export_is_not_reported_as_a_failed_one(tmp_path, monkeypatch):
    """`why` exists so a run that could not be reviewed says which of the two things happened.
    "no VTK boundary was written" of an export nobody asked for sends a reader to look for a
    bug."""
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.review_surface.build_region_msh",
        lambda ws: ({}, None))
    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=True, cells=336_666_582)
    assert out["surface"] is None
    assert out["why"] == out["volume_skipped"]
    assert "no VTK boundary was written" not in out["why"]


def test_the_runner_tells_it_what_the_mesh_was():
    """THE GAP THIS PACKAGE HAS HAD TWICE: a function that takes the facts and a caller that does
    not pass them."""
    src = inspect.getsource(N._run_conjugate_local)
    assert "refused=bool(report.problems)" in src
    assert "cells=int(report.fluid.cells + report.solid.cells)" in src


def test_the_default_is_to_export(tmp_path, monkeypatch):
    """Every caller that predates this - and there is one, in the tests - keeps the old behaviour."""
    sig = inspect.signature(N._review_aids)
    assert sig.parameters["refused"].default is False
    assert sig.parameters["cells"].default == 0
