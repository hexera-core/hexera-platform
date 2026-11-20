# Responsibility: Verify a simplification cannot run unless a deployment opted in, never removes a slot end or a feature at the load case's ends, writes only what the simulation check accepted to a NEW file, and refuses rather than keeping anything else.
# Boundaries: OpenCASCADE builds and defeatures real plates; the simulation check is a fake verifier (the real one runs in tests/native/test_native_simplify_verify.py).
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP")

import meshpipeline.settings.cad_simplify as scfg  # noqa: E402
from meshpipeline.cad.simplify import verify as V  # noqa: E402
from meshpipeline.cad.simplify.contracts import FeatureCandidate, FeatureKind  # noqa: E402
from meshpipeline.cad.simplify.simplify import (  # noqa: E402
    SimplifyRefused,
    SimplifyUnavailable,
    eligible,
    simplify_step_file,
)


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_ENABLED", True)


def _write_plate(path: Path, holes=((80.0, 30.0, 4.0), (120.0, 30.0, 2.0))) -> Path:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(200, 60, 10)).Shape()
    for x, y, r in holes:
        plate = BRepAlgoAPI_Cut(plate, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)),
                                                                 r, 12.0).Shape()).Shape()
    from OCP.Interface import Interface_Static
    w = STEPControl_Writer()
    # write.step.unit is PROCESS-GLOBAL (cad/repair/conservative._write_step): another test may
    # have left it at inches, so it is stated here, after the writer registers it
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(plate, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _holes_in(path: Path) -> list[float]:
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.simplify.recognise import recognise
    return sorted(round(c.radius, 6) for c in recognise(_read_shape(path))[0] if c.kind is FeatureKind.hole)


def _verifier(accept, seen=None):
    """A check that accepts the features whose radius is in `accept`, each at a small estimate."""
    def verify(original, simplified, features, *, budget, margin, deadline, max_tets):
        if seen is not None:
            seen.append({"features": [f.id for f in features], "budget": budget, "margin": margin,
                         "simplified_volume": V.volume(simplified), "original_volume": V.volume(original)})
        est = tuple(V.FeatureEstimate(f.id, True, estimate=-1e-5, cut_mismatch=0.0) for f in features)
        ok = {f.id: round(f.radius, 6) in accept for f in features}
        verdicts = tuple(V.FeatureVerdict(f.id, ok[f.id], "accepted" if ok[f.id] else "over_screen", -1e-5)
                         for f in features)
        n = sum(v.accepted for v in verdicts)
        return V.PackageVerdict(verdicts, est, budget, margin, spent=1e-5 * n, estimated_change=-1e-5 * n)
    return verify


# THE KILL SWITCH


def test_switched_off_it_refuses_before_any_kernel_call(tmp_path, monkeypatch):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_ENABLED", False)

    def _no_kernel(*a, **k):
        raise AssertionError("the kernel was reached with simplification off")

    monkeypatch.setattr("meshpipeline.cad.repair.brep._read_shape", _no_kernel)
    with pytest.raises(SimplifyUnavailable, match="CAD_SIMPLIFY_ENABLED"):
        simplify_step_file(tmp_path / "in.step", tmp_path / "out.step", verifier=_verifier({4.0}))


# WHAT IT KEEPS


def test_the_accepted_features_are_removed_into_a_new_file_and_the_input_is_untouched(tmp_path):
    src = _write_plate(tmp_path / "part.step")
    before = src.read_bytes()
    seen: list = []

    report = simplify_step_file(src, tmp_path / "part-simplified.step", verifier=_verifier({4.0, 2.0}, seen))

    assert src.read_bytes() == before
    assert _holes_in(tmp_path / "part-simplified.step") == []
    assert sorted(round(c.radius, 6) for c in report.candidates) == [2.0, 4.0]
    # ONE check, of the part with every chosen feature removed, against the approved budget
    assert len(seen) == 1 and len(seen[0]["features"]) == 2
    assert seen[0]["budget"] == 0.001 and seen[0]["margin"] == 0.5
    assert seen[0]["simplified_volume"] > seen[0]["original_volume"]
    assert {op["status"] for op in report.operations} == {"kept"}
    assert report.diagnostics["t0"]["valid"] and report.diagnostics["t0"]["closed"]


def test_only_what_the_check_accepted_is_kept_rebuilt_from_the_original(tmp_path):
    src = _write_plate(tmp_path / "part.step")

    report = simplify_step_file(src, tmp_path / "out.step", verifier=_verifier({2.0}))

    # the 4 mm hole was rejected, so the written part still has it - and only it
    assert _holes_in(tmp_path / "out.step") == [4.0]
    assert [round(c.radius, 6) for c in report.candidates] == [2.0]
    statuses = {round(op["radius"], 6): (op["status"], op["reason"]) for op in report.operations}
    assert statuses == {4.0: ("rejected", "over_screen"), 2.0: ("kept", "accepted")}


def test_nothing_accepted_is_refused_with_every_verdict_and_no_file(tmp_path):
    src = _write_plate(tmp_path / "part.step")
    with pytest.raises(SimplifyRefused) as caught:
        simplify_step_file(src, tmp_path / "out.step", verifier=_verifier(set()))
    assert not (tmp_path / "out.step").exists()
    ops = caught.value.measurements["operations"]
    assert {op["status"] for op in ops} == {"rejected"}
    assert caught.value.measurements["verification"]["accepted"] == []


def test_a_feature_at_the_load_cases_end_is_never_removed(tmp_path):
    # a hole 9 mm from the end: inside the 6 mm trimmed band plus its own zone
    src = _write_plate(tmp_path / "part.step", holes=((9.0, 30.0, 2.0), (100.0, 30.0, 3.0)))
    report = simplify_step_file(src, tmp_path / "out.step", verifier=_verifier({2.0, 3.0}))
    assert _holes_in(tmp_path / "out.step") == [2.0]
    skipped = [op for op in report.operations if op["status"] == "skipped"]
    assert [op["reason"] for op in skipped] == ["near_boundary_condition"]


def test_a_part_with_nothing_eligible_is_refused_unsolved(tmp_path):
    src = _write_plate(tmp_path / "part.step", holes=((9.0, 30.0, 2.0),))
    seen: list = []
    with pytest.raises(SimplifyRefused, match="no candidate"):
        simplify_step_file(src, tmp_path / "out.step", verifier=_verifier({2.0}, seen))
    assert seen == []


def test_a_package_that_moves_too_much_material_is_refused_before_it_is_solved(tmp_path, monkeypatch):
    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_MAX_VOLUME_CHANGE", 1e-6)
    src = _write_plate(tmp_path / "part.step")
    seen: list = []
    with pytest.raises(SimplifyRefused, match="volume") as caught:
        simplify_step_file(src, tmp_path / "out.step", verifier=_verifier({4.0, 2.0}, seen))
    assert seen == [] and caught.value.measurements["t0"]["volume_change"] > 0


def test_a_check_that_cannot_run_here_is_unavailable_not_a_refusal(tmp_path):
    src = _write_plate(tmp_path / "part.step")

    def _unavailable(*a, **k):
        raise V.VerifyUnavailable("CalculiX (ccx) is not installed")

    with pytest.raises(SimplifyUnavailable, match="CalculiX"):
        simplify_step_file(src, tmp_path / "out.step", verifier=_unavailable)


# WHAT IT MAY CHOOSE


def _candidate(fid, kind, cells, *, x=100.0, removable=True):
    return FeatureCandidate(id=fid, kind=kind, faces=(1,), radius=2.0, radius_ratio=0.01, concave=True,
                            area=1.0, centroid=(x, 30.0, 5.0), bbox=(x - 2, 28.0, 0.0, x + 2, 32.0, 10.0),
                            feature_size=0.5, est_surface_cells=cells, removable=removable)


def test_a_slot_end_is_never_chosen_and_the_costliest_go_first_up_to_the_cap():
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    cands = [_candidate("hole0", FeatureKind.hole, 10), _candidate("hole1", FeatureKind.hole, 50),
             _candidate("slot2", FeatureKind.slot_end, 999, removable=False),
             _candidate("blend3", FeatureKind.blend, 30)]
    chosen, skipped = eligible(cands, lc, 210.0, max_features=2)
    assert [c.id for c in chosen] == ["hole1", "blend3"]
    assert {s["feature"]: s["reason"] for s in skipped} == {"slot2": "not_removable",
                                                            "hole0": "over_feature_cap"}
