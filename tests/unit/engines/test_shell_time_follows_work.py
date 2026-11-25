# Responsibility: Hold the shell mesh's time limit to its work (SHMESH-21e): a mesh priced past the 15-minute floor
# is given its price, the runner's stop follows it up to the job's cap, a small mesh keeps 15 minutes exactly, and a
# stuck face is still stopped by the watchdog long before the larger budget (SHMESH-21f).
# Boundaries: the forked run and its folder in this process tree, one tiny python subprocess; made-up meshes and
# records; no gmsh mesh is made.
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.shell_mesh import ShellMesh


def _mesh() -> ShellMesh:
    xyz = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]])
    return ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]], dtype=np.int64), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.ones(1), mid=["Mid_1"], part=["Part_1"])


def _applied() -> dict:
    return {"quality": {"valid": True, "nastran_past": {}}}


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_rate_carried(monkeypatch):
    monkeypatch.setattr(sa, "_RATE_SEEN", {"rate": None}, raising=False)


def test_the_limit_is_priced_from_the_plan_and_never_under_15_minutes(monkeypatch):
    # SHMESH-21e: 3 x planned elements at 600 a second (the big plate's measured rate, rulebook 83); at or under 900 s
    # the limit is today's 900 s exactly
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    assert sa.MESH_RATE == 600.0
    got = {n: sa._priced_total(sa.mesh_price_s(n), 900.0, 0.0)[0] for n in (1e4, 1e5, 5e5, 1.5e6)}
    assert got == {1e4: 900.0, 1e5: 900.0, 5e5: 2500.0, 1.5e6: 7500.0}
    # a plan of 1.5 million (SHMESH-20's most) takes about 2,500 s at the measured rate: its limit is above that
    assert got[1.5e6] > sa.PLANNED_MAX / sa.MESH_RATE
    # once a first whole mesh stands, this part's own seconds price it where they are longer
    assert sa.mesh_price_s(1e5, first_s=2000.0) == 6000.0
    assert sa.mesh_price_s(1e5, first_s=10.0) == 3 * 1e5 / sa.MESH_RATE
    # the outer stop already short (31g: 300 s left of it) - a small mesh keeps that, as before
    assert sa._priced_total(sa.mesh_price_s(1e4), 300.0, 0.0) == (300.0, False)


def test_the_jobs_cap_holds_a_priced_limit_and_says_so(monkeypatch):
    # SHMESH-21e: the run's hard cap (the job's time) is the most a priced limit can reach; it is never less than the
    # usual limit, and the user is told the job's time held it
    monkeypatch.setenv(sa.SHELL_CAP_ENV, f"{time.time() + 2060.0:.3f}")
    total, capped = sa._priced_total(4500.0, 900.0, 0.0)
    assert capped and 1995.0 < total < 2001.0
    monkeypatch.setenv(sa.SHELL_CAP_ENV, f"{time.time() + 100.0:.3f}")
    assert sa._priced_total(4500.0, 900.0, 0.0)[0] == 900.0
    words = sa.said_words(sa.past_limit_words(2000.0, {"planned": 1.5e6, "capped": True}))
    assert words and "1,500,000 elements planned" in words and "33 minutes" in words
    assert "all the time this job had left" in words and "larger element size" in words


