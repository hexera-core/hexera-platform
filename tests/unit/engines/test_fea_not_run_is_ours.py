# Responsibility: Pin RUN-4's "a pass our side never ran is ours": a mesh whose machine could not be started (a refused permission, a quota) or whose result never came back pulls no lever and is never told as the part's geometry - the attempt ends at once as our failure, said as that.
# Boundaries: the real FEA driver on a small test part, the mesher faked to answer as the mesh executor does when its dispatch fails; plus the user's sentence for that outcome.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.contracts import decision_record as dr  # noqa: E402
from meshpipeline.contracts.failure_cause import describe  # noqa: E402
from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE, RUN_NOT_STARTED_TAG  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.preflight import read_refusal  # noqa: E402

# THE DEFECT (job bf863215, 26 Oct): every pass of an 11-part assembly went to the big-machine route and
# came back "[CLOUD_RUN_FAILED] gmsh: HTTPError: 403 Client Error: Forbidden for url: .../jobs/dev-mesh".
# The ladder read that as "no mesh came back from the fill", pulled LAD-fea-2, tied the parts, ran the
# fTetWild rung - all refused the same way - and told the user two faces of the CAD lay on top of each
# other: "Remove or thicken that detail in your CAD".
FORBIDDEN = {"rc": RC_INFRASTRUCTURE, "timed_out": False,
             "log_tail": (f"{RUN_NOT_STARTED_TAG} gmsh: HTTPError: 403 Client Error: Forbidden for url: "
                          "https://run.googleapis.com/v2/projects/p/locations/r/jobs/dev-mesh")}


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _stage(shape, ws: Path) -> Path:
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


def _drive(ws: Path, monkeypatch, answer: dict, job: str):
    from meshpipeline.engines import runtime

    calls = []
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or dict(answer)))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id=job, publish=None, run=_Run()))
    return ok, marker, calls


def test_a_pass_refused_by_our_platform_pulls_no_lever_and_is_ours(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "forbidden")
    ok, marker, calls = _drive(ws, monkeypatch, FORBIDDEN, "t-forbidden")
    assert not ok and marker == FD.NOT_RUN
    assert len(calls) == 1, "no second pass, no tie, no last rung: none of them would run either"
    rec = json.loads((ws / "fea_passes.json").read_text())
    assert rec["levers"] == [] and rec["not_run"]["pass"] == 1 and "403" in rec["not_run"]["error"]
    assert not any(e["decided"] == "lever" for e in dr.cited_for("t-forbidden"))
    assert any(e["rule"] == "RUN-4" and e["decided"] == "not run" for e in dr.cited_for("t-forbidden"))
    ref = read_refusal(ws)
    assert ref.gate == FD.NOT_RUN_GATE and ref.cause == "run_infrastructure"
    assert ref.facts["ours"] and not ref.facts["deterministic"]
    from meshpipeline.contracts.failure_cause import retry_can_help
    assert retry_can_help(ref.cause, ref.facts), "ours: the run is taken again"


def test_the_user_is_told_it_is_ours_never_the_cads():
    what, nxt = describe("run_infrastructure", {"not_run": {"pass": 1, "where": "gce", "error": FORBIDDEN["log_tail"]}})
    assert "larger machine" in what and "refused permission" in what and "never ran" in what
    assert "our failure" in what and "CAD" not in what + nxt and "thicken" not in what + nxt
    what, _ = describe("run_infrastructure", {"not_run": {"pass": 2, "where": "cloud_run",
                                                          "error": "[CLOUD_RUN_FAILED] gmsh: ConnectionError"}})
    assert "service that runs the mesher" in what and "permission" not in what


@pytest.mark.parametrize("result, ours", [
    (FORBIDDEN, True),
    ({"rc": RC_INFRASTRUCTURE, "timed_out": False, "log_tail": "[CLOUD_RUN_RESULT_UNCOLLECTED] gmsh: ..."}, True),
    ({"rc": -1, "timed_out": True}, False),           # ran out of time: the guard's, as before
    ({"rc": 1, "log_tail": "Error : No elements in volume"}, False),
    ({"rc": 0}, False),
])
def test_only_our_own_failures_are_read_as_not_run(result, ours):
    assert FD.not_run(result) is ours
