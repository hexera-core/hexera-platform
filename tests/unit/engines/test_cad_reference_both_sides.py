"""VER-14: the CAD check holds an internal mesh to the fluid's own boundary, never to the part's whole
surface with the lids on (cfMesh's geom.stl on a pipe holds the outside of the wall too).

Batch 7 (cfMesh, abc 00188110 / 00189603): "49% / 27% of your CAD surface is not reached" on meshes that
miss 0.0% of the fluid's boundary - the reference was 199 / 349 cm2 round 100 / 174 cm2 of fluid wall."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import cad_fidelity as F

A, B, L = 0.01, 0.02, 0.1          # bore radius, outer radius, length (m)
N_AROUND, N_ALONG = 48, 40


def _pipe():
    """(points, {piece: triangles}) of a thick-walled pipe along x, every piece sharing its rims' points."""
    th = 2 * np.pi * np.arange(N_AROUND) / N_AROUND
    xs = np.linspace(0.0, L, N_ALONG + 1)
    pts: list = []

    def ring(x, r):
        i0 = len(pts)
        pts.extend((x, r * np.cos(t), r * np.sin(t)) for t in th)
        return list(range(i0, i0 + N_AROUND))

    bore = [ring(x, A) for x in xs]
    outer = [ring(x, B) for x in xs]
    centre = []
    for x in (0.0, L):
        centre.append(len(pts))
        pts.append((x, 0.0, 0.0))
    nxt = [(i + 1) % N_AROUND for i in range(N_AROUND)]

    def band(r0, r1):
        out = []
        for i in range(N_AROUND):
            j = nxt[i]
            out += [(r0[i], r0[j], r1[j]), (r0[i], r1[j], r1[i])]
        return out

    pieces = {
        "bore": [t for k in range(N_ALONG) for t in band(bore[k], bore[k + 1])],
        "outer": [t for k in range(N_ALONG) for t in band(outer[k], outer[k + 1])],
        "ends": band(bore[0], outer[0]) + band(bore[-1], outer[-1]),
        "lids": [(centre[e], r[i], r[nxt[i]]) for e, r in ((0, bore[0]), (1, bore[-1])) for i in range(N_AROUND)],
    }
    return np.asarray(pts, dtype=float), pieces


def _surface(*names, upto: float | None = None) -> F.Surface:
    pts, pieces = _pipe()
    tris = np.asarray([t for n in names for t in pieces[n]], dtype=np.int64)
    if upto is not None:
        tris = tris[pts[tris].mean(axis=1)[:, 0] <= upto]
    s = F.Surface(pts, tris)
    s.cell_size = np.sqrt(s.areas())
    return s


def _write_stl(path: Path, s: F.Surface) -> Path:
    with path.open("w") as fh:
        fh.write("solid wall\n")
        for t in s.corners():
            fh.write("facet normal 0 0 0\nouter loop\n")
            for v in t:
                fh.write(f"vertex {v[0]:.9g} {v[1]:.9g} {v[2]:.9g}\n")
            fh.write("endloop\nendfacet\n")
        fh.write("endsolid wall\n")
    return path


def test_the_part_surface_with_lids_holds_both_sides_and_the_fluid_boundary_does_not():
    assert F.both_sides(_surface("bore", "outer", "ends", "lids"))
    assert not F.both_sides(_surface("bore", "lids"))
    assert not F.both_sides(F._empty())


def test_a_both_sided_staged_surface_is_replaced_by_the_fluid_boundary(tmp_path, monkeypatch):
    _write_stl(tmp_path / "geom.stl", _surface("bore", "outer", "ends", "lids"))
    fluid = _surface("bore", "lids")
    monkeypatch.setattr(F, "_restaged", lambda ws, intake: fluid)
    cad, source, fluid_side = F.cad_reference(tmp_path, "internal", [{"name": "inlet", "type": "inlet"}])
    assert (source, fluid_side) == ("fluid boundary staged from your file", True)
    assert cad.areas().sum() == pytest.approx(fluid.areas().sum())


def test_with_no_fluid_boundary_staged_it_bounds_the_metal_too(tmp_path, monkeypatch):
    _write_stl(tmp_path / "geom.stl", _surface("bore", "outer", "ends", "lids"))
    monkeypatch.setattr(F, "_restaged", lambda ws, intake: None)
    _, source, fluid_side = F.cad_reference(tmp_path, "internal", None)
    assert (source, fluid_side) == ("staged part surface and lids", False)


def test_a_staged_fluid_boundary_is_still_used_as_it_is(tmp_path, monkeypatch):
    _write_stl(tmp_path / "geom.stl", _surface("bore", "lids"))
    monkeypatch.setattr(F, "_restaged", lambda ws, intake: pytest.fail("not re-staged"))
    _, source, fluid_side = F.cad_reference(tmp_path, "internal", None)
    assert (source, fluid_side) == ("staged fluid boundary", True)


def test_the_bar_does_not_move_a_mesh_missing_half_the_passage_is_still_refused():
    both, fluid = _surface("bore", "outer", "ends", "lids"), _surface("bore", "lids")
    whole_mesh, half_mesh = _surface("bore", "lids"), _surface("bore", "lids", upto=L / 2)
    miss = lambda mesh, ref: F.surface_distance(mesh, mesh, ref)["cad_to_mesh"]["cad_missed_share"]  # noqa: E731
    assert miss(whole_mesh, both) > 0.3                    # the old reference: the outside of the wall "missed"
    assert miss(whole_mesh, fluid) <= F.CAD_MISSED_CONCERN  # the fluid's own boundary: nothing missed
    assert miss(half_mesh, fluid) > F.CAD_MISSED_REBUILD    # half the passage left out: still a rebuild
