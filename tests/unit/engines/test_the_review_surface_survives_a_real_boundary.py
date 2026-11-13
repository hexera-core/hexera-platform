# Responsibility: Hold the conjugate review surface to a size the mesh job can actually build,
# and to a file the review can actually open, on a boundary the size a customer plate delivers.
"""WHAT WAS MEASURED, 2026-11-12, on synthetic two-region all-quad boundaries.

`review_surface.build_region_msh` triangulated with a `while` loop that appended three Python ints
per triangle for EVERY boundary face, with no budget, no reduction and no size guard, and then
handed gmsh `.tolist()` of the lot. The only size anybody had ever measured for it was 35,654,271
bytes on a small two-region case. The Corintis plate's two regions are 31,184,373 and 40,667,460
boundary faces.

    boundary faces      OLD                                NEW
     4,000,000           14.36 s  1.649 GiB  390,176,539 B   6.37 s  0.644 GiB  38,464,735 B
     7,997,584           30.56 s  3.155 GiB  792,326,654 B  11.55 s  1.039 GiB  39,311,759 B
    19,998,784          (not run: ~76 s, ~7.9 GiB)          28.74 s  2.411 GiB  38,814,719 B

Both are linear in the face count, so at 71.9 M the old shape is about 273 s, 28 GiB of peak RSS
and a 7.1 GB mesh.msh - which `sandbox/mesh_reader.read_mesh` then has to open in the review
process. `mesh_paths.surface` is required=True, so that OOM takes the mesh with it.

AND THE SIZE WAS ONLY HALF OF IT. The budget those three rows were run at was the VIEWER's
800,000, shared out by face count across the patches of both regions, which at customer size gave
the plate's fluid interface 613,400 triangles and a 254 um cluster on a part whose channels are
100.631 um on a 200 um pitch. Measured 2026-11-12 on a comb of twenty such channels at the
delivered 25.1578 um cell, through the viewer's own reducer:

    h 512 um -> 10 of 40 wall planes      h 222 um -> 32 of 40
    h 337 um -> 18 of 40                  h 200 um -> 38 of 40
    h 254 um -> 22 of 40                  h 162 um -> 40 of 40

So the feature a semiconductor reviewer opens the surface to CHECK was being clustered away, and a
file bounded at a size that cannot show it is not a cheap review aid, it is a picture of a
different part. What the budget is now, and why it is this module's own rather than the viewer's,
is at `review_surface.REVIEW_MAX_TRIANGLES`.

These tests RUN the builder on a real VTK tree and assert on the file it wrote. The thing they are
guarding against is a feature whose guard test greps the source and passes on a dead code path, so
nothing here reads `inspect.getsource`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

pv = pytest.importorskip("pyvista")


def _quad_patch(nfaces: int):
    """A flat lattice of `nfaces`-ish quads with its points shared, in metres."""
    cols = max(1, int(np.sqrt(nfaces)))
    rows = max(1, nfaces // cols)
    xs, ys = np.meshgrid(np.arange(cols + 1) * 1e-3, np.arange(rows + 1) * 1e-3, indexing="ij")
    pts = np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)])
    i, j = np.meshgrid(np.arange(cols), np.arange(rows), indexing="ij")
    a = (i * (rows + 1) + j).ravel()
    faces = np.empty((a.size, 5), dtype=np.int64)
    faces[:, 0] = 4
    faces[:, 1], faces[:, 2], faces[:, 3], faces[:, 4] = a, a + rows + 1, a + rows + 2, a + 1
    return pv.PolyData(pts, faces.reshape(-1))


def _workspace(tmp_path: Path, *, per_patch: int, patches=("walls", "ports")) -> Path:
    """A workspace that looks like a finished two-region run: both regions' polyMesh on disk and
    both regions' foamToVTK boundary export beside it."""
    mesh = _quad_patch(per_patch)
    for region in ("fluid", "solid"):
        time_dir = tmp_path / "VTK" / region / "case_0"
        (time_dir / "boundary").mkdir(parents=True, exist_ok=True)
        for name in patches:
            mesh.save(time_dir / "boundary" / f"{name}.vtp")
        # the marker foamToVTK writes once a time is fully written (foam_exec.VTM_SUFFIX)
        (tmp_path / "VTK" / region / "case_0.vtm").write_text("<VTKFile/>")
        pm = tmp_path / "constant" / region / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        (pm / "boundary").write_text(
            "1\n(\nwalls { type wall; nFaces 1; startFace 0; }\n)\n")
    return tmp_path


