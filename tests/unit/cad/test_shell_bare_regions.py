# Responsibility: Verify SHELL-62, A THICK REGION HOLDS METAL: faces read as thick or solid whose measured metal past
# half a wall of every mid-face (SHELL-60) is under BARE_DEPTH of a wall over their surface are no thick region - a
# ribbed floor's rib sides, read thick, hold none - and a body left with no thick region reads shell; real bosses and
# standoffs stay thick; reporting only.
# Boundaries: parts built in the test (tests/shell_fixtures.py); the real extraction in process.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad.shell_words import left_out  # noqa: E402


def test_faces_read_thick_that_hold_no_metal_are_no_thick_region(monkeypatch):
    """A 3.5 mm floor with 0.5 mm ribs: the ribs' sides and ends were seven "thick regions" (924 mm2) holding 0 mm3 past the
    walls, so a shell said seven regions were left out. Now the floor is a shell and nothing is left out."""
    rec, surface = ms.extract(sf.ribbed_floor(), mm_per_unit=1.0)
    (b,) = rec["bodies"]
    assert b["verdict"] == "shell" and "solid_regions" not in b and b["bare"]["regions"] == 7
    assert left_out(rec) == {}
    monkeypatch.setattr(ms, "BARE_FLOOR", False)
    before, s_before = ms.extract(sf.ribbed_floor(), mm_per_unit=1.0)
    (b0,) = before["bodies"]
    assert b0["verdict"] == "mixed" and len(b0["solid_regions"]) == 7
    assert all(g["volume_mm3"] == 0.0 for g in b0["solid_regions"])
    # reporting only: the same mid-surface, joins and checks
    assert before["surface_faces"] == rec["surface_faces"] and before["sewn"] == rec["sewn"]
    assert before["checks"] == rec["checks"] and ms._area(surface) == pytest.approx(ms._area(s_before), rel=1e-12)


@pytest.mark.parametrize("make", [sf.plate_with_boss, sf.standoff_plate, sf.plate_with_filleted_pad])
def test_real_thick_metal_stays_a_thick_region(make):
    rec, _ = ms.extract(make(), mm_per_unit=1.0)
    (b,) = rec["bodies"]
    assert b["verdict"] == "mixed" and b["solid_regions"] and "bare" not in b
    assert all(g["volume_mm3"] > ms.BARE_DEPTH * g["area_mm2"] for g in b["solid_regions"])


def test_a_region_whose_metal_was_not_measured_stays_thick(monkeypatch):
    monkeypatch.setattr(ms, "THICK_WORK", 0)          # no reading allowed: no metal measured
    rec, _ = ms.extract(sf.ribbed_floor(), mm_per_unit=1.0)
    (b,) = rec["bodies"]
    assert b["verdict"] == "mixed" and all(g["volume_mm3"] is None for g in b["solid_regions"])
