# Responsibility: Verify SHMESH-52b (engines/shell_places): a picked face or a named hole that the shell collapses to an edge (a hole's bore, the plate's edge wall) is sized on the mid-surface curves lying on it - found by distance to the face's own triangles, a line that only runs up to the face is not one - told not applied with why when no curve lies on it, and measured on the elements touching that edge.
# Boundaries: pure numpy over hand-made shapes; no gmsh, no model.
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np

from meshpipeline.engines import shell_places as SP

R, T = 10.0, 3.0          # a 10 mm hole through a 3 mm plate (z 0..3), its rim at z = 1.5 on the mid-plane


def _bore(n: int = 48) -> list:
    """The bore's triangles (mm): a cylinder r = R, z 0..T."""
    a = np.linspace(0.0, 2 * math.pi, n + 1)
    out = []
    for a0, a1 in zip(a[:-1], a[1:], strict=True):
        p = [[R * math.cos(a0), R * math.sin(a0), 0.0], [R * math.cos(a1), R * math.sin(a1), 0.0],
             [R * math.cos(a1), R * math.sin(a1), T], [R * math.cos(a0), R * math.sin(a0), T]]
        out += [[p[0], p[1], p[2]], [p[0], p[2], p[3]]]
    return out


def _rim(r: float = R, n: int = 41) -> np.ndarray:
    a = np.linspace(0.0, 2 * math.pi, n)
    return np.c_[r * np.cos(a), r * np.sin(a), np.full(n, T / 2)]


def _places(tris) -> dict:
    target = {"id": "Pick_1", "name": "the face you picked (Face 7 · round Ø20 mm)", "area": True, "kind": "picked",
              "faces": ["Face_7"], **({"tris_mm": tris} if tris else {})}
    return {"status": "used", "entries": [{"quote": "1 mm here", "ask": "size", "min_mm": 1.0, "max_mm": 1.0,
                                           "targets": [target]}]}


def _plan(places: dict) -> dict:
    rows = [{"size_mm": 4.0, "wall_mm": T, "rule": "SHMESH-1"}]
    record = {"mids": [{"id": "Mid_1", "body": "Part_1", "faces": ["Face_1", "Face_2"]}]}
    got = SP.plan(rows, ["Mid_1"], record, places, [{"id": "r1", "quote": "1 mm here"}], areas_mm2=[20000.0])
    assert got is not None
    return got


def test_the_rim_lies_on_the_bore_and_a_line_running_up_to_it_does_not():
    line = np.c_[np.linspace(R, 40.0, 41), np.zeros(41), np.full(41, T / 2)]
    other = _rim(r=R + 3.0)                                   # a ring drawn round the hole, 3 mm out
    got = SP.edge_curves({1: _rim(), 2: line, 3: other}, _bore(), SP.EDGE_TOL_WALL * T)
    assert got == [1]
    assert float(SP.tri_dist([[0.0, 0.0, 1.5]], _bore())[0]) > 9.9          # the hole's centre: far from the bore


def test_a_bore_picked_is_planned_on_its_edge_then_sized_or_told():
    got = _plan(_places(_bore()))
    (p,) = got["places"]
    assert got["applied"] is True and p["edge"] is True and p["set_mm"] == 1.0 and got["finest_mm"] == 1.0
    assert got["asks"][0]["places"] == [0] and got["asks"][0]["not_applied"] == []
    SP.edge_found(got, 0, [_rim()], _places(_bore())["entries"][0]["targets"][0])
    assert p["edges"] == 1 and len(p["rim_mm"][0]) == 41
    # no curve on it after all: told, as before - never guessed
    miss = _plan(_places(_bore()))
    SP.edge_found(miss, 0, [], _places(_bore())["entries"][0]["targets"][0])
    assert miss["asks"][0]["places"] == [] and miss["asks"][0]["not_applied"][0]["why"] == \
        "it is not on the shell's mid-surface"


def test_a_face_with_no_triangles_kept_is_still_told_not_applied():
    got = _plan(_places(None))
    assert got["applied"] is False and got["asks"][0]["not_applied"][0]["why"] == "it is not on the shell's mid-surface"


def test_the_row_measures_the_elements_touching_the_edge():
    # a ring of quads round the hole, 1 mm deep, 1 mm along; then a ring of 4 mm quads further out
    a = np.linspace(0.0, 2 * math.pi, 63)[:-1]
    rings = [R, R + 1.0, R + 5.0]
    xyz = np.array([[r * math.cos(t), r * math.sin(t), T / 2] for r in rings for t in a])
    n = len(a)
    quads = [[i * n + k, i * n + (k + 1) % n, (i + 1) * n + (k + 1) % n, (i + 1) * n + k] for i in range(2) for k in range(n)]
    mesh = SimpleNamespace(xyz=xyz, quads=np.array(quads), trias=np.zeros((0, 3), dtype=int), mid=["Mid_1"] * len(quads))
    got = _plan(_places(_bore()))
    SP.edge_found(got, 0, [_rim()], _places(_bore())["entries"][0]["targets"][0])
    row = SP.row("r1", got, mesh)
    assert row["applied"] is True and row["met"] is True, row
    assert 0.9 <= row["measured"] <= 1.1 and "on a shell it is an edge; the elements along it are about 1" in row["delivered"]
    assert f"({n} elements)" in row["delivered"]


def test_with_shapes_leaves_a_reading_with_no_hole_or_pick_as_it_is():
    places = {"status": "used", "entries": [{"quote": "5 mm on part 2", "ask": "size", "min_mm": 5.0, "max_mm": 5.0,
                                             "targets": [{"id": "Part_2", "area": False}]}]}
    assert SP.with_shapes(places, None) is places and "tris_mm" not in str(places)
