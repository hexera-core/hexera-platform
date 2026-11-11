# Responsibility: Verify SURF-3..6 in the run: a CAD file that is a surface is meshed as a shell on the surface as
# given by the existing shell path and delivered as the result (no solid mesher called); the thickness written is
# the user's, in every solver file; with none given the shell is still delivered with its thickness plainly not set;
# the same file twice gives the same files byte for byte; separate sheets are separate parts; and a solid, faces that
# close and a closed skin with a gap go on exactly as before.
# Boundaries: the real surface run, shell mesher and FEA driver on small shapes (a second or two each), the solid
# mesher faked to record any call; no solver run, no store.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import shell_run, surface_run  # noqa: E402
from meshpipeline.engines import shell_writers as sw  # noqa: E402

FILES = ("shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb", "shell.msh")


class _AsBefore(Exception):
    """Raised by the solid path's next step in these tests: reaching it is the proof nothing changed before it."""


class _Run:
    deadline_s = None

    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _file(shape, folder: Path, name: str = "part.step") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return Path(sx.write_step(shape, folder / name))


def _stage(shape, ws: Path) -> Path:
    """The workspace as the run stages it: the upload read by gmsh, scaled to metres, written as geometry.step."""
    src = _file(shape, ws, "upload.step")
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
def bracket(tmp_path_factory):
    """A bent bracket's surface meshed at 1.5 mm: (workspace, record)."""
    ws = tmp_path_factory.mktemp("bracket")
    src = _file(sx.bracket(), ws / "in")
    return ws, surface_run.run(ws, src, mm_per_unit=1.0, thickness_mm=1.5, source="part.step")


def test_a_surface_is_meshed_as_a_shell_at_the_thickness_given(bracket):
    ws, rec = bracket
    assert rec["made"] is True and surface_run.is_surface(rec)
    assert rec["surface"]["thickness_mm"] == 1.5 and rec["surface"]["thickness_set"] is True
    assert rec["walls_mm"] == [1.5] and rec["open_edges"] == 0 and rec["solid_bodies"] == 0
    q = rec["quality"]
    assert q["invalid"] == 0 and q["valid"] and q["within_nastran"], q["notes"]
    assert rec["elements"] == rec["quads"] + rec["trias"] > 100
    # the shell run's own record, so the delivery, the result and the downloads are the shell path's
    assert shell_run.read(ws)["elements"] == rec["elements"]
    assert rec["words"].startswith("This file is a surface, not a solid. It is meshed as a shell, 1.5 mm thick.")
    assert "No thickness" not in rec["words"]


def test_every_solver_file_carries_the_thickness_and_a_group_per_cad_face(bracket):
    ws, rec = bracket
    for name, reader in (("shell.bdf", sw.read_bdf), ("shell.inp", sw.read_inp), ("shell_abaqus.inp", sw.read_inp),
                         ("shell.cdb", sw.read_cdb)):
        back = reader(ws / name)
        assert len(back.elements) == rec["elements"], name
        assert list(back.walls.values()) == [pytest.approx(0.0015)], name     # metres
    groups = sw.read_bdf(ws / "shell.bdf").groups
    assert {"Face_1", "Face_2", "Face_3", "Part_1"} <= set(groups)
    assert sum(len(groups[f]) for f in ("Face_1", "Face_2", "Face_3")) == rec["elements"] == len(groups["Part_1"])
    head = (ws / "shell.bdf").read_text()[:1500]
    assert "$ Made from a surface file: the elements lie on the surface as given." in head
    assert "$ Wall thickness 1.5 mm, as the user gave it." in head and "NOT SET" not in head
    assert "$ Femap Property 1 : Part_1_t1p5mm" in head


def test_pynastran_reads_the_surface_shell(bracket):
    bdf_mod = pytest.importorskip("pyNastran.bdf.bdf")
    ws, rec = bracket
    b = bdf_mod.BDF(debug=None)
    b.read_bdf(str(ws / "shell.bdf"), punch=True, xref=False)
    assert len(b.elements) == rec["elements"] and len(b.nodes) == rec["nodes"]
    assert [p.t for p in b.properties.values()] == [pytest.approx(0.0015)]


def test_the_same_file_twice_gives_the_same_files(bracket, tmp_path):
    ws, _rec = bracket
    again = tmp_path / "again"
    again.mkdir()
    surface_run.run(again, ws / "in" / "part.step", mm_per_unit=1.0, thickness_mm=1.5, source="part.step")
    for name in FILES:
        assert (again / name).read_bytes() == (ws / name).read_bytes(), name


