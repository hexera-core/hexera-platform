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
#:
#: WHAT THE MARGIN COVERS, AND WHAT IT DOES NOT. It covers everything either side of the ENGINE:
#: the boot, the image pull (three attempts, 15 s apart), and then `engines/dispatch`'s own two
#: steps after the engine has returned - `return_scope` deciding what rides home and
#: `gcs_exchange` writing and uploading the archive. It does NOT cover any part of the engine's
#: own run - that is `timeout`, and `lifetime_seconds` below is the only place the two are added
#: together. The boundary index USED to fall between the two, counted by neither; it is now inside
#: the engine's deadline (engines/cfmesh_conjugate/native._boundary_indexes takes `budget_left_s`).
#:
#: SO `timeout` HAS TO BE THE WHOLE RUN'S BUDGET, and for one engine it was not. Measured by
#: arithmetic on 2026-11-12: `cfmesh_conjugate` handed its run_timeout to `conjugate._foam` PER
#: STAGE across an eight-stage parallel chain, and foamToVTK took another EXPORT_TIMEOUT_S
#: (7,200 s) after it, while this line spent the same number ONCE. At the then-clamped 9,000 s
#: one stage's allowance was 71% of the 12,600 s the VM was given to live and the export's alone
#: was 57% of it, so a chain that was fine stage by stage would be deleted mid-run holding a
#: finished mesh. That engine now spends its budget as a deadline (conjugate.build), which is what
#: makes this sum mean something; an engine whose run can exceed its own `timeout` cannot be run
#: on this route safely, and `lifetime_seconds` is where that contract is written down.
#:
#: IT IS NOT THE WHOLE MARGIN ANY MORE, AND THE CLAIM THAT IT COULD BE WAS WRONG. This line used
#: to read "IT IS A CONSTANT, and the thing that makes a constant legitimate is that the returned
#: archive is bounded before the mesh is built (budget.refuse_for_return), so the send-home half
#: does not grow with the mesh." The ARCHIVE is bounded - 5.5% of the written mesh, 31 GiB at the
#: plate (budget.ARCHIVE_SHARE_OF_WRITTEN) - and the WORK of producing it is not. `return_scope`
#: says so in its own header: "Every byte of the workspace is compressed here to decide, and then
#: again by the archiver to send." So the send-home half grows with the MESH, not with the
#: archive, and what the constant covers is only the part that does not: the boot, the image pull
#: (three attempts, 15 s apart), the upload of a bounded archive, and the slack around them. On
#: the coupon's 1.12 GB workspace that whole return was 33.8 s on 32 cores and 242.9 s on one, so
#: 3,600 s is the generous fixed floor it always was for a small workspace and stays exactly that:
#: `return_margin_s(0.0)` is this number and nothing moves for any run that reached this route
#: before.
LIFETIME_MARGIN_S = 3600
#: WHAT THE RETURN COSTS A GiB, measured, on 32 cores - the three passes over every byte of the
#: workspace that stand between the mesh being finished and the VM being allowed to die. The
#: figures are `engines/return_scope`'s and `parallel_gzip`'s own, on the coupon's
#: 1,120,883,923-byte (1.0439 GiB) workspace:
#:
#:     the cheap measure, level 1   4.3 s on 32 cores   =   4.12 s a GiB
#:     the exact measure, level 6  22.1 s on 32 cores   =  21.17 s a GiB
#:     the archive,       level 6   7.4 s on 32 cores   =   7.09 s a GiB
#:                                                        --------------
#:                                                         32.38 s a GiB
#:
#: ALL THREE, because all three run on the workspace this route exists for. `scope_return` escalates
#: - raw bytes, then FAST_LEVEL, then MEASURE_LEVEL - and only a workspace that CLEARLY fits settles
#: early. At the Corintis plate the raw bytes are ten times the extraction ceiling and the level-1
#: estimate of two ASCII polyMeshes is still several times it, so both measures run and then the
#: archiver runs. This is the worst path and it is the path the plate takes.
#:
#: NOT THE UPLOAD, which stays inside the fixed floor above: the archive is bounded at 5.5% of the
#: written mesh, and no rate for it has been measured on this route. Said rather than priced.
RETURN_SECONDS_PER_GIB = 32.4
#: ...AND 32 CORES IS A PROPERTY OF THE MEASUREMENT, NOT OF THE MACHINE BEING PRICED. The line
#: above is a 32-core figure and it was charged to every VM this route starts, whatever it has -
#: and the header two lines up records the spread on the same coupon in its own words: "that whole
#: return was 33.8 s on 32 cores and 242.9 s on one", 7.2x. `return_scope.estimate_archive_bytes`
#: and `parallel_gzip` both spread over `job_cpus()`, so the cores are what the rate is OF.
#:
#: THE TWO RECORDED POINTS, AND THE ONLY MODEL THEY SUPPORT. A speedup of 7.19x for 32x the cores
#: is not linear, so a per-core rate cannot be a division: part of the return does not divide.
#: Solving 1/7.19 = s + (1 - s)/32 for the serial share gives s = 0.1113, and
#: 232.7 s/GiB on one core x (0.1113 + 0.8887/32) = 32.36 s/GiB - which IS the constant above, so
#: the model is the two measurements and nothing else.
#:
#:     cores    s/GiB     against the 32-core figure
#:         1    232.7          7.18 x
#:         8     51.8          1.60 x
#:        16     38.8          1.20 x
#:        32     32.4          1.00 x
#:        64     29.1          0.90 x
#:
#: IT ONLY EVER CHARGES MORE, which is deliberate and is the difference between a correction and a
#: re-opening. `seconds_per_gib` takes the HIGHER of the measured 32-core figure and the figure
#: for this machine's cores, so:
#:   * a VM with fewer cores than the measurement is charged what its own cores cost - the case
#:     this exists for, an FEA plan on an 8-core box priced at 1.60x what it used to be;
#:   * a VM with MORE is still charged the 32-core figure, which is conservative, and every number
#:     already settled on this route stays byte-identical - the Corintis plate's n2-highmem-64
#:     keeps return_margin_s = 22,831 s and lifetime_seconds = 44,431 s rather than being re-priced
#:     downwards on the strength of a two-point model.
#: A caller that does not know the machine's cores gets the 32-core figure, exactly as before.
RETURN_SECONDS_PER_GIB_ONE_CORE = 232.7
#: The share of the return that does not divide across cores, from the two points above.
RETURN_SERIAL_SHARE = 0.1113


