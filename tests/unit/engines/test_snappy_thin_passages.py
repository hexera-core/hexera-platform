# Responsibility: Verify the internal snappy case sizes its near-wall band for the passage and the
# budget, measures its local refinements against the cell AT the wall, and answers a mesh short of the
# resolution floor by raising its narrow passages first.
# Boundaries: snappy_runner's band planner and case renderer, the driver's retry arithmetic, and the
# driver after an under-resolved attempt with the mesher, the staging and the planner model stubbed.
"""annular_001 (a 13.2 mm gap between a 151 mm bore and a centre rod, 820 mm long): the near-wall band
- every cell within eight wall cells (or 2% of the part) of the wall one level finer - is deeper than
the gap is wide, so it filled the whole annulus at twice the planned resolution: ~30 M cells and a
quartered wall carrying 5 prism layers, against a 2 M budget. The retry raised the whole part and
was killed for memory three times.

The Fluent aorta: its narrow-passage boxes, held back by the budget to one level past the wall level
- the level the band already gives every cell there - refined nothing, and the retry raised the
whole part (24 -> 44 cells across the bore, 1.3 -> 6.5 M cells) with the smallest branch still at 10
cells across."""
from __future__ import annotations

import asyncio
import json
import math
import re
import types
from pathlib import Path

import numpy as np
import pytest

import meshpipeline.engines.snappy.drivers as drv
import meshpipeline.engines.snappy.snappy_runner as R
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo

# the band planner


def _pipe(D=0.1, L=1.0, n=400):
    """A round pipe's wall, per point: (areas, radius)."""
    return np.full(n, math.pi * D * L / n), np.full(n, D / 2.0)


def test_a_round_pipe_at_its_planned_resolution_keeps_the_band_it_always_had():
    D = 0.1
    areas, radius = _pipe(D, L=0.6)
    h = D / 24
    default = R.default_near_band_depth(h, (0, 0, 0), (0.6, D, D))
    depth, note = R.plan_near_band(wall_cell=h, default_depth=default, budget_cells=2_000_000,
                                   areas=areas, radius=radius, n_layers=5)
    assert depth == default and note == ""


def test_an_annulus_thinner_than_the_band_gets_no_band():
    # annular_001: 151 mm bore, 124.8 mm rod, 820 mm long, sized at 24 across its 26.4 mm Dh
    wall = math.pi * (0.1512 + 0.1248) * 0.82
    areas, radius = np.full(100, wall / 100), np.full(100, 0.0066)
    h = 0.0264 / 24
    default = R.default_near_band_depth(h, (0, -0.081, -0.081), (0.82, 0.081, 0.081))
    assert default > 0.0132, "the default band is deeper than the whole gap"
    full = R.near_band_cost(default, wall_cell=h, areas=areas, radius=radius, n_layers=5)
    assert full > 20e6                                       # the ~30 M the lab saw, and layers
    depth, note = R.plan_near_band(wall_cell=h, default_depth=default, budget_cells=2_000_000,
                                   areas=areas, radius=radius, n_layers=5, hard_cells=8_000_000)
    assert depth == 0.0 and "no near-wall band" in note


def test_a_band_the_budget_cannot_carry_but_the_limit_can_keeps_its_wall_cell():
    # one wall cell of band is over half the budget but well within the hard limit: the wall
    # keeps its finer cells (its cells across), only the depth goes
    areas, radius = _pipe(D=0.02, L=0.3)
    h = 0.02 / 24
    one = R.near_band_cost(h, wall_cell=h, areas=areas, radius=radius, n_layers=5)
    depth, note = R.plan_near_band(wall_cell=h, default_depth=8 * h, budget_cells=one,
                                   areas=areas, radius=radius, n_layers=5, hard_cells=8_000_000)
    assert depth == pytest.approx(h) and "one wall cell" in note


def test_a_band_over_its_share_is_made_shallower_to_fit():
    areas, radius = _pipe(D=0.02, L=0.3)
    h = 0.02 / 24
    default = 8 * h
    budget = 1.2 * R.near_band_cost(default, wall_cell=h, areas=areas, radius=radius, n_layers=5)
    depth, note = R.plan_near_band(wall_cell=h, default_depth=default, budget_cells=budget,
                                   areas=areas, radius=radius, n_layers=5)
    assert h <= depth < default and "held to" in note
    assert R.near_band_cost(depth, wall_cell=h, areas=areas, radius=radius,
                            n_layers=5) <= 0.5 * budget + 1


