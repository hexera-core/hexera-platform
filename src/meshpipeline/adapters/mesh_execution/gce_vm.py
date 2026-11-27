# Responsibility: Run one mesh on a Compute Engine VM sized to fit it (contracts/mesh_machine), with the SAME image, service account and exchange contract as the Cloud Run mesh job - the VM runs the job's own container (meshpipeline.runtime.mesh_runner) with INPUT_URI / OUTPUT_URI / ENGINE / TIMEOUT, then deletes itself - or, held for its job (RUN-4), takes the job's next run and deletes itself once none comes.
# Boundaries: moves work only. Whether it runs here is decided above it (the workspace's mesh_machine.json); the result is read from the exchange exactly as a Cloud Run run's is (cloud_run_client._exchange).
# Collaborates with: cloud_run_client.py (dispatch, poll, collect), contracts/mesh_machine.py (which machine).
from __future__ import annotations

import hashlib
import logging
import re
import time
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

#: How long past the run's own timeout the mesh container may run on the VM before the VM stops it (the
#: pull, the upload of the input and the hand-back of the result fit in it); under LIFETIME_MARGIN_S, so the
#: VM ends itself well before Compute Engine's lifetime limit would.
CONTAINER_MARGIN_S = 1800
#: How many times the VM asks to delete itself, and the pause between asks, before it powers off instead.
SELF_DELETE_TRIES = 3

#: HELD FOR ITS JOB (RUN-4): a VM held by a job (contracts/mesh_execution.vm_hold) waits, its run done, for
#: the job's next run - a remesh, a step back down, a rerun - for as long as the job's meshing run holds it:
#: the run renews the hold (HOLDS_PREFIX/<job>, its time) every contracts VM_HOLD_RENEW_S while it runs, and
#: the VM ends itself once the hold is gone (released) or older than HOLD_STALE_S (its worker died). Its next
#: run is handed to it in its metadata and starts at once on a machine that is up, its image already pulled.
HOLD_STALE_S = 300
#: Where a job's hold on its VMs is kept, in the exchange bucket.
HOLDS_PREFIX = "vm-holds/"
#: Compute Engine's lifetime limit for a held VM, at least (it cannot be moved once the VM is up): room for
#: the job's runs after the first. A run that would not fit in what is left of it goes to a new VM.
HOLD_LIFETIME_S = 6 * 3600
#: How often a VM waiting for its job's next run looks at its metadata.
HOLD_POLL_S = 15

