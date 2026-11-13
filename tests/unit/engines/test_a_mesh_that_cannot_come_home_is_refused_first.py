"""A mesh too big to send back is refused before it is built, not after.

WHY THIS EXISTS, measured end to end on 2026-10-29. The coupon plate went through the console,
meshed on the Cloud Run mesh job, split into two regions, built its review surface, and then:

    return scope: The mesh was too large to send back whole within the transfer limit (512 MiB),
    so the volume export the reviewer slices was left out (725 MB); the mesh, its boundary and
    every measurement are all there.
    return scope: the workspace is still about 570 MB compressed, over the 512 MiB ceiling, with
    nothing optional left to leave out

A Cloud Run execution and half an hour, to be told a number that was knowable before the first
cell. The mesher's workspace rides home as one archive through a bounded extractor, and the
archive grows with the cell count: `written_gib` already prices the mesh on disk for the machine
decision, so the same estimate prices the journey back.

IT IS NOT A CEILING ON THE ENGINE. At 336,666,582 cells the merged mesh alone is 71.6 GB on disk.
Above some size the archive simply stops being how a mesh travels, and this says so rather than
pretending a setting can fix it - though for the sizes where a setting CAN, the refusal names it,
because WORKSPACE_ARCHIVE_MAX_BYTES is a zip-bomb guard on an untrusted archive and a mesh job's
own workspace is not that.
"""
import inspect

import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B

MiB = 1024 ** 2
GiB = 1024 ** 3

#: What the coupon actually did: ~6.5M cells, 570 MB compressed - reported against a 512 MiB
#: ceiling, which turned out to be the WRONG ceiling. `return_scope` was pruning and refusing
#: against `safe_extract`'s generic 512 MiB while the caller extracts a returned mesh under
#: `result_extraction_limits`, which allowed 1,279 MiB once it read that mesh's cell count.
#: Nothing was too big. `OLD_CAP` is kept here because it is the number the run quoted and the
#: refusal's wording is still exercised through it; the DRIVER no longer uses it.
COUPON_CELLS = 6_500_000
OLD_CAP = 512 * MiB


def test_the_estimate_matches_the_run_that_prompted_it():
    """570 MB against a 512 MiB ceiling. The estimate has to land near that or it is describing
    something else: too high and it refuses meshes that come home, too low and it never fires."""
    got = B.archive_gib(COUPON_CELLS)
    assert 0.4 <= got <= 0.9, f"{got:.2f} GiB; the run came home at 570 MB"


def test_the_coupon_is_refused_at_the_ceiling_it_actually_hit():
    said = B.refuse_for_return(COUPON_CELLS, cap_bytes=OLD_CAP)
    assert said, "this is the exact run that failed at the return step"
    assert "cannot be sent home" in said
    assert "6,500,000" in said


def test_the_refusal_names_the_lever_that_moves_it_and_not_the_part():
    """It is a configuration to raise, not a geometry to redraw, and a customer told otherwise
    goes and edits their CAD for nothing.

    THE LEVER IS THE CELL BUDGET, not the generic archive ceiling, and the first version of this
    named the wrong one. `result_extraction_limits` sizes a returned mesh's archive from
    CELL_HARD_LIMIT and will not believe a cell count past twice it, so the generic
    WORKSPACE_ARCHIVE_MAX_BYTES is a FLOOR under that and raising it moves nothing.
    """
    said = B.refuse_for_return(COUPON_CELLS, cap_bytes=OLD_CAP)
    assert "CELL_HARD_LIMIT" in said
    assert "result_extraction_limits" in said
    assert "only a floor" in said
    assert "The mesh would build" in said


def test_the_same_mesh_passes_once_the_ceiling_is_raised():
    assert B.refuse_for_return(COUPON_CELLS, cap_bytes=8 * GiB) == ""


#   -- the direction the error leans ------------------------------------------------------------

def test_it_under_states_the_archive_rather_than_over_stating_it():
    """A wasted run is an hour; a wrongly refused mesh is a part the customer cannot have. The
    first version of this guessed a quarter of the written size, which prices the coupon at 2.8
    GiB and would have refused a mesh that came home fine."""
    assert B.ARCHIVE_SHARE_OF_WRITTEN < 0.1
    assert B.archive_gib(COUPON_CELLS) < 0.25 * B.written_gib(COUPON_CELLS)