def seconds_per_gib(cpus: int | None = None) -> float:
    """What one GiB of workspace costs the return on a machine with `cpus` cores - never less
    than the measured 32-core figure. See RETURN_SECONDS_PER_GIB_ONE_CORE for the model and why
    it is one-sided. `cpus` None or non-positive is "not known": the 32-core figure."""
    if not cpus or int(cpus) <= 0:
        return RETURN_SECONDS_PER_GIB
    n = float(int(cpus))
    priced = RETURN_SECONDS_PER_GIB_ONE_CORE * (RETURN_SERIAL_SHARE
                                                + (1.0 - RETURN_SERIAL_SHARE) / n)
    return max(RETURN_SECONDS_PER_GIB, priced)


def cpus_of(machine: str) -> int | None:
    """How many cores a machine type has, from the one table that knows (contracts/mesh_machine.
    MACHINES) - which is also where a decision's own `cpus` comes from. None for a name it does
    not hold."""
    from meshpipeline.contracts import mesh_machine as MM
    return next((m.cpus for m in MM.MACHINES if m.name == str(machine or "")), None)
#: WHAT IS ACTUALLY STILL ON THE DISK WHEN THE RETURN STARTS, as a multiple of `workspace_gib`.
#: `workspace_gib` is the finished mesh alone (budget.written_gib). The share table under
#: DISK_PER_WORKSPACE_GIB below is what a run WRITES at its peak - about 1.19 x - and two of those
#: shares are gone again by the time `return_scope` walks the tree: checkMesh's -writeFields output
#: (~7.7 GiB, cleared by native._boundary_quality once it has been reduced) and the metal's copy of
#: every cell (~45 GiB, dropped by native._drop_unread_volume). Worked out 2026-11-12 at the plate's
#: margined 333,869,285 cells: 565 (the mesh) + 17 (the coolant's internal.vtu) + 4 (the boundary
#: .vtp tree) + 3.2 (the two boundary indexes) + 0.8 (the quality sidecars) + 0.12 (the review msh)
#: = about 590 GiB, which is 1.044 x the 565. Charged at 1.05.
RETURNED_SHARE_OF_WORKSPACE = 1.05
#: The VM's disk: the workspace lives here (on Cloud Run it is in memory), at least this many GB.
MIN_DISK_GB = 200
#: HOW MUCH DISK A WORKSPACE FIGURE BUYS, and what the factor is for. `workspace_gib` is the
#: engine's estimate of the FINISHED MESH on disk and nothing else, so everything else a run
#: writes has to come out of this multiplier. For `cfmesh_conjugate`, which is the engine that
#: reaches this route, the shares are (worked out 2026-11-12 at the Corintis plate's margined
#: 333,869,285 cells, where written_gib is 565 GiB because it already counts the block AND the two
#: regions it is cut into):
#:
#:     the decomposed per-rank copies, then the merged regions   565 GiB   = 1.00 x workspace_gib
#:     foamToVTK's internal.vtu, both regions                    ~62 GiB   a second copy of
#:                                                                         every cell, ~200 B/cell
#:     the boundary .vtp tree, both regions                       ~4 GiB   72 M boundary faces
#:     checkMesh's -writeFields output                           ~7.7 GiB  4.25 GB measured at
#:                                                                         182 M cells, 23 B/cell
#:     the review .msh                                           <0.1 GiB  bounded by the triangle
#:                                                                         budget (review_surface)
#:     the archive it goes home in                                31 GiB   5.5% of written
#:                                                               --------
#:                                                               ~670 GiB  = 1.19 x workspace_gib
#:
#: So FOUR is about 3.4x what the run needs, and it stays four: the number was already generous
#: and the honest finding is that it was never the thing that ran out. (The hand-killed export of
#: 2026-10-29 ran on mesh-bench's 590 GB /scratch, not on a VM sized by this line, so it is not
#: evidence about this formula.) Written down because an unexplained multiplier is one nobody can
#: safely change, and `splitMeshRegions -overwrite` is why the pre-split block is not a third copy.
DISK_PER_WORKSPACE_GIB = 4
#: On top of the workspace: the boot image, the pulled mesh container and the COS stateful
#: partition's own overhead, none of which scale with the mesh.
DISK_FIXED_GB = 50
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
  -e HEXERA_MESH_CONTRACT="$(get instance/attributes/mesh-contract)" \
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