def _faces_of(msh: Path) -> int:
    """How many elements the msh holds, read out of the file itself."""
    from meshpipeline.sandbox.mesh_reader import read_mesh

    loaded = read_mesh(str(msh), "m")
    return sum(len(tags) for _d, _t, _n, tags in loaded.physical_groups)


# #
# a boundary smaller than the budget is untouched
# #
def test_a_small_boundary_keeps_every_face_it_had(tmp_path):
    """The budget must not change what this engine has always delivered. 2 x 2 x 10,000 faces is
    80,000 triangles against REVIEW_MAX_TRIANGLES' 2,000,000."""
    ws = _workspace(tmp_path, per_patch=10_000)
    into: dict = {}
    groups, bbox = RS.build_region_msh(ws, into=into)

    assert sorted(groups) == ["fluid", "solid"]
    assert bbox is not None
    assert "surface_reduced" not in into, "a boundary inside the budget is reduced by nothing"
    assert (ws / RS.REVIEW_MSH).is_file()


def test_the_small_surface_still_opens_and_names_both_regions(tmp_path):
    from meshpipeline.sandbox.mesh_reader import read_mesh

    ws = _workspace(tmp_path, per_patch=10_000)
    RS.build_region_msh(ws)
    loaded = read_mesh(str(ws / RS.REVIEW_MSH), "m")
    assert sorted(n for _d, _t, n, _g in loaded.physical_groups) == ["fluid", "solid"]
    assert sorted(loaded.entity_tags_by_name) == ["fluid", "solid"]


def test_every_element_is_a_triangle(tmp_path):
    """sandbox/mesh_reader np.vstack()s face blocks prefixed by a corner count, so one group
    holding two corner counts raises ValueError and the review never opens. The budget's own
    output is triangles and so is the fan, and this reads the file to prove it."""
    import gmsh

    ws = _workspace(tmp_path, per_patch=10_000)
    RS.build_region_msh(ws)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(ws / RS.REVIEW_MSH))
        kinds = {int(t) for t in gmsh.model.mesh.getElementTypes()}
    finally:
        gmsh.finalize()
    # gmsh element type 2 is the 3-node triangle; nothing else may appear
    assert kinds == {2}, kinds


# #
# a boundary larger than the budget is fitted into it, and says so
# #
def test_a_large_boundary_is_fitted_into_the_budget(tmp_path):
    """4 x 600,000 faces is 4,800,000 triangles against REVIEW_MAX_TRIANGLES' 2,000,000."""
    ws = _workspace(tmp_path, per_patch=600_000)
    into: dict = {}
    groups, _bbox = RS.build_region_msh(ws, into=into)

    assert sorted(groups) == ["fluid", "solid"], "both regions must still be carried"
    note = into.get("surface_reduced")
    assert note, "a reduced surface that does not say it is reduced is the thing to never ship"
    assert note["delivered_faces"] > note["drawn_triangles"]
    assert note["resolution_m"] > 0.0
    assert _faces_of(ws / RS.REVIEW_MSH) <= 4 * RS._budget(), (
        "the file must be bounded by the budget, not by the mesh")


def test_the_fitted_surface_is_small_enough_to_open(tmp_path):
    """The old shape's file grew with the mesh - 99 MB of msh per million boundary faces, 7.1 GB
    at the plate's size. This one is bounded by the budget, and the bound is what `read_mesh` has
    to open in the review process.

    MEASURED 2026-11-12 on a synthetic two-region boundary of 4,000,000 delivered triangles:
    50,315,711 B and read_mesh 1.54 s at a budget of 800,000; 126,529,907 B and 2.82 s at
    2,000,000; 254,896,883 B and 5.32 s at 4,000,000 - about 63 bytes a triangle. The bar here is
    that figure with room for the file's own headers and nothing like room for the mesh.
    """
    from meshpipeline.sandbox.mesh_reader import read_mesh

    ws = _workspace(tmp_path, per_patch=600_000)
    RS.build_region_msh(ws)
    size = (ws / RS.REVIEW_MSH).stat().st_size
    assert size < 80 * RS._budget(), (size, "63 B a triangle measured, with room")
    # AND IT OPENS. A bound on the bytes is only worth having if the reader can still take them.
    loaded = read_mesh(str(ws / RS.REVIEW_MSH), "m")
    assert sorted(n for _d, _t, n, _g in loaded.physical_groups) == ["fluid", "solid"]


def test_the_fitted_surface_still_names_both_regions(tmp_path):
    from meshpipeline.sandbox.mesh_reader import read_mesh

    ws = _workspace(tmp_path, per_patch=600_000)
    RS.build_region_msh(ws)
    loaded = read_mesh(str(ws / RS.REVIEW_MSH), "m")
    assert sorted(n for _d, _t, n, _g in loaded.physical_groups) == ["fluid", "solid"]


