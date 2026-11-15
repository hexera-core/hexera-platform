"""The review surface is built from the boundary index when there is no VTK tree.

WHAT THIS COSTS TO NOT HAVE. Job 57b613d7, 2026-11-15: 283,410,955 cells, every gating criterion
passed, and the reviewer refused with "required artifact 'mesh_paths.surface' does not exist". The
direct cause was native.py calling _review_aids with refused=bool(report.problems) and is fixed
there and pinned in test_a_mesh_that_passed_its_gates_gets_its_review_aids. This file is about the
SHAPE of the failure, which is review_surface's own:

    the review surface - 2 M triangles, about 120 MB - was a downstream consequence of whether
    foamToVTK wrote a second copy of 283 million CELLS.

Those two things have nothing to do with each other. Tying them together means every reason the
export does not happen - the size skip above VOLUME_SKIP_ABOVE_CELLS, the 7,200 s EXPORT_TIMEOUT_S,
ENOSPC, a stall - silently takes the picture with it, and the review is inconclusive on a mesh that
is perfectly good and already paid for.

MEASURED ON THE DELIVERED MESH OF THAT VERY JOB, 2026-11-15, through `build_region_msh` itself:

    route              source                        wall    peak RSS   mesh.msh
    boundary index     783 MB of sidecar             47.7 s   6.02 GiB   135,313,652 B
    foamToVTK          19.5 GB of ascii polyMesh     killed   -          -

The foamToVTK row is not a measurement and is not quoted as one: it was started on this same
workspace and stopped by hand, because the host has 15.8 GiB of RAM and the fluid region's `faces`
file alone is 11 GB of ascii. What the index route produced read back through the REVIEWER'S OWN
reader (sandbox.mesh_reader.read_mesh) in 5.25 s, reporting
`[(2, 1, 'fluid', (1, 2, 3)), (2, 2, 'solid', (4,))]` - both regions, which is the thing
build_region_msh refuses to publish half of.

THE EXPORT STILL WINS WHEN IT IS THERE. A workspace with both is the ordinary successful run, the
.vtp path is the one every other fixture in this package exercises, and changing which source a
working run reads is a change nobody asked for. The index is the fallback for the case that
currently has no picture at all.
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import review_surface as RS


def _body(fn) -> str:
    """A function's source with its docstring removed.

    These tests assert what the code CALLS, and the docstrings here deliberately name
    `boundary_surface` as the shape being matched - so a plain `getsource` scan reads the prose as
    the call and fails on the very comment that explains why the call is not there.
    """
    src = inspect.getsource(fn)
    for line in (inspect.getdoc(fn) or "").splitlines():
        if line.strip():
            src = src.replace(line, "")
    return src


def _ws(tmp_path: Path, *, index_for=("fluid", "solid"), vtk_for=()) -> Path:
    """A workspace with region dirs, optionally a boundaryIndex and optionally a VTK tree.

    Nothing here is a real mesh: these tests are about WHICH SOURCE is chosen, and the readers
    themselves are stubbed in the tests that get that far.
    """
    for region in ("fluid", "solid"):
        (tmp_path / "constant" / region / "polyMesh").mkdir(parents=True, exist_ok=True)
    for region in index_for:
        (tmp_path / "constant" / region / "polyMesh" / "boundaryIndex").mkdir(exist_ok=True)
    for region in vtk_for:
        d = tmp_path / "VTK" / region / f"{region}_0" / "boundary"
        d.mkdir(parents=True, exist_ok=True)
        (d / "wall_outer.vtp").write_bytes(b"not really a vtp")
    return tmp_path


# --------------------------------------------------------------------------- the route exists


def test_the_index_is_a_way_in_at_all():
    """THE WHOLE DEFECT IN ONE ASSERTION: before this, `_patch_files` was the only source."""
    assert hasattr(RS, "_patches_from_index"), (
        "with no index route, a run that writes no volume export has no review surface")
    src = inspect.getsource(RS._patches_from_index)
    assert "surface_from_index" in src, "it has to be the sidecar reader, not a second parse"


def test_it_reads_the_sidecar_and_not_the_polymesh():
    """The point of the route is the cost. `boundary_surface` reads the whole polyMesh - the
    fluid region's `faces` file is 11 GB of ascii on the measured job - and importing it here
    would quietly reintroduce exactly what the index exists to avoid."""
    body = _body(RS._patches_from_index)
    assert "boundary_surface" not in body, (
        "the docstring may name it as the shape being matched; the CODE must not call it")
    assert "read_points" not in body
    assert "with_area=False" in body, "area is a pass over every face, and nothing here reads it"


def test_the_interface_is_left_out_of_the_solid_side_here_too():
    """`_patch_files` drops SOLID_TO_FLUID because it is the same surface as the fluid side's.
    A second source that kept it would double the interface geometry in the file."""
    src = inspect.getsource(RS._patches_from_index)
    assert "SOLID_TO_FLUID" in src


def test_the_names_are_prefixed_once():
    """`_is_interface` reads "<region>/<patch>", and `surface_from_index` does the prefixing when
    it is given `region=` - its own docstring warns that a caller prefixing as well gets
    "fluid/fluid/coolant_in"."""
    src = inspect.getsource(RS._patches_from_index)
    assert "region=region" in src
    assert 'f"{region}/' not in src, "surface_from_index already prefixes; doing it again doubles it"


# --------------------------------------------------------------------------- which source wins


def test_a_workspace_with_neither_still_converts_nothing(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=RS.logger.name)
    ws = _ws(tmp_path, index_for=())
    groups, box = RS.build_region_msh(ws)
    assert (groups, box) == ({}, None)
    assert "nothing to convert" in caplog.text


def test_a_workspace_with_an_index_and_no_export_reaches_the_index(tmp_path, monkeypatch):
    """The case the measured job was in: no VTK tree at all, both sidecars present."""
    ws = _ws(tmp_path)
    asked: list[str] = []

    def _from_index(_ws, region):
        asked.append(region)
        return []

    monkeypatch.setattr(RS, "_patches_from_index", _from_index)
    groups, box = RS.build_region_msh(ws)
    assert asked == ["fluid", "solid"], (
        "with no export and an index present, both regions must be asked of the index")
    # nothing came back, so nothing is published - but it was ASKED, which is the regression
    assert (groups, box) == ({}, None)


def test_an_export_that_exists_is_still_what_is_read(tmp_path, monkeypatch):
    """THE DELIBERATE LIMIT. A successful ordinary run has both; it must keep reading the .vtp
    tree, because that is the path every other fixture in this package exercises."""
    ws = _ws(tmp_path, vtk_for=("fluid", "solid"))
    monkeypatch.setattr(RS, "_patches_from_index",
                        lambda *a, **k: pytest.fail("the export was there and was not read"))
    monkeypatch.setattr(RS, "_read_patch", lambda *a, **k: None)
    RS.build_region_msh(ws)


def test_half_an_export_is_not_topped_up_from_the_index(tmp_path, monkeypatch):
    """A tree for one region only is still 'there is an export', and mixing one region's .vtp
    with the other's sidecar would publish a surface built two different ways without saying so.
    `build_region_msh`'s own missing-region check then refuses it, which is the honest end."""
    ws = _ws(tmp_path, vtk_for=("fluid",))
    monkeypatch.setattr(RS, "_patches_from_index",
                        lambda *a, **k: pytest.fail("one region's export is still an export"))
    monkeypatch.setattr(RS, "_read_patch", lambda *a, **k: None)
    RS.build_region_msh(ws)


