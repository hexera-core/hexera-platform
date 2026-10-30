"""A boundary index answers the same question the whole-boundary parse does, at a window's cost.

`detail_window` refuses a boundary over MAX_SOURCE_FACES (2,000,000) and says why: "The read is
the whole boundary - points, the face stream and the patch table - because a face's position is
not known until its points are, so there is no way to seek to a neighbourhood." The customer
plate's 182,269,914-cell mesh (ws_par247) has 13,181,685 boundary faces on the fluid side, so the
detail window is not available on the mesh it was written for.

`render.boundary_index` removes the premise: it works out where every face is ONCE, in the worker,
and writes it down. The tests here hold it to the only standard that matters - the index's answer
must be the SAME SET the existing parse returns, face for face, on the same mesh and the same ball.
An index that is fast and disagrees is worse than no index.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.render import boundary_index as B
from meshpipeline.render import detail_window as D
from tests.foam_fixtures import write_row_of_hexes

#: The middle of the first cell. A ball here reaches the inlet and some wall faces, never the
#: outlet at x=3 - the same point the detail-window tests use, so the two suites talk about one
#: place in one mesh.
NEAR_INLET = (0.5, 0.5, 0.5)


@pytest.fixture
def mesh(tmp_path):
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    return tmp_path


@pytest.fixture
def polymesh(mesh):
    return mesh / "constant" / "polyMesh"


def _counts_from_detail(workspace, *, centre, radius) -> dict[str, int]:
    """What the authoritative path says is in range, per patch."""
    out = D.detail_at(workspace, centre=centre, radius=radius)
    got: dict[str, int] = {}
    for p in out["patches"]:
        # the region prefix is the detail route's naming; a single-region mesh has none
        got[str(p["name"]).split("/")[-1]] = int(p["face_count"])
    return got


def _counts_from_index(polymesh, *, centre, radius) -> dict[str, int]:
    """What the index says is in range, per patch."""
    idx = B.open_index(polymesh)
    ids = idx.faces_near(centre, radius)
    return {str(p["name"]): int(n.size) for p, n in idx.patch_slices(ids)}


def test_the_index_is_written_beside_the_mesh_it_describes(polymesh):
    """A mesh and its index travel together or not at all."""
    meta = B.write_index(polymesh)
    d = B.index_dir(polymesh)
    assert d.is_dir() and d.parent == polymesh
    assert meta["version"] == B.INDEX_VERSION
    assert (d / "meta.json").is_file()
    on_disk = json.loads((d / "meta.json").read_text())
    assert on_disk["n_faces"] == meta["n_faces"]


def test_every_boundary_face_is_indexed_and_numbered_as_the_boundary_file_numbers_it(polymesh):
    """14 boundary faces: inlet 1, outlet 1, wall 12, and the face ids are the file's own."""
    meta = B.write_index(polymesh)
    assert meta["n_faces"] == 14
    assert {p["name"]: p["n_faces"] for p in meta["patches"]} == {
        "inlet": 1, "outlet": 1, "wall": 12}
    idx = B.open_index(polymesh)
    # every id the index can return is inside the boundary it declares
    everything = idx.faces_near((1.5, 0.5, 0.5), 99.0)
    assert everything.min() >= 0 and everything.max() < meta["n_faces"]


@pytest.mark.parametrize("radius", [0.4, 0.7, 1.0, 1.6, 3.0, 10.0])
def test_the_index_returns_the_same_faces_the_whole_boundary_parse_returns(mesh, polymesh, radius):
    """THE ONLY STANDARD THAT MATTERS. Same mesh, same ball, same set - per patch, not in total,
    because a total can agree while the metal side is missing and the coolant side doubled."""
    B.write_index(polymesh)
    assert _counts_from_index(polymesh, centre=NEAR_INLET, radius=radius) == \
        _counts_from_detail(mesh, centre=NEAR_INLET, radius=radius)


@pytest.mark.parametrize("centre", [(0.5, 0.5, 0.5), (1.5, 0.5, 0.5), (3.0, 1.0, 1.0),
                                    (0.0, 0.0, 0.0), (-5.0, 0.0, 0.0)])
