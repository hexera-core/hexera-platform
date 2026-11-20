# Responsibility: Verify the simplification check judges what the bench measured - one driver solve, local pieces cut from the original and defeatured inside, -(W_s - W_o)/W_global, the screen margin and the budget ledger - and that every way it can fail leaves a feature not verified, never accepted.
# Boundaries: no gmsh and no CalculiX - the mesher, solver and job runner are fakes; OpenCASCADE builds the geometry. tests/native/test_native_simplify_verify.py runs the real tools.
from __future__ import annotations

import time

import numpy as np
import pytest

from meshpipeline.cad.simplify import verify as V


def _est(fid, estimate, *, ok=True, cut=0.0, why=""):
    return V.FeatureEstimate(fid, ok, estimate=estimate, cut_mismatch=cut, why=why)


# THE VERDICT


def test_a_feature_is_kept_only_under_the_screen_margin():
    verdicts, spent, change = V.judge([_est("a", -0.0004), _est("b", 0.0006)], budget=0.001, margin=0.5)
    assert [(v.id, v.accepted, v.reason) for v in verdicts] == [("a", True, "accepted"),
                                                                ("b", False, "over_screen")]
    assert spent == pytest.approx(0.0004)
    assert change == pytest.approx(-0.0004)


def test_the_ledger_keeps_the_least_error_first_while_the_sum_stays_in_budget():
    # each within the 0.05% screen; together 0.11% - the largest is the one left out
    ests = [_est("big", -0.0005), _est("small", -0.0001), _est("mid", 0.00045), _est("tiny", 0.00005)]
    verdicts, spent, _ = V.judge(ests, budget=0.001, margin=0.5)
    by = {v.id: v.reason for v in verdicts}
    assert by == {"tiny": "accepted", "small": "accepted", "mid": "accepted", "big": "over_budget"}
    assert spent == pytest.approx(0.0006)


def test_an_unverified_or_ill_cut_feature_is_never_accepted():
    verdicts, _, _ = V.judge([_est("x", None, ok=False, why="timed out"), _est("y", 0.0, cut=0.5)],
                             budget=0.001, margin=0.5)
    assert [(v.id, v.accepted, v.reason) for v in verdicts] == [("x", False, "not_verified"),
                                                                ("y", False, "cut_mismatch")]
    assert verdicts[0].detail == "timed out"


# THE LOAD CASE


def test_the_load_case_trims_the_longest_axis_and_bends_along_the_second():
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    assert (lc.axis, lc.bend_dir) == (0, 1)
    assert lc.lo_cut == pytest.approx(6.0) and lc.hi_cut == pytest.approx(194.0)
    assert V.LoadCase.from_dict(lc.to_dict()) == lc


def test_a_feature_in_an_end_band_or_its_own_zone_of_one_is_near_the_ends():
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    assert V.near_ends((5, 25, 0, 9, 35, 10), lc, pad=1.0)          # inside the trimmed band
    assert V.near_ends((10, 25, 0, 14, 35, 10), lc, pad=8.0)        # its zone reaches the clamp face
    assert not V.near_ends((96, 25, 0, 104, 35, 10), lc, pad=8.0)


def test_the_size_is_capped_at_half_the_thinnest_wall():
    assert V.mesh_size(300.0, None) == pytest.approx(10.0)
    assert V.mesh_size(300.0, 3.0) == pytest.approx(1.5)


def test_a_piece_surface_is_named_by_the_cut_plane_or_the_trimmed_end_it_lies_on():
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    region = (80, 10, -5, 120, 50, 15)
    assert V.plane_of((80, 20, 0, 80, 40, 10), region, 1e-6, lc) == "CX0"
    assert V.plane_of((90, 50, 0, 110, 50, 10), region, 1e-6, lc) == "CY1"
    assert V.plane_of((6, 0, 0, 6, 60, 10), region, 1e-6, lc) == "TRIM"
    assert V.plane_of((90, 20, 10, 110, 40, 10), region, 1e-6, lc) is None   # the part's own top


# READING CALCULIX


def test_the_dat_reader_takes_each_sets_forces_the_displacements_and_the_energy(tmp_path):
    dat = tmp_path / "job.dat"
    dat.write_text(
        "\n forces (fx,fy,fz) for set CX0 and time  0.1000000E+01\n\n"
        "         3  1.000000E+00  2.000000E+00  0.000000E+00\n"
        "         4 -5.000000E-01  0.000000E+00  1.000000E+00\n"
        "\n displacements (vx,vy,vz) for set NALL and time  0.1000000E+01\n\n"
        "         3  1.000000E-03  0.000000E+00  0.000000E+00\n"
        "\n total internal energy for set EALL and time  0.1000000E+01\n\n"
        "        4.857248E+01\n")
    blocks, energy = V.read_dat(dat)
    assert energy == pytest.approx(48.57248)
    assert sorted(blocks) == [("displacements", "NALL"), ("forces", "CX0")]
    assert blocks[("forces", "CX0")][4].tolist() == [-0.5, 0.0, 1.0]