#: What the VM runs at boot: the job's own container, run by run. Each run is ONE metadata value
#: (`mesh-run`: id|input|output|engine|timeout|contract|snapshots|ranks|image, read in one request so a run
#: handed over meanwhile is never read half old, half new), with its workspace on disk, held to the run's
#: timeout plus CONTAINER_MARGIN_S. A VM held for its job (`hold-lease`: its hold's URL) then waits for the
#: job's next run and takes it, for as long as the hold is renewed; one with no holder ends after its one run,
#: as it always did. A hold that cannot be read keeps its last time: a passing network fault is not a release. THE VM ENDS
#: ITSELF however its last run ends - finished, failed, never started, or stopped at its deadline - whatever
#: became of the worker that started it (RUN-4): it asks to delete itself, and when that is refused (job
#: 6cb2d606, 28 Oct: its identity may not delete instances, and the silent `curl -sf` left a 16-core VM
#: billing for hours) it POWERS OFF, which needs no permission and stops the machine's billing. The worker's
#: sweep deletes a VM that stopped (vm_sweep.py); Compute Engine's lifetime limit is the last backstop.
STARTUP_SCRIPT = r"""#!/bin/bash
set -u
MD=http://metadata.google.internal/computeMetadata/v1
get() { curl -sf -H Metadata-Flavor:Google "$MD/$1"; }
say() { echo "[mesh-vm] $*"; logger -t mesh-vm "$*" 2>/dev/null || true; }
end_vm() {
  trap - EXIT
  NAME=$(get instance/name); ZONE=$(get instance/zone)
  for i in $(seq 1 __TRIES__); do
    TOKEN=$(get instance/service-accounts/default/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $TOKEN" \
      "https://compute.googleapis.com/compute/v1/$ZONE/instances/$NAME")
    case "$CODE" in
      2*|404) say "deleting itself (HTTP $CODE)"; sleep 300; break ;;
      *) say "self-delete refused (HTTP $CODE), try $i of __TRIES__"; sleep 20 ;;
    esac
  done
  say "powering off: a stopped VM bills no machine, and the worker's sweep deletes it"
  shutdown -h now || poweroff -f || echo o > /proc/sysrq-trigger
}
trap end_vm EXIT
mkdir -p /mnt/stateful_partition/work /mnt/stateful_partition/dockercfg
# the mesh container runs as the image's own user, not root: the workspace folder (its TMPDIR) must be
# writable by it, or OpenMPI cannot make its session folder and every parallel snappyHexMesh stops at
# once (job c06bb745: "/work/ompi.<host>.1000 - Permission denied")
chmod 1777 /mnt/stateful_partition/work
export HOME=/mnt/stateful_partition/dockercfg
PULLED=""
run_one() {
  IFS='|' read -r ID IN OUT ENG T CON SNAP RANKS IMG <<< "$1"
  case "$T" in ''|*[!0-9]*) T=0 ;; esac
  if [ "$IMG" != "$PULLED" ]; then
    REG=$(echo "$IMG" | cut -d/ -f1)
    docker-credential-gcr configure-docker --registries="$REG" >/dev/null 2>&1 || true
    for i in 1 2 3; do docker pull "$IMG" && break; sleep 15; done
    PULLED=$IMG
  fi
  find /mnt/stateful_partition/work -mindepth 1 -delete 2>/dev/null || true
  END=$(( $(date +%s) + T + __MARGIN__ ))
  say "run $ID: meshing"
# --cpus: every core of the VM, stated as the container's CPU QUOTA. The mesher sizes its parallel
# work by that quota (engines/determinism.snappy_ranks); without one it fell back to MESH_THREADS (8)
# and snappyHexMesh ran 8 ranks on a 16-, 32- or 64-core VM. Same machine type, same count.
docker run -d --rm --name mesh --cpus "$(nproc)" \
    -e INPUT_URI="$IN" -e OUTPUT_URI="$OUT" -e ENGINE="$ENG" -e TIMEOUT="$T" \
    -e HEXERA_MESH_CONTRACT="$CON" -e MESH_PROGRESSIVE_SNAPSHOT_ENABLED="$SNAP" \
    -e MESH_SNAPPY_RANKS="$RANKS" \
    -e TMPDIR=/work -v /mnt/stateful_partition/work:/work \
    "$IMG" python -m meshpipeline.runtime.mesh_runner || say "the mesh container did not start"
  while docker inspect mesh >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$END" ]; then
      say "past its deadline (the run's timeout + __MARGIN__ s): stopping the mesh"
      docker stop -t 60 mesh || docker kill mesh || true
      break
    fi
    sleep 30
  done
  say "the mesh container has exited"
}
LEASE=$(get instance/attributes/hold-lease || true)
HELD_AT=$(date +%s)
held() {
  [ -n "$LEASE" ] || return 1
  TOKEN=$(get instance/service-accounts/default/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
  BODY=$(curl -s -w '|%{http_code}' -H "Authorization: Bearer $TOKEN" "$LEASE" || true)
  case "${BODY##*|}" in
    200) L=${BODY%%|*}; case "$L" in ''|*[!0-9]*) ;; *) HELD_AT=$L ;; esac ;;
    404) say "its job has released it"; return 1 ;;
  esac
  if [ $(( $(date +%s) - HELD_AT )) -ge __STALE__ ]; then
    say "its job has not renewed its hold in __STALE__ s"; return 1
  fi
}
LAST=""
while true; do
  RUN=$(get instance/attributes/mesh-run || true)
  if [ -n "$RUN" ] && [ "${RUN%%|*}" != "$LAST" ]; then
    LAST=${RUN%%|*}
    run_one "$RUN"
    HELD_AT=$(date +%s)
    continue
  fi
  held || break
  sleep __POLL__
done
""".replace("__TRIES__", str(SELF_DELETE_TRIES)).replace("__MARGIN__", str(CONTAINER_MARGIN_S)).replace(
    "__POLL__", str(HOLD_POLL_S)).replace("__STALE__", str(HOLD_STALE_S))


