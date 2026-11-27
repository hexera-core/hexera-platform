# Responsibility: Size one big FEA part on a VM of its own (TIME-21) through the big-machine route (RUN-4: gce_vm.trigger_vm, the mesh image, the exchange bucket): the packed sizing goes up, the speed machine is asked for first and then its fallback, and the result comes back from the exchange; every VM it starts is deleted when the sizing ends, fails or is given up.
# Boundaries: moves the sizing only - what is sized, when and what is stored are application/big_sizing.py's. It never holds a VM for a job and never waits: each call is one short question.
# Collaborates with: gce_vm.py (trigger_vm, describe_vm, find_vm, delete_vm), cloud_run_client.py (the exchange archive), exchange_coordinates.py, contracts/fea_sizing_remote.py (the seam it fills), contracts/mesh_machine.py (which machines).
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from pathlib import Path

import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.fea_sizing_remote import SIZING_ENGINE, SizingNotStarted

logger = logging.getLogger(__name__)

RULE = "TIME-21"
#: A sizing VM whose runner has not written its first beacon this long after it was started never booted (or never
#: pulled its image): given up, the worker sizes it. A boot and a pull take about 2-5 minutes.
NEVER_HEARD_S = 900
#: Its beacon pulses every minute while the sizing runs (runtime/mesh_runner BEACON_PULSE_S): quiet this long, its
#: machine is gone (preempted, deleted) and the worker sizes it.
STALL_S = 600
#: The Compute Engine states of a VM that is no longer working.
_STOPPED = frozenset({"TERMINATED", "STOPPED", "STOPPING", "SUSPENDED"})


def operation_key(key: str) -> str:
    """The exchange identity of one sizing (64 hex: exchange_coordinates), from the check's own key (the session and
    the sizing's token): the same sizing derives the same coordinates and the same VM name."""
    return hashlib.sha256(f"fea-sizing|{key}".encode()).hexdigest()


def machines() -> list[dict]:
    """The machines a sizing asks for, in order: the speed machine, then its fallback (RUN-10's two fast rungs; never
    the Cloud Run job - below them the worker sizes it)."""
    from meshpipeline.contracts import mesh_machine as MM
    out = []
    for m in (MM.speed_machine(), MM.speed_fallback()):
        out.append({"where": "gce", "machine": m.name, "cpus": m.cpus, "memory_gib": m.memory_gib,
                    "workspace_gib": 0.0, "why": "fea_sizing"})
    return out


def _coords(handle: dict):
    from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
    return coordinates_for(str(handle.get("op") or ""))


def _bucket():
    from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
    return storage_client().bucket(provcfg.GCP_MESH_BUCKET)


def _drop_exchange(op: str) -> None:
    """Every exchange object of one sizing, each by name (never a prefix). Best effort."""
    try:
        from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
        from meshpipeline.artifact_keys import progress_key_beside
        c = coordinates_for(op)
        bucket = _bucket()
        for k in (c.input_object_key, c.output_object_key, c.result_object_key,
                  progress_key_beside(c.output_object_key)):
            try:
                bucket.blob(k).delete()
            except Exception:  # noqa: BLE001 - not there is the state wanted
                pass
    except Exception as exc:  # noqa: BLE001 - the bucket's lifecycle tidies what is left
        logger.info("fea sizing: its exchange was not tidied (%s)", type(exc).__name__)


