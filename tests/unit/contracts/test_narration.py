# Responsibility: Verify the live narration says plain words only: every mesher stage has user words, the leak guard drops what must never reach the page, and the relay and the FEA pass publish what they should.
from __future__ import annotations

import asyncio
import re

import pytest

from meshpipeline import events as E
from meshpipeline.contracts import mesh_progress
from meshpipeline.contracts import narration as N
from meshpipeline.contracts.event_stream import EventStreamError


class _Rec:
    """A publisher double that records what it was asked to narrate."""

    def __init__(self):
        self.said: list[dict] = []
        self.stages: list[tuple] = []
        self.meshing: list[tuple] = []

    async def anarrate(self, kind, text, *, n=None, of=None, unit="", op_id=""):
        self.said.append({"kind": kind, "text": text, "n": n, "of": of, "unit": unit, "op_id": op_id})

    async def amesh_stage(self, key, label, op_id=""):
        self.stages.append((key, label, op_id))

    async def ameshing(self, engine, budget_s, history=None, op_id=""):
        self.meshing.append((engine, budget_s, op_id))


LEAKY = [
    "Wrote /tmp/ws/generation_1/attempt_2/plan.json",
    "see plan.json for details",
    "Rule RUN-4 moved it",
    "PLAN-fea-13 says so",
    "[FEA_PLAN] the part could not be measured",
    "the zone_floor_m is 0.2",
    "snappyHexMesh is castellating",
    "gmsh switched to HXT",
    "Netgen gave up",
    "the model said so",
    "gpt-5.6 picked it",
    "DeepSeek answered",
    "from the prompt",
    "size={0.2}",
    "Face_31 is thin",
]

PLAIN = [
    "Filling the volume",
    "Meshing on a large machine (256 GB)",
    "The 26 fillets and 3 holes carry the stress, so they get the finest cells.",
    "Two parts sit 0.2 mm apart, which reads as a weld. Joining them.",
    "Quality looks good: worst element 0.25, every wall at least 2 cells thick.",
    "Still filling the volume · 34 min",
]


@pytest.mark.parametrize("text", LEAKY)
def test_the_guard_drops_what_must_never_reach_the_page(text):
    assert N.leaks(text)
    assert N.plain(text) is None


@pytest.mark.parametrize("text", PLAIN)
def test_the_guard_lets_plain_sentences_through(text):
    assert N.plain(text) == text


def test_a_line_too_long_is_dropped_not_cut():
    assert N.plain("word " * 100) is None
    assert N.plain("  two   spaces  ") == "two spaces"
    assert N.plain("") is None


def _known_stage_keys() -> set[str]:
    """Every stage key a mesher reports today: the log readers, the gmsh FEA stage file, the runner."""
    from meshpipeline.engines import mesher_stage as MS
    from meshpipeline.runtime import mesh_runner
    keys = {k for k, _ in MS._TOOLS.values()}
    keys |= {k for _, k, _ in MS._SNAPPY} | {k for _, k, _ in MS._CFMESH}
    from pathlib import Path
    src = Path(__file__).parents[3] / "src" / "meshpipeline" / "engines" / "gmsh" / "fea_adapter.py"
    keys |= set(re.findall(r'note_stage\(ws, "([a-z_]+)"', src.read_text()))
    keys.add(mesh_runner.RETURNING["key"])
    return keys


def test_every_stage_a_mesher_reports_has_its_own_plain_words():
    keys = _known_stage_keys()
    assert len(keys) > 10, keys
    missing = sorted(k for k in keys if k not in N.STAGES)
    assert not missing, f"stages with no user words (they would read as plain meshing): {missing}"
    for key, (kind, words) in N.STAGES.items():
        assert kind in N.KINDS and kind != "say", key
        assert N.plain(words) == words, f"{key}: {words!r} fails the guard"


def test_an_unknown_stage_still_narrates_and_never_as_its_label():
    assert N.stage_words("brand_new_phase") == N.UNKNOWN_STAGE


def test_the_machine_is_told_by_its_size_never_its_name():
    assert N.machine_words({"where": "gce", "machine": "n2-highmem-32", "memory_gib": 256}) == \
        "Meshing on a large machine (256 GB)"
    assert N.machine_words({"where": "cloud_run", "memory_gib": 32}) == "Meshing on a mesh machine (32 GB)"
    assert N.machine_words(None) == "Meshing on a mesh machine"
    assert "n2" not in N.machine_words({"where": "gce", "machine": "n2-highmem-32", "memory_gib": 256})


def test_the_heartbeat_reads_as_still_working():
    assert N.still_words("Filling the volume", 34 * 60) == "Still filling the volume · 34 min"
    assert N.still_words("Filling the volume", 5) == "Still filling the volume · 1 min"


def test_plan_lines_are_the_records_own_words_only_when_plain():
    decisions = [
        {"words": "Version 1 meshes the solid with 10-node tetrahedra."},
        {"words": "Base size 12.8 mm: a tenth of the part, the rules refine below it."},
        {"words": "gmsh's HXT fills it."},                       # dropped: a mesher internal
        {"words": "26 fillet zone(s), the finest 0.4 mm."},        # softened
        {"words": "4 more"},
    ]
    lines = N.plan_lines(decisions)
    assert lines == ["Meshing the solid with 10-node tetrahedra.",
                     "Base size 12.8 mm: a tenth of the part, the rules refine below it.",
                     "26 fillet areas, the finest 0.4 mm."]
    assert N.plan_lines(None) == []


