# Responsibility: Verify the user's mesh requirements on the solid FEA mesh (PLAN-fea-33, 34) and in the driver: a size the notes ask for the whole mesh becomes the sizes per part the plan already holds; every requirement is told asked against delivered on the built tetrahedra, a limit with no lever measured and never called met when it is not; the driver reads the notes once, tells the user before meshing, and with no notes asks nothing, writes nothing and says nothing.
# Boundaries: plain zones, a hand-made tetrahedral mesh, a fake model router and one plate planned for real (gmsh + OCP, skipped without them). Nothing is meshed here.
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import fea_requirements as FR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines.gmsh.abaqus_deck import SolidMesh


def _req(notes: str, **fields) -> dict:
    base = {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    req, dropped, _na = MR.check(json.dumps(base), notes)
    assert dropped == [], dropped
    return req


def _size(what: str, value: float) -> dict:
    return {"what": what, "value": value, "unit": "mm", "quote": f"{what} {value:g} mm"}


def _sizes(**kw) -> dict:
    entries = [_size(k, v) for k, v in kw.items()]
    return _req(", ".join(e["quote"] for e in entries), sizes=entries)


# ------------------------------------------------------------------ PLAN-fea-33: a size for the whole mesh ---
def test_a_global_size_is_every_parts_own_size():
    got = FR.whole_sizes({}, _sizes(**{"global": 10}), ["Part_1"], base_m=0.02)
    assert got == {"Part_1": {"min_m": 0.005, "max_m": 0.015, "name": "the whole mesh", "whole": True}}
    # with their own shortest edge, no zone is held coarser than it; the part's size is still the global size
    got = FR.whole_sizes({}, _sizes(**{"global": 10, "min_edge": 2}), ["Part_1"], base_m=0.02)
    e = got["Part_1"]
    assert e["min_m"] == pytest.approx(0.002) and 0.5 * (e["min_m"] + e["max_m"]) == pytest.approx(0.010)


def test_a_part_the_user_sized_themselves_keeps_its_own_size():
    own = {"Part_2": {"min_m": 0.003, "max_m": 0.004, "quote": "3-4 mm on the rail"}}
    got = FR.whole_sizes(own, _sizes(**{"global": 10}), ["Part_1", "Part_2", "Part_3"], base_m=0.02)
    assert got["Part_2"] == own["Part_2"] and set(got) == {"Part_1", "Part_2", "Part_3"}
    assert got["Part_1"]["whole"] and got["Part_3"]["whole"]


def test_a_longest_edge_alone_only_caps_and_a_shortest_alone_only_holds():
    # the rules' own base (20 mm) is over the 8 mm asked: the base comes down to it, nothing is held up
    got = FR.whole_sizes({}, _sizes(max_edge=8), ["Part_1"], base_m=0.02)["Part_1"]
    assert got["min_m"] == 0.0 and 0.5 * (got["min_m"] + got["max_m"]) == pytest.approx(0.008)
    # the rules' own sizes are already under it: nothing to hold
    assert FR.whole_sizes({}, _sizes(max_edge=50), ["Part_1"], base_m=0.02) == {}
    # a shortest edge alone: every zone over it, the base the rules' own
    got = FR.whole_sizes({}, _sizes(min_edge=3), ["Part_1"], base_m=0.02)["Part_1"]
    assert got["min_m"] == pytest.approx(0.003) and 0.5 * (got["min_m"] + got["max_m"]) == pytest.approx(0.02)


def test_no_size_in_the_notes_leaves_the_sizes_as_they_were():
    own = {"Part_2": {"min_m": 0.003, "max_m": 0.004}}
    for nothing in (None, {}, MR.empty(), _req("aspect ratio under 5", limits=[
            {"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 5, "allowed_percent": 0,
             "outer": 0, "has_outer": False, "quote": "aspect ratio under 5"}])):
        assert FR.whole_sizes(own, nothing, ["Part_1", "Part_2"], base_m=0.02) == own
        assert FR.whole_sizes(None, nothing, ["Part_1"], base_m=0.02) == {}


def test_the_parts_are_keyed_as_the_plan_keys_them():
    zones = [{"kind": "wall", "faces": ["Face_3"]}, {"kind": "hole", "faces": ["Face_7"]}]
    assert FR.part_ids(zones) == ["Part_1"] and FR.part_ids([]) == ["Part_1"]
    asm = [{"kind": "part", "part": "Part_9", "faces": ["Part_9_Face_1"]},
           {"kind": "wall", "faces": ["Part_9_Face_3"]}, {"kind": "fillet", "faces": ["Part_7_Face_1"]}]
    assert FR.part_ids(asm) == ["Part_9", "Part_7"]


def test_the_whole_mesh_is_held_like_a_sized_part_and_said_once():
    wall = {"id": "W3", "kind": "wall", "faces": ["Face_3"], "size_m": 0.0006, "reach_m": 0.0012, "thickness_m": 0.0012}
    hole = {"id": "H2", "kind": "hole", "faces": ["Face_7"], "size_m": 0.0005, "reach_m": 0.001, "radius_m": 0.02,
            "spot": "H2"}
    sizes = FR.whole_sizes({}, _sizes(**{"global": 10}), FR.part_ids([wall, hole]), base_m=0.02)
    zs = {z["id"]: z for z in P.user_sized([wall, hole], sizes)}
    assert zs["W3"]["size_m"] == pytest.approx(0.005) and zs["H2"]["size_m"] == pytest.approx(0.005)
    assert P.user_base(sizes) == pytest.approx(0.010)
    said = P.user_words({"user_sizes": sizes, "zones": list(zs.values())})
    assert said[0].startswith("You asked for 5-15 mm elements on the whole mesh: they are meshed at 10 mm")
    # two parts, one line
    two = FR.whole_sizes({}, _sizes(**{"global": 10}), ["Part_1", "Part_2"], base_m=0.02)
    assert P.user_words({"user_sizes": two, "zones": []})[0].count("the whole mesh") == 1


def test_a_real_part_takes_the_global_size(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    rules = FD.choose_plan(record, surface, diag)
    sizes = FR.whole_sizes({}, _sizes(**{"global": 7}), FR.part_ids(P.zones(record)), base_m=P.base_size(diag))
    plan = FD.choose_plan(record, surface, diag, sizes=sizes)
    assert plan["base_size_m"] == pytest.approx(0.007)
    assert all(z["size_m"] >= 0.0035 * (1 - 1e-6) or z.get("valid_held") for z in plan["zones"])
    assert plan["estimate"]["elements"] != rules["estimate"]["elements"]
    assert any("on the whole mesh" in w for w in plan["words"])
    # and with nothing asked the plan is the rules' own, to the byte
    again = FD.choose_plan(record, surface, diag, sizes=FR.whole_sizes({}, None, ["Part_1"], base_m=P.base_size(diag)) or None)
    assert json.dumps(again, sort_keys=True, default=str) == json.dumps(rules, sort_keys=True, default=str)


# ------------------------------------------------------------------ PLAN-fea-34: asked against delivered ----
def _tets(shapes: list[list[tuple]]) -> SolidMesh:
    pts, els = [], []
    for s in shapes:
        els.append([len(pts) + k + 1 for k in range(4)])
        pts += s
    return SolidMesh(node_ids=np.arange(1, len(pts) + 1), xyz=np.array(pts, dtype=float) * 1e-3,
                     elements=np.array(els, dtype=np.int64), element_volume=np.ones(len(els), dtype=np.int64))


REGULAR = [(0, 0, 0), (10, 0, 0), (5, 8.660254, 0), (5, 2.886751, 8.164966)]       # every edge 10 mm
LONG = [(0, 0, 0), (60, 0, 0), (5, 8.660254, 0), (5, 2.886751, 8.164966)]          # one edge 60 mm


def test_the_solid_report_measures_each_requirement_on_the_tetrahedra():
    notes = "global size 10 mm, shortest edge 5 mm, longest edge 15 mm, aspect ratio under 4, warpage under 15, tet4"
    req = _req(notes, sizes=[{"what": "global", "value": 10, "unit": "mm", "quote": "global size 10 mm"},
                             {"what": "min_edge", "value": 5, "unit": "mm", "quote": "shortest edge 5 mm"},
                             {"what": "max_edge", "value": 15, "unit": "mm", "quote": "longest edge 15 mm"}],
               element_kind=[{"what": "linear", "value": 0, "quote": "tet4"}],
               limits=[{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 4,
                        "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "aspect ratio under 4"},
                       {"measure": "warpage", "shape": "any", "direction": "max", "target": 15, "allowed_percent": 0,
                        "outer": 0, "has_outer": False, "quote": "warpage under 15"}])
    rep = FR.report(_tets([REGULAR] * 9 + [LONG]), req, order=1)
    rows = {r["field"]: r for r in rep["rows"]}
    assert rows["global"]["met"] is True and rows["global"]["measured"] == pytest.approx(10.0, rel=1e-3)
    assert rows["min_edge"]["met"] is True
    assert (rows["max_edge"]["met"], rows["max_edge"]["outside"], rows["max_edge"]["outside_pct"]) == (None, 1, 10.0)
    assert rows["max_edge"]["worst"] == pytest.approx(60.0, rel=1e-3)
    aspect = rows["limit:aspect_ratio"]
    assert (aspect["met"], aspect["outside"]) == (False, 1) and aspect["worst"] == pytest.approx(6.0, rel=1e-2)
    assert MR.line(aspect).startswith("Aspect ratio under 4: not fully met - 10% of elements (1) are over 4; the worst is")
    # what a tetrahedron has no measure for, and what the solid route has no setting for, is told - never as met
    assert rows["limit:warpage"]["met"] is None and "not a measure of tetrahedra" in rows["limit:warpage"]["delivered"]
    assert rows["linear"]["met"] is None and rows["linear"]["applied"] is False
    assert (rep["met"], rep["missed"]) == (2, 1)
    # the verdict's rows: only what was measured, a miss counted in elements, each with its own plain line
    from meshpipeline.engines.verdict_limits import words

    asked = MR.asked_rows({"requirements": rep})
    assert [r["key"] for r in asked] == ["asked_r1", "asked_r2", "asked_r5"]
    assert [r["measured"] for r in asked] == [0, 0, 1] and words(asked[2]) == MR.line(rows["limit:aspect_ratio"])
    assert MR.line(rows["max_edge"]) == ("Longest element edge under 15 mm: a target - 10% of elements (1) have an "
                                         "edge longer than 15 mm (the longest is 60 mm).")
    assert FR.report(_tets([REGULAR]), None) is None and FR.report(_tets([REGULAR]), MR.empty()) is None
    assert FR.summary(rep).startswith("Of the 6 things you asked for: 2 met, 1 not fully met: aspect ratio under 4")
    assert "could not be applied to a solid mesh" in FR.summary(rep)


def test_the_verdict_and_the_result_read_the_users_rows_from_the_quality_record():
    from meshpipeline.engines.verdict_limits import fea_limits

    rep = FR.report(_tets([REGULAR] * 9 + [LONG]), _req("aspect ratio under 4", limits=[
        {"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 4, "allowed_percent": 0, "outer": 0,
         "has_outer": False, "quote": "aspect ratio under 4"}]))
    quality = {"fea": {"plan": True}, "element_order": 2, "requirements": rep}
    rows = fea_limits(quality)
    (mine,) = [r for r in rows if r["key"].startswith("asked_")]
    assert mine["label"] == "You asked: aspect ratio under 4" and mine["measured"] == 1
    # and with no requirement the rows are exactly the rows of before
    plain = fea_limits({"fea": {"plan": True}, "element_order": 2})
    assert [r["key"] for r in rows if not r["key"].startswith("asked_")] == [r["key"] for r in plain]
    assert not any(r["key"].startswith("asked_") for r in plain)


# ------------------------------------------------------------------ the driver reads the notes once ---------
class _Router:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer, exc, 0

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.answer


class _Publish:
    def __init__(self):
        self.said = []

    async def anarrate(self, kind, line, n=None, of=None, unit="", op_id=""):
        self.said.append((kind, line, op_id))


class _Run:
    deadline_s = None


def _read(ws, monkeypatch, state, router):
    ws.mkdir(exist_ok=True)
    cited: list = []
    monkeypatch.setattr(llm, "_router", router)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    publish = _Publish()
    sec = asyncio.run(FD.read_requirements(ws, state, job_id="j1", run=_Run(), publish=publish))
    return sec, cited, publish.said


def test_no_notes_asks_nothing_cites_nothing_says_nothing_and_writes_nothing(tmp_path, monkeypatch):
    router = _Router(exc=AssertionError("the model must not be asked"))
    for state in ({}, {"purpose": "structural", "fea_mesh_as": "shell"}, {"user_notes": []}):
        sec, cited, said = _read(tmp_path / "ws", monkeypatch, state, router)
        assert sec["status"] == "none" and cited == [] and said == []
    assert router.calls == 0 and list((tmp_path / "ws").iterdir()) == []


def test_the_notes_are_read_once_told_before_meshing_and_kept_for_the_mesh_runs(tmp_path, monkeypatch):
    answer = ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=MR.TOOL,
                                                           arguments=json.dumps(criteria_answer())),))
    router = _Router(answer)
    state = {"user_notes": [{"text": CRITERIA, "source": "console"}], "user_id": "u1"}
    sec, cited, said = _read(tmp_path, monkeypatch, state, router)
    assert router.calls == 1 and sec["status"] == "used"
    # told before any mesh is made: one short line each, in the user's order
    assert [line for _k, line, _o in said][:3] == [
        "From your notes: global element size 10 mm.",
        "From your notes: shortest element edge over 5 mm (0.5 x the global size).",
        "From your notes: longest element edge under 15 mm (1.5 x the global size)."]
    assert len(said) == 10 and all(k == "say" for k, _l, _o in said)
    assert cited[0]["rule"] == "NOTE-7" and cited[0]["stage"] == "fea_ai"
    # the shell and mixed runs (their own processes) read it from the workspace
    from meshpipeline.engines import shell_run

    assert shell_run.requirements_in(tmp_path)["sizes"]["global_mm"] == 10.0