def test_no_thickness_given_still_delivers_with_the_thickness_plainly_not_set(bracket, tmp_path):
    ws, with_t = bracket
    rec = surface_run.run(tmp_path, ws / "in" / "part.step", mm_per_unit=1.0, source="part.step")
    assert rec["made"] is True and rec["surface"]["thickness_mm"] is None and rec["surface"]["thickness_set"] is False
    assert rec["walls_mm"] == [] and rec["quality"]["invalid"] == 0 and rec["quality"]["within_nastran"]
    # SURF-5: sized as for a thin sheet (the sheet's size over SIZING_DIVISIONS), never a thickness in a file
    (wall,) = rec["surface"]["sizing_wall_mm"]
    size = json.loads((tmp_path / "surface_model.json").read_text())["sheets"][0]["size_mm"]
    assert wall == pytest.approx(size / surface_run.SIZING_DIVISIONS, rel=1e-5) and wall < 1.5
    for name, reader in (("shell.bdf", sw.read_bdf), ("shell.inp", sw.read_inp), ("shell.cdb", sw.read_cdb)):
        assert list(reader(tmp_path / name).walls.values()) == [0.0], name
    for name, mark in (("shell.bdf", "$"), ("shell.inp", "**"), ("shell_abaqus.inp", "**"), ("shell.cdb", "!")):
        head = (tmp_path / name).read_text()[:1500]
        assert f"{mark} WALL THICKNESS NOT SET: the file is a surface and no thickness was given." in head, name
    assert "Part_1_thickness_not_set" in (tmp_path / "shell.bdf").read_text()[:1500]
    assert "No thickness was given, so none is set in the files" in rec["words"]
    assert 'add it to your notes (for example "1.2 mm thick")' in rec["words"]


