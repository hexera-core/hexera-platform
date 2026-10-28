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
           margin: int = G.CONTAINER_MARGIN_S) -> tuple[dict, Path]:
    """Every command the startup script calls, as a script that records what it was asked."""
    bin_ = tmp / "bin"
    bin_.mkdir()
    log = tmp / "calls.log"
    clock = tmp / "clock"
    clock.write_text("1000")
    scripts = {
        # the metadata server and the Compute Engine API: curl answers by URL
        "curl": f"""#!/bin/bash
echo "curl $*" >> {log}
case "$*" in
  *-X\\ DELETE*) echo -n "{delete_code}" ;;
  *instance/attributes/timeout*) echo -n "{timeout}" ;;
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
        "sleep": f"#!/bin/bash\necho $(( $(cat {clock}) + $1 )) > {clock}\n",
        "shutdown": f"#!/bin/bash\necho \"shutdown $*\" >> {log}\n",
        "poweroff": f"#!/bin/bash\necho \"poweroff $*\" >> {log}\n",
        "logger": f"#!/bin/bash\necho \"logger $*\" >> {log}\n",
        "docker-credential-gcr": "#!/bin/bash\nexit 0\n",
        "mkdir": "#!/bin/bash\nexit 0\n",
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


def _sweep(vms, *, results=(), owners=None):
    deleted: list = []
    out = S.sweep(vms=vms, now=NOW, result_written=lambda b, k: k in results,
                  owner=lambda ref: (owners or {}).get(ref.rsplit("/", 1)[-1], True),
                  delete=lambda ref, why: deleted.append((ref.instance_name.rsplit("/", 1)[-1], why)) or True)
    return out, dict(deleted)


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
