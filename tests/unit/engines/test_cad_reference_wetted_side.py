"""VER-15 / VER-16: the CAD check holds an internal mesh to the fluid's side of the surface the engine was staged
with (the space the confirmed openings' lids close off from the outside air), before any re-staging; and a
share not reached that could not be judged is never read as met.

Batch 7: re-staged from the metre surface, abc 00157383 got its inlet lid 2 mm inside the bore (a 57 mm loop, not
the confirmed 61.07 mm mouth) and abc 00131564 the part's metal (23.0 cm3 against the 59.8 cm3 fluid); abc 00157702
could not be re-staged and passed with the share not reached unjudged."""
from __future__ import annotations

import numpy as np
import pytest
from test_cad_reference_both_sides import L, _pipe, _write_stl

from meshpipeline.engines import cad_fidelity as F

CELL = 0.002


def _piece(*names, where=None) -> F.Surface:
    pts, pieces = _pipe()
    tris = np.asarray([t for n in names for t in pieces[n]], dtype=np.int64)
    if where is not None:
        tris = tris[where(pts[tris].mean(axis=1)[:, 0])]
    s = F.Surface(pts, tris)
    s.cell_size = np.sqrt(s.areas())
    return s


def _parts(outlet: bool = True) -> dict[str, F.Surface]:
    out = {"wall": _piece("bore", "outer", "ends"), "inlet": _piece("lids", where=lambda x: x < L / 2)}
    if outlet:
        out["outlet"] = _piece("lids", where=lambda x: x > L / 2)
    return out


PORTS = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"}, {"name": "wall", "type": "wall"}]


def test_the_fluids_side_keeps_the_bore_and_the_lids_and_not_the_outside_of_the_wall():
    wet = F.wetted_side(_parts(), ["inlet", "outlet"], CELL)
    assert wet is not None
    fluid = _piece("bore", "lids").areas().sum()
    assert wet.areas().sum() == pytest.approx(fluid, rel=0.02)
    outer = _piece("outer")
    assert F._distance(outer.centroids(), wet).min() > 0.005      # no face of the outside kept


def test_a_wall_thinner_than_the_band_keeps_its_bore_face_and_drops_its_outer_face(monkeypatch):
    # a 0.6 mm wall read on a 1 mm grid: both faces lie by the fluid and by the air (batch 7 cfMesh meshes read
    # 1.1-1.3% "not reached" on such outer faces); each looks away from the face across the wall
    import test_cad_reference_both_sides as P
    monkeypatch.setattr(P, "B", P.A + 0.0006)
    wet = F.wetted_side(_parts(), ["inlet", "outlet"], CELL)
    assert wet is not None
    bore, outer = _piece("bore"), _piece("outer")
    kept = F._distance(bore.centroids(), wet)
    assert np.mean(kept < 1e-9) > 0.98                              # the bore is kept
    assert wet.areas().sum() == pytest.approx(_piece("bore", "lids").areas().sum(), rel=0.05)
    whole = _piece("bore", "lids")
    assert F.surface_distance(whole, whole, wet)["cad_to_mesh"]["cad_missed_share"] <= F.CAD_MISSED_CONCERN
    assert outer.n


def test_a_lid_with_outside_air_on_both_sides_cannot_tell_the_fluid():
    # the outlet left open: the outside air floods the bore, so the inlet's lid closes nothing off
    assert F.wetted_side(_parts(outlet=False), ["inlet"], CELL) is None
    assert F.wetted_side(_parts(), [], CELL) is None


def test_the_engines_own_staged_surface_is_read_before_any_re_staging(tmp_path, monkeypatch):
    stls = tmp_path / "_internal_stls"
    stls.mkdir()
    for name, s in _parts().items():
        _write_stl(stls / f"{name}.stl", s)
    _write_stl(tmp_path / "geom.stl", _piece("bore", "outer", "ends", "lids"))
    monkeypatch.setattr(F, "_restaged", lambda ws, intake: pytest.fail("re-staged"))
    cad, source, fluid_side = F.cad_reference(tmp_path, "internal", PORTS, cell=CELL)
    assert (source, fluid_side) == (F.WETTED_SIDE_SOURCE, True)
    assert cad.areas().sum() == pytest.approx(_piece("bore", "lids").areas().sum(), rel=0.02)


def test_when_the_fluids_side_cannot_be_told_the_re_staging_is_next(tmp_path, monkeypatch):
    stls = tmp_path / "_internal_stls"
    stls.mkdir()
    for name, s in _parts(outlet=False).items():
        _write_stl(stls / f"{name}.stl", s)
    fluid = _piece("bore", "lids")
    monkeypatch.setattr(F, "_restaged", lambda ws, intake: fluid)
    _, source, fluid_side = F.cad_reference(tmp_path, "internal", PORTS[:1], cell=CELL)
    assert (source, fluid_side) == ("fluid boundary staged from your file", True)


def test_a_share_not_reached_that_was_not_judged_is_a_concern_said_in_words(tmp_path, monkeypatch):
    wall = _piece("bore")
    monkeypatch.setattr(F, "mesh_patches", lambda ws: {"wall": wall, "inlet": _parts()["inlet"]})
    monkeypatch.setattr(F, "cad_reference", lambda ws, flow, intake, cell=0.0: (
        F.join(list(_parts().values())), "staged part surface and lids", False))
    rec = F.measure(tmp_path, intake_patches=[], manifest={"flow_topology": "internal"})
    assert rec["verdicts"]["mesh_to_cad"] == F.PASS
    assert rec["verdicts"]["cad_to_mesh"] == F.CONCERN
    assert rec["verdict"] == F.CONCERN
    assert any("was not judged" in w and "not counted as met" in w for w in F.concerns(rec))


def test_the_bar_does_not_move_half_the_passage_missed_on_the_fluids_side_is_still_a_rebuild():
    wet = F.wetted_side(_parts(), ["inlet", "outlet"], CELL)
    assert wet is not None
    half = _piece("bore", "lids", where=lambda x: x <= L / 2)
    whole = _piece("bore", "lids")
    miss = lambda mesh: F.surface_distance(mesh, mesh, wet)["cad_to_mesh"]["cad_missed_share"]  # noqa: E731
    assert miss(whole) <= F.CAD_MISSED_CONCERN
    assert miss(half) > F.CAD_MISSED_REBUILD
