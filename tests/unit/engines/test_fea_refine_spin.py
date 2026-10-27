"""LAD-fea-11: gmsh's 3D refinement can spin - insert points and reject nearly all of them - while
it keeps printing and burning CPU, so the stall watch (PLAN-fea-13) never stops it. g_c0103 (a 9-part
gripper mount, 1.4M elements planned) refined for 3.8 hours to the lab's limit. The watchdog now reads
gmsh's own counts and stops a spinning refinement, and the driver takes it as a fill that failed."""
from __future__ import annotations

import sys

from meshpipeline.engines.gmsh import gmsh_runner as GR
from meshpipeline.sandbox.safe_exec import Watched, run_watched

# g_c0103's refinement, 8 minutes in (lab, 27 Oct)
SPINNING = ("Info    : It. 9267000 - 210942 nodes created - worst tet radius 19.8956 (nodes removed 128094 8927964)\n"
            "Info    : It. 9267500 - 210945 nodes created - worst tet radius 13.2469 (nodes removed 128096 8928459)\n")
# g_c0007's, a healthy one: every point inserted is kept
HEALTHY = "Info    : It. 35000 - 35000 nodes created - worst tet radius 1.12439 (nodes removed 0 0)\n"


def test_gmshs_refinement_line_is_read():
    assert GR.refine_counts(SPINNING) == (9267500, 210945, 8928459)
    assert GR.refine_counts(HEALTHY) == (35000, 35000, 0)
    assert GR.refine_counts("Info    : Meshing 3D...\n") is None
    assert GR.refine_counts("") is None


def test_only_a_refinement_rejecting_most_of_its_points_is_spinning():
    assert GR.spinning_now(GR.refine_counts(SPINNING))
    assert not GR.spinning_now(GR.refine_counts(HEALTHY))
    # a big healthy refinement: millions of iterations, almost every point kept
    assert not GR.spinning_now((5_000_000, 4_900_000, 120_000))
    # early on, a burst of rejected points is no verdict yet
    assert not GR.spinning_now((400_000, 10_000, 390_000))
    assert not GR.spinning_now(None)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_the_spin_must_hold_before_the_run_is_stopped(tmp_path):
    log, clock = tmp_path / "gmshfea.log", _Clock()
    spin = GR.RefineSpin(log, hold_s=300, clock=clock)
    assert spin() is False                        # no log yet
    log.write_text("Info    : Meshing 3D...\n" + SPINNING)
    assert spin() is False                        # first look: spinning, not yet held
    clock.t = 299.0
    assert spin() is False
    clock.t = 300.0
    assert spin() is True and spin.counts == (9267500, 210945, 8928459)


def test_a_refinement_that_recovers_starts_the_hold_again(tmp_path):
    log, clock = tmp_path / "gmshfea.log", _Clock()
    spin = GR.RefineSpin(log, hold_s=300, clock=clock)
    log.write_text(SPINNING)
    assert spin() is False
    clock.t = 200.0
    log.write_text("Info    : It. 9300000 - 2000000 nodes created - worst tet radius 2.1 (nodes removed 128096 8930000)\n")
    assert spin() is False                        # 2M kept: under the ratio, the hold is dropped
    clock.t = 250.0
    log.write_text(SPINNING.replace("9267500", "9400000"))
    assert spin() is False
    clock.t = 500.0
    assert spin() is False                        # 250 s since it spun again
    clock.t = 550.0
    assert spin() is True


def test_a_healthy_refinement_is_never_stopped(tmp_path):
    log, clock = tmp_path / "gmshfea.log", _Clock()
    spin = GR.RefineSpin(log, hold_s=1, clock=clock)
    log.write_text(HEALTHY)
    for t in range(0, 10_000, 500):
        clock.t = float(t)
        assert spin() is False


def test_the_watch_stops_a_process_that_spins_though_it_prints(tmp_path):
    """It prints every 0.1 s and burns CPU - the stall watch alone would let it run to the ceiling."""
    log = tmp_path / "gmshfea.log"
    code = ("import time\nline = " + repr(SPINNING) + "\n"
            "for i in range(600):\n    print(line, flush=True); time.sleep(0.1)")
    w = run_watched([sys.executable, "-c", code], log_path=log, hard_timeout=60, stall_s=30, poll_s=0.1,
                    spinning=GR.RefineSpin(log, hold_s=0.5))
    assert w.stopped == "spinning" and w.seconds < 30


def test_a_spinning_check_that_fails_is_no_sign(tmp_path):
    def broken():
        raise RuntimeError("cannot read")
    w = run_watched([sys.executable, "-c", "print('done', flush=True)"], log_path=tmp_path / "a.log",
                    hard_timeout=60, stall_s=30, poll_s=0.1, spinning=broken)
    assert w.stopped == "ok" and w.returncode == 0


def test_the_runner_says_the_refinement_spun(tmp_path, monkeypatch):
    (tmp_path / "fea_plan.json").write_text("{}")
    (tmp_path / GR.FEA_LOG).write_text(SPINNING)
    seen = {}

    def fake(args, **kw):
        seen.update(kw)
        kw["spinning"]()                          # the watch looked at least once
        return Watched(returncode=-9, stopped="spinning", seconds=600.0, longest_silence_s=0.1,
                       silence_at_stop_s=0.1, tail=SPINNING)

    monkeypatch.setattr("meshpipeline.sandbox.safe_exec.run_watched", fake)
    out = GR._run_gmsh_local(tmp_path, timeout=13800)
    assert isinstance(seen["spinning"], GR.RefineSpin)
    assert out["timed_out"] and out["stalled"] and out["spinning"]
    assert out["watchdog"]["refine"] == {"iterations": 9267500, "kept": 210945, "rejected": 8928459}
    assert "8,928,459" in GR.spin_words((9267500, 210945, 8928459))