def test_a_big_mesh_runs_past_the_floor_to_its_price_and_is_delivered(tmp_path, monkeypatch):
    # SHMESH-21e (forked child, real clock, scaled down): floor 2 s; the child plans 8,000 elements at 1,000 a second
    # (price 8 s) and needs 4 s - before, it was stopped at 2 s with no mesh; now it finishes and the record says why
    # (the mechanics at a book rate of 1,000 a second; the book rate itself is the first test's)
    monkeypatch.setattr(sa, "MESH_RATE", 1000.0)
    def timed(*_a, **_k):
        sa._reprice(planned=8000.0)
        time.sleep(4.0)
        return _mesh(), _applied()

    monkeypatch.setattr(sa, "_mesh_shell_timed", timed)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    monkeypatch.setattr(sa, "PRICE_FACTOR", 1.0)
    monkeypatch.setattr(sa, "FACE_WATCH_POLL", 0.2)
    monkeypatch.delenv(sa.SHELL_END_ENV, raising=False)
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    started = time.monotonic()
    mesh, applied = sa.mesh_shell(str(tmp_path / "x.brep"), {}, requirements={})
    took = time.monotonic() - started
    assert 3.5 < took < 7.5 and mesh.n_elements == 1
    clock = applied["quality"]["clock"]
    assert clock["rule"] == "SHMESH-21e" and clock["planned"] == 8000 and abs(clock["limit_s"] - 8.0) < 0.5
    assert "time_limit" not in applied["quality"]


def test_a_stuck_face_is_stopped_by_the_watchdog_long_before_the_larger_budget(tmp_path, monkeypatch):
    # SHMESH-21f (forked child, real clock): the plan is priced at 60 s, past the 2 s floor; gmsh's output shows it on
    # surface 7 (10 planned elements) and nothing more - a stuck face. The watchdog stops it at the face's own floor
    # (1 s here), the face goes a rung down its ladder and the next child makes the mesh: seconds, not the 60 s
    calls: list = []

    def timed(*_a, **_k):
        calls.append(1)
        sa._reprice(planned=60_000.0)
        if not sa._FACE_FATE["level"]:
            sa._watch_tags({7: 0}, [{"centroid": [1.0, 2.0, 3.0], "area": 5.0}], {7: 10.0})
            os.write(1, b"Info    : Meshing surface 7 (Plane, Frontal-Delaunay)\n")
            time.sleep(120.0)
        return _mesh(), _applied()

    monkeypatch.setattr(sa, "_mesh_shell_timed", timed)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    monkeypatch.setattr(sa, "PRICE_FACTOR", 1.0)
    monkeypatch.setattr(sa, "FACE_HELD_FLOOR_S", 1.0)
    monkeypatch.setattr(sa, "FACE_WATCH_POLL", 0.2)
    monkeypatch.delenv(sa.SHELL_END_ENV, raising=False)
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    started = time.monotonic()
    _m, applied = sa.mesh_shell(str(tmp_path / "x.brep"), {}, requirements={})
    took = time.monotonic() - started
    assert took < 15.0, took
    assert applied["quality"]["face_watch"]["trips"] == 1
    assert applied["quality"]["clock"]["limit_s"] > 50.0


_CONE_ROOF_STREAM = (
    # (cen_s0195's gmsh output, cut down: the big curved face 7 meshes in seconds, Blossom cannot pair it, the last
    # surface is a small transfinite one; then gmsh is done and our own clean-up and pairing of the whole mesh runs
    # silent - 278 s on the test VM - with surface 68's line the last one read)
    b"Info    : Meshing 2D...\n"
    b"Info    : [ 10%] Meshing surface 7 (Cone, Frontal-Delaunay)\n"
    b"Info    : [ 10%] Blossom: 31629 internal 1200 closed\n"
    b"Info    : [ 80%] Meshing surface 67 (Transfinite)\n"
    b"Info    : [100%] Meshing surface 68 (Transfinite)\n"
    b"Info    : Done meshing 2D (Wall 28.1s, CPU 28.0s)\n"
    b"Info    : 58930 nodes 71187 elements\n")