def test_the_same_boundary_twice_is_the_same_file(tmp_path):
    """Determinism: the reducer is numpy over sorted keys and gmsh is pinned to one thread with a
    fixed seed (engines/determinism), so two builds of one boundary are one file."""
    first = _workspace(tmp_path / "a", per_patch=600_000)
    second = _workspace(tmp_path / "b", per_patch=600_000)
    RS.build_region_msh(first)
    RS.build_region_msh(second)
    assert (first / RS.REVIEW_MSH).read_bytes() == (second / RS.REVIEW_MSH).read_bytes()


# #
# a boundary past the ceiling is refused, clearly
# #
def test_a_boundary_past_the_ceiling_is_refused_and_says_so(tmp_path, monkeypatch):
    """A refusal is a bad outcome - no surface means the review says EVIDENCE_MISSING on a mesh
    that is on disk and paid for - and it beats the alternative, which is the same outcome reached
    by an OOM kill six hours in with nothing written down."""
    ws = _workspace(tmp_path, per_patch=10_000)
    monkeypatch.setattr(RS, "REFUSE_ABOVE_FACES", 1_000)
    into: dict = {}
    groups, bbox = RS.build_region_msh(ws, into=into)

    assert groups == {}
    assert bbox is None
    assert not (ws / RS.REVIEW_MSH).exists(), "nothing half-written is left behind"
    # `surface_refused`, not `refused`: the report already has a top-level `refused` that means
    # "the mesher refused this case" and that finalize branches on to return success=False
    assert "boundary faces is over" in into.get("surface_refused", "")
    assert "refused" not in into, "two keys called refused in one document is the defect"


def test_the_ceiling_refuses_before_a_single_vtp_is_READ(tmp_path, monkeypatch):
    """THE ORDER IS THE WHOLE POINT. The ceiling used to be asked after every .vtp of both regions
    had been read off disk and held as the compact pair, so at 119,999,999 faces it landed having
    already spent about 3.8 GiB plus the pyvista peak - the one dimension in which the new shape
    was worse than the per-patch loop it replaced.

    Proved by making the read ITSELF fatal: with the declared count over the ceiling, a refusal
    that still comes back cleanly can only have been reached without reading anything.
    """
    ws = _workspace(tmp_path, per_patch=10_000)
    # the delivered mesh's own boundary table is what is priced, so say what the mesh has
    for region in ("fluid", "solid"):
        (ws / "constant" / region / "polyMesh" / "boundary").write_text(
            "1\n(\nwalls { type wall; nFaces 90000000; startFace 0; }\n)\n")

    def never(*_a, **_k):
        raise AssertionError("a .vtp was read after the ceiling had already been passed")

    monkeypatch.setattr(RS, "_read_patch", never)
    into: dict = {}
    groups, bbox = RS.build_region_msh(ws, into=into)

    assert groups == {} and bbox is None
    assert "180,000,000 boundary faces is over" in into.get("surface_refused", "")


def test_the_declared_count_leaves_out_the_side_of_the_interface_the_file_drops(tmp_path):
    """`_patch_files` skips SOLID_TO_FLUID - the same surface as the fluid side, measured at
    93,600 faces on both - so the count the ceiling is asked about must skip it too, or a plate
    would be refused for faces this file was never going to carry."""
    ws = _workspace(tmp_path, per_patch=10)
    (ws / "constant" / "solid" / "polyMesh" / "boundary").write_text(
        "2\n(\nwalls { type wall; nFaces 7; startFace 0; }\n"
        "solid_to_fluid { type mappedWall; nFaces 93600; startFace 7; }\n)\n")
    (ws / "constant" / "fluid" / "polyMesh" / "boundary").write_text(
        "1\n(\nfluid_to_solid { type mappedWall; nFaces 93600; startFace 0; }\n)\n")
    assert RS._declared_faces(ws) == 93_600 + 7


def test_a_workspace_with_no_boundary_table_declares_nothing(tmp_path):
    """Every unit fixture that fakes a VTK tree without a polyMesh: the cheap gate says nothing
    about it and the gate after the read is what covers it."""
    (tmp_path / "VTK" / "fluid" / "case_0" / "boundary").mkdir(parents=True)
    assert RS._declared_faces(tmp_path) == 0


