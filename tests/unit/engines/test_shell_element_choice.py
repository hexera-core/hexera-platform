# Responsibility: Verify a shell's element choice end to end (SHSTAGE-8, STD-4, NOTE-11): where the stage's "Elements:
# Quads | Triangles" starts (the notes when they say one, else the settings page's switch, else Quads), when a flip asks
# first, what Proceed settles, how the stage, the notes and the settings combine on the ONE field (kind.shell), and that
# a run makes and says what was chosen - "Triangles, from your settings", "Quads, chosen on the stage" - with an
# all-triangle shell that has no quad in it and a quad-dominant one byte-identical to a run with no choice at all.
# Boundaries: the real FEA driver and shell run on a small built plate (gmsh + OCP, skipped without them); the notes'
# model stubbed. No solver, no store.
from __future__ import annotations

import json

import pytest

from meshpipeline.application import shell_elements as SE
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _answer(kinds=()) -> str:
    return json.dumps({"stated_unit": "none", "sizes": [], "limits": [], "other": [],
                       "element_kind": [{"what": w, "value": 0, "quote": q} for w, q in kinds]})


def _notes(text: str, kinds=()) -> dict:
    req, dropped, na = MR.check(_answer(kinds), text)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na, source="model")


def _std(kind: str = "quads", text: str = "") -> dict:
    return MS.snapshot(text, None, shell_elements=kind) or {}


# ------------------------------------------------------------------ the words that name an element kind (NOTE-11)
@pytest.mark.parametrize("text,kind", [
    ("Shell elements: triangles", "triangles"), ("Shell elements: trias", "triangles"), ("tri mesh", "triangles"),
    ("use triangles for shells", "triangles"), ("triangles only", "triangles"), ("tria mesh please", "triangles"),
    ("no quads", "triangles"), ("quads only", "quads"), ("no triangles", "quads"), ("all-quad mesh", "quads"),
    ("Shell elements: quads", "quads"), ("mesh it as a shell", ""), ("10 mm elements", "")])
def test_the_fixed_phrases_name_the_element_kind(text, kind):
    assert MR.kind_in_words(text) == kind


@pytest.mark.parametrize("text", ["Shell elements: triangles", "trias", "tri mesh", "use triangles for shells"])
def test_a_kind_the_model_left_in_no_field_is_read_from_the_users_line(text):
    notes = f"Global size 10 mm.\n{'Shell elements: ' + text if text == 'trias' else text}"
    req, _d, na = MR.check(_answer(), notes)
    assert req["kind"]["shell"] == "triangles"
    assert [r["field"] for r in req["rows"]] == ["triangles_only"]
    assert req["rows"][0]["asked"] == "shell elements: triangles only" and na == []


def test_a_kind_the_model_did_read_is_never_overridden_by_the_phrases():
    req, _d, _na = MR.check(_answer([("quads_only", "quads only")]), "quads only. triangles only on the flange")
    assert req["kind"]["shell"] == "quads"


# ------------------------------------------------------------------ where the stage starts, and when it asks first
def test_the_stage_starts_on_the_notes_else_the_settings_switch_else_quads():
    tri_said = [{"role": "user", "content": "shell please, use triangles for shells"}]
    assert SE.start(tri_said, standard=_std("quads", "Global size 10 mm")) == {"value": "triangles", "from": "notes"}
    assert SE.start([], standard=_std("triangles")) == {"value": "triangles", "from": "settings"}
    assert SE.start([], standard=_std("quads", "Global size 10 mm")) == {"value": "quads", "from": "settings"}
    assert SE.start([], standard=None) == {"value": "quads", "from": "default"}


def test_a_flip_away_from_the_settings_or_the_notes_asks_one_short_question():
    assert SE.confirm_words({"value": "quads", "from": "settings"}) == {
        "triangles": "Your settings say Quads. Use Triangles for this run?"}
    assert SE.confirm_words({"value": "triangles", "from": "notes"}) == {
        "quads": "Your notes say Triangles. Use Quads for this run?"}
    assert SE.confirm_words({"value": "quads", "from": "default"}) == {}
    view = SE.view({"mesh_as": "shell"}, [], standard=_std("triangles"))
    assert view["value"] == "triangles" and view["from"] == "settings"
    assert view["confirm"] == {"quads": "Your settings say Triangles. Use Quads for this run?"}
    assert [o["label"] for o in view["options"]] == ["Quads", "Triangles"]