def test_they_agree_wherever_the_ball_is_put_including_outside_the_mesh(mesh, polymesh, centre):
    """A point out in space brings back nothing from both, rather than the nearest thing."""
    B.write_index(polymesh)
    assert _counts_from_index(polymesh, centre=centre, radius=0.8) == \
        _counts_from_detail(mesh, centre=centre, radius=0.8)


def test_the_packed_buffers_are_the_pair_the_viewer_already_draws(polymesh):
    """float32 xyz, then uint32 [corner count, local ids...] per face - so a client renders a
    detail with the code it has. A buffer that needs new client code is not this."""
    B.write_index(polymesh)
    idx = B.open_index(polymesh)
    ids = idx.faces_near(NEAR_INLET, 1.0)
    pts, polys = idx.pack(ids)
    assert len(pts) % 12 == 0, "points are three float32 each"
    p = np.frombuffer(polys, dtype=np.uint32)
    # walk the stream the way the client does: a count, then that many ids
    seen, at = 0, 0
    while at < p.size:
        k = int(p[at])
        assert 3 <= k <= 255
        assert p[at + 1:at + 1 + k].max() < len(pts) // 12, "an id past its own points"
        at += k + 1
        seen += 1
    assert seen == ids.size


def test_a_degenerate_face_is_kept_in_the_numbering_and_left_out_of_the_answer(tmp_path):
    """The surface reader drops a face with under three corners; so must this, and WITHOUT
    renumbering - a face id has to stay the boundary file's own or two readers of one mesh
    disagree about which face is which."""
    pm = tmp_path / "constant" / "polyMesh"
    info = write_row_of_hexes(pm, degenerate_wall_face=True)
    meta = B.write_index(pm)
    assert meta["n_faces"] == 2 + info["n_wall"], "the degenerate face is counted in the boundary"
    idx = B.open_index(pm)
    ids = idx.faces_near((1.5, 0.5, 0.5), 99.0)
    assert ids.size == meta["n_faces"] - 1, "everything but the degenerate face"
    # and it agrees with the authority, which drops it the same way
    assert _counts_from_index(pm, centre=NEAR_INLET, radius=1.0) == \
        _counts_from_detail(tmp_path, centre=NEAR_INLET, radius=1.0)


def test_a_rebuilt_mesh_is_not_answered_from_the_old_index(polymesh):
    """A workspace rebuilt under one path would otherwise be answered from a surface that no
    longer exists. Same guard `detail_window._parsed` puts in its cache key, same reason."""
    B.write_index(polymesh)
    assert B.has_index(polymesh)
    write_row_of_hexes(polymesh, last_cell_length=2.0)   # same path, different mesh
    assert not B.has_index(polymesh)
    with pytest.raises(B.IndexUnavailable, match="changed since this index was written"):
        B.open_index(polymesh)


