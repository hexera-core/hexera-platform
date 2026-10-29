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

#: What the coupon actually did: ~6.5M cells, 570 MB compressed, against a 512 MiB ceiling.
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


def test_the_refusal_names_the_setting_and_not_the_part():
    """It is a configuration to raise, not a geometry to redraw, and a customer told otherwise
    goes and edits their CAD for nothing."""
    said = B.refuse_for_return(COUPON_CELLS, cap_bytes=OLD_CAP)
    assert "WORKSPACE_ARCHIVE_MAX_BYTES" in said
    assert "WORKSPACE_ARCHIVE_MAX_TOTAL_BYTES" in said
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

def test_the_driver_refuses_before_it_meshes():
    """THE WHOLE POINT. Checked beside the cell limit and the octree ceiling, all three before the
    first stage runs."""
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D.drive)
    assert "refuse_for_return" in src
    assert "max_archive_bytes" in src, "the real ceiling, read off the extractor's own limits"
    # ...after the octree check, which is the one that does not care what machine was chosen
    assert src.index("refuse_for_octree") < src.index("refuse_for_return")
    # ...and before the mesher is started
    assert src.index("refuse_for_return") < src.index("run_cartesian_mesh")


def test_it_reads_the_extractor_s_own_limit_rather_than_a_copy():
    """A second copy of 512 MiB in this engine is one that drifts from the extractor that enforces
    it, and the extractor's is a setting."""
    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D.drive)
    assert "safe_extract" in src
    assert "536870912" not in src and "512" not in src.split("refuse_for_return")[0][-400:]