@dataclass(frozen=True)
class VmReference:
    """projects/{p}/zones/{z}/instances/{name} - what a cancel deletes - and, for a VM HELD for its job (RUN-4),
    the run it was handed (`run`: run_id): one held VM takes several runs of its job."""
    instance_name: str
    run: str = ""

    @property
    def operation_name(self) -> str:
        # what the claim records as the provider reference: the instance, said as a GCE resource. Each claim's
        # reference is unique (uq_native_submission_claims_provider_reference), so a held VM's names its run too:
        # without it every pass after the first on the job's VM was refused at its claim (job c06bb745, 27 Nov:
        # "UniqueViolation ... provider_reference ... instances/mesh-big-fdf694fcb8a6c332ca27 already exists")
        return f"gce:{self.instance_name}" + (f"#{self.run}" if self.run else "")


def run_id(output_uri: str) -> str:
    """The id of one run (the `mesh-run` value's first field): its output's, so the same run handed over twice is
    run once."""
    return hashlib.sha256(output_uri.encode()).hexdigest()[:16]


def instance_name(operation_key: str, machine: str, holder: str = "") -> str:
    """A name derived from the operation (a replacement worker derives the same one) and the machine
    (a step up is a new VM): lower-case, under 63 characters. A VM HELD for its job (`holder`, RUN-4) is
    named by the job and the machine instead: every run of the job on that machine derives the same one."""
    ident = f"job:{holder}" if holder else operation_key
    digest = hashlib.sha256(f"{ident}|{machine}".encode()).hexdigest()[:20]
    return f"mesh-big-{digest}"


def job_label(holder: str) -> str:
    """The `job` label a held VM carries (a label value: lower-case letters, digits, - and _)."""
    return re.sub(r"[^a-z0-9_-]", "-", str(holder).lower())[:63]


def run_value(*, input_uri: str, output_uri: str, engine: str, timeout: int, contract: str, snapshots: bool,
              ranks: str, image: str) -> str:
    """The `mesh-run` metadata value the VM reads a run from (STARTUP_SCRIPT), one request for all of it. Its
    id is the run's output: the same run handed over twice is run once."""
    rid = run_id(output_uri)
    return "|".join([rid, input_uri, output_uri, engine, str(int(timeout)), contract,
                     "true" if snapshots else "false", ranks, image])