def test_the_notes_asking_too_is_one_line(monkeypatch):
    monkeypatch.setattr(SE, "CONFIRM_FROM", ("settings",))
    assert SE.confirm_words({"value": "triangles", "from": "notes"}) == {}


def test_no_control_for_a_solid_or_a_shell_that_cannot_be_made():
    assert SE.view(None, []) is None
    assert SE.view({"mesh_as": "solid"}, []) is None


def test_proceed_keeps_only_the_users_own_click():
    started = {"value": "quads", "from": "settings"}
    assert SE.settle("triangles", started) == {"pick": "triangles", "by": "stage", "value": "triangles",
                                               "words": "Elements: triangles, chosen on the stage."}
    assert SE.settle("quads", started)["pick"] == "" and SE.settle("", started)["by"] == "settings"
    assert SE.settle("quads", {"value": "quads", "from": "default"})["words"] == ""


# ------------------------------------------------------------------ one field: settings < notes < the stage (STD-4)
def test_the_settings_switch_alone_makes_triangles_from_your_settings():
    sec = MS.merge(MS.switched(MR.section("none"), "triangles"), MR.section("none"))
    assert MR.requirements_of(sec)["kind"]["shell"] == "triangles"
    assert MS.elements_of(sec)["words"] == "Triangles, from your settings."
    assert "From your settings: shell elements: triangles only." in MR.words(sec)


def test_the_settings_switch_on_quads_changes_nothing():
    notes = MR.section("none")
    assert MS.merge(MS.switched(MR.section("none"), "quads"), notes) is notes
    assert MS.snapshot("", None, shell_elements="quads") is None
    assert MS.snapshot("Global size 10 mm", None) == {"text": "Global size 10 mm"}


def test_the_notes_win_over_the_settings_switch():
    tri = _notes("triangles only", [("triangles_only", "triangles only")])
    sec = MS.merge(MS.switched(MR.section("none"), "quads"), tri)
    assert MS.elements_of(sec) == {"kind": "triangles", "from": "notes", "words": "Triangles, from your notes."}
    quads = _notes("quads only", [("quads_only", "quads only")])
    sec = MS.merge(MS.switched(MR.section("none"), "triangles"), quads)
    assert MS.elements_of(sec)["words"] == "Quads only, from your notes."


def test_a_click_on_the_stage_wins_over_both():
    tri = _notes("triangles only", [("triangles_only", "triangles only")])
    merged = MS.merge(MS.switched(MR.section("none"), "triangles"), tri)
    sec = MS.stage(merged, "quads")
    assert MR.requirements_of(sec) is None                       # quad-dominant: the rules' own shell
    assert MS.elements_of(sec)["words"] == "Quads, chosen on the stage."
    assert any(w.startswith("Not applied: shell elements: triangles only") for w in MR.words(sec))
    sec = MS.stage(MS.merge(MS.switched(MR.section("none"), "quads"), MR.section("none")), "triangles")
    assert MR.requirements_of(sec)["kind"]["shell"] == "triangles"
    assert MS.elements_of(sec)["words"] == "Triangles, chosen on the stage."


def test_quads_on_the_stage_keeps_a_stricter_quads_only_from_the_notes():
    quads = _notes("quads only", [("quads_only", "quads only")])
    sec = MS.stage(quads, "quads")
    assert MR.requirements_of(sec)["kind"]["shell"] == "quads"
    assert MS.elements_of(sec)["words"] == "Quads only, from your notes."


def test_no_pick_is_the_section_itself():
    sec = MR.section("none")
    assert MS.stage(sec, "") is sec and MS.stage(sec, None) is sec
    assert MS.elements_of(sec)["words"] == "Quads, the default."


