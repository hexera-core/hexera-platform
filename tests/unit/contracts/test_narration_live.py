# Responsibility: Verify the live narration's numbers and decisions: counts read from the mesher's own log, a moving count said again with the plan's estimate, the CFD plan and the review said in plain words, and counted passes.
from __future__ import annotations

import asyncio

from meshpipeline.contracts import mesh_progress
from meshpipeline.contracts import narration as N


class _Rec:
    def __init__(self):
        self.said: list[dict] = []
        self.stages: list[tuple] = []

    async def anarrate(self, kind, text, *, n=None, of=None, unit="", op_id=""):
        self.said.append({"kind": kind, "text": text, "n": n, "of": of, "unit": unit, "op_id": op_id})

    async def amesh_stage(self, key, label, op_id=""):
        self.stages.append((key, label, op_id))

    async def arationale(self, conclusion, because=""):
        pass


def test_the_mesher_log_gives_a_live_count_and_only_a_real_one(tmp_path):
    from meshpipeline.engines.mesher_stage import read_stage
    (tmp_path / "snappyHexMesh_1.log").write_text(
        "Refinement phase\nMesh : cells:120000 faces:300000\nMesh : cells:845000 faces:2000000\n")
    got = read_stage(tmp_path)
    assert got["key"] == "castellating" and got["n"] == 845000
    (tmp_path / "snappyHexMesh_1.log").write_text("Refinement phase\nnothing counted yet\n")
    assert "n" not in read_stage(tmp_path)


def test_a_count_that_moves_on_the_same_stage_is_said_again_with_the_plans_estimate():
    rec = _Rec()

    async def go():
        sink = mesh_progress.publishing_sink(rec, asyncio.get_running_loop(), op_prefix="p", of=1900000,
                                             unit="elements")

        def remote():
            with mesh_progress.relaying(sink):
                mesh_progress.report({"key": "volume", "label": "filling the solid with elements"})
                mesh_progress.report({"key": "volume", "label": "x", "n": 400000})
                mesh_progress.report({"key": "volume", "label": "x", "n": 400000})        # unchanged: dropped
                mesh_progress.report({"key": "volume", "label": "x", "n": 1230000})
        await asyncio.to_thread(remote)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert [k for k, _, _ in rec.stages] == ["volume"]                     # one stage, said once
    assert [(s["n"], s["of"], s["unit"]) for s in rec.said] == [
        (None, None, ""), (400000, 1900000, "elements"), (1230000, 1900000, "elements")]
    assert len({s["op_id"] for s in rec.said}) == 3


def test_a_cfd_plan_says_what_it_decided_in_plain_words():
    ext = N.cfd_plan_lines(internal=False, cells=2100000, layers=5, wake=True, boxes=2)
    assert ext[0].startswith("The flow goes around your part")
    assert "5 thin wall layers follow every wall, where the flow changes fastest." in ext
    assert "The wake behind the part gets finer cells." in ext
    assert ext[-1] == "I expect about 2.1M cells."
    inner = N.cfd_plan_lines(internal=True, cells_across=3, layers=0)
    assert inner == ["The fluid fills the inside of your part, so I'm meshing the space within it.",
                     "At least 3 cells go across the narrowest passage, so the flow there is resolved."]
    assert not any(N.leaks(line) for line in ext + inner)


def test_the_review_line_names_the_criteria_never_the_reviewers_prose():
    assert N.review_line(passed=True) == "The review checked the mesh against your brief, and it passes."
    line = N.review_line(passed=False, failed_axes=("boundary_layers", "wall_resolution"),
                         rebuild_axes=("boundary_layers",))
    assert line == "The review asked for one more pass, to fix: boundary layers."
    many = N.review_line(passed=False, failed_axes=("a_b", "c_d", "e_f", "g_h", "i_j"))
    assert many.endswith("and 1 more.") and not N.leaks(many)
    assert N.pass_words(2, 6) == "Pass 2 of up to 6"


def test_the_review_verdict_is_said_on_the_live_line():
    from meshpipeline.contracts import rationale
    rec = _Rec()
    asyncio.run(rationale.areviewer_verdict(rec, passed=True))
    assert rec.said and rec.said[0]["kind"] == "say" and rec.said[0]["text"].endswith("it passes.")
