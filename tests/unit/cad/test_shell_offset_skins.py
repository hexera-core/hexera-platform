# Responsibility: Hold the extraction rules SHELL-50 to SHELL-52 (cad/midsurface.py, cad/shell_pairs.py) to parts of
# KNOWN geometry: a stamped panel's two free-form skins a wall apart get the offset of one as their mid-face (even or
# not quite even), a pair whose wall varies too much does not; a pair the rays read as faces lying on each other is
# given back when the plain reading saw a wall; a body past the quick test's triangle budget is read by the exact
# classifier, to the same answer.
# Boundaries: parts and faces are built in the test (tests/shell_fixtures.py); OpenCASCADE only. No network, no store,
# no model, no mesher.
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import fea_features as ff  # noqa: E402
from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import shell_pairs as sp  # noqa: E402


def _skins(shape):
    """The panel's two skins (its two largest faces): the smaller first, as the extraction reads a pair."""
    big = sorted(ms._faces(shape), key=ms._area)[-2:]
    return big[0], big[1]


def _all_checks(rec: dict) -> None:
    assert rec["checks"] == {"fidelity": True, "connected": True, "faces_accounted": True, "volume": True}, rec["checks"]


# ------------------------------------------------------------------ SHELL-50: offset free-form skins ----
def test_two_b_spline_skins_are_offset_skins_and_two_plates_are_not():
    a, b = _skins(sf.stamped_skins(step=1.0))
    assert ms.offset_skins(a, b)
    p, q = _skins(sf.flat_plate(3.0))
    assert not ms.offset_skins(p, q)


def test_an_even_skin_pair_whose_fit_is_off_the_middle_has_the_offset_of_one_as_its_mid_face():
    # 1 mm skins written through a 1 mm grid: an even wall (within 2%), but the 17 / 33 point fit misses the ramps
    a, b = _skins(sf.stamped_skins(step=1.0))
    ok, t, _spread, why = sp.even_wall(a, b, 1.0)
    assert ok and not why and t == pytest.approx(1.0, rel=1e-3)
    fit = ms.fitted_mid_refined(a, b, 1.0, 1e-4, wall_wide=True)
    assert not fit[0] and fit[4].startswith(ms.FIT_OFF_MIDDLE)
    got, lo, hi, mean, why = ms.offset_mid(a, b, 1.0, 1e-4)
    assert len(got) == 1 and not why
    assert 0.98 <= lo <= mean <= hi <= 1.0 + 1e-3
    _worst, rel, n, _flo, _fhi = ms.fitted_fidelity(got[0], a, b)
    assert n >= 9 and rel <= ms.FIDELITY_SHARE


def test_a_wall_that_varies_past_the_limit_has_no_offset_mid_face():
    # the same panel through a 6 mm grid: the skins wave 0.79 to 1.01 mm apart, 28% - no one offset is its middle
    a, b = _skins(sf.stamped_skins(step=6.0))
    got, _lo, _hi, _mean, why = ms.offset_mid(a, b, 1.0, 1e-4)
    assert not got and why.startswith("the wall varies by")