def return_margin_s(workspace_gib: float = 0.0, *, cpus: int | None = None) -> int:
    """How long the VM needs AFTER the engine has returned, for a workspace of this size on a
    machine with this many cores.

    The fixed floor plus the priced return - see LIFETIME_MARGIN_S, RETURN_SECONDS_PER_GIB,
    RETURN_SECONDS_PER_GIB_ONE_CORE and RETURNED_SHARE_OF_WORKSPACE for the measured figures each
    term comes from. Pure, so the relationship can be exercised rather than read.

    MEASURED BY ARITHMETIC ON THE RECORDED RATES, 2026-11-12, at the Corintis plate's
    333,869,285 margined cells (budget.written_gib = 565.3 GiB) on its n2-highmem-64:

        590 GiB still on disk x 32.4 s a GiB   =  19,231 s   the three passes over every byte
        the fixed floor                        =   3,600 s   boot, image pull, upload, slack
                                                  --------
                                                  22,831 s

    against the 3,600 s that was the whole margin before: 6.3x. A 1.12 GB coupon prices at
    3,600 s, which is what it always got.

    `cpus` is the machine's cores. It can only RAISE the price (seconds_per_gib), so the plate's
    64-core figure above is unchanged and a smaller box is charged what its own cores cost.
    """
    priced = seconds_per_gib(cpus) * RETURNED_SHARE_OF_WORKSPACE * max(0.0, float(workspace_gib))
    return LIFETIME_MARGIN_S + int(priced)


