# Responsibility: Verify the team's meshing standard on every FEA route that meshes (STD-1..3): the solid plan, the shell mesh, a mixed mesh and a surface file each apply the merged set - settings only, notes only, and both with the notes winning one field - exactly as they apply the notes' set, and each result row says where its requirement came from; a standard alone gives the same mesh as the same words in the notes; with nothing in either the run is the run it was.
# Boundaries: the real FEA driver, shell run, mixed run and surface run on small built parts (gmsh + OCP, skipped without them); every reading stubbed (no model). No solver, no store.
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402
from tests import shell_fixtures as sf  # noqa: E402
from tests import surface_fixtures as sx  # noqa: E402
from tests.unit.engines import test_shell_requirements as TSR  # noqa: E402
from tests.unit.engines import test_surface_requirements as TSU  # noqa: E402
from tests.unit.engines.test_fea_requirements import _drive, _ToolRouter  # noqa: E402
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer  # noqa: E402

from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest  # noqa: E402
from meshpipeline.contracts.user_notes import read_remarks  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines import meshing_standard as MS  # noqa: E402

SOLID = "Global mesh size = 7 mm\nAspect ratio < 3"


def _solid_answer(g: float = 7.0, quote: str = "Global mesh size = 7 mm", aspect: bool = True) -> dict:
    return {"stated_unit": "none", "element_kind": [], "other": [],
            "sizes": [{"what": "global", "value": g, "unit": "mm", "quote": quote}],
            "limits": [{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 3,
                        "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "Aspect ratio < 3"}]
            if aspect else []}


def _kept(text: str, answer: dict) -> dict:
    return {"text": text, "reading": {"key": MS.key_of(text), "arguments": json.dumps(answer)}}


def _answer(args: dict) -> ModelRoundResult:
    return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=MR.TOOL,
                                                        arguments=json.dumps(args)),))


def _stage_solid(tmp_path, name):
    from tests.unit.engines.test_fea_driver import _stage

    return _stage(fx.plate_with_hole(), tmp_path / name)


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ the solid route ------------------------------
def test_a_solid_run_applies_the_standard_alone_and_says_it_came_from_settings(tmp_path, monkeypatch):
    from meshpipeline.engines.fea_results import results

    router = _ToolRouter()                                     # the kept reading: no model is asked for it
    ws = _stage_solid(tmp_path, "settings")
    state = {"purpose": "structural", "user_id": "u1", MS.STATE_KEY: _kept(SOLID, _solid_answer())}
    ok, marker, cited, said = _drive(ws, monkeypatch, state, router)
    assert ok, marker
    assert router.calls == 0
    assert [line for _k, line, _o in said if line.startswith("From your")] == [
        "From your settings: global element size 7 mm.", "From your settings: aspect ratio under 3."]
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert plan["base_size_m"] == pytest.approx(0.007)
    rows = json.loads((ws / "quality.json").read_text())["requirements"]["rows"]
    assert [(r["field"], r["from"]) for r in rows] == [("global", "settings"), ("limit:aspect_ratio", "settings")]
    asked = [r for r in results(ws)["check"]["rows"] if r["key"].startswith("asked_")]
    assert [r["key"] for r in asked] == ["asked_s1", "asked_s2"]
    remark = [r["text"] for r in read_remarks(ws, raw=True) if r["by"] == "mesh_requirements"]
    assert remark and remark[0].startswith("From your settings: ")
    assert any(e["rule"] == "STD-2" for e in cited)


