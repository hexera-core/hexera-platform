# Responsibility: Verify that the native CAD work a crash could take a run down with - the gmsh bundle's staging and the assembly's part-name read - runs in a process of its own, and that a real crash there costs only the optional parts.
# Boundaries: the crashes are real segmentation faults in a child process; the staging and the names are the real code.
"""A NATIVE CRASH COSTS THE OPTIONAL PARTS, NEVER THE RUN (FEA hunt, 27 Oct).

On real assemblies, native code died with a segmentation fault inside the run's own process: gmsh writing its
own B-rep of an I-DEAS export while staging, and OCC's XDE reader reading an NX assembly's part names in the
geometry check. The run was gone on main and on every branch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.engines.gmsh import gmsh_runner as GR  # noqa: E402
from meshpipeline.sandbox import safe_exec  # noqa: E402

CRASH = [sys.executable, "-c", "import ctypes; ctypes.string_at(0)"]


def _prepared():
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )
    unit = LengthUnit.millimetre
    return from_occ_transfer(GeometryInterpretation(
        interpretation_id="i-cad", owner_id="o-1", geometry_source_id="s-cad", unit=unit,
        scale_to_metres=scale_to_metres(unit), basis=ResolutionBasis.user_confirmed))


def _crash_when(pred):
    real = safe_exec.run_guarded
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        return real(CRASH if pred(args) else args, **kw)
    return run, calls


def _step(tmp_path: Path) -> Path:
    return fx.write_step(fx.two_blocks(), tmp_path / "upload.step")


def test_staging_runs_in_its_own_process_and_keeps_every_file(tmp_path, monkeypatch):
    run, calls = _crash_when(lambda a: False)
    monkeypatch.setattr(safe_exec, "run_guarded", run)
    monkeypatch.setattr(GR, "run_guarded", run)
    ws = tmp_path / "ws"
    GR.tessellate_to_stl(_step(tmp_path), ws / "input.stl", prepared=_prepared())
    assert len(calls) == 1 and calls[0][2:4] == ["meshpipeline.engines.gmsh.gmsh_runner", "stage"]
    assert all((ws / f).exists() for f in ("geometry.step", "geometry.brep", "input.stl"))


def test_a_crash_while_staging_is_staged_again_without_the_optional_parts(tmp_path, monkeypatch, caplog):
    """a REAL segmentation fault in the first staging: the second gives the staged solid, the picture is OCC's"""
    run, calls = _crash_when(lambda a: a[-1] == "full")
    monkeypatch.setattr(GR, "run_guarded", run)
    ws = tmp_path / "ws"
    with caplog.at_level("WARNING"):
        GR.tessellate_to_stl(_step(tmp_path), ws / "input.stl", prepared=_prepared())
    assert [c[-1] for c in calls] == ["full", "plain"]
    assert (ws / "geometry.step").exists() and (ws / "input.stl").exists()
    assert not (ws / "geometry.brep").exists()
    assert "the CAD kernel stopped" in caplog.text


def test_a_file_that_crashes_every_way_is_said_never_a_dead_process(tmp_path, monkeypatch):
    run, _calls = _crash_when(lambda a: True)
    monkeypatch.setattr(GR, "run_guarded", run)
    with pytest.raises(GR.StagingCrashed, match="could not be staged"):
        GR.tessellate_to_stl(_step(tmp_path), tmp_path / "ws" / "input.stl", prepared=_prepared())


def test_inside_a_callers_gmsh_session_the_staging_stays_in_it(tmp_path, monkeypatch):
    monkeypatch.setattr(GR, "run_guarded", lambda *a, **k: pytest.fail("no process inside a gmsh session"))
    src = _step(tmp_path)
    gmsh.initialize()
    try:
        GR.tessellate_to_stl(src, tmp_path / "ws" / "input.stl", prepared=_prepared())
    finally:
        gmsh.finalize()
    assert (tmp_path / "ws" / "geometry.step").exists()


def test_the_part_names_read_in_its_own_process_gives_the_same_names(tmp_path):
    src = _step(tmp_path)
    here = A.read_body_names_here(src)
    guarded = A.read_body_names(src)
    assert [(n, round(v, 9)) for n, _c, v in guarded] == [(n, round(v, 9)) for n, _c, v in here]


def test_a_part_name_reader_that_crashes_costs_the_names_only(tmp_path, monkeypatch, caplog):
    run, _calls = _crash_when(lambda a: True)
    monkeypatch.setattr(safe_exec, "run_guarded", run)
    with caplog.at_level("WARNING"):
        assert A.read_body_names(_step(tmp_path)) == []
    assert "the reader stopped" in caplog.text


def test_a_crash_after_the_staged_solid_is_written_keeps_it(tmp_path, monkeypatch, caplog):
    """the I-DEAS export: gmsh wrote the staged STEP, then died writing its own B-rep - the STEP is kept"""
    real = safe_exec.run_guarded
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        out_dir = Path(args[5]).parent
        script = ("import ctypes, pathlib, sys; d = pathlib.Path(sys.argv[1]); "
                  "(d / 'geometry.step').write_text('ISO-10303-21;'); (d / 'geometry.brep').write_text('half');"
                  " print('GMSH_STAGED_STEP', flush=True); ctypes.string_at(0)")
        return real([sys.executable, "-c", script, str(out_dir)], **kw)

    monkeypatch.setattr(GR, "run_guarded", run)
    monkeypatch.setattr("meshpipeline.cad.cad_tessellate.tessellate_to_stl",
                        lambda geom, out, prepared=None: Path(out).write_text("solid picture\nendsolid\n"))
    ws = tmp_path / "ws"
    with caplog.at_level("WARNING"):
        GR.tessellate_to_stl(_step(tmp_path), ws / "input.stl", prepared=_prepared())
    assert len(calls) == 1
    assert (ws / "geometry.step").exists() and not (ws / "geometry.brep").exists()
    assert "after the staged solid was written" in caplog.text


def test_when_gmsh_dies_every_way_opencascade_alone_writes_the_staged_solid(tmp_path, monkeypatch):
    """gmsh's own STEP writer died on an I-DEAS export: the staged solid is written by OCC, at the same size"""
    run, calls = _crash_when(lambda a: a[-1] in ("full", "plain"))
    monkeypatch.setattr(GR, "run_guarded", run)
    ws = tmp_path / "ws"
    GR.tessellate_to_stl(_step(tmp_path), ws / "input.stl", prepared=_prepared())
    assert [c[-1] for c in calls] == ["full", "plain", "occ"]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
    finally:
        gmsh.finalize()
    ref = tmp_path / "ref"
    monkeypatch.setattr(GR, "run_guarded", safe_exec.run_guarded)
    GR.tessellate_to_stl(_step(tmp_path), ref / "input.stl", prepared=_prepared())
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(ref / "geometry.step"))
        gmsh.model.occ.synchronize()
        box = gmsh.model.getBoundingBox(-1, -1)
    finally:
        gmsh.finalize()
    assert [x0, y0, z0, x1, y1, z1] == pytest.approx(list(box), abs=1e-9)