def test_the_ceiling_is_the_same_figure_the_boundary_index_uses():
    """One number for "more boundary than a mesh job will walk", not two that happen to agree."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    assert RS.REFUSE_ABOVE_FACES == N.INDEX_SKIP_ABOVE_FACES


def test_the_corintis_plate_is_inside_the_ceiling():
    """31,184,373 fluid + 40,667,460 solid boundary faces, before the solid side of the interface
    is dropped."""
    assert 31_184_373 + 40_667_460 < RS.REFUSE_ABOVE_FACES


# #
# half a surface is still not published
# #
def test_a_region_with_no_export_still_blocks_the_surface(tmp_path):
    """The rule this module already had, re-checked through the new path: the mesh has two regions
    and a surface carrying one of them reads as a review of all of it."""
    ws = _workspace(tmp_path, per_patch=10_000)
    import shutil

    shutil.rmtree(ws / "VTK" / "fluid")
    groups, bbox = RS.build_region_msh(ws)
    assert groups == {}
    assert bbox is None


# #
# AT CUSTOMER SIZE THE SURFACE MUST STILL HOLD THE CHANNELS
# #
#: The Corintis plate as delivered: a pitch-8 commensurate lattice, the measured channel, the
#: measured pitch, and the two regions' boundary face counts.
CELL_M = 25.1578e-6
CHANNEL_M = 100.631e-6
PITCH_M = 200.0e-6
INTERFACE_FACES = 31_184_373
#: the solid's patches once its side of the interface is dropped (_patch_files skips it)
SIDE_FACES = 40_667_460 - 31_184_373
#: the interface's area, from its own face count at the delivered cell size
INTERFACE_AREA_M2 = INTERFACE_FACES * CELL_M * CELL_M


def _comb(n_wall=40, n_long=200, n_deep=40):
    """`n_wall // 2` channels of CHANNEL_M on a PITCH_M pitch, each wall a lattice of CELL_M
    quads, the two walls of one channel wound so they face each other.

    THE SHAPE IS THE WHOLE POINT. `polymesh_budget._cluster_once` keys on which of six axis
    sectors a vertex faces, so the two walls of ONE channel never merge with each other however
    coarse the cluster gets. What merges is walls of the SAME parity, which are one PITCH apart -
    so the resolution the feature can stand is set by the pitch and not by the channel width.
    """
    pts, faces, base = [], [], 0
    ys = np.arange(n_long + 1) * CELL_M
    zs = np.arange(n_deep + 1) * CELL_M
    Y, Z = np.meshgrid(ys, zs, indexing="ij")
    for w in range(n_wall):
        ch, side = divmod(w, 2)
        x = ch * PITCH_M + (CHANNEL_M if side else 0.0)
        pts.append(np.column_stack([np.full(Y.size, x), Y.ravel(), Z.ravel()]))
        i, j = np.meshgrid(np.arange(n_long), np.arange(n_deep), indexing="ij")
        a = (i * (n_deep + 1) + j).ravel() + base
        f = np.empty((a.size, 5), dtype=np.int64)
        f[:, 0] = 4
        if side:
            f[:, 1], f[:, 2], f[:, 3], f[:, 4] = a, a + n_deep + 1, a + n_deep + 2, a + 1
        else:
            f[:, 1], f[:, 2], f[:, 3], f[:, 4] = a, a + 1, a + n_deep + 2, a + n_deep + 1
        faces.append(f.reshape(-1))
        base += pts[-1].shape[0]
    return {"name": "fluid/fluid_to_solid",
            "face_count": sum(f.size // 5 for f in faces),
            "points": np.concatenate(pts).astype(np.float32).tobytes(),
            "polys": np.concatenate(faces).astype(np.uint32).tobytes()}


def _wall_planes(patch) -> int:
    """How many distinct channel-wall planes the patch still has, counted off the points the
    surviving triangles actually refer to."""
    from meshpipeline.render.polymesh_budget import _triangles

    pts = np.frombuffer(patch["points"], dtype=np.float32).reshape(-1, 3)
    tri = _triangles(np.frombuffer(patch["polys"], dtype=np.uint32))
    if len(tri) == 0:
        return 0
    xs = np.unique(np.sort(pts[np.unique(tri.ravel()), 0].astype(np.float64)))
    kept = [xs[0]]
    for x in xs[1:]:
        if x - kept[-1] > CELL_M / 2:
            kept.append(x)
    return len(kept)


def _area(patch) -> float:
    from meshpipeline.render.polymesh_budget import _triangles

    pts = np.frombuffer(patch["points"], dtype=np.float32).reshape(-1, 3)
    v = pts[_triangles(np.frombuffer(patch["polys"], dtype=np.uint32))]
    return 0.5 * float(np.linalg.norm(
        np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1).sum())


def _budget_for_h(area: float, h: float) -> int:
    """The allowance that lands `reduce_patch` on cluster size `h`: it solves
    h = sqrt(2 * area / allow) off the patch's own area."""
    return max(1, int(2.0 * area / (h * h)))


