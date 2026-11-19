# Responsibility: Pin that a big-machine VM (RUN-4) never outlives its run: the VM ends itself when its container
# exits or its deadline passes - deleting itself, or powering off when its identity may not delete - whatever
# became of the worker; the worker deletes it once its result is written, collected or not, and finds it by name
# when another worker started it; and the sweep deletes this deployment's VMs that stopped, have a result, or
# whose job is gone, never another deployment's or one still working for a live job.
# Boundaries: the startup script is run here under bash with every command it calls stood in for; Compute
# Engine, Cloud Storage and the database are stood in for; nothing is created in the cloud.
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.adapters.mesh_execution import vm_sweep as S
from meshpipeline.contracts import mesh_machine as MM

# THE DEFECT (job 6cb2d606, dev-areen, 28 Oct): a 107M-cell cfMesh run on an n2-highmem-16 wrote its result at
# 03:06; the worker could not bring the result back, so it never deleted the VM, and the VM's own delete was
# refused in silence (`curl -sf`, an identity that may not delete instances). It was still RUNNING 4.5 h later.

BASH = shutil.which("bash")


def _shims(tmp: Path, *, delete_code: str, container_runs_s: int, timeout: int = 100,
           margin: int = G.CONTAINER_MARGIN_S, hold: int = 0, next_run_at: int | None = None,
           next_image: str = "img") -> tuple[dict, Path]:
    """Every command the startup script calls, as a script that records what it was asked. `hold` is the
    VM's idle hold for its job; `next_run_at`, when the job's next run is handed to it (clock seconds)."""
    bin_ = tmp / "bin"
    bin_.mkdir()
    log = tmp / "calls.log"
    clock = tmp / "clock"
    clock.write_text("1000")
    (tmp / "run").write_text(f"r1|gs://b/in1|gs://b/out1|cfmesh|{timeout}|1|false||img")
    if next_run_at is not None:
        (tmp / "next").write_text(f"r2|gs://b/in2|gs://b/out2|cfmesh|{timeout}|1|false||{next_image}")
    hand_over = (f"[ -f {tmp}/next ] && [ \"$(cat {clock})\" -ge {next_run_at} ] && mv {tmp}/next {tmp}/run\n"
                 if next_run_at is not None else "")
    scripts = {
        # the metadata server and the Compute Engine API: curl answers by URL
        "curl": f"""#!/bin/bash
echo "curl $*" >> {log}
case "$*" in
  *-X\\ DELETE*) echo -n "{delete_code}" ;;
  *instance/attributes/mesh-run*) cat {tmp}/run ;;
  *instance/attributes/hold-idle-s*) echo -n "{hold}" ;;
  *instance/service-accounts/default/token*) echo -n '{{"access_token":"tok"}}' ;;
  *instance/name*) echo -n mesh-big-x ;;
  *instance/zone*) echo -n projects/p/zones/z ;;
  *) echo -n value ;;
esac
""",
        # docker: the container "runs" until the clock passes its end
        "docker": f"""#!/bin/bash
echo "docker $*" >> {log}
case "$1" in
  run) echo $(( $(cat {clock}) + {container_runs_s} )) > {tmp}/ends ;;
  inspect) [ -f {tmp}/ends ] && [ "$(cat {clock})" -lt "$(cat {tmp}/ends)" ] ;;
  stop|kill) rm -f {tmp}/ends ;;
esac
""",
        # time: `date +%s` reads the clock, and `sleep N` moves it on
        "date": f"#!/bin/bash\ncat {clock}\n",
        "sleep": f"#!/bin/bash\necho $(( $(cat {clock}) + $1 )) > {clock}\n{hand_over}",
        "shutdown": f"#!/bin/bash\necho \"shutdown $*\" >> {log}\n",
        "poweroff": f"#!/bin/bash\necho \"poweroff $*\" >> {log}\n",
        "logger": f"#!/bin/bash\necho \"logger $*\" >> {log}\n",
        "docker-credential-gcr": "#!/bin/bash\nexit 0\n",
        "mkdir": "#!/bin/bash\nexit 0\n",
        "find": "#!/bin/bash\nexit 0\n",
    }
    for name, text in scripts.items():
        p = bin_ / name
        p.write_text(text)
        p.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:/usr/bin:/bin"}
    return env, log