class VmSizing:
    """contracts/fea_sizing_remote.SizingRemote on the big-machine VM route."""

    def start(self, ws: Path, *, key: str, timeout_s: int) -> dict:
        from meshpipeline.adapters.mesh_execution import gce_vm as G
        from meshpipeline.adapters.mesh_execution.cloud_run_client import (
            _UPLOAD_CHUNK_BYTES,
            _UPLOAD_TIMEOUT_S,
            _tar_dir,
            require_cloudrun_config,
        )
        from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
        from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate

        try:
            require_cloudrun_config()
        except Exception as exc:  # noqa: BLE001 - no big-machine route here: the worker sizes it
            raise SizingNotStarted(f"no big-machine route ({type(exc).__name__})") from None
        op = operation_key(key)
        c = coordinates_for(op)
        bucket_name = str(provcfg.GCP_MESH_BUCKET)
        try:
            _bucket().blob(c.input_object_key, chunk_size=_UPLOAD_CHUNK_BYTES).upload_from_string(
                _tar_dir(Path(ws)), content_type="application/gzip", timeout=_UPLOAD_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - nothing started: the worker sizes it
            _drop_exchange(op)
            raise SizingNotStarted(f"the part could not be sent ({type(exc).__name__})") from None
        tried: list[str] = []
        for decision in machines():
            name = str(decision["machine"])
            try:
                ref = G.trigger_vm(input_uri=f"gs://{bucket_name}/{c.input_object_key}",
                                   output_uri=f"gs://{bucket_name}/{c.output_object_key}", engine=SIZING_ENGINE,
                                   timeout=int(timeout_s), operation_key=op, decision=decision, holder="")
            except SubmissionIndeterminate as exc:
                # THE ANSWER NEVER CAME: the VM may exist - deleted by the name both sides derive, then the next rung
                gone = G.find_vm(G.instance_name(op, name))
                if gone is not None:
                    G.delete_vm(gone, why="its start was never confirmed (the FEA sizing moves on)")
                tried.append(f"{name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - no quota, none free, no permission: the next rung
                tried.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
                logger.warning("fea sizing: no %s VM (%s: %s)", name, type(exc).__name__, str(exc)[:200])
                continue
            cores = int(decision["cpus"]) // 2
            logger.warning("fea sizing runs on a %s VM (%s, %s)", name, ref.instance_name.rsplit("/", 1)[-1], RULE)
            return {"op": op, "vm": ref.instance_name, "machine": name, "cores": cores, "started_at": time.time()}
        _drop_exchange(op)
        raise SizingNotStarted("; ".join(tried) or "no machine named")

    def look(self, handle: dict) -> dict:
        try:
            from meshpipeline.artifact_keys import progress_key_beside
            c = _coords(handle)
            bucket = _bucket()
            if bucket.blob(c.result_object_key).exists():
                return {"state": "done", "why": "its result is written"}
            beacon = bucket.get_blob(progress_key_beside(c.output_object_key))
            updated = getattr(beacon, "updated", None) if beacon is not None else None
            heard = None if updated is None else max(0.0, time.time() - updated.timestamp())
        except Exception as exc:  # noqa: BLE001 - not known is still running: looked at again next time
            logger.info("fea sizing: its VM could not be looked at (%s)", type(exc).__name__)
            return {"state": "running", "why": "not known"}
        age = time.time() - float(handle.get("started_at") or time.time())
        if heard is None and age > NEVER_HEARD_S:
            return {"state": "lost", "why": f"its VM never reported in {NEVER_HEARD_S // 60} minutes"}
        if heard is not None and heard > STALL_S:
            return {"state": "lost", "why": f"its VM stopped answering ({int(heard) // 60} minutes without a word)"}
        if heard is None or heard > 120:
            # quiet for a while: the machine itself says whether it is still there
            from meshpipeline.adapters.mesh_execution import gce_vm as G
            vm = G.describe_vm(str(handle.get("vm") or "").rsplit("/", 1)[-1])
            if vm is not None and str(vm.get("status") or "") in _STOPPED:
                return {"state": "lost", "why": f"its VM has stopped ({vm.get('status')})"}
        return {"state": "running", "why": "sizing"}

    def collect(self, handle: dict, dest: Path) -> dict:
        from meshpipeline.sandbox.safe_extract import safe_extract_tar
        c = _coords(handle)
        bucket = _bucket()
        result = json.loads(bucket.blob(c.result_object_key).download_as_bytes())
        if not isinstance(result, dict):
            raise ValueError("the sizing's result is not a result")
        with tempfile.TemporaryDirectory(prefix="fea_sizing_back_") as tmp:
            archive = Path(tmp) / "result.tar.gz"
            bucket.blob(c.output_object_key).download_to_filename(str(archive))
            safe_extract_tar(path=archive, dest=str(dest))
        return result

    def release(self, handle: dict, *, why: str) -> None:
        try:
            from meshpipeline.adapters.mesh_execution import gce_vm as G
            name = str(handle.get("vm") or "")
            if name:
                G.delete_vm(G.VmReference(name), why=f"fea sizing: {why}")
        except Exception as exc:  # noqa: BLE001 - its own end and the sweep are the backstops
            logger.warning("fea sizing: its VM was not deleted (%s)", type(exc).__name__)
        _drop_exchange(str(handle.get("op") or ""))


__all__ = ["NEVER_HEARD_S", "RULE", "STALL_S", "VmSizing", "machines", "operation_key"]
