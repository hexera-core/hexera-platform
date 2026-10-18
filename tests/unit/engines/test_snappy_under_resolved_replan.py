# Responsibility: Verify the attempt after a mesh too coarse across its passage puts the floor across it, by arithmetic, whatever the re-plan proposed.
# Boundaries: planner.refine_after_under_resolved and its use by the internal snappy driver, with the mesher, the staging and the planner model stubbed.
"""Job 02ed0d14 (shell-and-tube exchanger): pass 2 of attempt 1 held 8.3 cells across its narrowest
passage. The gate told the re-plan to "refine the wall surface level"; the model raised the level
expecting twice the cells across, and cells_across_diameter only from 24 to 28. The internal wall
cell is bore / cells_across_diameter whatever the level, so attempt 2 came out at 9.8 - a 27-minute
attempt that could not have passed. The next plan now puts (floor + 1) across the passage it fell
short at, and the cell budget follows."""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.drivers as drv
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo
from meshpipeline.engines.snappy.planner import refine_after_under_resolved

GATED = {"approach": "pass 2", "cells_across_diameter": 24, "surface_level": 2,
         "n_layers": 5, "max_cells": 5_000_000}
FACTS = {"cells_across": 8.3, "needed": 12, "scope": "narrowest", "cells": 4_178_590,
         "cell_limit": 8_000_000, "rebuild_cells": 8_735_000}


def test_cells_across_go_up_by_what_the_passage_lacked():
    out, changes = refine_after_under_resolved(GATED, {**GATED, "cells_across_diameter": 28,
                                                       "surface_level": 3},
                                               measured=8.3, needed=12, cells=None,
                                               ceiling=8_000_000)
    assert out["cells_across_diameter"] == 38          # ceil(24 x 13 / 8.3)
    assert out["surface_level"] == 3, "the rest of the re-plan stands"
    assert changes == ["cells across the bore raised from 28 to 38"]


def test_the_budget_follows_up_to_the_ceiling():
    out, changes = refine_after_under_resolved(GATED, dict(GATED), measured=10.0, needed=12,
                                               cells=2_000_000, ceiling=8_000_000)
    # 1.5 x 2.0 M x (13 / 10)^2 = 5.07 M over the plan's 5 M
    assert out["max_cells"] == int(1.5 * 2_000_000 * 1.3 ** 2)
    assert "cell budget raised from 5 M to 5.1 M" in changes
    capped, _ = refine_after_under_resolved(GATED, dict(GATED), **{
        "measured": 8.3, "needed": 12, "cells": 4_178_590, "ceiling": 8_000_000})
    assert capped["max_cells"] == 8_000_000


@pytest.mark.parametrize("proposal", [{"cells_across_diameter": 40}, {"cells_across_diameter": 38}])
def test_a_re_plan_that_already_asks_for_enough_stands(proposal):
    out, changes = refine_after_under_resolved(GATED, {**GATED, **proposal}, measured=8.3,
                                               needed=12, ceiling=8_000_000)
    assert out["cells_across_diameter"] == proposal["cells_across_diameter"] and changes == []


@pytest.mark.parametrize("gated, measured, needed", [
    (None, 8.3, 12), (GATED, None, 12), (GATED, "8.3", 12), (GATED, 0.0, 12), (GATED, 13.0, 12),
    (GATED, float("nan"), 12), (GATED, True, 12)])
def test_nothing_known_to_refine_against_changes_nothing(gated, measured, needed):
    out, changes = refine_after_under_resolved(gated, dict(GATED), measured=measured,
                                               needed=needed, ceiling=8_000_000)
    assert out == GATED and changes == []


# the driver, after an attempt the floor refused

class _Publish:
    def __init__(self):
        self.notes: list[str] = []

    async def anote(self, text, *_a, **_k): self.notes.append(text)
    async def aerror(self, text, *_a, **_k): self.notes.append(text)
    async def awarn(self, *_a, **_k): pass
    async def astage(self, *_a, **_k): pass
    async def aattempt(self, *_a, **_k): pass
    async def acheck(self, *_a, **_k): pass
    async def arationale(self, *_a, **_k): pass
    async def atool_call(self, *_a, **_k): pass
    async def atool_result(self, *_a, **_k): pass
    async def ameshing(self, *_a, **_k): pass
    async def ameshed(self, *_a, **_k): pass
    async def areasoning(self, *_a, **_k): pass


