# Responsibility: Verify SHSTAGE-6, the shell mesh of an FEA run settled as "Mesh as: Shell": the staged solid
# (metres, as the builder stages it) is read at its own scale, its mid-surface meshed and written for every
# solver (Nastran first), read back with the walls of the part; the FEA driver runs it in a child process and
# cites it, and does nothing at all when Solid was settled; the downloads offer the shell files, Nastran first.
# Boundaries: a small T-joint meshed in a child process (a few seconds); no solver run here (the CalculiX
# textbook cases are tests/unit/engines/test_shell_pipeline.py).
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import shell_run  # noqa: E402
from meshpipeline.engines import shell_writers as sw  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """Stage a part as the builder does for gmsh: the upload read in mm, written in metres."""
    ws.mkdir(parents=True, exist_ok=True)
    src = sf.write_step(shape, ws / "upload.step")
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


@pytest.fixture(scope="module")
def tjoint(tmp_path_factory):
    return _stage(sf.t_joint(), tmp_path_factory.mktemp("tjoint"))


def test_the_staged_solid_is_read_at_its_own_scale(tjoint):
    mm = FD.staged_mm_per_unit(tjoint / "geometry.step")
    assert mm in (1.0, 1000.0)


def test_the_shell_is_meshed_and_written_for_every_solver(tmp_path, tjoint):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "geometry.step").write_bytes((tjoint / "geometry.step").read_bytes())
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged"]) == 0
    rec = shell_run.read(ws)
    assert rec["made"] is True and rec["bodies"] == {"shell": 1} and rec["open_edges"] == 0
    assert rec["elements"] == rec["quads"] + rec["trias"] > 0
    assert rec["walls_mm"] == [4.0, 5.0]
    assert rec["words"].startswith(f"Shell mesh: {rec['elements']:,} elements")
    for name in shell_run.FILES:
        assert (ws / name).stat().st_size > 0
    back = sw.read_bdf(ws / "shell.bdf")
    assert len(back.elements) == rec["elements"]
    # the walls are the part's, in the deck's metres
    assert sorted({round(float(t) * 1000.0, 3) for t in back.walls.values()}) == [4.0, 5.0]
    assert len(sw.read_cdb(ws / "shell.cdb").elements) == rec["elements"]
    assert len(sw.read_inp(ws / "shell.inp").elements) == rec["elements"]


def test_a_part_with_no_thin_wall_makes_no_shell_and_says_why(tmp_path):
    ws = _stage(sf.chunky_block(), tmp_path / "block")
    rec = shell_run.run(ws, ws / "geometry.step", mm_per_unit=FD.staged_mm_per_unit(ws / "geometry.step"))
    # SHSTAGE-6b: the reason in the measured numbers (the block is 50 x 40 x 30 mm)
    assert rec["made"] is False and rec["why"].startswith("its most plate-like wall is 30 mm thick"), rec["why"]
    assert rec["words"].startswith("No shell mesh was made: its most plate-like wall is 30 mm thick")
    assert not (ws / "shell.bdf").exists()


class _Run:
    deadline_s = None


def test_the_driver_meshes_the_shell_only_when_shell_was_settled(tmp_path, tjoint, monkeypatch):
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "geometry.step").write_bytes((tjoint / "geometry.step").read_bytes())
    assert asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "solid"}, job_id="j", run=_Run())) is None
    assert asyncio.run(FD.shell_pass(ws, {}, job_id="j", run=_Run())) is None
    assert cited == [] and not (ws / "shell.bdf").exists()
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "shell"}, job_id="j", run=_Run()))
    assert rec["made"] is True and (ws / "shell.bdf").exists()
    (entry,) = cited
    assert entry["rule"] == "SHSTAGE-6" and entry["decided"] == "shell mesh" and entry["words"] == rec["words"]


def test_a_shell_run_that_stops_is_said_and_the_solid_goes_on(tmp_path, monkeypatch):
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "geometry.step").write_text("not a step file")
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "shell"}, job_id="j", run=_Run()))
    assert rec["made"] is False and cited[0]["decided"] == "no shell mesh"
    assert json.loads((ws / shell_run.RECORD).read_text())["made"] is False


def test_a_shell_asked_for_as_2d_keeps_the_fea_driver():
    state = {"purpose": "structural", "dimensionality": "2D"}
    assert FD.applies(state) is False
    assert FD.applies({**state, "fea_mesh_as": "shell"}) is True
    assert FD.mesh_as_of({"fea_mesh_as": "SHELL"}) == "shell" and FD.mesh_as_of({}) == "solid"


def test_the_downloads_offer_the_shell_files_nastran_first():
    from meshpipeline.engines.fea_writers import EXPORT_ARTIFACTS
    from meshpipeline.engines.registry import get_spec

    files = [f for _k, f, _l in EXPORT_ARTIFACTS]
    shell = [f for f in files if f.startswith("shell")]
    assert shell == ["shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb"] == list(shell_run.FILES)
    assert files.index("shell.bdf") < files.index("mesh.inp")
    members = {m.path for m in get_spec("gmsh").deliverable.members}
    assert set(shell_run.FILES) | {shell_run.RECORD} <= members