def test_the_reducer_keeps_the_channels_below_the_pitch_and_loses_them_above_it():
    """THE MEASUREMENT THE BUDGET IS SET FROM, 2026-11-12. Twenty 100.631 um channels on a 200 um
    pitch at 25.1578 um cells, through the viewer's own reducer at a succession of cluster sizes:

        h 512 um -> 10 of 40 planes    h 222 um -> 32 of 40
        h 337 um -> 18 of 40           h 200 um -> 38 of 40
        h 254 um -> 22 of 40           h 162 um -> 40 of 40

    So the comb survives whole below about 0.8 x its pitch and is perforated above it. This runs
    the four rows the budget decision turns on.
    """
    from meshpipeline.render.polymesh_budget import reduce_patch

    comb = _comb()
    assert _wall_planes(comb) == 40, "the fixture itself has to have forty walls"
    area = _area(comb)
    got = {}
    for h in (512e-6, 337e-6, 254e-6, 162e-6):
        reduced, _came, used = reduce_patch(comb, max_triangles=_budget_for_h(area, h))
        got[h] = (_wall_planes(reduced), used)
    assert got[512e-6][0] <= 12, got
    assert got[337e-6][0] <= 20, got
    assert got[254e-6][0] <= 24, got
    assert got[162e-6][0] == 40, got
    # the cluster size each row actually ran at, so a drift in reduce_patch's own sizing shows up
    # here rather than quietly changing what this test is about
    for h, (_n, used) in got.items():
        assert abs(used - h) < 0.05 * h, (h, used)


def test_the_plates_interface_is_now_fitted_inside_the_h_the_comb_survives(monkeypatch):
    """THE FIX, at the size it is for. The allowance the interface is offered is `_fit`'s
    decision; the cluster size that follows from it is `reduce_patch`'s arithmetic on the patch's
    own area. Put together at the plate's measured 31,184,373 faces of 25.1578 um
    (0.01974 m2), the two have to land below the 162 um the comb survives whole.
    """
    import math

    from meshpipeline.render import polymesh_budget as PB

    seen: list[tuple[list[str], int]] = []
    real = PB.fit_to_budget

    def spy(patches, *, max_triangles):
        seen.append(([str(p["name"]) for p in patches], int(max_triangles)))
        return real(patches, max_triangles=max_triangles)

    monkeypatch.setattr(PB, "fit_to_budget", spy)
    # the plate's two groups, at the plate's face counts: the shares are all `_fit` reads
    RS._fit([{"name": "fluid/fluid_to_solid", "face_count": INTERFACE_FACES,
              "points": b"", "polys": b""},
             {"name": "solid/walls", "face_count": SIDE_FACES, "points": b"", "polys": b""}])

    allow = next(m for names, m in seen if "fluid/fluid_to_solid" in names)
    assert allow == int(RS._budget() * RS.INTERFACE_SHARE_OF_BUDGET)
    h = math.sqrt(2.0 * INTERFACE_AREA_M2 / allow)
    assert h < 162e-6, f"{h * 1e6:.1f} um of cluster on a {PITCH_M * 1e6:.0f} um pitch"


def test_the_old_shape_merged_those_channels_away_and_this_one_does_not():
    """THE CONTROL, run rather than reasoned about. The old allocation was the viewer's 800,000
    shared by face count across the patches of both regions, which gave the plate's interface
    800,000 x 31,184,373 / 40,667,460 = 613,451 triangles and a 253.67 um cluster. The new one
    gives it 1,800,000 and 148.09 um - NOT the 141 um this line used to say, which is the figure
    for the FULL budget rather than for 0.9 of it. Measured on the comb below: 613,451 keeps 22 of
    the 40 wall planes and 1,800,000 keeps 40, and the proportional share of the raised budget
    (1,533,627, 160.43 um) already keeps 40 too - so the BUDGET RAISE is the fix and this share is
    8.6% of margin against the comb's 162 um instead of 1.0%. Both operating points are put
    through the reducer on the same comb.
    """
    import math

    from meshpipeline.render.polymesh_budget import DRAWN_MAX_TRIANGLES, reduce_patch

    comb = _comb()
    area = _area(comb)
    old_allow = int(DRAWN_MAX_TRIANGLES * INTERFACE_FACES / (INTERFACE_FACES + SIDE_FACES))
    new_allow = int(RS._budget() * RS.INTERFACE_SHARE_OF_BUDGET)
    h_old = math.sqrt(2.0 * INTERFACE_AREA_M2 / old_allow)
    h_new = math.sqrt(2.0 * INTERFACE_AREA_M2 / new_allow)
    assert h_old > PITCH_M > h_new, (h_old, h_new)

    before = _wall_planes(reduce_patch(comb, max_triangles=_budget_for_h(area, h_old))[0])
    after = _wall_planes(reduce_patch(comb, max_triangles=_budget_for_h(area, h_new))[0])
    assert before <= 24, (before, "the old allowance kept more walls than it was measured to")
    assert after == 40, (after, "the new allowance has to keep every wall")


