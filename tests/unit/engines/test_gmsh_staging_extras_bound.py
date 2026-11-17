# Responsibility: The optional parts of gmsh staging - gmsh's own B-rep, the face names, the
#                 surface picture - never cost a caller the whole staging wall.
from __future__ import annotations

import subprocess
from pathlib import Path

from meshpipeline.engines.gmsh import gmsh_runner


def _fake_stage(record: list, *, times_out=()):
    """gmsh's staging process, faked: it records the wall each mode was given, times out for the
    modes named, and otherwise writes the staged solid as the real one does."""

    def run(args, *, timeout=None, **kwargs):
        mode, out_stl = args[-1], Path(args[-3])
        record.append((mode, float(timeout)))
        if mode in times_out:
            raise subprocess.TimeoutExpired(args, timeout)
        (out_stl.parent / "geometry.step").write_text("staged")
        return subprocess.CompletedProcess(args, 0, gmsh_runner._STAGED + "not drawn\n", "")

    return run


def test_the_optional_parts_get_their_own_wall_not_the_staging_one(tmp_path, monkeypatch):
    """A part whose picture never finishes must not spend the whole wall drawing it.

    The staged solid is what the driver meshes; the B-rep, the face names and the surface picture
    are a report's. Charging the optional parts the full wall is what refused a sound repair on the
    Formula car: the promotion proof stages, and its own clock (CAD_REPAIR_TIMEOUT_S) ran out while
    gmsh was still drawing a picture nobody meshes.
    """
    record: list = []
    monkeypatch.setattr(gmsh_runner, "run_guarded", _fake_stage(record, times_out=("full",)))

    drawn = gmsh_runner._stage_guarded(tmp_path / "part.step", tmp_path / "input.stl", 0.001)

    assert drawn is False                                  # staged by `plain`, so no picture
    assert [mode for mode, _ in record] == ["full", "plain"]
    walls = dict(record)
    assert walls["full"] == gmsh_runner.STAGE_EXTRAS_TIMEOUT_S
    assert walls["plain"] == gmsh_runner.STAGE_TIMEOUT_S
    # the point of the bound: the optional parts cost a fraction of the staging wall
    assert gmsh_runner.STAGE_EXTRAS_TIMEOUT_S < gmsh_runner.STAGE_TIMEOUT_S


def test_a_part_that_stages_whole_is_still_staged_whole(tmp_path, monkeypatch):
    """The bound changes nothing for a part gmsh stages in time: one process, `full`, extras and all."""
    record: list = []
    monkeypatch.setattr(gmsh_runner, "run_guarded", _fake_stage(record))

    gmsh_runner._stage_guarded(tmp_path / "part.step", tmp_path / "input.stl", 0.001)

    assert [mode for mode, _ in record] == ["full"]