def _render(tmp_path, **kw):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}
    s = R.render_internal_case(
        ws, names=names, features={k: f"{k}.eMesh" for k in names},
        interior_point=(0.3, 0.01, 0.01), bbox_min=(0, -0.05, -0.05), bbox_max=(0.6, 0.05, 0.05),
        base_cell=0.0166, surface_level=2, feature_level=3, n_layers=5, wall_key="wall", **kw)
    return (ws / "system" / "snappyHexMeshDict").read_text(), s


def test_no_band_means_the_wall_is_meshed_at_its_own_level(tmp_path):
    with_band, s = _render(tmp_path / "a")
    assert re.search(r"wall \{ mode distance; levels \(\([\d.e-]+ 3\)\); \}", with_band)
    assert s["near_band_level"] == 3 and s["near_band_m"] > 0
    without, s0 = _render(tmp_path / "b", near_band_depth=0.0)
    assert "mode distance" not in without.split("refinementRegions", 1)[1].split("locationInMesh")[0]
    assert s0["near_band_m"] == 0.0 and s0["near_band_level"] == 2
    held, s1 = _render(tmp_path / "c", near_band_depth=0.004)
    assert "wall { mode distance; levels ((0.004 3)); }" in held


def test_the_default_band_writes_the_case_as_it_always_was(tmp_path):
    a, s = _render(tmp_path / "a")
    b, _ = _render(tmp_path / "b", near_band_depth=None)
    assert a == b


def test_local_boxes_are_rebased_onto_the_wall_level():
    boxes = [{"level_bump": 1}, {"level_bump": 3}]
    assert [b["level_bump"] for b in drv._over_band(boxes, 1)] == [2, 4]
    assert [b["level_bump"] for b in drv._over_band([{"level_bump": 2}], 0)] == [2]


def _cylinder(r, length, n, *, inward):
    """A faceted cylinder along x, as a CAD tessellation stages it: n long facets round, each
    split in two triangles, wound so the normal points to (inward) or away from the axis."""
    tris = []
    for k in range(n):
        a0, a1 = 2 * math.pi * k / n, 2 * math.pi * (k + 1) / n
        p = [(x, r * math.cos(a), r * math.sin(a)) for x in (0.0, length) for a in (a0, a1)]
        t1, t2 = (p[0], p[1], p[3]), (p[0], p[3], p[2])
        if inward:
            t1, t2 = (t1[0], t1[2], t1[1]), (t2[0], t2[2], t2[1])
        tris += [t1, t2]
    return tris


def test_a_tube_bore_and_its_outer_skin_are_not_a_thin_plate():
    # annular_001's tube as its hollow-wall staging carries it: the bore and the outer skin,
    # 5.4 mm apart; the probe paired a bore facet with an outer one 21 degrees round and read
    # 0.42 mm. A gap reads the same from both faces, so there is no thin feature here.
    from meshpipeline.cad.thin_features import thin_refinement_boxes
    tris = (_cylinder(0.0756, 0.82, 63, inward=True) + _cylinder(0.081, 0.82, 63, inward=False)
            + _cylinder(0.0624, 0.82, 63, inward=False))
    boxes = thin_refinement_boxes(tris, cell_m=0.0011, budget_cells=1_000_000)
    assert len(boxes) == 0 and not boxes.thinnest_m


def test_a_real_plate_still_reads_its_thickness():
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.cad.thin_features import thin_refinement_boxes
    plate = _box_triangles([0.0, 0.0, 0.0], [0.2, 0.2, 0.002])      # 2 mm thick
    boxes = thin_refinement_boxes(plate, cell_m=0.004, budget_cells=1_000_000)
    assert boxes and boxes.thinnest_m == pytest.approx(0.002, rel=1e-6)