def test_with_no_thickness_a_mesh_past_nastrans_limits_is_sized_again_for_a_thicker_sheet(tmp_path, monkeypatch):
    """SURF-5: the sliver clean-up works in walls, so the thin sizing wall cleans little. When it leaves elements
    past Nastran's own limits the sheet is sized again at twice, then four times, that wall and the first mesh
    inside the limits is kept; with a thickness given the mesh is never made again."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = sa.mesh_run
    seen: list[float] = []

    def fake(out, *, mm_per_unit=1.0, target=None):
        wall = json.loads((out / "shell.json").read_text())["pairs"][0]["thickness_mm"]
        seen.append(wall)
        mesh, applied = real(out, mm_per_unit=mm_per_unit, target=target)
        # the first two meshes are said to leave triangles past the limits; the third is clean
        applied["quality"]["nastran_past"] = {"T3_SKEW": 3} if len(seen) == 1 else {"T3_SKEW": 1} if len(seen) == 2 else {}
        applied["quality"]["within_nastran"] = len(seen) > 2
        return mesh, applied

    monkeypatch.setattr(sa, "mesh_run", fake)
    src = _file(sx.bracket(), tmp_path / "in")
    rec = surface_run.run(tmp_path, src, mm_per_unit=1.0)
    assert rec["made"] and len(seen) == 3
    assert seen[1] == pytest.approx(2 * seen[0], rel=1e-5) and seen[2] == pytest.approx(4 * seen[0], rel=1e-5)
    assert rec["surface"]["sizing_wall_mm"] == [pytest.approx(seen[2])] and rec["quality"]["within_nastran"]
    assert rec["walls_mm"] == [] and list(sw.read_bdf(tmp_path / "shell.bdf").walls.values()) == [0.0]
    assert json.loads((tmp_path / "shell.json").read_text())["pairs"][0]["thickness_mm"] == pytest.approx(seen[2])
    # a thickness given: one mesh, whatever it leaves
    seen.clear()
    ws = tmp_path / "given"
    ws.mkdir()
    rec = surface_run.run(ws, src, mm_per_unit=1.0, thickness_mm=1.5)
    assert rec["made"] and seen == [1.5] and rec["walls_mm"] == [1.5]


def test_separate_sheets_are_separate_parts_with_a_property_each(tmp_path):
    shape = sx.compound([sx.bracket(), sx.moved(sx.plate_with_hole(), (0, 0, 200))])
    rec = surface_run.run(tmp_path, _file(shape, tmp_path / "in"), mm_per_unit=1.0, thickness_mm=2.0)
    assert rec["made"] and rec["surface"]["sheets"] == 2 and rec["bodies"] == {"shell": 2}
    back = sw.read_bdf(tmp_path / "shell.bdf")
    assert sorted(back.names.values()) == ["Part_1_t2mm", "Part_2_t2mm"]
    assert list(back.walls.values()) == [pytest.approx(0.002), pytest.approx(0.002)]
    assert {"Part_1", "Part_2"} <= set(back.groups) and not (back.groups["Part_1"] & back.groups["Part_2"])
    assert "The file's 2 separate sheets are 2 parts, each with its own property." in rec["words"]


def test_a_rib_on_a_plate_is_meshed_with_shared_nodes_along_its_foot(tmp_path):
    rec = surface_run.run(tmp_path, _file(sx.tee(split=True), tmp_path / "in"), mm_per_unit=1.0, thickness_mm=2.0)
    assert rec["made"] and rec["surface"]["sheets"] == 1 and rec["open_edges"] == 0
    edges = rec["quality"]["edges"]
    assert edges["junction"] > 0 and rec["quality"]["invalid"] == 0
    # a foot that lands inside one face is joined too (the plate scored along it): one part, shared nodes
    ws = tmp_path / "inside"
    ws.mkdir()
    rec = surface_run.run(ws, _file(sx.tee(split=False), ws / "in"), mm_per_unit=1.0, thickness_mm=2.0)
    assert rec["made"] and rec["surface"]["sheets"] == 1 and rec["open_edges"] == 0
    assert rec["quality"]["edges"]["junction"] > 0 and rec["quality"]["invalid"] == 0
    groups = sw.read_bdf(ws / "shell.bdf").groups
    assert {"Face_1", "Face_2", "Part_1"} <= set(groups) and len(groups["Face_1"]) + len(groups["Face_2"]) == rec["elements"]


def test_edges_that_do_not_meet_are_said_on_the_result(tmp_path):
    """SURF-2 as it was, when the user says not to stitch: the gaps are counted, sized and said, never closed."""
    from meshpipeline.cad import surface_model as sm

    tol = sm.read(sx.tray(), mm_per_unit=1.0)[0]["sew_tolerance_mm"]
    src = _file(sx.skin_with_crack(3.0 * tol), tmp_path / "in")
    rec = surface_run.run(tmp_path, src, mm_per_unit=1.0, thickness_mm=1.0, stitch=False)
    # one place: the lid's rim and the walls' tops are the two sides of one gap all round (SURF-8's one count)
    assert rec["made"] and rec["open_edges"] == 1
    assert "The surfaces do not meet at 1 place: the shell has gaps there (the widest " in rec["words"]
    assert "Nothing was stitched, as your notes asked." in rec["words"]


def test_small_gaps_are_closed_by_rule_and_said(tmp_path):
    """SURF-8: a panel drawn as three patches, the middle one written three sew tolerances off: stitched, meshed as
    one sheet with shared nodes along both joints, and the result says how many gaps and how wide."""
    from meshpipeline.cad import surface_model as sm

    for name in ("whole", "gapped"):
        (tmp_path / name).mkdir()
    whole = surface_run.run(tmp_path / "whole", _file(sx.strip_of_plates(), tmp_path / "a"), mm_per_unit=1.0,
                            thickness_mm=1.0)
    tol = sm.read(sx.strip_of_plates(), mm_per_unit=1.0)[0]["sew_tolerance_mm"]
    src = _file(sx.strip_of_plates(move={1: (0, 0, 3.0 * tol)}), tmp_path / "b")
    rec = surface_run.run(tmp_path / "gapped", src, mm_per_unit=1.0, thickness_mm=1.0, mend=False)
    assert rec["made"] and rec["open_edges"] == 0 and rec["surface"]["sheets"] == 1
    c = rec["surface"]["cleaned"]
    assert c["closed_edges"] == 2 and c["limit_from"] == "rule" and c["wider_gaps"] == 0
    assert c["places_before"] == 2 and c["closed_places"] == 2
    assert f"Closed both places where the surfaces did not meet, the widest {3.0 * tol:.2g} mm." in rec["words"]
    q = rec["quality"]
    assert q["invalid"] == 0 and q["within_nastran"] is True
    # one sheet: the mesh's only free edges are the panel's rim, as for the panel written whole
    assert q["edges"]["free"] == whole["quality"]["edges"]["free"]
    assert rec["elements"] == pytest.approx(whole["elements"], rel=0.05)
    assert all((tmp_path / "gapped" / f).stat().st_size > 0 for f in FILES)


def test_a_wider_gap_is_left_open_and_said_and_the_notes_tolerance_closes_it(tmp_path):
    from meshpipeline.cad import surface_model as sm

    tol = sm.read(sx.strip_of_plates(), mm_per_unit=1.0)[0]["sew_tolerance_mm"]
    src = _file(sx.strip_of_plates(move={1: (0, 0, 12.0 * tol)}), tmp_path / "in")
    for name in ("rule", "said"):
        (tmp_path / name).mkdir()
    rec = surface_run.run(tmp_path / "rule", src, mm_per_unit=1.0, thickness_mm=1.0, mend=False)
    assert rec["made"] and rec["surface"]["sheets"] == 3 and rec["surface"]["cleaned"]["closed_edges"] == 0
    assert rec["open_edges"] == 2 and "The surfaces do not meet at 2 places: the shell has gaps there." in rec["words"]
    assert "At both places, the surfaces run side by side " in rec["words"]
    assert 'If they should meet, add to your notes: "stitch gaps up to ' in rec["words"]
    # the tolerance the words suggest is enough to close them
    ask = float(rec["words"].split('"stitch gaps up to ')[1].split(" mm")[0])
    assert 12.0 * tol < ask <= 1.25 * 12.0 * tol
    said = surface_run.run(tmp_path / "said", src, mm_per_unit=1.0, thickness_mm=1.0, sew_mm=ask, mend=False)
    assert said["made"] and said["surface"]["sheets"] == 1 and said["open_edges"] == 0
    assert f"as your notes asked (up to {ask:.2g} mm)" in said["words"]
    assert "run side by side" not in said["words"]


def test_the_stitch_words_are_read_from_the_notes_the_latest_standing():
    state = {"request_txt": "FEA shell, stitch gaps up to 0.5 mm",
             "user_notes": [{"id": "n1", "text": "1 mm thick, stitch gaps up to 0.2 mm, ignore holes under 4 mm"}]}
    got = surface_run.stitch_for(state)
    assert got.mm == 0.2 and got.holes_mm == 4.0 and not got.off
    assert surface_run.stitch_for({"request_txt": "FEA", "user_notes": [{"id": "n", "text": "do not stitch"}]}).off
    assert not surface_run.stitch_for({"request_txt": "FEA of a bracket"}).given


def test_the_notes_mesh_requirements_reach_the_shell_mesher(tmp_path, monkeypatch):
    """SURF-11: the requirements read from the notes (NOTE-7..9) are handed to the shell mesher for a surface file as
    they are for a mid-surface shell; with none, the mesher is called exactly as before."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = sa.mesh_run
    seen: list = []

    def fake(out, *, mm_per_unit=1.0, target=None, **more):
        seen.append(dict(more, target=target))
        mesh, applied = real(out, mm_per_unit=mm_per_unit, target=target)
        if more.get("requirements"):
            applied["requirements"] = {"asked": more["requirements"]}
        return mesh, applied

    monkeypatch.setattr(sa, "mesh_run", fake)
    src = _file(sx.bracket(), tmp_path / "in")
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    asked = {"sizes": {"global_mm": 4.0}}
    rec = surface_run.run(tmp_path / "a", src, mm_per_unit=1.0, thickness_mm=1.5, requirements=asked,
                          target={"elements": 800, "mode": "about"})
    assert rec["made"] and seen[0] == {"requirements": asked, "target": {"elements": 800, "mode": "about"}}
    assert rec["requirements"] == {"asked": asked}
    seen.clear()
    plain = surface_run.run(tmp_path / "b", src, mm_per_unit=1.0, thickness_mm=1.5)
    assert plain["made"] and seen[0] == {"target": None} and "requirements" not in plain


