# Responsibility: Verify gmsh reads an internal flow's passage on its wall, never across a lid the wall meets at a slant.
from __future__ import annotations

import math

import numpy as np
import pytest

gmsh = pytest.importorskip("gmsh")

import meshpipeline.engines.gmsh.driver as D  # noqa: E402
from meshpipeline.cad.internal_surface import stage_triangles, write_staged  # noqa: E402

SIDES = 48
BORE, LIP, BORE_LEN, CONE_LEN = 0.014, 0.020, 0.10, 0.006   # a 45-degree cone from 28 to 40 mm


def _open_nozzle():
    """A thin-walled nozzle open at both ends: a 28 mm bore flaring through a 45-degree cone to a
    40 mm lip, the exit's lid meeting the cone at 45 degrees (the rocket nozzle's exit)."""
    profile = [(0.0, BORE), (BORE_LEN, BORE), (BORE_LEN + CONE_LEN, LIP)]
    rows = []
    for (x0, r0), (x1, r1) in zip(profile, profile[1:]):
        n = max(1, round(math.hypot(x1 - x0, r1 - r0) / 0.003))
        rows += [(x0 + (x1 - x0) * j / n, r0 + (r1 - r0) * j / n) for j in range(n)]
    rows.append(profile[-1])
    tris = []
    for (xa, ra), (xb, rb) in zip(rows, rows[1:]):
        for k in range(SIDES):
            a0, a1 = 2 * math.pi * k / SIDES, 2 * math.pi * (k + 1) / SIDES
            p = [(x, r * math.cos(t), r * math.sin(t)) for (x, r), t in
                 (((xa, ra), a0), ((xb, rb), a0), ((xb, rb), a1), ((xa, ra), a1))]
            tris += [(p[0], p[1], p[2]), (p[0], p[2], p[3])]
    return np.asarray(tris, dtype=float)


def _staged(tmp_path):
    ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 2000 * BORE},
             {"name": "outlet", "type": "outlet", "near_mm": [1000 * (BORE_LEN + CONE_LEN), 0, 0],
              "diameter_mm": 2000 * LIP},
             {"name": "wall", "type": "wall"}]
    rec = write_staged(stage_triangles(_open_nozzle(), intake_patches=ports), tmp_path / "staged")
    (tmp_path / "flow_topology").write_text("internal")
    return rec


def _field(tmp_path, *, on_the_wall: bool):
    rec = _staged(tmp_path)
    spec = {"groups": [{"name": "wall", "role": "wall", "surface_tags": [1]},
                       {"name": "inlet", "role": "inlet", "surface_tags": [2]},
                       {"name": "outlet", "role": "outlet", "surface_tags": [3]}]}
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("t")
        D._load_fluid_boundary(gmsh, rec["fluid_boundary"])
        names = {gmsh.model.getEntityName(2, t): t for _, t in gmsh.model.getEntities(2)}
        for g in spec["groups"]:
            g["surface_tags"] = [names.get(g["name"], g["surface_tags"][0])]
            gmsh.model.addPhysicalGroup(2, g["surface_tags"], name=g["name"])
        walls = D._wall_surfaces(gmsh, spec) if on_the_wall else None
        diag = 0.12
        _cb, pts, r, why = D._passage_field(gmsh, tmp_path, 0.004, diag, discrete=True, walls=walls)
        return walls, np.asarray(pts), np.asarray(r), why
    finally:
        gmsh.finalize()


def test_the_wall_surfaces_are_the_wall_groups(tmp_path):
    walls, pts, r, why = _field(tmp_path, on_the_wall=True)
    assert walls is not None and len(walls) == 1 and "on the wall" in why
    # only the wall's own points are measured: none of them lies on a lid's face
    assert np.all((pts[:, 0] > -1e-9) & (pts[:, 0] < BORE_LEN + CONE_LEN + 1e-9))


def test_a_cone_meeting_its_lid_at_a_slant_is_read_as_the_bore_it_is(tmp_path):
    """Read across the exit lid, the 45-degree cone's chords shrank to nothing at the lid's rim
    and the field's floor spread them over the bore. On the wall, the narrowest reading is the
    bore itself (14 mm), the widest the lip (20 mm)."""
    _, _, r_wall, _ = _field(tmp_path, on_the_wall=True)
    assert np.percentile(r_wall, 5) > 0.8 * BORE
    assert r_wall.max() <= 1.2 * LIP
    # the same boundary read across its lids: what sized the nozzle's 7.2 M tets
    _, _, r_all, _ = _field(tmp_path / "whole", on_the_wall=False)
    assert np.percentile(r_all, 5) < 0.5 * BORE


def test_a_spec_with_no_wall_group_measures_the_whole_boundary_as_before(tmp_path):
    gmsh.initialize(interruptible=False)
    try:
        gmsh.model.add("t")
        assert D._wall_surfaces(gmsh, {"groups": [{"name": "x", "role": "inlet"}]}) is None
    finally:
        gmsh.finalize()
