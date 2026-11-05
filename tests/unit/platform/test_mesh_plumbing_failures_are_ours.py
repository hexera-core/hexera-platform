# Responsibility: Pin that every way OUR side can fail to run a mesh - refused permission, no machine free or a quota used up, a machine that never began (did not boot, could not pull the image), a machine that went away part way, a result that never came back - comes back from the mesh executor as our own failure (RC_INFRASTRUCTURE, the not-started tag), is read by the FEA driver as a pass not run (no lever, never the part's), and is told to the user as ours with the right reason - never as the CAD's.
# Boundaries: the executor's exchange with the bucket, the VM route and the clock all faked; nothing is called.
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.contracts.failure_cause import describe, not_run_kind
from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE, RUN_NOT_STARTED_TAG

KEY = hashlib.sha256(b"plumbing").hexdigest()


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Beacon:
    def __init__(self, gen, at):
        self.generation, self.at = gen, at

    def download_as_bytes(self):
        import json
        return json.dumps({"key": "volume", "progress_at": self.at}).encode()


class _Bucket:
    """No result ever lands; the beacon is written while `alive(t)`."""

    def __init__(self, clock, alive):
        self.clock, self.alive, self.gen = clock, alive, 0

    def blob(self, _key, **_k):
        return SimpleNamespace(exists=lambda: False, upload_from_string=lambda *a, **k: None, delete=lambda: None)

    def get_blob(self, _key):
        if self.alive(self.clock.t):
            self.gen += 1
        return _Beacon(self.gen, self.clock.t) if self.gen else None


@pytest.fixture
def exchange(monkeypatch, tmp_path):
    clock = _Clock()
    seen: dict = {"deleted": [], "cancelled": []}
    monkeypatch.setattr(C.time, "time", clock.time)
    monkeypatch.setattr(C.time, "sleep", clock.sleep)
    monkeypatch.setattr(C, "abandonment_reason", lambda: None)
    monkeypatch.setattr(C, "require_cloudrun_config", lambda: None)
    monkeypatch.setattr(C.provcfg, "GCP_MESH_BUCKET", "b", raising=False)
    from meshpipeline.contracts import progress_clock
    monkeypatch.setattr(progress_clock, "touch", lambda: None)
    monkeypatch.setattr(G, "delete_vm", lambda ref, why: seen["deleted"].append(why) or True)
    monkeypatch.setattr(C, "cancel_execution", lambda op, engine, why: seen["cancelled"].append(why) or True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")

    def run(*, alive=lambda t: False, on_vm=True, trigger=None):
        from meshpipeline.adapters.mesh_execution import gcs_exchange
        bucket = _Bucket(clock, alive)
        monkeypatch.setattr(gcs_exchange, "storage_client", lambda: SimpleNamespace(bucket=lambda n: bucket))
        if on_vm:
            MM.write(ws, MM.choose(process_gib=100.0, workspace_gib=10.0))
            monkeypatch.setattr(G, "trigger_vm", trigger or (lambda **kw: G.VmReference("projects/p/zones/z/instances/v")))
        else:
            (ws / "mesh_machine.json").unlink(missing_ok=True)
            monkeypatch.setattr(C, "_trigger_job", trigger or (
                lambda **kw: C.CloudRunOperationReference("projects/p/locations/r/operations/o")))
        t0 = clock.t
        out = C.run_mesh_remote(ws, engine="gmsh", timeout=72_000, operation_key=KEY)
        return out, clock.t - t0, seen

    return run


def _ours(out: dict) -> str:
    assert out["rc"] == RC_INFRASTRUCTURE and not out.get("timed_out")
    assert str(out["log_tail"]).startswith(RUN_NOT_STARTED_TAG)
    from meshpipeline.engines.fea_driver import not_run
    assert not_run(out)
    return not_run_kind(out["log_tail"])


def test_a_refused_permission_is_ours(exchange):
    def refused(**_kw):
        raise PermissionError("HTTP 403: this worker may not read the mesh job dev-mesh (run.jobs.get)")
    out, waited, _ = exchange(trigger=refused)
    assert _ours(out) == "permission" and waited < 60


def test_no_machine_free_or_no_quota_is_ours(exchange):
    def full(**_kw):
        raise RuntimeError("no m1-ultramem-40 VM could be started (HTTP 403 in us-central1-f): QUOTA_EXCEEDED")
    out, _, _ = exchange(trigger=full)
    assert _ours(out) == "capacity"


def test_a_vm_that_never_begins_is_stopped_and_ours_not_waited_on_for_its_20_hours(exchange):
    """The VM was made, but its runner never wrote a beacon: it did not boot, or could not pull the image."""
    out, waited, seen = exchange(alive=lambda t: False)
    assert _ours(out) == "never_heard"
    assert waited == pytest.approx(C.NEVER_HEARD_VM_S, abs=20)
    assert seen["deleted"] == ["its runner never reported"]


def test_a_mesh_job_execution_that_never_begins_is_ours(exchange):
    out, waited, seen = exchange(alive=lambda t: False, on_vm=False)
    assert _ours(out) == "never_heard" and waited == pytest.approx(C.NEVER_HEARD_JOB_S, abs=20)
    assert seen["cancelled"]


def test_a_machine_that_goes_away_part_way_is_ours_not_a_mesh_too_big(exchange):
    start = 1_000_000.0
    out, waited, seen = exchange(alive=lambda t: t < start + 3600)
    assert _ours(out) == "gone"
    assert waited == pytest.approx(3600 + C.WAIT_STALL_S, abs=30)
    assert seen["deleted"] == ["it stopped answering"]


def test_a_remote_heard_late_in_its_start_window_is_never_cut_by_it(exchange):
    """First heard just inside the window, then meshing for hours: waited for until it goes quiet."""
    start = 1_000_000.0
    out, waited, _ = exchange(alive=lambda t: start + C.NEVER_HEARD_VM_S - 60 < t < start + 5 * 3600)
    assert _ours(out) == "gone" and waited == pytest.approx(5 * 3600 + C.WAIT_STALL_S, abs=30)


@pytest.mark.parametrize("error, kind, says", [
    ("[CLOUD_RUN_FAILED] gmsh: HTTPError: 403 Client Error: Forbidden", "permission", "refused permission"),
    ("[CLOUD_RUN_FAILED] gmsh: RuntimeError: no n2-highmem-64 VM could be started (HTTP 503 in us-central1-c)",
     "capacity", "no machine of that size was free"),
    ("[CLOUD_RUN_FAILED] gmsh: never-heard: a n2-highmem-8 VM was started but its mesher never reported", "never_heard",
     "never began"),
    ("[CLOUD_RUN_FAILED] gmsh: gone: a n2-highmem-8 VM stopped answering", "gone", "stopped answering"),
    ("[CLOUD_RUN_RESULT_UNCOLLECTED] gmsh: the mesh ran but its result could not be collected", "uncollected",
     "could not be brought back"),
])
def test_each_plumbing_failure_is_told_as_ours_with_its_reason(error, kind, says):
    assert not_run_kind(error) == kind
    what, nxt = describe("run_infrastructure", {"not_run": {"pass": 1, "where": "gce", "error": error}})
    assert says in what and "our failure" in what
    assert "CAD" not in what + nxt and "geometry check" not in what + nxt and "run it again" in nxt