def test_the_silent_work_after_gmshs_2d_mesh_drops_no_face(tmp_path, monkeypatch):
    # SHMESH-21f (forked child, real clock): the plan is priced past the floor; gmsh meshes every face and says it is
    # done, then the child works on for 4 s with no line (the whole mesh's clean-up). That wait is no face's: the last
    # surface read (68, a small face) is NOT stopped, left out and the part meshed again - one child, no trip
    def timed(*_a, **_k):
        sa._reprice(planned=60_000.0)
        sa._watch_tags({7: 0, 67: 0, 68: 0}, [{"centroid": [1.0, 2.0, 3.0], "area": 5.0}], {7: 40.0, 67: 1.0, 68: 1.0})
        os.write(1, _CONE_ROOF_STREAM)
        time.sleep(4.0)
        return _mesh(), _applied()

    monkeypatch.setattr(sa, "_mesh_shell_timed", timed)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    monkeypatch.setattr(sa, "PRICE_FACTOR", 1.0)
    monkeypatch.setattr(sa, "FACE_HELD_FLOOR_S", 1.0)
    monkeypatch.setattr(sa, "FACE_WATCH_POLL", 0.2)
    monkeypatch.delenv(sa.SHELL_END_ENV, raising=False)
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    _m, applied = sa.mesh_shell(str(tmp_path / "x.brep"), {}, requirements={})
    assert "face_watch" not in applied["quality"] and not sa._FACE_FATE["out"] and not sa._FACE_FATE["level"]
    assert applied["quality"]["clock"]["limit_s"] > 50.0


def test_gmshs_done_line_ends_the_face_and_a_new_surface_starts_one(tmp_path):
    # SHMESH-21f: read from the stream, no face is on after "Done meshing 2D" (the last one counts as done); the next
    # pass's first surface line is current again, so a face stuck in a later pass is still timed
    (tmp_path / "gmsh.out").write_bytes(_CONE_ROOF_STREAM)
    seen: dict = {}
    assert sa._watch_read(tmp_path, seen) is None and 68 in seen["done"] and 7 in seen["done"]
    with open(tmp_path / "gmsh.out", "ab") as fh:
        fh.write(b"Info    : Meshing 2D...\nInfo    : [  0%] Meshing surface 9 (Plane, Frontal-Delaunay)\n")
    tag, held = sa._watch_read(tmp_path, seen)
    assert tag == 9 and held < 1.0


def test_a_big_face_is_priced_at_this_runs_own_measured_rate(tmp_path, monkeypatch):
    # SHMESH-21f: cen_s0195's first whole mesh took 481.6 s for a 28,408-element plan (59 planned elements a second,
    # against the book's 600): a face of 15,000 planned elements is inside its price at 700 s (3 x 15,000 / 59 = 763 s)
    (tmp_path / "face_planned.json").write_text(json.dumps({"7": 15_000.0, "68": 2.0}))
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "t0": 0.0, "base": 900.0, "allowed": 1444.8,
                                       "planned": 28_408, "first_s": 481.6, "rate_seen": None})
    assert not sa._face_stuck(tmp_path, 7, 700.0) and sa._face_stuck(tmp_path, 7, 800.0)
    # a small face keeps the floor: a face truly stuck is still stopped at 300 s
    assert not sa._face_stuck(tmp_path, 68, 299.0) and sa._face_stuck(tmp_path, 68, 301.0)
    # a rate measured faster than the book's never shortens a face's price
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "planned": 28_408, "first_s": 1.0, "rate_seen": 5000.0})
    assert sa._face_rate() == sa.MESH_RATE


def test_a_face_is_stuck_only_past_its_own_price_and_only_under_a_stretched_limit(tmp_path, monkeypatch):
    # SHMESH-21f: held past max(300 s, 3 x its planned elements at 1,000 a second); under today's 900 s nothing new
    # (the mechanics at a book rate of 1,000 a second; the book rate itself is the first test's)
    monkeypatch.setattr(sa, "MESH_RATE", 1000.0)
    (tmp_path / "face_planned.json").write_text(json.dumps({"7": 1000.0, "8": 200_000.0}))
    # (no rate measured by an earlier mesh in this process: the book rate alone prices the faces here)
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "t0": 0.0, "base": 900.0, "allowed": 4500.0,
                                       "planned": None, "first_s": None, "rate_seen": None})
    assert not sa._face_stuck(tmp_path, 7, 299.0)
    assert sa._face_stuck(tmp_path, 7, 301.0)
    assert not sa._face_stuck(tmp_path, 8, 590.0) and sa._face_stuck(tmp_path, 8, 601.0)
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "allowed": 900.0})
    assert not sa._face_stuck(tmp_path, 7, 899.0)


