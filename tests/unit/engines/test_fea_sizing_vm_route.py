# Responsibility: Pin TIME-21's VM route (adapters/mesh_execution/fea_sizing_vm): the sizing asks for the speed machine, then its fallback, and never the Cloud Run job; a refused or unconfirmed start leaves no VM and no exchange behind; a look reads the result, the beacon and the machine; a release deletes the VM and every exchange object by name; and the mesh container runs the sizing, not a mesher, for its engine.
# Boundaries: Compute Engine and Cloud Storage are stood in for; nothing is created in the cloud. The sizing itself is stood in for in the container test.
from __future__ import annotations

import io
import json
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution import cloud_run_client as C
from meshpipeline.adapters.mesh_execution import fea_sizing_vm as V
from meshpipeline.adapters.mesh_execution import gce_vm as G
from meshpipeline.contracts import fea_sizing_remote as seam
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate


class _Blob:
    def __init__(self, bucket, key):
        self.bucket, self.name = bucket, key

    def upload_from_string(self, data, **_k):
        self.bucket.objects[self.name] = data if isinstance(data, bytes) else data.encode()

    def exists(self):
        return self.name in self.bucket.objects

    def delete(self):
        self.bucket.objects.pop(self.name, None)
        self.bucket.deleted.append(self.name)

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def download_to_filename(self, path):
        Path(path).write_bytes(self.bucket.objects[self.name])

    @property
    def updated(self):
        return self.bucket.updated.get(self.name)


class _Bucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.updated: dict[str, datetime] = {}
        self.deleted: list[str] = []

    def blob(self, key, **_k):
        return _Blob(self, key)

    def get_blob(self, key):
        return _Blob(self, key) if key in self.objects else None


@pytest.fixture
def cloud(monkeypatch):
    bucket = _Bucket()
    vms: dict[str, dict] = {}
    calls: list = []
    monkeypatch.setattr(provcfg, "GCP_PROJECT_ID", "p", raising=False)
    monkeypatch.setattr(provcfg, "GCP_REGION", "r", raising=False)
    monkeypatch.setattr(provcfg, "GCP_MESH_BUCKET", "b", raising=False)
    monkeypatch.setattr(provcfg, "CLOUDRUN_JOB", "j", raising=False)
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    monkeypatch.setattr(gcs_exchange, "storage_client", lambda: type("S", (), {"bucket": lambda self, n: bucket})())
    refuse: dict[str, Exception] = {}

    def trigger(*, input_uri, output_uri, engine, timeout, operation_key, decision, holder=""):
        name = str(decision["machine"])
        calls.append(("trigger", name, engine, timeout, holder))
        if name in refuse:
            raise refuse[name]
        vm = G.instance_name(operation_key, name)
        vms[vm] = {"status": "RUNNING"}
        return G.VmReference(f"projects/p/zones/r-a/instances/{vm}")

    def delete(ref, *, why):
        name = ref.instance_name.rsplit("/", 1)[-1]
        calls.append(("delete", name, why))
        vms.pop(name, None)
        return True

    monkeypatch.setattr(G, "trigger_vm", trigger)
    monkeypatch.setattr(G, "delete_vm", delete)
    monkeypatch.setattr(G, "find_vm", lambda name: G.VmReference(f"projects/p/zones/r-a/instances/{name}")
                        if name in vms else None)
    monkeypatch.setattr(G, "describe_vm", lambda name: ({"name": name, **vms[name]} if name in vms else None))
    return {"bucket": bucket, "vms": vms, "calls": calls, "refuse": refuse}


def _ws(tmp_path) -> Path:
    from meshpipeline.engines import fea_plan_options as PO
    src = tmp_path / "geometry.step"
    src.write_text("ISO-10303-21;")
    return PO.pack(src, tmp_path / "ws", to_metres=0.001, budget_s=1500)


def test_the_sizing_asks_for_the_speed_machine_and_then_its_fallback_never_the_job():
    names = [m["machine"] for m in V.machines()]
    assert names == [MM.SPEED_MACHINE_NAME, MM.SPEED_FALLBACK_NAME]
    assert all(m["where"] == "gce" for m in V.machines())