# ------------------------------------------------------------------ a run makes and says it
def _run(tmp_path, name: str, state: dict, monkeypatch, router=None):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf
    from tests.unit.engines import test_shell_delivery as TSD

    from meshpipeline.contracts import decision_cache
    from meshpipeline.contracts import model_inference as llm
    from meshpipeline.engines import shell_run

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    if router is not None:
        monkeypatch.setattr(llm, "_router", router)
    ws = TSD._stage(sf.plate_with_hole(), tmp_path / name)
    ok, _m, calls = TSD._drive(ws, monkeypatch, {"purpose": "structural", "fea_mesh_as": "shell", **state})
    assert ok and calls == []
    plan = json.loads((ws / "fea_plan.json").read_text())
    return ws, shell_run.read(ws), plan


def test_a_run_makes_triangles_from_the_settings_switch_and_quads_when_the_stage_says_so(tmp_path, monkeypatch):
    import hashlib

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    _ws0, plain, plan0 = _run(tmp_path, "plain", {}, monkeypatch)
    ws1, tri, plan1 = _run(tmp_path, "tri", {MS.STATE_KEY: _std("triangles")}, monkeypatch)
    ws2, back, plan2 = _run(tmp_path, "back", {MS.STATE_KEY: _std("triangles"), MS.STAGE_KEY: "quads"}, monkeypatch)
    assert plain["quads"] > 0 and tri["quads"] == 0 and tri["trias"] > plain["elements"]
    assert plan0["words"][1] == "Quads, the default."
    assert plan1["words"][1] == "Triangles, from your settings."
    assert plan2["words"][1] == "Quads, chosen on the stage."
    # the stage's Quads is today's shell, file for file
    assert (back["elements"], back["quads"], back["trias"]) == (plain["elements"], plain["quads"], plain["trias"])
    for f in ("shell.bdf", "shell_abaqus.inp", "shell.cdb", "shell.msh"):
        assert _sha(_ws0 / f) == _sha(ws2 / f), f
    # every deck says triangles: CTRIA3, CalculiX S6 (SHWRITE-2), Abaqus S3, SHELL181 triangles
    assert "$ Mesh only: 0 CQUAD4 and" in (ws1 / "shell.bdf").read_text()
    assert "*ELEMENT, TYPE=S6" in (ws1 / "shell.inp").read_text()
    assert "*ELEMENT, TYPE=S3" in (ws1 / "shell_abaqus.inp").read_text()
    assert "SHELL181 triangles" in (ws1 / "shell.cdb").read_text()
    from meshpipeline.engines.fea_results import results

    res = results(ws1)
    assert res["summary"].startswith("A mid-surface shell:") and "triangular plate elements" in res["summary"]
    assert res["plan_words"][1] == "Triangles, from your settings."


def test_a_run_takes_triangles_from_the_notes_over_the_settings(tmp_path, monkeypatch):
    from tests.unit.engines.test_fea_requirements import _ToolRouter

    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

    router = _ToolRouter(ModelRoundResult(tool_calls=(ToolCallRequest(
        id="call_abcdef123", name=MR.TOOL, arguments=_answer([("triangles_only", "use triangles for shells")])),)))
    state = {MS.STATE_KEY: _std("quads", "Global size 10 mm"),
             "user_notes": [{"id": "n1", "text": "use triangles for shells", "source": "chat"}]}
    # the standard's text is read too: its kept reading stands in for the model
    text = state[MS.STATE_KEY]["text"]
    state[MS.STATE_KEY]["reading"] = {"key": MS.key_of(text), "arguments": _answer()}
    _ws, rec, plan = _run(tmp_path, "notes", state, monkeypatch, router)
    assert rec["quads"] == 0 and rec["trias"] > 0
    assert plan["words"][1] == "Triangles, from your notes."


