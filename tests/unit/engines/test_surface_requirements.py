# Responsibility: Verify SURF-7, where the surface run and the notes' mesh requirements meet: the requirements read from the notes reach a surface file (a global size, the criteria list and a thickness all apply, and are told asked against delivered); one thickness comes out of the two places one can be read (the word table wins, the notes' typed set is used when the word table read none); with a global size and no thickness the sheet is worked at the size asked, never at the thin-sheet guess; with nothing said a surface file is meshed exactly as before and no model is asked.
# Boundaries: pure functions on plain data, then the real surface run, shell mesher and FEA driver on a small bent bracket (a few seconds each), the reading of the notes stubbed (no model). No solver, no store.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer  # noqa: E402

from meshpipeline.contracts import shell_thickness as st  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines import shell_run, surface_run  # noqa: E402
from meshpipeline.engines import shell_writers as sw  # noqa: E402
from meshpipeline.engines import surface_requirements as srq  # noqa: E402

FILES = ("shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb", "shell.msh")
G = 4.0


def notes_of(g: float = G, thickness: str = "") -> str:
    """The typical criteria list at a global size of `g` mm, as a user pastes it, with an optional thickness line."""
    return CRITERIA.replace("= 10 mm", f"= {g:g} mm") + (f"\n{thickness}" if thickness else "")


def asked(g: float = G, thickness: str = "", value: float | None = None, limits: bool = True) -> dict:
    """The checked set a model's reading of notes_of() gives (engines/mesh_requirements.check)."""
    notes = notes_of(g, thickness)
    answer = criteria_answer()
    answer["sizes"][0].update(value=g, quote=notes.split("\n")[0])
    if not limits:
        answer["sizes"], answer["limits"] = answer["sizes"][:1], []
    if thickness:
        answer["sizes"].append({"what": "thickness", "value": value, "unit": "mm", "quote": thickness})
    req, dropped, _na = MR.check(json.dumps(answer), notes)
    assert dropped == [], dropped
    return req


def _file(shape, folder: Path, name: str = "part.step") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return Path(sx.write_step(shape, folder / name))


def _typical_edge(ws: Path) -> float:
    """The middle element's mean edge length of the delivered shell, mm (the Nastran file is in metres)."""
    back = sw.read_bdf(ws / "shell.bdf")
    xyz = {n: np.array(p) for n, p in back.nodes.items()}
    means = []
    for conn in back.elements.values():
        pts = [xyz[n] for n in conn]
        means.append(np.mean([np.linalg.norm(pts[i] - pts[(i + 1) % len(pts)]) for i in range(len(pts))]))
    return 1000.0 * float(np.median(means))


# ---------------------------------------------------------------------------------- one thickness, two readers
def test_the_word_table_wins_and_the_notes_set_is_used_only_when_it_read_none():
    words = st.read(["sheet is 1.2 mm thick"])
    # both read the same: the word table's, nothing more said
    got, how = srq.thickness(words, asked(thickness="1.2 mm thick", value=1.2))
    assert got.mm == 1.2 and got.quote == "1.2 mm thick" and got.others_mm == ()
    assert how == {"by": srq.BY_WORDS, "set_mm": 1.2, "differs": False}
    # they differ: the word table's stands (it is what the stage showed), the other is said beside it
    got, how = srq.thickness(words, asked(thickness="Thickness 2 mm", value=2))
    assert got.mm == 1.2 and got.others_mm == (2.0,) and how["by"] == srq.BY_WORDS and how["differs"] is True
    # the word table read nothing: the notes' typed set, with the user's own words
    got, how = srq.thickness(st.ThicknessReading(), asked(thickness="Blechdicke 1,5 mm", value=1.5))
    assert got.mm == 1.5 and got.quote == "Blechdicke 1,5 mm" and how["by"] == srq.BY_SET
    # the word table refused something (a bare gauge, a range): it stays refused (SURF-4), the set does not override
    refused = st.read(["16 gauge sheet"])
    assert refused.unread and not refused.given
    got, how = srq.thickness(refused, asked(thickness="16 gauge sheet", value=16))
    assert got.mm is None and got.unread == refused.unread and how["by"] is None
    # nothing anywhere, or no requirement at all: the reading as it was
    none = st.ThicknessReading()
    assert srq.thickness(none, None) == (none, {"by": None, "set_mm": None, "differs": False})
    assert srq.thickness(words, None)[0] is words and srq.thickness(none, asked())[0] is none


