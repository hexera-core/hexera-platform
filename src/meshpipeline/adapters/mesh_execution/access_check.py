# Responsibility: Check, as the worker's own identity, that it holds every permission the mesh executor's two routes use - the Cloud Run mesh job, and the Compute Engine VM a plan too big for it runs on (RUN-4) - and say the exact command for any it lacks.
# Boundaries: read-only. Asks Google's testIamPermissions (and reads the job, for its identity); grants nothing, changes nothing, and never stops the worker - a missing grant is logged loudly at start, not discovered by a customer's job.
# Collaborates with: cloud_run_client.py and gce_vm.py (the calls these permissions are for), runtime/celery_worker.py (runs it once at start), deploy/gcp/scripts/create-worker-fleet.sh (makes the grants).
"""THE MESH EXECUTOR'S ACCESS, CHECKED AT WORKER START.

Job bf863215 (26 Oct) spent 39 minutes and five passes on shared dev, every one refused "403 Forbidden ...
jobs/dev-mesh": the VM route reads the mesh job before it starts a VM, and the fleet's identity could run
the job but not read it. Nothing had checked; the first to find out was a customer's 11-part assembly.

So every worker asks, at start and as itself, for exactly the permissions the routes call:

  the mesh job      run.jobs.runWithOverrides, run.executions.cancel   (every mesh)
                    run.jobs.get                                       (the VM route reads its image and identity)
  the job's account iam.serviceAccounts.actAs                          (the VM runs as it)
  the project       compute.instances.create / delete, disks, network  (the VM itself)

and logs one ERROR naming each missing one with the gcloud command that grants it. The image pull is the
job's account's own permission, which this identity cannot test; the deploy grants it
(create-worker-fleet.sh 2c).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: What every mesh dispatch calls on the job (cloud_run_client._trigger_job, cancel_execution).
JOB_RUN = ("run.jobs.runWithOverrides", "run.executions.cancel")
#: What the VM route reads first (gce_vm._job_container).
JOB_READ = ("run.jobs.get",)
#: What starting a VM as the job's account needs on that account.
ACT_AS = ("iam.serviceAccounts.actAs",)
#: What instances.insert with a boot disk, the default network and an external address needs, and
#: what deleting the VM afterwards needs (gce_vm.trigger_vm / delete_vm).
VM_CREATE = ("compute.instances.create", "compute.instances.delete", "compute.instances.setMetadata",
             "compute.instances.setLabels", "compute.instances.setServiceAccount", "compute.disks.create",
             "compute.subnetworks.use", "compute.subnetworks.useExternalIp")
#: The marker every failure line opens with, so an operator finds it in the worker's log.
TAG = "[MESH_ACCESS]"


@dataclass
class Access:
    """What was checked, and what is missing: [(permission, on what, the command that grants it)]."""
    checked: list[str] = field(default_factory=list)
    missing: list[tuple[str, str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown


def _post(session, token: str, url: str, permissions: tuple[str, ...]) -> set[str]:
    resp = session.post(url, json={"permissions": list(permissions)},
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return set((resp.json() or {}).get("permissions") or [])


def check(*, project: str, region: str, job: str, me: str, token: str, session=None) -> Access:
    """The permissions above, asked of Google as the caller of `token` (the worker). `me` is that
    caller's email, for the commands. Never raises: a question that could not be asked is `unknown`."""
    if session is None:
        import requests as session
    out = Access()
    name = f"projects/{project}/locations/{region}/jobs/{job}"
    member = f"serviceAccount:{me}" if me else "serviceAccount:<the worker's account>"

    def ask(what: str, url: str, perms: tuple[str, ...]) -> set[str] | None:
        out.checked.append(what)
        try:
            return _post(session, token, url, perms)
        except Exception as exc:  # noqa: BLE001 - said, never raised
            out.unknown.append(f"{what}: {type(exc).__name__}: {str(exc)[:200]}")
            return None

    have = ask(f"job {job}", f"https://run.googleapis.com/v2/{name}:testIamPermissions", JOB_RUN + JOB_READ)
    if have is not None:
        for p in JOB_RUN:
            if p not in have:
                out.missing.append((p, f"job {job}", f"gcloud run jobs add-iam-policy-binding {job} --project {project} "
                                                     f"--region {region} --member {member} "
                                                     "--role roles/run.jobsExecutorWithOverrides"))
        for p in JOB_READ:
            if p not in have:
                out.missing.append((p, f"job {job}", f"gcloud run jobs add-iam-policy-binding {job} --project {project} "
                                                     f"--region {region} --member {member} --role roles/run.viewer"))
    account = ""
    if have is not None and "run.jobs.get" in have:
        try:
            resp = session.get(f"https://run.googleapis.com/v2/{name}", headers={"Authorization": f"Bearer {token}"},
                               timeout=30)
            resp.raise_for_status()
            account = str((((resp.json() or {}).get("template") or {}).get("template") or {}).get("serviceAccount") or "")
        except Exception as exc:  # noqa: BLE001
            out.unknown.append(f"the job's account: {type(exc).__name__}")
    if account:
        got = ask(f"account {account}",
                  f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{account}:testIamPermissions", ACT_AS)
        if got is not None and ACT_AS[0] not in got:
            out.missing.append((ACT_AS[0], f"account {account}",
                                f"gcloud iam service-accounts add-iam-policy-binding {account} --project {project} "
                                f"--member {member} --role roles/iam.serviceAccountUser"))
    got = ask(f"project {project}", f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
              VM_CREATE)
    if got is not None:
        for p in VM_CREATE:
            if p not in got:
                out.missing.append((p, f"project {project}",
                                    f"gcloud projects add-iam-policy-binding {project} --member {member} "
                                    "--role roles/compute.instanceAdmin.v1 --condition None"))
    return out


def words(access: Access) -> list[str]:
    """One line per missing permission and per question not answered, each with its command."""
    lines = [f"{TAG} missing {p} on {on} - every mesh that needs it fails until it is granted: {cmd}"
             for p, on, cmd in access.missing]
    lines += [f"{TAG} could not check {u}" for u in access.unknown]
    return lines


def _my_email() -> str:
    """The account this worker runs as, from the instance's metadata server; "" off Compute Engine."""
    import requests
    try:
        resp = requests.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                            headers={"Metadata-Flavor": "Google"}, timeout=5)
        return resp.text.strip() if resp.status_code == 200 else ""
    except Exception:  # noqa: BLE001 - unknown is "", and the commands say so
        return ""