# ------------------------------------------------------------------ the decks (SHWRITE-2) and the clean-up
def _square(trias_only: bool):
    import numpy as np

    from meshpipeline.engines.shell_mesh import ShellMesh

    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], dtype=float)
    if trias_only:
        quads, trias = np.zeros((0, 4), dtype=np.int64), np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    else:
        quads, trias = np.array([[0, 1, 2, 3]], dtype=np.int64), np.zeros((0, 3), dtype=np.int64)
    n = len(quads) + len(trias)
    m = ShellMesh(xyz=xyz, quads=quads, trias=trias, wall=np.full(n, 2.0), mid=["Mid_1"] * n, part=["Part_1"] * n)
    m.groups["Face_1"] = np.arange(n)
    return m


def test_an_all_triangle_calculix_deck_is_s6_with_the_corner_numbers_kept(tmp_path):
    from meshpipeline.engines import shell_writers as sw

    sw.write_all(tmp_path, _square(True))
    back = sw.read_inp(tmp_path / "shell.inp")
    assert sorted(len(c) for c in back.elements.values()) == [6, 6]
    assert len(back.nodes) == 4 + 5                       # four corners, five edges
    assert back.nodes[1] == (0.0, 0.0, 0.0) and back.nodes[3] == (0.01, 0.01, 0.0)
    assert back.elements[1][3:] == (5, 6, 7)              # the mid-side nodes after the corners
    text = (tmp_path / "shell.inp").read_text()
    assert "*ELEMENT, TYPE=S6" in text and "TYPE=S3" not in text
    assert "*ELEMENT, TYPE=S3" in (tmp_path / "shell_abaqus.inp").read_text()
    assert "2 SHELL181 triangles (3-node: K repeated as L)" in (tmp_path / "shell.cdb").read_text()
    assert "$ Mesh only: 0 CQUAD4 and 2 CTRIA3" in (tmp_path / "shell.bdf").read_text()


def test_a_quad_shell_deck_is_as_before(tmp_path):
    from meshpipeline.engines import shell_writers as sw

    sw.write_all(tmp_path, _square(False))
    text = (tmp_path / "shell.inp").read_text()
    assert "*ELEMENT, TYPE=S4" in text and "S6" not in text and "All triangles" not in text
    assert "SHELL181 triangles" not in (tmp_path / "shell.cdb").read_text()


def test_a_cap_taken_in_on_an_all_triangle_shell_is_cut_into_triangles_only():
    import numpy as np

    from meshpipeline.engines import shell_quality as SQ

    # a cap (apex a sliver off its long edge) beside a fair triangle: as one polygon the best cut is a quad
    xyz = np.array([[0, 0, 0], [10, 0, 0], [5, 0.05, 0], [5, -6, 0]], dtype=float)
    T = np.array([[0, 1, 2], [1, 0, 3]], dtype=np.int64)
    Q = np.zeros((0, 4), dtype=np.int64)
    q_any, t_any, *_ = SQ.absorb_caps(xyz, Q, T, [], [0, 0], np.array([2.0]))
    q_tri, t_tri, _qf, _tf, gone = SQ.absorb_caps(xyz, Q, T, [], [0, 0], np.array([2.0]), quads_too=False)
    assert len(q_tri) == 0
    assert len(q_any) + len(t_any) >= 1 and (gone == 0 or len(t_tri) >= 1)


def test_the_settings_page_keeps_its_switch():
    from meshpipeline.application import meshing_standard_service as svc

    assert svc.EMPTY["shell_elements"] == "quads"
    assert svc.view_of("", None, shell_elements="triangles")["shell_elements"] == "triangles"
    assert svc.view_of("x", None, shell_elements="nonsense")["shell_elements"] == "quads"
    assert MS.standard_of({MS.STATE_KEY: MS.snapshot("", None, shell_elements="triangles")}) == {
        "text": "", "shell_elements": "triangles"}
    assert MS.standard_of({MS.STATE_KEY: {"text": "", "shell_elements": "quads"}}) is None


def test_the_api_takes_only_quads_or_triangles():
    from pydantic import ValidationError

    from meshpipeline.api.v1.organization import MeshingStandardIn

    assert MeshingStandardIn(text="x").shell_elements == "quads"
    assert MeshingStandardIn(text="", shell_elements="triangles").shell_elements == "triangles"
    with pytest.raises(ValidationError):
        MeshingStandardIn(text="", shell_elements="tets")
