# Responsibility: Fill the mesh-executor settings in .env from the resources the authenticated session can see.
# Owns: the identification of the job, its region and the exchange bucket, and the refusal when one is not certain.
# Boundaries: it writes settings that are empty and nothing else; it creates no cloud resource and no file.
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
RECORD = ROOT / "deploy" / "output" / "deployment.json"

# Roles that let a principal both read and write objects. A bucket the executor exchanges through
# must be writable by it, so a read-only binding is not evidence that this is that bucket.
OBJECT_ROLES = {"roles/storage.objectUser", "roles/storage.objectAdmin", "roles/storage.admin",
                "roles/storage.legacyObjectOwner", "roles/owner", "roles/editor"}


def die(message: str, *lines: str) -> None:
    print(f"\n  {message}", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    print(file=sys.stderr)
    sys.exit(1)


def gcloud(*args: str) -> str:
    result = subprocess.run(["gcloud", *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def gcloud_json(*args: str):
    raw = gcloud(*args, "--format=json")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def read_settings() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, _, value = stripped.partition("=")
            values[name] = value
    return values


def job_region(job: dict) -> str:
    # A Cloud Run job carries its region as a label rather than a field, and the listing is the
    # only place it appears without naming a region to ask about.
    labels = (job.get("metadata") or {}).get("labels") or {}
    return labels.get("cloud.googleapis.com/location", "")


def runtime_identity(job: dict) -> str:
    spec = (((job.get("spec") or {}).get("template") or {}).get("spec") or {})
    return ((spec.get("template") or {}).get("spec") or {}).get("serviceAccountName", "")


def holds_object_access(bucket: str, principals: set[str]) -> bool:
    policy = gcloud_json("storage", "buckets", "get-iam-policy", f"gs://{bucket}")
    if not policy:
        return False
    for binding in policy.get("bindings") or []:
        if binding.get("role") in OBJECT_ROLES and principals & set(binding.get("members") or []):
            return True
    return False


def resolve_bucket(project: str, region: str, principals: set[str]) -> str:
    # The deployment record is the only artefact that states which bucket this tooling provisioned,
    # so it wins whenever it exists. Everything below is inference, and inference that cannot reach
    # one answer must say so rather than pick.
    if RECORD.is_file():
        try:
            recorded = json.loads(RECORD.read_text())["resources"]["exchange_bucket"]["name"]
            if recorded:
                print(f"  exchange bucket    {recorded}  (from deploy/output/deployment.json)")
                return recorded
        except (ValueError, KeyError):
            pass

    buckets = gcloud_json("storage", "buckets", "list", f"--project={project}") or []
    same_region = [b for b in buckets
                   if (b.get("location") or "").lower() == region.lower()]
    if not same_region:
        die(f"no bucket in {region} to exchange workspaces through.",
            "The executor exchanges through a bucket in the job's own region.",
            "Provision one with: make mesh-setup")

    # The grant is what distinguishes an exchange bucket from any other bucket that happens to sit
    # in the same region. Provisioning grants the caller and the job's identity object access to it
    # and to nothing else, so a bucket carrying that binding is the one this executor uses.
    qualified = [b["name"] for b in same_region if holds_object_access(b["name"], principals)]
    if len(qualified) == 1:
        print(f"  exchange bucket    {qualified[0]}  (the only bucket in {region} the executor can exchange through)")
        return qualified[0]

    candidates = qualified or [b["name"] for b in same_region]
    die("cannot identify the exchange bucket, and it will not be guessed from a name.",
        f"{len(candidates)} candidates in {region} are equally consistent with the evidence:",
        *[f"    {name}" for name in candidates],
        "",
        "Set GCP_MESH_BUCKET in .env yourself, from the deployment record or the project",
        "administrator. docs/getting-started/setup.md, Path A, explains how to tell them apart.")
    return ""


def apply(discovered: dict[str, str]) -> None:
    # Only an empty setting is filled. A value already in .env was put there by someone who meant
    # it, and silently replacing it would make this command destructive to run twice.
    current = read_settings()
    text = ENV_FILE.read_text()
    written, kept = [], []
    for name, value in discovered.items():
        if current.get(name, ""):
            if current[name] != value:
                kept.append(f"{name} is already set to something else - left as it is")
            continue
        text = re.sub(rf"^{re.escape(name)}=.*$", f"{name}={value}", text, count=1, flags=re.M)
        written.append(f"{name}={value}")
    ENV_FILE.write_text(text)

    print()
    if written:
        print("  written to .env:")
        for line in written:
            print(f"    {line}")
    else:
        print("  every discovered setting was already present - .env is unchanged")
    for line in kept:
        print(f"  {line}")


def main() -> int:
    if not ENV_FILE.is_file():
        die("no .env to fill - run: make setup")
    if not shutil.which("gcloud"):
        die("the Google Cloud CLI is not installed, so nothing can be discovered.",
            "install: https://cloud.google.com/sdk/docs/install")

    account = gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    if not account:
        die("this host is not signed in to Google Cloud.", "run: gcloud auth login")

    project = gcloud("config", "get-value", "project")
    if not project or project == "(unset)":
        die("no project is selected.", "run: gcloud config set project <PROJECT_ID>")

    print(f"\n  Discovering the mesh executor in project {project} as {account}\n")

    jobs = gcloud_json("run", "jobs", "list", f"--project={project}") or []
    if not jobs:
        die(f"no Cloud Run job exists in {project}, so there is no executor to adopt.",
            "This is the other path: provision one with  make mesh-setup")
    if len(jobs) > 1:
        die(f"{len(jobs)} Cloud Run jobs exist and only one can be the mesh executor:",
            *[f"    {j['metadata']['name']}  ({job_region(j)})" for j in jobs],
            "",
            "Set CLOUDRUN_JOB and GCP_REGION in .env yourself.")

    job = jobs[0]
    name = job["metadata"]["name"]
    region = job_region(job)
    identity = runtime_identity(job)
    print(f"  project            {project}")
    print(f"  Cloud Run job      {name}")
    print(f"  region             {region}")
    if identity:
        print(f"  runtime identity   {identity}")

    principals = {f"user:{account}", f"serviceAccount:{account}"}
    if identity:
        principals.add(f"serviceAccount:{identity}")
    bucket = resolve_bucket(project, region, principals)

    apply({"GCP_PROJECT_ID": project, "GCP_REGION": region,
           "CLOUDRUN_JOB": name, "GCP_MESH_BUCKET": bucket})
    print("\n  Next command:  make mesh-doctor\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
