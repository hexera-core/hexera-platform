# Responsibility: Pin the routes a sound mesh takes (docs/RULES.md LAD-cfd-1..4): a resolution note climbs the ladder while a rung is left, a switch leaves the new engine one rung, a cell-limit note is never retried, and a run whose last attempt failed delivers its newest sound mesh instead of the failure.
from __future__ import annotations

import json

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.engines import resolution_policy as rp
from meshpipeline.pipeline.graph import node_deliver_sound, route_after_executor

NOTE = {"kind": rp.RESOLUTION_SHORT, "cells_across": 8.0, "floor": 12.0, "cells": 900_000,
        "cell_limit": 8_000_000, "facts": {"cells_across": 8.0, "needed": 12.0, "cells": 900_000}}
SPENT = bcfg.MAX_BUILDER_RETRIES + 1


def st(**over):
    base = {"executor_success": True, "retry_count": 1, "solvability_failed": False,
            "requirement_caveats": [], "job_id": "j", "engine": "snappy"}
    base.update(over)
    return base


def test_a_resolution_note_with_a_retry_left_climbs_a_rung():
    assert route_after_executor(st(requirement_caveats=[NOTE])) == "node_classifier"


def test_a_resolution_note_with_the_ladder_spent_is_delivered_with_the_note():
    assert route_after_executor(st(requirement_caveats=[NOTE], retry_count=SPENT)) == \
        "node_reviewer"
    assert route_after_executor(st(requirement_caveats=[NOTE],
                                   resolution_ladder={"rung": 2})) == "node_reviewer"


def test_a_switch_leaves_the_new_engine_one_rung():
    switched = st(requirement_caveats=[NOTE], retry_count=SPENT,
                  engine_ladder={"switches": [{"from": "cfmesh", "to": "snappy"}]},
                  resolution_ladder={"rung": 0, "engine": "snappy", "switched": True})
    assert route_after_executor(switched) == "node_classifier"
    used = {**switched, "resolution_ladder": {"rung": 1, "switched": True, "after_switch": True},
            "retry_count": SPENT + 1}
    assert route_after_executor(used) == "node_reviewer"


def test_a_cell_limit_note_is_delivered_never_retried():
    over = {"kind": rp.OVER_CELL_LIMIT, "cells": 16_000_000, "limit": 8_000_000}
    assert route_after_executor(st(requirement_caveats=[over], retry_count=0)) == "node_reviewer"


def _sound_ws(tmp_path, name="attempt_1"):
    ws = tmp_path / name
    ws.mkdir()
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"mesh_mode": "snappy", "cell_count": 1000, "quality": {"fatal": []}}))
    return str(ws)


def test_a_failed_last_attempt_delivers_the_newest_sound_mesh(tmp_path):
    ws = _sound_ws(tmp_path)
    failed = st(executor_success=False, retry_count=SPENT, executor_failed_gate="finalize",
                executor_failure_cause="engine_crashed",
                sound_attempts=[{"attempt": 1, "workspace": ws, "engine": "snappy",
                                 "requirement_caveats": [NOTE]}])
    assert route_after_executor(failed) == "node_deliver_sound"
    # and with no sound mesh, the run ends exactly as before
    assert route_after_executor({**failed, "sound_attempts": []}) == "__end__"


async def test_the_delivered_sound_mesh_carries_its_own_engine_and_notes(tmp_path):
    ws = _sound_ws(tmp_path)
    failed = st(executor_success=False, retry_count=SPENT, engine="cfmesh",
                executor_failed_gate="manifest_valid", executor_failure_cause="mesh_quality",
                openfoam_workspace=str(tmp_path / "attempt_2"),
                sound_attempts=[{"attempt": 1, "workspace": ws, "engine": "snappy",
                                 "engine_params": {"x": 1}, "requirement_caveats": [NOTE]}])
    out = await node_deliver_sound(failed)
    assert out["executor_success"] is True and out["executor_failed_gate"] == ""
    assert out["openfoam_workspace"] == ws and out["engine"] == "snappy"
    assert out["engine_params"] == {"x": 1}
    assert out["requirement_caveats"][0]["kind"] == rp.RESOLUTION_SHORT
    assert out["engine_ladder"]["delivered_from"]["rule"] == rp.LAD_DELIVER_SOUND


def test_a_sound_mesh_never_moves_the_run_to_another_engine():
    from meshpipeline.pipeline import engine_fallback as lad
    failure = lad.classify(st(requirement_caveats=[NOTE]))
    assert failure is not None and failure.kind == lad.NEVER
    assert failure.cause == "resolution_short"
