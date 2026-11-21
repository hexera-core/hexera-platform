"""The observation a model author reads: bounded, JSON-safe, and hermetic without the kernel."""
from __future__ import annotations

import json
import sys

from meshpipeline.cad.repair import observe
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.observe import observation_of
from meshpipeline.cad.repair.regions import Region, decompose
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
                  face_indices=frozenset(range(1, n + 1)))


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


def test_no_kernel_needed_for_located_fields(monkeypatch):
    # Through monkeypatch, so the real `OCP` package comes back afterwards. A bare pop leaves
    # `OCP.ShapeFix` & co. in sys.modules while the next `import OCP` builds a NEW package whose
    # `ShapeFix` attribute is the native `OCP.OCP.ShapeFix`, not that wrapper - so a later test
    # patching `OCP.ShapeFix` patches a module `from OCP.ShapeFix import ...` no longer reads.
    monkeypatch.delitem(sys.modules, "OCP", raising=False)
    calls = []

    def reader(shape, indices):
        calls.append(sorted(indices))
        return _faces(shape, indices)

    obs = observation_of(_region(3), (_defect(0),), (), None, original=ORIGINAL,
                         face_reader=reader)
    assert calls == [[1, 2, 3]]
    assert obs["region"]["bbox_min"] == [0.0, 0.0, 0.0]
    assert obs["faces"][0]["surface_type"] == "GeomAbs_Plane"
    assert "OCP" not in sys.modules


def test_unreadable_faces_do_not_fail_the_observation():
    def broken(shape, indices):
        raise RuntimeError("no kernel")

    obs = observation_of(_region(2), (_defect(0),), (), None, original=ORIGINAL,
                         face_reader=broken)
    assert obs["faces"] == []
    assert obs["faces_status"] == "unavailable: RuntimeError"


def test_ok_status_and_scope_markers():
    obs = observation_of(_region(3), (_defect(0),), (), None, original=ORIGINAL,
                         face_reader=_faces)
    assert obs["faces_status"] == "ok" and obs["defects_scope"] == "region"
    stray = observation_of(_region(3), (_defect(99),), (), None, original=ORIGINAL,
                           face_reader=_faces)
    assert stray["defects_scope"] == "part-wide-fallback"


def test_face_truncation_prefers_shown_defects_and_counts():
    region = Region(index=0, fingerprints=frozenset({"fp0"}), bbox_min=(0, 0, 0),
                    bbox_max=(1, 1, 1), face_indices=frozenset(range(1, 61)))
    obs = observation_of(region, (_defect(55),), (), None, original=ORIGINAL,
                         face_reader=_faces)
    # fp55 is not in the region's fingerprints, so it is a fallback defect; its face still leads.
    assert obs["faces"][0]["index"] == 55
    assert obs["face_count_total"] == 60
    assert obs["faces_omitted"] == 60 - len(obs["faces"])
    assert obs["region"]["face_indices_omitted"] == 20


def test_face_index_zero_is_skipped_and_counted_and_indices_are_one_based():
    found = decompose([_defect(1)], diagonal_mm=100.0)
    assert found[0].face_indices == frozenset({1})  # the kernel's 1-based index, as recorded
    region = Region(index=0, fingerprints=frozenset({"fp1"}), bbox_min=(0, 0, 0),
                    bbox_max=(1, 1, 1), face_indices=frozenset({0, 1}))
    asked = []

    def reader(shape, indices):
        asked.extend(indices)
        return _faces(shape, indices)

    obs = observation_of(region, (_defect(1),), (), None, original=ORIGINAL, face_reader=reader)
    assert asked == [1] and obs["faces_skipped"] == 1


def test_ceiling_holds_with_huge_attempts_and_faces_and_nonfinite_numbers():
    ops = tuple(OpVerdict(op="fix_small_edges", targets=tuple(f"fp{n}" * 20 for n in range(9)),
                          defect_code="c", status="reverted", reason="r" * 900)
                for _ in range(9))
    attempts = tuple(PackageVerdict(region_index=0, status=REVERTED, reason="q" * 900, verdicts=ops)
                     for _ in range(9))

    def fat(shape, indices):
        return {i: {"surface_type": "S" * 500, "area_mm2": float("nan"),
                    "bbox_min": [0, 0, float("inf")], "bbox_max": [1, 1, 1], "neighbours": 1}
                for i in indices}

    obs = observation_of(_region(30), tuple(_defect(i) for i in range(30)), attempts, None,
                         original=ORIGINAL, face_reader=fat)
    text = json.dumps(obs, allow_nan=False)
    assert len(text.encode()) <= observe.BYTES_MAX
    assert obs["attempts_omitted"] >= 5
    assert obs["defects_omitted"] + len(obs["defects"]) == 30


def test_attempt_ops_carry_targets_and_long_reasons():
    attempt = PackageVerdict(region_index=0, status=REVERTED, reason="z" * 390, verdicts=(
        OpVerdict(op="sew_faces", targets=("fpA", "fpB"), defect_code="c", status="reverted"),))
    obs = observation_of(_region(1), (_defect(0),), (attempt,), None, original=ORIGINAL,
                         face_reader=_faces)
    assert obs["attempts"][0]["ops"][0]["targets"] == ["fpA", "fpB"]
    assert obs["attempts"][0]["reason"] == "z" * 390


def test_minimal_last_resort_keeps_markers_and_error_codes(monkeypatch):
    defects = tuple(_defect(i, DefectSeverity.error if i < 3 else DefectSeverity.warning)
                    for i in range(6))
    monkeypatch.setattr(observe, "BYTES_MAX", 380)
    obs = observation_of(_region(6), defects, (), None, original=ORIGINAL, face_reader=_faces)
    assert obs["truncated"] == "minimal"
    assert obs["defects_scope"] == "region" and obs["faces_status"] == "ok"
    assert obs["error_defects_total"] == 3
    listing = obs.get("error_defects") or obs["error_codes"]
    assert listing and "invalid_brep" in json.dumps(listing)
    assert len(json.dumps(obs, allow_nan=False).encode()) <= 380
