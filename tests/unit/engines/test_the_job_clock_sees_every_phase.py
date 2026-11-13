# Responsibility: Hold every phase of a conjugate run to the JOB's clock, not the machine's - a
# phase that is working has to keep moving the one thing the job counts as progress.
"""THE MACHINE WAS GIVEN 44,431 s TO LIVE AND THE JOB STOPPED COUNTING AFTER 1,020.

`application/pipeline_budget.PROGRESS_GRACE_S` is 900 + 120 = 1,020 s. A run whose last sign of
progress is older than that gets `progress_extension() is None`, the lease is not extended, and
`application/pipeline_run` logs "Lease LOST" and accounts the job TIMED OUT - on a mesher that is
working perfectly.

WHAT "PROGRESS" MEANS, and it is one thing: `runtime/mesh_runner._output_mark` stats every `*.log`
and `*/*.log` under the workspace, `_beacon` moves `progress_at` only when that tuple changes, and
`adapters/mesh_execution/cloud_run_client._relay_progress` - the only thing that touches the
worker's clock during a remote mesh - calls `progress_clock.touch()` only when `progress_at` has
moved. Everything else is a pulse that proves the remote is alive and says nothing about progress.

THE PRICED PATH OF THE CORINTIS PLATE, and the four windows in it that were longer than the grace:

    chain      9,359 s   stage logs, watched            - fine, `conjugate._foam` writes one
    checkMesh  2,776 s   capture_output=True            - SILENT
    export     7,200 s   capture_output=True            - SILENT
    surface      144 s   in-process pyvista + gmsh      - inside the grace on its own
    index      1,006 s   in-process numpy               - SILENT
    return    19,103 s   return_scope, in-process       - SILENT, and the longest of all

These tests run the REAL beacon over a REAL workspace through each phase and read the REAL
`progress_extension` as it goes. Every one of them has a no-fix control that loads the same file
as it is at HEAD and shows the extension going None under a phase that is working.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meshpipeline.adapters.mesh_execution import gcs_exchange as GX
from meshpipeline.adapters.mesh_execution.cloud_run_client import _relay_progress
from meshpipeline.application import pipeline_budget as PB
from meshpipeline.contracts import progress_clock
from meshpipeline.engines import dispatch as D
from meshpipeline.runtime import mesh_runner as MR

#: How long a stub tool prints for. Long enough to cross a shrunk grace several times over, short
#: enough that the suite does not notice.
PHASE_S = 6
#: The grace these tests run at. PROGRESS_GRACE_S is 1,020 s in production and the plate's silent
#: windows are 1,006 to 19,103 s; this is the identical arithmetic with both shrunk by the same
#: factor, so what is asserted is the RELATIONSHIP and not a wall-clock number.
GRACE_S = 3
#: The beacon's own cadence here. In production `_beacon` looks every 15 s and pulses every 60 s
#: against a 1,020 s grace; this keeps the same shape at this scale.
LOOK_S, PULSE_S, POLL_S = 0.5, 1.0, 0.5

REPO = Path(__file__).resolve().parents[3]


# #
# the real beacon, the real relay, the real clock
# #
class _Blob:
    def __init__(self, generation, payload):
        self.generation, self._payload = generation, payload

    def download_as_bytes(self):
        return self._payload


class _Bucket:
    """One versioned object, written by the beacon and read by the worker, exactly as the mesh
    exchange's progress object is."""

    def __init__(self) -> None:
        self.gen, self.payload, self.lock = 0, None, threading.Lock()

    def put(self, doc) -> None:
        with self.lock:
            self.gen += 1
            self.payload = json.dumps(doc).encode()

    def get_blob(self, key):
        with self.lock:
            return None if self.payload is None else _Blob(self.gen, self.payload)