def test_the_requirements_are_read_from_the_workspace_when_that_reader_is_built_in(tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "meshpipeline.engines.mesh_requirements", None)        # not in this build
    assert surface_run.requirements_in(tmp_path) is None
    fake = types.ModuleType("meshpipeline.engines.mesh_requirements")
    fake.read = lambda ws: {"status": "used", "requirements": {"sizes": {"global_mm": 4.0}}}
    fake.requirements_of = lambda sec: sec.get("requirements")
    monkeypatch.setitem(sys.modules, "meshpipeline.engines.mesh_requirements", fake)
    import meshpipeline.engines as engines_pkg

    monkeypatch.setattr(engines_pkg, "mesh_requirements", fake, raising=False)
    assert surface_run.requirements_in(tmp_path) == {"sizes": {"global_mm": 4.0}}


def test_what_the_words_left_unread_and_a_thick_sheet_are_said_never_refused(tmp_path):
    src = _file(sx.plate(60.0, 40.0), tmp_path / "in")
    rec = surface_run.run(tmp_path, src, mm_per_unit=1.0, thickness_mm=8.0, others_mm=[1.5],
                          unread="a gauge number (16 gauge)")
    assert rec["made"] and rec["walls_mm"] == [8.0]
    assert "Your notes also name 1.5 mm: one thickness is used for the whole file" in rec["words"]
    assert "A gauge number (16 gauge) was not read as a thickness: say it in mm." in rec["words"]
    assert "At 8 mm the sheet is thick for its size" in rec["words"]


@pytest.mark.parametrize(("shape", "kind"), [(sx.box_solid, "solid"), (sx.closed_skin, "closed"),
                                             (sx.skin_with_pinhole, "gap")])
