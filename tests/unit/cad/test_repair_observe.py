"""The observation a model author reads: bounded, JSON-safe, and hermetic without the kernel."""
from __future__ import annotations

import json
import sys

from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.observe import observation_of
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import REVERTED, OpVerdict, PackageVerdict

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=10, edges=20, vertices=12,
                   diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=5000.0,
                   volume_mm3=20000.0)


def _defect(i: int, severity=DefectSeverity.error) -> EntityDefect:
    return EntityDefect(
        code=DefectCode.invalid_brep, severity=severity, entity_type="face", entity_index=i,
        message="gmsh could not mesh this face " + "x" * 2000,
        measurements={"message": "Surface mesh failed " + "y" * 2000, "stage": "2D",
                      **{f"k{n}": n for n in range(30)}},
        location={"bbox_min": (0.0, 0.0, float(i)), "bbox_max": (1.0, 1.0, float(i) + 1)},
        fingerprint=f"fp{i}")


def _region(n: int) -> Region:
    return Region(index=0, fingerprints=frozenset(f"fp{i}" for i in range(n)),
                  bbox_min=(0.0, 0.0, 0.0), bbox_max=(1.0, 1.0, 30.0),
                  face_indices=frozenset(range(n)))


def _faces(shape, indices):
    return {i: {"surface_type": "GeomAbs_Plane", "area_mm2": 4.0,
                "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "neighbours": 4}
            for i in indices}


def test_observation_is_json_and_bounded():
    defects = tuple(_defect(i, DefectSeverity.warning if i % 2 else DefectSeverity.error)
                    for i in range(30))
    obs = observation_of(_region(30), defects, (), object(), original=ORIGINAL,
                         face_reader=_faces)
    text = json.dumps(obs)
    assert json.loads(text) == obs
    assert len(text.encode()) < 16_000
    codes = [d["severity"] for d in obs["defects"]]
    assert codes == sorted(codes, key=lambda s: s != "error")  # errors kept first
    assert obs["defects"][0]["measurements"]["stage"] == "2D"
    assert obs["defects"][0]["measurements"]["message"].startswith("Surface mesh failed")
    assert obs["defect_count"] == 30
    assert obs["caps"]["max_area_delta_mm2"] > 0
    assert {"max_ops", "timeout_s"} <= set(obs["caps"])


def test_attempt_failures_are_carried():
    attempt = PackageVerdict(
        region_index=0, status=REVERTED, reason="predicate failed: face 3 still unmeshable",
        verdicts=(OpVerdict(op="fix_small_edges", targets=("fp1",), defect_code="invalid_brep",
                            status="reverted", reason="cap: area moved too far"),))
    obs = observation_of(_region(2), (_defect(0),), (attempt,), object(), original=ORIGINAL,
                         face_reader=_faces)
    text = json.dumps(obs)
    assert "cap: area moved too far" in text
    assert "face 3 still unmeshable" in text
    assert obs["attempts"][0]["ops"][0]["status"] == "reverted"


def test_no_kernel_needed_for_located_fields():
    sys.modules.pop("OCP", None)
    calls = []

    def reader(shape, indices):
        calls.append(sorted(indices))
        return _faces(shape, indices)

    obs = observation_of(_region(3), (_defect(0),), (), None, original=ORIGINAL,
                         face_reader=reader)
    assert calls == [[0, 1, 2]]
    assert obs["region"]["bbox_min"] == [0.0, 0.0, 0.0]
    assert obs["faces"][0]["surface_type"] == "GeomAbs_Plane"
    assert "OCP" not in sys.modules


def test_unreadable_faces_do_not_fail_the_observation():
    def broken(shape, indices):
        raise RuntimeError("no kernel")

    obs = observation_of(_region(2), (_defect(0),), (), None, original=ORIGINAL,
                         face_reader=broken)
    assert obs["faces"] == []
