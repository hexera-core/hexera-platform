# Responsibility: Verify a build skew between the API, the worker and the mesh job makes a job WAIT, never fail.
# Boundaries: the contract fingerprint, the Celery header guard, and the mesh job's refusal + the worker's resubmission.
"""25 Oct, dev-areen: the API and the mesh job moved to a new build while the worker fleet stayed on
the old one. Merges to main always leave the fleet behind (deploy.yml), so the two sides must
agree on what passes between them - and when they do not, the job waits for a side that matches."""
from __future__ import annotations

import pytest

from meshpipeline.application import platform_contract as pc

# --- the run contract (API -> worker) ------------------------------------------------------------


def test_the_run_contract_is_a_short_stable_fingerprint():
    c = pc.run_contract()
    assert len(c) == 12 and int(c, 16) >= 0
    pc.run_contract.cache_clear()
    assert pc.run_contract() == c


def test_a_new_run_argument_changes_the_contract(monkeypatch):
    # an argument the API adds is a TypeError in an old worker: exactly what the contract must catch
    from meshpipeline.application import dispatch_contract
    before = pc.run_contract()
    real = dispatch_contract.accepted_keys
    monkeypatch.setattr(dispatch_contract, "accepted_keys", lambda: real() | {"a_new_argument"})
    pc.run_contract.cache_clear()
    try:
        assert pc.run_contract() != before
    finally:
        monkeypatch.undo()
        pc.run_contract.cache_clear()


def test_the_contract_is_never_the_build():
    # merges deploy the API without the fleet: keying on the build would stall every job after a merge
    import inspect
    src = inspect.getsource(pc.run_contract)
    assert "sha" not in src.lower().replace("sha256", "") and "BUILD" not in src


class _Req:
    def __init__(self, **kw):
        self.headers = kw.pop("headers", None)
        self.__dict__.update(kw)


def test_the_header_is_read_from_the_request_or_its_headers():
    assert pc.message_contract(_Req(hexera_contract="abc")) == "abc"
    assert pc.message_contract(_Req(headers={"hexera_contract": "def"})) == "def"
    assert pc.message_contract(_Req()) == ""
    assert pc.message_contract(None) == ""


def test_no_stamp_or_the_same_stamp_runs_another_stamp_waits():
    assert not pc.mismatch("")                       # an API from before this: runs as always
    assert not pc.mismatch(pc.run_contract())
    assert pc.mismatch("0123456789ab")


def test_holding_tells_the_user_waits_and_requeues_once(monkeypatch):
    notes, slept, requeued = [], [], []

    class _Pub:
        def note(self, text, op_id=""):
            notes.append((text, op_id))

    import meshpipeline.contracts.event_stream as es
    monkeypatch.setattr(es, "publisher", lambda job_id, agent=None: _Pub())
    out = pc.hold_for_update("job-1", seen="0123456789ab", requeue=lambda: requeued.append(1),
                             sleep=slept.append)
    assert out["status"] == "waiting_for_platform_update"
    assert notes and "platform is updating" in notes[0][0].lower()
    assert notes[0][1] == f"platform_updating:0123456789ab:{pc.run_contract()}"  # repeats dedupe
    assert slept == [pc.HOLD_S] and requeued == [1]


def test_a_note_that_cannot_be_sent_still_requeues(monkeypatch):
    import meshpipeline.contracts.event_stream as es

    def boom(*_a, **_k):
        raise RuntimeError("no stream")
    monkeypatch.setattr(es, "publisher", boom)
    requeued = []
    pc.hold_for_update("job-1", seen="x" * 12, requeue=lambda: requeued.append(1), sleep=lambda s: None)
    assert requeued == [1]


# --- the Celery seam ------------------------------------------------------------------------------


def test_the_launch_stamps_the_contract(monkeypatch):
    import asyncio

    import meshpipeline.adapters.pipeline_execution.celery as cel
    task = _Task()
    monkeypatch.setattr(cel, "run_simulation", task)
    import meshpipeline.application.dispatch_contract as dc
    monkeypatch.setattr(dc, "to_run_kwargs", lambda payload, where="": {"job_id": "j1"})
    asyncio.run(cel.launch(None, "j1", {"schema_version": 2}))
    assert task.sent[0]["task_id"] == "j1"
    assert task.sent[0]["headers"] == {pc.HEADER: pc.run_contract()}


