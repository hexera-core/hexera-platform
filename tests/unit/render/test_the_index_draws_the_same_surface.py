"""The surface drawn from the boundary index is the surface drawn from the polyMesh.

`render.index_surface.surface_from_index` exists for one reason: `boundary_surface` reads the whole
polyMesh and this mesh is 277,233,795 faces on one side and 752,277,309 on the other. That makes it
a COST change, and the only thing that keeps it a cost change rather than a second opinion about
the mesh is that the two agree byte for byte. This is where that is held.

Byte for byte and not approximately: `points` and `polys` are raw buffers the browser reinterprets
as float32 and uint32, so a renumbering that is correct but different would draw the same picture
and still break `render.boundary_quality`, which numbers its per-face arrays against the index. The
degenerate-face case is in here because the two paths disagree about it by design - the index keeps
such a face in its NUMBERING so a face id stays the boundary file's own, and the drawer drops it -
so `face_count` is the one place that convention could leak.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import boundary_surface
from meshpipeline.render.boundary_index import write_index
from meshpipeline.render.index_surface import _ragged_arange, surface_from_index
from tests.foam_fixtures import write_row_of_hexes

CASES = [
    pytest.param({}, id="clean"),
    pytest.param({"degenerate_wall_face": True}, id="degenerate-wall-face"),
    pytest.param({"shear_last_y": 0.3, "last_cell_length": 2.0}, id="sheared-and-stretched"),
]


def _both(tmp_path: Path, **kw):
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm, **kw)
    write_index(pm)
    return boundary_surface(pm), surface_from_index(pm)


@pytest.mark.parametrize("kw", CASES)
def test_every_patch_is_the_same_bytes_from_either_path(tmp_path, kw):
    (want, want_stats), (got, got_stats) = _both(tmp_path, **kw)
    assert [p["name"] for p in got] == [p["name"] for p in want]
    for a, b in zip(want, got):
        assert a["name"] == b["name"]
        assert a["type"] == b["type"], a["name"]
        assert a["face_count"] == b["face_count"], a["name"]
        assert a["points"] == b["points"], f"{a['name']}: points differ byte for byte"
        assert a["polys"] == b["polys"], f"{a['name']}: polys differ byte for byte"
        assert a["area_m2"] == pytest.approx(b["area_m2"], rel=1e-12), a["name"]
    assert want_stats is not None and got_stats is not None
    for k in ("p10", "typ"):
        assert want_stats[k] == pytest.approx(got_stats[k], rel=1e-12), k


def test_the_index_answers_with_the_polymesh_arrays_gone(tmp_path):
    """The point of the sidecar: it is the answer, not a cache of a file that must still be there.

    The viewer payload is built in a worker that may not be the machine the mesh is on, and the
    detail window already allows a mesh that is absent altogether. If this needed `points` or
    `faces` back it would be an index of nothing.
    """
    (want, _), _ = _both(tmp_path)
    pm = tmp_path / "constant" / "polyMesh"
    for name in ("points", "faces", "owner", "neighbour"):
        (pm / name).unlink()
    got, stats = surface_from_index(pm)
    assert [p["name"] for p in got] == [p["name"] for p in want]
    for a, b in zip(want, got):
        assert a["points"] == b["points"] and a["polys"] == b["polys"], a["name"]
    assert stats is not None


def test_a_skipped_patch_is_not_drawn_and_nothing_else_moves(tmp_path):
    (want, _), _ = _both(tmp_path)
    kept = [p["name"] for p in want if p["name"] != "wall"]
    got, _ = surface_from_index(tmp_path / "constant" / "polyMesh", skip_names=("wall",))
    assert [p["name"] for p in got] == kept
    by_name = {p["name"]: p for p in want}
    for p in got:
        assert p["points"] == by_name[p["name"]]["points"]
        assert p["polys"] == by_name[p["name"]]["polys"]


def test_the_region_prefix_is_applied_once_or_not_at_all(tmp_path):
    """`conjugate_viewer` prefixes the names itself, so it must pass no region. A caller that
    passes one gets it exactly once - never "fluid/fluid/wall"."""
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    write_index(pm)
    bare, _ = surface_from_index(pm)
    pref, _ = surface_from_index(pm, region="fluid")
    assert [p["name"] for p in pref] == [f"fluid/{p['name']}" for p in bare]


def test_the_ragged_gather_is_the_loop_it_replaced(tmp_path):
    """The one piece of arithmetic in the module that is not a slice.

    It replaced `concatenate([arange(offs[i], offs[i + 1]) for i in keep])`, which is the same
    per-face comprehension the module's docstring disqualifies `BoundaryIndex.pack` for - measured
    at 2.397 us a face against pack's 2.361 - and which one degenerate face anywhere in a 31 M-face
    patch was enough to trigger for the whole patch.
    """
    import numpy as np

    rng = np.random.default_rng(11)
    for _ in range(8):
        lengths = rng.integers(1, 9, size=int(rng.integers(1, 40))).astype(np.int64)
        starts = (np.cumsum(np.concatenate(([0], lengths[:-1]))) + 17).astype(np.int64)
        want = np.concatenate([np.arange(s, s + n) for s, n in zip(starts, lengths)])
        assert np.array_equal(want, _ragged_arange(starts, lengths))
    assert _ragged_arange([], []).size == 0


def test_there_is_no_per_face_python_on_any_path():
    """A comprehension over faces is the thing this module exists to avoid, so its absence is
    asserted rather than left to review. `_areas` loops over the CORNER COUNTS present - at most a
    handful on a cartesian boundary - and over blocks, never over faces."""
    src = Path(__import__("meshpipeline.render.index_surface", fromlist=["x"]).__file__).read_text()
    body = src.split('__all__')[0]
    for bad in ("for i in keep", "for i in part", "for i in sel", "for f in range"):
        assert bad not in body, f"a per-face loop is back: {bad}"