def _job_container() -> tuple[str, str]:
    """(image, service account) of the deployment's Cloud Run mesh job: the VM runs exactly what the
    job runs, as the identity the job runs as."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    name = f"projects/{provcfg.GCP_PROJECT_ID}/locations/{provcfg.GCP_REGION}/jobs/{provcfg.CLOUDRUN_JOB}"
    resp = _rq.get(f"https://run.googleapis.com/v2/{name}", headers={"Authorization": f"Bearer {_access_token()}"},
                   timeout=60)
    if resp.status_code == 403:
        # said as what it is, with the grant (deploy/gcp create-worker-fleet.sh 2c; access_check at start)
        raise PermissionError(f"HTTP 403: this worker may not read the mesh job {provcfg.CLOUDRUN_JOB} (run.jobs.get), "
                              "which the big-machine route needs - grant it roles/run.viewer on the job")
    resp.raise_for_status()
    tpl = (resp.json().get("template") or {}).get("template") or {}
    image = str(((tpl.get("containers") or [{}])[0]).get("image") or "")
    account = str(tpl.get("serviceAccount") or "")
    if not image:
        raise RuntimeError("the Cloud Run mesh job names no image")
    return image, account


def run_metadata(*, image: str, input_uri: str, output_uri: str, engine: str, timeout: int) -> dict:
    """The metadata of one run, as the VM reads it (`mesh-run`) and as an operator and the sweep read it."""
    from meshpipeline.contracts.mesh_execution import MESH_EXCHANGE_VERSION

    # THE SAME SWITCH THE CLOUD RUN ROUTE PASSES, for the same reason: the VM runs the job's own
    # container, which carries no env of its own, so a setting the mesher reads reaches it only
    # from here. The two routes must agree, or a mesh would publish frames on one path and not the
    # other depending on how big it was - which reads as a bug in the feature rather than in us.
    #
    # So it is read from the SETTING THAT OWNS IT (settings/mesh_snapshot), not from the sibling
    # adapter that happens to dispatch the other route: agreement between the two routes is the
    # invariant, and an underscore import across siblings makes it brittle rather than enforcing
    # it. Guarded and lazy, because a progress nicety must never be why a mesh is not submitted:
    # the setting raises on a malformed boolean, and that must not take a dispatch with it.
    try:
        from meshpipeline.settings.mesh_snapshot import snapshots_enabled_at_dispatch
        snapshots = snapshots_enabled_at_dispatch()
    except Exception:  # noqa: BLE001 - unreadable settings mean no frames, never no mesh
        snapshots = False
    # The rank pin rides with the frame switch, for the reason stated on the Cloud Run route: the
    # frame is only read from a serial run, and the module that decides rank count runs in the
    # container. Sending one without the other is how a deployment ends up holding both settings
    # and still skipping every frame.
    try:
        import meshpipeline.engines.determinism as dcfg
        ranks = str(dcfg.MESH_SNAPPY_RANKS or "")
    except Exception:  # noqa: BLE001 - a sizing hint never stops a mesh being submitted
        ranks = ""
    meta = {"mesh-image": image, "input-uri": input_uri, "output-uri": output_uri, "engine": engine,
            "timeout": str(int(timeout)), "mesh-contract": str(MESH_EXCHANGE_VERSION),
            "mesh-snapshots": "true" if snapshots else "false",
            "mesh-snappy-ranks": ranks}
    meta["mesh-run"] = run_value(input_uri=input_uri, output_uri=output_uri, engine=engine, timeout=timeout,
                                 contract=str(MESH_EXCHANGE_VERSION), snapshots=snapshots, ranks=ranks, image=image)
    return meta


def instance_body(*, name: str, zone: str, machine: str, image: str, account: str, input_uri: str,
                  output_uri: str, engine: str, timeout: int, disk_gb: int, holder: str = "") -> dict:
    """The instances.insert body (pure: tests read it). A VM held for its job (`holder`) waits for the job's
    next run while the job holds it (hold_url) and lives at least HOLD_LIFETIME_S; one with no holder ends with
    its run."""
    meta = {**run_metadata(image=image, input_uri=input_uri, output_uri=output_uri, engine=engine,
                           timeout=timeout),
            "hold-lease": hold_url(holder) if holder else "",
            "startup-script": STARTUP_SCRIPT}
    labels = {"purpose": LABEL, "engine": engine.lower()[:60]}
    if holder:
        labels["job"] = job_label(holder)
    lifetime = int(timeout) + LIFETIME_MARGIN_S
    if holder:
        lifetime = max(lifetime, HOLD_LIFETIME_S)
    body: dict = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{machine}",
        "labels": labels,
        "disks": [{"boot": True, "autoDelete": True,
                   "initializeParams": {"sourceImage": BOOT_IMAGE, "diskSizeGb": str(int(disk_gb)),
                                        "diskType": f"zones/{zone}/diskTypes/pd-balanced"}}],
        "networkInterfaces": [{"network": "global/networks/default",
                               "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]}],
        "metadata": {"items": [{"key": k, "value": v} for k, v in meta.items()]},
        "scheduling": {"automaticRestart": False, "onHostMaintenance": "TERMINATE",
                       "instanceTerminationAction": "DELETE",
                       "maxRunDuration": {"seconds": str(lifetime)}},
    }
    if account:
        body["serviceAccounts"] = [{"email": account, "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
    return body


#: The states of a VM that may still take its job's next run.
LIVE = frozenset({"PROVISIONING", "STAGING", "RUNNING"})
#: How long a worker waits for a held VM it could not reuse to be gone before starting its replacement.
REPLACE_WAIT_S = 300


def _ref(vm: dict) -> VmReference:
    return VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{vm.get('zone')}/instances/{vm.get('name')}")


def _lifetime_left_s(vm: dict, now: float) -> float:
    """What is left of a VM's Compute Engine lifetime limit (maxRunDuration), in seconds; 0 when unreadable."""
    from datetime import datetime
    try:
        created = datetime.fromisoformat(str(vm.get("creationTimestamp"))).timestamp()
        limit = float(((vm.get("scheduling") or {}).get("maxRunDuration") or {}).get("seconds") or 0)
    except (TypeError, ValueError):
        return 0.0
    return created + limit - now