def test_a_solid_run_with_both_takes_the_notes_size_and_keeps_the_standards_limit(tmp_path, monkeypatch):
    router = _ToolRouter(_answer(_solid_answer(9.0, "Global mesh size = 9 mm", aspect=False)))
    ws = _stage_solid(tmp_path, "both")
    state = {"purpose": "structural", "user_id": "u1", MS.STATE_KEY: _kept(SOLID, _solid_answer()),
             "user_notes": [{"text": "Global mesh size = 9 mm", "source": "console"}]}
    ok, marker, _cited, said = _drive(ws, monkeypatch, state, router)
    assert ok, marker
    assert router.calls == 1                                   # the notes only
    assert [line for _k, line, _o in said if line.startswith("From your")] == [
        "From your settings: aspect ratio under 3.", "From your notes: global element size 9 mm."]
    assert json.loads((ws / "fea_plan.json").read_text())["base_size_m"] == pytest.approx(0.009)
    rows = json.loads((ws / "quality.json").read_text())["requirements"]["rows"]
    assert [(r["field"], r["from"]) for r in rows] == [("limit:aspect_ratio", "settings"), ("global", "notes")]
    remark = [r["text"] for r in read_remarks(ws, raw=True) if r["by"] == "mesh_requirements"]
    assert remark and remark[0].startswith("From your settings and notes: ")


def test_a_solid_run_with_neither_is_the_run_it_was(tmp_path, monkeypatch):
    router = _ToolRouter()
    a, b = _stage_solid(tmp_path, "a"), _stage_solid(tmp_path, "b")
    ok_a, _m, _c, said_a = _drive(a, monkeypatch, {"purpose": "structural"}, router)
    ok_b, _m, _c, said_b = _drive(b, monkeypatch, {"purpose": "structural", MS.STATE_KEY: None}, router)
    assert ok_a and ok_b and router.calls == 0 and said_a == said_b
    for name in ("fea_plan.json", "mesh.inp"):
        assert _sha(a / name) == _sha(b / name), name
    # the quality record, but for the process's own memory peak (the same mesh, measured in another process)
    qa, qb = (json.loads((w / "quality.json").read_text()) for w in (a, b))
    assert {k: v for k, v in qa.items() if k != "peak_rss_mb"} == {k: v for k, v in qb.items() if k != "peak_rss_mb"}
    assert not (b / MR.FILE).exists()


# ------------------------------------------------------------------ the shell route ------------------------------
def _settings(text: str, answer: dict) -> dict:
    return MS.checked(json.dumps(answer), text, key=MS.key_of(text), source="kept")


def _notes_sec(text: str, answer: dict) -> dict:
    req, dropped, na = MR.check(json.dumps(answer), MR.notes_text({"user_notes": [{"text": text}]}))
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model")


def _criteria_at(g: float) -> tuple[str, dict]:
    text = CRITERIA.replace("= 10 mm", f"= {g:g} mm")
    answer = criteria_answer()
    answer["sizes"][0].update(value=g, quote=text.split("\n")[0])
    return text, answer


def _shell(ws, sec) -> dict:
    MR.write(ws, sec)
    from meshpipeline.engines import shell_run

    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged"]) == 0
    return shell_run.read(ws)


def test_a_shell_from_the_standard_is_the_shell_the_same_words_in_the_notes_make(tmp_path):
    plate = TSR._stage(sf.plate_with_hole(), tmp_path / "plate")
    text, answer = _criteria_at(5.0)
    by_settings = _shell(TSR._copy(plate, tmp_path / "s"), MS.merge(_settings(text, answer), MR.section("none")))
    by_notes = _shell(TSR._copy(plate, tmp_path / "n"), _notes_sec(text, answer))
    assert TSR._sha(tmp_path / "s") == TSR._sha(tmp_path / "n")
    assert by_settings["elements"] == by_notes["elements"]
    rs, rn = by_settings["requirements"]["rows"], by_notes["requirements"]["rows"]
    assert [r["from"] for r in rs] == ["settings"] * 10 and all("from" not in r for r in rn)
    strip = ("id", "from")
    assert [{k: v for k, v in r.items() if k not in strip} for r in rs] == \
        [{k: v for k, v in r.items() if k not in strip} for r in rn]