def lifetime_seconds(timeout: int, *, workspace_gib: float = 0.0,
                     cpus: int | None = None) -> int:
    """How long the VM may live, for a run whose WHOLE budget is `timeout` seconds and whose
    finished mesh is about `workspace_gib` on disk, on a machine with `cpus` cores.

    Pure, and separate from `instance_body`, so the relationship can be exercised rather than
    read: the machine's life must be derived from what the whole chain can need, never from one
    stage's cap - and `timeout` is only the ENGINE's half of that chain, so the return has to be
    priced from the same workspace figure the disk is (`disk_gb_for`) and on the machine it will
    actually run on. `workspace_gib` defaults to 0.0 and `cpus` to None rather than being
    required, because a caller that does not know them gets the fixed floor and the 32-core rate
    this line always had rather than a number nobody chose.
    """
    return max(1, int(timeout)) + return_margin_s(workspace_gib, cpus=cpus)


def disk_gb_for(workspace_gib: float) -> int:
    """The VM's disk, in GB, for a run whose finished mesh is about `workspace_gib` on disk.

    Pure for the same reason as `lifetime_seconds`: see DISK_PER_WORKSPACE_GIB for what the
    multiplier is paying for and the measured shares it was checked against.
    """
    want = int(DISK_PER_WORKSPACE_GIB * max(0.0, float(workspace_gib))) + DISK_FIXED_GB
    return max(MIN_DISK_GB, want)


def instance_body(*, name: str, zone: str, machine: str, image: str, account: str, input_uri: str,
                  output_uri: str, engine: str, timeout: int, disk_gb: int,
                  workspace_gib: float = 0.0, cpus: int | None = None) -> dict:
    """The instances.insert body (pure: tests read it).

    `cpus` is the machine's cores, for the return's price. When it is not given it is looked up
    from `machine` (cpus_of, the same table a decision's own `cpus` comes from), so a caller that
    knows only the machine type still prices the return on the box it is about to start.
    """
    from meshpipeline.contracts.mesh_execution import MESH_EXCHANGE_VERSION
    cores = cpus if cpus else cpus_of(machine)
    meta = {"mesh-image": image, "input-uri": input_uri, "output-uri": output_uri, "engine": engine,
            "timeout": str(int(timeout)), "mesh-contract": str(MESH_EXCHANGE_VERSION),
            "startup-script": STARTUP_SCRIPT}
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
                       "maxRunDuration": {
                           "seconds": str(lifetime_seconds(timeout, workspace_gib=workspace_gib,
                                                           cpus=cores))}},
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
    # ONE WORKSPACE FIGURE BUYS BOTH THE DISK AND THE TIME. The return is a walk over every byte
    # of that workspace (return_scope) and then an archive of it, so the machine's life has to be
    # priced from the same number its disk is - see return_margin_s.
    #
    # ...AND ON THE CORES IT WILL ACTUALLY HAVE. Both measure passes and the archive spread over
    # `job_cpus()` (return_scope, parallel_gzip), and the rate they were measured at is a 32-core
    # rate; the decision this route is dispatched by already records the machine's cores, so the
    # price is charged against them rather than against the measurement's (seconds_per_gib).
    workspace_gib = float(decision.get("workspace_gib") or 0.0)
    cpus = decision.get("cpus")
    disk_gb = disk_gb_for(workspace_gib)
    last = ""
    for suffix in ZONE_SUFFIXES:
        zone = f"{provcfg.GCP_REGION}-{suffix}"
        body = instance_body(name=name, zone=zone, machine=machine, image=image, account=account,
                             input_uri=input_uri, output_uri=output_uri, engine=engine, timeout=timeout,
                             disk_gb=disk_gb, workspace_gib=workspace_gib,
                             cpus=int(cpus) if cpus else None)
        url = f"https://compute.googleapis.com/compute/v1/projects/{provcfg.GCP_PROJECT_ID}/zones/{zone}/instances"
        try:
            resp = _rq.post(url, json=body, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=60)
        except _rq.exceptions.RequestException as exc:
            raise SubmissionIndeterminate(
                f"{engine}: the VM request failed in transport ({type(exc).__name__}); it may exist") from None
        if 200 <= resp.status_code < 300:
            # THE 200 IS THE REQUEST BEING ACCEPTED, NOT THE VM EXISTING. instances.insert returns
            # an Operation, and quota is evaluated inside it: a project with four free vCPU accepts
            # the request for an eight-core VM with HTTP 200 and then fails the operation with
            # QUOTA_EXCEEDED. Nothing here looked, so this logged "mesh runs on a n2-highmem-8 VM",
            # returned a reference to an instance that was never created, and the run settled in
            # to wait for a result object that nothing would ever write - until the whole job's
            # timeout. Measured end to end on 2026-10-29: CPUS_ALL_REGIONS 28 used of 32, the
            # operation failed at 06:45:27, and the job was still "running" twenty-five minutes
            # later with no VM in the project and nothing said about why.
            why = _insert_failed(resp, zone=zone)
            if why:
                last = why
                if "QUOTA" in why.upper():
                    # NOT A ZONE PROBLEM, so trying the next zone cannot help: the quota that
                    # binds here is CPUS_ALL_REGIONS, which is one number for the project.
                    break
                continue
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


