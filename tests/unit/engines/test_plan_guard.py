# Responsibility: Verify AIG-2: an AI builder plan that plans over the cell limit where the rules plan does not is replaced by the rules plan, said to the builder and cited; every other AI plan is used unchanged; the cfMesh planned-cell estimate lands on the octree level cfMesh makes.
# Boundaries: plan_guard over stub renders, configure_mesh with a stub engine bundle, and the real cfMesh render of a small external case. Nothing meshes.
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import plan_guard as G
from meshpipeline.engines import rules_plan as rb
from meshpipeline.engines.cfmesh import cfmesh_runner as R

LIMIT = 8_000_000

#: THE REPRODUCER (aiguard lab, arm A, the real model as builder, 2026-10-28): the cfMesh carotid tube
#: biomedical__carotid_nonstenotic_plaques__orig__right-01-icas.stl. The AI asked 2 layers and a wall
#: cell under the 40-across ceiling (held to 0.121 mm, which cfMesh meshes at the 0.068 mm octree
#: level); the mesh came to 32.7 M cells, delivered over the 8 M limit (LAD-cfd-4). Configured offline
#: on that file, the AI strategy plans 28.07 M cells and the rules plan (the 5 layers asked, the
#: engine's sizes) 3.14 M; the rules plan meshed clean in the lab (0.53 M cells).
AI_STRATEGY = {"n_layers": 2, "wall_cell": 6e-05}
AI_PLANNED = 28_067_694
RULES_PLANNED = 3_144_349


def _ws(tmp_path: Path, request: str = "Internal CFD mesh. Near-wall: 5 prism layers.") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "request.txt").write_text(request, encoding="utf-8")
    return ws


class _Render:
    """A stand-in for configure_mesh's render seam: the planned cells per strategy, every call kept."""

    def __init__(self, rules_result: dict | None = None, raises: bool = False) -> None:
        self.calls: list = []
        self.rules_result = rules_result
        self.raises = raises

    def __call__(self, rules_args):
        self.calls.append(rules_args)
        if rules_args is None:
            return {"success": True, "planned_cells": AI_PLANNED, "who": "ai"}
        if self.raises:
            raise RuntimeError("render failed")
        return self.rules_result if self.rules_result is not None else {
            "success": True, "planned_cells": RULES_PLANNED, "who": "rules", "next": "run_mesh"}


def _guard(ws, ai_result, render, *, strategy=AI_STRATEGY, job_id="job-aig2"):
    return G.keep_no_worse(engine="cfmesh", workspace=ws, ai_strategy=strategy, ai_result=ai_result,
                           render=render, limit=LIMIT, job_id=job_id)


def test_the_carotid_tube_ai_plan_over_the_limit_gives_way_to_the_rules_plan(tmp_path):
    ws = _ws(tmp_path)
    render = _Render()
    out = _guard(ws, {"success": True, "planned_cells": AI_PLANNED, "who": "ai"}, render,
                 job_id="job-carotid")
    # the rules plan the RulesBuilder sends (the layers the request asks for) is what was written
    assert render.calls == [{"n_layers": 5}]
    assert out["who"] == "rules" and out["rules_plan_used"] is True and out["rule"] == "AIG-2"
    assert out["ai_plan"] == {"planned_cells": AI_PLANNED, "strategy": AI_STRATEGY}
    # the builder is told plainly, so it does not fight it
    assert "about 28.1 M cells, over the 8.0 M cell limit" in out["note"]
    assert "the rules plan (about 3.1 M cells) was configured instead" in out["note"]
    assert out["next"].startswith(out["note"]) and "call run_mesh NOW" in out["next"]
    # ...and the why record cites the rule with both plans' numbers
    cited = [e for e in dr.cited_for("job-carotid") if e["rule"] == "AIG-2"]
    assert len(cited) == 1
    assert cited[0]["value"] == "rules plan"
    assert cited[0]["inputs"]["ai_planned_cells"] == AI_PLANNED
    assert cited[0]["inputs"]["rules_planned_cells"] == RULES_PLANNED
    assert cited[0]["inputs"]["cell_limit"] == LIMIT