#: Renewals this close together keep a hold UNBROKEN (renew_job_hold): well inside HOLD_STALE_S, so a VM that
#: saw no gap this long cannot have ended itself.
HOLD_UNBROKEN_S = HOLD_STALE_S - 2 * HOLD_POLL_S
#: The jobs this process holds VMs for: only they renew a hold (a job that never took a VM writes nothing).
_HELD: set[str] = set()


def hold_object(holder: str) -> str:
    return f"{HOLDS_PREFIX}{job_label(holder)}"


def hold_url(holder: str) -> str:
    """Where the VM reads its job's hold (the JSON API's media URL: the VM has curl and a token, no SDK)."""
    from urllib.parse import quote
    return (f"https://storage.googleapis.com/storage/v1/b/{provcfg.GCP_MESH_BUCKET}/o/"
            f"{quote(hold_object(holder), safe='')}?alt=media")


def _read_hold(holder: str) -> tuple[float, float] | None:
    """(when the hold was last renewed, since when it has been unbroken), or None when there is none. A read
    that fails for any other reason RAISES: unreadable is not released."""
    from meshpipeline.adapters.mesh_execution.gcs_exchange import download_exchange_bytes
    raw = download_exchange_bytes(hold_object(holder), bucket=str(provcfg.GCP_MESH_BUCKET or ""))
    if raw is None:
        return None
    at, _, since = raw.decode("utf-8", "replace").partition("|")
    try:
        return float(at), float(since or at)
    except ValueError:
        return None


def _read_hold_or_none(holder: str) -> tuple[float, float] | None:
    try:
        return _read_hold(holder)
    except Exception as exc:  # noqa: BLE001 - not known: no hold to continue (a VM is then not reused)
        logger.warning("the hold of job %s could not be read (%s)", holder, type(exc).__name__)
        return None


def renew_job_hold(holder: str, *, now: float | None = None, force: bool = False) -> None:
    """The job's meshing run still holds its VMs: write `<now>|<since>` to its hold, `since` carried over while
    the renewals are unbroken (HOLD_UNBROKEN_S apart at most). Only for a job that holds VMs: one that took a
    VM here (`force`), or whose hold is already there - a run adopted from a worker that handed the job on."""
    now = time.time() if now is None else now
    prev = _read_hold_or_none(holder)
    if not (force or holder in _HELD or prev is not None):
        return
    _HELD.add(holder)
    from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
    since = prev[1] if prev is not None and now - prev[0] <= HOLD_UNBROKEN_S else now
    storage_client().bucket(provcfg.GCP_MESH_BUCKET).blob(hold_object(holder)).upload_from_string(
        f"{int(now)}|{int(since)}", content_type="text/plain")


def hold_since(holder: str, *, now: float | None = None) -> float | None:
    """Since when the job's hold on its VMs has been unbroken, or None when it is gone or stale."""
    now = time.time() if now is None else now
    hold = _read_hold_or_none(holder)
    if hold is None or now - hold[0] > HOLD_UNBROKEN_S:
        return None
    return hold[1]