def test_a_whole_part_refinement_is_held_within_the_hard_limit():
    from meshpipeline.engines.snappy.planner import refine_after_under_resolved
    gated = {"cells_across_diameter": 24, "max_cells": 2_000_000}
    # annular_001 without its band: ~4.9 M cells at 10 across. x 13/10 projects 8.3 M, over 8 M
    out, _ = refine_after_under_resolved(gated, dict(gated), measured=10.0, needed=12,
                                         cells=4_900_000, ceiling=8_000_000)
    # held to the limit and rounded DOWN: 24 x sqrt(7.6 / 4.9) = 29.9 -> 29, not 30 (annular_001
    # at 30 came to 8.005 M cells)
    assert out["cells_across_diameter"] == math.floor(24 * math.sqrt(0.95 * 8e6 / 4.9e6))
    # a re-plan asking for more than the limit allows is held too
    out, changes = refine_after_under_resolved(gated, {**gated, "cells_across_diameter": 40},
                                               measured=10.0, needed=12, cells=4_900_000,
                                               ceiling=8_000_000)
    assert out["cells_across_diameter"] == 29 and "held to 29" in changes[0]
    # held, but never below the floor itself (12 / 10): past that no rebuild can help
    out, _ = refine_after_under_resolved(gated, dict(gated), measured=10.0, needed=12,
                                         cells=7_000_000, ceiling=8_000_000)
    assert out["cells_across_diameter"] == math.ceil(24 * 1.2)
    # well within it: (needed + 1) / measured, as before
    out, _ = refine_after_under_resolved(gated, dict(gated), measured=10.0, needed=12,
                                         cells=1_000_000, ceiling=8_000_000)
    assert out["cells_across_diameter"] == math.ceil(24 * 1.3)


# the retry, narrow passages first

GATED = {"approach": "pass 1", "cells_across_diameter": 24, "surface_level": 2, "n_layers": 5,
         "max_cells": 1_000_000, "near_band_m": 0.0}
FACTS = {"cells_across": 10.0, "needed": 12, "scope": "narrowest", "cells": 1_319_728,
         "cell_limit": 8_000_000, "rebuild_cells": 1_900_000}


def _field(narrow=True):
    """A 20 mm trunk with, optionally, a 3 mm branch beside it: (points, radius, areas)."""
    x = np.linspace(0.02, 0.28, 200)
    trunk = np.stack([x, np.full_like(x, 0.15), np.full_like(x, 0.15)], axis=1)
    pts, r = [trunk], [np.full(len(x), 0.010)]
    if narrow:
        branch = np.stack([x[:60], np.full(60, 0.05), np.full(60, 0.05)], axis=1)
        pts.append(branch)
        r.append(np.full(60, 0.0015))
    p = np.concatenate(pts)
    return p, np.concatenate(r), np.full(len(p), 1e-5)


def test_the_retry_raises_the_narrow_passages_and_keeps_the_bore():
    out = drv._narrow_retry(GATED, FACTS, _field(), 0.020, ceiling=8_000_000)
    assert out is not None
    fields, words = out
    assert fields["cells_across_diameter"] == 24, "the bore keeps its cell"
    assert fields["narrow_target"] == pytest.approx(13 * 13 / 10.0, abs=0.01)
    assert fields["narrow_retry"] is True
    assert 1_000_000 <= fields["max_cells"] <= 8_000_000
    assert fields["narrow_budget"] >= 500_000
    assert fields["thin_budget"] == 500_000, "thin features keep the share of the mesh before"
    assert "refined locally" in words and "kept at 24 across" in words


def test_a_narrow_passage_the_budget_held_back_first_gets_its_levels_back():
    # the last pass gave its boxes almost nothing: they were held down, and dropped
    gated = {**GATED, "narrow_budget": 10}
    out = drv._narrow_retry(gated, {**FACTS, "cells_across": 4.0}, _field(), 0.020,
                            ceiling=8_000_000)
    assert out is not None
    # the 3 mm branch at a 0.83 mm wall cell needed 2 levels for 13 across and got none: 4 x 4 =
    # 16 across expected with them, over the 13 asked - the target is not raised on top
    assert out[0]["narrow_target"] == 13.0
    assert out[0]["narrow_budget"] >= 500_000


@pytest.mark.parametrize("gated, facts, field", [
    ({**GATED, "narrow_retry": True}, FACTS, _field()),          # raised locally already: go global
    (GATED, FACTS, _field(narrow=False)),                       # no narrow passage: it is global
    (GATED, {**FACTS, "cells_across": 12.5}, _field()),         # not short at all
    (GATED, FACTS, None),                                       # no reading
    (None, FACTS, _field()),
])
def test_the_retry_goes_global_when_the_shortfall_is_not_local(gated, facts, field):
    assert drv._narrow_retry(gated, facts, field, 0.020, ceiling=8_000_000) is None


# where the last mesh measured short

