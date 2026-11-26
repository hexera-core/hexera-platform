# Responsibility: Verify TIME-20 - cartesianMesh is stopped when its untangling is not converging, read from its own bad-face
# counts (never the clock): static_mixer_004 spent 4,531 s of 5,514 s in 108 untangling passes that never converged.
from __future__ import annotations

from meshpipeline.engines.cfmesh import native as N


def _pass(counts, *, finished: bool = True) -> str:
    lines = ["Starting untangling the mesh"]
    lines += [f"Iteration {i}. Number of bad faces is {c}" for i, c in enumerate(counts)]
    if finished:
        lines.append("Finished untangling the mesh")
    return "\n".join(lines) + "\n"


def _stuck(n: int = 50, start: int = 60_000) -> list[int]:
    return [start + 100 * i for i in range(n)]


def test_a_mixer_that_keeps_failing_to_untangle_is_stopped():
    log = _pass(_stuck(), finished=True) + _pass(_stuck(), finished=True) + _pass(_stuck(25), finished=False)
    why = N.untangle_not_converging(log)
    assert why.startswith("cfMesh was not untangling the mesh: 2 untangling passes")
    assert "were not falling" in why


def test_a_pass_falling_fast_enough_is_left_to_finish():
    falling = [int(60_000 * 0.5 ** i) for i in range(15)]
    log = _pass(_stuck()) + _pass(_stuck()) + _pass(falling, finished=False)
    assert N.untangle_not_converging(log) == ""


def test_a_slow_pass_is_stopped_when_it_cannot_reach_zero_in_the_iterations_left():
    slow = [int(60_000 * 0.97 ** i) for i in range(30)]       # ~25 k after 30: needs ~330 more, has 20
    log = _pass(_stuck()) + _pass(_stuck()) + _pass(slow, finished=False)
    assert "more iterations, and had 20 left" in N.untangle_not_converging(log)


def test_no_stop_before_two_passes_have_failed_or_under_the_face_floor():
    assert N.untangle_not_converging(_pass(_stuck()) + _pass(_stuck(25), finished=False)) == ""
    clean = _pass([5, 0]) + _pass([3, 0])
    assert N.untangle_not_converging(clean + _pass(_stuck()) + _pass(_stuck()) + _pass([900] * 30, finished=False)) == ""


def test_the_rate_is_read_inside_the_loop_running_now_not_across_a_restart():
    # static_mixer_004's last untangling: one loop rose 109 -> 64,438, the next restarted at 53,362 and
    # fell about 10% an iteration; 109 (ten counts back, in the earlier loop) is not this loop's start.
    rising = [109, 3609, 12021, 26343, 42574, 56559, 64438]
    falling = [int(53_362 * 0.9 ** i) for i in range(4)]
    log = _pass(_stuck()) + _pass(_stuck()) + "Starting untangling the mesh\n" + "".join(
        f"Iteration {i}. Number of bad faces is {c}\n" for i, c in enumerate(rising)) + "".join(
        f"Iteration {i}. Number of bad faces is {c}\n" for i, c in enumerate(falling))
    assert N.untangle_not_converging(log) == ""
    # the same loop, still slow after more than the window, is judged on its own counts
    slow = [int(53_362 * 0.97 ** i) for i in range(15)]
    log2 = log.rsplit("Iteration 0. Number of bad faces is 53362", 1)[0] + "".join(
        f"Iteration {i}. Number of bad faces is {c}\n" for i, c in enumerate(slow))
    why = N.untangle_not_converging(log2)
    assert "after 15 iterations" in why and "had 35 left" in why


def test_the_watch_reads_the_growing_log_and_trips_once(tmp_path):
    log = tmp_path / "cartesianMesh.log"
    log.write_text(_pass(_stuck()))
    watch = N.UntangleWatch(log)
    assert watch() is None
    log.write_text(_pass(_stuck()) + _pass(_stuck()) + _pass(_stuck(20), finished=False))
    first = watch()
    assert first and first == watch() == watch.tripped