def test_a_shell_with_both_takes_the_notes_size_and_the_standards_limits(tmp_path):
    plate = TSR._stage(sf.plate_with_hole(), tmp_path / "plate")
    text, answer = _criteria_at(10.0)
    notes = "Global Mesh Size = 4 mm"
    merged = MS.merge(_settings(text, answer), _notes_sec(notes, {
        "stated_unit": "none", "element_kind": [], "limits": [], "other": [],
        "sizes": [{"what": "global", "value": 4, "unit": "mm", "quote": notes}]}))
    ws = TSR._copy(plate, tmp_path / "both")
    rec = _shell(ws, merged)
    rows = {r["field"]: r for r in rec["requirements"]["rows"]}
    assert rows["global"]["from"] == "notes" and rows["global"]["met"] is True
    assert 0.7 * 4.0 <= rows["global"]["measured"] <= 1.3 * 4.0
    assert rows["min_edge"]["from"] == "settings" and rows["min_edge"]["asked"].startswith(
        "shortest element edge over 2 mm")
    assert rows["limit:warpage"]["from"] == "settings"
    from meshpipeline.engines import shell_delivery as SD
    from meshpipeline.engines.fea_results import results

    assert SD.deliver(ws, rec)
    asked = [r for r in results(ws)["check"]["rows"] if r["key"].startswith("asked_")]
    assert any(r["key"] == "asked_r1" for r in asked) and any(r["key"].startswith("asked_s") for r in asked)
    from meshpipeline.engines.verdict_limits import fea_limits

    limits = [r for r in fea_limits(json.loads((ws / "quality.json").read_text())) if r["key"].startswith("asked_")]
    assert {r["label"].split(":")[0] for r in limits} == {"Your settings ask", "Your notes ask"}
    assert {r["source"].split(" (")[0] for r in limits} == {"your settings", "your notes"}


def test_a_mixed_run_applies_the_merged_set_to_its_shell_and_its_solid(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_driver as FD

    class _Run:
        deadline_s = None

    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    ws = TSR._stage(sf.plate_with_boss(), tmp_path / "ws")
    text, answer = _criteria_at(10.0)
    notes = "Global Mesh Size = 6 mm"
    MR.write(ws, MS.merge(_settings(text, answer), _notes_sec(notes, {
        "stated_unit": "none", "element_kind": [], "limits": [], "other": [],
        "sizes": [{"what": "global", "value": 6, "unit": "mm", "quote": notes}]})))
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "mixed"}, job_id="j", run=_Run()))
    assert rec["made"] is True, rec.get("why")
    rows = {r["field"]: r for r in rec["requirements"]["rows"]}
    assert rows["global"]["from"] == "notes" and 0.7 * 6.0 <= rows["global"]["measured"] <= 1.3 * 6.0
    assert rows["limit:warpage"]["from"] == "settings"
    from meshpipeline.engines import mixed_run

    plan = json.loads((ws / mixed_run.FOLDER / "solid" / "fea_plan.json").read_text())
    assert plan["base_size_m"] == pytest.approx(0.006)


# ------------------------------------------------------------------ a surface file ------------------------------
def test_a_surface_file_takes_the_standard_and_says_where_it_came_from(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_results, shell_run, surface_run

    ws = TSU._stage(sx.bracket(), tmp_path / "ws")
    text, answer = _criteria_at(TSU.G)
    state = {"purpose": "structural", MS.STATE_KEY: _kept(text, answer)}
    ok, calls, cited, asked_model = TSU._drive(ws, monkeypatch, state, None)   # no model may be asked
    assert ok is True and asked_model == []
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and rec["sizing"]["global_mm"] == TSU.G
    rows = json.loads((ws / "quality.json").read_text())["requirements"]["rows"]
    assert len(rows) == 10 and {r["from"] for r in rows} == {"settings"}
    words = " ".join(fea_results.results(ws)["plan_words"])
    assert "Global element size 4 mm (from your settings): met" in words
    remark = " ".join(str(r) for r in read_remarks(ws))
    assert remark.startswith("From your settings: "), remark
    assert any(c["rule"] == "STD-2" for c in cited)