def test_a_mesh_well_inside_the_ceiling_is_never_refused():
    for cells in (100_000, 1_000_000, 5_000_000, 28_000_000):
        assert B.refuse_for_return(cells, cap_bytes=8 * GiB) == "", cells


def test_the_band_this_engine_is_for_is_what_it_catches():
    """100 to 700 million cells is the working band, and no archive ceiling carries it."""
    for cells in (102_630_992, 336_666_582, 700_000_000):
        assert B.refuse_for_return(cells, cap_bytes=8 * GiB), cells


@pytest.mark.parametrize("cap", [0, -1])
def test_an_unknown_ceiling_says_nothing(cap):
    """A fact nobody has refuses nothing - the same rule the turf facts and admit() follow."""
    assert B.refuse_for_return(10 ** 9, cap_bytes=cap) == ""


def test_it_grows_with_the_mesh():
    assert B.archive_gib(10_000_000) > B.archive_gib(1_000_000)
    assert B.archive_gib(0) >= 0.0


#   -- and the driver checks it before anything runs --------------------------------------------

class _Outcome:
    def __init__(self, produced_deliverable: bool, terminal_value: str) -> None:
        self.produced_deliverable = produced_deliverable
        self.terminal_value = terminal_value


class _Run:
    """The builder run surface `drive` uses, with no ownership fence to assert against."""

    def __init__(self) -> None:
        self.native_runs = 0

    async def fence(self, phase: str) -> None:
        return None

    def note_authoring(self) -> None:
        return None

    def note_native_run(self, *, produced_usable_mesh: bool) -> None:
        self.native_runs += 1

    def outcome(self, *, produced_deliverable: bool, exhausted: bool = False,
                failure_marker: str = ""):
        return _Outcome(produced_deliverable, failure_marker)


def _drive_a_part_of(monkeypatch, tmp_path, *, estimate: int):
    """Run the REAL `drive` on a part whose cell estimate is `estimate`, with the mesher replaced
    by a spy. Everything between the CAD and the size gates is stubbed - this is about the gates
    and the order they are asked in, and the arithmetic they use has its own tests above. The
    sizing is taken through `cells_across_for` (a part with no wetted area measured), which is the
    branch that has no cell-limit refusal of its own, so what the archive gate does is not hidden
    behind the one before it.

    Returns (what drive returned, how many times the mesher was started).
    """
    import asyncio

    from meshpipeline.cad import fluid_volume as FV
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    cad = tmp_path / "part.step"
    cad.write_text("")
    lo, hi = [0.0, 0.0, 0.0], [0.17, 0.17, 0.01]
    monkeypatch.setattr(D, "find_source", lambda ws, source_path="": cad)
    monkeypatch.setattr(D, "skin_bounds", lambda ws: (lo, hi))
    monkeypatch.setattr(D, "cad_scale", lambda src: 1.0)
    monkeypatch.setattr(D, "coolant_sits_inside", lambda rec, a, b: "")
    monkeypatch.setattr(FV, "surface_bounded", lambda *a, **k: {
        "narrowest_m": 1.0e-4, "wetted_area_m2": 0.0, "volume_m3": 5.4e-5, "passages": 2,
        "bbox_min_m": lo, "bbox_max_m": hi})
    monkeypatch.setattr(D.B, "cells_across_for",
                        lambda **k: (4, int(estimate), "priced for this test"))

    started: list = []

    def spy(ws, *, job_id=""):
        started.append(job_id)
        return {"rc": 0}

    monkeypatch.setattr(D, "_run_off_the_wall", spy)
    got = asyncio.run(D.drive(tmp_path, {}, job_id="t", publish=None, run=_Run()))
    return got, len(started)


