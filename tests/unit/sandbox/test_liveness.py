"""sandbox/liveness: a run shows life by its output, its CPU or its memory, and is stalled only when
all three are flat. gmsh's curve meshing prints nothing for long stretches while it computes."""
from __future__ import annotations

import os
import sys

import pytest

from meshpipeline.sandbox.liveness import LivenessWatch, TreeSample, sample_tree


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _watch(samples: list, clock: _Clock, paths=()):
    it = iter(samples)
    return LivenessWatch(1234, paths, clock=clock, sampler=lambda pid, exclude=(): next(it))


def test_cpu_burned_between_looks_is_life():
    clock = _Clock()
    w = _watch([TreeSample(1.0, 100, 1), TreeSample(6.0, 100, 1)], clock)
    clock.t = 10.0
    assert w.poll() == "cpu" and w.cpu_s == 6.0


def test_an_idle_tree_is_not_alive():
    # 0.1 s of CPU in 10 s is an idle interpreter's bookkeeping, well under 5 % of a core
    clock = _Clock()
    w = _watch([TreeSample(1.0, 100, 1), TreeSample(1.1, 100, 1)], clock)
    clock.t = 10.0
    assert w.poll() is None


def test_a_real_memory_change_is_life():
    clock = _Clock()
    w = _watch([TreeSample(1.0, 100 << 20, 1), TreeSample(1.0, 200 << 20, 1)], clock)
    clock.t = 10.0
    assert w.poll() == "memory"


def test_output_is_life(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("a")
    clock = _Clock()
    w = _watch([TreeSample(1.0, 100, 1), TreeSample(1.0, 100, 1), TreeSample(1.0, 100, 1)],
               clock, paths=(log,))
    clock.t = 10.0
    assert w.poll() is None
    log.write_text("ab")
    clock.t = 20.0
    assert w.poll() == "output"


def test_a_new_file_under_a_wildcard_is_output(tmp_path):
    (tmp_path / "log.blockMesh").write_text("a")
    clock = _Clock()
    w = _watch([TreeSample(1.0, 100, 1)] * 3, clock, paths=(tmp_path / "log.*",))
    clock.t = 10.0
    assert w.poll() is None
    (tmp_path / "log.snappyHexMesh").write_text("b")
    clock.t = 20.0
    assert w.poll() == "output"


def test_a_tree_that_cannot_be_read_falls_back_to_output_alone(tmp_path):
    clock = _Clock()
    w = _watch([None, None], clock)
    clock.t = 10.0
    assert w.poll() is None and w.cpu_s == 0.0


def test_a_tree_that_lost_a_process_is_not_read_as_work():
    clock = _Clock()
    w = _watch([TreeSample(50.0, 100, 2), TreeSample(10.0, 100, 1)], clock)
    clock.t = 10.0
    assert w.poll() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="CPU is read from /proc")
def test_the_tree_of_this_process_is_read_and_can_leave_one_out():
    me = sample_tree(os.getpid())
    assert me is not None and me.processes >= 1 and me.cpu_s > 0.0 and me.rss_bytes > 0
    assert sample_tree(os.getpid(), exclude=(os.getpid(),)) == TreeSample(0.0, 0, 0)
    assert sample_tree(2 ** 30) is None
