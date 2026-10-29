# Responsibility: Delete the big-machine VMs (RUN-4) this deployment started that have nothing left to do - stopped, or with their result written, or whose job has ended or is unknown - whatever became of the worker that started them.
# Boundaries: lists and deletes this deployment's mesh VMs only (its exchange bucket, read off each VM's own metadata), and reads whether a result object exists and whether the job that started a VM is still running. It never touches a VM of another deployment, never a VM still working for a live job, and never the exchange's objects.
# Collaborates with: gce_vm.py (list_vms, delete_vm, VmReference; the VM's own startup script stops it when its container exits), persistence/repositories/native_submission_repository.py (owner_live), runtime/celery_worker.py (runs it at start and every SWEEP_EVERY_S).
"""THE VM SWEEP (RUN-4).

A big-machine VM ends itself when its container exits: it deletes itself when its identity may, and powers off
when it may not (gce_vm.STARTUP_SCRIPT). The worker that started it deletes it once its result is written. Neither
is enough on its own - job 6cb2d606 (28 Oct): the worker could not bring a 107M-cell result back, so it never
deleted the VM, and the VM's own delete was refused in silence; the 16-core machine billed for hours. So every
worker also sweeps, at start and every SWEEP_EVERY_S, and deletes a VM of this deployment when:

  - it has STOPPED (powered itself off: its container is done);
  - its RESULT is written in the exchange (its run is over, collected or not);
  - the JOB that started it has ended, or no claim names it - its owner is gone - once it is older than
    OWNER_GRACE_S (a VM is started before its claim records it).

A VM still WORKING is never touched (28 Oct): one whose beacon (runtime/mesh_runner, every minute while it
meshes and while it sends its result back) was written within WORKING_S is left alone, whatever its claim says -
a claim records its VM only once the run is over, so "no claim names it" is true of every VM mid-run, and a job
handed on to another worker still wants the result its VM is sending (RUN-4).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

import meshpipeline.settings.providers as provcfg

logger = logging.getLogger(__name__)

#: How often a worker sweeps.
SWEEP_EVERY_S = 900
#: A VM younger than this is not judged by its owner: it is started before the claim records its reference.
OWNER_GRACE_S = 1800
#: The Compute Engine states of a VM that is not running its work any more.
STOPPED = frozenset({"TERMINATED", "STOPPED", "STOPPING", "SUSPENDED"})
#: A VM whose beacon was written this recently is working (the waiting worker's own stall window,
#: cloud_run_client.WAIT_STALL_S: its beacon pulses every minute while it lives).
WORKING_S = 900 + 300


def _meta(vm: dict, key: str) -> str:
    for item in ((vm.get("metadata") or {}).get("items") or []):
        if item.get("key") == key:
            return str(item.get("value") or "")
    return ""


def exchange_of(vm: dict) -> tuple[str, str]:
    """(bucket, output key) of the run a VM was started for, from its own metadata (gs://bucket/key)."""
    uri = _meta(vm, "output-uri")
    if not uri.startswith("gs://"):
        return "", ""
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def reference_of(vm: dict) -> str:
    """The claim's provider reference for this VM (gce_vm.VmReference.operation_name)."""
    return f"gce:projects/{provcfg.GCP_PROJECT_ID}/zones/{vm.get('zone')}/instances/{vm.get('name')}"


def age_s(vm: dict, now: float) -> float:
    try:
        return now - datetime.fromisoformat(str(vm.get("creationTimestamp"))).timestamp()
    except ValueError:
        return 0.0


def decide(vm: dict, *, now: float, bucket: str, result_written: Callable[[str, str], bool],
           owner: Callable[[str], bool | None],
           beacon_age: Callable[[str, str], float | None] | None = None) -> str:
    """Why this VM is to be deleted, in plain words, or "" to leave it. Pure but for the questions it asks
    (whether a result object exists; how long ago its beacon was written; whether the VM's job is still
    running), asked only when they matter."""
    vm_bucket, out_key = exchange_of(vm)
    if not vm_bucket or vm_bucket != bucket:
        return ""                                   # another deployment's VM (or one we cannot place)
    if str(vm.get("status") or "") in STOPPED:
        return f"it has stopped ({vm.get('status')}): its container is done"
    from meshpipeline.artifact_keys import progress_key_beside, result_key_beside
    heard = (beacon_age or _beacon_age)(vm_bucket, progress_key_beside(out_key)) if out_key else None
    if heard is not None and heard <= WORKING_S:
        return ""          # working, sending its result back, or finishing after it: never swept
    if out_key and result_written(vm_bucket, result_key_beside(out_key)):
        # written LAST, after its output and every file sent on its own: the result is whole in the
        # exchange, which deleting the machine never touches - and its beacon has gone quiet
        return "its result is written in the exchange"
    if age_s(vm, now) >= OWNER_GRACE_S:
        live = owner(reference_of(vm))
        if live is False:
            return "the job that started it has ended"
        if live is None:
            return "no job's claim names it"
    return ""


def _result_written(bucket: str, key: str) -> bool:
    from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
    return bool(storage_client().bucket(bucket).blob(key).exists())


def _beacon_age(bucket: str, key: str) -> float | None:
    """Seconds since the run's beacon was last written, or None when it has none (or it cannot be read)."""
    try:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
        blob = storage_client().bucket(bucket).get_blob(key)
        updated = getattr(blob, "updated", None) if blob is not None else None
        return None if updated is None else max(0.0, time.time() - updated.timestamp())
    except Exception as exc:  # noqa: BLE001 - unread is "not heard": the other rules decide
        logger.warning("mesh VM sweep: a beacon could not be read (%s)", type(exc).__name__)
        return None


def _owner(reference: str) -> bool | None:
    from meshpipeline.persistence.repositories.native_submission_repository import owner_live
    return owner_live(reference)


def sweep(*, vms: list[dict] | None = None, now: float | None = None,
          result_written: Callable[[str, str], bool] | None = None,
          owner: Callable[[str], bool | None] | None = None,
          delete: Callable | None = None,
          beacon_age: Callable[[str, str], float | None] | None = None) -> dict:
    """One sweep: {"seen", "deleted": [(name, why)], "kept"}. Never raises: a question that cannot be answered
    keeps the VM (it is swept again next time, and Compute Engine's lifetime limit is the backstop)."""
    from meshpipeline.adapters.mesh_execution import gce_vm as G

    out: dict = {"seen": 0, "deleted": [], "kept": 0}
    try:
        vms = G.list_vms() if vms is None else vms
    except Exception as exc:  # noqa: BLE001 - nothing listed is nothing swept, said
        logger.warning("mesh VM sweep: the VMs could not be listed (%s)", type(exc).__name__)
        return out
    now = time.time() if now is None else now
    bucket = str(provcfg.GCP_MESH_BUCKET or "")
    for vm in vms:
        out["seen"] += 1
        try:
            why = decide(vm, now=now, bucket=bucket, result_written=result_written or _result_written,
                         owner=owner or _owner, beacon_age=beacon_age)
        except Exception as exc:  # noqa: BLE001 - not judged this time is kept this time
            logger.warning("mesh VM sweep: %s not judged (%s)", vm.get("name"), type(exc).__name__)
            why = ""
        if not why:
            out["kept"] += 1
            continue
        ref = G.VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{vm.get('zone')}/instances/{vm.get('name')}")
        if (delete or G.delete_vm)(ref, why=f"swept: {why}"):
            out["deleted"].append((str(vm.get("name")), why))
    if out["deleted"]:
        logger.warning("mesh VM sweep: deleted %s", "; ".join(f"{n} ({w})" for n, w in out["deleted"]))
    return out


def run_forever(stop=None) -> None:
    """Sweep now and every SWEEP_EVERY_S until `stop` (a threading.Event) is set; for a worker's own thread."""
    import threading

    stop = stop or threading.Event()
    while not stop.is_set():
        if provcfg.GCP_PROJECT_ID and provcfg.GCP_MESH_BUCKET:
            try:
                sweep()
            except Exception:  # noqa: BLE001 - a sweep never stops the worker
                logger.exception("mesh VM sweep failed")
        stop.wait(SWEEP_EVERY_S)


__all__ = ["OWNER_GRACE_S", "STOPPED", "SWEEP_EVERY_S", "WORKING_S", "age_s", "decide", "exchange_of", "reference_of",
           "run_forever", "sweep"]
