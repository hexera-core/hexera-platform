# Responsibility: Verify the FEA driver end to end on small parts of known geometry: the plan is measured off the staged solid, gmsh meshes it as planned (within its estimate, every planned rule met on the mesh, every face named), the same plan gives the same mesh twice, a plan over its level stops before meshing with its options, a file of two bodies stops, and only a 3D structural solid takes this driver.
# Boundaries: parts are built in the test (tests/fea_fixtures.py) and meshed in-process at a small made-up level, so the suite stays quick; the real levels are the lab's (~/fea4_lab).
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.contracts import decision_record as dr  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter  # noqa: E402

#: A made-up level small enough for a unit test (the plate fixture at ~40k elements).
SMALL = P.Level("draft", "Quick", 40_000)


def _stage(shape, ws: Path) -> Path:
    """Stage a part as the builder does for gmsh: the upload read in mm, written in metres."""
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def _plan(ws: Path, level: P.Level = SMALL, **kw) -> dict:
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.make_plan(record, surface, diag, fidelity="", level=level, **kw)
    FD.write_plan(ws, plan)
    return plan


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    ws = _stage(fx.plate_with_hole(), tmp_path_factory.mktemp("fea_plate"))
    plan = _plan(ws)
    assert fea_adapter.mesh(ws) == 0
    return ws, plan, json.loads((ws / "quality.json").read_text())


def test_the_plan_reads_the_part_in_metres(plate):
    _ws, plan, _q = plate
    hole = next(z for z in plan["zones"] if z["kind"] == "hole")
    assert hole["radius_m"] == pytest.approx(fx.PLATE["hole_radius"] * 1e-3, rel=1e-3)
    wall = min((z for z in plan["zones"] if z["kind"] == "wall"), key=lambda z: z["thickness_m"])
    assert wall["thickness_m"] == pytest.approx(fx.PLATE["thickness"] * 1e-3, rel=1e-3)
    assert plan["fits"] and plan["base_size_m"] <= plan["coarsest_base_m"]


def test_the_mesh_comes_out_near_its_estimate(plate):
    _ws, plan, q = plate
    assert 0.65 * plan["estimate"]["elements"] <= q["cells"] <= 1.35 * plan["estimate"]["elements"]
    assert q["cells"] <= plan["detail"]["ceiling"]


def test_every_planned_rule_is_met_on_the_mesh(plate):
    _ws, _plan_, q = plate
    rules = q["fea"]["rules"]
    assert rules["short"] == []
    assert rules["through_wall"]["min"] >= P.WALL_ELEMENTS * 0.9
    assert q["fea"]["through_wall_min"] == rules["through_wall"]["min"]
    assert q["min_sicn"] >= 0.1 and q["quality_bar"] == "fea"


def test_every_cad_face_is_named_in_the_deck(plate):
    ws, plan, _q = plate
    deck = (ws / "mesh.inp").read_text()
    assert "*ELEMENT, TYPE=C3D10" in deck.upper().replace("TYPE =", "TYPE=")
    for k in range(1, plan["measure"]["faces"] + 1):
        assert f"*SURFACE, NAME=Face_{k}," in deck


def test_the_same_plan_gives_the_same_mesh(plate, tmp_path):
    ws, _plan_, _q = plate
    again = _stage(fx.plate_with_hole(), tmp_path / "again")
    plan2 = _plan(again)
    assert json.loads((ws / P.PLAN_FILE).read_text()) == plan2
    assert fea_adapter.mesh(again) == 0
    digest = [hashlib.sha256((w / "mesh.inp").read_bytes()).hexdigest() for w in (ws, again)]
    assert digest[0] == digest[1]


def test_over_its_level_a_plan_stops_with_options(tmp_path):
    ws = _stage(fx.plate_with_hole(), tmp_path / "tiny")
    plan = _plan(ws, level=P.Level("draft", "Quick", 300))
    assert not plan["fits"]
    assert plan["options"] and plan["options"][0]["option"] == "level_up"


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws: Path, monkeypatch, fidelity="draft"):
    from meshpipeline.engines import runtime

    monkeypatch.setitem(P.LEVELS, "draft", SMALL)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    return asyncio.run(FD.drive(ws, {"effective_mesh_fidelity": fidelity}, job_id=f"t-{ws.name}",
                                publish=None, run=_Run()))


def test_the_driver_meshes_and_cites_every_rule(tmp_path, monkeypatch):
    ws = _stage(fx.bracket(fillet=None), tmp_path / "bracket")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    cited = {e["rule"] for e in dr.cited_for(f"t-{ws.name}")}
    assert {"PLAN-fea-9", "PLAN-fea-1", "PLAN-fea-2", "PLAN-fea-5", "PLAN-fea-8", "LAD-fea-5"} <= cited
    passes = json.loads((ws / "fea_passes.json").read_text())
    assert passes["delivered"] == 1 and passes["levers"] == []


def test_two_bodies_stop_before_meshing(tmp_path, monkeypatch):
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.two_blocks(), tmp_path / "two")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert not ok and marker == FD.NOT_ONE_PART
    ref = read_refusal(ws)
    assert ref.gate == "fea_plan" and "2 separate bodies" in ref.facts["reason"]


@pytest.mark.parametrize("state, takes", [
    ({"purpose": "structural", "dimensionality": "3D", "intake_patches": []}, True),
    ({"purpose": "structural"}, True),
    ({"purpose": "structural", "dimensionality": "2D"}, False),
    ({"purpose": "structural", "intake_patches": [{"name": "fixed end", "type": "fixed"}]}, False),
    ({"purpose": "internal_cfd", "flow_topology": "internal"}, False),
    ({"purpose": "external_cfd"}, False),
])
def test_only_a_3d_structural_solid_takes_the_fea_driver(state, takes):
    from meshpipeline.engines.registry import get_spec

    driver = get_spec("gmsh").build_driver_for(state)
    assert (driver is FD.drive) is takes
    if not takes:
        assert driver is None            # the builder loop, as before


def test_no_other_engine_gains_a_driver():
    from meshpipeline.engines.registry import ENGINE_CATALOG

    state = {"purpose": "structural"}
    for name, spec in ENGINE_CATALOG.items():
        if name != "gmsh":
            assert spec.build_driver_for(state) is spec.build_driver


def test_a_guard_trip_pulls_no_lever_and_ends_the_attempt(tmp_path, monkeypatch):
    """The clock only guards (D3): a run past its guard is our infrastructure, never a reason to
    change the plan - no lever is pulled and nothing is delivered from it."""
    from meshpipeline.engines import runtime

    ws = _stage(fx.plate_with_hole(), tmp_path / "trip")
    monkeypatch.setitem(P.LEVELS, "draft", SMALL)
    calls = []
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": -1, "timed_out": True}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {"effective_mesh_fidelity": "draft"}, job_id="t-trip",
                                          publish=None, run=_Run()))
    assert not ok and marker == FD.GUARD_TRIP and len(calls) == 1
    rec = json.loads((ws / "fea_passes.json").read_text())
    assert rec["levers"] == [] and rec["guard_trip"]["unit"] == "elements"
    assert not any(e["rule"].startswith("LAD-fea-") and e["decided"] == "lever" for e in dr.cited_for("t-trip"))
    from meshpipeline.engines.preflight import read_refusal

    ref = read_refusal(ws)
    assert ref.gate == FD.GUARD_GATE and ref.cause == "run_infrastructure" and ref.facts["ours"]
