# Responsibility: Verify that the Netgen CFD engine (#399) and the CFD wrap rung (#386, LAD-cfd-5) read the user's requirements through the one seam (engines/cfd_requirements, CFDREQ-1..7): each field applied, the nearest, or not applied and told; nothing said = the plan exactly as the rules make it.
# Boundaries: pure over plain data and temporary workspaces; no model, no mesher is run.
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.contracts import user_notes as un
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines.netgen import plan as P


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return json.dumps(base)


def _read(notes: str, **fields):
    req, _d, _x = MR.check(_answer(**fields), notes, route="flow")
    return req


def _tube(r: float = 0.01, n: int = 400):
    th = np.linspace(0, 2 * np.pi, 20, endpoint=False)
    z = np.linspace(0, 0.2, n // 20)
    pts = np.array([[r * np.cos(a), r * np.sin(a), zz] for zz in z for a in th])
    faces = []
    for i in range(len(z) - 1):
        for j in range(20):
            a, b = i * 20 + j, i * 20 + (j + 1) % 20
            faces += [[a, b, a + 20], [b, b + 20, a + 20]]
    return pts, np.array(faces), np.full(len(pts), r)


def _plan(**kw):
    pts, faces, rad = _tube()
    p = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02], volume=6.3e-5,
                    wall_area=1.26e-2, walls=["wall"], cell_limit=8_000_000, **{"layers": 5, **kw})
    p["sizes"] = p["sizes"].tolist()
    return p


def test_netgen_is_an_engine_of_the_seam_and_reads_the_notes():
    assert "netgen" in CR.ENGINES and "mesh_requirements" in un.ENGINE_CONSUMERS["netgen"]


def test_nothing_asked_the_netgen_plan_is_exactly_the_rules_own():
    assert _plan() == _plan(size_lo=None, size_hi=None, soft_budget=None, user_budget=False, target_cells=None,
                            first_layer=None, layer_stack=None, growth=P.GROWTH)


def test_netgen_takes_the_sizes_as_clamps_and_says_so():
    req = _read("cells 0.4 mm at the wall, largest 0.5 mm, growth 1.1",
                sizes=[{"what": "near_wall", "value": 0.4, "unit": "mm", "quote": "cells 0.4 mm at the wall"},
                       {"what": "max_edge", "value": 0.5, "unit": "mm", "quote": "largest 0.5 mm"},
                       {"what": "growth", "value": 1.1, "unit": "none", "quote": "growth 1.1"}])
    ask = CR.Ask(req, "netgen")
    kw = CR.netgen_inputs(ask, layers=5, port_diameters=[0.02], soft_budget=P.SOFT_BUDGET, cell_limit=8_000_000)
    assert kw["size_lo"] == pytest.approx(4e-4) and kw["size_hi"] == pytest.approx(4e-4)
    plan = _plan(**{k: v for k, v in kw.items() if k != "layers"})
    assert max(plan["sizes"]) == pytest.approx(4e-4 / P.EDGE_PER_H)
    assert ask.rows[ask.rid("near_wall")]["status"] == "applied"
    assert ask.rows[ask.rid("max_edge")]["status"] == "applied"
    assert ask.rows[ask.rid("growth")]["status"] == "not_applied"


def test_netgen_takes_the_layers_and_a_cell_count():
    req = _read("8 layers, first layer 0.02 mm, growth 1.15, about 6 million cells",
                layers=[{"what": "count", "value": 8, "unit": "none", "quote": "8 layers"},
                        {"what": "first_height", "value": 0.02, "unit": "mm", "quote": "first layer 0.02 mm"},
                        {"what": "growth", "value": 1.15, "unit": "none", "quote": "growth 1.15"}],
                counts=[{"what": "target", "value": 6_000_000, "quote": "about 6 million cells"}])
    ask = CR.Ask(req, "netgen")
    kw = CR.netgen_inputs(ask, layers=5, port_diameters=[0.02], soft_budget=P.SOFT_BUDGET, cell_limit=8_000_000)
    assert kw["layers"] == 8 and kw["first_layer"] == pytest.approx(2e-5) and kw["growth"] == 1.15
    assert kw["soft_budget"] == 6_000_000 and kw["target_cells"] == 6_000_000 and kw["user_budget"]
    base = _plan()
    plan = _plan(**kw)
    assert plan["layers"] == 8 and plan["first_layer_m"] == pytest.approx(2e-5)
    assert plan["rungs"][0]["growth"] == 1.15 and plan["rungs"][1]["first_layer"] == pytest.approx(1e-5)
    assert plan["estimated_cells"] > base["estimated_cells"] and plan["cells_across"] > base["cells_across"]
    for f in ("layers:count", "layers:first_height", "layers:growth", "cells:target"):
        assert ask.rows[ask.rid(f)]["status"] == "applied", f