def _last_run(vm: dict) -> tuple[bool, float | None]:
    """(whether the VM's current run is over - its result is written, or it was collected and its exchange
    released, input included - and seconds since its beacon was last written, None when unknown), read from the
    exchange its metadata names. Never raises: unreadable reads as busy."""
    meta = {str(i.get("key")): str(i.get("value") or "") for i in ((vm.get("metadata") or {}).get("items") or [])}
    uri, in_uri = meta.get("output-uri", ""), meta.get("input-uri", "")
    if not uri.startswith("gs://"):
        return False, None
    bucket_name, _, key = uri[5:].partition("/")
    in_key = in_uri[5:].partition("/")[2] if in_uri.startswith(f"gs://{bucket_name}/") else ""
    try:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
        from meshpipeline.artifact_keys import progress_key_beside, result_key_beside
        bucket = storage_client().bucket(bucket_name)
        done = bool(bucket.blob(result_key_beside(key)).exists()) or bool(
            in_key and not bucket.blob(in_key).exists())
        beacon = bucket.get_blob(progress_key_beside(key))
        updated = getattr(beacon, "updated", None) if beacon is not None else None
        return done, None if updated is None else max(0.0, time.time() - updated.timestamp())
    except Exception as exc:  # noqa: BLE001 - not known is busy: the VM is left alone
        logger.warning("mesh VM %s: its last run could not be read (%s)", vm.get("name"), type(exc).__name__)
        return False, None


def reusable(vm: dict, *, timeout: int, now: float | None = None, done: bool = True,
             idle_s: float | None = 0.0, held_since: float | None = 0.0) -> bool:
    """Whether a held VM may take a run of `timeout` seconds (pure: tests read it): it is up, its last run is
    over (`done`: never one still meshing - a run of a job taken over must not queue behind, or kill, a pass
    left to finish), it is still waiting for its next run (its job's hold unbroken, `held_since`, since its
    last run ended, `idle_s` ago by its beacon: a VM that saw its hold lapse may be ending itself, and a run it
    never reads would be lost to the never-heard wait), and the run fits, with its margin, in what is left of
    its lifetime limit."""
    now = time.time() if now is None else now
    return (str(vm.get("status") or "") in LIVE and done
            and idle_s is not None and held_since is not None and held_since <= now - idle_s
            and _lifetime_left_s(vm, now) >= int(timeout) + CONTAINER_MARGIN_S + 600)


def _hand_over(vm: dict, meta: dict, session=None) -> bool:
    """Give a held VM its job's next run: its metadata is set to the run's (the VM reads `mesh-run` and
    starts it). True once Compute Engine took the change; a request whose answer never came is
    INDETERMINATE (the VM may be meshing it)."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    s = session or _rq
    path = _ref(vm).instance_name
    for _ in range(2):
        md = vm.get("metadata") or {}
        items = {str(i.get("key")): i.get("value") for i in (md.get("items") or [])}
        items.update(meta)
        body = {"fingerprint": md.get("fingerprint", ""),
                "items": [{"key": k, "value": v} for k, v in items.items()]}
        try:
            resp = s.post(f"https://compute.googleapis.com/compute/v1/{path}/setMetadata", json=body,
                          headers={"Authorization": f"Bearer {_access_token()}"}, timeout=60)
        except _rq.exceptions.RequestException as exc:
            raise SubmissionIndeterminate(
                f"handing a run to VM {vm.get('name')} failed in transport ({type(exc).__name__})") from None
        if 200 <= resp.status_code < 300:
            return True
        if resp.status_code != 412:            # 412: its metadata moved meanwhile - read it again, once
            logger.warning("mesh VM %s did not take the run (HTTP %s)", vm.get("name"), resp.status_code)
            return False
        again = describe_vm(str(vm.get("name")), session=session)
        if again is None:
            return False
        vm = again
    return False


def _wait_gone(name: str, limit_s: int = REPLACE_WAIT_S) -> bool:
    """Whether the VM of this name is gone, waited for up to `limit_s`."""
    waited = 0
    while describe_vm(name) is not None:
        if waited >= limit_s:
            return False
        time.sleep(10)
        waited += 10
    return True


def _held_vm(name: str, holder: str, machine: str, *, meta: dict, timeout: int,
             disk_gb: int = 0) -> tuple[VmReference | None, bool]:
    """THE JOB'S VM, TAKEN AGAIN (RUN-4): (the VM held for this job on this machine, when it took the run;
    whether its name is free for a new one). The job's VM on ANOTHER machine - the run before a step up or
    back down - is released once its run is over: one VM per job. A VM still meshing is never deleted (a job
    taken over may have a pass left to finish on it), and never handed a run to queue behind that pass."""
    label = job_label(holder)
    for other in list_vms():
        if (other.get("labels") or {}).get("job") == label and other.get("name") != name:
            if str(other.get("status") or "") not in LIVE or _last_run(other)[0]:
                delete_vm(_ref(other), why=f"its job's next run is on a {machine}")
    vm = describe_vm(name)
    if vm is None:
        return None, True
    up = str(vm.get("status") or "") in LIVE
    done, idle_s = _last_run(vm) if up else (True, None)
    since = hold_since(holder) if up and done else None
    disk = int(((vm.get("disks") or [{}])[0]).get("diskSizeGb") or 0)
    roomy = disk >= disk_gb or not vm.get("disks")              # a later pass may need a bigger workspace
    if roomy and reusable(vm, timeout=timeout, done=done, idle_s=idle_s, held_since=since) \
            and _hand_over(vm, meta):
        logger.warning("mesh for %s runs on the %s VM its job holds (%s, %s): no boot, no image pull",
                       meta.get("engine"), machine, name, RULE)
        return _ref(vm), False
    if up and not done:
        return None, False
    delete_vm(_ref(vm), why="it could not take its job's next run (its hold lapsed, stopped, or too little "
                            "lifetime left)")
    return None, _wait_gone(name)


class _NameTaken(Exception):
    """A held VM's name is taken by a VM that did not take this run."""


