"""SURF-13: a surface file's gaps closed in the shell mesh - the boundary nodes facing each other across the gap
joined half way, a node with no partner put into the edge it faces; no node moved farther than the gap, nothing
folded, nothing past Nastran's own limits; told place by place."""
from __future__ import annotations

import numpy as np

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
