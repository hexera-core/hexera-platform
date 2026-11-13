"""The boundary index is written by the mesher, or the detail window is dead code.

`render.boundary_index` makes `detail_window` work on a boundary of any size, and it does that by
writing a sidecar once. Until this, NOTHING IN src/ CALLED `write_index`: the reader fell back to
the parse on every mesh, and the parse refuses anything over MAX_SOURCE_FACES, 2,000,000 faces.
The customer plate delivers 13,181,685 on the coolant side, so every one of its detail requests
still refused while the mechanism that would answer them sat one call away.

These hold the call site to what the aids beside it promise: it runs for every delivered region,
it records what happened per region including when nothing did, and it cannot fail the run.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.foam_fixtures import write_row_of_hexes

from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.render import boundary_index as B
from meshpipeline.render import detail_window as D


@pytest.fixture
def two_regions(tmp_path):
    """The shape a conjugate delivery has: named regions, and the pre-split block still at the
    root because splitMeshRegions leaves it there."""
    for region in ("fluid", "solid"):
        write_row_of_hexes(tmp_path / "constant" / region / "polyMesh")
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    return tmp_path


def test_every_delivered_region_is_indexed(two_regions):
    out = N._boundary_indexes(two_regions)
    assert sorted(out["regions"]) == ["fluid", "solid"]
    for name, r in out["regions"].items():
        assert r["ok"] is True, (name, r)
        assert r["faces"] == 14
        assert r["bytes"] > 0 and r["buckets"] >= 1
        assert isinstance(r["seconds"], float)
        assert B.has_index(two_regions / "constant" / name / "polyMesh")


def test_the_presplit_block_is_not_indexed(two_regions):
    """It carries both regions' face counts ADDED TOGETHER and was never delivered, so an index
    of it would answer detail requests with a surface nobody was given. `delivered_regions` is
    the function that knows this, which is why the set written is taken from it."""
    N._boundary_indexes(two_regions)
    assert not B.has_index(two_regions / "constant" / "polyMesh")


def test_the_index_is_the_one_the_detail_window_then_uses(two_regions):
    """The point of the whole thing, end to end: after the mesher runs, a cut comes back from the
    index rather than from the parse - which is what lets the cap stop applying."""
    N._boundary_indexes(two_regions)
    cut = D.detail_at(two_regions, centre=(0.5, 0.5, 0.5), radius=1.0)
    assert cut["faces_in_range"] > 0
    assert [q["name"] for q in cut["patches"]]


def test_a_region_whose_index_fails_is_named_and_the_others_still_get_one(two_regions,
                                                                         monkeypatch):
    """A region with no index is a region whose detail window will refuse. A reader of the report
    should be able to see which one, and why, rather than an absence."""
    real = B.write_index

    def fail_on_solid(polymesh, *, region="", **kw):
        if region == "solid":
            raise RuntimeError("the solid boundary could not be read")
        return real(polymesh, region=region, **kw)

    monkeypatch.setattr(N, "_boundary_indexes", N._boundary_indexes)
    monkeypatch.setattr("meshpipeline.render.boundary_index.write_index", fail_on_solid)

    out = N._boundary_indexes(two_regions)
    assert out["regions"]["fluid"]["ok"] is True
    assert out["regions"]["solid"]["ok"] is False
    assert "could not be read" in out["regions"]["solid"]["why"]
    assert B.has_index(two_regions / "constant" / "fluid" / "polyMesh")


def test_a_boundary_over_the_ceiling_is_skipped_and_says_so(two_regions, monkeypatch):
    monkeypatch.setattr(N, "INDEX_SKIP_ABOVE_FACES", 3)
    out = N._boundary_indexes(two_regions)
    for name, r in out["regions"].items():
        assert r["ok"] is False, name
        assert "over the 3" in r["why"], r
    assert not B.has_index(two_regions / "constant" / "fluid" / "polyMesh")


def test_a_workspace_with_nothing_delivered_says_so_rather_than_failing(tmp_path):
    out = N._boundary_indexes(tmp_path)
    assert out["regions"] == {}
    assert "no delivered region" in out["why"]


def test_it_cannot_fail_the_run(two_regions, monkeypatch):
    """The rule the aids beside it follow: a mesh is never lost over a way of looking at it."""
    def boom(_ws):
        raise OSError("the filesystem went away")

    monkeypatch.setattr(D, "delivered_regions", boom)
    out = N._boundary_indexes(two_regions)          # must not raise
    assert out["regions"] == {}
    assert "could not be listed" in out["why"]


def test_the_quality_is_reduced_beside_the_index_and_the_raw_fields_cleared(two_regions):
    """checkMesh's fields are an intermediate of this step: 116 MB for a 5.69 M-cell region
    against 158 MB once reduced to the boundary, and nothing else reads them. They go as soon as
    they have been read, so they never travel with the workspace."""
    from tests.foam_fixtures import write_quality_fields

    from meshpipeline.render import boundary_quality as Q

    for name in ("fluid", "solid"):
        pm = two_regions / "constant" / name / "polyMesh"
        write_quality_fields(pm.parent, cells=[1.0, 2.0, 3.0],
                             per_face={"skewness": {"inlet": [0.1], "outlet": [0.2],
                                                    "wall": [0.3] * 12}})
    before = sum(p.stat().st_size for p in (two_regions / "constant" / "fluid").glob("*")
                 if p.is_file())
    assert before > 0

    out = N._boundary_indexes(two_regions)
    for name, r in out["regions"].items():
        assert r["quality"]["ok"] is True, (name, r["quality"])
        assert r["quality"]["metrics"] == ["aspect_ratio", "non_ortho", "skewness"]
        assert r["quality"]["basis"]["skewness"] == "patch"
        assert r["quality"]["fields_cleared_bytes"] > 0
        pm = two_regions / "constant" / name / "polyMesh"
        assert Q.has_quality(pm)
        for fname in Q.FIELD_FILES.values():
            assert not (pm.parent / fname).exists(), f"{name}/{fname} was left behind"


def test_losing_the_quality_does_not_lose_the_detail_window(two_regions):
    """No checkMesh fields at all. The index is still written, the cut still comes back, and the
    report says plainly that there are no colours rather than leaving an absence."""
    out = N._boundary_indexes(two_regions)          # no fields were ever written
    for name, r in out["regions"].items():
        assert r["ok"] is True, name
        assert r["quality"]["ok"] is False
        assert "checkMesh wrote none of" in r["quality"]["why"]
    cut = D.detail_at(two_regions, centre=(0.5, 0.5, 0.5), radius=1.0)
    assert cut["faces_in_range"] > 0
    assert all(q["quality"] is None for q in cut["patches"])


def test_a_failed_reduce_leaves_the_fields_as_the_evidence(two_regions, monkeypatch):
    from tests.foam_fixtures import write_quality_fields

    from meshpipeline.render import boundary_quality as Q

    pm = two_regions / "constant" / "fluid" / "polyMesh"
    write_quality_fields(pm.parent, cells=[1.0, 2.0, 3.0],
                         per_face={"skewness": {"wall": [0.3] * 12}})

    def boom(*_a, **_k):
        raise RuntimeError("the reduce went wrong")

    monkeypatch.setattr("meshpipeline.render.boundary_quality.write_quality", boom)
    out = N._boundary_indexes(two_regions)
    assert out["regions"]["fluid"]["ok"] is True, "the index still lands"
    assert out["regions"]["fluid"]["quality"]["ok"] is False
    assert (pm.parent / Q.FIELD_FILES["skewness"]).is_file(), "the evidence was cleared away"


def test_checkmesh_is_asked_to_write_the_fields_in_the_first_place(two_regions):
    """The reduce has nothing to read unless the run that measured the mesh wrote it down. +15.1%
    on that call, measured, with a byte-identical verdict."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import conjugate as C
    from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE

    assert "write_fields=True" in inspect.getsource(C), "the per-region call does not ask for them"
    src = inspect.getsource(FE.check_mesh)
    assert "-writeFields" in src and "CHECKMESH_WRITE_FIELDS" in src
    assert "write_fields: bool = False" in inspect.getsource(FE), "it must be opt-in"


