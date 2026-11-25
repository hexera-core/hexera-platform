# Responsibility: Verify SHELL-61, THE MID-SURFACE IS A FUNCTION OF THE CAD AND THE SETTINGS ONLY: the extraction runs
# with OpenCASCADE's thread pool held to one thread (a forked general join is then a whole copy of its parent, which
# cannot hang on a lock a vanished worker held), the pool's size is given back after, a general join that could not
# be made is said, and a machine so slow that most of the work budget is gone makes the same shell as a quiet one.
# Boundaries: parts built in the test (tests/shell_fixtures.py); OpenCASCADE and the real extraction in process.
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402

LINUX = sys.platform.startswith("linux")


def _threads() -> int:
    return len(os.listdir("/proc/self/task"))


def _start_the_pool() -> None:
    """A parallel tessellation, as cad/fea_measure makes one: OpenCASCADE starts its worker pool."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.OSD import OSD_ThreadPool

    OSD_ThreadPool.DefaultPool_s().Init(-1)
    BRepMesh_IncrementalMesh(BRepPrimAPI_MakeBox(10, 20, 30).Shape(), 0.01, False, 0.1, True)


def _bare(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k != "seconds"}
    return out


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_the_extraction_runs_on_one_thread_and_gives_the_pool_back(monkeypatch):
    from OCP.OSD import OSD_ThreadPool

    _start_the_pool()
    before = _threads()
    if before <= 1 or OSD_ThreadPool.DefaultPool_s().NbThreads() <= 1:
        pytest.skip("this machine runs OpenCASCADE on one core")
    seen: list[int] = []
    real = ms._extract

    def counted(*a, **k):
        # OpenCASCADE's own pool, not the process's threads: numpy's BLAS may hold its threads in a test process
        # (whether it has started them depends on what ran before; the extraction's own process starts it on one
        # thread: test_the_extractions_own_process_forks_its_join_from_one_thread)
        seen.append(OSD_ThreadPool.DefaultPool_s().NbThreads())
        return real(*a, **k)

    monkeypatch.setattr(ms, "_extract", counted)
    rec, _ = ms.extract(sf.t_joint(), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert seen and set(seen) == {1}                    # no OCC worker while the mid-surface is made
    assert OSD_ThreadPool.DefaultPool_s().NbThreads() > 1   # the caller's pool is given back its size


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_the_forked_general_join_is_forked_with_no_occ_worker(monkeypatch):
    """In process, OpenCASCADE's pool holds no worker when the general join forks (numpy's BLAS may still hold its
    own here: the extraction's own process is started with it on one thread, next test)."""
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    from OCP.OSD import OSD_ThreadPool

    _start_the_pool()
    at_fork: list[int] = []
    real = ms._join_general_bounded

    def counted(*a, **k):
        at_fork.append(int(OSD_ThreadPool.DefaultPool_s().NbThreads()))
        return real(*a, **k)

    monkeypatch.setattr(ms, "_join_general_bounded", counted)
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == "general" and at_fork and set(at_fork) == {1}


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_the_extractions_own_process_forks_its_join_from_one_thread(tmp_path):
    """The bounded child (extract_file, as production runs it) starts with BLAS and OpenMP on one thread
    (SINGLE_THREAD_ENV) and with OpenCASCADE's pool held: the general join is forked from a one-thread process."""
    import json
    import os

    assert ms.child_environment()["OPENBLAS_NUM_THREADS"] == "1" and ms.child_environment()["OMP_NUM_THREADS"] == "1"
    probe = tmp_path / "probe.py"
    out = tmp_path / "threads.json"
    probe.write_text(f"""
import json, os
from meshpipeline.cad import midsurface as ms
from tests import shell_fixtures as sf
seen = []
real = ms._join_general_bounded
def counted(*a, **k):
    seen.append(len(os.listdir("/proc/self/task")))
    return real(*a, **k)
ms._join_general_bounded = counted
ms.JOIN_CLOSE_FIRST = False   # the general join is the subject here
ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
json.dump(seen, open({str(out)!r}, "w"))
""")
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    env = {**ms.child_environment(), "PYTHONPATH": os.pathsep.join([os.path.join(root, "src"), root])}
    subprocess.run([sys.executable, str(probe)], env=env, check=True, timeout=600, cwd=root)
    seen = json.loads(out.read_text())
    assert seen and set(seen) == {1}


def test_a_machine_with_most_of_its_budget_gone_makes_the_same_shell(monkeypatch):
    """A slow, loaded machine: the work budget's clock reads 80 s left all the way through (its own stop, at 0, is
    never reached). Before SHELL-61 the general join was not started then and the closing passes were skipped; the
    shell is now the quiet machine's, byte for byte."""
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    from OCP.BRepTools import BRepTools

    shape = sf.tube_on_plate()
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    monkeypatch.setattr(ms._Clock, "left", lambda self: 80.0)
    slow, slow_surface = ms.extract(shape, mm_per_unit=1.0)
    assert _bare(slow) == _bare(rec)
    assert BRepTools.Write_s(surface, "/dev/null") is not None   # writable shapes
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.brep"), os.path.join(tmp, "b.brep")
        BRepTools.Write_s(surface, a)
        BRepTools.Write_s(slow_surface, b)
        assert open(a, "rb").read() == open(b, "rb").read()


def test_with_the_old_clock_rules_the_slow_machine_made_another_shell(monkeypatch):
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    # the same slow machine under the rules as they were (JOIN_TIMED, CLOSING_TIMED): another shell, told only in part
    shape = sf.tube_on_plate()
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    monkeypatch.setattr(ms, "JOIN_TIMED", True)
    monkeypatch.setattr(ms, "CLOSING_TIMED", True)
    monkeypatch.setattr(ms, "PLATE_PASS_TIMED", True)
    monkeypatch.setattr(ms._Clock, "left", lambda self: 80.0)
    slow, _ = ms.extract(shape, mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == "general" and slow["sewn"][0]["join"] == "straight"


def test_a_general_join_that_could_not_be_made_is_said(monkeypatch):
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    def died(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(ms, "_join_general_bounded", died)
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == "straight"
    assert any("a general join could not be made (its process stopped without a result)" in n for n in rec["notes"])