def _run_watched(ws: Path, body, monkeypatch) -> dict:
    """Run `body` with `mesh_runner._beacon` beside it and `cloud_run_client._relay_progress`
    polled as `_poll_gcs_json` polls it. Returns what the JOB saw."""
    bucket = _Bucket()
    monkeypatch.setattr(GX, "upload_progress", lambda uri, doc: bucket.put(doc))
    monkeypatch.setattr(PB, "PROGRESS_GRACE_S", GRACE_S)
    progress_clock.reset()
    progress_clock.touch()                       # the run was handed to its machine
    stop = threading.Event()
    beacon = threading.Thread(target=MR._beacon, args=(str(ws), "gs://o", stop),
                              kwargs={"every_s": LOOK_S, "pulse_s": PULSE_S}, daemon=True)
    seen: dict = {}
    ages: list = []
    extensions: list = []
    moved: list = []

    def worker() -> None:
        while not stop.is_set():
            _relay_progress(bucket, "progress.json", seen)
            ages.append(progress_clock.seconds_since())
            extensions.append(PB.progress_extension())
            moved.append(seen.get("progress_at"))
            time.sleep(POLL_S)

    beacon.start()
    w = threading.Thread(target=worker, daemon=True)
    w.start()
    t0 = time.monotonic()
    try:
        out = body()
    finally:
        stop.set()
        beacon.join(timeout=5.0)
        w.join(timeout=5.0)
    distinct = len({m for m in moved if m is not None})
    return {"out": out, "seconds": time.monotonic() - t0,
            "oldest_age_s": max((a for a in ages if a is not None), default=0.0),
            "lost_the_extension": any(e is None for e in extensions),
            "progress_at_moves": distinct}