def _run_startup(tmp: Path, **kw) -> list[str]:
    env, log = _shims(tmp, **kw)
    script = tmp / "startup.sh"
    script.write_text(G.STARTUP_SCRIPT)
    subprocess.run([BASH, str(script)], env=env, timeout=60, check=False, capture_output=True)
    return log.read_text().splitlines() if log.exists() else []


pytestmark = pytest.mark.skipif(BASH is None, reason="bash is needed to run the VM's startup script")


def test_the_startup_script_is_valid_bash():
    subprocess.run([BASH, "-n"], input=G.STARTUP_SCRIPT, text=True, check=True)
    assert "curl -sf -X DELETE" not in G.STARTUP_SCRIPT          # a refused delete is never silent again


def test_a_vm_whose_identity_may_not_delete_it_powers_off_when_its_container_exits(tmp_path):
    calls = _run_startup(tmp_path, delete_code="403", container_runs_s=120)
    deletes = [c for c in calls if "-X DELETE" in c]
    assert len(deletes) == G.SELF_DELETE_TRIES                   # asked, retried, then...
    assert any(c.startswith("shutdown") for c in calls)          # ...powered off: no permission needed
    assert any("self-delete refused (HTTP 403)" in c for c in calls if c.startswith("logger"))


def test_a_vm_that_may_delete_itself_does_so_and_does_not_retry(tmp_path):
    calls = _run_startup(tmp_path, delete_code="200", container_runs_s=60)
    assert len([c for c in calls if "-X DELETE" in c]) == 1


def test_a_container_past_its_deadline_is_stopped_and_the_vm_ends(tmp_path):
    # a mesher that never finishes: stopped at the run's timeout + CONTAINER_MARGIN_S, then the VM ends itself
    calls = _run_startup(tmp_path, delete_code="403", container_runs_s=10 ** 9, timeout=100)
    assert any(c.startswith("docker stop") for c in calls)
    assert any(c.startswith("shutdown") for c in calls)


def test_the_vm_carries_the_lifetime_backstop_past_its_own_deadline():
    body = G.instance_body(name="mesh-big-x", zone="z", machine="n2-highmem-16", image="i", account="a",
                           input_uri="gs://b/in", output_uri="gs://b/out", engine="cfmesh", timeout=1000, disk_gb=200)
    sched = body["scheduling"]
    assert sched["instanceTerminationAction"] == "DELETE"
    assert int(sched["maxRunDuration"]["seconds"]) == 1000 + G.LIFETIME_MARGIN_S
    assert G.CONTAINER_MARGIN_S < G.LIFETIME_MARGIN_S             # the VM ends itself first


# ------------------------------------------------------------------- the worker's own delete ----

class _Blob:
    def __init__(self, store, key):
        self.store, self.key, self.size, self.generation = store, key, None, 1

    def upload_from_string(self, data, **_k):
        self.store[self.key] = data

    def exists(self):
        return self.key in self.store

    def download_as_bytes(self):
        return self.store[self.key]

    def download_to_filename(self, path):
        Path(path).write_bytes(self.store[self.key])

    def reload(self):
        self.size = len(self.store.get(self.key, b""))

    def delete(self):
        self.store.pop(self.key, None)


class _Bucket:
    def __init__(self):
        self.store = {}

    def blob(self, key, **_k):
        return _Blob(self.store, key)

    def get_blob(self, key):
        return None