def test_a_file_that_is_not_a_surface_is_left_alone(shape, kind, tmp_path):
    rec = surface_run.run(tmp_path, _file(shape(), tmp_path / "in"), mm_per_unit=1.0, thickness_mm=None)
    assert rec["made"] is False and rec["surface"]["kind"] == kind and not surface_run.is_surface(rec)
    assert shell_run.read(tmp_path) == {} and not any((tmp_path / f).exists() for f in FILES)


def test_a_gap_file_is_a_surface_once_a_shell_or_a_thickness_was_said(tmp_path):
    src = _file(sx.skin_with_pinhole(), tmp_path / "in")
    for k, kw in enumerate(({"said_shell": True}, {"thickness_mm": 1.0})):
        ws = tmp_path / str(k)
        ws.mkdir()
        rec = surface_run.run(ws, src, mm_per_unit=1.0, **kw)
        assert rec["made"] and surface_run.is_surface(rec) and "a shell was asked for" in rec["surface"]["why"]


def test_the_runs_thickness_is_the_latest_word():
    T = surface_run.thickness_for
    assert T({}).mm is None and T(None).mm is None
    assert T({"request_txt": "FEA, thickness 2 mm"}).mm == 2.0
    # settled on the stage from the chat beats the model's summary; a note (it can be added later) beats both
    assert T({"request_txt": "thickness 2 mm", "fea_shell_thickness_mm": 1.2}).mm == 1.2
    state = {"request_txt": "thickness 2 mm", "fea_shell_thickness_mm": 1.2,
             "user_notes": [{"text": "use quads"}, {"text": "sheet is 0.9 mm thick"}]}
    assert T(state).mm == 0.9 and T(state).quote == "0.9 mm thick"
    assert T({"fea_shell_thickness_mm": "x", "user_notes": [{"text": "16 gauge"}]}).unread == "a gauge number (16 gauge)"
    assert surface_run.said_shell({"fea_mesh_as": "Shell"}) and not surface_run.said_shell({"fea_mesh_as": "solid"})


# ------------------------------------------------------------------------------------------ through the driver
def _as_before(_ws):
    raise _AsBefore


def _drive(ws: Path, monkeypatch, state: dict):
    from meshpipeline.engines import runtime

    calls: list = []
    cited: list = []
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": 3, "timed_out": False, "log_tail": ""}))
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ok, marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-surface", publish=None, run=_Run()))
    return ok, marker, calls, cited


@pytest.fixture(scope="module")
def delivered(tmp_path_factory):
    """A bracket's surface through the real driver, the thickness in the user's notes."""
    mp = pytest.MonkeyPatch()
    try:
        ws = _stage(sx.bracket(), tmp_path_factory.mktemp("driven") / "ws")
        state = {"purpose": "structural", "request_txt": "FEA of a bracket",
                 "user_notes": [{"id": "n1", "text": "steel, 1.2 mm thick"}]}
        ok, marker, calls, cited = _drive(ws, mp, state)
    finally:
        mp.undo()
    return ws, ok, calls, cited


def test_the_driver_delivers_the_surfaces_shell_and_calls_no_solid_mesher(delivered):
    ws, ok, calls, cited = delivered
    assert ok is True and calls == []
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and rec["walls_mm"] == [1.2]
    q = json.loads((ws / "quality.json").read_text())
    assert q["fea"]["shell"] is True and q["fea"]["surface"] is True and q["fea"]["thickness_set"] is True
    assert q["cells"] == rec["elements"] > 0 and q["fea_inverted"] == 0 and q["fea"]["walls_mm"] == [1.2]
    assert (ws / "mesh.inp").read_bytes() == (ws / "shell.inp").read_bytes()
    assert json.loads((ws / "fea_passes.json").read_text())["delivered"] == "shell"
    # the staged file is in metres: the thickness is still the user's millimetres (0.0012 m in the files)
    assert list(sw.read_bdf(ws / "shell.bdf").walls.values()) == [pytest.approx(0.0012)]
    said = [(c["decided"], c["rule"]) for c in cited if c["stage"] == surface_run.GATE]
    assert said == [("surface part", "SURF-1"), ("thickness", "SURF-4"), ("shell mesh", "SURF-3"),
                    ("shell delivered", "SURF-3")]
    assert not any(c["stage"] == "fea_plan" for c in cited), "nothing of the solid plan ran"


def test_the_delivery_and_the_result_say_what_it_is(delivered):
    from meshpipeline.engines import fea_results, fea_review
    from meshpipeline.engines.gmsh.gmsh_runner import check_mesh, finalize

    ws, _ok, _calls, _cited = delivered
    assert check_mesh(ws)["mesh_ok"] is True
    assert finalize(str(ws), [], "gmsh", domain="structural FEA")["success"] is True
    res = fea_results.results(ws)
    rec = shell_run.read(ws)
    assert res["title"] == "FEA shell mesh"
    assert res["summary"] == (f"A shell on your surface: {rec['elements']:,} plate elements ({rec['quads']:,} quads, "
                              f"{rec['trias']:,} triangles), 1.2 mm thick.")
    assert res["plan_words"][0].startswith("This file is a surface, not a solid. It is meshed as a shell, 1.2 mm thick.")
    facts = fea_review.shell_facts_text(ws)
    assert "SHELL mesh of a SURFACE file" in facts and "the wall thickness the user gave" in facts
    assert "mid-surface" not in facts


