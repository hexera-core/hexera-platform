"""RUN-1/4: a workspace whose decision says "gce" is dispatched to a VM (never the Cloud Run job), waited
for longer (it boots and pulls the image), the VM deleted once its result is collected, and the result
says where it ran. One without a decision goes to Cloud Run as before."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.contracts import mesh_machine as MM

KEY = hashlib.sha256(b"op").hexdigest()


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
        with open(path, "wb") as fh:
            fh.write(self.store[self.key])

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


def _out_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"ok"
        info = tarfile.TarInfo("quality.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def remote(monkeypatch):
    bucket = _Bucket()
    calls: dict = {"vm": [], "job": [], "deleted": [], "waited": []}
    monkeypatch.setattr(C, "require_cloudrun_config", lambda: None)
    monkeypatch.setattr(C.provcfg, "GCP_MESH_BUCKET", "b", raising=False)
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    monkeypatch.setattr(gcs_exchange, "storage_client", lambda: type("S", (), {"bucket": lambda self, n: bucket})())

    def finish(key):
        coords = C.coordinates_for(key)
        bucket.store[coords.output_object_key] = _out_tar()
        bucket.store[coords.result_object_key] = json.dumps({"rc": 0, "timed_out": False}).encode()

    def vm(**kw):
        calls["vm"].append(kw)
        finish(kw["operation_key"])
        return G.VmReference("projects/p/zones/z/instances/mesh-big-x")

    def job(**kw):
        calls["job"].append(kw)
        finish(KEY)
        return C.CloudRunOperationReference("projects/p/locations/us-central1/operations/o1")

    real_poll = C._poll_gcs_json

    def poll(bucket_, key, deadline_s, progress_key=None):
        calls["waited"].append(deadline_s)
        return real_poll(bucket_, key, deadline_s, progress_key)

    monkeypatch.setattr(G, "trigger_vm", vm)
    monkeypatch.setattr(G, "delete_vm", lambda ref, why: calls["deleted"].append((ref.instance_name, why)) or True)
    monkeypatch.setattr(C, "_trigger_job", job)
    monkeypatch.setattr(C, "_poll_gcs_json", poll)
    monkeypatch.setattr(C, "worker_room", lambda *a, **k: None)
    return calls


def test_a_vm_decision_runs_on_a_vm_and_the_vm_is_deleted_once_collected(tmp_path, remote):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    MM.write(ws, MM.choose(process_gib=100.0, workspace_gib=10.0))
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert len(remote["vm"]) == 1 and not remote["job"]
    assert remote["vm"][0]["decision"]["machine"] == "n2-highmem-16"
    assert remote["waited"] == [1000 + 600 + C.VM_START_S]
    assert remote["deleted"] and "collected" in remote["deleted"][0][1]
    assert out["rc"] == 0 and out["ran_on"]["machine"] == "n2-highmem-16"
    assert out["provider_reference"].startswith("gce:")


def test_no_decision_runs_on_cloud_run_as_before(tmp_path, remote):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    out = C.run_mesh_remote(ws, engine="gmsh", timeout=1000, operation_key=hashlib.sha256(b"op").hexdigest())
    assert len(remote["job"]) == 1 and not remote["vm"] and not remote["deleted"]
    assert remote["waited"] == [1600] and out["ran_on"]["where"] == "cloud_run"
