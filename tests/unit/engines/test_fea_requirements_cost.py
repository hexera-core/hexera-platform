# Responsibility: Verify fea_plan_options.requirements_cost - what the user's typed requirements do to an FEA plan, said BEFORE the run: the elements after them, the machine and the time, read off the run's own sizing (fea_driver.requirement_sizes) and planner (choose_plan), never a number of its own.
# Boundaries: a small filleted bracket built in the test (tests/fea_fixtures.py), measured and planned in-process; no model, no mesher. The big-plan words use plans given as data.
from __future__ import annotations

import pytest

pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.contracts import mesh_machine as MM  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan_options as PO  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402


@pytest.fixture(scope="module")
def bracket(tmp_path_factory):
    import gmsh

    ws = tmp_path_factory.mktemp("bracket")
    src = fx.write_step(fx.bracket(4.0), ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    record, surface, diag = FD.measure_staged(ws)
    return record, surface, diag, FD.choose_plan(record, surface, diag)


def _global(mm: float) -> dict:
    req = MR.empty()
    req["sizes"]["global_mm"] = mm
    return MR.section("used", requirements=req)


def _fillets_finer(record: dict) -> dict:
    spot = next(h for h in record.get("hot_spots") or [] if h.get("type") == "fillet")
    faces = list(spot["faces"])
    return {"status": "used", "features": {"Feature_1": faces},
            "parts": [{"id": "Feature_1", "feature": True, "name": "1 rounded edge (fillet)"}],
            "entries": [{"ask": "refine", "parts": ["Feature_1"], "quote": "finer at the fillets"}]}


def test_finer_at_the_fillets_is_costed_as_the_run_would_plan_it(bracket):
    record, surface, diag, before = bracket
    asked, parts = _global(3.0), _fillets_finer(record)
    got = PO.requirements_cost(record, surface, diag, before=before, asked=asked, part_sizes=parts)
    # the run's own sizing and planner, called the way drive calls them: the same estimate to the element
    sizes, _scaled, _controls = FD.requirement_sizes(record, asked, parts, diag)
    run_plan = FD.choose_plan(record, surface, diag, sizes=sizes)
    assert got["after"]["elements"] == int(run_plan["estimate"]["elements"])
    assert got["before"]["elements"] == int(before["estimate"]["elements"])
    assert got["after"]["elements"] > got["before"]["elements"]
    assert got["factor"] == round(got["after"]["elements"] / got["before"]["elements"], 2)
    for r in (got["before"], got["after"]):
        assert r["where"] in ("cloud_run", "gce") and r["minutes"] >= 1 and r["budget_minutes"] >= 60   # LAD-fea-15's floor, an hour
    assert got["words"].startswith("That takes the mesh from about ")
    assert "on the standard machine" in got["words"] and "minutes" in got["words"]


def test_nothing_asked_keeps_the_plan(bracket):
    record, surface, diag, before = bracket
    got = PO.requirements_cost(record, surface, diag, before=before, asked=MR.section("none"), part_sizes=None)
    assert got["after"]["elements"] == got["before"]["elements"] and got["factor"] == 1.0
    assert got["words"].startswith("That keeps the mesh at about ") and not got["bigger_machine"]


def test_a_tenfold_plan_says_the_bigger_machine_and_the_time(monkeypatch):
    # the cast knuckle, 25 Nov: about 1 million elements by the rules, 11.7 million planned after "finer at the
    # fillets" - on a 128 GB machine. The words come from the run's own memory model, machine list and rates
    record = {"faces": {f"Face_{i}": {} for i in range(300)}, "hot_spots": []}       # a complex part
    before = {"estimate": {"elements": 1_000_000}, "zones": []}
    after = {"estimate": {"elements": 11_700_000}, "zones": []}
    monkeypatch.setattr(FD, "choose_plan", lambda *a, **k: after)
    got = PO.requirements_cost(record, None, 0.43, before=before, asked=MR.section("none"), part_sizes=None)
    process, files = FD.memory_need(11_700_000)
    machine = MM.choose(process_gib=process, workspace_gib=files)
    assert got["after"]["machine"] == machine["machine"] and got["bigger_machine"]
    assert got["after"]["minutes"] == 195                     # 11.7M at the complex class's 1,000 a second
    assert got["after"]["budget_minutes"] == 975              # LAD-fea-15: five times that (the run's 58,542 s)
    assert got["words"] == ("That takes the mesh from about 1.0 million to about 12 million elements, on a larger "
                            f"machine ({machine['memory_gib']:g} GB), usually about 1.7 hours, up to 3.2 hours.")