def test_the_budget_is_this_modules_own_and_not_the_viewers(monkeypatch):
    """settings/viewer_limits documents DRAWN_MAX_TRIANGLES as a TRANSPORT figure measured on
    "Intel UHD via ANGLE/D3D11" and invites raising it. This file is not transport: it is what
    `read_mesh` opens in the review process. Moving the viewer's number must not move it."""
    from meshpipeline.render import polymesh_budget as PB
    from meshpipeline.settings import viewer_limits as VL

    before = RS._budget()
    monkeypatch.setattr(VL, "DRAWN_MAX_TRIANGLES", 10_000_000)
    monkeypatch.setattr(PB, "DRAWN_MAX_TRIANGLES", 10_000_000)
    assert RS._budget() == before == RS.REVIEW_MAX_TRIANGLES


def test_a_boundary_with_no_interface_patch_is_fitted_in_one_call(monkeypatch):
    """There is nothing to take the budget away from, so the split must not happen - which is
    also what keeps every case this engine has delivered fitted exactly as it was."""
    from meshpipeline.render import polymesh_budget as PB

    calls: list[int] = []
    real = PB.fit_to_budget

    def spy(patches, *, max_triangles):
        calls.append(int(max_triangles))
        return real(patches, max_triangles=max_triangles)

    monkeypatch.setattr(PB, "fit_to_budget", spy)
    RS._fit([{"name": "solid/walls", "face_count": 10, "points": b"", "polys": b""},
             {"name": "solid/ports", "face_count": 10, "points": b"", "polys": b""}])
    assert calls == [RS._budget()]


def test_the_merged_note_counts_every_patch_and_not_only_the_reduced_group(tmp_path, monkeypatch):
    """`fit_to_budget` returns None when it reduced nothing and counts only the patches IT was
    given, so adding the two notes would report the interface's faces as the whole boundary's
    whenever the block sides happened to fit. Run on a workspace whose interface is far over its
    share and whose sides are far under theirs - the budget is pinned small so that shape is
    reached in a second rather than in a minute."""
    monkeypatch.setattr(RS, "REVIEW_MAX_TRIANGLES", 20_000)
    mesh = _quad_patch(60_000)
    small = _quad_patch(100)
    for region, big, little in (("fluid", "fluid_to_solid", "inlet"),
                                ("solid", "walls", "ports")):
        t = tmp_path / "VTK" / region / "case_0"
        (t / "boundary").mkdir(parents=True, exist_ok=True)
        (mesh if region == "fluid" else small).save(t / "boundary" / f"{big}.vtp")
        small.save(t / "boundary" / f"{little}.vtp")
        (tmp_path / "VTK" / region / "case_0.vtm").write_text("<VTKFile/>")
        pm = tmp_path / "constant" / region / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        (pm / "boundary").write_text("1\n(\nwalls { type wall; nFaces 1; startFace 0; }\n)\n")

    into: dict = {}
    groups, _bbox = RS.build_region_msh(tmp_path, into=into)
    assert sorted(groups) == ["fluid", "solid"]
    note = into["surface_reduced"]
    # every patch's delivered faces, not just the group that was cut
    assert note["delivered_faces"] >= mesh.GetNumberOfPolys() + 3 * small.GetNumberOfPolys()
    assert note["drawn_triangles"] < note["delivered_faces"]
    assert "fluid/fluid_to_solid" in note["patches"]
    # the block sides fitted their share, so they contribute no per-patch row and their faces
    # would have been missing from both totals if the note had been added rather than recounted
    assert "solid/ports" not in note["patches"]


# #
# the arrays that go into gmsh are the dtypes gmsh takes without copying
# #
def test_the_node_tags_are_handed_over_as_sizes():
    """gmsh's `_ivectorsize` converts a "vector of sizes" with
    `numpy.ascontiguousarray(o, numpy.uintp)`, which is a no-op on an array already of that
    dtype. The Python lists these replaced were 240 million ints for the plate's elements and
    another 100 million floats for its points."""
    tags = RS._node_tags(100, 4)
    assert tags.dtype == np.uintp
    assert tags.tolist() == [101, 102, 103, 104]
    assert RS._node_tags(0, 0).size == 0


