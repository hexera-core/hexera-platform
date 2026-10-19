# Responsibility: Verify the face-level self-intersection scan against the real kernel - with no budget every fixture scans exactly as it did before the scan existed, a timeout is silence, and the analyzer's record of a crossing face is the one that survives.
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from tests.cad_fixtures import (
    write_cross_drilled_cylinder_step,
    write_cylinder_cap_hole_step,
    write_loose_faces_step,
    write_missing_bore_wall_step,
    write_mixed_assembly_step,
    write_open_shell_step,
    write_small_edge_step,
)

from meshpipeline.cad.repair import localize
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect, localize_scan

pytest.importorskip("OCP.BOPAlgo", reason="the CAD kernel is a native-tier dependency")

FIXTURES = (write_open_shell_step, write_missing_bore_wall_step, write_small_edge_step,
            write_loose_faces_step, write_mixed_assembly_step, write_cylinder_cap_hole_step,
            write_cross_drilled_cylinder_step)

#: Captured from the code BEFORE the face scan existed (`[d.to_dict() for d in
#: localize_scan(shape)[0]]` per fixture). The proof that the flag-off path is unchanged.
BASELINE = json.loads((Path(__file__).parent / "_face_scan_baseline.json").read_text())


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_flag_off_scans_are_identical_to_todays(tmp_path):   # every committed repair fixture
    for write in FIXTURES:
        shape = _read(write(tmp_path / f"{write.__name__}.step"))
        assert localize_scan(shape) == localize_scan(shape, face_scan_s=0.0)
        assert [d.to_dict() for d in localize_scan(shape)[0]] == BASELINE[write.__name__]


def test_a_timed_out_face_scan_keeps_the_status_walks_completeness(tmp_path, monkeypatch):
    shape = _read(write_open_shell_step(tmp_path / "o.step"))
    monkeypatch.setattr(localize, "_kernel_face_pairs", lambda s: time.sleep(1) or [(1, 2)])
    found, complete = localize_scan(shape, face_scan_s=0.05)
    assert complete is True and not [d for d in found if d.kernel_status == "SelfInterference"]


def test_the_analyzer_record_wins_dedupe_over_a_status_walk_record(tmp_path, monkeypatch):
    shape = _read(write_open_shell_step(tmp_path / "o.step"))
    monkeypatch.setattr(localize, "_kernel_face_pairs", lambda s: [(1, 2)])
    walk = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                        entity_type="face", entity_index=1, message="walk",
                        kernel_status="SelfIntersectingWire")
    monkeypatch.setattr(localize, "_status_defects", lambda s, **kw: ([walk], True))
    found, _ = localize_scan(shape, valid=False, face_scan_s=5.0)
    survivor = next(d for d in found if d.entity_type == "face" and d.entity_index == 1
                    and d.code is DefectCode.self_intersection)
    assert survivor.measurements["crossing_faces"]


def test_the_real_analyzer_names_crossing_faces_and_none_on_a_sound_box():
    # The kernel half the other tests stub out: two overlapping boxes in one compound cross face
    # to face, and each crossing face is linked to its partners by fingerprint.
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    assert localize._kernel_face_pairs(box) == []

    both = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(both)
    builder.Add(both, box)
    builder.Add(both, BRepPrimAPI_MakeBox(gp_Pnt(5.0, 5.0, 5.0), 10.0, 10.0, 10.0).Shape())
    found, _ = localize_scan(both, face_scan_s=20.0)
    crossing = [d for d in found if d.kernel_status == "SelfInterference"]
    assert crossing
    names = {d.fingerprint for d in crossing}
    for defect in crossing:
        assert defect.measurements["crossing_faces"]
        assert set(defect.measurements["crossing_faces"]) <= names - {defect.fingerprint}