def test_the_measure_names_where_the_wall_falls_under_the_floor():
    from meshpipeline.engines.passage import under_floor_regions
    x = np.linspace(0.0, 0.3, 300)
    trunk = np.stack([x, np.zeros_like(x), np.full_like(x, 0.010)], axis=1)
    branch = np.stack([np.full(50, 0.1), np.linspace(0.01, 0.04, 50), np.full(50, 0.0015)], axis=1)
    pts = np.concatenate([trunk, branch])
    r = np.concatenate([np.full(300, 0.010), np.full(50, 0.0015)])
    h = np.full(len(pts), 0.0005)                  # trunk 40 across, branch 6 across
    out = under_floor_regions(pts, r, h)
    assert out["points"] == 350
    assert sum(g["points"] for g in out["regions"]) == 50
    for g in out["regions"]:
        assert g["cells"] == pytest.approx(6.0) and g["edge_m"] == pytest.approx(0.0005)
    # boxed tight along the branch, in bins about a passage wide: together they span it
    assert min(g["min"][1] for g in out["regions"]) <= 0.01
    assert max(g["max"][1] for g in out["regions"]) >= 0.04
    assert all(g["max"][0] - g["min"][0] < 0.005 for g in out["regions"])
    assert under_floor_regions(pts, np.full(len(pts), 0.010), h) == {}


def test_a_measured_region_is_refined_from_its_own_wall_cell():
    under = {"points": 1000, "regions": [{"min": [0, 0, 0], "max": [0.01, 0.01, 0.01],
                                           "radius_m": 0.0008, "edge_m": 0.00042, "cells": 3.5,
                                           "points": 200}]}
    boxes, cost, share = drv._gate_boxes(under, wall_cell=0.00084, target=13.0)
    # the wall there was one level past the wall level (0.42 of 0.84 mm); 13 / 3.5 needs two more
    assert boxes[0]["level_bump"] == 3 and boxes[0]["extra"] == 2
    assert share == pytest.approx(0.2) and cost > 0


def test_the_retry_refines_where_the_mesh_measured_short_even_without_a_staged_reading():
    under = {"points": 1000, "regions": [{"min": [0.1, 0.1, 0.1], "max": [0.11, 0.11, 0.11],
                                           "radius_m": 0.002, "edge_m": 0.0004, "cells": 9.0,
                                           "points": 150}]}
    out = drv._narrow_retry(GATED, FACTS, None, 0.020, ceiling=8_000_000, under=under)
    assert out is not None
    assert out[0]["cells_across_diameter"] == 24 and len(out[0]["local_boxes"]) == 1
    assert "measured under the floor" in out[1]
    # most of the wall short: the part is coarse as a whole, refined whole
    most = {**under, "regions": [{**under["regions"][0], "points": 700}]}
    assert drv._narrow_retry(GATED, FACTS, None, 0.020, ceiling=8_000_000, under=most) is None


