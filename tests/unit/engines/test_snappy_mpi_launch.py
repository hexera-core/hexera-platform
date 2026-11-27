# Responsibility: Verify a parallel snappyHexMesh starts its ranks when the mesh machine hands it an unwritable TMPDIR,
# that a run whose ranks never started is told as that and stops its passes at once, and that the job's held VM gives
# each run its own claim reference (job c06bb745, 27 Nov: the big-machine route lost "the wall" on every pass).
# Boundaries: the native runner with a stand-in OpenFOAM whose mpirun makes its session folder in TMPDIR exactly as
# OpenMPI 4 does; the VM route with Compute Engine stood in for. No OpenFOAM, no cloud.
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from meshpipeline.engines.snappy import native as N
from meshpipeline.engines.snappy import parallel_stages as ps

#: The site's own log of pass 1 (job c06bb745, attempt_1/snappyHexMesh.log, first lines)
SITE_LOG = """--------------------------------------------------------------------------
A call to mkdir was unable to create the desired directory:

  Directory: /work/ompi.baa92e751c72.1000
  Error:     Permission denied

Please check to ensure you have adequate permissions to perform
the desired operation.
--------------------------------------------------------------------------
[baa92e751c72:00694] [[6751,0],0] ORTE_ERROR_LOG: Error in file ../../../orte/util/session_dir.c at line 107
It looks like orte_init failed for some reason; your parallel process is
likely to abort.
  orte_session_dir failed
"""

#: A stand-in OpenFOAM: each tool writes what the native runner checks for; mpirun makes its session folder
#: under $TMPDIR first, as OpenMPI does, and stops with OpenMPI's words when it cannot.
FAKE_BASHRC = r"""
_poly() { mkdir -p constant/polyMesh; for f in points faces owner neighbour boundary; do echo x > constant/polyMesh/$f; done; }
blockMesh() { _poly; echo "End"; }
surfaceFeatureExtract() { echo "End"; }
decomposePar() { mkdir -p processor0; echo "End"; }
reconstructParMesh() { _poly; echo "End"; }
mpirun() {
  d="${TMPDIR:-/tmp}/ompi.fake.$(id -u)"
  if [ -n "${FAKE_MPI_FAIL:-}" ] || ! mkdir -p "$d" 2>/dev/null; then
    echo "A call to mkdir was unable to create the desired directory:"
    echo ""
    echo "  Directory: $d"
    echo "  Error:     Permission denied"
    echo "It looks like orte_init failed for some reason"
    return 1
  fi
  echo "ranks started in $d"; echo "Finished meshing without any errors"; return 0
}
"""


def _case(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "system").mkdir(parents=True)
    (ws / "constant" / "triSurface").mkdir(parents=True)
    rc = tmp_path / "fake_bashrc"
    rc.write_text(FAKE_BASHRC)
    return ws, rc


@pytest.fixture
def quiet_native(monkeypatch):
    monkeypatch.setattr(N, "_rank_count", lambda **_k: 4)
    monkeypatch.setattr(N, "_measure", lambda *_a, **_k: None)
    monkeypatch.setattr(N, "_export_volume_timed", lambda *_a, **_k: 0.0)


def _unwritable(tmp_path: Path) -> Path:
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere: an unwritable folder cannot be made")
    ro = tmp_path / "work"
    ro.mkdir()
    ro.chmod(0o555)
    return ro


def test_a_parallel_run_on_a_machine_whose_tmpdir_it_cannot_write_starts_its_ranks(tmp_path, monkeypatch,
                                                                                   quiet_native):
    # THE BIG-MACHINE ROUTE: the VM runs the mesh container as the image's user with TMPDIR=/work, a folder owned by
    # root. Python quietly used /tmp for the workspace; OpenMPI did not, and every pass's snappyHexMesh stopped
    # before a rank ran - the background box came home as the mesh, judged "the wall came out with no faces"
    ws, rc = _case(tmp_path)
    monkeypatch.setenv("TMPDIR", str(_unwritable(tmp_path)))
    out = N._run_snappy_local(ws, bashrc=str(rc), timeout=120)
    log = (ws / "snappyHexMesh.log").read_text()
    assert "ranks started" in log, log
    assert out["rc"] == 0 and not out.get("launch_failed")


def test_a_run_whose_ranks_never_started_is_told_so_and_no_box_goes_home(tmp_path, monkeypatch, quiet_native):
    ws, rc = _case(tmp_path)
    monkeypatch.setenv("FAKE_MPI_FAIL", "1")
    out = N._run_snappy_local(ws, bashrc=str(rc), timeout=120)
    assert out["rc"] != 0
    assert "could not start" in out["launch_failed"] and "ompi.fake" in out["launch_failed"]
    # blockMesh's background box is not a mesh: nothing on disk can be judged as one
    assert not (ws / "constant" / "polyMesh").exists()


