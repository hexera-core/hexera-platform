"""Fix 1 review follow-ups: an over-limit mesh still has its regions counted, a broken last snappy
pass never throws an earlier sound one away, the after-switch rung reads the earlier of the run's
two clocks, a VMTK rung survives the budget fit, a VMTK fill measured only by its median radius
keeps its note, and every passage's openings get their cells across in cfMesh."""
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.engines import resolution_policy as rp


def _foam_list(path: Path, values) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(str(int(v)) for v in values)
    path.write_text(f"FoamFile\n{{\n    format ascii;\n}}\n\n{len(values)}\n(\n{body}\n)\n")


def _polymesh(ws: Path, owner, neighbour) -> None:
    _foam_list(ws / "constant" / "polyMesh" / "owner", owner)
    _foam_list(ws / "constant" / "polyMesh" / "neighbour", neighbour)


class TestRegionCountOverTheLimit:
    def test_one_connected_region_passes(self, tmp_path):
        from meshpipeline.engines.snappy.solvability import check_connected
        # cells 0-1-2 in a row; boundary faces owned by each
        _polymesh(tmp_path, [0, 1, 0, 1, 2], [1, 2])
        assert check_connected(tmp_path) == (True, "")

    def test_a_disconnected_pocket_fails(self, tmp_path):
        from meshpipeline.engines.snappy.solvability import check_connected
        # cells 0-1 joined, cell 2 on its own
        _polymesh(tmp_path, [0, 0, 1, 2], [1])
        ok, why = check_connected(tmp_path)
        assert not ok and "2 disconnected regions" in why

    def test_one_region_per_passage_meshed_is_fine(self, tmp_path):
        from meshpipeline.cad.fluid_passages import PASSAGES_FILE
        from meshpipeline.engines.snappy.solvability import check_connected
        _polymesh(tmp_path, [0, 0, 1, 2], [1])
        (tmp_path / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"]}))
        assert check_connected(tmp_path)[0]


class TestSoundPassKept:
    def _pass(self, ws: Path, tag: str) -> None:
        _polymesh(ws, [0, 0], [1])
        (ws / "constant" / "polyMesh" / "tag").write_text(tag)
        (ws / "mesh_quality.json").write_text(json.dumps({"tag": tag}))

    def test_a_broken_last_pass_gives_way_to_the_kept_sound_one(self, tmp_path):
        from meshpipeline.engines.snappy import drivers as d
        self._pass(tmp_path, "sound")
        assert d._keep_sound_mesh(tmp_path)
        self._pass(tmp_path, "broken")
        assert d._end_passes(tmp_path, False, "j") is True
        assert (tmp_path / "constant" / "polyMesh" / "tag").read_text() == "sound"
        assert json.loads((tmp_path / "mesh_quality.json").read_text())["tag"] == "sound"
        assert not (tmp_path / d._SOUND_KEEP).exists()

    def test_a_sound_last_pass_stands(self, tmp_path):
        from meshpipeline.engines.snappy import drivers as d
        self._pass(tmp_path, "older")
        d._keep_sound_mesh(tmp_path)
        self._pass(tmp_path, "newest")
        assert d._end_passes(tmp_path, True, "j") is True
        assert (tmp_path / "constant" / "polyMesh" / "tag").read_text() == "newest"
        assert not (tmp_path / d._SOUND_KEEP).exists()

    def test_nothing_kept_nothing_delivered(self, tmp_path):
        from meshpipeline.engines.snappy import drivers as d
        assert d._end_passes(tmp_path, False, "j") is False


def test_the_after_switch_rung_reads_the_earlier_clock():
    st = {"engine": "snappy", "retry_count": 1, "engine_ladder": {"switches": [{"to": "snappy"}]},
          "resolution_ladder": {"rung": 0, "switched": True},
          "requirement_caveats": [{"kind": rp.RESOLUTION_SHORT, "cells_across": 8.0,
                                   "floor": 12.0, "cells": 1_000_000, "cell_limit": 8_000_000}],
          "pipeline_deadline_epoch": 100_000.0, "builder_deadline_epoch": 1000.0}
    ok, why = rp.rung_after_switch(st, attempts_ceiling=3, attempt_seconds=3600, now=0.0)
    assert not ok and "time" in why
    st["builder_deadline_epoch"] = 50_000.0
    assert rp.rung_after_switch(st, attempts_ceiling=3, attempt_seconds=3600, now=0.0)[0]


class TestVmtk:
    def test_a_fill_measured_only_by_its_median_radius_keeps_its_note(self, tmp_path):
        from meshpipeline.engines.gates import GateCtx
        from meshpipeline.engines.vmtk.gates import _gate_resolution_floor
        q = {"passage_cells_across": 6.0, "under_resolved": True}
        ok, fb = _gate_resolution_floor(GateCtx(workspace=tmp_path, manifest={"quality": q}))
        assert ok is False and "6" in fb
        q = {"passage_cells_across": 14.0}
        assert _gate_resolution_floor(GateCtx(workspace=tmp_path, manifest={"quality": q}))[0]
        assert _gate_resolution_floor(GateCtx(workspace=tmp_path, manifest={"quality": {}}))[0]

    def test_the_budget_fit_never_undoes_a_rung(self):
        from meshpipeline.engines.vmtk.budget import edge_factor_ceiling
        from meshpipeline.engines.vmtk.criteria import PASSAGE_MIN_CELLS_ACROSS
        # the coarsest edge the fit may reach at rung 1 (x1.5) is finer than the plain floor's
        assert (edge_factor_ceiling(PASSAGE_MIN_CELLS_ACROSS * 1.5)
                < edge_factor_ceiling(PASSAGE_MIN_CELLS_ACROSS))


def test_cfmesh_sizes_every_passages_openings(monkeypatch):
    from meshpipeline.engines import passage
    from meshpipeline.engines.cfmesh import cfmesh_runner as cr
    monkeypatch.setattr(passage, "lid_hydraulic_diameters",
                        lambda srcs, wall: {"inlet": 0.01, "outlet_b": 0.002, "wall": 0.5})
    srcs = {"wall": ["w.stl"], "inlet": ["i.stl"], "outlet_b": ["o.stl"]}
    widths = cr._opening_widths(srcs, "wall", [], names=list(srcs))
    assert set(widths) == {"inlet", "outlet_b"}


class TestRungsThatCannotRefine:
    """Lab gr-br2 (2026-10-23): a held gap plan whose budget was already the whole job limit
    rebuilt the same mesh on every rung (cfMesh 7 of 7); one under a smaller budget could still
    gain (snappy gyroid phantoms 8 -> 12 cells across)."""

    def _note(self, **plan):
        return {**{"kind": rp.RESOLUTION_SHORT, "cells_across": 9.5, "floor": 12.0,
                   "cells": 5_000_000, "cell_limit": 8_000_000}, "plan": plan}

    def test_a_held_plan_at_the_full_limit_gets_no_rung(self):
        c = self._note(status="limited", budget=8_000_000, limit=8_000_000)
        assert not rp.retry_can_change([c], {"resolution_ladder": {"rung": 0}})

    def test_a_held_plan_under_a_smaller_budget_keeps_its_rung(self):
        c = self._note(status="limited", budget=5_465_986, limit=8_000_000)
        assert rp.retry_can_change([c], {"resolution_ladder": {"rung": 0}})

    def test_a_plan_that_fit_keeps_its_rung(self):
        c = self._note(status="fits", budget=8_000_000, limit=8_000_000)
        assert rp.retry_can_change([c], {"resolution_ladder": {"rung": 0}})

    def test_the_caveat_carries_the_plan_and_says_route_big(self):
        plan = {"status": "limited", "route_big": True, "needed": 139_669_015,
                "affordable": 7.5, "budget": 8_000_000, "limit": 8_000_000}
        c = rp.caveat("resolution_floor", {"cells_across": 9.5, "needed": 12.0}, {}, plan=plan)
        assert c["plan"]["route_big"] is True and c["plan"]["budget"] == 8_000_000
        assert any("140 M cells" in line for line in rp.note_lines(c))


class TestTheBestSoundMeshIsDelivered:
    def _ws(self, root: Path, name: str) -> str:
        ws = root / name
        ws.mkdir()
        (ws / "mesh_manifest.json").write_text(json.dumps({"quality": {"fatal": []}}))
        return str(ws)

    def _row(self, n, ws, across):
        cav = [] if across is None else [{"kind": rp.RESOLUTION_SHORT, "cells_across": across}]
        return {"attempt": n, "workspace": ws, "requirement_caveats": cav}

    def test_a_coarser_rung_never_replaces_a_finer_sound_mesh(self, tmp_path):
        a1, a2 = self._ws(tmp_path, "attempt_1"), self._ws(tmp_path, "attempt_2")
        state = {"executor_success": True, "openfoam_workspace": a2,
                 "sound_attempts": [self._row(1, a1, 9.5), self._row(2, a2, 8.1)]}
        assert rp.deliverable_sound(state)["attempt"] == 1
        assert rp.better_earlier_sound(state)["attempt"] == 1

    def test_a_finer_or_equal_last_mesh_stands(self, tmp_path):
        a1, a2 = self._ws(tmp_path, "attempt_1"), self._ws(tmp_path, "attempt_2")
        finer = {"openfoam_workspace": a2,
                 "sound_attempts": [self._row(1, a1, 8.0), self._row(2, a2, 11.8)]}
        assert rp.better_earlier_sound(finer) is None
        same = {"openfoam_workspace": a2,
                "sound_attempts": [self._row(1, a1, 9.5), self._row(2, a2, 9.5)]}
        assert rp.deliverable_sound(same)["attempt"] == 2 and rp.better_earlier_sound(same) is None
        met = {"openfoam_workspace": a2,
               "sound_attempts": [self._row(1, a1, 11.0), self._row(2, a2, None)]}
        assert rp.better_earlier_sound(met) is None

    def test_the_router_delivers_the_better_earlier_mesh(self, tmp_path):
        import meshpipeline.agents.builder.settings as bcfg
        from meshpipeline.pipeline.graph import route_after_executor
        a1, a2 = self._ws(tmp_path, "attempt_1"), self._ws(tmp_path, "attempt_2")
        note = {"kind": rp.RESOLUTION_SHORT, "cells_across": 8.1, "floor": 12.0,
                "cells": 2_500_000, "cell_limit": 8_000_000}
        state = {"executor_success": True, "retry_count": bcfg.MAX_BUILDER_RETRIES + 1,
                 "job_id": "j", "engine": "snappy", "openfoam_workspace": a2,
                 "requirement_caveats": [note],
                 "sound_attempts": [self._row(1, a1, 9.5), self._row(2, a2, 8.1)]}
        assert route_after_executor(state) == "node_deliver_sound"
