# Responsibility: Generate the tiny CAD and surface files tests need, on demand under the test's own tmp_path.
# Boundaries: generated per test, never committed - a fixture that outlives its test is state, not input.
from __future__ import annotations

from pathlib import Path

SIDE = 10.0          # 10 coordinate units, whatever the file says those units are


def write_step(path: Path, unit: str) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    box = BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()
    # Construct the writer BEFORE setting the unit. `write.step.unit` is registered by the STEP
    # controller's initialisation, and SetCVal_s against an unregistered static does not raise -
    # it returns False and leaves the value empty, so OCC writes its default millimetre. Setting
    # it first therefore produced a file in the WRONG unit whenever this helper was the process's
    # first STEP contact, and the right one once any earlier test had touched STEP. The unit a
    # fixture claims must never depend on test order, so the result is checked rather than
    # assumed.
    w = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    w.Transfer(box, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.Write(str(path))
    return path


def write_iges(path: Path, unit: str) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IGESControl import IGESControl_Writer

    box = BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()
    path.parent.mkdir(parents=True, exist_ok=True)
    w = IGESControl_Writer(unit, 0)
    w.AddShape(box)
    w.Write(str(path))
    return path


def corrupt_step_unit(src: Path, dest: Path, how: str) -> Path:
    import re
    text = src.read_text(errors="replace")
    replacement = {"malformed": "SI_UNIT(.NOPE.,.METRE.)", "missing": "SI_UNIT($,$)"}[how]
    text = re.sub(r"SI_UNIT\((\.MILLI\.|\.CENTI\.|\$),\.METRE\.\)", replacement, text, count=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


def write_iges_with_scale(src: Path, dest: Path, scale: str = "2.0") -> Path:
    lines = src.read_text().splitlines()
    globals_ = [ln for ln in lines if len(ln) > 72 and ln[72] == "G"]
    fields = "".join(ln[:72] for ln in globals_).split(",")
    fields[12] = scale                       # parameter 13 is the model scale
    payload = ",".join(fields)
    rebuilt, i, n = [], 0, 0
    while i < len(payload):
        n += 1
        rebuilt.append(f"{payload[i:i + 72]:<72}G{n:>7}")
        i += 72
    others = [ln for ln in lines if not (len(ln) > 72 and ln[72] == "G")]
    at = next(i for i, ln in enumerate(others) if len(ln) > 72 and ln[72] == "D")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(others[:at] + rebuilt + others[at:]) + "\n")
    return dest


def write_stl(path: Path, side: float = SIDE) -> Path:
    tri = ((0.0, 0.0, 0.0), (side, 0.0, 0.0), (0.0, side, 0.0))
    lines = ["solid box", "facet normal 0 0 1", "  outer loop"]
    lines += [f"    vertex {v[0]!r} {v[1]!r} {v[2]!r}" for v in tri]
    lines += ["  endloop", "endfacet", "endsolid box"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def write_vtp(path: Path, side: float = SIDE) -> Path:
    import numpy as np
    import pyvista as pv

    pts = np.array([[0.0, 0.0, 0.0], [side, 0.0, 0.0], [0.0, side, 0.0]], dtype=float)
    faces = np.array([3, 0, 1, 2])
    mesh = pv.PolyData(pts, faces)
    mesh.point_data["marker"] = np.array([1.0, 2.0, 3.0])
    mesh.cell_data["region"] = np.array([7.0])
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(str(path))
    return path


def stl_extent(path: Path) -> float:
    from meshpipeline.cad.stl_io import read_stl_triangles
    xs = [v[0] for tri in read_stl_triangles(Path(path)) for v in tri]
    return max(xs) - min(xs)


def vtp_extent(path: Path) -> float:
    import pyvista as pv
    b = pv.read(str(path)).bounds
    return b[1] - b[0]


def write_step_of_units(path: Path, unit: str, units: float = SIDE) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    metres_per = {"MM": 1e-3, "CM": 1e-2, "INCH": 0.0254, "M": 1.0}[unit]
    side_mm = units * metres_per * 1000.0          # OCC builds in its own millimetres
    box = BRepPrimAPI_MakeBox(side_mm, side_mm, side_mm).Shape()

    w = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(f"OCC refused write.step.unit={unit!r}")
    w.Transfer(box, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.Write(str(path))
    return path