@pytest.fixture
def remote(monkeypatch):
    bucket = _Bucket()
    calls: dict = {"vm": [], "deleted": [], "found": []}
    monkeypatch.setattr(C, "require_cloudrun_config", lambda: None)
    monkeypatch.setattr(C.provcfg, "GCP_MESH_BUCKET", "b", raising=False)
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    monkeypatch.setattr(gcs_exchange, "storage_client", lambda: type("S", (), {"bucket": lambda self, n: bucket})())

    def finish(key):
        coords = C.coordinates_for(key)
        bucket.store[coords.output_object_key] = b"not a tar archive"          # cannot be brought back
        bucket.store[coords.result_object_key] = json.dumps(
            {"rc": 0, "timed_out": False, "quality": {"cells": 106983984}}).encode()

    def vm(**kw):
        calls["vm"].append(kw)
        finish(kw["operation_key"])
        return G.VmReference("projects/p/zones/z/instances/mesh-big-x")

    monkeypatch.setattr(G, "trigger_vm", vm)
    monkeypatch.setattr(G, "delete_vm", lambda ref, why: calls["deleted"].append((ref.instance_name, why)) or True)
    monkeypatch.setattr(G, "find_vm", lambda name: calls["found"].append(name)
                        or G.VmReference(f"projects/p/zones/z/instances/{name}"))
    monkeypatch.setattr(C, "worker_room", lambda *a, **k: None)
    calls["finish"] = finish
    return calls


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, MM.choose(process_gib=100.0, workspace_gib=10.0))
    return ws


def test_a_vm_whose_result_could_not_be_brought_back_is_still_deleted(tmp_path, remote):
    key = hashlib.sha256(b"6cb2d606").hexdigest()
    out = C.run_mesh_remote(_ws(tmp_path), engine="cfmesh", timeout=1000, operation_key=key)
    assert out["rc"] != 0 and "remote_quality" in out                          # not collected...
    assert remote["deleted"] == [("projects/p/zones/z/instances/mesh-big-x", "its result was written")]


def test_a_worker_collecting_another_workers_run_deletes_the_vm_it_finds_by_name(tmp_path, remote):
    key = hashlib.sha256(b"replacement").hexdigest()
    remote["finish"](key)
    ws = _ws(tmp_path)
    C.collect_mesh_remote(ws, engine="cfmesh", timeout=1000, operation_key=key)
    name = G.instance_name(key, MM.read(ws)["machine"])
    assert remote["found"] == [name] and remote["deleted"][0][0].endswith(name)


# ---------------------------------------------------------------------------------- the sweep ----

NOW = 2_000_000_000.0


def _vm(name, *, status="RUNNING", bucket="b", age_s=7200, key="jobs/k1/output.tar.gz"):
    from datetime import UTC, datetime
    created = datetime.fromtimestamp(NOW - age_s, tz=UTC).isoformat()
    return {"name": name, "zone": "us-central1-a", "status": status, "creationTimestamp": created,
            "metadata": {"items": [{"key": "output-uri", "value": f"gs://{bucket}/{key}"}]}}


@pytest.fixture
def ours(monkeypatch):
    monkeypatch.setattr(S.provcfg, "GCP_MESH_BUCKET", "b", raising=False)
    monkeypatch.setattr(S.provcfg, "GCP_PROJECT_ID", "p", raising=False)


def _sweep(vms, *, results=(), owners=None, beacons=None):
    deleted: list = []
    out = S.sweep(vms=vms, now=NOW, result_written=lambda b, k: k in results,
                  owner=lambda ref: (owners or {}).get(ref.rsplit("/", 1)[-1], True),
                  delete=lambda ref, why: deleted.append((ref.instance_name.rsplit("/", 1)[-1], why)) or True,
                  beacon_age=lambda b, k: (beacons or {}).get(k))
    return out, dict(deleted)


def test_a_vm_whose_beacon_is_fresh_is_never_swept(ours):
    # 28 Oct: a claim records its VM only when the run is over, so mid-run "no claim names it" is true of every
    # VM - and a job handed on still wants the result its VM is sending. A VM heard within WORKING_S is working.
    vms = [_vm("meshing", key="jobs/m/output.tar.gz"), _vm("returning", key="jobs/r/output.tar.gz"),
           _vm("finishing", key="jobs/f/output.tar.gz")]
    _out, deleted = _sweep(vms, results={"jobs/f/result.json"},
                           owners={"meshing": None, "returning": False, "finishing": None},
                           beacons={"jobs/m/progress.json": 60.0, "jobs/r/progress.json": 30.0,
                                    "jobs/f/progress.json": 10.0})
    assert deleted == {}