def test_a_solid_and_faces_that_close_never_reach_the_surface_run(tmp_path, monkeypatch):
    """A solid file's run is untouched: solids_from_shells says nothing of an open file, so no surface run starts."""
    for k, (shape, made) in enumerate(((sx.box_solid(), 0), (sx.closed_skin(), 1))):
        ws = _stage(shape, tmp_path / str(k))
        seen: dict = {}
        assert FD.solids_from_shells(ws, seen) == made and seen == {}
        assert FD.solids_from_shells(ws) in (0, made)            # the call as it was still works
    ws = _stage(sx.bracket(), tmp_path / "open")
    seen = {}
    assert FD.solids_from_shells(ws, seen) == 0 and seen == {"open": True}

    async def never(*a, **k):
        raise AssertionError("the surface run was started for a solid")

    monkeypatch.setattr(surface_run, "surface_pass", never)
    monkeypatch.setattr(FD, "open_single_skins", _as_before)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    with pytest.raises(_AsBefore):                        # the solid path's next step, reached as before
        asyncio.run(FD.drive(tmp_path / "0", {"purpose": "structural"}, job_id="t", publish=None, run=_Run()))


def test_a_closed_skin_with_a_gap_goes_on_as_before(tmp_path, monkeypatch):
    ws = _stage(sx.skin_with_pinhole(), tmp_path / "ws")
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    monkeypatch.setattr(FD, "open_single_skins", _as_before)
    with pytest.raises(_AsBefore):
        asyncio.run(FD.drive(ws, {"purpose": "structural"}, job_id="t", publish=None, run=_Run()))
    assert surface_run.read(ws)["surface"]["kind"] == "gap" and shell_run.read(ws) == {}


def test_a_surface_whose_shell_cannot_be_made_is_the_runs_plain_reason(tmp_path, monkeypatch):
    """No solid to fall back to: the run fails with the reason in words, never a solid mesher's "no solid volume"."""
    from meshpipeline.engines.preflight import read_refusal

    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    rec = {"made": False, "why": "the shell mesh ran past 15 minutes and was stopped (SHMESH-21)",
           "surface": {"kind": "surface", "thickness_mm": None}}
    tmp_path.mkdir(exist_ok=True)
    ok, marker, _o = asyncio.run(FD._deliver_surface(tmp_path, rec, job_id="t", run=_Run()))
    assert ok is False and marker == FD.BUILD_FAILED
    refusal = read_refusal(tmp_path)
    assert refusal is not None and refusal.gate == surface_run.GATE
    assert "it can only be meshed as a shell" in refusal.builder_text and "SHMESH-21" in refusal.builder_text


# ------------------------------------------------------------------- SURF-11: the user's count and requirements
def test_the_users_element_count_reaches_a_surface_file(tmp_path, monkeypatch):
    """The count is read by the FEA plan's own reader (shown the file with no zones) and the shell is sized to it."""
    ws = _stage(sx.strip_of_plates(), tmp_path / "ws")
    shown: list = []

    async def decide(record, state, zones, **kw):
        shown.append((dict(record), list(zones), state.get("user_notes")))
        return {"status": "used", "decision": {"target_elements": 600, "target_mode": "about", "target_min": 0,
                                               "target_max": 0}}

    monkeypatch.setattr(FD.FA, "decide", decide)
    state = {"purpose": "structural", "request_txt": "FEA of a panel",
             "user_notes": [{"id": "n1", "text": "1 mm thick, about 600 elements"}]}
    ok, _marker, calls, cited = _drive(ws, monkeypatch, state)
    assert ok is True and calls == [] and len(shown) == 1 and shown[0][1] == []
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and abs(rec["elements"] - 600) <= 0.25 * 600
    assert int(rec["target"]["asked"]) == 600
    assert ("element count", "SURF-11") in [(c["decided"], c["rule"]) for c in cited]
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert plan["asked"]["elements"] == 600


def test_with_no_words_no_model_is_asked_for_a_surface_file(tmp_path, monkeypatch):
    ws = _stage(sx.strip_of_plates(), tmp_path / "ws")

    async def never(*a, **k):
        raise AssertionError("the model was asked with no words to read")

    monkeypatch.setattr(FD.FA, "decide", never)
    ok, _marker, calls, cited = _drive(ws, monkeypatch, {"purpose": "structural"})
    assert ok is True and calls == []
    assert [(c["decided"], c["rule"]) for c in cited if c["stage"] == surface_run.GATE] == [
        ("surface part", "SURF-1"), ("thickness", "SURF-4"), ("shell mesh", "SURF-3"), ("shell delivered", "SURF-3")]