def test_the_patch_arrays_are_the_dtypes_gmsh_wants():
    patch = {"name": "p", "face_count": 1,
             "points": np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
                                dtype=np.float32).tobytes(),
             "polys": np.array([4, 0, 1, 2, 3], dtype=np.uint32).tobytes()}
    coords, tris = RS._as_arrays(patch, 100)
    assert coords.dtype == np.float64 and coords.size == 12
    assert tris.dtype == np.uintp
    # one quad fanned from its first corner, with the patch's node base added
    assert tris.tolist() == [101, 102, 103, 101, 103, 104]


# #
# ...AND IT IS PAID FOR OUT OF THE RUN'S OWN BUDGET, which it was not
# #
#: What the plate's two boundaries are, before the solid side of the interface is dropped - the
#: figure `SURFACE_SECONDS_PER_FACE` is priced against.
PLATE_BOUNDARY_FACES = 31_184_373 + 40_667_460


def _declare(ws: Path, faces: int) -> None:
    """Say, in the delivered mesh's own boundary table, that each region has this many faces -
    which is where both the ceiling and the price are read from."""
    for region in ("fluid", "solid"):
        (ws / "constant" / region / "polyMesh" / "boundary").write_text(
            f"1\n(\nwalls {{ type wall; nFaces {faces}; startFace 0; }}\n)\n")


def test_a_surface_the_budget_cannot_pay_for_is_not_started(tmp_path, monkeypatch):
    """THE LAST PHASE INSIDE THE ENGINE WITH NO BUDGET AT ALL. `_review_aids` passed
    `budget_left_s` to the export and `native.py` passed it to the index; this was handed nothing,
    while at the plate it reads 71.9 M faces of .vtp through pyvista and writes 121 MiB of msh.
    `REFUSE_ABOVE_FACES` does not bind there - 71.9 M is 60% of it - so without a budget the only
    thing that could stop it was the machine being deleted under it.

    PROVED BY MAKING THE READ ITSELF FATAL, the same way the ceiling's order is proved above: a
    refusal that still comes back cleanly can only have been reached before anything was read.
    """
    ws = _workspace(tmp_path, per_patch=10_000)
    _declare(ws, 20_000_000)

    def never(*_a, **_k):
        raise AssertionError("a .vtp was read for a surface the budget could not pay for")

    monkeypatch.setattr(RS, "_read_patch", never)
    into: dict = {}
    groups, bbox = RS.build_region_msh(ws, into=into, budget_left_s=1.0)

    assert groups == {} and bbox is None
    assert not (ws / RS.REVIEW_MSH).exists()
    said = into.get("surface_refused", "")
    assert "40,000,000 boundary faces price at about 80s" in said, said
    assert "budget has 1s left" in said, said
    # said as the RESOURCE it is, not as a builder that broke
    assert "CONJUGATE_RUN_TIMEOUT_S is the budget" in said, said
    assert "refused" not in into, "two keys called refused in one document is the defect"


def test_the_same_boundary_with_the_budget_absent_is_built_THE_CONTROL(tmp_path):
    """THE NO-FIX CONTROL, in the same breath: the only thing that changed is the budget, so a
    test that passes with and without it would be measuring the ceiling instead."""
    ws = _workspace(tmp_path, per_patch=10_000)
    _declare(ws, 20_000_000)
    groups, bbox = RS.build_region_msh(ws, budget_left_s=None)

    assert sorted(groups) == ["fluid", "solid"], groups
    assert bbox is not None
    assert (ws / RS.REVIEW_MSH).is_file()


def test_a_budget_that_can_afford_it_builds_it(tmp_path):
    """The gate must bite on the PRICE and not on the presence of a number."""
    ws = _workspace(tmp_path, per_patch=10_000)
    _declare(ws, 20_000_000)
    into: dict = {}
    groups, _bbox = RS.build_region_msh(ws, into=into, budget_left_s=600.0)

    assert sorted(groups) == ["fluid", "solid"], groups
    assert "surface_refused" not in into
    assert 40_000_000 * RS.SURFACE_SECONDS_PER_FACE < 600.0