def test_an_ai_plan_under_the_limit_is_used_unchanged(tmp_path):
    render = _Render()
    ai = {"success": True, "planned_cells": 2_000_000, "who": "ai"}
    assert _guard(_ws(tmp_path), ai, render, job_id="job-under") is ai
    assert render.calls == []                               # the rules plan is never even written
    assert not [e for e in dr.cited_for("job-under") if e["rule"] == "AIG-2"]


@pytest.mark.parametrize("estimate", [None, "28e6", True, -5, 0, float("nan"), float("inf"), [1]])
def test_an_estimate_that_does_not_read_never_blocks_the_ai_plan(tmp_path, estimate):
    # the strategy already passed the engine's validation (keys, types, ranges): with no readable
    # estimate there is nothing measured to set against it, so it stands as validated
    render = _Render()
    ai = {"success": True, "planned_cells": estimate, "who": "ai"}
    assert _guard(_ws(tmp_path), ai, render) is ai
    assert render.calls == []


@pytest.mark.parametrize("rules_result", [
    {"success": True, "planned_cells": 9_000_000, "who": "rules"},     # the rules plan is over too
    {"success": True, "who": "rules"},                                 # the rules plan has no estimate
    {"success": False, "error": "x"},                                  # the rules plan was refused
])
def test_a_rules_plan_that_is_no_better_leaves_the_ai_plan_written_back(tmp_path, rules_result):
    render = _Render(rules_result=rules_result)
    out = _guard(_ws(tmp_path), {"success": True, "planned_cells": AI_PLANNED, "who": "ai"}, render)
    assert render.calls == [{"n_layers": 5}, None]          # the AI plan is written back over it
    assert out["who"] == "ai" and "rules_plan_used" not in out


def test_a_rules_render_that_raises_leaves_the_ai_plan_written_back(tmp_path):
    render = _Render(raises=True)
    out = _guard(_ws(tmp_path), {"success": True, "planned_cells": AI_PLANNED, "who": "ai"}, render)
    assert render.calls == [{"n_layers": 5}, None] and out["who"] == "ai"


def test_the_rules_plan_itself_is_never_compared_with_itself(tmp_path):
    render = _Render()
    ai = {"success": True, "planned_cells": AI_PLANNED, "who": "ai"}
    assert _guard(_ws(tmp_path), ai, render, strategy={"n_layers": 5}) is ai
    assert render.calls == []


def test_worse_means_over_the_limit_where_the_rules_plan_is_not():
    assert G.worse(9_000_000, 3_000_000, LIMIT)
    assert not G.worse(9_000_000, 9_500_000, LIMIT)      # both over: not proven worse
    assert not G.worse(7_000_000, 1_000_000, LIMIT)      # under the limit: the AI's call
    assert not G.worse(None, 1, LIMIT) and not G.worse(9_000_000, None, LIMIT)


def test_rules_configure_args_are_the_rules_builders_own(tmp_path):
    ws = _ws(tmp_path)
    assert rb.configure_args("cfmesh", ws) == {"n_layers": 5}
    assert rb.configure_args("cfmesh", _ws(tmp_path / "b", "mesh this duct")) == {}
    # the engines whose rules plan needs a tool's answer first have none to compare with
    for e in ("vmtk", "gmsh", "snappy_multiregion", "snappy"):
        assert rb.configure_args(e, ws) is None


# configure_mesh, end to end with a stub engine bundle

