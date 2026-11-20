#!/usr/bin/env python3
# Responsibility: Launch the WHOLE CRS v3 verdict (T19 + T29, both clean controls) as one wave of
#                 labelled, self-deleting GCE VMs in hexera-dev / us-central1, one shard process per
#                 vCPU.
# Owns: argument validation (machine family, the 100-vCPU ceiling, pinned image, full commits, the
#       toolchain-drift rule), the shard plan and its packing onto VMs, VM names/labels/metadata, and
#       the resume rule (skip shards that are DONE or already running).
# Boundaries: --dry-run prints every gcloud command and runs NONE (only local `git` reads). A live
#             run reads state (image tags, staged objects, quota, running VMs, DONE markers) before
#             it creates anything, and creates only VMs.
"""Launch everything: three arms on the 120-file sample plus two clean controls, in one wave.

    launch_fleet.py --image <ref@sha256:...> --commit <code 40-hex> \\
        --image-commit <the image's commit> [--machine-type c2d-standard-8] [--shards-total 96] --dry-run

THE SHARDS. evalspec.shard_plan shares --shards-total out across the five units (arm x kind) by
estimated file-minutes, so every shard takes about the same wall. Shards are packed onto VMs with
one shard process per vCPU (--cpus=1, OMP_NUM_THREADS=1 each). Total vCPU <= 100.

THE COMMITS. --commit is the CODE that runs: stage.py git-archives its src/ and
devtools/validation/ into code.tgz, and every container runs with PYTHONPATH=/eval/code/src, so the
image's installed wheel is shadowed and the image supplies only the toolchain (OCP, gmsh, fTetWild,
the Python dependencies). A newer commit can therefore be measured without an image redeploy.
--image-commit is the commit the image was built from: a tag mismatch with it is a WARNING, and a
dependency/toolchain change between it and --commit (requirements/, pyproject.toml, Dockerfile,
deploy/docker/) is REFUSED unless --allow-dep-drift, because the image would not carry it.

RESULTS land under gs://dev-crsv3-transfer-<n>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/ (rows
stream per file into bundle/rows.jsonl, synced every 4 min), so the fleet is detached: nothing
local needs to survive. Each VM deletes itself after its shards; --max-run-duration with
--instance-termination-action=DELETE is the backstop.

RESUME. Re-running the same command launches only the shards that have no DONE marker and are not
claimed by a running VM (read off each eval VM's v3-units metadata). --only narrows it further.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROJECT = "hexera-dev"
PROJECT_NUMBER = "224734058693"
REGION = "us-central1"
BUCKET = f"gs://dev-crsv3-transfer-{PROJECT_NUMBER}"
#: Inputs and code (staged once by stage.py / clean_control.py fetch).
PREFIX = f"{BUCKET}/eval/v3-verdict"
#: The label every crsv3 resource carries (`make destroy-env SLUG=crsv3` uses DEPLOYMENT_ID=dev-crsv3).
DEPLOYMENT_ID = "dev-crsv3"
SERVICE_ACCOUNT = f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DIGEST_RE = re.compile(r"^[a-z0-9.-]+-docker\.pkg\.dev/[^@\s]+@sha256:[0-9a-f]{64}$")
#: The quota metric that bounds each allowed family in us-central1.
QUOTA_METRIC = {"c2d": "C2D_CPUS", "c3": "C3_CPUS", "c3d": "C3D_CPUS", "c2": "C2_CPUS"}
#: Paths whose change between the image's commit and the code commit the image cannot absorb.
TOOLCHAIN_PATHS = ("requirements/", "pyproject.toml", "Dockerfile", "deploy/docker/")


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")


def results_root(arm: str, commit: str, bucket: str = BUCKET) -> str:
    """THE DETACHED RESULTS ROOT: every shard of one arm at one engine commit writes under
    <bucket>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/, so collect.py needs GCS and nothing else."""
    return f"{bucket}/eval/{arm}-{commit[:12]}"


def out_uri(arm: str, commit: str, kind: str, i: int, n: int, bucket: str = BUCKET) -> str:
    return f"{results_root(arm, commit, bucket)}/{kind}/shard-{i}-of-{n}"


def code_uri(commit: str, prefix: str = PREFIX) -> str:
    return f"{prefix}/code-{commit[:12]}/code.tgz"


def vm_name(launch_id: str, j: int, m: int) -> str:
    name = f"{DEPLOYMENT_ID}-v3e-{launch_id}-{j}of{m}"
    if len(name) > 63:
        raise ValueError(f"{name} is longer than 63 characters")
    return name


def labels(commit: str, launch_id: str, units: list[str]) -> str:
    arms = sorted({evalspec.arm(u.split(":")[0])["short"] for u in units})
    return ",".join([f"deployment-id={DEPLOYMENT_ID}", "purpose=v3-eval",
                     f"commit={commit[:12]}", f"launch={launch_id}", f"shards={len(units)}",
                     f"arms={'-'.join(arms)}",
                     "app=hexera", "managed-by=v3-eval"])


def create_command(args, launch_id: str, j: int, m: int, units: list[str]) -> list[str]:
    metadata = "|".join([
        f"v3-image={args.image}", f"v3-commit={args.commit}",
        f"v3-image-commit={args.image_commit}", f"v3-units={','.join(units)}",
        f"v3-bucket={BUCKET}", f"v3-prefix={args.prefix}",
        f"v3-code={code_uri(args.commit, args.prefix)}",
        f"v3-machine-type={args.machine_type}", f"v3-model-secret={evalspec.MODEL_SECRET}",
    ])
    return [
        "gcloud", "compute", "instances", "create", vm_name(launch_id, j, m),
        f"--project={PROJECT}", f"--zone={args.zone}", f"--machine-type={args.machine_type}",
        "--provisioning-model=STANDARD", "--instance-termination-action=DELETE",
        f"--max-run-duration={args.max_run_hours}h",
        "--image-family=ubuntu-2204-lts", "--image-project=ubuntu-os-cloud",
        "--boot-disk-size=100GB", "--boot-disk-type=pd-balanced",
        "--network=default", f"--subnet=projects/{PROJECT}/regions/{REGION}/subnetworks/default",
        "--no-address", f"--service-account={SERVICE_ACCOUNT}", "--scopes=cloud-platform",
        f"--labels={labels(args.commit, launch_id, units)}",
        "--metadata-from-file=" + ",".join([
            f"startup-script={HERE / 'vm_startup.sh'}",
            f"shutdown-script={HERE / 'vm_shutdown.sh'}"]),
        f"--metadata=^|^{metadata}",
    ]


def validate(parser: argparse.ArgumentParser, args) -> None:
    try:
        args.machine_type = evalspec.validate_machine_type(args.machine_type)
    except ValueError as exc:
        parser.error(str(exc))
    if not DIGEST_RE.match(args.image or ""):
        parser.error(f"--image must be pinned by digest in Artifact Registry "
                     f"(<region>-docker.pkg.dev/...@sha256:<64 hex>), not {args.image!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit or ""):
        parser.error("--commit must be the full 40-hex sha of the code to run")
    if not re.fullmatch(r"[0-9a-f]{7,40}", args.image_commit or ""):
        parser.error("--image-commit must be the (abbreviated) sha the image was built from")
    if not args.zone.startswith(f"{REGION}-"):
        parser.error(f"--zone must be in {REGION}")
    if not args.prefix.startswith("gs://dev-crsv3-"):
        parser.error("--prefix must be a crsv3-owned bucket (gs://dev-crsv3-...)")
    slots = evalspec.vcpus_of(args.machine_type)
    try:
        plan = evalspec.shard_plan(args.shards_total)
    except ValueError as exc:
        parser.error(str(exc))
    vms = -(-sum(plan.values()) // slots)
    if vms * slots > evalspec.MAX_TOTAL_VCPU:
        parser.error(f"{sum(plan.values())} shards on {args.machine_type} need {vms} VMs = "
                     f"{vms * slots} vCPU, over the {evalspec.MAX_TOTAL_VCPU}-vCPU ceiling")


def drift_problems(image_commit: str, commit: str) -> tuple[list[str], list[str]]:
    """(problems, warnings) for running `commit`'s src/ on an image built from `image_commit`.
    A toolchain/dependency change between them is a problem; any other src/ change is the point
    of running from the archive and is only reported."""
    git = ["git", "-C", str(REPO)]
    for sha in (image_commit, commit):
        if subprocess.run(git + ["cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode:
            return [f"commit {sha[:12]} is not in this checkout (git fetch)"], []
    changed = subprocess.run(git + ["diff", "--name-only", image_commit, commit],
                             capture_output=True, text=True).stdout.split()
    toolchain = [p for p in changed if p.startswith(TOOLCHAIN_PATHS)]
    src = [p for p in changed if p.startswith("src/")]
    warnings = [f"code {commit[:12]} differs from the image's {image_commit[:12]} in {len(src)} "
                f"src/ file(s); they run from the archive"] if src else []
    return ([f"toolchain/dependency files changed since the image's commit {image_commit[:12]}: "
             f"{toolchain[:6]} - the image does not carry them (--allow-dep-drift to override)"]
            if toolchain else []), warnings


def _gcloud_json(cmd: list[str]):
    out = subprocess.run(cmd + ["--format=json"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


#: Roles that let the compute SA create, REPLACE and DELETE result objects: publish_unit re-copies
#: rows.json/PROVENANCE.json over the rsync'd copies, and a rerun shard removes its old FAILED
#: marker. objectCreator (create-only) is deliberately absent. Unconditional grants only.
WRITE_ROLES = frozenset({
    "roles/storage.objectAdmin", "roles/storage.objectUser",
    "roles/storage.admin", "roles/storage.legacyBucketWriter", "roles/storage.legacyBucketOwner",
    "roles/editor", "roles/owner",
})


def sa_write_problem(bucket_policy: dict, project_policy: dict,
                     bucket: str = BUCKET) -> str | None:
    """None if SERVICE_ACCOUNT holds an unconditional write role on the bucket or the project.

    A launch whose VMs can read the staged inputs but cannot upload results burns every shard and
    leaves nothing to collect (the lost launch: the SA had only objectViewer). Group membership is
    not resolved; grant the role to the SA directly."""
    member = f"serviceAccount:{SERVICE_ACCOUNT}"
    for policy in (bucket_policy, project_policy):
        for b in (policy or {}).get("bindings", []):
            if b.get("role") in WRITE_ROLES and member in b.get("members", []) \
                    and not b.get("condition"):
                return None
    return (f"{SERVICE_ACCOUNT} cannot write results to the bucket (no unconditional write role "
            f"on the bucket or project). Grant it: gcloud storage buckets add-iam-policy-binding "
            f"{bucket} --member={member} --role=roles/storage.objectAdmin")


def live_checks(args, vcpus: int) -> tuple[list[str], list[str]]:
    """Read-only state checks before anything is created. Returns (problems, warnings)."""
    problems, warnings = [], []
    try:
        desc = _gcloud_json(["gcloud", "artifacts", "docker", "images", "describe", args.image])
        tags = (desc.get("image_summary") or {}).get("tags") or []
        if args.image_commit[:12] not in tags:
            # A WARNING, not a refusal: the code runs from the archive, the image is the toolchain.
            warnings.append(f"the image carries tags {tags}, not {args.image_commit[:12]}")
    except RuntimeError as exc:
        warnings.append(f"cannot describe the image: {exc}")
    for obj in (code_uri(args.commit, args.prefix), f"{args.prefix}/sample.json",
                f"{args.prefix}/clean-control-100.json"):
        if subprocess.run(["gcloud", "storage", "ls", obj], capture_output=True).returncode:
            problems.append(f"{obj} is not staged (run stage.py)")
    for kind in evalspec.KINDS:
        if subprocess.run(["gcloud", "storage", "ls", f"{args.prefix}/inputs/{kind}/"],
                          capture_output=True).returncode:
            problems.append(f"no {kind} inputs staged under {args.prefix}/inputs/{kind}/")
    # Workers upload results to BUCKET (create_command's v3-bucket), whatever --prefix the inputs
    # are read from, so that is the bucket whose write access decides whether a launch can land.
    bucket = BUCKET
    try:
        problem = sa_write_problem(
            _gcloud_json(["gcloud", "storage", "buckets", "get-iam-policy", bucket]),
            _gcloud_json(["gcloud", "projects", "get-iam-policy", PROJECT]), bucket)
        if problem:
            problems.append(problem)
    except RuntimeError as exc:
        problems.append(f"cannot read the IAM policy that decides whether results upload: {exc}")
    family = args.machine_type.split("-")[0]
    try:
        region = _gcloud_json(["gcloud", "compute", "regions", "describe", REGION,
                               f"--project={PROJECT}"])
        quota = {q["metric"]: q for q in region.get("quotas", [])}
        fam = quota.get(QUOTA_METRIC[family])
        if fam and fam["limit"] - fam["usage"] < vcpus:
            problems.append(f"{QUOTA_METRIC[family]}: {fam['usage']:.0f}/{fam['limit']:.0f} used, "
                            f"{vcpus} more needed")
    except RuntimeError as exc:
        problems.append(f"cannot read quota: {exc}")
    return problems, warnings


def done_units(commit: str) -> set[str]:
    """Unit ids whose shard wrote DONE, from one recursive listing per arm."""
    done = set()
    for arm in evalspec.ARMS:
        out = subprocess.run(["gcloud", "storage", "ls", "-r", results_root(arm, commit)],
                             capture_output=True, text=True)
        for line in out.stdout.splitlines():
            m = re.search(r"/(failing|clean)/shard-(\d+)-of-(\d+)/DONE$", line.strip())
            if m:
                done.add(f"{arm}:{m.group(1)}:{m.group(2)}/{m.group(3)}")
    return done


def running_units(commit: str) -> tuple[set[str], list[str]]:
    """Unit ids claimed by eval VMs that still exist AND run `commit`, read off their v3-units and
    v3-commit metadata. Unit ids (arm:kind:I/N) carry no commit, so a VM still running an older
    commit's shards must not mark this commit's same-named shards as claimed. Every live eval VM
    is still returned by name, for the operator."""
    vms = _gcloud_json(["gcloud", "compute", "instances", "list", f"--project={PROJECT}",
                        "--filter=labels.purpose=v3-eval"]) or []
    claimed: set[str] = set()
    for vm in vms:
        meta = {item.get("key"): item.get("value", "")
                for item in (vm.get("metadata") or {}).get("items") or []}
        if meta.get("v3-commit") != commit:
            continue
        claimed.update(u for u in (meta.get("v3-units") or "").split(",") if u)
    return claimed, [vm["name"] for vm in vms]


def preflight_settings(arm: str) -> list[str]:
    """run_arm.py's settings preflight against THIS checkout, under the arm's environment. The
    container repeats it against the image; it is decisive here only when the checkout's src/ is
    the engine commit's."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "PYTHONPATH": str(REPO / "src"), **evalspec.arm(arm)["env"],
           evalspec.MODEL_KEY_ENV: "preflight-placeholder"}
    cmd = [sys.executable, str(HERE / "run_arm.py"), "--arm", arm, "--kind", "failing",
           "--preflight-only", "--allow-no-ftetwild", "--code-root", str(REPO)]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    found = [f"{arm}: {ln[len('PREFLIGHT '):]}" for ln in out.stderr.splitlines()
             if ln.startswith("PREFLIGHT ")]
    return found or ([] if out.returncode == 0 else
                     [f"{arm}: preflight did not run: {out.stderr.strip()[-400:]}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True,
                        help="the code that runs: its src/ and devtools/validation (code.tgz)")
    parser.add_argument("--image-commit", required=True,
                        help="the commit the image was built from (its tag); toolchain only")
    parser.add_argument("--allow-dep-drift", action="store_true")
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--shards-total", type=int, default=96)
    parser.add_argument("--zone", default=f"{REGION}-a")
    parser.add_argument("--max-run-hours", type=int, default=3)
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--only", default="",
                        help="resume: launch only these unit ids (arm:kind:I/N, comma-separated)")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="skip the host settings preflight (the container still runs it)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    validate(parser, args)
    args.prefix = args.prefix.rstrip("/")
    slots = evalspec.vcpus_of(args.machine_type)
    plan = evalspec.shard_plan(args.shards_total)
    units = evalspec.work_units(plan)
    if args.only:
        wanted = [u.strip() for u in args.only.split(",") if u.strip()]
        unknown = [u for u in wanted if u not in units]
        if unknown:
            parser.error(f"--only names units outside the plan for --shards-total "
                         f"{args.shards_total}: {unknown[:5]}")
        units = [u for u in units if u in wanted]

    problems, warnings = drift_problems(args.image_commit, args.commit)
    if args.allow_dep_drift:
        warnings += problems
        problems = []
    if not args.skip_preflight:
        for arm in evalspec.ARMS:
            problems += preflight_settings(arm)
    skipped: list[str] = []
    if not args.dry_run:
        try:
            done = done_units(args.commit)
            claimed, live_vms = running_units(args.commit)
        except RuntimeError as exc:
            problems.append(str(exc))
            done, claimed, live_vms = set(), set(), []
        skipped = [u for u in units if u in done or u in claimed]
        units = [u for u in units if u not in done and u not in claimed]
        if live_vms:
            print(f"{len(live_vms)} eval VM(s) still exist: {', '.join(live_vms)}")
    vms = evalspec.pack(units, slots) if units else []
    if not args.dry_run and units:
        more, warned = live_checks(args, len(vms) * slots)
        problems += more
        warnings += warned

    minutes = evalspec.plan_minutes(plan)
    print(f"plan ({args.shards_total} shards, one per vCPU, {slots} per {args.machine_type}):")
    for (arm, kind), n in plan.items():
        print(f"  {arm:<20} {kind:<8} {n:>3} shard(s) x <= {-(-evalspec.files_of(kind) // n)} "
              f"file(s)  ~{minutes[(arm, kind)]:.0f} min  [{evalspec.arm(arm)['model_role']}]")
    print(f"launch: {len(units)} shard(s) on {len(vms)} VM(s) = {len(vms) * slots} vCPU "
          f"(ceiling {evalspec.MAX_TOTAL_VCPU}); skipped {len(skipped)} done/running; code "
          f"{args.commit[:12]} (src/ from the archive), image built from "
          f"{args.image_commit[:12]} (toolchain); model key from secret "
          f"{evalspec.MODEL_SECRET}; known no-ops: {evalspec.KNOWN_NOOPS}")
    for w in warnings:
        print(f"WARN    {w}", file=sys.stderr)
    for p in problems:
        print(f"REFUSE  {p}", file=sys.stderr)

    launch_id = time.strftime("%m%d%H%M", time.gmtime())
    for j, vm_units in enumerate(vms, start=1):
        cmd = create_command(args, launch_id, j, len(vms), vm_units)
        line = " ".join(shlex.quote(c) for c in cmd)
        if args.dry_run or problems:
            print(f"{'DRY-RUN  ' if args.dry_run else 'WOULD    '}{line}")
            continue
        print(f"create   {cmd[4]}  ({len(vm_units)} shards)", flush=True)
        subprocess.run(cmd, check=True)
    if problems:
        print(f"\n{len(problems)} problem(s): "
              f"{'nothing would launch' if args.dry_run else 'nothing launched'}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\nlaunched {len(vms)} VM(s). Watch: gcloud compute instances list "
              f"--project={PROJECT} --filter=labels.purpose=v3-eval ; status: collect.py "
              f"--commit {args.commit} --status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
