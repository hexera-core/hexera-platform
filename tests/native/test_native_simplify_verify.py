# Responsibility: Verify the simplification check against the real gmsh and CalculiX: on a plate with holes, each feature's one-driver local estimate agrees with the global answer it stands in for, and simplify_step_file keeps the harmless hole while refusing the one that moves the answer.
# Boundaries: the native tier; skipped where gmsh, OCP or CalculiX (`ccx`) is missing. The verdict logic is unit-tested in tests/unit/cad/test_simplify_verify.py with a fake runner.
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")
pytest.importorskip("scipy", reason="the driver field is interpolated with scipy")
if shutil.which("ccx") is None:
    pytest.skip("CalculiX (ccx) is not installed", allow_module_level=True)

import meshpipeline.settings.cad_simplify as scfg  # noqa: E402
from meshpipeline.cad.simplify import verify as V  # noqa: E402
from meshpipeline.cad.simplify.contracts import FeatureKind  # noqa: E402
from meshpipeline.cad.simplify.recognise import recognise  # noqa: E402
from meshpipeline.cad.simplify.simplify import simplify_step_file  # noqa: E402


def _plate(holes):
    """200 x 60 x 10 plate: the load case clamps x = 6 and bends x = 194 along y."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(200, 60, 10)).Shape()
    for x, y, r in holes:
        plate = BRepAlgoAPI_Cut(plate, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)),
                                                                 r, 12.0).Shape()).Shape()
    return plate


def _compliance(shape, lc, diag, h, work: Path) -> float:
    """T3: the global compliance of the trimmed part, from one driver-style solve."""
    V.write_brep(V.trim(shape, lc, diag), work.with_suffix(".brep"))
    r = V.run_job_in_child({"kind": V.DRIVER, "brep": str(work.with_suffix(".brep")), "h": h, "ho": 1,
                            "lc": lc.to_dict(), "max_tets": 10 ** 6, "work": str(work),
                            "field": str(work / "field.npz")}, 300)
    assert r["ok"], r
    return 1e-3 * (lc.hi - lc.lo) / r["reaction"][lc.bend_dir]


def test_each_local_estimate_agrees_with_the_global_answer(tmp_path):
    plate = _plate([(100.0, 30.0, 4.0), (60.0, 15.0, 2.5)])
    cands, diag = recognise(plate)
    index = V.face_sigs(plate)
    feats = [V.FeatureToVerify(c.id, c.kind.value, c.radius, c.bbox, tuple(index[i - 1][0] for i in c.faces))
             for c in cands]
    simplified, why = V.defeature(plate, [index[i - 1][1] for c in cands for i in c.faces])
    assert simplified is not None, why

    pv = V.verify_package(plate, simplified, feats, budget=0.001, margin=0.5,
                          deadline=time.monotonic() + 300, max_tets=200_000)
    assert not pv.abstained, pv.abstained

    lc = V.load_case(V.bbox_of(plate))
    h = pv.driver["h"]
    c0 = _compliance(plate, lc, diag, h, tmp_path / "orig")
    for c, e in zip(cands, pv.estimates, strict=True):
        alone, _ = V.defeature(plate, [index[i - 1][1] for i in c.faces])
        t3 = (_compliance(alone, lc, diag, h, tmp_path / c.id) - c0) / c0
        assert e.ok, e.why
        # filling a hole stiffens: both read negative, and the local reading is within a quarter
        # of the global one (measured: -0.568% vs -0.586%, -0.181% vs -0.200%)
        assert t3 < 0 and e.estimate < 0
        assert e.estimate == pytest.approx(t3, rel=0.25)
    # both are far past the 0.05% screen, so neither is kept
    assert pv.accepted == ()


def test_the_harmless_hole_is_removed_and_the_one_that_matters_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_ENABLED", True)
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    src = tmp_path / "plate.step"
    from OCP.Interface import Interface_Static
    w = STEPControl_Writer()
    # write.step.unit is PROCESS-GLOBAL (cad/repair/conservative._write_step): another test may
    # have left it at inches, so it is stated here, after the writer registers it
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(_plate([(100.0, 30.0, 4.0), (60.0, 30.0, 1.0)]), STEPControl_AsIs)
    w.Write(str(src))

    report = simplify_step_file(src, tmp_path / "plate-simplified.step")

    from meshpipeline.cad.repair.brep import _read_shape
    left = [c.radius for c in recognise(_read_shape(tmp_path / "plate-simplified.step"))[0]
            if c.kind is FeatureKind.hole]
    assert left == [pytest.approx(4.0)]
    rows = {round(op["radius"], 3): op for op in report.operations if op.get("radius")}
    assert rows[1.0]["status"] == "kept" and abs(rows[1.0]["estimate"]) <= 0.0005
    assert rows[4.0]["status"] == "rejected" and rows[4.0]["reason"] == "over_screen"
