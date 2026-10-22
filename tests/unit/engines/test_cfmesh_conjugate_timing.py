"""Where a conjugate build's time actually goes.

The finding this pins: on a 100-channel part cartesianMesh took 617 s and the tagging and splitting
after it took over an hour. For a conjugate mesh the post-processing dominates by a wide margin, so
a turnaround promise that budgets only for the mesher is a promise about the wrong stage.
"""
from meshpipeline.engines.cfmesh import conjugate as C


def _rep(seconds):
    return C.ConjugateReport(
        fluid=C.RegionReport("fluid", cells=1, volume_m3=1.0, pieces=1, mesh_ok=True),
        solid=C.RegionReport("solid", cells=1, volume_m3=1.0, pieces=1, mesh_ok=True),
        interface_faces_fluid=10, interface_faces_solid=10, seconds=seconds)


def test_the_measured_corintis_split_reads_as_post_dominated():
    # The real numbers from the 100-channel run, rounded: the mesher is a tenth of the wall clock.
    r = _rep({"cartesianMesh": 617.0, "topoSet": 2400.0, "splitMeshRegions": 1200.0,
              "checkMesh:fluid": 60.0, "checkMesh:solid": 60.0})
    assert r.mesher_seconds == 617.0
    assert r.post_seconds == 3720.0
    assert r.post_share > 0.85
    assert r.post_seconds > 5 * r.mesher_seconds


def test_an_empty_timing_set_does_not_divide_by_zero():
    assert _rep({}).post_share == 0.0


def test_the_mesher_is_not_counted_as_post_processing():
    r = _rep({"cartesianMesh": 100.0})
    assert r.post_seconds == 0.0
    assert r.post_share == 0.0


def test_every_stage_is_timed_not_just_the_mesher():
    # If a stage stops being timed, the attribution silently shifts to whatever is left, which is
    # how "the mesher is slow" survives as an explanation.
    import inspect
    src = inspect.getsource(C.build)
    for stage in ("cartesianMesh", "topoSet", "splitMeshRegions", "checkMesh"):
        assert stage in src
    assert "seconds[" in src
