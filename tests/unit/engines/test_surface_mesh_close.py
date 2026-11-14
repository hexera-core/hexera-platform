"""SURF-13: a surface file's gaps closed in the shell mesh - the boundary nodes facing each other across the gap
joined half way, a node with no partner put into the edge it faces; no node moved farther than the gap, nothing
folded, nothing past Nastran's own limits; told place by place."""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import shell_quality
from meshpipeline.engines import surface_mesh_close as smc
from meshpipeline.engines.shell_mesh import ShellMesh, edge_census


def _grid(x0: float, x1: float, nx: int, ys: np.ndarray, *, z: float = 0.0, start: int = 0):
    xs = np.linspace(x0, x1, nx + 1)
    xyz = np.array([[x, y, z] for y in ys for x in xs], dtype=float)
    quads = []
    for j in range(len(ys) - 1):
        for i in range(nx):
            a = start + j * (nx + 1) + i
            quads.append([a, a + 1, a + nx + 2, a + nx + 1])
    return xyz, np.array(quads, dtype=np.int64)


def _two(gap: float, *, lift: float = 0.0, overlap: float = 0.0, shift: float = 0.0, size: float = 4.0) -> ShellMesh:
    """Face 1 on x 0..20, face 2 on x 20+gap..40 (or lifted, or running back over face 1), 4 mm quads; face 2's nodes
    along the gap set off by `shift`."""
    n = int(round(40.0 / size))
    ya = np.linspace(0.0, 40.0, n + 1)
    yb = np.clip(np.linspace(0.0, 40.0, n + 1) + shift * np.r_[0, np.ones(n - 1), 0], 0.0, 40.0)
    xa, qa = _grid(0.0, 20.0, 5, ya)
    xb, qb = _grid(20.0 + gap - overlap, 40.0, 5, yb, z=lift, start=len(xa))
    xyz = np.vstack([xa, xb])
    quads = np.vstack([qa, qb])
    k = len(quads)
    mid = ["Mid_1"] * len(qa) + ["Mid_2"] * len(qb)
    return ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(k, 1.2), mid=mid,
                     part=["Part_1"] * k, groups={"Face_1": np.arange(len(qa)), "Face_2": np.arange(len(qa), k),
                                                  "Part_1": np.arange(k)})


def _place(gap: float) -> dict:
    return {"at_mm": [20.0, 20.0, 0.0], "gap_mm": gap, "faces": [["Face_1", "Face_2"]]}


def _sheets(mesh: ShellMesh) -> int:
    users = {}
    for e, c in enumerate(mesh.elements()):
        for i in range(len(c)):
            a, b = int(c[i]), int(c[(i + 1) % len(c)])
            users.setdefault((min(a, b), max(a, b)), []).append(e)
    parent = list(range(mesh.n_elements))

    def root(a):
        while parent[a] != a:
            a = parent[a]
        return a

    for us in users.values():
        for o in us[1:]:
            ra, rb = root(us[0]), root(o)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return len({root(e) for e in range(mesh.n_elements)})


def _free_length(mesh: ShellMesh) -> float:
    free = edge_census(mesh)["free_edges"]
    return float(sum(np.linalg.norm(mesh.xyz[a] - mesh.xyz[b]) for a, b in free))