@pytest.fixture
def retry(monkeypatch, tmp_path):
    import meshpipeline.engines.snappy.planner as P
    import meshpipeline.engines.snappy.snappy_runner as R
    from meshpipeline.cad.stl_io import _box_triangles, _write_solid
    w = types.SimpleNamespace(cases=[], plans=[])
    attempt_1, attempt_2 = tmp_path / "attempt_1", tmp_path / "attempt_2"
    attempt_1.mkdir()
    attempt_2.mkdir()
    (attempt_1 / ".last_plan.json").write_text(json.dumps(GATED))
    stl_dir = attempt_2 / "_internal_stls"

    def _stl(name, lo, hi):
        stl_dir.mkdir(parents=True, exist_ok=True)
        p = stl_dir / f"{name}.stl"
        with p.open("w") as fh:
            _write_solid(fh, name, _box_triangles(lo, hi))
        return str(p)

    def _tess(*_a, **_k):
        return {"stls": {"wall": _stl("wall", [0, 0, 0], [0.5, 0.3, 0.3]),
                         "inlet": _stl("inlet", [0, 0.1, 0.1], [0.001, 0.2, 0.2]),
                         "outlet": _stl("outlet", [0.499, 0.1, 0.1], [0.5, 0.2, 0.2])},
                "interior_point": [0.25, 0.15, 0.15], "bbox_min": [0, 0, 0],
                "bbox_max": [0.5, 0.3, 0.3],
                "openings": {"inlet": {"area": 0.00683, "centroid": [0.0, 0.15, 0.15]},
                             "outlet": {"area": 0.00683, "centroid": [0.5, 0.15, 0.15]}}}

    def _run_native(workspace, **_k):
        w.cases.append((Path(workspace) / "system" / "snappyHexMeshDict").read_text())
        return {"rc": 0, "timed_out": False}

    async def _plan(**kw):
        w.plans.append(kw)
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"), input_tokens=1,
                              output_tokens=1)
        # the model's own re-plan: a level up and 28 across, as job 02ed0d14's was
        return P.PlanOutcome({**GATED, "cells_across_diameter": 28, "surface_level": 3}, rr)

    monkeypatch.setattr(P, "plan_with_accounting", _plan)
    monkeypatch.setattr(R, "tessellate_internal", _tess)
    monkeypatch.setattr(R, "run_snappy", _run_native)
    monkeypatch.setattr(R, "check_mesh", lambda *_a, **_k: {
        "cells": 7_000_000, "fatal": [], "skew_fraction": 0.0, "skew_faces": 0})
    monkeypatch.setattr(R, "_patch_face_counts",
                        lambda *_a, **_k: {"wall": 810_216, "inlet": 900, "outlet": 900})
    monkeypatch.setattr(drv, "_plan_surface", lambda *_a, **_k: types.SimpleNamespace(
        consumed=None))
    monkeypatch.setattr(drv, "read_purpose", lambda *_a, **_k: "internal_cfd")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(drv.polcfg, "CELL_HARD_LIMIT", 8_000_000, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})

    def go(cause: str = "under_resolved"):
        from tests._geometry_support import geometry_state as _geometry_state

        from meshpipeline.contracts.geometry_units import LengthUnit
        src = tmp_path / "part.step"
        src.write_text("ISO-10303-21;\n")
        state = {"builder_mode": "revise", "engine": "snappy", "request_txt": "r",
                 "intake_patches": [], "dimensionality": "3D", "flow_topology": "internal",
                 "input_kind": "fluid-domain", "purpose": "internal_cfd",
                 "executor_failure_cause": cause, "executor_failure_facts": dict(FACTS),
                 "geometry": _geometry_state(tmp_path / "_src", unit=LengthUnit.metre,
                                             filename="part.step")}
        run = BuilderDriverRun(job_id="j", engine="snappy", mode="revise", deadline_s=600.0)

        async def _fence(*_a, **_k):
            return None

        monkeypatch.setattr(run, "fence", _fence)
        pub = _Publish()
        out = asyncio.run(drv.drive(attempt_2, state, job_id="j", publish=pub, run=run,
                                    source_path=str(src)))
        return out, pub
    w.go = go
    w.ws = attempt_2
    return w


def test_the_attempt_after_an_under_resolved_mesh_is_finer_where_it_counts(retry):
    (ok, _value, _outcome), pub = retry.go()
    assert ok is True
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    # x 13 / 8.3 would project 4.18 M cells to 10.3 M, over the 8 M limit: the factor is held
    # to the limit, but never below 12 / 8.3 - the floor itself: ceil(24 x 1.446) = 35
    assert "about 35 cells across the bore" in filling[0], filling
    opened = [n for n in pub.notes if n.startswith("Meshing pass 1 of")]
    assert ("made finer because the last mesh had too few cells across its narrowest passage: "
            "cells across the bore raised from 28 to 35; cell budget raised from 5 M to 8 M"
            in opened[0]), opened
    plan = json.loads((retry.ws / ".last_plan.json").read_text())
    assert plan["cells_across_diameter"] == 35 and plan["max_cells"] == 8_000_000
    assert "maxGlobalCells 8000000" in retry.cases[0]


def test_any_other_failure_leaves_the_re_plan_alone(retry):
    (_ok, _value, _outcome), pub = retry.go(cause="mesh_quality")
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    assert "about 28 cells across the bore" in filling[0], filling
    assert not any("made finer" in n for n in pub.notes)