# ONE JOB, WITH A FAKE MESHER AND SOLVER


def _mesh(sets):
    """Ten nodes on a line along x and one dummy tet; `sets` names node tags."""
    tags = np.arange(1, 11)
    xyz = np.c_[np.linspace(0.0, 9.0, 10), np.zeros(10), np.zeros(10)]
    return V.Meshed(tags, xyz, np.array([1]), np.arange(1, 11).reshape(1, 10), dict(sets), 0.1)


def _solver(seen, energy=2.0, planes=None):
    def solve(m, work, bcs, *, reaction_sets, want_field, budget_s):
        seen.append({"bcs": bcs, "reaction_sets": list(reaction_sets), "want_field": want_field})
        out = {"ok": True, "energy": energy,
               "reactions": {k: (planes or {}).get(k, [0.0, 1.0, 0.0]) for k in reaction_sets}}
        if want_field:
            out["u"] = np.c_[np.zeros(len(m.tags)), m.xyz[:, 0] * 1e-3, np.zeros(len(m.tags))]
        return out
    return solve


def test_the_driver_clamps_one_end_and_displaces_the_other_along_the_bend(tmp_path):
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    seen: list = []
    out = V.run_job({"kind": V.DRIVER, "brep": "x.brep", "h": 1.0, "lc": lc.to_dict(),
                     "work": str(tmp_path / "d"), "field": str(tmp_path / "d" / "f.npz")},
                    mesher=lambda *a: _mesh({"NFIX": [1, 2], "NLOAD": [9, 10]}),
                    solver=_solver(seen))
    assert out["ok"] and out["energy"] == 2.0
    bcs = seen[0]["bcs"]
    assert [(list(n), d) for n, d, _ in bcs] == [([1, 2], 1), ([1, 2], 2), ([1, 2], 3), ([9, 10], 2)]
    assert all(np.all(v == 0) for _, d, v in bcs[:3])
    assert np.allclose(bcs[3][2], 1e-3 * (lc.hi - lc.lo))
    assert seen[0]["want_field"] and np.load(out["field"])["u"].shape == (10, 3)


def test_a_piece_drives_every_cut_and_trimmed_face_from_the_driver_and_reads_only_the_cuts(tmp_path):
    lc = V.load_case((0, 0, 0, 200, 60, 10))
    xs = np.linspace(-5.0, 15.0, 21)
    g = np.array([[x, y, z] for x in xs for y in (-1.0, 1.0) for z in (-1.0, 1.0)])
    np.savez(tmp_path / "drive.npz", xyz=g, u=np.c_[g[:, 0] * 2e-3, np.zeros(len(g)), np.zeros(len(g))])
    seen: list = []
    out = V.run_job({"kind": V.PIECE, "brep": "p.brep", "h": 1.0, "lc": lc.to_dict(), "region": [0] * 6,
                     "tol": 1e-6, "drive": str(tmp_path / "drive.npz"), "work": str(tmp_path / "p")},
                    mesher=lambda *a: _mesh({"CX0": [1, 2], "CX1": [9], "TRIM": [5], "OTHER": [7]}),
                    solver=_solver(seen, energy=3.0, planes={"CX0": [1.0, 0, 0], "CX1": [-1.0, 0, 0]}))
    assert out["ok"] and out["energy"] == 3.0 and out["planes"]["CX0"] == [1.0, 0, 0]
    driven, dof, ux = seen[0]["bcs"][0]
    assert list(driven) == [1, 2, 5, 9] and dof == 1
    # the driver's field reproduced at the piece's nodes (x = 0, 1, 4, 8)
    assert np.allclose(ux, [0.0, 2e-3, 8e-3, 16e-3])
    assert seen[0]["reaction_sets"] == ["CX0", "CX1"]    # the trimmed face is driven, never read


def test_a_mesh_larger_than_the_check_solves_is_not_solved(tmp_path):
    seen: list = []
    out = V.run_job({"kind": V.DRIVER, "brep": "x.brep", "h": 1.0, "max_tets": 0,
                     "lc": V.load_case((0, 0, 0, 200, 60, 10)).to_dict(), "work": str(tmp_path)},
                    mesher=lambda *a: _mesh({"NFIX": [1], "NLOAD": [10]}), solver=_solver(seen))
    assert not out["ok"] and "tets" in out["why"] and seen == []


# THE PACKAGE, ON A REAL PLATE WITH A FAKE RUNNER


def _plate_with_holes():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(200, 60, 10)).Shape()
    for x in (80.0, 120.0):
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 30, -1), gp_Dir(0, 0, 1)), 4.0, 12.0).Shape()
        plate = BRepAlgoAPI_Cut(plate, hole).Shape()
    return plate


