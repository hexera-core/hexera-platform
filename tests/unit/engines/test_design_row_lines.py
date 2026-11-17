# Responsibility: Verify the "Designing the mesh" row says each fact once, in plain words (Areen, 16 Nov, a real FEA
# run): the user's element count once and with its outcome, no line cut short with "[truncated]", none of the
# operator's text (a field's description, the staged file's name, the model's own reasoning), no "Not applied" line
# for a remark that asks for restraint with nothing to measure it by - while a count beside it is still read - and no
# "wall areas" line that only repeats the base size.
# Boundaries: pure over plain data and the workspace (the model's answer is written out as the model gave it); no
# model is called, no mesh is made.
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from meshpipeline.contracts import decision_record as dr
from meshpipeline.contracts import mesh_design as MD
from meshpipeline.contracts.user_notes import read_remarks, record_remark
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.events import MAX_RECEIPT_WHY, receipt

NOTES = "Aim for 800k, try not to overdo it with the number of cells."
COUNT_QUOTE = "Aim for 800k"
VAGUE = "try not to overdo it with the number of cells"


def _answer() -> dict:
    """The reading as the model gave it on the run: the count read, the rest listed as about the mesh."""
    return {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "local": [], "layers": [],
            "physics": [], "counts": [{"what": "target", "value": 800_000, "quote": COUNT_QUOTE}],
            "other": [{"quote": VAGUE, "kind": "mesh"}]}


def _section(route: str = "") -> dict:
    req, dropped, not_applied = MR.check(json.dumps(_answer()), NOTES, route=route)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied)


# ----------------------------------------------------- NOTE-8: a call for restraint asks nothing -------------
@pytest.mark.parametrize("said", [VAGUE, "don't go crazy with the element count", "keep the number of cells reasonable",
                                  "not too many elements please", "Try not to overdo it."])
def test_a_call_for_restraint_with_nothing_to_measure_it_by_is_no_requirement_left_out(said):
    assert MR.asks_nothing(said)


@pytest.mark.parametrize("said", [NOTES, "keep the cell count under 2 million", "fewer cells near the outlet",
                                  "more elements please", "keep the cell count low", "refine the fillets",
                                  "not too many triangles", "keep the number of cells reasonable on the bracket"])
def test_a_number_a_direction_a_mesh_term_or_a_place_still_asks_something(said):
    assert not MR.asks_nothing(said)


@pytest.mark.parametrize("route", ["", MR.FLOW])
def test_the_count_beside_the_remark_is_read_and_the_remark_is_not_told(route):
    sec = _section(route)
    assert sec["not_applied"] == [] and not any(w.startswith("Not applied") for w in MR.words(sec))
    if route == MR.FLOW:
        assert [r["field"] for r in sec["requirements"]["rows"]] == ["cells:target"]


def test_the_floor_never_tells_a_left_over_call_for_restraint():
    # the model listed nothing: the rules' own floor finds the clause, and it asks nothing
    answer = {**_answer(), "other": []}
    notes = "Aim for 800k. Try not to overdo it with the number of elements."
    _req, _dropped, not_applied = MR.check(json.dumps(answer), notes, route="")
    assert not_applied == []


# ----------------------------------------------------- the count said once ---------------------------------
def test_a_route_that_tells_the_count_itself_is_not_told_it_twice_before_meshing():
    sec = _section(MR.FLOW)
    assert MR.live_words(sec) == ["From your notes: about 800,000 cells."]
    assert MR.live_words(sec, told_elsewhere=("cells:",)) == []


# ----------------------------------------------------- the design receipt ----------------------------------
def test_the_notes_reading_is_said_by_the_notes_lines_never_again_by_the_why_record(tmp_path):
    job = f"design-{uuid.uuid4()}"
    sec = MR.section("used", requirements=_section(MR.FLOW)["requirements"], not_applied=["washers round holes"])
    for e in MR.decisions(sec, stage="fea_ai"):
        dr.cite(job, **{k: e[k] for k in ("stage", "decided", "value", "rule", "inputs", "words")})
    dr.cite(job, stage="fea_plan", decided="element count", value=0.5, rule="PLAN-fea-27",
            words="You asked for about 800k elements; planned about 1.51M.")
    dr.cite(job, stage="fea_plan", decided="base size", value=0.279, rule="PLAN-fea-14",
            words="Base size 279 mm: a tenth of the part, the rules refine below it.")
    record_remark(tmp_path, "you asked for about 800k elements; planned about 1.51M - not reached: that is the "
                            "lowest this part can go with every element still valid.", by="fea_target")
    whys = [r["why"] for r in MD.design_rows(job, tmp_path)]
    assert whys == ["Base size 279 mm: a tenth of the part, the rules refine below it.",
                    "From your notes: you asked for about 800k elements; planned about 1.51M - not reached: that is "
                    "the lowest this part can go with every element still valid."]
    assert not any("told to the user" in w for w in whys)