def _insert(*, name: str, holder: str, machine: str, image: str, account: str, input_uri: str,
            output_uri: str, engine: str, timeout: int, disk_gb: int) -> VmReference:
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    last = ""
    for suffix in ZONE_SUFFIXES:
        zone = f"{provcfg.GCP_REGION}-{suffix}"
        body = instance_body(name=name, zone=zone, machine=machine, image=image, account=account,
                             input_uri=input_uri, output_uri=output_uri, engine=engine, timeout=timeout,
                             disk_gb=disk_gb, holder=holder)
        url = f"https://compute.googleapis.com/compute/v1/projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances"
        try:
            resp = _rq.post(url, json=body, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=60)
        except _rq.exceptions.RequestException as exc:
            raise SubmissionIndeterminate(
                f"{engine}: the VM request failed in transport ({type(exc).__name__}); it may exist") from None
        if 200 <= resp.status_code < 300:
            ref = VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances/{name}")
            logger.warning("mesh for %s runs on a %s VM in %s (%s, %s%s)", engine, machine, zone, name, RULE,
                           f", held for job {holder}" if holder else "")
            return ref
        if resp.status_code == 409:
            if holder:
                # a held VM is named by its job, not its run: the VM there is not running THIS run
                raise _NameTaken(name)
            # ALREADY THERE: a previous worker started this very run (same operation, same machine)
            return VmReference(f"projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances/{name}")
        text = resp.text[:400]
        last = f"HTTP {resp.status_code} in {zone}"
        if any(k in text for k in ("ZONE_RESOURCE_POOL_EXHAUSTED", "does not exist in zone", "not available",
                                   "RESOURCE_AVAILABILITY", "UNSUPPORTED_OPERATION")):
            continue
        break
    raise RuntimeError(f"no {machine} VM could be started ({last})")


def trigger_vm(*, input_uri: str, output_uri: str, engine: str, timeout: int, operation_key: str,
               decision: dict, holder: str = "") -> VmReference:
    """Start the run on a VM. A run of a job that holds a VM on this machine is handed to it (RUN-4);
    otherwise one is started, each zone of the region tried in turn when one has no such machine free. When
    the job's VM is busy, or its name is not yet free, the run gets a VM of its own that ends with it. A
    request whose answer never came is INDETERMINATE (the VM may exist), never "not started"."""
    machine = str(decision.get("machine") or "")
    if not machine:
        raise RuntimeError("no machine named for a Compute Engine run")
    image, account = _job_container()
    disk_gb = max(MIN_DISK_GB, int(4 * float(decision.get("workspace_gib") or 0.0)) + 50)
    from functools import partial
    insert = partial(_insert, machine=machine, image=image, account=account, input_uri=input_uri,
                     output_uri=output_uri, engine=engine, timeout=timeout, disk_gb=disk_gb)
    unheld = instance_name(operation_key, machine)
    if not holder:
        return insert(name=unheld, holder="")
    name = instance_name(operation_key, machine, holder)
    # the run holds the job's VMs from now (a broken hold restarts its `since`: a VM idle across the gap is not
    # handed the run)
    renew_job_hold(holder, force=True)
    meta = run_metadata(image=image, input_uri=input_uri, output_uri=output_uri, engine=engine, timeout=timeout)
    held, free = _held_vm(name, holder, machine, meta=meta, timeout=timeout, disk_gb=disk_gb)
    # the job's VM takes many runs: each one's reference names its run (VmReference.operation_name)
    from dataclasses import replace
    if held is not None:
        return replace(held, run=run_id(output_uri))
    if free:
        try:
            return replace(insert(name=name, holder=holder), run=run_id(output_uri))
        except _NameTaken:
            pass
    logger.warning("mesh for %s runs on a %s VM of its own: the one job %s holds is busy or not yet gone",
                   engine, machine, holder)
    return insert(name=unheld, holder="")