def test_a_thickness_no_shell_can_have_is_not_taken_from_the_set():
    req = asked()
    req["sizes"]["thickness_mm"] = 5000.0            # inside the notes' own size range, past a shell thickness
    got, how = srq.thickness(st.ThicknessReading(), req)
    assert got.mm is None and how == {"by": None, "set_mm": None, "differs": False}


def test_the_working_wall_is_the_global_size_only_when_one_is_given():
    assert srq.working_wall(None) is None and srq.working_wall(MR.empty()) is None
    only_limits = asked()
    only_limits["sizes"]["global_mm"] = None
    assert srq.working_wall(only_limits) is None                       # SURF-5's thin sheet stands
    assert srq.working_wall(asked(4.0)) == pytest.approx(4.0 * srq.WALL_PER_GLOBAL)
    assert srq.global_size(asked(2.5)) == 2.5


def test_the_thickness_row_is_told_as_a_surface_files():
    row = {"id": "r4", "field": "thickness", "asked": "wall thickness 1.2 mm", "applied": False, "met": None,
           "delivered": "not applied - each wall's thickness is measured from your CAD (1.2 mm)", "excess": 0}
    size = {"id": "r1", "field": "global", "asked": "global element size 4 mm", "applied": True, "met": True,
            "delivered": "elements are about 3.9 mm; some elements are smaller than half the wall, finer than a "
                         "shell usually needs", "excess": 0}
    rep = {"rows": [size, row], "met": 1, "missed": 0, "definitions": "d"}
    how = {"by": srq.BY_WORDS, "set_mm": 1.2, "differs": False}
    out = srq.told(rep, elements=100, thickness_mm=1.2, how=how)
    t = out["rows"][1]
    assert t["applied"] is True and t["met"] is True and t["delivered"] == "every shell property carries 1.2 mm"
    assert MR.line(t) == "Wall thickness 1.2 mm: met - every shell property carries 1.2 mm."
    assert out["met"] == 2 and out["missed"] == 0 and out["definitions"] == "d"
    assert out["rows"][0]["delivered"] == size["delivered"]             # a wall was given: the clause stands
    assert rep["rows"][1]["applied"] is False, "the mesher's own report is not changed"
    # the word table read another thickness: said with the one in the files, never as met
    t = srq.told(rep, elements=100, thickness_mm=2.0, how={**how, "differs": True})["rows"][1]
    assert t["applied"] is False and t["met"] is None and "the files carry 2 mm" in t["delivered"]
    # no thickness used at all: not applied, with the reason; and no "half the wall" where there is no wall
    out = srq.told(rep, elements=100, thickness_mm=None, how={"by": None, "set_mm": 1.2, "differs": False},
                   unread="a range (1-2 mm thick)")
    assert out["rows"][1]["delivered"] == ("not applied - a range (1-2 mm thick) was not read as a thickness: say it "
                                           "in mm")
    assert out["rows"][0]["delivered"] == "elements are about 3.9 mm"
    assert srq.told(None, elements=0, thickness_mm=None) is None
    assert srq.told({"rows": []}, elements=0, thickness_mm=1.2) is None      # nothing to say: the caller keeps its own


# ---------------------------------------------------------------------------------------- the surface run
@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    return _file(sx.bracket(), tmp_path_factory.mktemp("src"))


@pytest.fixture(scope="module")
def plain(tmp_path_factory, source):
    """The bracket with nothing said beyond a thickness: (workspace, record)."""
    ws = tmp_path_factory.mktemp("plain")
    return ws, surface_run.run(ws, source, mm_per_unit=1.0, thickness_mm=1.2, source="part.step")


@pytest.fixture(scope="module")
def sized(tmp_path_factory, source):
    """The bracket with "Global Mesh Size = 4 mm", the criteria list and "1.2 mm thick" in the notes."""
    ws = tmp_path_factory.mktemp("sized")
    req = asked(thickness="1.2 mm thick", value=1.2)
    got, how = srq.thickness(st.read([notes_of(thickness="1.2 mm thick")]), req)
    rec = surface_run.run(ws, source, mm_per_unit=1.0, thickness_mm=got.mm, source="part.step", requirements=req,
                          thickness_how=how)
    return ws, rec


