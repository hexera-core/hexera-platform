# Responsibility: Verify the design receipt every route publishes for the run box's "Designing the mesh" row: the planned size, the design decisions the run cited (never its plumbing), what was done with the user's notes, in plain words, once per distinct content - and the CFD plan's base and wall cells.
from __future__ import annotations

import asyncio
import uuid

from meshpipeline.contracts import decision_record as dr
from meshpipeline.contracts import mesh_design as MD
from meshpipeline.contracts import narration as N
from meshpipeline.contracts.event_stream import EventStreamError
from meshpipeline.contracts.user_notes import NOTES_FILE, record_remark
from meshpipeline.events import receipt


class _Rec:
    def __init__(self, fail: Exception | None = None):
        self.sent: list[tuple] = []
        self.fail = fail

    async def areceipt(self, title, rows, op_id=""):
        if self.fail:
            raise self.fail
        self.sent.append((title, rows, op_id))


def _job() -> str:
    return f"design-{uuid.uuid4()}"


def test_the_design_is_the_runs_design_decisions_in_plain_words_never_its_plumbing():
    job = _job()
    dr.cite(job, stage="fea_plan", decided="base size", value=0.0244, rule="PLAN-fea-14",
            words="Base size 24.4 mm: a tenth of the part, the rules refine below it.")
    dr.cite(job, stage="fea_plan", decided="walls", value=563, rule="PLAN-fea-3",
            words="563 walls zone(s), the finest 0.396 mm.")
    dr.cite(job, stage="fea_machine", decided="machine", value="vm", rule="RUN-3", words="a 32 GB machine")
    dr.cite(job, stage="fea_time", decided="watchdog", value=600, rule="PLAN-fea-13", words="Pass 1: ten minutes")
    # a decision whose words name a mesher or a rule never reaches the page
    dr.cite(job, stage="fea_plan", decided="engine_internal", value=1, rule="PLAN-fea-1",
            words="gmsh HXT runs with PLAN-fea-1")
    rows = MD.design_rows(job, None, planned=(1675539, "elements"))
    assert [r["why"] for r in rows] == ["About 1.7M elements planned.",
                                        "Base size 24.4 mm: a tenth of the part, the rules refine below it.",
                                        "563 wall areas, the finest 0.396 mm."]
    assert [r["label"] for r in rows] == ["Size", "Base size", "Walls"]
    assert not any(N.leaks(r["why"]) or N.leaks(r["label"]) for r in rows)


def test_a_later_pass_replaces_the_earlier_word_on_the_same_decision():
    job = _job()
    dr.cite(job, stage="fea_plan", decided="base size", value=1, rule="PLAN-fea-14", words="Base size 30 mm.")
    dr.cite(job, stage="fea_plan", decided="base size", value=1, rule="PLAN-fea-14", words="Base size 20 mm.")
    assert [r["why"] for r in MD.design_rows(job, None)] == ["Base size 20 mm."]


def test_what_was_done_with_the_notes_is_said_and_unread_notes_are_never_claimed_as_used(tmp_path):
    record_remark(tmp_path, "Applied the requested three prism layers on both car and ground.", by="snappy_planner")
    rows = MD.design_rows(_job(), tmp_path)
    assert rows == [{"label": "Your notes", "value": "",
                     "why": "From your notes: Applied the requested three prism layers on both car and ground."}]
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    (quiet / NOTES_FILE).write_text("3 prism layers please\n")
    assert MD.design_rows(_job(), quiet)[0]["why"] == \
        "Your notes were read as guidance; no step reported a change for them."
    empty = tmp_path / "none"
    empty.mkdir()
    assert MD.design_rows(_job(), empty) == []


def test_the_receipt_survives_the_event_constructor_whole():
    job = _job()
    dr.cite(job, stage="fea_ai", decided="analysis", value="static", rule="AI-fea-1",
            words="The fillets at the base carry the stress, so they get the finest elements.")
    rows = MD.design_rows(job, None, planned=(2_100_000, "cells"))
    ev = receipt("builder", MD.TITLE, rows).wire()
    assert ev["title"] == "The mesh design" and [r["why"] for r in ev["rows"]] == [r["why"] for r in rows]


def test_it_publishes_once_per_content_and_says_nothing_when_there_is_nothing():
    job = _job()
    rec = _Rec()
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, attempt=1)) is False
    assert rec.sent == []
    dr.cite(job, stage="plan", decided="narrow spots", value=4, rule="PLAN-1",
            words="4 narrow spots are sized locally, 13 cells across.")
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, attempt=1)) is True
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, attempt=1)) is True
    assert rec.sent[0][2] == rec.sent[1][2], "the same rows must carry the same key, so a replay is one event"
    assert rec.sent[0][2].startswith("design:1:")
    # a publisher with no receipt (an older double) is skipped; any failure but a lost claim is swallowed
    assert asyncio.run(MD.apublish(object(), job_id=job, workspace=None)) is False
    assert asyncio.run(MD.apublish(_Rec(RuntimeError("down")), job_id=job, workspace=None)) is False
    try:
        asyncio.run(MD.apublish(_Rec(EventStreamError("lost")), job_id=job, workspace=None))
    except EventStreamError:
        pass
    else:
        raise AssertionError("a lost claim was swallowed")


def test_a_cfd_plan_says_its_base_and_wall_cells():
    lines = N.cfd_plan_lines(internal=False, cells=7_800_000, layers=3, base_m=0.16, wall_m=0.005)
    assert "Base cells about 160 mm across, refined down to about 5 mm at the walls." in lines
    assert "Base cells about 1.2 m across." in N.cfd_plan_lines(internal=False, base_m=1.2)
    assert not any(N.leaks(x) for x in lines)
    # without the case's own numbers the plan is said without sizes, never with guessed ones
    assert not any("Base cells" in x for x in N.cfd_plan_lines(internal=True, cells_across=3))


def test_the_attempts_catch_all_says_nothing_once_the_engine_said_the_design():
    job = _job()
    dr.cite(job, stage="fea_plan", decided="base size", value=1, rule="PLAN-fea-14", words="Base size 20 mm.")
    rec = _Rec()
    MD.begin(job)
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, planned=(900, "elements"), attempt=1))
    # the end of the attempt: a different reading of the size must not replace what the engine said
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, attempt=1, unsaid_only=True)) is False
    # a new attempt that never said its design gets it from the catch-all
    MD.begin(job)
    assert asyncio.run(MD.apublish(rec, job_id=job, workspace=None, attempt=2, unsaid_only=True)) is True
    assert len(rec.sent) == 2