def _zones() -> list[str]:
    return [f"{provcfg.GCP_REGION}-{s}" for s in ZONE_SUFFIXES]


def list_vms(session=None) -> list[dict]:
    """Every VM this route started in the deployment's project and region (LABEL), as Compute Engine
    describes it (name, zone, status, creationTimestamp, metadata). A zone that cannot be read is skipped,
    said; never raises."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    s = session or _rq
    out: list[dict] = []
    for zone in _zones():
        url = (f"https://compute.googleapis.com/compute/v1/projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}"
               f"/instances?filter=labels.purpose%3D{LABEL}")
        try:
            resp = s.get(url, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - one zone unread is one zone not swept this time
            logger.warning("mesh VMs in %s not listed: %s", zone, type(exc).__name__)
            continue
        for item in (resp.json() or {}).get("items") or []:
            out.append({**item, "zone": zone})
    return out


def describe_vm(name: str, session=None) -> dict | None:
    """The VM of this name in the region's zones as Compute Engine describes it (with its `zone`), or None."""
    import requests as _rq

    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
    s = session or _rq
    for zone in _zones():
        path = f"projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances/{name}"
        try:
            resp = s.get(f"https://compute.googleapis.com/compute/v1/{path}",
                         headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30)
        except Exception as exc:  # noqa: BLE001 - not found this time; the sweep finds it later
            logger.info("mesh VM %s not looked up in %s: %s", name, zone, type(exc).__name__)
            continue
        if resp.status_code == 200:
            return {**(resp.json() or {}), "name": name, "zone": zone}
    return None


def find_vm(name: str, session=None) -> VmReference | None:
    """The VM of this name in the region's zones, or None (a worker collecting a run another worker started
    finds the VM by the name both derive: instance_name)."""
    vm = describe_vm(name, session=session)
    return None if vm is None else _ref(vm)


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


def release_job_vms(holder: str, *, why: str = "its job has ended") -> int:
    """Delete every VM held for job `holder` (RUN-4): the job has ended, nothing will run on them again.
    Best effort, never raises; how many were deleted."""
    label = job_label(holder)
    _HELD.discard(holder)
    try:
        from meshpipeline.adapters.mesh_execution.gcs_exchange import storage_client
        storage_client().bucket(provcfg.GCP_MESH_BUCKET).blob(hold_object(holder)).delete()
    except Exception as exc:  # noqa: BLE001 - none to release, or not now: the VMs are deleted below anyway
        logger.info("the hold of job %s was not deleted (%s)", holder, type(exc).__name__)
    gone = 0
    for vm in list_vms():
        if (vm.get("labels") or {}).get("job") == label and delete_vm(_ref(vm), why=why):
            gone += 1
    return gone


__all__ = ["CONTAINER_MARGIN_S", "HOLDS_PREFIX", "HOLD_LIFETIME_S", "HOLD_STALE_S", "LABEL", "SELF_DELETE_TRIES", "STARTUP_SCRIPT",
           "VmReference", "delete_vm", "describe_vm", "find_vm", "instance_body", "instance_name", "job_label",
           "hold_object", "hold_since", "hold_url", "list_vms", "release_job_vms", "renew_job_hold", "reusable",
           "run_metadata", "run_value", "trigger_vm"]