def test_the_runners_stop_never_cuts_below_the_meshs_own_limit(tmp_path):
    # SHMESH-21e: the runner's stop is its usual one, moved out to the shell mesh's end (plus the 60 s for the decks)
    # the mesh wrote beside its surface file, never past the cap
    assert sa.outer_end(1000.0, 10.0, 100.0, tmp_path) == 1010.0
    (tmp_path / "shell").mkdir()
    (tmp_path / "shell" / sa.CLOCK_FILE).write_text(json.dumps({"end": 1030.0}))
    assert sa.outer_end(1000.0, 10.0, 200.0, tmp_path) == 1030.0 + sa.END_MARGIN_S
    assert sa.outer_end(1000.0, 10.0, 50.0, tmp_path) == 1050.0
    # a real process: its usual stop 1 s, its shell mesh says it must be done 2.5 s from now - it is let finish
    script = ("import json, pathlib, sys, time; p = pathlib.Path(sys.argv[1]) / 'shell'; p.mkdir(exist_ok=True); "
              f"(p / '{sa.CLOCK_FILE}').write_text(json.dumps({{'end': time.time() + 2.5}})); time.sleep(2.0); "
              "print('done')")
    proc, allowed = sa.run_following([sys.executable, "-c", script, str(tmp_path)], base_s=1.0, cap_s=600.0,
                                     folder=tmp_path, env=dict(os.environ), poll_s=0.2)
    assert proc.returncode == 0 and proc.stdout.strip() == "done" and allowed > 2.0


def test_the_shell_runs_usual_stop_and_cap_follow_the_jobs_time():
    # SHSTAGE-6 / SHMESH-21e: 2,400 s usual, the cap half of what the attempt has left (the solid mesh keeps the rest),
    # never under the usual stop; no budget: the machine's wall
    from meshpipeline.engines import fea_driver as fd
    from meshpipeline.engines import surface_run as sr

    class Run:
        deadline_s = 20_300.0
        _started = time.monotonic()

    limit, cap = fd.shell_run_limits(Run())
    assert limit == 2400.0 and 9990.0 < cap <= 10_000.0
    assert fd.shell_run_limits(None) == (2400.0, float(fd.ceiling_for(None)))
    assert sr.run_limits(None) == (1800.0, 13_800.0)
    assert sr.run_limits(1000.0) == (800.0, 800.0)


def test_a_mesh_stopped_at_its_limit_says_what_was_asked_and_the_way_on(monkeypatch, tmp_path):
    # SHMESH-21b/21e: the note says the limit, the plan, what is delivered (its elements) and what to do
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "planned": 1.2e6, "capped": False})
    sa._SO_FAR.update(dir=str(tmp_path), mine=None, file=None, label="the first path", t0=time.time())
    try:
        sa._so_far(_mesh(), _applied(), 0, {}, name="pass 1")
        mesh, applied = sa._so_far_deliver(tmp_path, {}, 3600.0, "stopped")
    finally:
        sa._SO_FAR.update(dir=None, mine=None, file=None, label="the first path")
    note = next(n for n in mesh.notes if "SHMESH-21b" in n)
    assert "60 minutes for about 1,200,000 planned elements" in note and "1 elements) is delivered" in note
    assert "larger element size" in note and applied["quality"]["time_limit"]["planned"] == 1200000


