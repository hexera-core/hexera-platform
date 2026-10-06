# Responsibility: Prove the placed-parts (snap-grid) ECXML mesher keeps the file's model: exact boxes, every layer, the precedence rule, matching interfaces, named outside patches, a valid polyMesh.
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from tests.unit.cad.ecxml_models import Ecxml, ducted_board, set_top_box, tiny_board

from meshpipeline.cad.ingest.ecxml import read_ecxml
from meshpipeline.cad.ingest.ecxml_build import plan_region_names
from meshpipeline.cad.ingest.ecxml_place import place, placed_skin
from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml
from meshpipeline.engines.snapgrid.polymesh import SIDES, internal_faces, side_faces


def _write(tmp_path, doc: Ecxml, name: str = "model.ecxml"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes(doc.xml())
    return p


def _mesh(tmp_path, doc: Ecxml, **plan):
    src = _write(tmp_path, doc)
    return mesh_ecxml(src, tmp_path / "case", G.GridPlan(**plan), binary=False)


def _region(result, name):
    return next(r for r in result.report["regions"] if r["name"] == name)


# ------------------------------------------------------------------------------ the polyMesh --
def _quad_area_vector(pts, quad):
    p = pts[quad]
    return 0.5 * np.cross(p[2] - p[0], p[3] - p[1])


def test_every_cell_is_closed_and_every_face_points_out_of_its_owner():
    shape = (3, 2, 4)
    nx, ny, nz = shape
    xs, ys, zs = np.array([0, 1, 3, 4.0]), np.array([0, 2, 3.0]), np.array([0, 1, 2, 4, 7.0])
    pts = np.stack(np.meshgrid(xs, ys, zs, indexing="ij"), -1).transpose(2, 1, 0, 3).reshape(-1, 3)
    centres = np.stack(np.meshgrid((xs[1:] + xs[:-1]) / 2, (ys[1:] + ys[:-1]) / 2,
                                   (zs[1:] + zs[:-1]) / 2, indexing="ij"),
                       -1).transpose(2, 1, 0, 3).reshape(-1, 3)
    owner, neigh, quads = internal_faces(shape)
    assert (owner < neigh).all()
    keys = owner * (nx * ny * nz) + neigh
    assert (np.diff(keys) > 0).all(), "internal faces must be upper-triangular"
    total = np.zeros((nx * ny * nz, 3))
    for o, n, q in zip(owner, neigh, quads):
        a = _quad_area_vector(pts, q)
        assert np.dot(a, centres[n] - centres[o]) > 0
        total[o] += a
        total[n] -= a
    for axis, side in SIDES:
        o, q = side_faces(shape, axis, side)
        for oo, qq in zip(o, q):
            a = _quad_area_vector(pts, qq)
            assert a[axis] * (1 if side else -1) > 0, "a boundary face must point out"
            total[oo] += a
    assert np.abs(total).max() < 1e-12, "every cell's faces must close it"


def _polygon_area_vectors(points, off, pts):
    """0.5 * sum of p_i x p_{i+1} for each polygon (exact for planar polygons)."""
    out = np.zeros((len(off) - 1, 3))
    for f in range(len(off) - 1):
        loop = points[pts[off[f]:off[f + 1]]]
        out[f] = 0.5 * np.cross(loop, np.roll(loop, -1, axis=0)).sum(axis=0)
    return out


def test_blocks_meet_with_hanging_nodes_and_every_cell_still_closes():
    from meshpipeline.engines.snapgrid import blocks as B
    from meshpipeline.engines.snapgrid import boxmesh

    pl = place(read_ecxml(set_top_box().xml()))
    plan = G.GridPlan(max_cells=10**7)
    geo = B.geometry_of(pl, plan)
    H = max(geo.domain.hi[i] - geo.domain.lo[i] for i in range(3)) / 12
    layout = B.decompose(geo, plan, H, leaf_cells=64)
    assert len(layout.blocks) > 4
    topo = boxmesh.build_topology(layout)
    assert topo.block_faces > 0 and topo.hanging_faces > 0
    keys = np.unique(np.concatenate([topo.face_pts, topo.b_pts]))
    ix, iy, iz = boxmesh._decode(keys, topo.dims)
    g = layout.global_lines
    points = np.stack([g[0][ix], g[1][iy], g[2][iz]], axis=1)
    ids_i = np.searchsorted(keys, topo.face_pts)
    ids_b = np.searchsorted(keys, topo.b_pts)
    area_i = _polygon_area_vectors(points, topo.face_off, ids_i)
    area_b = _polygon_area_vectors(points, topo.b_off, ids_b)
    # cell centres, block by block
    centres = []
    for blk in layout.blocks:
        c = [(layout.coords(blk, a)[1:] + layout.coords(blk, a)[:-1]) / 2 for a in range(3)]
        cz, cy, cx = np.meshgrid(c[2], c[1], c[0], indexing="ij")
        centres.append(np.stack([cx.ravel(), cy.ravel(), cz.ravel()], axis=1))
    centres = np.concatenate(centres)
    assert (topo.owner < topo.neighbour).all()
    assert (np.diff(topo.owner * topo.n_cells + topo.neighbour) > 0).all()
    d = centres[topo.neighbour] - centres[topo.owner]
    assert (np.einsum("ij,ij->i", area_i, d) > 0).all(), "faces point from owner to neighbour"
    total = np.zeros((topo.n_cells, 3))
    np.add.at(total, topo.owner, area_i)
    np.add.at(total, topo.neighbour, -area_i)
    np.add.at(total, topo.b_owner, area_b)
    scale = np.abs(area_i).max()
    assert np.abs(total).max() < 1e-9 * scale, "every cell's faces close it"
    # every point on a face's edge is in that face: no edge is used by one face only
    edges: dict[tuple[int, int], int] = {}
    for off, ids in ((topo.face_off, ids_i), (topo.b_off, ids_b)):
        for f in range(len(off) - 1):
            loop = ids[off[f]:off[f + 1]].tolist()
            for a_, b_ in zip(loop, loop[1:] + loop[:1]):
                key = (min(a_, b_), max(a_, b_))
                edges[key] = edges.get(key, 0) + 1
    assert min(edges.values()) >= 2


def test_a_part_that_reaches_a_block_on_some_axes_only_is_not_looked_up_there():
    """server_1u / heatsink_plate_fin_duct regression: a heat sink's bounding box (or a vent, or
    a patch) overlapping a block on x but not on y must not have its x faces looked up in that
    block - they need not be its planes."""
    from meshpipeline.cad.ingest.ecxml_build import _Box

    grid = G.Grid(lines=[np.array([0.0, 0.1, 0.2]), np.array([0.0, 0.1]), np.array([0.0, 0.1])],
                  plan=G.GridPlan())
    # x bound 0.15 is no plane of this grid, and the box does not reach it on y at all
    assert G._slice(grid, _Box([0.05, 0.2, 0.0], [0.15, 0.3, 0.1]), 1e-9) is None
    doc = Ecxml("Split sink")
    doc.material("Al", 2700, 900, 0.1, ("isotropic", 200))
    doc.domain((0, 0, 0), (0.2, 0.2, 0.05))
    # two blocks far apart: the sink's bounding box spans regions none of its blocks reach
    doc.heatsink("Sink", [("A", (0.01, 0.01, 0.01), (0.004, 0.004, 0.02), "Al", 0.0),
                          ("B", (0.15, 0.15, 0.01), (0.004, 0.004, 0.02), "Al", 0.0)])
    for k in range(12):
        doc.block(f"P{k}", (0.03 + 0.009 * k, 0.12, 0.01), (0.002, 0.002, 0.001), "Al")
    from meshpipeline.engines.snapgrid import blocks as B

    pl = place(read_ecxml(doc.xml()))
    layout = B.decompose(B.geometry_of(pl, G.GridPlan()), G.GridPlan(), 0.01, leaf_cells=64)
    assert len(layout.blocks) > 2
    B.paint_blocks(pl, layout)                      # raised "not a grid plane" before
    painted = sum(int((blk.zone == 0).sum()) for blk in layout.blocks)
    assert painted > 0


# ------------------------------------------------------------------------------ the grid ------
def test_the_grid_holds_every_plane_the_model_states_and_grows_smoothly():
    pl = place(read_ecxml(tiny_board().xml()))
    grid = G.build_grid(pl, G.GridPlan(max_cells=10**6, growth=1.3))
    for i in range(3):
        for v in G.cluster(pl.planes[i], pl.tol.noise):
            assert np.abs(grid.lines[i] - v).min() < 1e-12
        w = grid.widths(i)
        assert (w > 0).all()
        # growth holds within ceil rounding of an interval's cell count
        assert np.max(np.maximum(w[1:] / w[:-1], w[:-1] / w[1:])) < 1.3 * 1.5


def test_a_model_that_cannot_keep_its_layers_within_the_budget_is_told_the_count_it_needs():
    doc = Ecxml("Stack")
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.domain((0, 0, 0), (0.1, 0.1, 0.05))
    z = 0.01
    for k in range(30):          # thirty 20 um layers, each a stated plane pair
        doc.block(f"L{k}", (0.01 + 0.002 * k, 0.01, z + 0.0001 * k), (0.03, 0.03, 2e-5), "Si")
    pl = place(read_ecxml(doc.xml()))
    with pytest.raises(G.OverBudget) as exc:
        G.build_grid(pl, G.GridPlan(max_cells=1000))
    assert exc.value.needed > 1000
    assert f"{exc.value.needed:,}" in str(exc.value)


# ------------------------------------------------------------------------------ the mesh ------
def test_tiny_board_regions_volumes_contacts_and_a_valid_case(tmp_path):
    result = _mesh(tmp_path, tiny_board())
    rep = result.report
    assert rep["fluid_regions"] == ["air"] and rep["solid_regions"] == ["Board", "Chip"]
    board, chip = _region(result, "Board"), _region(result, "Chip")
    assert math.isclose(board["volume_m3"], 0.03 * 0.02 * 0.0016, rel_tol=1e-12)
    assert math.isclose(chip["volume_m3"], 0.01 * 0.01 * 0.002, rel_tol=1e-12)
    assert math.isclose(sum(r["volume_m3"] for r in rep["regions"]), 0.05 * 0.04 * 0.02,
                        rel_tol=1e-12)
    assert rep["solid_contacts"] == [["Board", "Chip"]]
    faces = {(i["a"], i["b"]): i["faces"] for i in rep["interfaces"]}
    assert faces[("Board", "Chip")] > 0 and faces[("air", "Chip")] > 0
    case = tmp_path / "case"
    for f in ("points", "faces", "owner", "neighbour", "boundary", "cellZones"):
        assert (case / "constant" / "polyMesh" / f).is_file()
    props = (case / "constant" / "regionProperties").read_text()
    assert "fluid       (air)" in props and "solid       (Board Chip)" in props
    side = json.loads((case / "thermal_model.json").read_text())
    assert side["mesher"] == "snapgrid" and side["total_power_W"] == pytest.approx(2.0)


def test_region_names_are_the_fused_paths_names(tmp_path):
    doc = set_top_box()
    result = _mesh(tmp_path, doc, max_cells=600_000)
    assert tuple(result.region_names) == plan_region_names(read_ecxml(doc.xml()))


def test_a_cylinder_and_a_round_fan_hole_are_snapped_onto_their_true_surface(tmp_path):
    result = _mesh(tmp_path, set_top_box(), max_cells=600_000)
    curved = {s["part"]: s for s in result.report["staircased"]}
    cap, case = curved["Bulk_cap"], curved["Case"]      # Case: the fan's round hole in its wall
    # a staircase has 4/pi (+27%) of a cylinder's side; snapped, the side is the file's
    assert abs(cap["side_area_error_pct"]) < 10.0 and abs(cap["volume_error_pct"]) < 3.0
    assert case["kind"] == "round hole" and abs(case["side_area_error_pct"]) < 10.0
    assert any(line.startswith("Curved: Bulk_cap") for line in result.report["build_report"])
    # every box part is still exactly the file's, snapped points or not
    pcb = _region(result, "PCB")
    assert math.isclose(pcb["volume_m3"], 0.09 * 0.07 * 0.0016, rel_tol=1e-9)


def test_a_cylinder_lying_on_a_board_touches_it_along_a_line_only(tmp_path):
    doc = Ecxml("Lying coil")
    doc.material("FR4", 1900, 1200, 0.9, ("isotropic", 0.3))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    doc.domain((0, 0, 0), (0.04, 0.03, 0.02))
    doc.block("Board", (0.005, 0.005, 0.005), (0.03, 0.02, 0.0016), "FR4")
    doc.cylinder("Coil", (0.01, 0.01, 0.0066), (0.02, 0.006, 0.006), "+yz", "Cu", 1.0)
    doc.cylinder("Post", (0.028, 0.012, 0.0066), (0.004, 0.004, 0.005), "+xy", "Cu")
    result = _mesh(tmp_path, doc, max_cells=150_000)
    contacts = {tuple(c) for c in result.report["solid_contacts"]}
    # the coil lies on the board (tangent: a line); the post stands on it (its end: a disc)
    assert ("Board", "Coil") not in contacts and ("Board", "Post") in contacts
    released = result.report["line_contacts"]
    assert len(released) == 1 and released[0]["axis"] == "z" and released[0]["side"] == "-"
    coil = next(s_ for s_ in result.report["staircased"] if s_["part"] == "Coil")
    # the air under it near the line costs the coil some volume at this coarse grid (reported)
    assert abs(coil["volume_error_pct"]) < 10.0 and abs(coil["side_area_error_pct"]) < 5.0
    assert math.isclose(_region(result, "Board")["volume_m3"], 0.03 * 0.02 * 0.0016,
                        rel_tol=1e-9)


def test_a_50um_interface_and_a_25um_die_attach_keep_their_cells(tmp_path):
    doc = Ecxml("Lidded package")
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.material("TIM", 2500, 800, 0.9, ("isotropic", 4))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    doc.domain((0, 0, 0), (0.06, 0.06, 0.03))
    z = 0.01
    doc.block("Substrate", (0.01, 0.01, z), (0.04, 0.04, 0.001), "Cu")
    z += 0.001
    doc.block("Die attach", (0.025, 0.025, z), (0.01, 0.01, 25e-6), "TIM")
    z += 25e-6
    doc.block("Die", (0.025, 0.025, z), (0.01, 0.01, 0.0005), "Si", 50.0)
    z += 0.0005
    doc.block("TIM1", (0.025, 0.025, z), (0.01, 0.01, 50e-6), "TIM")
    z += 50e-6
    doc.block("Lid", (0.015, 0.015, z), (0.03, 0.03, 0.001), "Cu")
    result = _mesh(tmp_path, doc, max_cells=400_000, min_cells_across=2)
    thin = {r["part"]: r for r in result.report["thin_layers"]}
    assert thin["Die_attach"]["thickness_m"] == pytest.approx(25e-6)
    assert thin["Die_attach"]["cells_across"] >= 2 and thin["TIM1"]["cells_across"] >= 2
    assert math.isclose(_region(result, "TIM1")["volume_m3"], 0.01 * 0.01 * 50e-6, rel_tol=1e-9)
    contacts = {tuple(c) for c in result.report["solid_contacts"]}
    assert {("Die", "Die_attach"), ("Die", "TIM1"), ("Lid", "TIM1"),
            ("Die_attach", "Substrate")} <= contacts


def _lidded_package():
    doc = Ecxml("Lidded package")
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.material("TIM", 2500, 800, 0.9, ("isotropic", 4))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    doc.domain((0, 0, 0), (0.06, 0.06, 0.03))
    z = 0.01
    doc.block("Substrate", (0.01, 0.01, z), (0.04, 0.04, 0.001), "Cu")
    z += 0.001
    doc.block("Die attach", (0.025, 0.025, z), (0.01, 0.01, 25e-6), "TIM")
    z += 25e-6
    doc.block("Die", (0.025, 0.025, z), (0.01, 0.01, 0.0005), "Si", 50.0)
    z += 0.0005
    doc.block("TIM1", (0.025, 0.025, z), (0.01, 0.01, 50e-6), "TIM")
    z += 50e-6
    doc.block("Lid", (0.015, 0.015, z), (0.03, 0.03, 0.001), "Cu")
    return doc


def test_a_budget_that_takes_a_cell_from_a_thin_layer_says_so_and_names_the_budget(tmp_path):
    from meshpipeline.engines.snapgrid import blocks as B

    doc = _lidded_package()
    pl = place(read_ecxml(doc.xml()))
    with pytest.raises(G.OverBudget) as one:            # the fewest cells with ONE cell through
        B.build_layout(pl, G.GridPlan(max_cells=1000, min_cells_across=1, growth=2.0,
                                      cylinder_cells=6))
    budget = one.value.needed + 50
    lay = B.build_layout(pl, G.GridPlan(max_cells=budget, min_cells_across=2))
    assert lay.plan.min_cells_across == 1 and lay.layers_budget > budget
    result = _mesh(tmp_path, doc, max_cells=budget, min_cells_across=2)
    short = {r["part"]: r for r in result.report["thin_layers_short"]}
    assert short and all(r["cells_across"] < 2 and r["wanted"] == 2 for r in short.values())
    assert result.report["layers_budget"] == lay.layers_budget
    warn = [ln for ln in result.report["build_report"] if ln.startswith("WARNING")]
    assert warn and f"{lay.layers_budget:,}" in warn[0]
    # with that budget every layer keeps its two cells, and nothing is said
    full = _mesh(tmp_path / "full", doc, max_cells=lay.layers_budget, min_cells_across=2)
    assert not full.report["thin_layers_short"] and not full.report["layers_budget"]


def test_overlaps_follow_the_files_precedence(tmp_path):
    def model(producer):
        doc = Ecxml("Overlap", producer=producer)
        doc.material("A", 1000, 1000, 0.5, ("isotropic", 1))
        doc.domain((0, 0, 0), (0.1, 0.1, 0.1))
        doc.block("Small", (0.04, 0.04, 0.04), (0.02, 0.02, 0.02), "A")   # first, inside Big
        doc.block("Big", (0.02, 0.02, 0.02), (0.06, 0.06, 0.06), "A")
        return doc

    later = _mesh(tmp_path / "f", model("FloTHERM"))
    assert "Small" not in later.region_names         # the later Big overwrites all of it
    assert any("overwrite(s) all of Small" in line for line in later.report["build_report"])
    embedded = _mesh(tmp_path / "i", model("Icepak"))
    assert math.isclose(_region(embedded, "Small")["volume_m3"], 0.02 ** 3, rel_tol=1e-12)
    assert math.isclose(_region(embedded, "Big")["volume_m3"], 0.06 ** 3 - 0.02 ** 3,
                        rel_tol=1e-12)


def test_a_sealed_enclosure_keeps_its_own_air(tmp_path):
    doc = Ecxml("Sealed")
    doc.material("Steel", 7850, 460, 0.3, ("isotropic", 50))
    doc.domain((0, 0, 0), (0.2, 0.2, 0.1))
    doc.enclosure("Box", (0.05, 0.05, 0.02), (0.1, 0.1, 0.05), "Steel", 0.002)
    result = _mesh(tmp_path, doc)
    assert result.report["fluid_regions"] == ["air", "air_2"]
    inner = _region(result, "air_2")
    assert math.isclose(inner["volume_m3"], 0.096 * 0.096 * 0.046, rel_tol=1e-12)


def test_the_files_fans_vents_and_wall_plates_are_named_outside_patches(tmp_path):
    result = _mesh(tmp_path, ducted_board())
    patches = result.report["patches"]
    assert {"Inlet_fan", "Outlet", "Low_X", "High_X", "Low_Z", "High_Z"} <= set(patches)
    boundary = (tmp_path / "case" / "constant" / "polyMesh" / "boundary").read_text()
    block = boundary[boundary.index("Low_X"):]
    assert "type            wall;" in block[:200]
    side = json.loads((tmp_path / "case" / "thermal_model.json").read_text())
    fan = next(p for p in side["patches"] if p["name"] == "Inlet fan")
    assert fan["mesh_patch"] == "Inlet_fan" and fan["faces"] == patches["Inlet_fan"]


def test_the_geometry_stage_draws_each_placed_part_under_its_own_name():
    pl = place(read_ecxml(set_top_box().xml()))
    skin = placed_skin(pl)
    names = [p["name"] for p in skin["patches"]]
    assert len(names) == len(set(names)) == 8
    assert {"Case", "PCB", "SoC", "SoC_heat_sink", "Bulk_cap"} <= set(names)