def test_the_report_carries_it_under_its_own_key(two_regions, monkeypatch):
    """So a reader of a finished run can tell whether its detail window will work, without
    opening the workspace.

    EXERCISED, NOT GREPPED. This asserted the literal source text of the call
    (`body["boundary_index"] = _boundary_indexes(ws)`) and of the `_write_report` line after it,
    which said nothing about whether the key reaches the file - and broke the moment the call grew
    an argument. Now the real runner is driven with the mesher faked out, and the report is read
    off disk.
    """
    import json

    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    _a_plan(two_regions)
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.native.build",
        lambda ws, **k: _a_report())
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.solver_case.write_solver_case",
        lambda *a, **k: [])
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.native._review_aids",
        lambda ws, **k: {"volume": None, "surface": None, "groups": {}, "bbox": None, "why": "x"})

    result = N._run_conjugate_local(two_regions, bashrc="/dev/null", timeout=600)
    assert result["rc"] == 0, result
    body = json.loads((two_regions / PL.REPORT_NAME).read_text())
    assert sorted(body["boundary_index"]["regions"]) == ["fluid", "solid"]
    assert all(r["ok"] is True for r in body["boundary_index"]["regions"].values())


def _a_plan(ws: Path) -> None:
    """The smallest plan `native._run_conjugate_local` will accept."""
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    PL.write_plan(ws, PL.ConjugatePlan(
        background_cell_m=1e-3, levels=1, expect_fluid_volume_m3=1.0, expect_fluid_pieces=1,
        volume_tol=0.2, narrowest_m=1e-4))