def test_a_rate_measured_on_this_part_prices_its_plan(monkeypatch):
    # SHMESH-21e: a whole mesh this run made measured 200 elements a second: the next plan is priced at that rate, not
    # at 1,000 a second; a faster measured rate never lowers the price below MESH_RATE's
    monkeypatch.setattr(sa, "_RATE_SEEN", {"rate": 200.0})
    assert sa.mesh_price_s(1e5) == 1500.0
    monkeypatch.setattr(sa, "_RATE_SEEN", {"rate": 5000.0})
    assert sa.mesh_price_s(1e5) == 3 * 1e5 / sa.MESH_RATE
    assert sa.mesh_price_s(1e5, rate=100.0) == 3000.0


def test_the_faces_gmsh_finishes_price_the_mesh_and_a_stuck_one_adds_nothing(tmp_path, monkeypatch):
    # SHMESH-21e: 15 faces of 100,000 planned elements; the first took 100 s (1,000 a second): the 1.5M plan is priced
    # at 4,500 s; while gmsh then stays on face 2, the price does not grow (a stuck face is SHMESH-21f's)
    # (the progress arithmetic at a book rate of 1,000 a second; the book rate itself is the first test's)
    monkeypatch.setattr(sa, "MESH_RATE", 1000.0)
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    (tmp_path / "face_planned.json").write_text(json.dumps({str(k): 1e5 for k in range(1, 16)}))
    now = time.monotonic()
    monkeypatch.setattr(sa, "_CLOCK", {**sa._CLOCK, "t0": now - 110.0, "base": 900.0, "allowed": 900.0,
                                       "planned": 1.5e6, "first_s": None, "outer": None})
    seen = {"since": now - 110.0}
    assert sa._progress_take(tmp_path, seen) == 0.0            # nothing finished yet
    seen.update(done={1}, since=now - 10.0, tag=2)
    gain = sa._progress_take(tmp_path, seen)
    assert abs(gain - 3600.0) < 1.0 and abs(sa._CLOCK["allowed"] - 4500.0) < 1.0 and sa._CLOCK["rate_seen"] == 1000.0
    assert sa._progress_take(tmp_path, seen) == 0.0            # face 2 held on: the same price


def test_a_mesh_whose_faces_mesh_slowly_gets_the_time_its_rate_needs(tmp_path, monkeypatch):
    # SHMESH-21e (forked child, real clock, scaled down): floor 2 s; the plan (1,000 elements) is priced at nothing at
    # the book rate, but gmsh is seen to finish a 100-element face every 0.5 s (200 a second): priced at 5 s, the mesh
    # needs about 4 s - before, it was stopped at 2 s with no mesh
    def timed(*_a, **_k):
        sa._reprice(planned=1000.0)
        sa._watch_tags(dict.fromkeys(range(1, 11), 0), [{"centroid": [0.0, 0.0, 0.0], "area": 1.0}],
                       dict.fromkeys(range(1, 11), 100.0))
        for k in range(1, 9):
            os.write(1, f"Info    : Meshing surface {k} (Plane, Frontal-Delaunay)\n".encode())
            time.sleep(0.5)
        return _mesh(), _applied()

    monkeypatch.setattr(sa, "_mesh_shell_timed", timed)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    monkeypatch.setattr(sa, "MESH_RATE", 1e6)
    monkeypatch.setattr(sa, "PRICE_FACTOR", 1.0)
    monkeypatch.setattr(sa, "PROGRESS_MIN_S", 0.9)
    monkeypatch.setattr(sa, "FACE_WATCH_POLL", 0.1)
    monkeypatch.delenv(sa.SHELL_END_ENV, raising=False)
    monkeypatch.delenv(sa.SHELL_CAP_ENV, raising=False)
    started = time.monotonic()
    mesh, applied = sa.mesh_shell(str(tmp_path / "x.brep"), {}, requirements={})
    assert time.monotonic() - started < 8.0 and mesh.n_elements == 1
    clock = applied["quality"]["clock"]
    assert clock["limit_s"] > 3.0 and clock["rate_seen"] and clock["rate_seen"] < 400.0
    assert (tmp_path / sa.CLOCK_FILE).exists()                 # the runner was told the later end