def test_openmpis_own_words_are_read_and_a_mesher_failure_is_not_one():
    words = ps.launch_failure(SITE_LOG)
    assert words and "/work/ompi.baa92e751c72.1000" in words and "Permission denied" in words
    assert ps.launch_failure("--> FOAM FATAL ERROR: cannot find file \"constant/triSurface/wall.stl\"") is None
    assert ps.launch_failure("") is None


def test_an_unwritable_tmpdir_is_replaced_for_child_processes_only(tmp_path, monkeypatch):
    from meshpipeline.sandbox.safe_exec import scrubbed_subprocess_env
    ro = _unwritable(tmp_path)
    monkeypatch.setenv("TMPDIR", str(ro))
    env = scrubbed_subprocess_env()
    assert env["TMPDIR"] != str(ro) and os.access(env["TMPDIR"], os.W_OK)
    assert os.environ["TMPDIR"] == str(ro)
    good = tmp_path / "good"
    good.mkdir()
    monkeypatch.setenv("TMPDIR", str(good))
    assert scrubbed_subprocess_env()["TMPDIR"] == str(good)


def test_the_vm_makes_its_workspace_writable_for_the_container_user():
    from meshpipeline.adapters.mesh_execution.gce_vm import STARTUP_SCRIPT
    chmod = STARTUP_SCRIPT.index("chmod 1777 /mnt/stateful_partition/work")
    assert chmod < STARTUP_SCRIPT.index("docker run")


# THE DRIVER STOPS ON THE FIRST PASS THAT NEVER STARTED

class _Pub:
    def __init__(self):
        self.errors: list[str] = []

    async def aerror(self, text, **_k):
        self.errors.append(text)


def test_the_driver_stops_its_passes_on_an_unstarted_run_and_says_why(tmp_path):
    from meshpipeline.contracts.failure_cause import FailureCause, describe
    from meshpipeline.engines.preflight import read_refusal
    from meshpipeline.engines.snappy import drivers as drv
    pub = _Pub()
    why = ps.launch_failure(SITE_LOG)
    assert asyncio.run(drv._stop_on_launch_failure(tmp_path, {"rc": 1, "launch_failed": why}, pub, attempt=1,
                                                   job_id="j")) is True
    ref = read_refusal(tmp_path)
    assert ref is not None and ref.cause == FailureCause.RUN_INFRASTRUCTURE.value
    assert ref.facts["launch_failed"] == why
    said = " ".join(describe(FailureCause.RUN_INFRASTRUCTURE, ref.facts))
    assert "could not start the mesher" in said and "Nothing about your part" in said
    assert pub.errors and "could not start the mesher" in pub.errors[0]
    from meshpipeline.pipeline.executor import refusal_stands_over_mesh
    assert refusal_stands_over_mesh(ref)
    # a pass that ran is never stopped here
    assert asyncio.run(drv._stop_on_launch_failure(tmp_path / "x", {"rc": 1}, pub, attempt=1, job_id="j")) is False


# THE JOB'S HELD VM: ONE CLAIM REFERENCE PER RUN

def test_each_run_on_the_jobs_held_vm_has_its_own_claim_reference(monkeypatch):
    from meshpipeline.adapters.mesh_execution import gce_vm as G
    monkeypatch.setattr(G, "_job_container", lambda: ("img", "acct"))
    monkeypatch.setattr(G, "renew_job_hold", lambda *_a, **_k: None)
    vm = "projects/p/zones/z/instances/mesh-big-1"
    # the first pass starts the job's VM; the next ones are handed to it (or to its replacement of the same name)
    calls = iter([(None, True), (G.VmReference(vm), False), (G.VmReference(vm), False)])
    monkeypatch.setattr(G, "_held_vm", lambda *_a, **_k: next(calls))
    monkeypatch.setattr(G, "_insert", lambda **_k: G.VmReference(vm))
    refs = [G.trigger_vm(input_uri="gs://b/in", output_uri=f"gs://b/out{k}", engine="snappy", timeout=60,
                         operation_key=f"op{k}", decision={"machine": "c2d-standard-32"}, holder="job-1").operation_name
            for k in (1, 2, 2)]
    assert refs[0] != refs[1], "two runs of one held VM were one claim reference (UniqueViolation, job c06bb745)"
    assert refs[1] == refs[2], "the same run handed over again keeps its reference"
    assert all(r.startswith(f"gce:{vm}") for r in refs)
    # a VM of its own (no holder) keeps the reference the sweep looks its claim up by
    assert G.VmReference(vm).operation_name == f"gce:{vm}"