def test_a_line_too_long_for_the_receipt_is_left_out_never_cut():
    long_why = "From your notes: " + "the geometry remains intact and its faces stay named " * 4
    ev = receipt("builder", "The mesh design", [{"label": "Your notes", "value": "", "why": long_why},
                                                 {"label": "Size", "value": "", "why": "About 1.5M elements planned."}])
    rows = ev.data["rows"]
    assert len(long_why) > MAX_RECEIPT_WHY
    assert [r["why"] for r in rows] == ["About 1.5M elements planned."]
    assert "truncated" not in json.dumps(ev.data)


def test_the_staged_files_name_is_never_a_design_line():
    from meshpipeline.engines import fea_driver as FD

    assert FD.staged_words("geometry.brep", healed=False) == ""
    assert FD.staged_words("geometry.brep", healed=True) == "The part was healed before it was measured."


def test_the_count_line_replaces_the_models_own_remark_on_the_notes(tmp_path):
    from meshpipeline.engines import fea_driver as FD

    record_remark(tmp_path, "Used the requested target of about 800k elements; the geometry remains intact.",
                  by="fea_ai")
    FD.tell_count(tmp_path, "you asked for about 800k elements; planned about 1.51M.")
    assert read_remarks(tmp_path) == ["you asked for about 800k elements; planned about 1.51M."]
    # no count line: the model's remark is the only word on the notes, and it stays
    other = tmp_path / "other"
    other.mkdir()
    record_remark(other, "Refined the fillets as asked.", by="fea_ai")
    FD.tell_count(other, "")
    assert read_remarks(other) == ["Refined the fillets as asked."]


# ----------------------------------------------------- the walls line ---------------------------------------
def _plan(wall_sizes: list[float], base: float = 0.279) -> dict:
    zones = [{"id": f"W{i}", "kind": "wall", "size_m": s} for i, s in enumerate(wall_sizes)]
    zones.append({"id": "H1", "kind": "hole", "size_m": 0.00026})
    return {"zones": zones, "base_size_m": base, "growth": 1.3, "estimate": {"elements": 1_510_000},
            "limit": {"elements": 4_000_000}}


def test_walls_meshed_at_the_base_size_are_not_said_as_a_finest_size():
    words = {d["decided"]: d["words"] for d in P.decisions({}, _plan([0.279] * 475))}
    assert words["walls"] == ""                       # every wall at the base size: nothing finer to say
    assert words["hot spots"] == "1 hot spots zone(s), the finest 0.26 mm."


def test_walls_finer_than_the_base_are_counted_and_their_finest_said():
    words = {d["decided"]: d["words"] for d in P.decisions({}, _plan([0.279, 0.012, 0.004]))}
    assert words["walls"] == "2 walls zone(s), the finest 4 mm."


def test_the_record_still_counts_every_wall_zone():
    d = {d["decided"]: d for d in P.decisions({}, _plan([0.279] * 3))}["walls"]
    assert d["value"] == 3 and d["inputs"]["finest_m"] == 0.279


def test_a_vague_note_reaches_no_fea_live_line(tmp_path, monkeypatch):
    # the FEA route says the notes before meshing: the restraint is not told, and the count it tells itself is left out
    from meshpipeline.engines import fea_driver as FD

    said: list[str] = []

    async def _say(publish, line, op_id=""):
        said.append(line)
        return True

    async def _decide(state, **kw):
        return _section(MR.FLOW)

    from meshpipeline.contracts import narration as N
    from meshpipeline.engines import meshing_standard as MS
    monkeypatch.setattr(N, "asay", _say)
    monkeypatch.setattr(MS, "decide", _decide)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    asyncio.run(FD.read_requirements(tmp_path, {"user_notes": [{"text": NOTES}]}, job_id="j", publish=object(),
                                     told=("cells:",)))
    assert said == []
