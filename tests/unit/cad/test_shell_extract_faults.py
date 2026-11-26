# Responsibility: Hold the extraction rules from SHELL-55 on (cad/midsurface.py) to parts of KNOWN geometry: a
# funnel's two coaxial cones are one even wall read where they face each other (SHELL-55); a speck is no thick region
# (SHELL-56); a ribbed floor's steps count in its wall region's width (SHELL-57).
# Boundaries: parts are built in the test (tests/shell_fixtures.py); OpenCASCADE only. No network, no store, no
# model, no mesher.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402


def _cone_pair(rec: dict) -> dict:
    kinds = {f["face"]: f["kind"] for f in rec["faces"]}
    (got,) = [p for p in rec["pairs"] if all(kinds[f] == "cone" for f in p["faces"])]
    return got


# ------------------------------------------------------------- SHELL-55: an even wall read where it faces ----
def test_a_funnel_wall_is_one_even_wall_where_its_cones_face_each_other(monkeypatch):
    rec, _s = ms.extract(sf.funnel_with_spout(t=5.0), mm_per_unit=1.0)
    cone = _cone_pair(rec)
    assert cone["even"] and cone["thickness_mm"] == pytest.approx(5.0, rel=1e-6) and cone["shell"]
    assert rec["bodies"][0]["verdict"] in ("shell", "mixed")
    kinds = {m["kind"] for m in rec["mids"] if m.get("shell")}
    assert "cone" in kinds


def test_without_the_rule_the_funnel_had_no_wall(monkeypatch):
    monkeypatch.setattr(ms, "EVEN_WHERE_FACING", False)
    rec, _s = ms.extract(sf.funnel_with_spout(t=5.0), mm_per_unit=1.0)
    cone = _cone_pair(rec)
    assert not cone["even"] and "no mid-surface" in cone["why"]
    assert rec["bodies"][0]["verdict"] == "solid"


def test_a_real_taper_stays_a_taper():
    # a drafted tube 4 to 2.5 mm thick: read where its faces face each other it still varies past EVEN_SHARE
    rec, _s = ms.extract(sf.drafted_tube(), mm_per_unit=1.0)
    walls = [p for p in rec["pairs"] if p.get("tapered")]
    assert walls and not any(p["even"] for p in walls)


# ------------------------------------------------------------- SHELL-56: a speck is no thick region ----
def _body(rec: dict) -> dict:
    (b,) = rec["bodies"]
    return b


def test_a_plate_whose_only_thick_regions_are_specks_reads_shell(monkeypatch):
    rec, surf = ms.extract(sf.plate_with_pins(), mm_per_unit=1.0)
    b = _body(rec)
    assert b["verdict"] == "shell" and "solid_regions" not in b
    assert b["specks"]["regions"] == 4 and b["specks"]["area_mm2"] < 4 * 4.0 * 9.0
    # reporting only: the same mid-surface and record checks as without the rule
    monkeypatch.setattr(ms, "SPECK_FLOOR", False)
    rec0, surf0 = ms.extract(sf.plate_with_pins(), mm_per_unit=1.0)
    assert _body(rec0)["verdict"] == "mixed" and len(_body(rec0)["solid_regions"]) == 4
    assert rec0["surface_faces"] == rec["surface_faces"] and rec0["checks"] == rec["checks"]
    assert rec0["sewn"] == rec["sewn"]


def test_a_real_boss_keeps_the_plate_mixed_and_the_specks_are_counted_apart():
    rec, _s = ms.extract(sf.plate_with_pins(boss=True), mm_per_unit=1.0)
    b = _body(rec)
    assert b["verdict"] == "mixed" and len(b["solid_regions"]) == 1 and b["specks"]["regions"] == 4
    assert b["solid_regions"][0]["area_mm2"] >= 4 * 20 * 20


def test_big_pins_are_thick_regions():
    # 12 mm pins 4 mm high on a 3 mm plate: their tops 113 mm2 each, over 4 x 3^2 - thick metal
    rec, _s = ms.extract(sf.plate_with_pins(r=6.0, h=4.0), mm_per_unit=1.0)
    b = _body(rec)
    assert b["verdict"] == "mixed" and len(b["solid_regions"]) == 4 and "specks" not in b


# ------------------------------------------------- SHELL-57: a wall region's width counts its steps ----
def _ribbed_faces(t: float = 3.5, h: float = 0.5):
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad import shell_pairs as sp

    shape = sf.ribbed_floor(t=t, h=h)
    faces = ms._faces(shape)
    nb = ms._neighbours(shape, ms._map(shape, TopAbs_FACE))
    tol = 1e-6
    boxes = [ms._box(f) for f in faces]

    def flat_at(k, z):
        lo, hi = boxes[k]
        return abs(lo[2] - z) < tol and abs(hi[2] - z) < tol

    bottom = [k for k in range(len(faces)) if flat_at(k, 0.0)]
    strips = [k for k in range(len(faces)) if flat_at(k, t)]
    tops = [k for k in range(len(faces)) if flat_at(k, t + h)]
    # the ribs' sides and ends: the faces standing between the floor's top and the ribs' tops
    rib_steps = [k for k in range(len(faces)) if abs(boxes[k][0][2] - t) < tol and abs(boxes[k][1][2] - t - h) < tol]
    rib_sides = [k for k in rib_steps if boxes[k][1][1] - boxes[k][0][1] < tol]
    plate_sides = [k for k in range(len(faces)) if abs(boxes[k][0][2]) < tol and abs(boxes[k][1][2] - t) < tol]
    (b,) = bottom
    pairs = [sp.Pair(a=k, b=b, thickness=t, spread=0.0, rays=10, even=True, width=2.0) for k in strips]
    pairs += [sp.Pair(a=k, b=b, thickness=t + h, spread=0.0, rays=10, even=True, width=2.0) for k in tops]
    # the ribs read sideways: a 2 mm "wall" between a rib's two sides, 0.5 mm wide
    sides = sorted(rib_sides, key=lambda k: boxes[k][0][1])
    pairs += [sp.Pair(a=sides[i], b=sides[i + 1], thickness=2.0, spread=0.0, rays=10, even=True, width=h)
              for i in range(0, len(sides) - 1, 2)]
    by_sheet = {0: list(range(len(strips) + len(tops)))}
    return faces, nb, pairs, by_sheet, set(strips + tops + bottom), set(rib_steps), set(plate_sides)


def test_the_steps_of_a_ribbed_floor_are_taken_in_and_its_rims_are_not():
    faces, nb, pairs, by_sheet, region, rib_steps, plate_sides = _ribbed_faces()
    steps = ms._region_steps(region, [0], by_sheet, pairs, faces, nb, 4.0)
    assert rib_steps and steps == rib_steps
    assert not steps & plate_sides


def test_with_steps_the_ribbed_floor_reads_its_own_width():
    faces, nb, pairs, by_sheet, region, rib_steps, _plates = _ribbed_faces()
    bare = ms.union_width([faces[k] for k in sorted(region)])
    stepped = ms.union_width([faces[k] for k in sorted(region | rib_steps)])
    assert bare < 10.0 < stepped                     # 40 mm wide: strips alone read a few millimetres