def test_a_long_vm_run_survives_the_sweep_under_the_real_claim_order(ours):
    """A whole simulated 3-hour VM run, the sweep ticking every 15 minutes, with the REAL order of claim writes
    (application/native_submission): the claim is written when the run is submitted, WITHOUT a provider
    reference; the reference is recorded (mark_accepted) only when the run returns. So owner_live() answers
    None - "no claim names it" - for the whole run. The beacon pulses every minute while it meshes and while it
    sends its result back; the result document is written last; then the container exits and the beacon stops."""
    minute = 60.0
    start = NOW - 4 * 3600                       # the VM was created four hours before "now" at the end
    meshing_until, returning_until = start + 170 * minute, start + 180 * minute
    claim = {"reference": None}                  # native_submission_claims.provider_reference

    def owner_live(ref):                         # persistence: the claim that names this reference, if any
        return True if claim["reference"] == ref else None

    deleted: list = []
    vm = _vm("mesh-big-long", key="jobs/long/output.tar.gz", age_s=NOW - start)
    t = start
    while t <= start + 4 * 3600:
        if t >= returning_until and claim["reference"] is None:
            claim["reference"] = S.reference_of(vm)              # mark_accepted, once the run returned
        last_pulse = min(t, returning_until) // minute * minute  # every minute while the runner lives
        if (t - start) % (15 * minute) == 0 and not deleted:      # a worker's sweep tick
            vm_now = dict(vm, creationTimestamp=vm["creationTimestamp"])
            S.sweep(vms=[vm_now], now=t,
                    result_written=lambda b, k, t=t: k == "jobs/long/result.json" and t >= returning_until,
                    owner=owner_live, beacon_age=lambda b, k, t=t, p=last_pulse: t - p,
                    delete=lambda ref, why, t=t: deleted.append((t, why)) or True)
        t += minute
    # never swept while it meshed or sent its result back - three hours under "no claim names it"
    assert deleted and deleted[0][0] >= returning_until + S.WORKING_S
    assert "result is written" in deleted[0][1]


def test_a_vm_gone_silent_with_no_result_is_still_swept(ours):
    # a VM whose runner died (no beacon for longer than WORKING_S) and wrote no result is not working
    _out, deleted = _sweep([_vm("dead", key="jobs/d/output.tar.gz")], owners={"dead": None},
                           beacons={"jobs/d/progress.json": S.WORKING_S + 60})
    assert "no job's claim" in deleted["dead"]


def test_the_sweep_deletes_the_stopped_the_finished_and_the_orphaned(ours):
    vms = [_vm("stopped", status="TERMINATED"),
           _vm("finished", key="jobs/done/output.tar.gz"),
           _vm("ended", key="jobs/e/output.tar.gz"),
           _vm("unclaimed", key="jobs/u/output.tar.gz"),
           _vm("working", key="jobs/w/output.tar.gz")]
    out, deleted = _sweep(vms, results={"jobs/done/result.json"}, owners={"ended": False, "unclaimed": None})
    assert set(deleted) == {"stopped", "finished", "ended", "unclaimed"}
    assert "stopped" in deleted["stopped"] and "result is written" in deleted["finished"]
    assert "ended" in deleted["ended"] and "no job's claim" in deleted["unclaimed"]
    assert out["seen"] == 5 and out["kept"] == 1                       # the live job's VM keeps meshing


def test_the_sweep_never_touches_another_deployments_vm_or_a_young_one(ours):
    vms = [_vm("theirs", status="TERMINATED", bucket="other-exchange"),
           _vm("young", age_s=60, key="jobs/y/output.tar.gz"),
           _vm("unreadable", bucket="")]
    _out, deleted = _sweep(vms, owners={"young": None})
    assert deleted == {}                                               # a young VM's claim may not be written yet


