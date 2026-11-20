#!/usr/bin/env python3
# Responsibility: Launch the labelled, self-deleting GCE VMs that run one arm of the CRS v3 verdict,
#                 one shard per VM, in project hexera-dev / us-central1.
# Owns: argument validation (machine family, pinned image, full commit, shard count), the VM names
#       and labels, the metadata each VM reads, and the per-shard idempotence check.
# Boundaries: --dry-run prints every gcloud command and runs NONE of them (not even read-only
#             ones). A live run reads state (image tags, staged objects, quota, existing VMs and
#             DONE markers) before it creates anything, and creates only VMs.
"""Launch one arm of the v3 verdict as N self-deleting VMs.

    launch_fleet.py --arm v3-full --kind failing --image <ref@sha256:...> --commit <40-hex> \\
        --shards 10 [--machine-type c2d-highcpu-4] [--zone us-central1-a] --dry-run

Each VM runs vm_startup.sh: docker-runs run_arm.py for shard I/N in the image, writes rows +
PROVENANCE.json under <prefix>/<commit12>/out/<arm>/<kind>/shard-I-of-N/, writes DONE, and deletes
itself. --instance-termination-action=DELETE with --max-run-duration is the backstop. Re-running
the same command skips every shard whose DONE marker exists and every VM that already exists.
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
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROJECT = "hexera-dev"
PROJECT_NUMBER = "224734058693"
REGION = "us-central1"
PREFIX = f"gs://dev-crsv3-transfer-{PROJECT_NUMBER}/eval/v3-verdict"
#: The label every crsv3 resource carries (`make destroy-env SLUG=crsv3` uses DEPLOYMENT_ID=dev-crsv3).
DEPLOYMENT_ID = "dev-crsv3"
SERVICE_ACCOUNT = f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DIGEST_RE = re.compile(r"^[a-z0-9.-]+-docker\.pkg\.dev/[^@\s]+@sha256:[0-9a-f]{64}$")
#: The quota metric that bounds each allowed family in us-central1.
QUOTA_METRIC = {"c2d": "C2D_CPUS", "c3": "C3_CPUS", "c3d": "C3D_CPUS", "c2": "C2_CPUS"}


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")


def vm_name(arm: str, kind: str, i: int, n: int) -> str:
    name = f"{DEPLOYMENT_ID}-v3e-{evalspec.arm(arm)['short']}-{kind}-{i}of{n}"
    if len(name) > 63:
        raise ValueError(f"{name} is longer than 63 characters")
    return name


def out_uri(prefix: str, commit: str, arm: str, kind: str, i: int, n: int) -> str:
    return f"{prefix}/{commit[:12]}/out/{arm}/{kind}/shard-{i}-of-{n}"


def labels(arm: str, kind: str, commit: str) -> str:
    return ",".join([f"deployment-id={DEPLOYMENT_ID}", "purpose=v3-eval", f"arm={arm}",
                     f"kind={kind}", f"commit={commit[:12]}", "app=hexera", "managed-by=v3-eval"])


def create_command(args, i: int, env_file: str) -> list[str]:
    n = args.shards
    metadata = "|".join([
        f"v3-image={args.image}", f"v3-commit={args.commit}", f"v3-arm={args.arm}",
        f"v3-kind={args.kind}", f"v3-shard={i}/{n}", f"v3-prefix={args.prefix}",
        f"v3-machine-type={args.machine_type}", f"v3-model-secret={evalspec.MODEL_SECRET}",
        f"v3-accept-inert={','.join(args.accept_inert)}",
    ])
    return [
        "gcloud", "compute", "instances", "create", vm_name(args.arm, args.kind, i, n),
        f"--project={PROJECT}", f"--zone={args.zone}", f"--machine-type={args.machine_type}",
        "--provisioning-model=STANDARD", "--instance-termination-action=DELETE",
        f"--max-run-duration={args.max_run_hours}h",
        "--image-family=ubuntu-2204-lts", "--image-project=ubuntu-os-cloud",
        "--boot-disk-size=50GB", "--boot-disk-type=pd-balanced",
        "--network=default", f"--subnet=projects/{PROJECT}/regions/{REGION}/subnetworks/default",
        "--no-address", f"--service-account={SERVICE_ACCOUNT}", "--scopes=cloud-platform",
        f"--labels={labels(args.arm, args.kind, args.commit)}",
        "--metadata-from-file=" + ",".join([
            f"startup-script={HERE / 'vm_startup.sh'}",
            f"shutdown-script={HERE / 'vm_shutdown.sh'}",
            f"v3-arm-env={env_file}"]),
        f"--metadata=^|^{metadata}",
    ]


def validate(parser: argparse.ArgumentParser, args) -> None:
    try:
        args.machine_type = evalspec.validate_machine_type(args.machine_type)
        evalspec.arm(args.arm)
    except ValueError as exc:
        parser.error(str(exc))
    if not DIGEST_RE.match(args.image or ""):
        parser.error(f"--image must be pinned by digest in Artifact Registry "
                     f"(<region>-docker.pkg.dev/...@sha256:<64 hex>), not {args.image!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit or ""):
        parser.error("--commit must be the full 40-hex sha the image was built from")
    if not 1 <= args.shards <= 40:
        parser.error("--shards must be between 1 and 40")
    if not args.zone.startswith(f"{REGION}-"):
        parser.error(f"--zone must be in {REGION}")
    if not args.prefix.startswith(f"gs://dev-crsv3-"):
        parser.error("--prefix must be a crsv3-owned bucket (gs://dev-crsv3-...)")


def _gcloud_json(cmd: list[str]):
    out = subprocess.run(cmd + ["--format=json"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


def live_checks(args) -> list[str]:
    """Read-only state checks before anything is created. Returns problems."""
    problems = []
    # 1. the image digest carries the commit's 12-char tag (the deploy tags images by commit)
    try:
        desc = _gcloud_json(["gcloud", "artifacts", "docker", "images", "describe", args.image])
        tags = (desc.get("image_summary") or {}).get("tags") or []
        if args.commit[:12] not in tags and not args.allow_untagged_image:
            problems.append(f"the image carries tags {tags}, not {args.commit[:12]}: it may not be "
                            f"built from {args.commit[:12]} (--allow-untagged-image to override)")
    except RuntimeError as exc:
        problems.append(f"cannot describe the image: {exc}")
    # 2. what the VMs will read is staged
    for obj in (f"{args.prefix}/{args.commit[:12]}/code.tgz", f"{args.prefix}/sample.json",
                f"{args.prefix}/clean-control-100.json"):
        if subprocess.run(["gcloud", "storage", "ls", obj], capture_output=True).returncode:
            problems.append(f"{obj} is not staged (run stage.py)")
    if subprocess.run(["gcloud", "storage", "ls", f"{args.prefix}/inputs/{args.kind}/"],
                      capture_output=True).returncode:
        problems.append(f"no {args.kind} inputs are staged under {args.prefix}/inputs/{args.kind}/")
    # 3. regional quota for the family
    family = args.machine_type.split("-")[0]
    want = evalspec.vcpus_of(args.machine_type) * args.shards
    try:
        region = _gcloud_json(["gcloud", "compute", "regions", "describe", REGION,
                               f"--project={PROJECT}"])
        quota = {q["metric"]: q for q in region.get("quotas", [])}.get(QUOTA_METRIC[family])
        if quota and quota["limit"] - quota["usage"] < want:
            problems.append(f"{QUOTA_METRIC[family]}: {quota['usage']:.0f}/{quota['limit']:.0f} "
                            f"used, {want} more needed")
    except RuntimeError as exc:
        problems.append(f"cannot read quota: {exc}")
    return problems


def preflight_settings(args) -> list[str]:
    """run_arm.py's settings preflight against THIS checkout, under the arm's environment. It is
    the same check the container repeats against the image; it is only decisive here when the
    checkout is at --commit."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "PYTHONPATH": str(REPO / "src"), **evalspec.arm(args.arm)["env"],
           evalspec.MODEL_KEY_ENV: "preflight-placeholder"}
    cmd = [sys.executable, str(HERE / "run_arm.py"), "--arm", args.arm, "--kind", args.kind,
           "--preflight-only", "--allow-no-ftetwild", "--code-root", str(REPO)]
    for flag in args.accept_inert:
        cmd += ["--accept-inert", flag]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return [ln[len("PREFLIGHT "):] for ln in out.stderr.splitlines() if ln.startswith("PREFLIGHT ")] \
        or ([] if out.returncode == 0 else [f"preflight did not run: {out.stderr.strip()[-400:]}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--kind", required=True, choices=evalspec.KINDS)
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--zone", default=f"{REGION}-a")
    parser.add_argument("--max-run-hours", type=int, default=4)
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--accept-inert", action="append", default=[],
                        help="launch although this CAD_REPAIR_ flag is inert in the build")
    parser.add_argument("--allow-untagged-image", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="skip the host settings preflight (the container still runs it)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    validate(parser, args)
    args.prefix = args.prefix.rstrip("/")

    problems = [] if args.skip_preflight else preflight_settings(args)
    if not args.dry_run:
        problems += live_checks(args)

    env_file = Path(tempfile.gettempdir()) / f"v3-eval-{args.arm}.env"
    env_file.write_text(evalspec.env_file_text(args.arm))
    existing: set[str] = set()
    if not args.dry_run:
        try:
            vms = _gcloud_json(["gcloud", "compute", "instances", "list", f"--project={PROJECT}",
                                "--filter=labels.purpose=v3-eval"])
            existing = {vm["name"] for vm in vms or []}
        except RuntimeError as exc:
            problems.append(f"cannot list instances: {exc}")

    print(f"arm {args.arm} ({evalspec.arm(args.arm)['about']}), kind {args.kind}, "
          f"{args.shards} shard(s) on {args.machine_type} in {args.zone}; "
          f"{evalspec.vcpus_of(args.machine_type) * args.shards} vCPU; "
          f"max {args.max_run_hours} h each; model key from secret {evalspec.MODEL_SECRET}")
    for p in problems:
        print(f"REFUSE  {p}", file=sys.stderr)

    created = skipped = 0
    for i in range(1, args.shards + 1):
        name = vm_name(args.arm, args.kind, i, args.shards)
        done = out_uri(args.prefix, args.commit, args.arm, args.kind, i, args.shards) + "/DONE"
        if args.dry_run:
            print(f"DRY-RUN  (skip if exists) gcloud storage ls {done}")
        elif name in existing:
            print(f"skip     {name}: the VM exists (running or not yet deleted)")
            skipped += 1
            continue
        elif subprocess.run(["gcloud", "storage", "ls", done], capture_output=True).returncode == 0:
            print(f"skip     {name}: {done} exists")
            skipped += 1
            continue
        cmd = create_command(args, i, str(env_file))
        line = " ".join(shlex.quote(c) for c in cmd)
        if args.dry_run or problems:
            print(f"{'DRY-RUN  ' if args.dry_run else 'WOULD    '}{line}")
            continue
        print(f"create   {name}", flush=True)
        subprocess.run(cmd, check=True)
        created += 1
    if args.dry_run:
        print(f"\narm env ({env_file}):\n" + evalspec.env_file_text(args.arm))
    if problems:
        print(f"\n{len(problems)} problem(s): {'nothing would launch' if args.dry_run else 'nothing launched'}",
              file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\ncreated {created}, skipped {skipped}. Watch: gcloud compute instances list "
              f"--project={PROJECT} --filter=labels.purpose=v3-eval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