# --------------------------------------------------------------------------- it is never fatal


def test_an_unreadable_sidecar_is_an_empty_list_and_not_a_raise(tmp_path, monkeypatch):
    """This module never fails a mesh. A sidecar that is absent, stale or corrupt has to come
    back the same way an unreadable .vtp does."""
    ws = _ws(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("stale change stamp")

    monkeypatch.setattr("meshpipeline.render.index_surface.surface_from_index", _boom)
    assert RS._patches_from_index(ws, "fluid") == []


def test_an_empty_patch_is_dropped(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setattr(
        "meshpipeline.render.index_surface.surface_from_index",
        lambda *a, **k: ([{"name": "fluid/a", "face_count": 0},
                          {"name": "fluid/b", "face_count": 7}], None))
    got = RS._patches_from_index(ws, "fluid")
    assert [p["name"] for p in got] == ["fluid/b"]


def test_an_index_that_yields_nothing_says_so_and_publishes_nothing(tmp_path, monkeypatch, caplog):
    ws = _ws(tmp_path)
    monkeypatch.setattr(RS, "_patches_from_index", lambda *a, **k: [])
    groups, box = RS.build_region_msh(ws)
    assert (groups, box) == ({}, None)
    assert "boundary index" in caplog.text


def test_the_dropped_region_message_does_not_say_zero_boundary_files(tmp_path, monkeypatch,
                                                                    caplog):
    """The index route always has 0 boundary files, so the old wording would report the route as
    the bug on every single index-built surface."""
    src = inspect.getsource(RS.build_region_msh)
    assert "its boundary index" in src
    assert 'f"{len(files)} boundary file(s)' not in src.replace(
        'f"{len(files)} boundary file(s): "', "")