def test_the_driver_refuses_before_it_meshes(tmp_path, monkeypatch):
    """THE WHOLE POINT, EXECUTED. A mesh the extractor would not take back is refused with the
    mesher never started - which is the only thing the refusal is for, and is what this used to
    assert by reading the source text of `drive` and comparing string offsets. That form broke the
    moment the mesher call moved into a helper, while never having run a line of the path it was
    about.

    102,630,992 cells: past what `result_extraction_limits` will take back on this deployment's
    cell budget, and inside `refuse_for_octree`'s 178,956,970 per process - so the refusal under
    test is the archive one and not one of the gates around it.
    """
    # THE CELL LIMIT THIS ROW'S NUMBERS WERE CHOSEN AGAINST, pinned rather than inherited.
    # polcfg.CELL_HARD_LIMIT is read at call time from the deployment's own configuration
    # (settings/env.py loads the repo .env into the process), and the sizes below only mean what
    # this row says they mean relative to a particular limit. A deployment configured for large
    # meshes - which is the supported way to mesh a real customer part - silently turned the
    # comparison round and the row went red for being configured, not for being wrong.
    import meshpipeline.settings.policy as _polcfg

    monkeypatch.setattr(_polcfg, "CELL_HARD_LIMIT", 8_000_000)

    from meshpipeline.engines.cfmesh_conjugate import budget as _B
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    assert _B.refuse_for_octree(102_630_992, ranks=1) == "", (
        "this size has to clear the octree ceiling or the wrong gate is being tested")

    (ok, marker, outcome), started = _drive_a_part_of(
        monkeypatch, tmp_path, estimate=102_630_992)

    assert ok is False
    assert marker == D.TOO_FINE, "a size refusal files itself as a size refusal"
    assert outcome.produced_deliverable is False
    assert started == 0, "the mesher was started on a mesh that cannot come home"


def test_the_same_part_inside_the_ceiling_does_reach_the_mesher(tmp_path, monkeypatch):
    """THE CONTROL. Without it the test above passes for any part at all - including one the
    driver refuses for some other reason, or one it never gets to the gates for."""
    (ok, _marker, _outcome), started = _drive_a_part_of(
        monkeypatch, tmp_path, estimate=6_500_000)

    assert started == 1, "a mesh that can come home has to be meshed"
    assert ok is True


def test_it_reads_the_ceiling_the_caller_will_apply():
    """A second copy of 512 MiB in this engine is one that drifts from the extractor that enforces
    it - and worse, 512 MiB is not the ceiling the caller applies at all. A returned mesh is
    extracted under `result_extraction_limits`, which sizes the archive from the cell budget and
    from the mesh's own cell count."""
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D.drive)
    assert "result_extraction_limits" in src
    assert "default_limits" not in src, "the generic ceiling is a floor, not the bound"
    assert "536870912" not in src


def test_the_ceiling_it_uses_is_the_one_the_extractor_gives(monkeypatch):
    """Apples to apples: the estimate is handed over as the cell count, because that is the number
    the extractor will be given too."""
    # THE CELL LIMIT THIS ROW'S NUMBERS WERE CHOSEN AGAINST, pinned rather than inherited.
    # polcfg.CELL_HARD_LIMIT is read at call time from the deployment's own configuration
    # (settings/env.py loads the repo .env into the process), and the sizes below only mean what
    # this row says they mean relative to a particular limit. A deployment configured for large
    # meshes - which is the supported way to mesh a real customer part - silently turned the
    # comparison round and the row went red for being configured, not for being wrong.
    import meshpipeline.settings.policy as _polcfg

    monkeypatch.setattr(_polcfg, "CELL_HARD_LIMIT", 8_000_000)

    from meshpipeline.adapters.mesh_execution.cloud_run_client import result_extraction_limits

    for cells in (6_707_503, 28_444_758):
        cap = result_extraction_limits({"rc": 0, "quality": {"cells": cells}}).max_archive_bytes
        assert B.refuse_for_return(cells, cap_bytes=cap) == "", (
            f"{cells:,} came home under that ceiling")
    for cells in (102_630_992, 336_666_582):
        cap = result_extraction_limits({"rc": 0, "quality": {"cells": cells}}).max_archive_bytes
        assert B.refuse_for_return(cells, cap_bytes=cap), f"{cells:,} cannot"