#: How long to watch the creation operation before believing the VM is coming. A rejection is
#: immediate - quota is checked as the operation starts - and a real creation takes tens of
#: seconds, so this is long enough to catch the first and short enough to cost nothing on the
#: second. The run's own timeout is the backstop it always was.
CONFIRM_S = 20.0
#: ...checked this often. Three looks in twenty seconds.
CONFIRM_EVERY_S = 7.0


def _insert_failed(resp, *, zone: str) -> str:
    """Why the accepted insert did NOT produce a VM ("" when it did, or when it is still coming).

    Returns the reason as the API gave it, because "QUOTA_EXCEEDED" is the one word that tells a
    reader to go and look at a quota rather than at the part.
    """
    import time

    import requests as _rq

    try:
        op = resp.json()
        op_name = str(op.get("name") or "")
    except ValueError:
        return ""
    if not op_name:
        return ""
    url = (f"https://compute.googleapis.com/compute/v1/projects/{provcfg.GCP_PROJECT_ID}"
           f"/zones/{zone}/operations/{op_name}")
    from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token

    deadline = time.monotonic() + CONFIRM_S
    while True:
        try:
            got = _rq.get(url, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30)
            if not (200 <= got.status_code < 300):
                return ""              # cannot tell; the run's own timeout is the backstop
            body = got.json()
        except (_rq.exceptions.RequestException, ValueError):
            return ""
        errors = ((body.get("error") or {}).get("errors") or [])
        if errors:
            first = errors[0]
            code = str(first.get("code") or "").strip()
            msg = str(first.get("message") or "").strip()
            logger.error("the VM creation operation failed in %s: %s %s", zone, code, msg)
            return f"{code or 'operation failed'} in {zone}: {msg}"[:400]
        if str(body.get("status") or "") == "DONE":
            return ""                  # it exists
        if time.monotonic() >= deadline:
            return ""                  # still being made; that is the normal case
        time.sleep(CONFIRM_EVERY_S)


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


__all__ = ["LABEL", "LIFETIME_MARGIN_S", "RETURNED_SHARE_OF_WORKSPACE", "RETURN_SECONDS_PER_GIB",
           "RETURN_SECONDS_PER_GIB_ONE_CORE", "RETURN_SERIAL_SHARE", "STARTUP_SCRIPT",
           "VmReference", "cpus_of", "delete_vm", "disk_gb_for", "instance_body", "instance_name",
           "lifetime_seconds", "return_margin_s", "seconds_per_gib", "trigger_vm"]
