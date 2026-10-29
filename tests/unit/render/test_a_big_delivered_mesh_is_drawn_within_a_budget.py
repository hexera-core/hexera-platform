"""A delivered boundary too big for a browser is drawn within a budget, and the payload says so.

WHY THIS EXISTS. The INPUT skin path has had a 400,000-triangle budget since it was written
(`skin_mesh.STAGE_MAX_TRIANGLES`) and simplifies anything past it. The DELIVERED polyMesh path had
no budget at all: `viewer_pack.polymesh_response` base64-packs every face it is handed. Measured on
the first real customer cold plate on 2026-10-28, that is 13,022,400 faces on the coolant side
alone - 416.8 MB raw, 555.8 MB of base64, against a 512 MiB cap on the workspace archive that
carries it. The mesh was on disk, checkMesh passed on both regions, and nothing downstream could
open it.

The reduction is for DRAWING only. What this file is really guarding is the second half of that:
that a payload which has been merged says, in the payload, how many faces were delivered and at
what resolution they are being shown. A reduced mesh that does not admit it reads to a user as a
coarser mesh than the one they paid for.
"""
import base64

import numpy as np
import pytest

from meshpipeline.render import polymesh_budget as PB
from meshpipeline.render.viewer_pack import polymesh_response


def _grid_patch(name="wall", nx=4, ny=4, h=1.0):
    """A flat nx-by-ny sheet of quads, in the (points bytes, polys bytes) form a boundary reader
    hands over: points (n,3) float32, polys as VTK's [4, a, b, c, d] stream."""
    xs = np.arange(nx + 1) * h
    ys = np.arange(ny + 1) * h
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1).astype(np.float32)

    def pid(i, j):
        return i * (ny + 1) + j

    quads = []
    for i in range(nx):
        for j in range(ny):
            quads.append([4, pid(i, j), pid(i + 1, j), pid(i + 1, j + 1), pid(i, j + 1)])
    polys = np.asarray(quads, dtype=np.uint32).reshape(-1)
    return {"name": name, "type": "wall", "face_count": nx * ny,
            "points": pts.tobytes(), "polys": polys.tobytes()}


def _faces_of(patch):
    """The patch's polys back as a list of point-id lists."""
    polys = np.frombuffer(patch["polys"], dtype=np.uint32)
    out, i = [], 0
    while i < len(polys):
        k = int(polys[i])
        out.append([int(v) for v in polys[i + 1:i + 1 + k]])
        i += k + 1
    return out


def test_a_boundary_that_already_fits_is_handed_back_untouched():
    """The ordinary case, and it must be byte-identical: most meshes are nowhere near the budget,
    and merging one that fits would throw away exact faces for nothing."""
    p = _grid_patch(nx=10, ny=10)
    out, note = PB.fit_to_budget([p], max_triangles=100_000)
    assert note is None, "nothing was merged, so the payload must not claim it was"
    assert out[0]["points"] == p["points"]
    assert out[0]["polys"] == p["polys"]
    assert out[0]["face_count"] == 100


def test_the_quads_survive_when_nothing_is_merged():
    """A cartesian boundary is quads, vtk.js draws quads, and turning them into triangles for no
    reason doubles the payload."""
    p = _grid_patch(nx=6, ny=6)
    out, _ = PB.fit_to_budget([p], max_triangles=100_000)
    assert all(len(f) == 4 for f in _faces_of(out[0]))


def test_a_boundary_past_the_budget_is_brought_under_it():
    p = _grid_patch(nx=120, ny=120)                      # 14,400 quads -> 28,800 triangles
    out, note = PB.fit_to_budget([p], max_triangles=2_000)
    assert note is not None
    assert out[0]["face_count"] <= 2_000, "the budget is the point"
    assert out[0]["face_count"] > 0, "a boundary that is not drawn reads as one that is not there"


def test_the_payload_says_what_it_is_showing():
    """The honesty requirement, stated as numbers a viewer can put on screen."""
    p = _grid_patch(nx=120, ny=120)
    out, note = PB.fit_to_budget([p], max_triangles=2_000)
    assert note["drawn_triangles"] == out[0]["face_count"]
    assert note["drawn_triangles"] < note["delivered_faces"]
    assert note["resolution_m"] > 0.0
    assert "wall" in note["patches"]
    assert note["patches"]["wall"]["delivered_faces"] == 14_400   # the patch's own faces, quads