def test_the_notes_requirements_reader_is_called_for_a_surface_file_when_it_is_built_in(tmp_path, monkeypatch):
    """NOTE-7..9's reader (fea_driver.read_requirements, with engines/mesh_requirements) is asked before the surface
    run, so what it leaves in the workspace is there for the shell mesher."""
    ws = _stage(sx.strip_of_plates(), tmp_path / "ws")
    order: list = []

    async def decide(record, state, zones, **kw):
        return {"status": "off"}

    async def reader(w, state, *, job_id, run=None, publish=None):
        order.append("requirements")
        return {"status": "none"}

    real_pass = surface_run.surface_pass

    async def passed(*a, **k):
        order.append("surface run")
        return await real_pass(*a, **k)

    monkeypatch.setattr(FD.FA, "decide", decide)
    monkeypatch.setattr(FD, "read_requirements", reader, raising=False)
    monkeypatch.setattr(surface_run, "surface_pass", passed)
    state = {"purpose": "structural", "user_notes": [{"id": "n1", "text": "global size 4 mm"}]}
    ok, _marker, _calls, _cited = _drive(ws, monkeypatch, state)
    assert ok is True and order == ["requirements", "surface run"]


def test_the_cleanup_is_cited_when_it_did_something(tmp_path, monkeypatch):
    from meshpipeline.cad import surface_model as sm

    tol = sm.read(sx.strip_of_plates(), mm_per_unit=1.0)[0]["sew_tolerance_mm"]
    ws = _stage(sx.strip_of_plates(move={1: (0, 0, 3.0 * tol)}), tmp_path / "ws")
    ok, _marker, _calls, cited = _drive(ws, monkeypatch, {"purpose": "structural"})
    assert ok is True
    (entry,) = [c for c in cited if c["decided"] == "surface cleaned"]
    assert entry["rule"] == "SURF-8" and entry["value"] == 2 and entry["inputs"]["limit_from"] == "rule"
    assert entry["words"].startswith("Closed both places where the surfaces did not meet")
    res = shell_run.read(ws)
    assert "Closed both places where the surfaces did not meet" in res["words"]