def test_configure_mesh_writes_the_rules_plan_when_the_ai_plan_is_worse(tmp_path, monkeypatch):
    from tests._geometry_support import materialized

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import _tool_configure_mesh
    from meshpipeline.engines import runtime
    ws = _ws(tmp_path)
    (ws / "input.stl").write_text("solid x\nendsolid x\n")
    rendered: list = []

    class _Bundle:
        name = "cfmesh"

        def configure_mesh(self, workspace, *, strategy, **_k):
            rendered.append(dict(strategy))
            (Path(workspace) / "system").mkdir(exist_ok=True)
            (Path(workspace) / "system" / "meshDict").write_text(repr(strategy))
            cells = AI_PLANNED if strategy.get("wall_cell") else RULES_PLANNED
            return {"success": True, "planned_cells": cells, "next": "Call run_mesh NOW."}
    monkeypatch.setattr(runtime, "get_engine", lambda *_a, **_k: _Bundle())
    ctx = BuilderToolContext(workspace=ws, geometry=materialized(tmp_path / "geo"), engine="cfmesh",
                             job_id="job-e2e")
    out = _tool_configure_mesh(ctx, dict(AI_STRATEGY))
    assert rendered == [AI_STRATEGY, {"n_layers": 5}]
    assert out["success"] and out["rules_plan_used"] and out["planned_cells"] == RULES_PLANNED
    assert (ws / "system" / "meshDict").read_text() == repr({"n_layers": 5})
    # an AI plan under the limit is rendered once and used as it is
    rendered.clear()
    out = _tool_configure_mesh(ctx, {"n_layers": 3})
    assert rendered == [{"n_layers": 3}] and "rules_plan_used" not in out


# the cfMesh estimate

def test_octree_cell_is_the_level_cfmesh_lands_on():
    assert math.isclose(R.octree_cell(0.000543, 0.0001206), 0.000543 / 8)     # the carotid wall
    assert math.isclose(R.octree_cell(0.4, 0.1), 0.1)                          # exactly on a level
    assert math.isclose(R.octree_cell(0.4, 0.1 * 1.001), 0.1)                  # nudged up: stops there
    assert math.isclose(R.octree_cell(0.4, 0.1 * 0.999), 0.05)                 # a hair under: one finer
    assert R.octree_cell(0.4, 0.5) == 0.5 and R.octree_cell(0.0, 0.1) == 0.1


def _external(tmp_path, **strategy) -> dict:
    body_bbox = ([-0.5, -0.1, -0.1], [0.5, 0.1, 0.1])
    dmin, dmax = R.domain_from_strategy(body_bbox, L=1.0, strategy=strategy)
    return R.render_cfmesh_case(
        tmp_path, surface_file="geom.fms", wall_patch="body",
        patches=[{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
        body_bbox=body_bbox, L=1.0, domain_min=dmin, domain_max=dmax, strategy=strategy,
        cell_budget=LIMIT, wall_area=50.0)


def test_the_planned_cells_cost_the_wall_at_its_octree_level_and_every_refinement(tmp_path):
    level = _external(tmp_path / "m", max_cell_factor=40)["max_cell_size"] / 8.0
    on = _external(tmp_path / "a", max_cell_factor=40, wall_cell=level * 1.001, n_layers=3)
    under = _external(tmp_path / "b", max_cell_factor=40, wall_cell=level * 0.999, n_layers=3)
    assert R.planned_cells_of(on) and R.planned_cells_of(under)
    # asked a hair finer than a level, cfMesh makes the next level: 4 x the wall's cells
    bg = on["est_background_cells"]
    assert under["est_background_cells"] == bg
    assert under["planned_cells"] - bg == pytest.approx(4.0 * (on["planned_cells"] - bg), rel=0.01)
    box = {"name": "b", "type": "box", "cellSize": level * 1.001, "centre": [0, 0, 0],
           "lengthX": 1.0, "lengthY": 0.2, "lengthZ": 0.2}
    boxed = _external(tmp_path / "c", max_cell_factor=40, wall_cell=level * 1.001, n_layers=3,
                      features=[box])
    assert boxed["planned_cells"] - on["planned_cells"] == pytest.approx(0.04 / level ** 3, abs=2)


def test_the_cfmesh_spec_reads_its_planned_cells():
    from meshpipeline.engines.registry import get_spec
    spec = get_spec("cfmesh")
    assert spec.planned_cells({"planned_cells": 123456}) == 123456
    assert spec.planned_cells({"planned_cells": "x"}) is None and spec.planned_cells({}) is None
    assert get_spec("gmsh").planned_cells({"planned_cells": 5}) is None     # declares no estimate
