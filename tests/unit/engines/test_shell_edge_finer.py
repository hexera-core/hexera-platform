# Responsibility: Verify SHMESH-52c (engines/shell_places): "finer" / "coarser" with no number at a place the shell makes an edge of (a hole's wall, a notch, a named hole or group of holes) is applied - the size the mesh would have had along that edge halved or doubled (coarser no more than the plate round it and 8 round a hole, else told not applied with why) - and its row is met only when the elements along the edge came out clearly finer (coarser) than before, with the plate round it measured beside them.
# Boundaries: pure numpy over hand-made shapes; no gmsh, no model.
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
from tests.unit.engines.test_shell_edge_places import R, T, _bore, _rim

from meshpipeline.engines import shell_places as SP


def _places(ask: str) -> dict:
    target = {"id": "Feature_4", "name": "a hole, 20 mm across", "area": True, "kind": "hole", "faces": ["Face_7"],
              "tris_mm": _bore()}
    return {"status": "used", "entries": [{"quote": "finer at the big hole", "ask": ask, "targets": [target]}]}


def _plan(ask: str) -> dict:
    rows = [{"size_mm": 4.0, "wall_mm": T, "rule": "SHMESH-1"}]
    record = {"mids": [{"id": "Mid_1", "body": "Part_1", "faces": ["Face_1", "Face_2"]}]}
    got = SP.plan(rows, ["Mid_1"], record, _places(ask), [{"id": "r1", "quote": "finer at the big hole"}],
                  areas_mm2=[20000.0])
    assert got is not None
    return got


def _mesh(along: float = 1.0) -> SimpleNamespace:
    """A ring of quads round the hole `along` mm deep and long, then a ring of 4 mm quads further out (the plate)."""
    n = max(8, int(round(2 * math.pi * R / along)))
    a = np.linspace(0.0, 2 * math.pi, n + 1)[:-1]
    rings = [R, R + along, R + along + 4.0]
    xyz = np.array([[r * math.cos(t), r * math.sin(t), T / 2] for r in rings for t in a])
    quads = [[i * n + k, i * n + (k + 1) % n, (i + 1) * n + (k + 1) % n, (i + 1) * n + k] for i in range(2)
             for k in range(n)]
    return SimpleNamespace(xyz=xyz, quads=np.array(quads), trias=np.zeros((0, 3), dtype=int),
                           mid=["Mid_1"] * len(quads))


def test_finer_at_a_hole_is_planned_on_its_edge_not_told_not_applied():
    got = _plan("refine")
    (p,) = got["places"]
    assert got["applied"] is True and p["edge"] is True and p["scale"] == 0.5 and p["rule"] == "SHMESH-52c"
    assert got["asks"][0]["places"] == [0] and got["asks"][0]["not_applied"] == []
    assert "set_mm" not in p and got["finest_mm"] is None          # the mesher sets it, from the size there


def test_finer_halves_the_size_there_and_coarser_stops_at_the_plate_and_the_holes_roundness():
    got = _plan("refine")
    SP.edge_found(got, 0, [_rim()], _places("refine")["entries"][0]["targets"][0])
    by = SP.edge_scaled(got, 0, [5, 6], [2.0, 3.0], [4.0, 4.0], ["Mid_1"])
    assert by == {1.0: [5], 1.5: [6]}
    p = got["places"][0]
    assert p["before_mm"] == 2.5 and p["set_mm"] == 1.25 and p["mids"] == ["Mid_1"] and p["curves"] == [5, 6]
    # coarser: doubled, but no more than the plate round it / 8 round the hole
    co = _plan("coarsen")
    SP.edge_found(co, 0, [_rim()], _places("coarsen")["entries"][0]["targets"][0])
    assert SP.edge_scaled(co, 0, [5, 6], [1.0, 3.0], [4.0, 4.0], ["Mid_1"]) == {2.0: [5], 4.0: [6]}
    # already at the most it can take everywhere: told not applied, with why
    no = _plan("coarsen")
    SP.edge_found(no, 0, [_rim()], _places("coarsen")["entries"][0]["targets"][0])
    assert SP.edge_scaled(no, 0, [5], [4.0], [4.0], ["Mid_1"]) == {}
    assert no["asks"][0]["places"] == [] and no["asks"][0]["not_applied"][0]["why"] == SP.NO_COARSER_WHY


def _row(ask: str, along: float, own: float) -> dict:
    got = _plan(ask)
    SP.edge_found(got, 0, [_rim()], _places(ask)["entries"][0]["targets"][0])
    SP.edge_scaled(got, 0, [5], [own], [4.0], ["Mid_1"])
    row = SP.row("r1", got, _mesh(along))
    assert row is not None
    return row


def test_the_row_says_the_edge_against_the_plate_and_is_met_only_when_clearly_finer():
    row = _row("refine", 1.0, 2.0)                      # built about 1 mm, it would have been 2 mm: clearly finer
    assert row["applied"] is True and row["met"] is True, row
    assert "on a shell it is an edge; elements along it are about 1" in row["delivered"]
    assert "the plate round it about" in row["delivered"] and "2 mm, halved to 1 mm" in row["delivered"]
    same = _row("refine", 1.0, 1.1)                     # built about as before: not met, and said
    assert same["applied"] is True and same["met"] is False
    assert "did not come out clearly finer than before" in same["delivered"]


def test_coarser_is_met_only_when_clearly_coarser_than_before():
    assert _row("coarsen", 2.0, 0.9)["met"] is True     # 2 mm along it, it would have been 0.9 mm
    assert _row("coarsen", 1.0, 0.9)["met"] is False    # about as before