def test_at_head_this_builder_could_not_be_given_a_budget_at_all_THE_CONTROL():
    """THE SECOND CONTROL, and the shape of the defect: at HEAD the parameter does not exist, so
    no caller COULD bound this phase. Executed against the file at HEAD rather than read."""
    import importlib.util
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    path = "src/meshpipeline/engines/cfmesh_conjugate/review_surface.py"
    try:
        src = subprocess.run(["git", "show", "HEAD:" + path], cwd=str(repo),
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"the control needs the file at HEAD: {exc}")
    spec = importlib.util.spec_from_loader("_head_review_surface", loader=None)
    head = importlib.util.module_from_spec(spec)
    head.__file__ = "HEAD:" + path
    sys.modules["_head_review_surface"] = head
    try:
        exec(compile(src, "HEAD:" + path, "exec"), head.__dict__)
    finally:
        sys.modules.pop("_head_review_surface", None)

    import inspect as _inspect
    taken = _inspect.signature(head.build_region_msh).parameters
    assert "budget_left_s" not in taken, (
        "HEAD already took a budget, so this is not the control it claims to be")
    with pytest.raises(TypeError):
        head.build_region_msh("/nowhere", budget_left_s=1.0)


def test_the_price_is_what_the_module_charges_and_the_plate_fits_it():
    """The constant, against the figure it was measured for: 2.0 us a face over the plate's
    71,851,833 boundary faces is 144 s, and the engine's budget is 21,600 s."""
    from meshpipeline.engines.registry import get_spec

    priced = PLATE_BOUNDARY_FACES * RS.SURFACE_SECONDS_PER_FACE
    assert 140.0 < priced < 150.0, priced
    assert priced < get_spec("cfmesh_conjugate").run_policy.run_timeout()


def test_the_review_aids_hand_the_surface_what_the_export_left_of_the_budget(tmp_path,
                                                                             monkeypatch):
    """THE WIRING, EXECUTED through `native._review_aids`: the remaining budget is RECOMPUTED
    after the export rather than the export's own figure being handed on again, because the export
    may have spent most of it."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    ws = _workspace(tmp_path, per_patch=10_000)
    _declare(ws, 20_000_000)
    seen: list = []

    def spent(ws_, *, all_regions=False, region="", bashrc="", timeout=None):
        seen.append(("export", timeout))
        return "VTK/fluid/case_0/internal.vtu"

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk", spent)
    real = RS.build_region_msh

    def watched(ws_, *, into=None, budget_left_s=None):
        seen.append(("surface", budget_left_s))
        return real(ws_, into=into, budget_left_s=budget_left_s)

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.review_surface.build_region_msh", watched)

    out = N._review_aids(ws, bashrc="/dev/null", cells=1, budget_left_s=5.0)

    assert [k for k, _v in seen] == ["export", "surface"], seen
    assert seen[0][1] == 5.0, "the export is bounded by what is left of the run"
    left = seen[1][1]
    assert left is not None and left <= 5.0, seen
    # 40,000,000 declared faces price at 80 s, which 5 s cannot pay for
    assert "price at about 80s" in out.get("surface_refused", ""), out
    assert out["surface"] is None
    assert out["why"] == out["surface_refused"], (
        "the builder's own refusal has to come first, or a reader is sent to look for an export "
        "bug that is not there")


def test_the_file_is_the_size_of_the_BUDGET_and_not_of_the_MESH(tmp_path):
    """THE CLAIM THE WHOLE CHANGE RESTS ON, executed over a 4x range of boundary.

    The old shape grew with the mesh - 99 MB of msh per million boundary faces, 7.1 GB at the
    plate's size. Measured 2026-11-12 at REVIEW_MAX_TRIANGLES over SIXTEEN times the boundary
    (1 M to 16 M delivered faces, every one of them at or over the budget) the file went
    127,069,475 -> 125,309,891 B, which is 1.4% SMALLER. Two points four times apart are enough to
    catch a file that has started following the mesh again, and cheap enough to run every time.
    """
    # BOTH POINTS HAVE TO BE OVER THE BUDGET, which is the thing this test had to be taught: a
    # boundary UNDER it is returned unchanged by `reduce_patch` and its file is then the size of
    # the MESH, correctly and deliberately (see test_a_small_boundary_keeps_every_face_it_had).
    # Measured while writing this: 600,000 faces is 1,200,000 triangles, under the 2,000,000, and
    # writes 75,782,034 B - which is the mesh, not the budget.
    a = _workspace(tmp_path / "a", per_patch=300_000)      # 1,200,000 faces, 2.4 M triangles
    b = _workspace(tmp_path / "b", per_patch=1_200_000)    # 4,800,000 faces, 9.6 M triangles
    RS.build_region_msh(a)
    RS.build_region_msh(b)
    small = (a / RS.REVIEW_MSH).stat().st_size
    large = (b / RS.REVIEW_MSH).stat().st_size

    assert abs(large - small) < 0.10 * small, (small, large, "the file followed the mesh")
    # ...and the bound is the budget's, at the 63 B a triangle this file is measured at
    assert large < 80 * RS._budget(), large
