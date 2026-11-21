# Responsibility: Verify that a kept repair the final whole-part mesh probe still refuses ends
#                 `unmeshable`, never `repaired` - and that a probe that never answered (skipped,
#                 timed out, dead) is not counted as a mesh failure.
# Boundaries: hermetic - the loop, the measurement and the STEP write are stubbed.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import definitive_mesh_failure


def _defect(code: DefectCode, severity=DefectSeverity.error, index: int = 1) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type="face", entity_index=index,
                        message=code.value)


OPEN_SHELL = _defect(DefectCode.open_shell)
EDGE_THROUGH = _defect(DefectCode.mesh_edge_through_face, index=23)
TIMEOUT = _defect(DefectCode.mesh_timeout)
PROBE_ERROR = _defect(DefectCode.mesh_probe_error)


def test_a_mesher_answer_beside_geometry_errors_is_a_definitive_failure():
    # The 0051_00513292 shape: open_shell left AND gmsh named the clash.
    assert definitive_mesh_failure((OPEN_SHELL, EDGE_THROUGH)) is True
    assert definitive_mesh_failure((EDGE_THROUGH,)) is True


@pytest.mark.parametrize("defects", [
    (),
    (OPEN_SHELL,),
    (TIMEOUT,),
    (PROBE_ERROR,),
    # A TIMED-OUT final probe carries the session's earlier findings forward
    # (session._with_mesh): those are stale, so they are not a final answer.
    (EDGE_THROUGH, TIMEOUT),
    (OPEN_SHELL, EDGE_THROUGH, PROBE_ERROR),
])
def test_no_answer_from_the_final_probe_is_not_a_mesh_failure(defects):
    assert definitive_mesh_failure(defects) is False


def _run_conservative(monkeypatch, tmp_path, defects):
    import meshpipeline.cad.repair.caps as caps
    import meshpipeline.cad.repair.conservative as conservative
    import meshpipeline.cad.repair.loop as loop
    from meshpipeline.cad.repair.caps import Measure
    from meshpipeline.cad.repair.session import KEPT, OpVerdict

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "off")
    monkeypatch.setattr(conservative, "_occ", lambda: None)
    monkeypatch.setattr("meshpipeline.cad.repair.brep._read_shape", lambda path: "shape")
    monkeypatch.setattr(caps, "measure", lambda shape: Measure(
        valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40, diagonal_mm=200.0,
        max_tolerance_mm=0.01, surface_area_mm2=1000.0, volume_mm3=8000.0))
    monkeypatch.setattr(conservative, "_write_step", lambda shape, dest, **_: None)
    kept = OpVerdict(op="fix_small_edges", targets=("edge:1",), defect_code="small_edge",
                     status=KEPT)

    def fake_run(shape, **_):
        return loop.RepairRun(shape=shape, verdicts=(kept,), refusals=(), defects=tuple(defects),
                              stopped_because="stalled", complete_scan=True)
    monkeypatch.setattr(loop, "run", fake_run)
    report = conservative.repair_step_file(tmp_path / "in.step", tmp_path / "out.step")
    return {m.name: m.value for m in report.measurements}["ending"]


def test_a_kept_repair_the_final_probe_still_refuses_is_not_repaired(monkeypatch, tmp_path):
    assert _run_conservative(monkeypatch, tmp_path, (OPEN_SHELL, EDGE_THROUGH)) == "unmeshable"


def test_a_kept_repair_whose_final_probe_timed_out_stays_repaired(monkeypatch, tmp_path):
    assert _run_conservative(monkeypatch, tmp_path,
                             (OPEN_SHELL, EDGE_THROUGH, TIMEOUT)) == "repaired"


def test_a_kept_repair_with_the_mesh_probe_off_stays_repaired(monkeypatch, tmp_path):
    # Probe off: no mesh code can be on the list, so `repaired` keeps its old meaning.
    assert _run_conservative(monkeypatch, tmp_path, (OPEN_SHELL,)) == "repaired"