def _at_head(repo_path: str, name: str):
    """One file as it is at HEAD, as a module of its own. The no-fix control, with nothing on the
    shared tree touched and nothing written anywhere."""
    try:
        src = subprocess.run(["git", "show", "HEAD:" + repo_path], cwd=str(REPO),
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:   # pragma: no cover - no git, no control
        pytest.skip(f"the no-fix control needs the file at HEAD: {exc}")
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = "HEAD:" + repo_path
    sys.modules[name] = mod
    try:
        exec(compile(src, "HEAD:" + repo_path, "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


# #
# checkMesh
# #
def _foam_workspace(tmp_path: Path, tool: str, seconds: int) -> Path:
    """A workspace the case guard accepts, with `tool` replaced by a stub that prints a line a
    second. The REAL `run_guarded` runs the REAL shell; only the OpenFOAM binary is a stand-in,
    because what is under test is where its output goes."""
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "controlDict").write_text("application foamRun;\n")
    (tmp_path / "constant").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake_bashrc").write_text(
        f"{tool}() {{\n"
        f"  i=0\n"
        f"  while [ $i -lt {seconds} ]; do echo \"{tool} working $i\"; i=$((i+1)); sleep 1; done\n"
        "  mkdir -p VTK/fluid/case_0\n"
        "  echo xx > VTK/fluid/case_0/internal.vtu\n"
        "  echo '<VTKFile/>' > VTK/fluid/case_0.vtm\n"
        "  echo 'cells:           1000'\n"
        "  echo 'Mesh OK.'\n"
        "}\n")
    return tmp_path


def _check_mesh_with(module, ws: Path):
    return module.check_mesh(ws, bashrc=str(ws / "fake_bashrc"), region="fluid",
                             write_sets=False)


def test_checkmesh_keeps_the_job_clock_moving(tmp_path, monkeypatch):
    """2,776 s on the plate, and not one byte of it reached a file the job looks at."""
    from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE

    ws = _foam_workspace(tmp_path, "checkMesh", PHASE_S)
    got = _run_watched(ws, lambda: _check_mesh_with(FE, ws), monkeypatch)

    assert got["seconds"] > 2 * GRACE_S, "the phase has to outlast the grace or this proves nothing"
    assert got["lost_the_extension"] is False, (
        f"the job stopped counting a working checkMesh (oldest age {got['oldest_age_s']:.1f}s)")
    assert got["progress_at_moves"] > 1, "progress_at never moved while checkMesh ran"
    assert got["oldest_age_s"] < GRACE_S
    # ...and the measurement is unchanged: the parser reads the same text off the same file
    assert got["out"]["mesh_ok"] is True
    assert got["out"]["cells"] == 1000
    # ...and the name is one `engines/mesher_stage` can read, so the customer's live line says
    # what the mesher is doing instead of falling back to a bare clock (see CHECKMESH_LOG)
    assert (ws / "checkMesh_fluid.log").is_file()
    from meshpipeline.engines.mesher_stage import read_stage
    assert read_stage(ws) == {"key": "checking", "label": "checking the cells",
                              "tool": "checkMesh"}


def test_checkmesh_at_head_loses_it_THE_CONTROL(tmp_path, monkeypatch):
    """THE NO-FIX CONTROL. The same stub tool, the same beacon, the same relay - against the file
    as it is at HEAD, where `capture_output=True` sends every byte to a pipe."""
    head = _at_head("src/meshpipeline/engines/cfmesh_conjugate/foam_exec.py", "_head_foam_exec")
    ws = _foam_workspace(tmp_path, "checkMesh", PHASE_S)
    got = _run_watched(ws, lambda: _check_mesh_with(head, ws), monkeypatch)

    assert got["out"]["mesh_ok"] is True, "the control has to run the same working phase"
    assert got["lost_the_extension"] is True, (
        "HEAD kept the job clock moving, so this test is not the control it claims to be")
    assert got["oldest_age_s"] > GRACE_S


# #
# foamToVTK
# #
def _export_with(module, ws: Path):
    return module.export_volume_vtk(ws, all_regions=True, region="fluid",
                                    bashrc=str(ws / "fake_bashrc"), timeout=600)


def test_the_volume_export_keeps_the_job_clock_moving(tmp_path, monkeypatch):
    """EXPORT_TIMEOUT_S is 7,200 s and the plate's export is a second copy of 334 M cells."""
    from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE

    ws = _foam_workspace(tmp_path, "foamToVTK", PHASE_S)
    got = _run_watched(ws, lambda: _export_with(FE, ws), monkeypatch)

    assert got["seconds"] > 2 * GRACE_S
    assert got["lost_the_extension"] is False, (
        f"the job stopped counting a working export (oldest age {got['oldest_age_s']:.1f}s)")
    assert got["progress_at_moves"] > 1
    assert got["out"] == "VTK/fluid/case_0/internal.vtu", "the export still returns its artifact"
    assert (ws / FE.EXPORT_LOG).is_file()


def test_the_volume_export_at_head_loses_it_THE_CONTROL(tmp_path, monkeypatch):
    head = _at_head("src/meshpipeline/engines/cfmesh_conjugate/foam_exec.py", "_head_foam_exec2")
    ws = _foam_workspace(tmp_path, "foamToVTK", PHASE_S)
    got = _run_watched(ws, lambda: _export_with(head, ws), monkeypatch)

    assert got["out"] == "VTK/fluid/case_0/internal.vtu", "the control runs the same working phase"
    assert got["lost_the_extension"] is True
    assert got["oldest_age_s"] > GRACE_S


# #
# the return - the longest window of the four, and the one the new margin exists for
# #
def _return_workspace(tmp_path: Path, mb: int = 800) -> Path:
    """A workspace shaped like a conjugate run's: ASCII polyMesh files, which is what the two
    measure passes of `return_scope` actually have to compress."""
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "controlDict").write_text("application foamRun;\n")
    block = ("\n".join(str(i % 9973) for i in range(200_000)) + "\n").encode()
    per = max(1, mb * 1024 * 1024 // (4 * len(block)))
    for n in range(4):
        with (tmp_path / "constant" / "polyMesh" / f"owner{n}").open("wb") as fh:
            for _ in range(per):
                fh.write(block)
    return tmp_path


def _return_through(module, ws: Path, monkeypatch):
    """The REAL return: `run_engine_local`'s span after the engine, with the engine itself a
    no-op because it is not what this is about."""
    from meshpipeline.engines import return_scope as RSC

    monkeypatch.setattr(RSC, "archive_ceiling", lambda result=None: 50 * 1024 * 1024)
    monkeypatch.setattr(module, "engine_runners",
                        lambda: {"cfmesh_conjugate":
                                 lambda w, *, bashrc, timeout: {"rc": 0}})
    return module.run_engine_local(ws, engine="cfmesh_conjugate", timeout=60)


def test_the_return_keeps_the_job_clock_moving(tmp_path, monkeypatch):
    """19,103 s of the plate's 39,588 s priced path, and the beacon's own `returning` flag does
    not cover it: `mesh_runner.run` sets that AFTER `run_engine_local` returns, and this happens
    inside it."""
    monkeypatch.setattr(D, "WATCH_EVERY_S", 0.5)
    ws = _return_workspace(tmp_path)
    got = _run_watched(ws, lambda: _return_through(D, ws, monkeypatch), monkeypatch)

    assert got["seconds"] > GRACE_S + 1.0, (
        "the measure passes have to outlast the grace or this proves nothing")
    assert got["lost_the_extension"] is False, (
        f"the job stopped counting a working return (oldest age {got['oldest_age_s']:.1f}s)")
    assert got["progress_at_moves"] > 1
    assert got["out"]["rc"] == 0 and got["out"]["returned_bytes"]["raw"] > 0
    assert (ws / D.PHASE_LOG.format(phase=D.RETURN_PHASE)).is_file()


def test_the_return_at_head_loses_it_THE_CONTROL(tmp_path, monkeypatch):
    head = _at_head("src/meshpipeline/engines/dispatch.py", "_head_dispatch")
    ws = _return_workspace(tmp_path)
    got = _run_watched(ws, lambda: _return_through(head, ws, monkeypatch), monkeypatch)

    assert got["out"]["rc"] == 0, "the control runs the same working return"
    assert got["lost_the_extension"] is True
    assert got["oldest_age_s"] > GRACE_S
    assert not (ws / D.PHASE_LOG.format(phase=D.RETURN_PHASE)).exists()


# #
# the two in-process phases really go through the watch
# #
def test_the_boundary_index_is_watched_while_it_runs(tmp_path):
    """Executed on a real two-region mesh: the index runs, and the phase log it leaves is what
    `_output_mark` reads."""
    from tests.foam_fixtures import write_row_of_hexes

    from meshpipeline.engines.cfmesh_conjugate import native as N

    for region in ("fluid", "solid"):
        write_row_of_hexes(tmp_path / "constant" / region / "polyMesh")
    out = N._boundary_indexes(tmp_path)

    assert sorted(out["regions"]) == ["fluid", "solid"]
    assert all(r["ok"] is True for r in out["regions"].values()), out
    log = tmp_path / D.PHASE_LOG.format(phase=N.INDEX_PHASE)
    assert log.is_file(), "the index ran outside the progress channel"
    said = log.read_text().splitlines()
    assert said[0].endswith("started") and said[-1].startswith(N.INDEX_PHASE)


def test_the_review_surface_is_watched_while_it_runs(tmp_path, monkeypatch):
    """Executed through `_review_aids` on a real VTK boundary tree."""
    pv = pytest.importorskip("pyvista")
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate import native as N

    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    patch = pv.PolyData(pts, np.array([4, 0, 1, 2, 3]))
    for region in ("fluid", "solid"):
        t = tmp_path / "VTK" / region / "case_0"
        (t / "boundary").mkdir(parents=True, exist_ok=True)
        patch.save(t / "boundary" / "walls.vtp")
        (tmp_path / "VTK" / region / "case_0.vtm").write_text("<VTKFile/>")
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: "VTK/fluid/case_0/internal.vtu")

    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1)

    assert sorted(out["groups"]) == ["fluid", "solid"], out
    assert (tmp_path / D.PHASE_LOG.format(phase=N.SURFACE_PHASE)).is_file(), (
        "the review surface ran outside the progress channel")