def test_a_stamped_panel_whose_big_skins_are_an_even_wall_is_one_shell(monkeypatch):
    # SHELL-50c: read as an offset only for big skins - this small fixture stands in for one with the line at 0 poles
    monkeypatch.setattr(ms, "OFFSET_EVEN_POLES", 0)
    rec, _ = ms.extract(sf.stamped_skins(step=1.0), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    (mid,) = [m for m in rec["mids"] if m["shell"]]
    assert mid["thickness_mm"] == pytest.approx(1.0, rel=1e-3)
    # SHELL-50b: the skins are within 2% (an even wall) but vary by over 1%: read like a fitted wall
    assert mid.get("fitted") and 0.98 < mid["thickness_range_mm"][0] < mid["thickness_range_mm"][1] <= 1.001


def test_a_small_even_skin_pair_is_read_exactly_as_before(monkeypatch):
    # SHELL-50c: skins under OFFSET_EVEN_POLES poles never take the offset when the pair is even
    a, b = _skins(sf.stamped_skins(step=1.0))
    assert ms.offset_skins(a, b) and not ms.big_skins(a, b)
    rec, _ = ms.extract(sf.stamped_skins(step=1.0), mm_per_unit=1.0)
    monkeypatch.setattr(ms, "OFFSET_SKINS", False)
    before, _ = ms.extract(sf.stamped_skins(step=1.0), mm_per_unit=1.0)
    for key in ("bodies", "pairs", "mids", "sewn"):
        assert rec[key] == before[key], key


def test_a_tapered_skin_pair_whose_fit_is_off_the_middle_takes_the_offset(monkeypatch):
    # through a 3 mm grid the skins are 0.98 to 1.02 mm apart, turning 5 degrees: a tapered wall. Its fit is made
    # here through 5 points a side, too few for the swage's ramps, so it lies off the middle; the offset does not
    a, b = _skins(sf.stamped_skins(step=3.0))
    ok, lo, hi, _turn, _why = sp.tapered_wall(a, b, 1.0)
    assert ok and 0.97 < lo < hi < 1.03
    monkeypatch.setattr(ms, "FIT_SAMPLES", 5)
    monkeypatch.setattr(ms, "FIT_REFINEMENTS", 0)
    rec, _ = ms.extract(sf.stamped_skins(step=3.0), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    (pair,) = [p for p in rec["pairs"] if p["shell"]]
    assert pair.get("tapered")
    (mid,) = [m for m in rec["mids"] if m["shell"]]
    assert mid["fitted"] and 0.97 < mid["thickness_range_mm"][0] < mid["thickness_range_mm"][1] < 1.03
    # without SHELL-50 the same fit leaves the panel with no wall at all
    monkeypatch.setattr(ms, "OFFSET_SKINS", False)
    rec, _ = ms.extract(sf.stamped_skins(step=3.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "solid"


# ------------------------------------------------------------------ SHELL-51: no coincidence across a wall ----
def _fake_reach(lying_pair: tuple[int, int] | None):
    """wall_reach as the coarse tessellation of a big thin skin reads it: with the lying-on pass (a surface with its
    tessellation errors) every ray of faces 0 and 1 is read through; the plain pass reads the wall between them."""
    def reach(s, origins, dirs, labels, *, eps, lying_on=None, exit_label=None, ray_tol=0.0):
        other = np.where(labels == 0, 1, np.where(labels == 1, 0, -1))
        if s.defl_mm is not None and lying_pair is not None:
            if lying_on is not None:
                lying_on.append(lying_pair)
            exit_label[:] = -1
        else:
            exit_label[:] = other
        return np.full(len(origins), 1.0)
    return reach


def _two_face_surface():
    from meshpipeline.cad import fea_measure as fm

    s, _faces, _kinds = fm._brep_surface(sf.flat_plate(3.0), 1.0)
    big = sorted(range(len(s.names)), key=lambda k: -float(s.areas[s.label == k].sum()))[:2]
    lab = np.where(s.label == big[0], 0, np.where(s.label == big[1], 1, 2))
    from dataclasses import replace

    return replace(s, label=lab, names=["Face_1", "Face_2", "Face_3"],
                   defl_mm=np.array([5.0, 5.0, 0.0]))


def test_a_pair_the_lying_on_pass_loses_is_given_back_when_the_plain_pass_reads_a_wall(monkeypatch):
    s = _two_face_surface()
    monkeypatch.setattr(ff, "wall_reach", _fake_reach((0, 1)))
    restored: set = set()
    got = sp.ray_partners(s, restored)
    assert (0, 1) in got and restored == {(0, 1)}
    monkeypatch.setattr(sp, "COINCIDENT_RESCUE", False)
    assert (0, 1) not in sp.ray_partners(s)


def test_a_part_with_no_faces_lying_on_each_other_is_read_as_before(monkeypatch):
    s = _two_face_surface()
    monkeypatch.setattr(ff, "wall_reach", _fake_reach(None))
    restored: set = set()
    got = sp.ray_partners(s, restored)
    assert (0, 1) in got and not restored


# ------------------------------------------------------------------ SHELL-52: the quick test's budget ----
def test_the_quick_triangulation_is_estimated_from_the_pairings_one():
    from dataclasses import replace

    s = _two_face_surface()
    n = np.bincount(s.label, minlength=3)
    wall = 1.0
    d = ms.QUICK_DEFLECTION * wall
    # a face whose tessellation is 5 mm off grows by 5 / d; a flat one (0 mm) keeps its count
    assert ms.quick_triangles(s, [0, 1, 2], wall) == pytest.approx(n[0] * 5.0 / d + n[1] * 5.0 / d + n[2])
    assert ms.quick_triangles(replace(s, defl_mm=None), [0, 1, 2], wall) == 0.0


def test_a_body_past_the_budget_is_read_by_the_exact_classifier_to_the_same_shell(monkeypatch):
    shape = sf.plate_with_hole(3.0)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    built: list = []
    real = ms._QuickInside

    def counted(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ms, "_QuickInside", counted)
    monkeypatch.setattr(ms, "QUICK_MAX_TRIANGLES", 0)
    again, _ = ms.extract(shape, mm_per_unit=1.0)
    assert not built
    for key in ("bodies", "pairs", "mids", "sewn", "checks"):
        assert again[key] == rec[key], key
