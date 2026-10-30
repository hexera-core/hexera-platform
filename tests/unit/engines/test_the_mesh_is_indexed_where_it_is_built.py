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

from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.render import boundary_index as B
from meshpipeline.render import detail_window as D
from tests.foam_fixtures import write_row_of_hexes


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


def test_the_report_carries_it_under_its_own_key(two_regions, monkeypatch, tmp_path):
    """So a reader of a finished run can tell whether its detail window will work, without
    opening the workspace."""
    import inspect

    src = inspect.getsource(N)
    assert 'body["boundary_index"] = _boundary_indexes(ws)' in src
    # and it is written to the report AFTER being filled, not before
    after = src.split('body["boundary_index"] = _boundary_indexes(ws)', 1)[1]
    assert after.lstrip().startswith("_write_report(ws, body)"), after[:120]
