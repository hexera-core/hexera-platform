# Responsibility: Run one mesh on a Compute Engine VM sized to fit it (contracts/mesh_machine), with the SAME image, service account and exchange contract as the Cloud Run mesh job - the VM runs the job's own container (meshpipeline.runtime.mesh_runner) with INPUT_URI / OUTPUT_URI / ENGINE / TIMEOUT, then deletes itself.
# Boundaries: moves work only. Whether it runs here is decided above it (the workspace's mesh_machine.json); the result is read from the exchange exactly as a Cloud Run run's is (cloud_run_client._exchange).
# Collaborates with: cloud_run_client.py (dispatch, poll, collect), contracts/mesh_machine.py (which machine).
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate

logger = logging.getLogger(__name__)

#: The zones tried, in order, in the deployment's region (a zone out of a machine type moves on).
ZONE_SUFFIXES = ("a", "b", "c", "f")
#: The VM's boot image: Container-Optimized OS runs the mesh container and nothing else.
BOOT_IMAGE = "projects/cos-cloud/global/images/family/cos-stable"
#: Past the run's own timeout, how long the VM may live before Compute Engine deletes it anyway (it
#: boots, pulls the image and hands back the result in this time). Nothing is left running unpaid-for.
LIFETIME_MARGIN_S = 3600
#: The VM's disk: the workspace lives here (on Cloud Run it is in memory), at least this many GB.
MIN_DISK_GB = 200
#: The rule this route is (docs/RULES.md): the VM is the job.
RULE = "RUN-4"
#: Every VM this route starts carries this label, so an operator finds them all.
LABEL = "mesh-bigjob"

#: What the VM runs at boot: the job's own container with the run's four values from its metadata, its
#: workspace on disk, then it deletes itself (Compute Engine's lifetime limit is the backstop).
STARTUP_SCRIPT = r"""#!/bin/bash
set -u
MD=http://metadata.google.internal/computeMetadata/v1
get() { curl -sf -H Metadata-Flavor:Google "$MD/$1"; }
IMG=$(get instance/attributes/mesh-image)
mkdir -p /mnt/stateful_partition/work /mnt/stateful_partition/dockercfg
export HOME=/mnt/stateful_partition/dockercfg
REG=$(echo "$IMG" | cut -d/ -f1)
docker-credential-gcr configure-docker --registries="$REG" >/dev/null 2>&1 || true
for i in 1 2 3; do docker pull "$IMG" && break; sleep 15; done
docker run --rm --name mesh \
  -e INPUT_URI="$(get instance/attributes/input-uri)" \
  -e OUTPUT_URI="$(get instance/attributes/output-uri)" \
  -e ENGINE="$(get instance/attributes/engine)" \
  -e TIMEOUT="$(get instance/attributes/timeout)" \
  -e TMPDIR=/work -v /mnt/stateful_partition/work:/work \
  "$IMG" python -m meshpipeline.runtime.mesh_runner
TOKEN=$(get instance/service-accounts/default/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
  "https://compute.googleapis.com/compute/v1/$(get instance/zone)/instances/$(get instance/name)" >/dev/null
"""


@dataclass(frozen=True)
class VmReference:
    """projects/{p}/zones/{z}/instances/{name} - what a cancel deletes."""
    instance_name: str

    @property
    def operation_name(self) -> str:
        # what the claim records as the provider reference: the instance, said as a GCE resource
        return f"gce:{self.instance_name}"


def instance_name(operation_key: str, machine: str) -> str:
    """A name derived from the operation (a replacement worker derives the same one) and the machine
    (a step up is a new VM): lower-case, under 63 characters."""
    digest = hashlib.sha256(f"{operation_key}|{machine}".encode()).hexdigest()[:20]
    return f"mesh-big-{digest}"


