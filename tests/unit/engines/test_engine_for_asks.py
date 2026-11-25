"""ENG-10: what the user typed steers the engine, by what each engine declares it can set - among the valid engines
only, never one the user named, and nothing typed changes nothing."""
from __future__ import annotations

import asyncio
import json

from tests.unit.engines.test_cfd_requirements import _read

from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

LADDER = ["vmtk", "cfmesh", "snappy", "netgen"]
YP1 = {"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 on the walls please"}


def _elbow():
    """The dev run's three asks: 0.5 mm cells on a picked wall face, y+ 1, finer around the bend."""
    notes = "can you make the cells 0.5 mm on this wall?\ny+ of 1 on the walls please\nfiner around the bend"
    req, _d, _na = _read(notes, layers=[YP1], local=[
        {"where": "wall", "what": "size", "value": 0.5, "unit": "mm", "quote": "cells 0.5 mm on this wall"},
        {"where": "the bend", "what": "finer", "value": 0, "unit": "none", "quote": "finer around the bend"}])
    return MR.section("used", requirements=req)


def test_the_elbow_moves_off_the_vessel_mesher_to_the_engine_that_sets_all_three():
    got = CR.steer(_elbow(), "vmtk", LADDER)
    assert got["engine"] == "snappy" and got["missing"] == []
    # typed numbers count twice a vague "finer"
    assert got["scores"]["vmtk"] == 0 and got["scores"]["snappy"] == 5 and got["scores"]["cfmesh"] == 5
    assert sorted(a["weight"] for a in got["asks"]) == [1, 2, 2]
    w = got["words"]
    assert w.startswith("Meshed with ") and "has no setting for" in w and "2 other things" in w


def test_no_ask_and_an_engine_that_sets_it_all_stay_as_they_are():
    assert CR.steer(None, "vmtk", LADDER)["engine"] == "vmtk"
    assert CR.steer(MR.section("none"), "vmtk", LADDER) == {
        "engine": "vmtk", "before": "vmtk", "order": [], "scores": {}, "asks": [], "missing": [], "words": ""}
    req, _d, _na = _read("5 layers, growth 1.2", layers=[
        {"what": "count", "value": 5, "unit": "none", "quote": "5 layers"},
        {"what": "growth", "value": 1.2, "unit": "none", "quote": "growth 1.2"}])
    # VMTK sets a layer count and a layer growth itself: it stays
    got = CR.steer(MR.section("used", requirements=req), "vmtk", LADDER)
    assert got["engine"] == "vmtk" and got["words"] == ""
    # cfMesh with y+ 1: it has the setting, so nothing moves even though snappy also has it
    sec = MR.section("used", requirements=_read("y+ of 1 on the walls please", layers=[YP1])[0])
    assert CR.steer(sec, "cfmesh", LADDER)["engine"] == "cfmesh" and CR.steer(sec, "vmtk", LADDER)["engine"] == "snappy"


def test_when_no_engine_sets_everything_the_one_that_sets_the_most_and_says_what_it_cannot():
    req, _d, _na = _read("y+ of 1 on the walls please, cells grow by 1.1", layers=[YP1],
                         sizes=[{"what": "growth", "value": 1.1, "unit": "none", "quote": "cells grow by 1.1"}])
    got = CR.steer(MR.section("used", requirements=req), "vmtk", ["vmtk", "cfmesh"])
    assert got["engine"] == "cfmesh" and len(got["missing"]) == 1
    assert "either" in got["words"]
    # no valid engine sets more than the one picked: it stays (its rows say not applied, as before)
    assert CR.steer(MR.section("used", requirements=req), "vmtk", ["vmtk", "gmsh"])["engine"] == "vmtk"


def test_the_declaration_matches_what_each_applier_tells_as_not_applied():
    # the fields every engine's applier says "not applied" for are the declared ones
    assert CR._cannot("vmtk", "local") and CR._cannot("netgen", "local") and CR._cannot("gmsh", "local")
    assert not CR._cannot("snappy", "local") and not CR._cannot("cfmesh", "local")
    assert CR._cannot("cfmesh", "growth") and CR._cannot("vmtk", "growth") and not CR._cannot("snappy", "growth")
    assert CR._cannot("gmsh", "layers:count") and CR._cannot("snapgrid", "layers:growth")
    assert not CR._cannot("vmtk", "layers:count") and CR._cannot("vmtk", "layers:y_plus")
    assert CR._cannot("cfmesh", "layers:total_thickness") and not CR._cannot("netgen", "layers:total_thickness")


def test_the_record_replays_and_a_named_engine_stays():
    order, scores = ["vmtk", "snappy", "cfmesh"], {"vmtk": 0, "snappy": 5, "cfmesh": 5}
    assert dr.engine_for_asks({"engine_source": "suggested_confirmed", "order": order, "scores": scores}) == \
        ("snappy", "ENG-10")
    assert dr.engine_for_asks({"engine_source": "user_direct", "order": order, "scores": scores}) == ("vmtk", "ENG-6")
    # a tie with the engine as picked keeps it
    assert dr.engine_for_asks({"order": order, "scores": {"vmtk": 5, "snappy": 5}})[0] == "vmtk"
    assert dr.REPLAYERS[dr.STAGE_ENGINE_FOR_ASKS] is dr.engine_for_asks
    assert CR.RULE_ASKS_ENGINE == dr.RULE_ENGINE_FOR_ASKS == "ENG-10"