def test_a_model_that_is_down_never_fails_the_run_and_is_said(tmp_path, monkeypatch):
    from meshpipeline.contracts import decision_cache

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    state = {"user_notes": [{"text": CRITERIA, "source": "console"}]}
    sec, cited, said = _read(tmp_path, monkeypatch, state, _Router(exc=TimeoutError()))
    assert sec["status"] == "failed" and MR.requirements_of(sec) is None
    assert not (tmp_path / MR.FILE).exists()                      # nothing for the mesh runs to apply
    assert cited[0]["rule"] == "NOTE-9" and len(said) == 1 and "were not applied this run" in said[0][1]


# ------------------------------------------------------------------ a solid run, end to end ------------------
class _ToolRouter(_Router):
    """Answers the mesh-requirements question only; every other AI step of the run finds the model down."""

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        if not tools or tools[0]["function"]["name"] != MR.TOOL:
            raise TimeoutError()
        self.calls += 1
        return self.answer


class _DriveRun(_Run):
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        from types import SimpleNamespace
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws, monkeypatch, state, router):
    from types import SimpleNamespace

    from meshpipeline.contracts import decision_cache
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import fea_adapter

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_router", router)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    publish = _Publish()
    ok, marker, _o = asyncio.run(FD.drive(ws, state, job_id=f"t-{ws.name}", publish=publish, run=_DriveRun()))
    return ok, marker, cited, publish.said