class _Task:
    """Stands in for the Celery task object (the unit tier's Celery decorator returns the bare function)."""

    def __init__(self):
        self.sent = []

    def apply_async(self, **k):
        self.sent.append(k)


def _call_task(monkeypatch, *, stamp):
    import meshpipeline.adapters.pipeline_execution.celery as cel
    ran, held = [], []
    monkeypatch.setattr(cel, "run_pipeline", lambda **k: ran.append(k) or {"status": "completed"})
    task = _Task()

    def fake_hold(job_id, *, seen, requeue, sleep=None):
        held.append((job_id, seen))
        monkeypatch.setattr(cel, "run_simulation", task)   # the re-publish goes through the task
        requeue()
        return {"status": "waiting_for_platform_update"}
    monkeypatch.setattr(pc, "hold_for_update", fake_hold)
    request = _Req(id="j1", **({pc.HEADER: stamp} if stamp is not None else {}))
    kwargs = {"job_id": "j1", "owner_id": "o"}
    out = cel.held_for_update(request, kwargs)
    if out is None:
        out = cel.run_pipeline(**kwargs)
    return out, ran, held, task.sent


def test_the_task_checks_the_contract_before_it_runs():
    import inspect

    import meshpipeline.adapters.pipeline_execution.celery as cel
    src = inspect.getsource(cel.run_simulation)
    assert src.index("held_for_update(") < src.index("run_pipeline(**kwargs)")


def test_a_job_stamped_with_another_contract_is_held_and_put_back(monkeypatch):
    out, ran, held, requeued = _call_task(monkeypatch, stamp="0123456789ab")
    assert ran == []                                       # never run into code that cannot read it
    assert held == [("j1", "0123456789ab")]
    assert requeued == [{"kwargs": {"job_id": "j1", "owner_id": "o"}, "task_id": "j1",
                         "headers": {pc.HEADER: "0123456789ab"}}]
    assert out["status"] == "waiting_for_platform_update"


@pytest.mark.parametrize("stamp", [None, "same"])
def test_an_unstamped_or_matching_job_runs(monkeypatch, stamp):
    out, ran, held, _ = _call_task(monkeypatch, stamp=pc.run_contract() if stamp == "same" else None)
    assert ran == [{"job_id": "j1", "owner_id": "o"}] and held == []


# --- the worker <-> mesh job half --------------------------------------------------------------------


def test_the_mesh_job_refuses_another_exchange_contract_without_meshing():
    from meshpipeline.contracts.mesh_execution import (
        MESH_EXCHANGE_VERSION,
        PLATFORM_UPDATING_KEY,
        PLATFORM_UPDATING_TAG,
        RC_INFRASTRUCTURE,
    )
    from meshpipeline.runtime.mesh_runner import contract_refusal
    assert contract_refusal("") is None                      # a worker from before: meshes as always
    assert contract_refusal(str(MESH_EXCHANGE_VERSION)) is None
    r = contract_refusal(str(MESH_EXCHANGE_VERSION + 1))
    assert r["rc"] == RC_INFRASTRUCTURE and r[PLATFORM_UPDATING_KEY]
    assert r["log_tail"].startswith(PLATFORM_UPDATING_TAG)


def test_the_invocation_carries_the_contract():
    from meshpipeline.runtime.mesh_invocation import from_environment
    inv = from_environment({"INPUT_URI": "gs://b/i", "OUTPUT_URI": "gs://b/o", "HEXERA_MESH_CONTRACT": "7"})
    assert inv.contract == "7"
    assert from_environment({"INPUT_URI": "gs://b/i", "OUTPUT_URI": "gs://b/o"}).contract == ""