def test_a_question_the_sweep_cannot_answer_keeps_the_vm(ours):
    def broken(_b, _k):
        raise OSError("storage unreachable")
    deleted: list = []
    out = S.sweep(vms=[_vm("x")], now=NOW, result_written=broken, owner=lambda r: False,
                  delete=lambda ref, why: deleted.append(ref) or True)
    assert out["kept"] == 1 and not deleted


def test_the_sweep_names_the_vm_as_its_claim_does(ours):
    vm = _vm("mesh-big-0f7d1de0e8279462ddbc")
    assert S.reference_of(vm) == G.VmReference(
        "projects/p/zones/us-central1-a/instances/mesh-big-0f7d1de0e8279462ddbc").operation_name


# --------------------------------------------------------------- held for its job (RUN-4) ----
# A job keeps its big-machine VM across its remeshes: the VM takes the job's next run as soon as it is handed
# over (no boot, no image pull), the worker does not delete it once a run's result is in, and it goes when the
# job ends - released by the job, ended by itself once idle past its hold, or swept.


def _run_held(tmp: Path, **kw) -> list[str]:
    env, log = _shims(tmp, **kw)
    script = tmp / "startup.sh"
    script.write_text(G.STARTUP_SCRIPT)
    subprocess.run([BASH, str(script)], env=env, timeout=60, check=False, capture_output=True)
    return log.read_text().splitlines() if log.exists() else []


def test_a_held_vm_takes_its_jobs_next_run_without_booting_or_pulling_again(tmp_path):
    calls = _run_held(tmp_path, delete_code="200", container_runs_s=60, hold=G.HOLD_IDLE_S, next_run_at=1400)
    runs = [c for c in calls if c.startswith("docker run")]
    assert len(runs) == 2
    assert "INPUT_URI=gs://b/in1" in runs[0] and "INPUT_URI=gs://b/in2" in runs[1]
    assert len([c for c in calls if c.startswith("docker pull")]) == 1      # the image was pulled once
    assert len([c for c in calls if "-X DELETE" in c]) == 1                 # and it ended after its hold


def test_a_new_image_is_pulled_before_the_next_run(tmp_path):
    calls = _run_held(tmp_path, delete_code="200", container_runs_s=60, hold=G.HOLD_IDLE_S, next_run_at=1400,
                      next_image="img2")
    assert [c for c in calls if c.startswith("docker pull")] == ["docker pull img", "docker pull img2"]


def test_a_held_vm_given_no_next_run_ends_itself_after_its_hold(tmp_path):
    calls = _run_held(tmp_path, delete_code="403", container_runs_s=60, hold=G.HOLD_IDLE_S)
    assert len([c for c in calls if c.startswith("docker run")]) == 1
    assert any(c.startswith("shutdown") for c in calls)
    assert any(f"no new run of its job in {G.HOLD_IDLE_S} s" in c for c in calls if c.startswith("logger"))


def test_a_vm_with_no_holder_runs_once_and_ends_as_before(tmp_path):
    calls = _run_held(tmp_path, delete_code="200", container_runs_s=60, hold=0, next_run_at=1400)
    assert len([c for c in calls if c.startswith("docker run")]) == 1


def test_a_held_vm_is_named_by_its_job_and_machine_and_carries_its_hold():
    a = G.instance_name("op-1", "n2-highmem-16", "job-1")
    assert a == G.instance_name("op-2", "n2-highmem-16", "job-1")           # every run of the job: one VM
    assert a != G.instance_name("op-1", "n2-highmem-32", "job-1")           # a step up is another machine
    assert G.instance_name("op-1", "n2-highmem-16") != G.instance_name("op-2", "n2-highmem-16")
    body = G.instance_body(name=a, zone="z", machine="n2-highmem-16", image="i", account="a",
                           input_uri="gs://b/in", output_uri="gs://b/out", engine="cfmesh", timeout=1000,
                           disk_gb=200, holder="JOB-1")
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert body["labels"]["job"] == "job-1"
    assert meta["hold-idle-s"] == str(G.HOLD_IDLE_S)
    assert meta["mesh-run"].split("|")[1:5] == ["gs://b/in", "gs://b/out", "cfmesh", "1000"]
    assert int(body["scheduling"]["maxRunDuration"]["seconds"]) == G.HOLD_LIFETIME_S