def test_a_solid_run_applies_the_global_size_and_tells_asked_against_delivered(tmp_path, monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    from meshpipeline.contracts.user_notes import read_remarks
    from meshpipeline.engines.fea_results import results

    notes = "Global mesh size = 7 mm\nAspect ratio < 3\nWarpage < 15 deg"
    lines = notes.split("\n")
    answer = {"stated_unit": "none", "element_kind": [], "other": [],
              "sizes": [{"what": "global", "value": 7, "unit": "mm", "quote": lines[0]}],
              "limits": [{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 3,
                          "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": lines[1]},
                         {"measure": "warpage", "shape": "any", "direction": "max", "target": 15,
                          "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": lines[2]}]}
    router = _ToolRouter(ModelRoundResult(tool_calls=(ToolCallRequest(
        id="call_abcdef123", name=MR.TOOL, arguments=json.dumps(answer)),)))
    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    state = {"purpose": "structural", "user_notes": [{"text": notes, "source": "console"}], "user_id": "u1"}
    ok, marker, cited, said = _drive(ws, monkeypatch, state, router)
    assert ok, marker
    assert router.calls == 1
    # before meshing: what was read, one short line each
    assert [line for _k, line, _o in said if line.startswith("From your notes")] == [
        "From your notes: global element size 7 mm.", "From your notes: aspect ratio under 3.",
        "From your notes: warpage under 15°."]
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert plan["base_size_m"] == pytest.approx(0.007)
    assert any("You asked for 3.5-10.5 mm elements on the whole mesh" in w for w in plan["words"])
    assert "From your notes: global element size 7 mm." in plan["words"]
    # after: asked against delivered, measured on the built mesh
    rows = {r["field"]: r for r in json.loads((ws / "quality.json").read_text())["requirements"]["rows"]}
    assert rows["global"]["met"] is True and 3.5 <= rows["global"]["measured"] <= 10.5
    assert rows["limit:aspect_ratio"]["met"] in (True, False) and rows["limit:aspect_ratio"]["worst"] >= 1.0
    assert rows["limit:warpage"]["met"] is None                       # no such figure on a tetrahedron: said
    block = results(ws)
    asked = [r for r in block["check"]["rows"] if r["key"].startswith("asked_")]
    assert [r["key"] for r in asked] == ["asked_r1", "asked_r2"] and not any(r["gating"] for r in asked)
    assert block["check"]["outcome"] != "fail"
    assert any(w.startswith("Global element size 7 mm: met") for w in block["plan_words"])
    assert any(w.startswith("Warpage under 15°: not applied") for w in block["plan_words"])
    remark = [r for r in read_remarks(ws, raw=True) if r["by"] == "mesh_requirements"]
    assert remark and "you asked for" in remark[0]["text"]
    assert any(e["rule"] == "NOTE-7" for e in cited)


def test_a_solid_run_with_no_notes_asks_nothing_and_leaves_no_trace(tmp_path, monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    router = _ToolRouter()
    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    ok, marker, cited, said = _drive(ws, monkeypatch, {"purpose": "structural"}, router)
    assert ok, marker
    assert router.calls == 0 and not (ws / MR.FILE).exists()
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert "requirements" not in plan and "user_sizes" not in plan
    assert not any("From your notes" in w for w in plan["words"])
    assert "requirements" not in json.loads((ws / "quality.json").read_text())
    assert not any(str(e.get("rule")).startswith("NOTE-") for e in cited)
    assert not any("notes" in line.lower() for _k, line, _o in said)