def test_a_start_sends_the_packed_sizing_to_an_unheld_vm_of_its_own(cloud, tmp_path):
    handle = V.VmSizing().start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    (call,) = [c for c in cloud["calls"] if c[0] == "trigger"]
    assert call == ("trigger", MM.SPEED_MACHINE_NAME, seam.SIZING_ENGINE, 1620, "")     # never held for a job
    assert handle["machine"] == MM.SPEED_MACHINE_NAME and handle["vm"].endswith(next(iter(cloud["vms"])))
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    sent = cloud["bucket"].objects[coordinates_for(handle["op"]).input_object_key]
    with tarfile.open(fileobj=io.BytesIO(sent), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["fea_sizing.json", "geometry.step"]
    # the same sizing derives the same exchange and the same VM name
    assert V.operation_key("s1|t1") == handle["op"]


def test_a_speed_machine_out_of_quota_falls_back_to_the_next_fast_one(cloud, tmp_path):
    cloud["refuse"][MM.SPEED_MACHINE_NAME] = RuntimeError("no c2d-standard-32 VM could be started (QUOTA_EXCEEDED)")
    handle = V.VmSizing().start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    assert handle["machine"] == MM.SPEED_FALLBACK_NAME
    assert [c[1] for c in cloud["calls"] if c[0] == "trigger"] == [MM.SPEED_MACHINE_NAME, MM.SPEED_FALLBACK_NAME]


def test_no_machine_at_all_is_not_started_and_leaves_nothing_behind(cloud, tmp_path):
    for m in V.machines():
        cloud["refuse"][m["machine"]] = RuntimeError("ZONE_RESOURCE_POOL_EXHAUSTED")
    with pytest.raises(seam.SizingNotStarted):
        V.VmSizing().start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    assert not cloud["vms"] and not cloud["bucket"].objects                 # its input is gone too


def test_a_start_whose_answer_never_came_deletes_the_vm_it_may_have_made(cloud, tmp_path, monkeypatch):
    real = G.trigger_vm

    def maybe(**kw):
        real(**kw)                                                          # the VM was made...
        raise SubmissionIndeterminate("the VM request failed in transport")  # ...but the answer was lost

    monkeypatch.setattr(G, "trigger_vm", maybe)
    with pytest.raises(seam.SizingNotStarted):
        V.VmSizing().start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    assert not cloud["vms"]
    assert sum(1 for c in cloud["calls"] if c[0] == "delete") == 2           # both rungs' VMs, each deleted


def test_without_the_route_configured_nothing_is_started(cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(provcfg, "CLOUDRUN_JOB", "", raising=False)
    with pytest.raises(seam.SizingNotStarted):
        V.VmSizing().start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    assert not cloud["calls"]


def test_a_look_reads_the_result_the_beacon_and_the_machine(cloud, tmp_path):
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    from meshpipeline.artifact_keys import progress_key_beside
    vm = V.VmSizing()
    handle = vm.start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    c = coordinates_for(handle["op"])
    beacon = progress_key_beside(c.output_object_key)
    assert vm.look(handle)["state"] == "running"                             # just started
    assert vm.look({**handle, "started_at": time.time() - V.NEVER_HEARD_S - 1})["state"] == "lost"
    cloud["bucket"].objects[beacon] = b"{}"
    cloud["bucket"].updated[beacon] = datetime.now(UTC)
    assert vm.look(handle)["state"] == "running"
    cloud["bucket"].updated[beacon] = datetime.fromtimestamp(time.time() - V.STALL_S - 1, UTC)
    assert vm.look(handle)["state"] == "lost"
    cloud["bucket"].updated[beacon] = datetime.fromtimestamp(time.time() - 200, UTC)
    cloud["vms"][handle["vm"].rsplit("/", 1)[-1]]["status"] = "TERMINATED"
    assert vm.look(handle)["state"] == "lost"                                # quiet, and its machine stopped
    cloud["bucket"].objects[c.result_object_key] = b"{}"
    assert vm.look(handle)["state"] == "done"


def test_a_release_deletes_the_vm_and_every_exchange_object(cloud, tmp_path):
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    vm = V.VmSizing()
    handle = vm.start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    c = coordinates_for(handle["op"])
    cloud["bucket"].objects[c.output_object_key] = b"tar"
    cloud["bucket"].objects[c.result_object_key] = b"{}"
    vm.release(handle, why="its result was collected")
    vm.release(handle, why="again")                                         # twice is harmless
    assert not cloud["vms"] and not cloud["bucket"].objects
    assert ("delete", handle["vm"].rsplit("/", 1)[-1], "fea sizing: its result was collected") in cloud["calls"]


def test_a_collect_brings_back_the_result_and_its_files(cloud, tmp_path):
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    vm = V.VmSizing()
    handle = vm.start(_ws(tmp_path), key="s1|t1", timeout_s=1620)
    c = coordinates_for(handle["op"])
    back = tmp_path / "back"
    back.mkdir()
    (back / "fea_plans.json").write_text('{"key": "k"}')
    cloud["bucket"].objects[c.output_object_key] = C._tar_dir(back)
    cloud["bucket"].objects[c.result_object_key] = json.dumps({"rc": 0, "fea_sizing": {"options": {}}}).encode()
    got = vm.collect(handle, tmp_path / "dest")
    assert got["rc"] == 0 and (tmp_path / "dest" / "fea_plans.json").read_text() == '{"key": "k"}'


def test_the_mesh_container_runs_the_sizing_for_its_engine(monkeypatch, tmp_path):
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    from meshpipeline.engines import dispatch
    from meshpipeline.engines import fea_plan_options as PO
    from meshpipeline.runtime import mesh_runner as R
    ws = tmp_path / "ws"
    ws.mkdir()
    sent: dict = {}
    monkeypatch.setattr(gcs_exchange, "download_workspace", lambda uri: str(ws))
    monkeypatch.setattr(gcs_exchange, "upload_result", lambda uri, w, result: sent.update(result=result))
    monkeypatch.setattr(gcs_exchange, "upload_progress", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "run_engine_local", lambda *a, **k: pytest.fail("a mesher was started"))
    monkeypatch.setattr(PO, "run_packed", lambda w: {"options": {"full": {}}, "out_of_time": False,
                                                     "planning_code": "c", "workers": 8, "seconds": 1.0})
    got = R.run("gs://b/in", "gs://b/out", seam.SIZING_ENGINE, 1620)
    assert got["rc"] == 0 and got["fea_sizing"]["options"] == {"full": {}} and sent["result"] is got