def _a_report():
    """What `conjugate.build` hands back on a clean two-region run, with nothing in it this file
    is about."""
    from meshpipeline.engines.cfmesh_conjugate.conjugate import (
        ConjugateReport,
        RegionReport,
    )

    fluid = RegionReport(name="fluid", cells=10, volume_m3=1.0, pieces=1, mesh_ok=True)
    solid = RegionReport(name="solid", cells=20, volume_m3=2.0, pieces=1, mesh_ok=True)
    return ConjugateReport(fluid=fluid, solid=solid)


# #
# and it is paid for out of the run's own budget
# #
def test_a_region_whose_index_the_budget_cannot_afford_is_not_started(two_regions, monkeypatch):
    """THE PLACE NOTHING WAS COUNTING. `write_index` is one pass with no deadline of its own, so
    the only honest way to bound it is not to start one that cannot finish - and until this it
    ran with no budget at all, between the engine's deadline and the VM's return margin, counted
    by neither. Measured at 105 s and 126 s for 13.2 M and 13.4 M faces, which is about 15 minutes
    at the customer plate's 71.9 M."""
    started: list = []
    monkeypatch.setattr("meshpipeline.render.boundary_index.write_index",
                        lambda *a, **k: started.append(a) or {})
    out = N._boundary_indexes(two_regions, budget_left_s=0.0)
    assert started == [], "a region was indexed out of a budget that had nothing left"
    for name, r in out["regions"].items():
        assert r["ok"] is False, (name, r)
        assert "price at about" in r["why"] and "budget has" in r["why"]


def test_a_budget_that_covers_it_still_builds_it(two_regions):
    out = N._boundary_indexes(two_regions, budget_left_s=3_600.0)
    assert sorted(out["regions"]) == ["fluid", "solid"]
    assert all(r["ok"] is True for r in out["regions"].values()), out


def test_no_budget_given_means_unbounded_by_the_run(two_regions):
    """Every caller outside the pipeline - the CLI, the devtools, these tests - keeps what it
    had."""
    out = N._boundary_indexes(two_regions)
    assert all(r["ok"] is True for r in out["regions"].values()), out


def test_the_price_is_the_measured_one_and_not_a_round_number():
    """105 s / 13,181,685 faces = 7.97 us and 126 s / 13,407,661 = 9.40 us for the index; 34.9 s
    and 59.9 s for the quality reduction beside it. The slower pair is 13.9 us a face."""
    assert 1.3e-5 < N.INDEX_SECONDS_PER_FACE < 1.5e-5
    assert N.INDEX_SECONDS_PER_FACE * 13_407_661 > 126 + 59.9
