# Responsibility: Verify that a file with no solid can never fall between the solid route (MESH-1) and the surface
# route (SURF-1): the two read "the faces close" the same way (MESH-1d, SURF-19), and a stage stopped by its clock keeps
# what it had finished and says so (TIME-22).
# Boundaries: small synthetic shapes made here (tests/surface_fixtures); no corpus file, no network.
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.cad import surface_model as sm  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import gmsh_runner as GR  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """The workspace as the run stages it: the upload read by gmsh, scaled to metres, written as geometry.step."""
    ws.mkdir(parents=True, exist_ok=True)
    src = Path(sx.write_step(shape, ws / "upload.step"))
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def _balls(n: int = 3, r: float = 40.0, apart: float = 500.0):
    """`n` whole spheres, each written as its one face (no solid, no shell): a file of balls."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    return sx.compound([sx.moved(f, (k * apart, 0.0, 0.0))
                        for k in range(n) for f in sx.faces_of(BRepPrimAPI_MakeSphere(r).Shape())])


def test_whole_spheres_written_as_faces_are_read_as_the_solids_they_bound(tmp_path):
    """MESH-1d: a sphere written as its one face closes on itself (its only edges its seam and its poles). The sewing
    hands such a face back loose, so MESH-1 found no closed shell and called the file open, while SURF-1 (no free edge)
    read it closed and refused it: no mesh. Both now read it closed: the balls are the solids they bound."""
    ws = _stage(_balls(), tmp_path / "ws")
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(ws / "geometry.step")
    sheets = sm.measure(sm.sew(shape, leave_out=frozenset()), mm_per_unit=1000.0)
    assert len(sheets) == 3 and all(s["closed"] for s in sheets)          # SURF-1's reading, as before
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 3 and seen == {"closed": 3}
    assert (ws / "geometry.brep").exists()


def test_a_face_open_at_its_ends_is_no_closed_face(tmp_path):
    """MESH-1d reads only a face whose every edge is its own seam or a pole: an open tube (two circles free) is still the
    surface it is."""
    ws = _stage(sx.open_tube(), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 0 and seen.get("open") is True


def test_a_sheet_with_a_small_closed_body_is_one_surface_part_on_both_routes(tmp_path):
    """SURF-19: MESH-1b reads a sheet whose faces include a small closed body (10% of the area) as the surface it is and
    sends it to the surface route; SURF-1 then read the same closed body as "its faces close into a solid" and refused
    it - the part fell between the routes. One share decides both: closed bodies stand for the file only when they hold
    SOLIDS_HOLD_SHARE of its face area; below it they are skins of the surface part, meshed with it and told."""
    pocket = sx.moved(sx.closed_skin(10.0, 10.0, 5.0), (20.0, 20.0, 1.0))
    ws = _stage(sx.compound([sx.plate_with_hole(), pocket]), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 0 and seen.get("open") is True and seen.get("partial")
    from meshpipeline.cad.scout import read_cad

    reading, sewn = sm.read(read_cad(ws / "geometry.step"), mm_per_unit=1000.0, said_shell=True)
    assert reading["kind"] == "surface" and sewn is not None
    assert reading.get("closed_bodies") == 1 and "close" in reading["why"]
    # a file that is all closed skin is still "closed" (MESH-1 reads its solid), as before
    whole, _ = sm.read(read_cad(_stage(sx.closed_skin(), tmp_path / "ws2") / "geometry.step"), mm_per_unit=1000.0,
                       said_shell=True)
    assert whole["kind"] == "closed"


def test_the_share_that_decides_is_mesh_1b_s():
    """SURF-19 reads MESH-1b's own share, not a copy of it."""
    assert sm.CLOSED_HOLD_SHARE is FD.SOLIDS_HOLD_SHARE or sm.CLOSED_HOLD_SHARE == FD.SOLIDS_HOLD_SHARE == 0.98


def test_faces_that_close_where_the_solid_route_made_no_solid_are_meshed_as_the_skin(tmp_path):
    """SURF-19: a file sent to the surface route with no solid whose faces close (the solid route could not make one
    of them) was refused - "the file is not a surface part (its faces close into a solid)" - and the solid route had
    nothing to mesh. With a shell asked for, the faces are now meshed as the part's skin, and the reason is told."""
    from meshpipeline.engines import surface_run

    ws = tmp_path / "ws"
    ws.mkdir()
    step = sx.write_step(sx.closed_skin(), ws / "part.step")
    rec = surface_run.run(ws, step, mm_per_unit=1.0, said_shell=True)
    assert rec["made"] and int(rec.get("elements") or 0) > 0, rec.get("why")
    assert rec["surface"]["why"] == surface_run.BETWEEN_ROUTES_WHY
    assert surface_run.BETWEEN_ROUTES_RULE == "SURF-19"
    # no shell asked for: the run does not shell it (a shell is meshed only when the user says shell)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    rec2 = surface_run.run(ws2, sx.write_step(sx.closed_skin(), ws2 / "part.step"), mm_per_unit=1.0)
    assert not rec2["made"] and rec2["surface"]["kind"] == "closed"