def test_the_delivered_count_is_the_mesh_s_own_faces_not_a_triangulation_of_them():
    """THE UNITS HAVE TO MATCH. The first version of this summed the fan triangulation into
    `delivered_faces`, so a 14,400-quad patch reported 28,800 delivered - the mesh's own
    measurement of itself, doubled, in a field a user reads as the size of their mesh."""
    p = _grid_patch(nx=120, ny=120)
    out, note = PB.fit_to_budget([p], max_triangles=2_000)
    assert note["delivered_faces"] == 14_400, "quads delivered, not the triangles drawn from them"
    assert note["delivered_faces"] == sum(
        v["delivered_faces"] for v in note["patches"].values()), (
        "the headline has to be the sum of the per-patch numbers, in the same unit")


def test_what_comes_out_is_drawable():
    """Every face a triangle, every id inside the points it ships with. A payload that indexes past
    its own points does not render, it throws in the browser."""
    p = _grid_patch(nx=120, ny=120)
    out, _ = PB.fit_to_budget([p], max_triangles=2_000)
    pts = np.frombuffer(out[0]["points"], dtype=np.float32).reshape(-1, 3)
    faces = _faces_of(out[0])
    assert faces and all(len(f) == 3 for f in faces)
    flat = np.asarray([i for f in faces for i in f])
    assert flat.min() >= 0 and flat.max() < len(pts)
    assert len(faces) == out[0]["face_count"], "face_count has to be what is in the polys"


def test_the_part_is_still_where_it_was():
    """Clustering moves points to the centre of their cell, so the outline may shrink by up to one
    cluster size - and must not move by more than that, or the viewer is drawing a different part."""
    p = _grid_patch(nx=120, ny=120, h=0.001)
    out, note = PB.fit_to_budget([p], max_triangles=2_000)
    before = np.frombuffer(p["points"], dtype=np.float32).reshape(-1, 3)
    after = np.frombuffer(out[0]["points"], dtype=np.float32).reshape(-1, 3)
    slack = note["resolution_m"]
    assert np.all(after.min(axis=0) >= before.min(axis=0) - slack)
    assert np.all(after.max(axis=0) <= before.max(axis=0) + slack)
    assert np.all(after.max(axis=0) >= before.max(axis=0) - 2 * slack), (
        "the part must not have collapsed to a corner")


def test_a_small_patch_is_not_annihilated_by_a_big_neighbour():
    """THE SHAPE A CONJUGATE SPLIT ACTUALLY HAS: one enormous interface and a handful of small
    block sides. Shared out by face count alone the sides get a budget of about nothing, and the
    block stops being drawn at all."""
    big = _grid_patch(name="fluid/fluid_to_solid", nx=200, ny=200)
    small = _grid_patch(name="solid/xmin", nx=3, ny=3)
    out, note = PB.fit_to_budget([big, small], max_triangles=4_000)
    by_name = {p["name"]: p for p in out}
    assert by_name["solid/xmin"]["face_count"] > 0
    assert by_name["solid/xmin"]["polys"] == small["polys"], (
        "a patch under the floor should not be touched at all")
    assert by_name["fluid/fluid_to_solid"]["face_count"] <= 4_000


def test_the_reduction_reaches_the_payload_the_browser_reads():
    """fit_to_budget can be as honest as it likes; if polymesh_response drops the block on the
    floor the browser still has no way to say what it is showing."""
    p = _grid_patch(nx=120, ny=120)
    out, note = PB.fit_to_budget([p], max_triangles=2_000)
    resp = polymesh_response(out, {"p10": 1.0, "typ": 2.0}, {}, "m", reduced=note)
    assert resp["reduced"] == note
    assert resp["patches"][0]["face_count"] == out[0]["face_count"]
    # and the bytes are the reduced ones, not the originals
    assert base64.b64decode(resp["patches"][0]["polys_b64"]) == out[0]["polys"]


def test_an_unreduced_payload_never_claims_to_be_reduced():
    """The other half of the same guarantee."""
    p = _grid_patch(nx=6, ny=6)
    out, note = PB.fit_to_budget([p], max_triangles=100_000)
    resp = polymesh_response(out, None, {}, "m", reduced=note)
    assert "reduced" not in resp


def test_an_empty_boundary_is_survived():
    assert PB.fit_to_budget([]) == ([], None)


@pytest.mark.parametrize("n", [5, 6])
def test_an_n_gon_is_triangulated_rather_than_dropped(n):
    """cfMesh's octree leaves pentagons and hexagons where it grades - 11,567 and 200 of them on
    the customer plate. Dropping them would open holes in the surface exactly where the mesh
    changes size, which is where a reader is looking."""
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = np.stack([np.cos(ang), np.sin(ang), np.zeros(n)], axis=1).astype(np.float32)
    polys = np.asarray([n, *range(n)], dtype=np.uint32)
    tris = PB._triangles(polys)
    assert len(tris) == n - 2
    assert set(tris.reshape(-1).tolist()) == set(range(n))
