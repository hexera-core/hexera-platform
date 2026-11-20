#!/usr/bin/env python3
# Responsibility: Launch the WHOLE CRS v3 verdict (T19 + T29, both clean controls) as one wave of
#                 labelled, self-deleting GCE VMs in hexera-dev / us-central1, one shard process per
#                 vCPU.
# Owns: argument validation (machine family, the 100-vCPU ceiling, pinned image, full commits, the
#       harness-only diff), the shard plan and its packing onto VMs, VM names/labels/metadata, and
#       the resume rule (skip shards that are DONE or already running).
# Boundaries: --dry-run prints every gcloud command and runs NONE (only local `git` reads). A live
#             run reads state (image tags, staged objects, quota, running VMs, DONE markers) before
#             it creates anything, and creates only VMs.
"""Launch everything: three arms on the 120-file sample plus two clean controls, in one wave.

    launch_fleet.py --image <ref@sha256:...> --commit <engine 40-hex> \\
        --harness-commit <40-hex> [--machine-type c2d-standard-8] [--shards-total 96] --dry-run

THE SHARDS. evalspec.shard_plan shares --shards-total out across the five units (arm x kind) by
estimated file-minutes, so every shard takes about the same wall. Shards are packed onto VMs with
one shard process per vCPU (--cpus=1, OMP_NUM_THREADS=1 each). Total vCPU <= 100.

THE COMMITS. --commit is the engine: the commit the image was built from (its tag). The harness
(devtools/validation at --harness-commit) is shipped to the VMs as code.tgz and must differ from
the engine commit ONLY under devtools/validation/ and tests/: the code that measures may be newer
than the engine, the engine may not be.

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
#: Paths the harness commit may change relative to the engine commit.
HARNESS_PATHS = ("devtools/validation/", "tests/")


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


def code_uri(harness: str, prefix: str = PREFIX) -> str:
    return f"{prefix}/harness-{harness[:12]}/code.tgz"


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
        f"v3-harness={args.harness_commit}", f"v3-units={','.join(units)}",
        f"v3-bucket={BUCKET}", f"v3-prefix={args.prefix}",
        f"v3-code={code_uri(args.harness_commit, args.prefix)}",
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
    for name in ("commit", "harness_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", getattr(args, name) or ""):
            parser.error(f"--{name.replace('_', '-')} must be a full 40-hex sha")
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


def harness_problems(commit: str, harness: str) -> list[str]:
    """The harness commit must descend from the engine commit and change only harness paths."""
    git = ["git", "-C", str(REPO)]
    if subprocess.run(git + ["cat-file", "-e", f"{harness}^{{commit}}"],
                      capture_output=True).returncode:
        return [f"harness commit {harness[:12]} is not in this checkout"]
    if subprocess.run(git + ["merge-base", "--is-ancestor", commit, harness],
                      capture_output=True).returncode:
        return [f"harness {harness[:12]} does not descend from engine {commit[:12]}"]
    changed = subprocess.run(git + ["diff", "--name-only", commit, harness],
                             capture_output=True, text=True).stdout.split()
    outside = [p for p in changed if not p.startswith(HARNESS_PATHS)]
    return [f"harness {harness[:12]} changes engine paths vs {commit[:12]}: {outside[:5]}"] \
        if outside else []


def _gcloud_json(cmd: list[str]):
    out = subprocess.run(cmd + ["--format=json"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


def live_checks(args, vcpus: int) -> list[str]:
    """Read-only state checks before anything is created. Returns problems."""
    problems = []
    try:
        desc = _gcloud_json(["gcloud", "artifacts", "docker", "images", "describe", args.image])
        tags = (desc.get("image_summary") or {}).get("tags") or []
        if args.commit[:12] not in tags and not args.allow_untagged_image:
            problems.append(f"the image carries tags {tags}, not {args.commit[:12]}: it may not be "
                            f"built from the engine commit (--allow-untagged-image to override)")
    except RuntimeError as exc:
        problems.append(f"cannot describe the image: {exc}")
    for obj in (code_uri(args.harness_commit, args.prefix), f"{args.prefix}/sample.json",
                f"{args.prefix}/clean-control-100.json"):
        if subprocess.run(["gcloud", "storage", "ls", obj], capture_output=True).returncode:
            problems.append(f"{obj} is not staged (run stage.py)")
    for kind in evalspec.KINDS:
        if subprocess.run(["gcloud", "storage", "ls", f"{args.prefix}/inputs/{kind}/"],
                          capture_output=True).returncode:
            problems.append(f"no {kind} inputs staged under {args.prefix}/inputs/{kind}/")
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
    return problems


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


def running_units() -> tuple[set[str], list[str]]:
    """Unit ids claimed by eval VMs that still exist, read off their v3-units metadata."""
    vms = _gcloud_json(["gcloud", "compute", "instances", "list", f"--project={PROJECT}",
                        "--filter=labels.purpose=v3-eval"]) or []
    claimed: set[str] = set()
    for vm in vms:
        for item in (vm.get("metadata") or {}).get("items") or []:
            if item.get("key") == "v3-units":
                claimed.update(u for u in item.get("value", "").split(",") if u)
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
    parser.add_argument("--commit", required=True, help="engine: the commit the image was built from")
    parser.add_argument("--harness-commit", required=True,
                        help="the commit whose devtools/validation the VMs run (code.tgz)")
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--shards-total", type=int, default=96)
    parser.add_argument("--zone", default=f"{REGION}-a")
    parser.add_argument("--max-run-hours", type=int, default=3)
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--only", default="",
                        help="resume: launch only these unit ids (arm:kind:I/N, comma-separated)")
    parser.add_argument("--allow-untagged-image", action="store_true")
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

    problems = harness_problems(args.commit, args.harness_commit)
    if not args.skip_preflight:
        for arm in evalspec.ARMS:
            problems += preflight_settings(arm)
    skipped: list[str] = []
    if not args.dry_run:
        try:
            done = done_units(args.commit)
            claimed, live_vms = running_units()
        except RuntimeError as exc:
            problems.append(str(exc))
            done, claimed, live_vms = set(), set(), []
        skipped = [u for u in units if u in done or u in claimed]
        units = [u for u in units if u not in done and u not in claimed]
        if live_vms:
            print(f"{len(live_vms)} eval VM(s) still exist: {', '.join(live_vms)}")
    vms = evalspec.pack(units, slots) if units else []
    if not args.dry_run and units:
        problems += live_checks(args, len(vms) * slots)

    minutes = evalspec.plan_minutes(plan)
    print(f"plan ({args.shards_total} shards, one per vCPU, {slots} per {args.machine_type}):")
    for (arm, kind), n in plan.items():
        print(f"  {arm:<20} {kind:<8} {n:>3} shard(s) x <= {-(-evalspec.files_of(kind) // n)} "
              f"file(s)  ~{minutes[(arm, kind)]:.0f} min  [{evalspec.arm(arm)['model_role']}]")
    print(f"launch: {len(units)} shard(s) on {len(vms)} VM(s) = {len(vms) * slots} vCPU "
          f"(ceiling {evalspec.MAX_TOTAL_VCPU}); skipped {len(skipped)} done/running; engine "
          f"{args.commit[:12]}, harness {args.harness_commit[:12]}; model key from secret "
          f"{evalspec.MODEL_SECRET}; known no-ops: {evalspec.KNOWN_NOOPS}")
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