def test_a_retry_says_why_and_never_a_leaky_reason():
    assert N.retry_line(2, {"words": "a degenerate element, so the next pass grows sizes more gently."}) == \
        "Pass 1 did not pass every check. A degenerate element, so the next pass grows sizes more gently."
    line = N.retry_line(3, {"words": "the next pass uses gmsh's other 3D algorithm (HXT)."})
    assert line.startswith("Pass 2 did not pass every check") and not N.leaks(line)


def test_astep_publishes_only_plain_lines_of_a_known_kind():
    rec = _Rec()

    async def go():
        assert await N.astep(rec, "fill", "Filling the volume", n=1200000, of=1900000, unit="cells")
        assert not await N.astep(rec, "fill", "snappyHexMesh is filling")
        assert not await N.astep(rec, "dance", "Filling the volume")
        assert await N.asay(rec, "Joining them.")
        assert not await N.asay(object(), "Joining them.")       # a double that cannot narrate
    asyncio.run(go())
    assert [s["kind"] for s in rec.said] == ["fill", "say"]
    assert rec.said[0]["n"] == 1200000 and rec.said[0]["of"] == 1900000


def test_a_lost_claim_is_raised_and_any_other_failure_is_swallowed():
    class Lost:
        async def anarrate(self, *a, **k):
            raise EventStreamError("gone")

    class Broken:
        async def anarrate(self, *a, **k):
            raise RuntimeError("redis hiccup")

    with pytest.raises(EventStreamError):
        asyncio.run(N.asay(Lost(), "Joining them."))
    assert asyncio.run(N.asay(Broken(), "Joining them.")) is False


def test_the_event_carries_a_known_kind_only():
    ev = E.narrate("builder", "fill", "Filling the volume", 10, 20, "cells")
    assert ev.type == E.NARRATE and E.NARRATE in E.EVENT_TYPES
    assert ev.data == {"kind": "fill", "text": "Filling the volume", "n": 10, "of": 20, "unit": "cells"}
    with pytest.raises(ValueError):
        E.narrate("builder", "dance", "x")


def test_the_relay_says_each_new_stage_in_plain_words_and_beats_while_quiet():
    rec = _Rec()

    async def go():
        loop = asyncio.get_running_loop()
        sink = mesh_progress.publishing_sink(rec, loop, op_prefix="mesh_stage:g1/a1:1")

        def remote():
            with mesh_progress.relaying(sink):
                mesh_progress.report({"key": "castellating", "label": "refining and cutting the cells (castellating)"})
                mesh_progress.report({"key": "castellating", "label": "again"})      # a repeat: dropped
                mesh_progress.beat()
                mesh_progress.report({"key": "layers", "label": "adding wall layers", "n": 3, "of": 5})
        await asyncio.to_thread(remote)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert [k for k, _, _ in rec.stages] == ["castellating", "layers"]
    texts = [s["text"] for s in rec.said]
    assert texts[0] == "Refining the cells around your part"
    assert texts[1].startswith("Still refining the cells around your part")
    assert texts[2] == "Building boundary layers" and rec.said[2]["n"] == 3 and rec.said[2]["of"] == 5
    assert not any(N.leaks(t) for t in texts)
    assert len({s["op_id"] for s in rec.said}) == len(rec.said)


def test_a_beat_before_any_stage_says_nothing():
    rec = _Rec()

    async def go():
        sink = mesh_progress.publishing_sink(rec, asyncio.get_running_loop(), op_prefix="p")
        with mesh_progress.relaying(sink):
            mesh_progress.beat()
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert rec.said == []


def test_an_fea_pass_says_its_plan_its_machine_and_starts_the_mesh():
    from meshpipeline.engines import fea_driver as FD
    rec = _Rec()
    plan = {"decisions": [{"words": "Base size 12.8 mm: a tenth of the part, the rules refine below it.",
                           "rule": "PLAN-fea-3"}]}
    machine = {"where": "gce", "machine": "n2-highmem-32", "memory_gib": 256}
    asyncio.run(FD._narrate_pass(rec, n=1, first=True, plan=plan, levers=[], machine=machine, budget_s=7200))
    assert [s["kind"] for s in rec.said] == ["say", "machine"]
    assert rec.said[1]["text"] == "Meshing on a large machine (256 GB)"
    assert rec.meshing == [(FD.ENGINE, 7200, "meshing:fea:1")]
    rec2 = _Rec()
    asyncio.run(FD._narrate_pass(rec2, n=2, first=False, plan=plan, levers=[{"words": "the sizes grow more gently."}],
                                 machine={"where": "cloud_run", "memory_gib": 32}, budget_s=3600))
    assert rec2.said[0]["text"].startswith("Pass 1 did not pass every check.")
    assert not any(N.leaks(s["text"]) for s in rec.said + rec2.said)