def test_an_index_from_a_version_this_does_not_know_is_refused_not_guessed(polymesh):
    B.write_index(polymesh)
    d = B.index_dir(polymesh)
    meta = json.loads((d / "meta.json").read_text())
    meta["version"] = B.INDEX_VERSION + 1
    (d / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(B.IndexUnavailable, match="index version"):
        B.open_index(polymesh)


def test_no_index_is_said_and_not_crashed_into(polymesh):
    assert not B.has_index(polymesh)
    with pytest.raises(B.IndexUnavailable, match="no boundary index"):
        B.open_index(polymesh)


def test_the_grid_holds_every_face_exactly_once(polymesh):
    """A face in two buckets is returned twice; a face in none is invisible. Both are silent."""
    meta = B.write_index(polymesh)
    d = B.index_dir(polymesh)
    order = np.frombuffer((d / "bucket_face.u32").read_bytes(), dtype=np.uint32)
    assert order.size == meta["n_faces"]
    assert sorted(order.tolist()) == list(range(meta["n_faces"]))
    starts = np.frombuffer((d / "bucket_start.i64").read_bytes(), dtype=np.int64)
    assert starts.size == meta["grid"]["n_buckets"] + 1
    assert starts[0] == 0 and starts[-1] == meta["n_faces"]
    assert (np.diff(starts) >= 0).all(), "the bucket table must not go backwards"


def test_the_cap_refuses_a_boundary_it_would_have_to_parse(mesh, monkeypatch):
    """The behaviour that is being changed, pinned first: with nothing written down, a boundary
    over the cap is refused and named. This is the state the customer plate is in today."""
    monkeypatch.setattr(D, "MAX_SOURCE_FACES", 2)
    with pytest.raises(D.DetailError, match="14 faces and this reads at most 2"):
        D.detail_at(mesh, centre=NEAR_INLET, radius=1.0)


def test_an_indexed_boundary_is_not_refused_however_big_it_is(mesh, polymesh, monkeypatch):
    """THE POINT OF ALL OF IT. The same mesh, the same cap, the same ball - but the positions are
    already worked out, so nothing is parsed and there is nothing for the cap to guard. This is
    what makes the detail window available on the 13,181,685-face coolant side of the customer
    plate, where today it refuses."""
    B.write_index(polymesh)
    monkeypatch.setattr(D, "MAX_SOURCE_FACES", 2)
    out = D.detail_at(mesh, centre=NEAR_INLET, radius=1.0)
    assert out["faces_in_range"] > 0
    assert [q["name"] for q in out["patches"]]


def test_the_route_gets_the_same_answer_with_the_index_as_without(mesh, polymesh):
    """An index is a faster way to the same answer, not a different answer. Patch for patch,
    count for count, and the drawn bytes byte for byte."""
    before = D.detail_at(mesh, centre=NEAR_INLET, radius=1.0)
    B.write_index(polymesh)
    after = D.detail_at(mesh, centre=NEAR_INLET, radius=1.0)

    assert after["faces_in_range"] == before["faces_in_range"]
    assert after["faces_returned"] == before["faces_returned"]
    assert after["truncated"] == before["truncated"]
    key = lambda cut: {q["name"]: (q["face_count"], q["faces_in_range"]) for q in cut["patches"]}
    assert key(after) == key(before)

    # THE BYTES TOO. A patch can agree on its count and hand the client a different surface: the
    # index renumbers points into its own compacted set, so if that renumbering were wrong the
    # counts would still match and the picture would be wrong.
    bb = {q["name"]: (q["points"], q["polys"]) for q in before["patches"]}
    for q in after["patches"]:
        pts_b, polys_b = bb[q["name"]]
        assert _same_surface(q["points"], q["polys"], pts_b, polys_b), q["name"]


def _same_surface(pts_a, polys_a, pts_b, polys_b) -> bool:
    """Whether two packed surfaces draw the same faces, whatever order their points are in.

    The two paths compact points independently (`np.unique` over different id spaces), so the
    buffers are not expected to be equal byte for byte. What must hold is that each face resolves
    to the same set of coordinates.
    """
    def faces(pts, polys):
        xyz = np.frombuffer(pts, dtype=np.float32).reshape(-1, 3)
        p = np.frombuffer(polys, dtype=np.uint32)
        out, at = [], 0
        while at < p.size:
            k = int(p[at])
            ids = p[at + 1:at + 1 + k]
            out.append(tuple(sorted(tuple(np.round(xyz[i], 6)) for i in ids)))
            at += k + 1
        return sorted(out)
    return faces(pts_a, polys_a) == faces(pts_b, polys_b)


def test_the_sidecar_is_a_fraction_of_the_mesh_it_indexes(polymesh):
    """The index earns its place by being small. It carries the boundary's own points, not the
    volume's: on the customer plate's fluid side that is ~13 M of 84.8 M."""
    meta = B.write_index(polymesh)
    mesh_bytes = sum(p.stat().st_size for p in polymesh.glob("*") if p.is_file())
    assert sum(meta["bytes"].values()) < mesh_bytes
    assert meta["n_points"] < 32, "only the points the 14 boundary faces touch"