def test_a_gap_in_the_sheet_is_closed_one_connected_sheet():
    mesh = _two(2.4, shift=1.0)
    assert _sheets(mesh) == 2
    out, rec = smc.close(mesh, [_place(2.4)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert rec["closed"] == 1 and row["closed"], row
    assert _sheets(out) == 1
    # the outline only is free: 2 x 40 + 2 x (40 - gap) mm, give or take the gap
    assert abs(_free_length(out) - (80.0 + 2 * 40.0 - 2.4)) < 2.5
    q = shell_quality.measure(out)
    assert q["valid"] and not q["nastran_past"]
    assert float(row["moved_mm"]) <= 2.4
    assert edge_census(out)["normals_disagree"] == 0
    # every element keeps its groups and its wall
    assert len(out.mid) == out.n_elements and set(out.groups) == {"Face_1", "Face_2", "Part_1"}
    assert len(out.groups["Part_1"]) == out.n_elements


def test_a_step_and_an_overlap_are_closed_nodes_moved_by_at_most_the_gap():
    for mesh, gap in ((_two(0.0, lift=2.1), 2.1), (_two(0.0, overlap=1.5, lift=0.3), 1.5)):
        before = mesh.xyz.copy()
        out, rec = smc.close(mesh, [_place(gap)], reach_mm=4.0, reach_from="asked")
        assert rec["closed"] == 1, rec["places"]
        assert _sheets(out) == 1
        q = shell_quality.measure(out)
        assert q["valid"] and not q["nastran_past"]
        # every node of the closed mesh lies within the gap of where a node was
        d = np.min(np.linalg.norm(out.xyz[:, None, :] - before[None, :, :], axis=2), axis=1)
        assert float(d.max()) <= gap + 1e-9


def test_nodes_with_no_partner_go_into_the_edge_they_face():
    # face 2 meshed twice as fine along the gap: every other node of it has no partner across
    mesh = _two(1.0)
    ya = np.linspace(0.0, 40.0, 11)
    yb = np.linspace(0.0, 40.0, 21)
    xa, qa = _grid(0.0, 20.0, 5, ya)
    xb, qb = _grid(21.0, 40.0, 10, yb, start=len(xa))
    k = len(qa) + len(qb)
    mesh = ShellMesh(xyz=np.vstack([xa, xb]), quads=np.vstack([qa, qb]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.full(k, 1.2), mid=["Mid_1"] * len(qa) + ["Mid_2"] * len(qb), part=["Part_1"] * k)
    out, rec = smc.close(mesh, [_place(1.0)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert row["closed"] and row["put_in"] >= 9, row
    assert _sheets(out) == 1 and shell_quality.measure(out)["valid"]
    assert len(out.trias) > 0                    # the coarse side's edge cut where the fine side's nodes landed


def test_a_gap_wider_than_the_reach_is_left_and_the_mesh_is_untouched():
    mesh = _two(3.0)
    out, rec = smc.close(mesh, [_place(3.0)], reach_mm=2.0, reach_from="asked")
    assert out is mesh and rec["closed"] == 0
    assert rec["places"][0]["why"] == "wider than the reach"


def test_a_join_that_would_fold_elements_is_refused():
    # 0.5 mm elements across a 3 mm gap: half the gap is three elements deep
    mesh = _two(3.0, size=0.5)
    xs_a, qa = _grid(19.0, 20.0, 2, np.linspace(0.0, 4.0, 9))
    xs_b, qb = _grid(23.0, 24.0, 2, np.linspace(0.0, 4.0, 9) + 0.25, start=len(xs_a))
    k = len(qa) + len(qb)
    mesh = ShellMesh(xyz=np.vstack([xs_a, xs_b]), quads=np.vstack([qa, qb]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.full(k, 1.2), mid=["Mid_1"] * len(qa) + ["Mid_2"] * len(qb), part=["Part_1"] * k)
    out, rec = smc.close(mesh, [_place(3.0)], reach_mm=4.0, reach_from="asked")
    assert rec["closed"] == 0 and out is mesh
    assert "fold" in rec["places"][0]["why"] or "limits" in rec["places"][0]["why"]


def test_the_same_mesh_twice_closes_the_same():
    a, _ = smc.close(_two(2.4, shift=1.0), [_place(2.4)], reach_mm=4.0, reach_from="asked")
    b, _ = smc.close(_two(2.4, shift=1.0), [_place(2.4)], reach_mm=4.0, reach_from="asked")
    assert np.array_equal(a.xyz, b.xyz) and np.array_equal(a.quads, b.quads) and np.array_equal(a.trias, b.trias)


def _tee(gap: float, x_wall: float = 18.0) -> ShellMesh:
    """A T: a plate (face 1, x and y 0..40, 4 mm quads) and a wall standing on it at x = x_wall (face 2, y 4..36, 4 mm
    quads) whose foot stops `gap` above the plate - an edge stopping short of a face's inside."""
    xa, qa = _grid(0.0, 40.0, 10, np.linspace(0.0, 40.0, 11))
    ys, zs = np.linspace(4.0, 36.0, 9), np.linspace(gap, gap + 12.0, 4)
    xb = np.array([[x_wall, y, z] for z in zs for y in ys], dtype=float)
    qb = []
    for j in range(len(zs) - 1):
        for i in range(len(ys) - 1):
            a = len(xa) + j * len(ys) + i
            qb.append([a, a + 1, a + len(ys) + 1, a + len(ys)])
    k = len(qa) + len(qb)
    return ShellMesh(xyz=np.vstack([xa, xb]), quads=np.vstack([qa, np.array(qb, dtype=np.int64)]),
                     trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(k, 1.2),
                     mid=["Mid_1"] * len(qa) + ["Mid_2"] * len(qb), part=["Part_1"] * k)


def _tee_place(gap: float) -> dict:
    return {"at_mm": [18.0, 20.0, gap], "gap_mm": gap, "faces": [["Face_2", "Face_1"]]}


def test_a_wall_stopping_short_of_a_plates_inside_is_joined_onto_it():
    # SURF-13, a T: the plate has no free edge there; its row of nodes nearest the foot's line is moved onto the
    # line, the foot's nodes onto the plate, and the two joined - one sheet, the plate's nodes still on the plate
    out, rec = smc.close(_tee(2.5), [_tee_place(2.5)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert row["closed"], row
    assert _sheets(out) == 1 and shell_quality.measure(out)["valid"]
    assert row["moved_mm"] <= 2.5 + 1e-9
    low = out.xyz[:, 2] < 1.0
    assert float(np.max(np.abs(out.xyz[low, 2]))) < 1e-9                        # the plate stays flat
    users: dict = {}
    for e, c in enumerate(out.elements()):
        for i in range(len(c)):
            a, b = int(c[i]), int(c[(i + 1) % len(c)])
            users.setdefault((min(a, b), max(a, b)), []).append(e)
    assert sum(1 for u in users.values() if len(u) == 3) >= 8                   # the foot is a junction, end to end


def test_a_wall_short_of_a_plate_away_from_its_nodes_is_imprinted_on_it():
    # 0.5 mm short over 4 mm elements, the line 2 mm from the plate's nodes: no row of the plate's nodes lies within
    # the gap, so the line is imprinted on the plate (its elements cut along it) and the foot joined to the new row -
    # one sheet, nothing moved farther than the gap, the plate still flat
    before = _tee(0.5)
    out, rec = smc.close(_tee(0.5), [_tee_place(0.5)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert row["closed"], row
    assert _sheets(out) == 1 and shell_quality.measure(out)["valid"]
    assert row["moved_mm"] <= 0.5 + 1e-9
    assert out.n_elements > before.n_elements
    low = out.xyz[:, 2] < 0.25
    assert float(np.max(np.abs(out.xyz[low, 2]))) < 1e-9
    assert not shell_quality.measure(out)["nastran_past"]


def _fine_under(o0: float, o1: float, *, lift: float = 0.3) -> ShellMesh:
    """Face 1 (x 0..20, its last three columns 0.3 mm deep) running o0 mm (at y 0) to o1 mm (at y 20) under face 2,
    which is lifted `lift` and 1 mm rows; met half way, face 1's edge nodes cross its own next column."""
    ys = np.arange(0.0, 20.0 + 1e-9, 1.0)
    xa = [0.0, 4.0, 8.0, 12.0, 16.0, 19.1, 19.4, 19.7, 20.0]
    pts = [[x, y, 0.0] for y in ys for x in xa]
    na = len(xa)
    quads = [[j * na + i, j * na + i + 1, (j + 1) * na + i + 1, (j + 1) * na + i]
             for j in range(len(ys) - 1) for i in range(na - 1)]
    n_a, start, nb = len(quads), len(pts), 5
    for y in ys:
        e = 20.0 - (o0 + (o1 - o0) * y / ys[-1])
        pts += [[e + i * (40.0 - e) / nb, y, lift] for i in range(nb + 1)]
    quads += [[start + j * (nb + 1) + i, start + j * (nb + 1) + i + 1, start + (j + 1) * (nb + 1) + i + 1,
               start + (j + 1) * (nb + 1) + i] for j in range(len(ys) - 1) for i in range(nb)]
    k = len(quads)
    return ShellMesh(xyz=np.array(pts), quads=np.array(quads, dtype=np.int64), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.full(k, 1.2), mid=["Mid_1"] * n_a + ["Mid_2"] * (k - n_a), part=["Part_1"] * k)


def _under_place(gap: float) -> dict:
    return {"at_mm": [20.0, 10.0, 0.0], "gap_mm": gap, "faces": [["Face_1", "Face_2"]]}


def test_one_side_brought_the_whole_gap_is_told_and_held_to_its_real_move():
    # SURF-13: met half way, face 1's fine edge folds; face 1 kept and face 2 brought the WHOLE gap to it (the
    # 0.8 mm overlap and the 0.3 mm lift: 0.854 mm) - the move told is that, not half of it
    mesh = _fine_under(0.8, 0.8)
    out, rec = smc.close(mesh, [_under_place(0.9)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert row["closed"], row
    assert row["moved_mm"] == pytest.approx(float(np.hypot(0.8, 0.3)), abs=2e-3)
    assert _sheets(out) == 1 and shell_quality.measure(out)["valid"]
    # every way of joining is measured on what it did: half way, and each side kept
    mm = smc._Mesh(_two(1.0))
    for onto, real in (("", 0.5), ("b", 1.0), ("a", 1.0)):
        trial, total, _why = smc._attempt(mm, [["Face_1", "Face_2"]], 1.0, 4.0, onto)
        moved = float(np.max(np.linalg.norm(trial.xyz[:len(mm.xyz)] - mm.xyz, axis=1)))
        assert total["moved_mm"] == pytest.approx(real) and moved == pytest.approx(real), onto


def test_one_side_brought_farther_than_the_gap_is_refused():
    # the overlap opens from 1.0 to 1.9 mm along the place (its gap read 1.45 at the middle): face 2 brought the
    # whole way moves its nodes up to 1.84 mm - farther than the gap, though half of it is not
    mesh = _fine_under(1.0, 1.9)
    out, rec = smc.close(mesh, [_under_place(1.45)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert not row["closed"] and out is mesh, row
    trial, total, _why = smc._attempt(smc._Mesh(mesh), [["Face_1", "Face_2"]], 1.45, 4.0, "a")
    assert 1.25 * 1.45 < total["moved_mm"] <= float(np.hypot(1.9, 0.3)) + 1e-9


def test_a_node_put_into_an_edge_moves_that_edge_and_is_measured():
    # face 2 twice as fine along the gap, kept where it is: face 1's edge is brought the whole 1 mm, and face 2's
    # nodes with no partner go into face 1's edges - which then lie 1 mm from where face 1 was
    ya, yb = np.linspace(0.0, 40.0, 11), np.linspace(0.0, 40.0, 21)
    xa, qa = _grid(0.0, 20.0, 5, ya)
    xb, qb = _grid(21.0, 40.0, 10, yb, start=len(xa))
    k = len(qa) + len(qb)
    mesh = ShellMesh(xyz=np.vstack([xa, xb]), quads=np.vstack([qa, qb]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.full(k, 1.2), mid=["Mid_1"] * len(qa) + ["Mid_2"] * len(qb), part=["Part_1"] * k)
    trial, total, _why = smc._attempt(smc._Mesh(mesh), [["Face_1", "Face_2"]], 1.0, 4.0, "b")
    assert total["put_in"] >= 9 and total["moved_mm"] == pytest.approx(1.0)
    assert any(len(p) for p in trial.put)


def _tee_y(gap: float, y0: float, y1: float, n: int, x_wall: float = 18.0) -> ShellMesh:
    """_tee, its wall's foot running y0..y1 in n elements."""
    xa, qa = _grid(0.0, 40.0, 10, np.linspace(0.0, 40.0, 11))
    ys, zs = np.linspace(y0, y1, n + 1), np.linspace(gap, gap + 12.0, 4)
    xb = np.array([[x_wall, y, z] for z in zs for y in ys], dtype=float)
    qb = [[len(xa) + j * len(ys) + i, len(xa) + j * len(ys) + i + 1, len(xa) + (j + 1) * len(ys) + i + 1,
           len(xa) + (j + 1) * len(ys) + i] for j in range(len(zs) - 1) for i in range(len(ys) - 1)]
    k = len(qa) + len(qb)
    return ShellMesh(xyz=np.vstack([xa, xb]), quads=np.vstack([qa, np.array(qb, dtype=np.int64)]),
                     trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(k, 1.2),
                     mid=["Mid_1"] * len(qa) + ["Mid_2"] * len(qb), part=["Part_1"] * k)


def _foot_free_on_plate(mesh: ShellMesh, x_wall: float, gap: float) -> list:
    """The free edges lying along the foot's line on the plate: the foot left unjoined."""
    out = []
    for a, b in edge_census(mesh)["free_edges"]:
        pa, pb = mesh.xyz[a], mesh.xyz[b]
        if max(abs(pa[2]), abs(pb[2])) <= gap + 1e-9 and max(abs(pa[0] - x_wall), abs(pb[0] - x_wall)) < 0.5:
            out.append((float(pa[1]), float(pb[1])))
    return out


def test_a_foot_ending_inside_the_plates_elements_is_joined_end_to_end():
    # the foot runs y 5..35 over plate rows 4 mm apart: the line crosses element edges only from y 8 to 32, so the
    # imprinted row stopped there and the foot's last 3 mm at each end lay on the plate unjoined - told closed. Now
    # the line's ends are imprinted too and the foot is a junction end to end
    for x_wall in (18.0, 17.0):
        out, rec = smc.close(_tee_y(0.5, 5.0, 35.0, 10, x_wall), [_tee_place(0.5)], reach_mm=4.0, reach_from="asked")
        row = rec["places"][0]
        assert row["closed"], row
        assert _foot_free_on_plate(out, x_wall, 0.5) == []
        q = shell_quality.measure(out)
        assert q["valid"] and not q["nastran_past"] and _sheets(out) == 1
        assert row["moved_mm"] <= 0.5 + 1e-9
        low = out.xyz[:, 2] < 0.25
        assert float(np.max(np.abs(out.xyz[low, 2]))) < 1e-9                    # the plate stays flat


def test_a_foot_left_partly_unjoined_is_told_open(monkeypatch):
    # whatever the row reaches, the place is closed only when no stretch of the foot is left lying on the plate
    # unjoined: with the line's ends not imprinted, the 3 mm at each end are left, and the place is told open
    def no_ends(mm, elems, p, tol):
        return None
    monkeypatch.setattr(smc, "_holding", no_ends)
    mesh = _tee_y(0.5, 5.0, 35.0, 10)
    out, rec = smc.close(mesh, [_tee_place(0.5)], reach_mm=4.0, reach_from="asked")
    row = rec["places"][0]
    assert not row["closed"] and out is mesh, row
    assert "not be joined" in row["why"]


def test_a_flat_triangle_zipping_a_seam_is_taken_into_the_element_across_it():
    # a seam with a node more on one side (face 2 has a node half way, 0.02 mm off the line), zipped by a triangle of
    # no area - past Nastran's limits: the node goes into face 1's element edge, the triangle goes
    xyz = np.array([[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0], [2, 4.02, 0], [0, 8, 0], [2, 8, 0], [4, 8, 0]],
                   dtype=float)
    quads = np.array([[0, 1, 2, 3], [3, 4, 6, 5], [4, 2, 7, 6]], dtype=np.int64)
    trias = np.array([[3, 2, 4]], dtype=np.int64)
    mesh = ShellMesh(xyz=xyz, quads=quads, trias=trias, wall=np.full(4, 1.2),
                     mid=["Mid_1", "Mid_2", "Mid_2", "Mid_2"], part=["Part_1"] * 4)
    assert shell_quality.measure(mesh)["nastran_past"]
    out, n = smc.absorb_flat(mesh)
    assert n == 1
    assert not shell_quality.measure(out)["nastran_past"] and shell_quality.measure(out)["valid"]
    assert _sheets(out) == 1 and _free_length(out) == pytest.approx(_free_length(mesh), abs=0.05)
    assert out.n_elements >= 3 and "Mid_1" in out.mid
    again, m = smc.absorb_flat(out)
    assert m == 0 and again is out