def test_a_global_size_the_criteria_list_and_a_thickness_all_reach_a_surface_file(sized, plain):
    ws, rec = sized
    assert rec["made"] is True and rec["quality"]["invalid"] == 0
    rows = {r["field"]: r for r in rec["requirements"]["rows"]}
    assert len(rec["requirements"]["rows"]) == 11                      # 3 sizes, 7 limits, the thickness
    # the size asked is the size made (the rules' own bracket is much finer)
    assert rows["global"]["met"] is True and rec["sizing"]["global_mm"] == G
    assert 0.5 * G <= _typical_edge(ws) <= 1.5 * G
    assert rec["elements"] < plain[1]["elements"]
    # every quality limit is measured and told met or not, never left out
    limits = [r for r in rec["requirements"]["rows"] if r["field"].startswith("limit:")]
    assert len(limits) == 7 and all(r["applied"] is True and r["met"] in (True, False) for r in limits)
    # the thickness is in every solver file, and its row says so
    assert rows["thickness"]["met"] is True and rows["thickness"]["delivered"] == "every shell property carries 1.2 mm"
    for name, reader in (("shell.bdf", sw.read_bdf), ("shell.inp", sw.read_inp), ("shell_abaqus.inp", sw.read_inp),
                         ("shell.cdb", sw.read_cdb)):
        assert list(reader(ws / name).walls.values()) == [pytest.approx(0.0012)], name
    assert rec["walls_mm"] == [1.2] and rec["surface"]["thickness_by"] == srq.BY_WORDS
    assert rec["surface"]["notes"].startswith("The sizes are the 4 mm your notes ask for")
    # the record the delivery reads is the shell run's own, and the plan kept beside the surface says the same
    assert shell_run.read(ws)["requirements"] == rec["requirements"]
    kept = json.loads((ws / shell_run.FOLDER / "shell_plan.json").read_text())
    assert kept["requirements"] == rec["requirements"]


def test_the_same_notes_twice_give_the_same_files(sized, source, tmp_path):
    ws, rec = sized
    req = asked(thickness="1.2 mm thick", value=1.2)
    got, how = srq.thickness(st.read([notes_of(thickness="1.2 mm thick")]), req)
    again = surface_run.run(tmp_path, source, mm_per_unit=1.0, thickness_mm=got.mm, source="part.step",
                            requirements=req, thickness_how=how)
    for name in FILES:
        assert (tmp_path / name).read_bytes() == (ws / name).read_bytes(), name
    assert again["requirements"] == rec["requirements"] and again["sizing"] == rec["sizing"]