def test_a_part_with_no_shell_says_why_in_numbers():
    # SHSTAGE-6b: the extraction's measured reason, not a fixed sentence
    from meshpipeline.engines import shell_run as sr

    rec = {"bodies": [{"verdict": "solid", "why": "no wall is thin against its size: its most plate-like wall is 60 mm "
                       "thick and spans about 11 mm, only 0.2 times its thickness; a shell needs at least 10 times"}]}
    why = sr.no_shell_why(rec)
    assert why.startswith("its most plate-like wall is 60 mm thick")
    assert "60 mm" in sr.words({"made": False, "why": why})
    assert sr.no_shell_why({"bodies": []}) == sr.NO_WALL_WHY
    two = {"bodies": [{"verdict": "solid", "why": "no wall is thin against its size: a"},
                      {"verdict": "solid", "why": "no wall is thin against its size: a"},
                      {"verdict": "shell", "why": "every wall is thin against its size"},
                      {"verdict": "solid", "why": "the mid-surface extraction ran past 780 s and was stopped"}]}
    assert sr.no_shell_why(two) == "a; the mid-surface extraction ran past 780 s and was stopped"


@pytest.fixture(scope="module")
def rules_count(tmp_path_factory, tjoint):
    """The T-joint's shell count by the rules alone (no count asked)."""
    ws = tmp_path_factory.mktemp("tjoint_rules")
    (ws / "geometry.step").write_bytes((tjoint / "geometry.step").read_bytes())
    rec = shell_run.run(ws, ws / "geometry.step", mm_per_unit=FD.staged_mm_per_unit(ws / "geometry.step"))
    assert rec["made"] and "target" not in rec
    return int(rec["elements"])


def test_the_shell_follows_the_users_element_count(tmp_path, tjoint, rules_count):
    # SHMESH-23: the count the user named (fea_driver.element_target) sizes the shell as it sizes the solid - here
    # four times the rules' own count, finer than the rules: said, never refused
    ask = 4 * rules_count
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "geometry.step").write_bytes((tjoint / "geometry.step").read_bytes())
    target = {"elements": ask, "mode": "about"}
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged", json.dumps(target)]) == 0
    rec = shell_run.read(ws)
    assert rec["made"] is True
    assert abs(rec["elements"] - ask) <= 0.25 * ask, (rec["elements"], ask, rules_count)
    assert rec["target"]["asked"] == ask and rec["target"]["built"] == rec["elements"]
    assert rec["target"]["scale"] < 1.0
    assert f"Sized to the {ask:,} elements you asked for" in rec["words"]
    plan = json.loads((ws / "_shell" / "shell_plan.json").read_text())
    assert all(r["rule"].endswith("+SHMESH-23") for r in plan["faces"])


def test_the_driver_hands_the_count_to_the_shell(tmp_path, tjoint, rules_count, monkeypatch):
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "geometry.step").write_bytes((tjoint / "geometry.step").read_bytes())
    ask = 3 * rules_count
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "shell"}, job_id="j", run=_Run(),
                                    target={"elements": ask, "mode": "about", "quote": "", "source": "ai"}))
    assert rec["made"] is True and rec["target"]["asked"] == ask
    assert abs(rec["elements"] - ask) <= 0.25 * ask
    assert cited[0]["inputs"]["target"]["asked"] == ask


def test_short_walls_are_said_with_the_shell_and_a_part_with_no_plate_says_why():
    # SHELL-23: a wall under ten of its thicknesses across is shelled as asked, and the result says the one line of
    # advice; a part with no plate at all says its measured reason (the new words, and a stored record's old ones)
    from meshpipeline.engines import shell_run as sr

    advice = ("Part_6: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is less exact under 10 times "
              "(about 2% off on a free plate this short, more where it bends from a joint).")
    said = sr.words({"made": True, "elements": 120, "quads": 120, "trias": 0, "walls_mm": [12.7], "advice": advice})
    assert said.startswith("Shell mesh: 120 elements") and said.endswith(" " + advice)
    assert "less exact" not in sr.words({"made": True, "elements": 1, "quads": 1, "trias": 0})
    rec = {"bodies": [{"verdict": "solid", "why": "no wall of it is a plate: its most plate-like wall is 30 mm thick but "
                       "only 40 mm wide, 1.3 times its thickness; under 2 times, it is a bar or a block, not a plate"}]}
    assert sr.no_shell_why(rec) == ("its most plate-like wall is 30 mm thick but only 40 mm wide, 1.3 times its "
                                    "thickness; under 2 times, it is a bar or a block, not a plate")


def test_a_shell_whose_short_plates_will_not_mesh_is_made_without_them(tmp_path, monkeypatch):
    # SHELL-26: the mesher cannot close the boundary where a part's short plates join; the run reads the part again
    # without them, meshes that, and says so in one line
    from meshpipeline.engines import shell_run as sr
    from meshpipeline.engines.gmsh import shell_adapter as sa

    step = sf.write_step(sf._fuse(sf._box(0, 0, 0, 200, 100, 3), sf._box(0, 0, 3, 5, 100, 20)), tmp_path / "p.step")
    real, calls = sa.mesh_run, []

    def flaky(out, **kw):
        rec = json.loads((Path(out) / "shell.json").read_text())
        calls.append(len(rec.get("short_walls") or []))
        if rec.get("short_walls"):
            raise ValueError("gmsh could not close the boundary of 5 faces in turn (SHMESH-11c)")
        return real(out, **kw)

    monkeypatch.setattr(sa, "mesh_run", flaky)
    rec = sr.run(tmp_path / "ws", step, mm_per_unit=1.0)
    assert calls == [1, 0] and rec["made"] is True
    assert rec["plates_left_out"] == sr.PLATES_OUT and rec["advice"] == ""
    assert rec["words"].endswith(" " + sr.PLATES_OUT)
    # a shell with no short plate that will not mesh is not read again: the solid is delivered, as before
    monkeypatch.setattr(sa, "mesh_run", lambda out, **kw: (_ for _ in ()).throw(ValueError("no")))
    thin = sr.run(tmp_path / "ws2", sf.write_step(sf.flat_plate(2.0), tmp_path / "q.step"), mm_per_unit=1.0)
    assert thin["made"] is False and "plates_left_out" not in thin