def run_at_start() -> Access | None:
    """The check for this deployment, logged: None when the mesh executor is not Cloud Run here (no
    project or job configured - a local stack)."""
    import meshpipeline.settings.providers as provcfg
    if not (provcfg.GCP_PROJECT_ID and provcfg.CLOUDRUN_JOB):
        return None
    try:
        from meshpipeline.adapters.mesh_execution.cloud_run_client import _access_token
        token, me = _access_token(), _my_email()
    except Exception as exc:  # noqa: BLE001 - said, never raised
        logger.error("%s could not get this worker's credential to check its mesh access (%s)", TAG,
                     type(exc).__name__)
        return None
    access = check(project=provcfg.GCP_PROJECT_ID, region=provcfg.GCP_REGION, job=provcfg.CLOUDRUN_JOB,
                   me=me, token=token)
    if access.ok:
        logger.info("%s this worker may run the mesh job and start its big-machine VMs (%s)", TAG,
                    "; ".join(access.checked))
    for line in words(access):
        logger.error("%s", line)
    return access


def main() -> int:
    """`python -m meshpipeline.adapters.mesh_execution.access_check`: the same check, exit 1 when anything is
    missing or could not be checked (for an operator inside a worker container)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    access = run_at_start()
    if access is None:
        return 1
    return 0 if access.ok else 1


__all__ = ["ACT_AS", "Access", "JOB_READ", "JOB_RUN", "TAG", "VM_CREATE", "check", "main", "run_at_start", "words"]


if __name__ == "__main__":
    raise SystemExit(main())