def test_faces_that_sew_open_in_metres_are_read_again_in_millimetres(tmp_path, monkeypatch):
    """MESH-1e: when the metre reading makes no solid that holds the file, the faces are sewn again in millimetres (the
    unit the surface route reads them in) and those solids, scaled back to metres, are the staged solid. Here the metre
    sewing is made to fail, as OpenCASCADE's fixed precisions made it on a small circuit board."""
    ws = _stage(sx.closed_skin(), tmp_path / "ws")
    real = FD._plain_solids
    sizes: list[float] = []

    def plain(shape, tol, diag):
        sizes.append(diag)
        if diag < 1.0:                       # the metre reading (a 74 mm box is 0.074 m across)
            return real(shape, tol * 1e-6, diag)[0], 0, 0
        return real(shape, tol, diag)

    monkeypatch.setattr(FD, "_plain_solids", plain)
    monkeypatch.setattr(FD, "_solidified", lambda *_a, **_k: None)
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1 and seen == {"closed": 1, "read_in_mm": 1}
    assert len(sizes) == 2 and 900 < sizes[1] / sizes[0] < 1100
    from meshpipeline.cad.scout import read_cad
    from meshpipeline.cad.validity_brep import _bbox

    lo, hi = _bbox(read_cad(ws / "geometry.brep"))
    assert 0.05 < max(hi - lo) < 0.1                                          # metres, as staged


def _fake_stage(ws: Path, said: list[str]):
    """A staging process that wrote what `said` says and was then stopped by the clock."""
    def run(args, *, timeout, **_k):
        if GR._STEP_DONE in said:
            (ws / "geometry.step").write_text("step")
        if GR._BREP_DONE in said:
            (ws / "geometry.brep").write_text("brep")
        (ws / "input.stl").write_text("half a picture")
        raise subprocess.TimeoutExpired(args, timeout, output="\n".join(["chatter", *said, "more chatter"]))
    return run


def test_a_stage_stopped_by_its_clock_keeps_what_it_had_finished(tmp_path, monkeypatch):
    """TIME-22: `full` wrote the staged solid and gmsh's B-rep and was still drawing the report's picture at its wall.
    Before, both files were deleted and the part staged again without the B-rep (a blade: 300 s, then no B-rep for the
    surface run). Now they are kept, the half-drawn picture is removed (the OCC tessellator draws it) and the staging
    says so in plain words."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(GR, "run_guarded", _fake_stage(ws, [GR._STEP_DONE, GR._BREP_DONE]))
    assert GR._stage_guarded(tmp_path / "part.igs", ws / "input.stl", 0.001, extras_timeout_s=300) is False
    assert (ws / "geometry.step").exists() and (ws / "geometry.brep").exists() and not (ws / "input.stl").exists()
    note = json.loads((ws / GR.STAGE_NOTE_FILE).read_text())
    assert note["rule"] == GR.STAGE_CLOCK_RULE == "TIME-22" and note["kept"] == ["geometry.brep", "geometry.step"]
    assert "5 minutes" in note["words"] and "mesh goes ahead" in note["words"]
    assert FD._staging_note(ws)["words"] == note["words"]


def test_a_stage_stopped_before_its_solid_was_written_is_staged_again(tmp_path, monkeypatch):
    """TIME-22 keeps only what the process said it finished: stopped before the staged solid, it is staged again."""
    ws = tmp_path / "ws"
    ws.mkdir()
    calls: list = []
    stopped = _fake_stage(ws, [])

    def run(args, *, timeout, **k):
        calls.append(args[-1])
        if args[-1] == "full":
            return stopped(args, timeout=timeout, **k)
        (ws / "geometry.step").write_text("step")
        return subprocess.CompletedProcess(args, 0, stdout=f"{GR._STAGED}plain\n", stderr="")

    monkeypatch.setattr(GR, "run_guarded", run)
    assert GR._stage_guarded(tmp_path / "part.igs", ws / "input.stl", 0.001) is False
    assert calls == ["full", "plain"] and not (ws / GR.STAGE_NOTE_FILE).exists()