def test_a_wall_size_and_a_count_that_do_not_fit_keep_the_size_and_say_so():
    req = _read("wall cells 0.2 mm, at most 100000 cells",
                sizes=[{"what": "near_wall", "value": 0.2, "unit": "mm", "quote": "wall cells 0.2 mm"}],
                counts=[{"what": "ceiling", "value": 100_000, "quote": "at most 100000 cells"}])
    ask = CR.Ask(req, "netgen")
    kw = CR.netgen_inputs(ask, layers=5, port_diameters=[0.02], soft_budget=P.SOFT_BUDGET, cell_limit=8_000_000)
    plan = _plan(**kw)
    assert plan["sizes_held"] is True and max(plan["sizes"]) == pytest.approx(2e-4 / P.EDGE_PER_H)
    assert not any("coarsens" in n or "lowers" in n for n in plan["notes"])      # no plan note that is not so
    CR.netgen_planned(ask, plan)
    row = ask.rows[ask.rid("cells:ceiling")]
    assert row["status"] == "nearest" and "follows your sizes" in row["built"]
    assert ask.rows[ask.rid("near_wall")]["status"] == "applied"


def test_a_mesh_built_on_a_thinner_rung_is_told_on_the_layer_rows(tmp_path):
    req = _read("6 layers", layers=[{"what": "count", "value": 6, "unit": "none", "quote": "6 layers"}])
    MR.write(tmp_path, MR.section("used", requirements=req))
    ask = CR.Ask(req, "netgen")
    CR.netgen_inputs(ask, layers=5, port_diameters=[0.02], soft_budget=P.SOFT_BUDGET, cell_limit=8_000_000)
    ask.write(tmp_path)
    CR.netgen_built(tmp_path, {"rung": 1, "layers": 6})
    rows = json.loads((tmp_path / CR.PLAN_FILE).read_text())["rows"]
    assert rows[ask.rid("layers:count")]["status"] == "applied"
    CR.netgen_built(tmp_path, {"rung": 3, "layers": 0})
    rows = json.loads((tmp_path / CR.PLAN_FILE).read_text())["rows"]
    assert rows[ask.rid("layers:count")]["status"] == "nearest"
    assert "no layers could be built" in rows[ask.rid("layers:count")]["built"]


def _on_wrap(ws, engine):
    (ws / "cfd_wrap.json").write_text(json.dumps({"flow": "internal", "engine": engine, "rule": "LAD-cfd-5"}))
    (ws / "cfd_wrap_report.json").write_text(json.dumps({"max_on_cad_m": 0.00012, "max_m": 0.0003}))


def test_on_the_wrap_rung_every_size_row_says_it_was_applied_to_the_wrap(tmp_path):
    from meshpipeline.cad.surface_wrap import WRAP_REPORT_FILE, WRAP_REQUEST_FILE
    assert (WRAP_REQUEST_FILE, WRAP_REPORT_FILE) == ("cfd_wrap.json", "cfd_wrap_report.json")
    req = _read("cells 2 mm, 5 layers", sizes=[{"what": "global", "value": 2, "unit": "mm", "quote": "cells 2 mm"}],
                layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 layers"}])
    _on_wrap(tmp_path, "snappy")
    ask = CR.Ask(req, "snappy")
    ask.done("global", "base cells of 2 mm")
    ask.done("layers:count", "5 layers")
    ask.write(tmp_path)
    rows = json.loads((tmp_path / CR.PLAN_FILE).read_text())["rows"]
    assert "on the wrapped surface, which lies within 0.12 mm of your CAD" in rows[ask.rid("global")]["note"]
    assert rows[ask.rid("layers:count")]["status"] == "applied"


def test_on_the_wraps_last_step_no_layers_and_the_wall_keeps_the_wraps_triangles(tmp_path):
    req = _read("cells 1 mm at the wall, 5 layers",
                sizes=[{"what": "near_wall", "value": 1, "unit": "mm", "quote": "cells 1 mm at the wall"}],
                layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 layers"}])
    _on_wrap(tmp_path, "gmsh")
    ask = CR.Ask(req, "gmsh")
    CR.gmsh_sizes(ask, 0.01, external=False)
    ask.write(tmp_path)
    rows = json.loads((tmp_path / CR.PLAN_FILE).read_text())["rows"]
    assert rows[ask.rid("near_wall")]["status"] == "nearest"
    assert "own triangles" in rows[ask.rid("near_wall")]["built"]
    assert "builds no wall layers" in rows[ask.rid("layers:count")]["built"]


def test_off_the_wrap_rung_the_record_is_what_it_was(tmp_path):
    req = _read("cells 2 mm", sizes=[{"what": "global", "value": 2, "unit": "mm", "quote": "cells 2 mm"}])
    ask = CR.Ask(req, "snappy")
    ask.done("global", "base cells of 2 mm")
    ask.write(tmp_path)
    assert json.loads((tmp_path / CR.PLAN_FILE).read_text())["rows"] == {
        ask.rid("global"): {"status": "applied", "built": "base cells of 2 mm"}}