def test_both_routes_send_the_mesh_contract(monkeypatch):
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    import meshpipeline.adapters.mesh_execution.gce_vm as vm
    from meshpipeline.contracts.mesh_execution import MESH_CONTRACT_ENV, MESH_EXCHANGE_VERSION
    body = vm.instance_body(name="n", zone="z", machine="m", image="i", account="", input_uri="a",
                            output_uri="b", engine="gmsh", timeout=60, disk_gb=200)
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert meta["mesh-contract"] == str(MESH_EXCHANGE_VERSION)
    assert 'HEXERA_MESH_CONTRACT="$(get instance/attributes/mesh-contract)"' in vm.STARTUP_SCRIPT

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k: sent.update(json) or _Resp())
    monkeypatch.setattr(crc, "_access_token", lambda: "t")
    monkeypatch.setattr(crc, "_operation_reference", lambda resp, engine: "op")
    crc._trigger_job(input_uri="a", output_uri="b", engine="gmsh", timeout=60)
    env = {e["name"]: e["value"] for e in sent["overrides"]["containerOverrides"][0]["env"]}
    assert env[MESH_CONTRACT_ENV] == str(MESH_EXCHANGE_VERSION)


class _Blob:
    def __init__(self, bucket, key):
        self.b, self.key = bucket, key

    def upload_from_string(self, *_a, **_k):
        self.b.uploaded.append(self.key)

    def delete(self):
        self.b.deleted.append(self.key)


class _Bucket:
    def __init__(self):
        self.uploaded, self.deleted = [], []

    def blob(self, key, **_k):
        return _Blob(self, key)


def _exchange_with(monkeypatch, results, *, total_s=None):
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    import meshpipeline.adapters.mesh_execution.gcs_exchange as gx
    import meshpipeline.settings.providers as provcfg
    for k, v in {"GCP_PROJECT_ID": "p", "GCP_MESH_BUCKET": "bkt", "CLOUDRUN_JOB": "job",
                 "GCP_REGION": "us-central1"}.items():
        monkeypatch.setattr(provcfg, k, v, raising=False)
    bucket = _Bucket()
    starts, slept = [], []

    class _Client:
        def bucket(self, name):
            return bucket
    monkeypatch.setattr(gx, "storage_client", lambda: _Client())
    monkeypatch.setattr(crc, "_tar_dir", lambda ws: b"tar")
    monkeypatch.setattr(crc, "_start", lambda *a, **k: starts.append(1) or None)
    seq = list(results)
    monkeypatch.setattr(crc, "_poll_gcs_json", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(crc.time, "sleep", slept.append)
    monkeypatch.setattr(crc, "abandonment_reason", lambda: None)
    if total_s is not None:
        monkeypatch.setattr(crc, "UPDATING_TOTAL_S", total_s)
    out = crc.run_mesh_remote("/nonexistent-ws", engine="gmsh", timeout=60, operation_key="a" * 64)
    return out, bucket, starts, slept


def _updating():
    from meshpipeline.runtime.mesh_runner import contract_refusal
    return contract_refusal("999")


def test_the_worker_clears_the_refusal_and_submits_again(monkeypatch):
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    from meshpipeline.contracts.mesh_execution import RC_TIMED_OUT
    out, bucket, starts, slept = _exchange_with(monkeypatch, [_updating(), None])
    c = coordinates_for("a" * 64)
    assert len(starts) == 2                                   # the same input, submitted again
    assert {c.result_object_key, c.output_object_key} <= set(bucket.deleted)
    assert c.input_object_key not in bucket.deleted
    assert slept == [60]
    assert out["rc"] == RC_TIMED_OUT                           # the second run's own outcome stands


def test_a_long_update_is_given_up_in_words_that_say_so(monkeypatch):
    from meshpipeline.contracts.mesh_execution import PLATFORM_UPDATING_TAG, RC_INFRASTRUCTURE
    out, _bucket, starts, slept = _exchange_with(monkeypatch, [_updating()] * 3, total_s=120)
    assert out["rc"] == RC_INFRASTRUCTURE
    assert out["log_tail"].startswith(PLATFORM_UPDATING_TAG) and "platform is updating" in out["log_tail"]
    assert len(starts) == 3 and slept == [60, 60]