def _state(source: str) -> dict:
    return {"job_id": "j-eng10", "purpose": "internal_cfd", "engine": "vmtk", "engine_source": source,
            "user_notes": [{"text": "y+ of 1 on the walls please", "source": "console"}],
            "engine_ladder": {"order": ["vmtk", "cfmesh", "snappy"], "order_turf": {"netgen": "weak"}}}


def _patch(monkeypatch, sec, able):
    from meshpipeline.engines import meshing_standard as MS
    from meshpipeline.pipeline import engine_fallback as lad
    from meshpipeline.pipeline import engine_select as ES

    async def _decide(*_a, **_k):
        return sec

    async def _pub(*_a, **_k):
        return None
    monkeypatch.setattr(MS, "decide", _decide)
    monkeypatch.setattr(lad, "able_engines", lambda *_a, **_k: list(able))
    monkeypatch.setattr(ES, "_publish", _pub)
    return ES


def test_the_run_moves_a_system_pick_cites_it_and_keeps_the_line_for_the_result(monkeypatch, tmp_path):
    sec = MR.section("used", requirements=_read("y+ of 1 on the walls please", layers=[YP1])[0])
    ES = _patch(monkeypatch, sec, ["vmtk", "netgen", "cfmesh", "snappy"])
    up = asyncio.run(ES.engine_for_asks(_state("suggested_confirmed"), "vmtk"))
    assert up["engine"] == "snappy" and up["engine_ladder"]["asks"]["from"] == "vmtk"
    assert up["engine_ladder"]["order"] == ["vmtk", "cfmesh", "snappy"]           # the record it had is kept
    entry = [e for e in dr.cited_for("j-eng10") if e["stage"] == dr.STAGE_ENGINE_FOR_ASKS][-1]
    assert entry["rule"] == "ENG-10" and entry["value"] == "snappy" and dr.replay([entry]) == []
    assert "netgen" in entry["inputs"]["held_back"]                                # its turf marks the case weak
    # the attempt on that engine keeps the line with its result
    CR._say_engine_for_asks(tmp_path, {**_state("suggested_confirmed"), **up}, "snappy")
    from meshpipeline.contracts.user_notes import REMARKS_FILE, read_remarks
    rows = json.loads((tmp_path / REMARKS_FILE).read_text())
    assert rows[-1]["by"] == "engine_for_asks" and read_remarks(tmp_path)[-1].startswith("Meshed with ")
    # on another engine (the ladder moved on) nothing is kept for it
    other = tmp_path / "other"
    other.mkdir()
    CR._say_engine_for_asks(other, {**_state("suggested_confirmed"), **up}, "cfmesh")
    assert read_remarks(other) == []


def test_the_why_record_names_the_engine_as_picked_before_the_asks_moved_it():
    from meshpipeline.application import why_record as W
    st = {"engine": "snappy", "engine_source": "suggested_confirmed", "purpose": "internal_cfd",
          "engine_ladder": {"order": ["vmtk", "cfmesh", "snappy"], "order_recommended": "vmtk", "order_rule": "ENG-2",
                            "order_source": "measured", "asks": {"from": "vmtk", "to": "snappy", "rule": "ENG-10"},
                            "attempts": [{"engine": "snappy"}]}}
    e = W.engine_pick_entry(st)
    assert e is not None and e.value == "vmtk" and e.rule == "ENG-2"
    assert dr.replay([e.as_dict()]) == []


def test_a_named_engine_a_dispute_and_no_notes_are_never_moved(monkeypatch):
    sec = MR.section("used", requirements=_read("y+ of 1 on the walls please", layers=[YP1])[0])
    ES = _patch(monkeypatch, sec, ["vmtk", "cfmesh", "snappy"])
    assert asyncio.run(ES.engine_for_asks(_state("user_direct"), "vmtk")) == {}
    assert asyncio.run(ES.engine_for_asks({**_state("suggested_confirmed"), "user_dispute": {"x": 1}}, "vmtk")) == {}
    assert asyncio.run(ES.engine_for_asks({**_state("system"), "user_notes": []}, "vmtk")) == {}
    # FEA is not a flow run
    assert asyncio.run(ES.engine_for_asks({**_state("system"), "purpose": "structural_fea"}, "vmtk")) == {}


def test_an_engine_whose_turf_marks_the_case_weak_or_outside_is_never_moved_to(monkeypatch):
    sec = MR.section("used", requirements=_read("y+ of 1 on the walls please", layers=[YP1])[0])
    ES = _patch(monkeypatch, sec, ["vmtk", "snappy", "cfmesh"])
    st = {**_state("system"), "engine_ladder": {"order_turf": {"snappy": "outside", "cfmesh": "weak"}}}
    assert asyncio.run(ES.engine_for_asks(st, "vmtk")) == {}
    st = {**_state("system"), "engine_ladder": {"order_turf": {"snappy": "outside"}}}
    assert asyncio.run(ES.engine_for_asks(st, "vmtk"))["engine"] == "cfmesh"