def _package():
    from meshpipeline.cad.simplify.recognise import recognise
    plate = _plate_with_holes()
    cands, _ = recognise(plate)
    index = V.face_sigs(plate)
    feats = [V.FeatureToVerify(c.id, c.kind.value, c.radius, c.bbox,
                               tuple(index[i - 1][0] for i in c.faces)) for c in cands]
    simplified, why = V.defeature(plate, [index[i - 1][1] for c in cands for i in c.faces])
    assert simplified is not None, why
    return plate, simplified, feats


class _Runner:
    """Answers each job from its own BREP: a piece's energy is its volume, so filling a hole raises
    W and the estimate comes out negative, as a stiffening must."""

    def __init__(self, driver=None):
        self.specs: list[dict] = []
        self.driver = driver or {}

    def __call__(self, spec, budget_s):
        self.specs.append(dict(spec))
        if spec["kind"] == V.DRIVER:
            if self.driver:
                return dict(self.driver)
            from pathlib import Path
            Path(spec["work"]).mkdir(parents=True, exist_ok=True)
            g = np.array([[x, y, z] for x in range(0, 201, 10) for y in (0, 60) for z in (0, 10)], dtype=float)
            np.savez(spec["field"], xyz=g, u=np.zeros_like(g))
            return {"ok": True, "energy": 1.0e6, "field": spec["field"], "elements": 5000}
        return {"ok": True, "energy": V._brep_volume(spec["brep"]), "elements": 100,
                "planes": {"CX0": [1.0, 0.0, 0.0], "CX1": [-1.0, 0.0, 0.0]}}


def test_one_driver_solve_then_two_pieces_per_feature_cut_from_the_original():
    plate, simplified, feats = _package()
    runner = _Runner()
    pv = V.verify_package(plate, simplified, feats, budget=0.001, margin=0.5,
                          deadline=time.monotonic() + 120, max_tets=10 ** 6, runner=runner, jobs=1)
    kinds = [s["kind"] for s in runner.specs]
    assert kinds == [V.DRIVER] + [V.PIECE] * (2 * len(feats))
    assert len(feats) == 2 and not pv.abstained
    for e in pv.estimates:
        # the defeatured piece holds exactly one filled hole more than the original piece
        assert e.ok and e.estimate == pytest.approx(-(np.pi * 16 * 10) / 1.0e6, rel=1e-3)
        assert e.cut_mismatch == 0.0
    # a filled 8 mm hole reads 0.0503% here: just over the 0.05% screen, so neither is kept
    assert {v.reason for v in pv.verdicts} == {"over_screen"}
    pv = V.verify_package(plate, simplified, feats, budget=0.002, margin=0.5,
                          deadline=time.monotonic() + 120, max_tets=10 ** 6, runner=_Runner(), jobs=1)
    assert sorted(pv.accepted) == sorted(f.id for f in feats)


def test_a_part_too_large_to_verify_abstains_without_solving():
    plate, simplified, feats = _package()
    runner = _Runner()
    pv = V.verify_package(plate, simplified, feats, budget=0.001, margin=0.5,
                          deadline=time.monotonic() + 120, max_tets=10, runner=runner)
    assert runner.specs == []
    assert "tets" in pv.abstained and pv.accepted == ()
    assert all(v.reason == "not_verified" for v in pv.verdicts)


def test_a_failed_driver_is_retried_straight_sided_then_abstains():
    plate, simplified, feats = _package()
    runner = _Runner(driver={"ok": False, "why": "worker died"})
    pv = V.verify_package(plate, simplified, feats, budget=0.001, margin=0.5,
                          deadline=time.monotonic() + 120, max_tets=10 ** 6, runner=runner)
    assert [s["ho"] for s in runner.specs] == [1, 0]
    assert "driver solve failed" in pv.abstained and pv.accepted == ()


def test_without_calculix_the_check_is_unavailable_not_failed(monkeypatch):
    plate, simplified, feats = _package()
    monkeypatch.setattr(V.shutil, "which", lambda name: None)
    with pytest.raises(V.VerifyUnavailable, match="CalculiX"):
        V.verify_package(plate, simplified, feats, budget=0.001, margin=0.5,
                         deadline=time.monotonic() + 120, max_tets=10 ** 6)


def test_a_feature_is_found_again_by_what_its_faces_are():
    plate, simplified, feats = _package()
    assert V.find_faces(plate, feats[0].faces, 1e-6) is not None
    # filled: its wall is gone from the simplified part
    assert V.find_faces(simplified, feats[0].faces, 1e-6) is None


def test_a_defeaturing_that_changes_nothing_is_refused():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(10, 10, 10)).Shape()
    out, why = V.defeature(box, [])
    assert out is None and why
