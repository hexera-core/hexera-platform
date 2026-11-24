# Responsibility: Verify INS-14 - a solid declared a body whose ports are plain discs is seeded in the cavity
# those ports open into when one is proven (a box with an inner chamber, its port bores ending on discs),
# never in its metal; a solid rod with disc ports still seeds in the rod, as before.
# Boundaries: real OpenCASCADE solids written to STEP; the B-rep internal staging (cad_tessellate); no mesher.
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.engines.port_binding import declaration_targets


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _ports(xs, d=46.0):
    return [{"name": n, "type": t, "diameter_mm": d, "near_mm": [x, 0.0, 0.0]}
            for (n, t), x in zip((("inlet", "inlet"), ("outlet", "outlet")), xs)] + [{"name": "wall", "type": "wall"}]


def _stage(path: Path, ports: list, tmp: Path) -> dict:
    return tessellate_internal(path, tmp / "staged", prepared=_prepared(path), fluid_solid=False,
                               declared_ports=declaration_targets(ports))


def test_a_box_with_a_chamber_seeds_in_the_chamber(tmp_path):
    """A 200 mm box with a 150 mm chamber; each side wall bored 46 mm from the chamber out to a 15 mm disc
    that closes the bore (the port the user declared on it). The chamber and bores are the flow."""
    box = BRepAlgoAPI_Cut(_box(-100, -100, -100, 200, 200, 200), _box(-75, -75, -75, 150, 150, 150)).Shape()
    for x0, d in ((-85.0, 1.0), (85.0, -1.0)):
        bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x0, 0, 0), gp_Dir(d, 0, 0)), 23.0, 11.0).Shape()
        box = BRepAlgoAPI_Cut(box, bore).Shape()
    t = _stage(_write(box, tmp_path / "box.step"), _ports((-85.0, 85.0)), tmp_path)
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    in_chamber = max(abs(x), abs(y), abs(z)) < 75.0
    in_bore = 74.0 <= abs(x) <= 85.0 and (y * y + z * z) ** 0.5 < 23.0
    assert in_chamber or in_bore, (x, y, z)
    assert t["cavity_seeded"] is True and t["wall_bounds_fluid"] is False


def test_a_rod_with_disc_ports_still_seeds_in_the_rod(tmp_path):
    rod = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 23.0, 300.0).Shape()
    t = _stage(_write(rod, tmp_path / "rod.step"), _ports((0.0, 300.0)), tmp_path)
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert 0.0 < x < 300.0 and (y * y + z * z) ** 0.5 < 23.0
    assert t["cavity_seeded"] is False