def test_a_held_vm_is_reused_only_while_up_with_room_for_the_run():
    from datetime import UTC, datetime
    created = datetime.fromtimestamp(NOW - 3600, tz=UTC).isoformat()
    vm = {"status": "RUNNING", "creationTimestamp": created,
          "scheduling": {"maxRunDuration": {"seconds": str(G.HOLD_LIFETIME_S)}}}
    left = G.HOLD_LIFETIME_S - 3600
    assert G.reusable(vm, timeout=left - G.CONTAINER_MARGIN_S - 600, now=NOW)
    assert not G.reusable(vm, timeout=left, now=NOW)                        # would outlive its limit
    assert not G.reusable({**vm, "status": "TERMINATED"}, timeout=10, now=NOW)


@pytest.fixture
def gce(monkeypatch):
    """Compute Engine stood in for: the VMs there, and what was asked of it."""
    state: dict = {"vms": [], "posted": [], "handed": [], "deleted": []}
    monkeypatch.setattr(G, "_job_container", lambda: ("img", "acct"))
    monkeypatch.setattr(G, "list_vms", lambda session=None: list(state["vms"]))
    monkeypatch.setattr(G, "describe_vm", lambda name, session=None: next(
        (v for v in state["vms"] if v["name"] == name), None))
    monkeypatch.setattr(G, "_hand_over", lambda vm, meta, session=None: state["handed"].append(
        (vm["name"], meta["mesh-run"])) or True)
    monkeypatch.setattr(G, "_wait_gone", lambda name, limit_s=0: None)

    def delete(ref, why):
        name = ref.instance_name.rsplit("/", 1)[-1]
        state["deleted"].append((name, why))
        state["vms"] = [v for v in state["vms"] if v["name"] != name]
        return True

    monkeypatch.setattr(G, "delete_vm", delete)
    monkeypatch.setattr(C, "_access_token", lambda: "tok")
    import requests

    def post(url, json, headers, timeout):
        state["posted"].append(json)
        return type("R", (), {"status_code": 200, "text": ""})()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(G.provcfg, "GCP_PROJECT_ID", "p", raising=False)
    monkeypatch.setattr(G.provcfg, "GCP_REGION", "us-central1", raising=False)
    return state


def _held(name, *, status="RUNNING", job="job-1"):
    from datetime import UTC, datetime
    return {"name": name, "zone": "us-central1-a", "status": status, "labels": {"purpose": G.LABEL, "job": job},
            "creationTimestamp": datetime.now(tz=UTC).isoformat(),
            "scheduling": {"maxRunDuration": {"seconds": str(G.HOLD_LIFETIME_S)}}}


def _trigger(holder="job-1", machine="n2-highmem-16", key="op-2"):
    return G.trigger_vm(input_uri="gs://b/in", output_uri=f"gs://b/{key}/out", engine="cfmesh", timeout=1000,
                        operation_key=key, decision={"machine": machine}, holder=holder)


def test_a_remesh_is_handed_to_the_vm_its_job_holds(gce):
    name = G.instance_name("op-1", "n2-highmem-16", "job-1")
    gce["vms"] = [_held(name)]
    ref = _trigger()
    assert ref.instance_name.endswith(name)
    assert gce["handed"] and gce["handed"][0][0] == name and not gce["posted"] and not gce["deleted"]