def test_the_measured_regions_are_read_from_the_attempt_before(tmp_path):
    a1, a2 = tmp_path / "attempt_1", tmp_path / "attempt_2"
    a1.mkdir()
    a2.mkdir()
    uf = {"points": 10, "regions": [{"min": [0, 0, 0], "max": [1, 1, 1], "radius_m": 0.1,
                                     "edge_m": 0.01, "cells": 5.0, "points": 2}]}
    (a1 / "mesh_quality.json").write_text(json.dumps({"cells": 5, "passage_under_floor": uf}))
    assert drv._sibling_under_floor(a2) == uf
    assert drv._sibling_under_floor(a1) is None


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
    from meshpipeline.cad.stl_io import _box_triangles, _write_solid
    w = types.SimpleNamespace(cases=[])
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
        return {"stls": {"wall": _stl("wall", [0, 0, 0], [0.3, 0.3, 0.3]),
                         "inlet": _stl("inlet", [0, 0.14, 0.14], [0.001, 0.16, 0.16]),
                         "outlet": _stl("outlet", [0.299, 0.14, 0.14], [0.3, 0.16, 0.16])},
                "interior_point": [0.15, 0.15, 0.15], "bbox_min": [0, 0, 0],
                "bbox_max": [0.3, 0.3, 0.3],
                "openings": {"inlet": {"area": 3.14e-4, "centroid": [0.0, 0.15, 0.15]},
                             "outlet": {"area": 3.14e-4, "centroid": [0.3, 0.15, 0.15]}}}

    def _run_native(workspace, **_k):
        w.cases.append((Path(workspace) / "system" / "snappyHexMeshDict").read_text())
        return {"rc": 0, "timed_out": False}

    async def _plan(**_kw):
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"), input_tokens=1,
                              output_tokens=1)
        # the model's own re-plan reads the gate's advice and raises the whole part
        return P.PlanOutcome({**GATED, "cells_across_diameter": 32}, rr)

    monkeypatch.setattr(P, "plan_with_accounting", _plan)
    monkeypatch.setattr(R, "tessellate_internal", _tess)
    monkeypatch.setattr(R, "run_snappy", _run_native)
    monkeypatch.setattr(R, "check_mesh", lambda *_a, **_k: {
        "cells": 2_000_000, "fatal": [], "skew_fraction": 0.0, "skew_faces": 0})
    monkeypatch.setattr(R, "_patch_face_counts",
                        lambda *_a, **_k: {"wall": 80_000, "inlet": 900, "outlet": 900})
    monkeypatch.setattr(drv, "_plan_surface", lambda *_a, **_k: types.SimpleNamespace(
        consumed=None))
    monkeypatch.setattr(drv, "read_purpose", lambda *_a, **_k: "internal_cfd")
    monkeypatch.setattr(drv, "_staged_passage_field", lambda *_a, **_k: w.field)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(drv.polcfg, "CELL_HARD_LIMIT", 8_000_000, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})

    def go(field):
        from tests._geometry_support import geometry_state as _geometry_state

        from meshpipeline.contracts.geometry_units import LengthUnit
        w.field = field
        src = tmp_path / "part.step"
        src.write_text("ISO-10303-21;\n")
        state = {"builder_mode": "revise", "engine": "snappy", "request_txt": "r",
                 "intake_patches": [], "dimensionality": "3D", "flow_topology": "internal",
                 "input_kind": "fluid-domain", "purpose": "internal_cfd",
                 "executor_failure_cause": "under_resolved",
                 "executor_failure_facts": dict(FACTS),
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


def test_after_an_under_resolved_mesh_the_narrow_passages_are_raised_and_the_bore_kept(retry):
    (ok, _v, _o), pub = retry.go(_field())
    assert ok is True
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    assert "about 24 cells across the bore" in filling[0], filling
    narrow = [n for n in pub.notes if n.startswith("Narrow passages")]
    assert narrow and "refining them locally to 16.9 across" in narrow[0], narrow
    opened = [n for n in pub.notes if n.startswith("Meshing pass 1 of")]
    assert "refined locally to 16.9 cells across" in opened[0], opened
    assert "the bore kept at 24 across" in opened[0], opened
    plan = json.loads((retry.ws / ".last_plan.json").read_text())
    assert plan["narrow_retry"] is True and plan["cells_across_diameter"] == 24
    assert plan["near_band_m"] == 0.0, "the band of the pass it follows: the retry stays local"
    assert "thinZone" in retry.cases[0]
    assert re.search(r"wall \{ mode distance", retry.cases[0]) is None


def test_the_retry_boxes_where_the_last_mesh_measured_short(retry):
    uf = {"points": 5000, "regions": [{"min": [0.05, 0.05, 0.05], "max": [0.0612, 0.0634, 0.0656],
                                       "radius_m": 0.0015, "edge_m": 0.0004, "cells": 6.0,
                                       "points": 400}]}
    (retry.ws.parent / "attempt_1" / "mesh_quality.json").write_text(
        json.dumps({"cells": 2_000_000, "passage_under_floor": uf}))
    (ok, _v, _o), pub = retry.go(_field(narrow=False))
    assert ok is True
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    assert "about 24 cells across the bore" in filling[0], filling
    opened = [n for n in pub.notes if n.startswith("Meshing pass 1 of")]
    assert "where the last mesh measured under the floor" in opened[0], opened
    assert "max (0.0612 0.0634 0.0656)" in retry.cases[0]
    plan = json.loads((retry.ws / ".last_plan.json").read_text())
    assert plan["local_boxes"][0]["level_bump"] == 3       # 1 at the wall + 2 for 13 / 6


def test_with_no_narrow_passage_the_whole_part_is_refined(retry):
    (ok, _v, _o), pub = retry.go(_field(narrow=False))
    assert ok is True
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    # the re-plan's 32 stands: it already asks for more than ceil(24 x 13 / 10) = 32
    assert "about 32 cells across the bore" in filling[0], filling
    plan = json.loads((retry.ws / ".last_plan.json").read_text())
    assert not plan.get("narrow_retry")