def test_with_a_global_size_and_no_thickness_the_sheet_is_not_sized_as_a_thin_sheet(source, tmp_path, monkeypatch):
    """SURF-7: the sizes are the user's and the working wall is their size - the thin-sheet guess (the sheet's size
    over 300, SURF-5) plays no part, and the mesh is made once whatever it leaves past a limit."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = sa.mesh_run
    seen: list[float] = []

    def spy(out, *, mm_per_unit=1.0, target=None, requirements=None):
        seen.append(json.loads((out / "shell.json").read_text())["pairs"][0]["thickness_mm"])
        mesh, applied = real(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
        applied["quality"]["nastran_past"] = {"T3_SKEW": 2}             # said to leave triangles past the limits
        return mesh, applied

    monkeypatch.setattr(sa, "mesh_run", spy)
    rec = surface_run.run(tmp_path, source, mm_per_unit=1.0, source="part.step", requirements=asked(limits=False))
    assert rec["made"] is True and seen == [pytest.approx(G * srq.WALL_PER_GLOBAL)], "one mesh, at the size asked"
    thin = surface_run.sizing_wall(json.loads((tmp_path / "surface_model.json").read_text())["sheets"][0]["size_mm"])
    assert rec["surface"]["sizing_wall_mm"] == [pytest.approx(G * srq.WALL_PER_GLOBAL)] and thin < 0.5
    assert rec["surface"]["sizing_by"] == "global size"
    # about 4 mm elements, where the thin-sheet guess made them a few times finer
    row = rec["requirements"]["rows"][0]
    assert row["field"] == "global" and row["met"] is True and "half the wall" not in row["delivered"]
    assert 0.5 * G <= _typical_edge(tmp_path) <= 1.5 * G
    # still no thickness: none is written, and the files say so
    assert rec["surface"]["thickness_set"] is False and rec["walls_mm"] == []
    assert list(sw.read_bdf(tmp_path / "shell.bdf").walls.values()) == [0.0]
    assert "WALL THICKNESS NOT SET" in (tmp_path / "shell.bdf").read_text()[:1500]


def test_limits_alone_keep_the_thin_sheet_sizing_and_reach_every_mesh_of_it(source, tmp_path, monkeypatch):
    """No global size: SURF-5 stands (sized as a thin sheet, sized again while elements are past the limits) - and
    the user's limits go to each of those meshes."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = sa.mesh_run
    seen: list[tuple[float, bool]] = []

    def spy(out, *, mm_per_unit=1.0, target=None, requirements=None):
        seen.append((json.loads((out / "shell.json").read_text())["pairs"][0]["thickness_mm"], bool(requirements)))
        mesh, applied = real(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
        if len(seen) == 1:
            applied["quality"]["nastran_past"] = {"T3_SKEW": 1}
        return mesh, applied

    monkeypatch.setattr(sa, "mesh_run", spy)
    req = asked()
    req["sizes"].update(global_mm=None, min_edge_mm=None, max_edge_mm=None)
    req["rows"] = [r for r in req["rows"] if r["field"].startswith("limit:")]
    rec = surface_run.run(tmp_path, source, mm_per_unit=1.0, source="part.step", requirements=req)
    assert rec["made"] is True and len(seen) == 2 and all(with_req for _w, with_req in seen)
    assert seen[1][0] == pytest.approx(2 * seen[0][0], rel=1e-5) and "sizing_by" not in rec["surface"]
    assert len(rec["requirements"]["rows"]) == 7


def test_nothing_asked_is_the_surface_run_as_it_was(plain, source, tmp_path):
    ws, rec = plain
    assert not ({"requirements", "sizing", "count_set_aside"} & set(rec))
    assert not ({"notes", "thickness_by", "sizing_by"} & set(rec["surface"]))
    # an empty set, or none: the same bytes
    again = surface_run.run(tmp_path, source, mm_per_unit=1.0, thickness_mm=1.2, source="part.step",
                            requirements=MR.requirements_of(MR.section("used", requirements=MR.empty())))
    for name in FILES:
        assert (tmp_path / name).read_bytes() == (ws / name).read_bytes(), name
    assert {k: v for k, v in again.items() if k not in ("seconds", "extract_s", "mesh_s")} == \
        {k: v for k, v in rec.items() if k not in ("seconds", "extract_s", "mesh_s")}


# ------------------------------------------------------------------------------------- through the driver
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


def _stage(shape, ws: Path) -> Path:
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


def _drive(ws: Path, mp, state: dict, reading: dict | None):
    """The real driver on a staged surface; the notes' reading is `reading` (a stubbed model), or - None - a model
    that fails the test if it is asked."""
    from meshpipeline.engines import runtime

    calls: list = []
    cited: list = []
    asked_model: list = []

    async def decide(st_, **_kw):
        asked_model.append(1)
        if reading is None:
            raise AssertionError("the model was asked on a run with no notes")
        return reading

    mp.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": 3, "timed_out": False, "log_tail": ""}))
    mp.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    if reading is not None:
        mp.setattr(MR, "decide", decide)
    else:
        from meshpipeline.contracts import model_inference as llm

        mp.setattr(llm, "call_builder_model", decide, raising=False)
    ok, _marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-surface-notes", publish=None, run=_Run()))
    return ok, calls, cited, asked_model


@pytest.fixture(scope="module")
def driven(tmp_path_factory):
    """The bracket's surface through the real driver: the criteria list at 4 mm and "1.2 mm thick" in the notes."""
    mp = pytest.MonkeyPatch()
    try:
        ws = _stage(sx.bracket(), tmp_path_factory.mktemp("driven") / "ws")
        notes = notes_of(thickness="1.2 mm thick")
        state = {"purpose": "structural", "request_txt": "FEA of a bracket", "user_notes": [{"id": "n1", "text": notes}]}
        sec = MR.section("used", requirements=asked(thickness="1.2 mm thick", value=1.2), source="model")
        ok, calls, cited, asked_model = _drive(ws, mp, state, sec)
    finally:
        mp.undo()
    return ws, ok, calls, cited, asked_model


def test_the_driver_reads_the_notes_before_the_surface_is_meshed_and_only_once(driven):
    ws, ok, calls, cited, asked_model = driven
    assert ok is True and calls == [] and asked_model == [1]
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and rec["walls_mm"] == [1.2] and rec["sizing"]["global_mm"] == G
    assert 0.5 * G <= _typical_edge(ws) <= 1.5 * G
    said = [(c["decided"], c["rule"]) for c in cited if c["stage"] == surface_run.GATE]
    assert said == [("surface part", "SURF-1"), ("thickness", "SURF-4"), ("shell mesh", "SURF-3"),
                    ("notes applied", "SURF-7"), ("shell delivered", "SURF-3")]
    applied = next(c for c in cited if c["rule"] == "SURF-7")
    assert applied["value"] == 11 and applied["inputs"]["thickness_by"] == srq.BY_WORDS
    assert any(c["rule"] == MR.RULE_READ for c in cited), "the reading itself is cited as on every route"


def test_the_result_tells_every_requirement_asked_against_delivered(driven):
    from meshpipeline.contracts.user_notes import read_remarks
    from meshpipeline.engines import fea_results

    ws, _ok, _calls, _cited, _asked = driven
    q = json.loads((ws / "quality.json").read_text())
    assert q["fea"]["surface"] is True and q["fea"]["thickness_set"] is True
    assert len(q["requirements"]["rows"]) == 11
    res = fea_results.results(ws)
    assert res["title"] == "FEA shell mesh" and "1.2 mm thick" in res["summary"]
    assert len(res["requirements"]["rows"]) == 11
    lines = " ".join(res["plan_words"])
    assert "Global element size 4 mm: met" in lines and "Wall thickness 1.2 mm: met" in lines
    remark = " ".join(str(r) for r in read_remarks(ws))
    assert "you asked for" in remark, remark


def test_a_surface_file_with_no_notes_asks_no_model_and_writes_nothing_new(tmp_path, monkeypatch):
    ws = _stage(sx.bracket(), tmp_path / "ws")
    ok, calls, cited, asked_model = _drive(ws, monkeypatch, {"purpose": "structural"}, None)
    assert ok is True and calls == [] and asked_model == []
    assert not (ws / MR.FILE).exists()
    rec = shell_run.read(ws)
    assert not ({"requirements", "sizing"} & set(rec)) and "notes" not in rec["surface"]
    said = [(c["decided"], c["rule"]) for c in cited]
    assert said == [("surface part", "SURF-1"), ("thickness", "SURF-4"), ("shell mesh", "SURF-3"),
                    ("shell delivered", "SURF-3")]
    assert "requirements" not in json.loads((ws / "quality.json").read_text())


def test_a_file_that_is_not_a_surface_carries_the_same_reading_on(tmp_path, monkeypatch):
    """A closed skin with a gap is not a surface part: the driver goes on as before, and the notes it read for the
    surface run are the ones the rest of the run uses - the model is not asked twice."""
    ws = _stage(sx.skin_with_pinhole(), tmp_path / "ws")
    asked_model: list = []
    sec = MR.section("used", requirements=asked(), source="model")

    async def decide(_state, **_kw):
        asked_model.append(1)
        return sec

    class _Reached(Exception):
        pass

    async def stop(*_a, **_k):
        raise _Reached

    monkeypatch.setattr(MR, "decide", decide)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    monkeypatch.setattr(FD, "shell_pass", stop)                         # the step right after the solid path's read
    monkeypatch.setattr(FD, "open_single_skins", lambda _ws: None)
    record = {"faces": {"Face_1": {"thickness_min_mm": 1.0}}, "bodies": 1, "notes": []}
    monkeypatch.setattr(FD, "measure_staged", lambda _ws: (record, None, 0.1))
    monkeypatch.setattr(FD.P, "zones", lambda _record, **_k: [])

    async def no_ai(*_a, **_k):
        return {}

    monkeypatch.setattr(FD.FA, "decide", no_ai)
    monkeypatch.setattr(FD, "element_target", lambda _state, _ai: None)
    state = {"purpose": "structural", "user_notes": [{"id": "n1", "text": notes_of()}]}
    with pytest.raises(_Reached):
        asyncio.run(FD.drive(ws, state, job_id="t", publish=None, run=_Run()))
    assert asked_model == [1] and surface_run.read(ws)["surface"]["kind"] == "gap"
    assert shell_run.requirements_in(ws) == asked()
