# Responsibility: Pin the resolution ladder and the sound-mesh notes (engines/resolution_policy, docs/RULES.md PLAN-cfd / LAD-cfd): a rung always changes the case, a switch spends no rung, our own bars are notes on a sound mesh, and the note names the gap in plain words.
from __future__ import annotations

import json
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.engines import resolution_policy as rp
from meshpipeline.engines.gates import GateCtx, cause_of, facts_of


def _note(cells_across=8.0, needed=12.0, cells=1_000_000, **extra) -> dict:
    return {"kind": rp.RESOLUTION_SHORT, "cells_across": cells_across, "floor": needed,
            "cells": cells, "cell_limit": polcfg.CELL_HARD_LIMIT,
            "facts": {"cells_across": cells_across, "needed": needed, "cells": cells}, **extra}


class TestTheRung:
    def test_rung_zero_is_the_plan(self):
        assert rp.rung_factor(0) == 1.0

    def test_each_rung_takes_its_factor_or_the_measured_shortfall(self):
        # 11 measured against a floor of 12: the rung's own 1.5 is more than (12 + 1) / 11
        assert rp.rung_factor(1, measured=11.0, floor=12.0) == 1.5
        assert rp.rung_factor(2, measured=11.0, floor=12.0) == 2.0
        # 5 measured: the shortfall needs (12 + 1) / 5 = 2.6
        assert rp.rung_factor(1, measured=5.0, floor=12.0) == 2.6
        # never past the cap, however short
        assert rp.rung_factor(1, measured=1.0, floor=12.0) == rp.RUNG_CAP

    def test_a_sound_mesh_short_of_the_floor_climbs_one_rung_on_its_engine(self):
        st = {"engine": "cfmesh", "executor_success": True, "retry_count": 1,
              "requirement_caveats": [_note(cells_across=8.0)]}
        lad = rp.next_ladder(st)
        assert lad["rung"] == 1 and lad["engine"] == "cfmesh"
        assert lad["factor"] == rp.rung_factor(1, measured=8.0, floor=12.0)

    def test_a_switch_resets_the_rung_and_spends_none(self):
        st = {"engine": "cfmesh", "executor_success": False, "retry_count": 1,
              "resolution_ladder": {"rung": 1, "engine": "cfmesh", "factor": 1.5}}
        lad = rp.next_ladder(st, switch_to="snappy")
        assert lad == {"rung": 0, "engine": "snappy", "switched": True}

    def test_the_rung_is_written_beside_the_case_and_cleared_at_rung_zero(self, tmp_path):
        rp.write_fact(tmp_path, {"engine": "vmtk",
                                 "resolution_ladder": {"rung": 1, "factor": 1.5}})
        assert rp.factor_of(tmp_path) == 1.5
        assert json.loads((tmp_path / rp.LADDER_FILE).read_text())["rule"] == rp.LAD_RUNG
        rp.write_fact(tmp_path, {"engine": "vmtk", "resolution_ladder": {"rung": 0}})
        assert not (tmp_path / rp.LADDER_FILE).exists() and rp.factor_of(tmp_path) == 1.0

    def test_a_rung_changes_the_case_the_rerun_guard_hashes(self, tmp_path):
        from meshpipeline.agents.builder.rerun_guard import case_digest
        (tmp_path / "gmsh_spec.json").write_text("{}")
        before = case_digest(tmp_path, ["gmsh_spec.json"])
        rp.write_fact(tmp_path, {"engine": "gmsh", "resolution_ladder": {"rung": 1, "factor": 1.5}})
        assert case_digest(tmp_path, ["gmsh_spec.json"]) != before


class TestWhetherARetryCanChangeIt:
    def test_a_resolution_note_retries_while_a_rung_is_left(self):
        assert rp.retry_can_change([_note()], {"resolution_ladder": {"rung": 0}})
        assert rp.retry_can_change([_note()], {"resolution_ladder": {"rung": 1}})
        assert not rp.retry_can_change([_note()], {"resolution_ladder": {"rung": 2}})

    def test_no_rung_when_the_mesh_already_fills_the_limit(self):
        full = _note(cells=int(0.95 * polcfg.CELL_HARD_LIMIT))
        assert not rp.retry_can_change([full], {})

    def test_a_cell_limit_note_never_retries(self):
        assert not rp.retry_can_change([{"kind": rp.OVER_CELL_LIMIT, "cells": 9e6}], {})

    def test_a_domain_extent_near_miss_retries_as_it_always_did(self):
        assert rp.retry_can_change([{"direction": "downstream"}], {})


class TestTheRungAfterASwitch:
    def _st(self, **over):
        st = {"executor_success": True, "retry_count": 2, "requirement_caveats": [_note()],
              "engine_ladder": {"switches": [{"from": "cfmesh", "to": "snappy"}]},
              "resolution_ladder": {"rung": 0, "engine": "snappy", "switched": True}}
        st.update(over)
        return st

    def test_the_engine_switched_to_gets_one_rung(self):
        assert rp.rung_after_switch(self._st(), attempts_ceiling=3, attempt_seconds=3600)[0]

    def test_not_without_a_switch(self):
        st = self._st(engine_ladder={}, resolution_ladder={"rung": 0})
        assert not rp.rung_after_switch(st, attempts_ceiling=3, attempt_seconds=3600)[0]

    def test_only_once(self):
        st = self._st(resolution_ladder={"rung": 1, "switched": True, "after_switch": True})
        assert not rp.rung_after_switch(st, attempts_ceiling=3, attempt_seconds=3600)[0]

    def test_never_past_the_runs_ceiling_of_attempts(self):
        assert not rp.rung_after_switch(self._st(retry_count=3), attempts_ceiling=3,
                                        attempt_seconds=3600)[0]

    def test_never_when_a_whole_attempt_no_longer_fits_the_runs_time(self):
        ok, why = rp.rung_after_switch(self._st(pipeline_deadline_epoch=1000.0),
                                       attempts_ceiling=3, attempt_seconds=3600, now=0.0)
        assert not ok and "time" in why