def _job_container() -> tuple[str, str]:
    """(image, service account) of the deployment's Cloud Run mesh job: the VM runs exactly what the
    job runs, as the identity the job runs as."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    name = f"projects/{provcfg.GCP_PROJECT_ID}/locations/{provcfg.GCP_REGION}/jobs/{provcfg.CLOUDRUN_JOB}"
    resp = _rq.get(f"https://run.googleapis.com/v2/{name}", headers={"Authorization": f"Bearer {_access_token()}"},
                   timeout=60)
    resp.raise_for_status()
    tpl = (resp.json().get("template") or {}).get("template") or {}
    image = str(((tpl.get("containers") or [{}])[0]).get("image") or "")
    account = str(tpl.get("serviceAccount") or "")
    if not image:
        raise RuntimeError("the Cloud Run mesh job names no image")
    return image, account


def instance_body(*, name: str, zone: str, machine: str, image: str, account: str, input_uri: str,
                  output_uri: str, engine: str, timeout: int, disk_gb: int) -> dict:
    """The instances.insert body (pure: tests read it)."""
    meta = {"mesh-image": image, "input-uri": input_uri, "output-uri": output_uri, "engine": engine,
            "timeout": str(int(timeout)), "startup-script": STARTUP_SCRIPT}
    body: dict = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{machine}",
        "labels": {"purpose": LABEL, "engine": engine.lower()[:60]},
        "disks": [{"boot": True, "autoDelete": True,
                   "initializeParams": {"sourceImage": BOOT_IMAGE, "diskSizeGb": str(int(disk_gb)),
                                        "diskType": f"zones/{zone}/diskTypes/pd-balanced"}}],
        "networkInterfaces": [{"network": "global/networks/default",
                               "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]}],
        "metadata": {"items": [{"key": k, "value": v} for k, v in meta.items()]},
        "scheduling": {"automaticRestart": False, "onHostMaintenance": "TERMINATE",
                       "instanceTerminationAction": "DELETE",
                       "maxRunDuration": {"seconds": str(int(timeout) + LIFETIME_MARGIN_S)}},
    }
    if account:
        body["serviceAccounts"] = [{"email": account, "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
    return body


def trigger_vm(*, input_uri: str, output_uri: str, engine: str, timeout: int, operation_key: str,
               decision: dict) -> VmReference:
    """Start the VM. Each zone of the region is tried in turn when one has no such machine free; a
    request whose answer never came is INDETERMINATE (the VM may exist), never "not started"."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    machine = str(decision.get("machine") or "")
    if not machine:
        raise RuntimeError("no machine named for a Compute Engine run")
    image, account = _job_container()
    name = instance_name(operation_key, machine)
    disk_gb = max(MIN_DISK_GB, int(4 * float(decision.get("workspace_gib") or 0.0)) + 50)
    last = ""
    for suffix in ZONE_SUFFIXES:
        zone = f"{provcfg.GCP_REGION}-{suffix}"
        body = instance_body(name=name, zone=zone, machine=machine, image=image, account=account,
                             input_uri=input_uri, output_uri=output_uri, engine=engine, timeout=timeout,
                             disk_gb=disk_gb)
        url = f"https://compute.googleapis.com/compute/v1/projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances"
        try:
            resp = _rq.post(url, json=body, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=60)
        except _rq.exceptions.RequestException as exc:
            raise SubmissionIndeterminate(
                f"{engine}: the VM request failed in transport ({type(exc).__name__}); it may exist") from None
        if 200 <= resp.status_code < 300:
            ref = VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances/{name}")
            logger.warning("mesh for %s runs on a %s VM in %s (%s, %s)", engine, machine, zone, name, RULE)
            return ref
        if resp.status_code == 409:
            # ALREADY THERE: a previous worker started this very run (same operation, same machine)
            return VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances/{name}")
        text = resp.text[:400]
        last = f"HTTP {resp.status_code} in {zone}"
        if any(k in text for k in ("ZONE_RESOURCE_POOL_EXHAUSTED", "does not exist in zone", "not available",
                                   "RESOURCE_AVAILABILITY", "UNSUPPORTED_OPERATION")):
            continue
        break
    raise RuntimeError(f"no {machine} VM could be started ({last})")


def delete_vm(ref: VmReference, *, why: str) -> bool:
    """Delete a VM whose result nobody will collect. Best effort, never raises."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    try:
        resp = _rq.delete(f"https://compute.googleapis.com/compute/v1/{ref.instance_name}",
                          headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30)
    except Exception as exc:  # noqa: BLE001 - best effort; the VM's own lifetime limit deletes it anyway
        logger.warning("mesh VM %s NOT deleted (%s): %s", ref.instance_name.rsplit("/", 1)[-1], why,
                       type(exc).__name__)
        return False
    ok = 200 <= resp.status_code < 300 or resp.status_code == 404
    logger.warning("mesh VM %s %s (%s)", ref.instance_name.rsplit("/", 1)[-1],
                   "deleted" if ok else f"NOT deleted (HTTP {resp.status_code})", why)
    return ok


__all__ = ["LABEL", "STARTUP_SCRIPT", "VmReference", "delete_vm", "instance_body", "instance_name", "trigger_vm"]