def test_a_step_up_releases_the_jobs_smaller_vm_and_starts_one_held_for_the_job(gce):
    small = G.instance_name("op-1", "n2-highmem-16", "job-1")
    other_job = G.instance_name("op-1", "n2-highmem-16", "job-2")
    gce["vms"] = [_held(small), _held(other_job, job="job-2")]
    _trigger(machine="n2-highmem-32")
    assert [n for n, _ in gce["deleted"]] == [small]                         # never another job's
    assert gce["posted"] and gce["posted"][0]["labels"]["job"] == "job-1"


def test_a_held_vm_that_cannot_take_the_run_is_replaced(gce):
    name = G.instance_name("op-1", "n2-highmem-16", "job-1")
    gce["vms"] = [_held(name, status="TERMINATED")]
    _trigger()
    assert gce["deleted"] and gce["deleted"][0][0] == name and not gce["handed"]
    assert gce["posted"] and gce["posted"][0]["name"] == name


def test_the_jobs_end_releases_only_its_own_vms(gce):
    gce["vms"] = [_held("mesh-big-a"), _held("mesh-big-b"), _held("mesh-big-c", job="job-2")]
    assert G.release_job_vms("job-1") == 2
    assert sorted(n for n, _ in gce["deleted"]) == ["mesh-big-a", "mesh-big-b"]


def test_the_worker_keeps_a_held_vm_once_its_result_is_in(tmp_path, remote):
    from meshpipeline.contracts.mesh_execution import vm_hold
    key = hashlib.sha256(b"held").hexdigest()
    with vm_hold("job-1"):
        C.run_mesh_remote(_ws(tmp_path), engine="cfmesh", timeout=1000, operation_key=key)
    assert remote["vm"][0]["holder"] == "job-1"
    assert remote["deleted"] == []                                           # waits for the job's next run


def test_a_held_vm_still_goes_when_its_run_is_given_up(tmp_path, remote, monkeypatch):
    from meshpipeline.contracts.mesh_execution import CANCELLED_BY_OWNER, abandonment_watch, vm_hold
    monkeypatch.setattr(G, "trigger_vm", lambda **kw: remote["vm"].append(kw)
                        or G.VmReference("projects/p/zones/z/instances/mesh-big-x"))
    monkeypatch.setattr(C, "_poll_gcs_json", lambda *a, **k: (_ for _ in ()).throw(C._Abandoned(CANCELLED_BY_OWNER)))
    with vm_hold("job-1"), abandonment_watch(lambda: None):
        C.run_mesh_remote(_ws(tmp_path), engine="cfmesh", timeout=1000,
                          operation_key=hashlib.sha256(b"cancelled").hexdigest())
    assert remote["deleted"] and remote["deleted"][0][1] == CANCELLED_BY_OWNER


def _held_sweep(vm, *, results=(), beacons=None, live=True):
    deleted: list = []
    S.sweep(vms=[vm], now=NOW, result_written=lambda b, k: k in results,
            owner=lambda ref: pytest.fail("a held VM is judged by its job, not by a claim"),
            delete=lambda ref, why: deleted.append(why) or True,
            beacon_age=lambda b, k: (beacons or {}).get(k), job_live=lambda job: live)
    return deleted


def test_the_sweep_keeps_a_held_vm_between_its_jobs_runs(ours):
    vm = {**_vm("mesh-big-h"), "labels": {"purpose": G.LABEL, "job": "job-1"}}
    from meshpipeline.artifact_keys import progress_key_beside, result_key_beside
    result, beacon = result_key_beside("jobs/k1/output.tar.gz"), progress_key_beside("jobs/k1/output.tar.gz")
    idle = S.WORKING_S + 60.0                                                # its run over, waiting for the next
    assert _held_sweep(vm, results={result}, beacons={beacon: idle}) == []
    assert _held_sweep(vm, results={result}, beacons={beacon: idle}, live=False) == [
        "swept: the job that holds it has ended"]
    assert _held_sweep(vm, beacons={beacon: S.HELD_IDLE_S + 60}) == [
        f"swept: it has had no run of its job for {int(S.HELD_IDLE_S + 60) // 60} minutes"]
    assert _held_sweep({**vm, "status": "TERMINATED"}) and _held_sweep(vm, live=None)