class TestTheNote:
    def test_a_resolution_gate_becomes_a_note_naming_the_gap(self):
        manifest = {"cell_count": 900_000, "quality": {"passage_under_floor": {"regions": [
            {"min": [0.0, 0.0, 0.0], "max": [0.002, 0.002, 0.002], "radius_m": 0.0015,
             "cells": 8.4, "edge_m": 0.0004, "points": 40},
            {"min": [0.1, 0.1, 0.1], "max": [0.2, 0.2, 0.2], "radius_m": 0.01, "cells": 11.0,
             "edge_m": 0.002, "points": 40}]}}}
        c = rp.caveat("resolution_floor", {"cells_across": 8.4, "needed": 12.0}, manifest,
                      engine="snappy")
        assert c["kind"] == rp.RESOLUTION_SHORT and c["rule"] == rp.LAD_DELIVER_SOUND
        assert c["cells"] == 900_000 and c["cell_limit"] == polcfg.CELL_HARD_LIMIT
        assert c["narrowest"]["cells_across"] == 8.4
        assert c["narrowest"]["at_m"] == [0.001, 0.001, 0.001]
        words = "\n".join(rp.note_lines(c))
        assert "about 3 mm across" in words and "(1, 1, 1) mm" in words
        assert "8.4 cells across" in words and "Our floor is 12" in words
        assert "sound" in words and "delivered" in words

    def test_a_cell_limit_note_says_the_size_plainly(self):
        c = rp.caveat("cell_limit", {"cells": 16_000_000, "limit": 8_000_000}, {})
        assert c["kind"] == rp.OVER_CELL_LIMIT and c["rule"] == rp.LAD_CELL_LIMIT
        assert "16.0 M cells" in "\n".join(rp.note_lines(c))

    def test_the_cell_limit_gate_is_a_note_with_both_counts(self, tmp_path):
        (tmp_path / "mesh_manifest.json").write_text(json.dumps(
            {"cell_count": polcfg.CELL_HARD_LIMIT + 5}))
        ok, fb = rp.gate_cell_limit(GateCtx(workspace=Path(tmp_path)))
        assert ok is False and cause_of(fb) == "cell_budget"
        assert facts_of(fb) == {"cells": polcfg.CELL_HARD_LIMIT + 5,
                                "limit": polcfg.CELL_HARD_LIMIT}
        (tmp_path / "mesh_manifest.json").write_text(json.dumps({"cell_count": 10}))
        assert rp.gate_cell_limit(GateCtx(workspace=Path(tmp_path)))[0] is True

    def test_every_flow_engine_declares_the_note_gates_non_blocking(self):
        from meshpipeline.engines.registry import get_spec
        for engine in ("snappy", "cfmesh", "vmtk", "gmsh"):
            gates = {g.key: g for g in get_spec(engine).gates}
            for key in rp.NOTE_GATES:
                assert key in gates, f"{engine} has no {key} gate"
                assert gates[key].blocking is False, f"{engine}'s {key} still blocks"


class TestTheSoundMeshesOfARun:
    def _ws(self, root: Path, name: str, *, fatal=()) -> str:
        ws = root / name
        ws.mkdir()
        (ws / "mesh_manifest.json").write_text(json.dumps(
            {"mesh_mode": "snappy", "quality": {"fatal": list(fatal)}}))
        return str(ws)

    def test_the_newest_sound_mesh_is_the_one_delivered(self, tmp_path):
        a1, a2 = self._ws(tmp_path, "attempt_1"), self._ws(tmp_path, "attempt_2")
        st = {"sound_attempts": [{"attempt": 1, "workspace": a1}, {"attempt": 2, "workspace": a2}]}
        assert rp.deliverable_sound(st)["attempt"] == 2

    def test_a_mesh_that_reads_broken_now_is_never_delivered(self, tmp_path):
        a1 = self._ws(tmp_path, "attempt_1")
        a2 = self._ws(tmp_path, "attempt_2", fatal=["open cells"])
        st = {"sound_attempts": [{"attempt": 1, "workspace": a1}, {"attempt": 2, "workspace": a2}]}
        assert rp.deliverable_sound(st)["attempt"] == 1
        assert rp.deliverable_sound({"sound_attempts": []}) is None

    def test_an_entry_is_kept_once_per_workspace(self):
        st = {"retry_count": 1, "engine": "cfmesh", "engine_params": {},
              "sound_attempts": [{"attempt": 1, "workspace": "/w/a1"}]}
        rows = rp.with_sound(st, rp.sound_entry(st, workspace="/w/a1", manifest={}, caveats=[]))
        assert len(rows) == 1


def test_the_gap_plan_records_the_big_machine_hook():
    from meshpipeline.engines import gap_sizing as gs

    def regions_at(t):
        return [{"min": [0, 0, 0], "max": [1, 1, 1], "radius_m": 0.001, "cell_m": 0.0001,
                 "volume_m3": 1.0, "fluid_m3": 1.0}]

    over = gs.plan_gaps(regions_at, cell_m=0.001, budget=1_000_000, limit=8_000_000)
    assert over.record()["route_big"] is True and over.record()["route_rule"] == rp.PLAN_ROUTE_BIG
    fits = gs.plan_gaps(lambda t: [], cell_m=0.001, budget=1_000_000, limit=8_000_000)
    assert fits.record()["route_big"] is False