# #
# ...and it cannot say a stuck phase is working, which is the whole constraint
# #
#: Both halves of the honesty constraint, run in a process of their OWN - which is what the mesh
#: runner is. `LivenessWatch` reads the CPU of the whole process TREE, so inside a pytest process
#: that has just allocated and freed a gigabyte, a garbage collection pass is real CPU that no
#: watch can tell from a phase's. A mesh runner container runs one engine and nothing else.
_HUNG_AND_BUSY = """
import sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
from meshpipeline.engines import dispatch as D

out = Path({out!r})
every, looks = 1.0, 4
with D.watching_progress(out, phase="hung", every_s=every):
    time.sleep(looks * every + 0.5)          # a thread blocked on a lock looks exactly like this
with D.watching_progress(out, phase="busy", every_s=every):
    end, x = time.monotonic() + looks * every + 0.5, 0
    while time.monotonic() < end:            # ...and a phase computing looks like this
        x = (x * 1103515245 + 12345) % (2 ** 61 - 1)
print("OK")
"""


def _hung_and_busy(tmp_path: Path):
    script = tmp_path / "run_both.py"
    script.write_text(_HUNG_AND_BUSY.format(repo=str(REPO / "src"), out=str(tmp_path)))
    done = subprocess.run([sys.executable, script.name], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return ((tmp_path / "hung.log").read_text().splitlines(),
            (tmp_path / "busy.log").read_text().splitlines())


def test_a_hung_phase_writes_nothing_and_so_is_still_caught(tmp_path):
    """THE HOUSE RULE (contracts/native_time): a run continues while it SHOWS progress and is
    stopped when it shows none. The watch reports `sandbox/liveness.LivenessWatch`'s signs and
    nothing else, so a phase with no output, no CPU and no memory movement produces no line
    between `started` and `finished` - `_output_mark` does not move and the 900 s stall window
    ends the run exactly as it would have.

    A ticker would have written a line every look and made this phase look busy forever.
    """
    hung, _busy = _hung_and_busy(tmp_path)

    assert len(hung) == 2, f"the watch invented progress for a hung phase: {hung}"
    assert hung[0] == "hung: started"
    assert hung[1].startswith("hung: finished after")


def test_a_working_phase_writes_what_it_saw_THE_CONTROL(tmp_path):
    """THE CONTROL FOR THE TEST ABOVE, in the same process and at the same interval, over a body
    that is really burning CPU: now the watch has something to say, so `len(hung) == 2` is a
    measurement of the hung phase and not of a watch that never writes anything."""
    hung, busy = _hung_and_busy(tmp_path)

    assert len(busy) > 2, f"a phase burning a whole core showed no sign of life: {busy}"
    assert any("cpu" in line or "memory" in line or "output" in line for line in busy[1:-1])
    assert len(busy) > len(hung)


def test_the_watch_touches_the_local_runs_own_clock(tmp_path, monkeypatch):
    """ON THE LOCAL ROUTE the engine runs in the worker's own process, so there is no beacon and
    no relay - `progress_clock` here IS the clock `pipeline_budget` reads. The watch touches it on
    every sign, exactly as `safe_exec._wait_watched` does for a subprocess."""
    monkeypatch.setattr(PB, "PROGRESS_GRACE_S", GRACE_S)
    progress_clock.reset()
    assert progress_clock.seconds_since() is None
    with D.watching_progress(tmp_path, phase="local", every_s=5.0):
        pass
    age = progress_clock.seconds_since()
    assert age is not None and age < 1.0
    assert PB.progress_extension() is not None


def test_the_watch_never_fails_a_run(tmp_path):
    """A log that cannot be written is a line that is not said - never an exception out of a
    phase that produced a mesh."""
    missing = tmp_path / "gone"
    with D.watching_progress(missing, phase="nowhere", every_s=0.1):
        time.sleep(0.25)
    assert not missing.exists()


def test_the_production_cadence_leaves_room(tmp_path):
    """The numbers these tests shrink: a look every 30 s against a grace of 1,020 s is 34 looks
    inside the window the job allows."""
    assert D.WATCH_EVERY_S * 30 < PB.PROGRESS_GRACE_S
    assert MR.BEACON_PULSE_S * 10 < PB.PROGRESS_GRACE_S