# ------------------------------------------------------------------- SHELL-15j / SHELL-15k: MESH-1's reading
def _stage_file(src: Path, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return ws


def _kinds(shape) -> dict:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: dict = {}
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        kind = str(BRepAdaptor_Surface(TopoDS.Face_s(ex.Current()), True).GetType()).rsplit("_", 1)[-1]
        out[kind] = out.get(kind, 0) + 1
        ex.Next()
    return out


def test_an_iges_copy_of_a_solid_is_staged_as_the_solid_its_step_is(tmp_path):
    """SHELL-15j: an IGES file's trimmed surfaces carry no solid and write a cylinder as a surface of revolution. MESH-1
    reads them the way the mid-surface extraction does: one valid solid, its bore a cylinder again."""
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.scout import read_cad

    solid = sx._cut(sx.box_solid(60.0, 40.0, 4.0), sx._cyl((30, 20, -1), (0, 0, 1), 6.0, 6.0))
    igs = Path(sx.write_iges(solid, tmp_path / "part.igs"))
    raw = read_cad(igs)
    assert not TopExp_Explorer(raw, TopAbs_SOLID).More() and "Cylinder" not in _kinds(raw)
    ws = _stage_file(igs, tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1 and seen == {}
    staged = read_cad(ws / "geometry.brep")
    assert TopExp_Explorer(staged, TopAbs_SOLID).More()
    assert _kinds(staged) == {"Plane": 6, "Cylinder": 1}


def test_a_face_written_twice_is_no_solid(tmp_path):
    """SHELL-15k: two copies of one face sew into a closed pocket of no volume. It is no solid: the file is still the
    surface it is, and the surface run leaves the second copy out (SURF-9)."""
    one = sx.plate_with_hole()
    ws = _stage(sx.compound([one, sx.copy_of(one)]), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 0 and seen == {"open": True}
    rec = surface_run.run(ws, ws / "geometry.step", mm_per_unit=1000.0, thickness_mm=1.0)
    assert rec["made"] and rec["surface"]["cleaned"]["duplicates"] == 1 and rec["surface"]["faces"] == 1
    assert "1 face was in the file twice: the second copy of it is left out." in rec["words"]


def _volumes(shape) -> list[float]:
    from OCP.TopAbs import TopAbs_SOLID

    from meshpipeline.cad import midsurface as ms

    m = ms._map(shape, TopAbs_SOLID)
    return sorted(ms._volume(m.FindKey(i)) for i in range(1, m.Extent() + 1))


def test_an_iges_pipe_bend_is_read_with_its_torus(tmp_path):
    """SHELL-15m: an IGES writer stores a torus as a surface of revolution. Re-made on its surface alone, the face's
    curves were left for the old surface and it read invalid, so the bend was kept as written and no body closed.
    Converted whole (surface and curves together) it is a torus again and the bend is the solid its STEP is."""
    import math

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad.scout import read_cad

    outer = BRepPrimAPI_MakeTorus(80.0, 12.0, math.pi / 2).Shape()
    inner = BRepPrimAPI_MakeTorus(80.0, 10.0, math.pi / 2).Shape()
    bend = sx._cut(outer, inner)
    want = _volumes(bend)
    igs = Path(sx.write_iges(bend, tmp_path / "bend.igs"))
    raw = read_cad(igs)
    assert "Torus" not in _kinds(raw)
    solid, _notes = ms.solidify(raw, 1.0)
    assert _kinds(solid).get("Torus") == 2
    got = _volumes(solid)
    assert len(got) == 1 and got[0] == pytest.approx(want[0], rel=1e-4)


def test_a_closed_shell_of_no_volume_is_never_a_body(tmp_path):
    """SHELL-15l: two touching bodies' coincident faces (a pipe's outside, its insulation's inside) sew to each other
    into a closed shell of no volume. Before, that pocket was made a "solid" of 0 mm3 beside the real ones; now it is no
    body, and no solid read from the file encloses nothing."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad.scout import read_cad

    pipe = sx._cut(sx._cyl((0, 0, 0), (0, 0, 1), 20.0, 200.0), sx._cyl((0, 0, -1), (0, 0, 1), 18.0, 202.0))
    lag = sx._cut(sx._cyl((0, 0, 0), (0, 0, 1), 45.0, 200.0), sx._cyl((0, 0, -1), (0, 0, 1), 20.0, 202.0))
    raw = read_cad(Path(sx.write_iges(sx.compound([pipe, lag]), tmp_path / "lagged.igs")))
    solid, _notes = ms.solidify(raw, 1.0)
    assert _volumes(solid) and all(v > 1.0 for v in _volumes(solid))


@pytest.mark.xfail(strict=True, reason="still open: touching bodies whose coincident faces leave an edge at the same "
                                       "angle beside faces at their own angles (SHELL-15i reads only 2 + 2 clusters)")
def test_a_pipe_and_its_insulation_written_as_iges_are_two_bodies(tmp_path):
    """The STEP's two bodies. Today the IGES copy reads as one body holding both (the coincident faces pair off into
    a pocket of nothing and the caps sew across): documented in docs/design/shell/README.md section 12."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad.scout import read_cad

    pipe = sx._cut(sx._cyl((0, 0, 0), (0, 0, 1), 20.0, 200.0), sx._cyl((0, 0, -1), (0, 0, 1), 18.0, 202.0))
    lag = sx._cut(sx._cyl((0, 0, 0), (0, 0, 1), 45.0, 200.0), sx._cyl((0, 0, -1), (0, 0, 1), 20.0, 202.0))
    both = sx.compound([pipe, lag])
    want = _volumes(both)
    raw = read_cad(Path(sx.write_iges(both, tmp_path / "lagged.igs")))
    solid, _notes = ms.solidify(raw, 1.0)
    got = _volumes(solid)
    assert len(got) == 2 and got == pytest.approx(want, rel=1e-4)
    assert all(v > 0 for v in got)


def test_the_stated_tolerance_words_stand_alone_and_add_up():
    """With a stitch tolerance stated in the notes, each gap sentence names what was stitched and its own count out of
    the places in all: never "of them", never "(s)"."""
    c = {"left_open": 1, "wider_gaps": 2, "applied_mm": 2.0, "left_widest_mm": 1.0, "wider_narrowest_mm": 3.0,
         "wider_widest_mm": 4.0, "limit_from": "notes"}
    words = surface_run.gap_words(3, c)
    assert words[0] == "The surfaces do not meet at 3 places: the shell has gaps there."
    assert words[1].startswith("At 1 of these 3 places, the gap is narrower than the 2 mm stitched here")
    assert words[2].startswith("At 2 of these 3 places, the surfaces run side by side ")
    assert "wider than the 2 mm stitched here." in words[2]
    assert not any("of them" in w or "(s)" in w for w in words)
    one = surface_run.gap_words(1, {**c, "left_open": 1, "wider_gaps": 0})
    assert one[1].startswith("At that place, the gap is narrower than the 2 mm stitched here") and "join it" in one[1]
