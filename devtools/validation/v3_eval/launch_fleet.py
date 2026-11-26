#!/usr/bin/env python3
# Responsibility: Launch the WHOLE CRS v3 verdict (T19 + T29, both clean controls) as one wave of
#                 labelled, self-deleting GCE VMs in hexera-dev / us-central1, one shard process per
#                 vCPU.
# Owns: argument validation (machine family, the 100-vCPU ceiling, pinned image, full commits, the
#       toolchain-drift rule), the shard plan and its packing onto VMs, VM names/labels/metadata, and
#       the resume rule (skip shards that are DONE or already running).
#       The v3 bindings (metadata keys, purpose label, bucket prefix, arms, run_arm path, names) are
#       a PROFILE; `--profile v4` drives the same launcher over the v4 re-probe wave.
#       THE BUDGET GATE: no wave, v3 or v4, creates a VM except through `create_wave`, which runs
#       `enforce_budget` first. `--allow-over-budget` waives the VM-hour ceiling only.
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

THE V4 PROFILE. `--profile v4` launches the re-probe wave: units are `reprobe:pool:I/N` (hash shards
of the pool, evalspec.shard_of), metadata keys are v4-*, VMs are labelled purpose=v4-eval and run
v4_eval/run_arm.py. `--sources` names the pool sources (default: the cleared ones; Fusion 360 is not
cleared and is refused) and reaches run_arm as --sources, so an uncleared source is never probed even if
the pool file carries it. The default (v3) profile is byte-identical to the launcher before profiles.

RESUME. Re-running the same command launches only the shards that have no DONE marker and are not
claimed by a running VM (read off each eval VM's v3-units metadata). --only narrows it further.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: THE ONE DEFINITION of the project, bucket, service account, digest grammar, cache prefix and results
#: root. v4_eval/preflight.py imports the same module, so what it validates is what this launcher writes.
wave_spec = _load("v4_wave_spec", REPO / "devtools" / "validation" / "v4_eval" / "wave_spec.py")
PROJECT, PROJECT_NUMBER, BUCKET = wave_spec.PROJECT, wave_spec.PROJECT_NUMBER, wave_spec.BUCKET
SERVICE_ACCOUNT, DIGEST_RE, V4_PREFIX = wave_spec.SERVICE_ACCOUNT, wave_spec.DIGEST_RE, wave_spec.V4_PREFIX
REGION = "us-central1"
#: Inputs and code (staged once by stage.py / clean_control.py fetch).
PREFIX = f"{BUCKET}/eval/v3-verdict"
#: The label every crsv3 resource carries (`make destroy-env SLUG=crsv3` uses DEPLOYMENT_ID=dev-crsv3).
DEPLOYMENT_ID = "dev-crsv3"
#: The quota metric that bounds each allowed family in us-central1.
QUOTA_METRIC = {"c2d": "C2D_CPUS", "c3": "C3_CPUS", "c3d": "C3D_CPUS", "c2": "C2_CPUS"}
#: Paths whose change between the image's commit and the code commit the image cannot absorb.
TOOLCHAIN_PATHS = ("requirements/", "pyproject.toml", "Dockerfile", "deploy/docker/")


evalspec = _load("evalspec", HERE / "evalspec.py")
#: The v4 spec holds the cost control (Projection, check_budget, enforce_budget, BudgetRefused) for
#: BOTH profiles, and the cleared-source list. It is loaded under the name run_arm.py uses for it.
budget_spec = _load("v4_evalspec", REPO / "devtools" / "validation" / "v4_eval" / "evalspec.py")
# V4_PREFIX now comes from wave_spec (task 8d): one definition of the wave's prefix, imported above.
#: The v4 wave controls: the probe + its pass record, and the wave-count limits / shard-index arithmetic.
#: Loaded for BOTH profiles' sake of one module graph, but only the v4 path ever calls them.
marker_probe = _load("v4_marker_probe", REPO / "devtools" / "validation" / "v4_eval" / "marker_probe.py")
wave_limits = _load("v4_wave_limits", REPO / "devtools" / "validation" / "v4_eval" / "wave_limits.py")
#: The bucket the ledger's markers live in: the wave bucket itself, written with the native GCS conditional
#: create over ADC (the controller's here, the VM service account's on a shard). The probe proves THIS bucket; a
#: shard is handed the same name by metadata and refuses unless it reads the probe's record back.
MARKER_BUCKET = BUCKET.removeprefix("gs://")
MARKER_BACKEND = f"gcs/{MARKER_BUCKET}"


@dataclass(frozen=True)
class Profile:
    """Everything the launcher binds to one evaluation: the VM metadata key prefix, the purpose and
    managed-by labels, the deployment id, the VM name tag, the bucket the code/inputs live under, the
    arms (what DONE markers and running VMs are matched against) and the run_arm.py the VM invokes."""
    name: str
    meta: str                 # VM metadata key prefix: f"{meta}-units", f"{meta}-commit", ...
    purpose: str              # the purpose label, also the filter that finds this wave's live VMs
    managed_by: str
    deployment_id: str
    vm_tag: str               # VM name: {deployment_id}-{vm_tag}-{launch}-{j}of{m}
    prefix_required: str      # --prefix must start with this
    prefix_owner: str
    default_prefix: str
    default_max_run_hours: int
    arms: tuple[str, ...]     # arm names as they appear in unit ids and results roots
    kinds: tuple[str, ...]    # kind names as they appear in unit ids and results paths
    run_arm: str | None       # repo-relative run_arm.py; None = the startup script's default (v3's)
    model_secret: str | None
    stage_hint: str

    def arm_short(self, arm: str) -> str:
        raise NotImplementedError

    def shard_count(self, total: int) -> int:
        raise NotImplementedError

    def units(self, total: int) -> list[str]:
        raise NotImplementedError

    def extra_metadata(self, args) -> list[str]:
        return []

    def plan_lines(self, args) -> list[str]:
        raise NotImplementedError

    def launch_tail(self, args) -> str:
        raise NotImplementedError

    def required_objects(self, args) -> list[str]:
        raise NotImplementedError

    def required_dirs(self, args) -> list[str]:
        raise NotImplementedError

    def wave_preflight(self, args) -> tuple[list[str], list[str]]:
        """(problems, notes) from the profile's own proof that the fleet can use what it is launched
        against. v3 has none; v4 runs v4_eval/preflight.py."""
        return [], []

    def preflight_arms(self) -> tuple[str, ...]:
        return ()

    def projection(self, args, units: list[str], vms: int, slots: int):
        raise NotImplementedError


class V3Profile(Profile):
    """The v3 verdict wave: evalspec.ARMS x KINDS, units `arm:kind:I/N`, the v3-* metadata."""

    def arm_short(self, arm: str) -> str:
        return evalspec.arm(arm)["short"]

    def shard_count(self, total: int) -> int:
        return sum(evalspec.shard_plan(total).values())

    def units(self, total: int) -> list[str]:
        return evalspec.work_units(evalspec.shard_plan(total))

    def plan_lines(self, args) -> list[str]:
        plan = evalspec.shard_plan(args.shards_total)
        minutes = evalspec.plan_minutes(plan)
        slots = evalspec.vcpus_of(args.machine_type)
        lines = [f"plan ({args.shards_total} shards, one per vCPU, {slots} per {args.machine_type}):"]
        for (arm, kind), n in plan.items():
            lines.append(f"  {arm:<20} {kind:<8} {n:>3} shard(s) x <= "
                         f"{-(-evalspec.files_of(kind) // n)} file(s)  ~{minutes[(arm, kind)]:.0f} min"
                         f"  [{evalspec.arm(arm)['model_role']}]")
        return lines

    def launch_tail(self, args) -> str:
        return (f"model key from secret {evalspec.MODEL_SECRET}; "
                f"known no-ops: {evalspec.KNOWN_NOOPS}")

    def required_objects(self, args) -> list[str]:
        return [code_uri(args.commit, args.prefix), f"{args.prefix}/sample.json",
                f"{args.prefix}/clean-control-100.json"]

    def required_dirs(self, args) -> list[str]:
        return [f"{args.prefix}/inputs/{kind}/" for kind in evalspec.KINDS]

    def preflight_arms(self) -> tuple[str, ...]:
        return tuple(evalspec.ARMS)

    def projection(self, args, units: list[str], vms: int, slots: int):
        """The wave's cost from the shard plan's own estimates: the slowest shard sets the wall every
        VM bills; the worst case is every VM running to its --max-run-duration."""
        minutes: list[float] = []
        for unit in units:
            arm, kind, _, n = evalspec.parse_unit(unit)
            minutes.append(-(-evalspec.files_of(kind) // n) * evalspec.MINUTES_PER_FILE[(arm, kind)])
        wall = max(minutes) / 60.0
        return budget_spec.Projection(
            files=0, full_files=0, shards=len(units), vms=vms, vcpus_per_vm=slots,
            cpu_hours=sum(minutes) / 60.0, wall_hours=wall, vm_hours=vms * wall,
            max_run_hours=args.max_run_hours, worst_vm_hours=vms * args.max_run_hours, skew=1.0)


class V4Profile(Profile):
    """The v4 re-probe wave: one `reprobe` arm over the whole pool, hash-sharded, units
    `reprobe:pool:I/N`, the v4-* metadata, v4_eval/run_arm.py in the container."""

    def arm_short(self, arm: str) -> str:
        return arm

    def shard_count(self, total: int) -> int:
        if total < 1:
            raise ValueError("--shards-total must be at least 1")
        return total

    def units(self, total: int, arm: str = "reprobe") -> list[str]:
        return [f"{arm}:pool:{i}/{total}" for i in range(1, total + 1)]

    def extra_metadata(self, args) -> list[str]:
        # the model ledger's inputs: the wave type (the cap comes from its table), its id, N (the same N for
        # every shard) and the bucket whose markers they use (the one the probe proved; they re-check it)
        return [f"v4-run-arm={self.run_arm}", f"v4-pool={args.prefix}/pool.json",
                # what the launcher proved about the toolchain image: its org.opencontainers.image.revision label
                # equals --image-commit (preflight `image_commit_label`), or "unverified" when no check ran
                f"v4-image-commit-link={getattr(args, 'image_commit_link', None) or 'unverified'}",
                f"v4-cache-uri={wave_spec.verdict_cache_uri(args.prefix)}",
                f"v4-sources={','.join(args.sources)}",
                f"v4-wave-id={args.wave_id}", f"v4-wave-type={args.wave_type}",
                f"v4-n-shards={args.shards_total}", f"v4-marker-bucket={MARKER_BUCKET}",
                # a cost-balanced shard plan (wave.py): every shard is cut from it instead of the hash
                *([f"v4-shard-plan={args.shard_plan_uri}"] if getattr(args, "shard_plan_uri", None) else []),
                # an ARMS wave (below) only; empty for the re-probe, whose startup command is unchanged
                *([f"v4-wave={args.wave}", f"v4-arms={','.join(args.arms_list)}", f"v4-split={args.split}",
                   f"v4-split-uri={args.split_uri}", f"v4-h-mm={args.h_mm if args.h_mm is not None else ''}"]
                  if getattr(args, "arms_list", None) else []),
                *self.plan_metadata(args),
                # a model arm's key: ONLY a secret the operator named (--model-secret); there is no default
                *([f"v4-model-secret={args.model_secret}"] if getattr(args, "model_secret", None) else [])]

    def plan_metadata(self, args) -> list[str]:
        """An ARMS wave hands every shard `plan_wave(...).provenance()` so the shard can check it against its own flags
        and copy it into its PROVENANCE.json. Before the slot is claimed (a dry run) the value says so instead of
        inventing a slot; `vm_startup.sh` refuses a value that is not a JSON object."""
        if getattr(args, "wave", None) is None:
            return []
        plan = getattr(args, "plan", None)
        if plan is None:
            return ["v4-plan-provenance=UNCLAIMED-dry-run"]
        return [f"v4-plan-provenance={json.dumps(plan.provenance(), sort_keys=True)}"]

    def pool_files(self, args) -> int:
        """Files the wave will probe: --files, else the pool manifest's entries in the named sources."""
        if args.files is not None:
            return args.files
        pool = _load("v4_pool_manifest", REPO / "devtools" / "validation" / "v4_eval" / "pool.py")
        wanted = set(args.sources)
        return sum(1 for e in pool.read_pool(Path(args.pool)) if e.source in wanted)

    def plan_lines(self, args) -> list[str]:
        cut = (f"cost-balanced shards from {args.shard_plan_uri}" if getattr(args, "shard_plan_uri", None)
               else "hash shards of the pool")
        return [f"plan ({args.shards_total} {cut}, one per vCPU, "
                f"{evalspec.vcpus_of(args.machine_type)} per {args.machine_type}):",
                f"  {'reprobe' if not getattr(args, 'arms_list', None) else ','.join(args.arms_list):<20} pool     "
                f"{args.shards_total:>3} shard(s)  {args.files_counted} files from sources {','.join(args.sources)}"
                + (f"; split {args.split} ({args.split_uri}), unit {args.unit_arm}"
                   if getattr(args, "arms_list", None) else "")]

    def wave_preflight(self, args) -> tuple[list[str], list[str]]:
        """Run v4_eval/preflight.py against THIS launch's image, commit, prefix, pool and sources. Every
        failed check is a problem (nothing launches); a passing check's caveat is returned as a note."""
        pf = _load("v4_preflight", REPO / "devtools" / "validation" / "v4_eval" / "preflight.py")
        report = pf.run_preflight(
            image=args.image, commit=args.commit, image_commit=args.image_commit, runner=pf.gcloud_runner(),
            locations=pf.locations_for_prefix(args.prefix, args.commit),
            staged_sources=args.sources, pool_path=Path(args.pool))
        # the image's own label, checked against --image-commit, is what every VM records (`extra_metadata`)
        args.image_commit_link = next((c.detail for c in report.checks if c.name == "image_commit_label" and c.ok),
                                      None)
        return ([f"preflight {c.name}: {c.detail}" for c in report.checks if not c.ok],
                [f"preflight {c.name}: {c.caveat}" for c in report.checks if c.ok and c.caveat])

    def launch_tail(self, args) -> str:
        cap = wave_limits.WAVE_CAPS_USD[args.wave_type]
        key = (f"model key from secret {args.model_secret}" if getattr(args, "model_secret", None)
               else "the checker needs no model key")
        return (f"sources {','.join(args.sources)}; {key}; {args.wave_type} wave "
                f"{args.wave_id}: model cap ${cap:,.2f} over {args.shards_total} shards = "
                f"${cap / args.shards_total:,.4f} each")

    def required_objects(self, args) -> list[str]:
        return [code_uri(args.commit, args.prefix), f"{args.prefix}/pool.json",
                *([args.split_uri] if getattr(args, "arms_list", None) else []),
                *([args.shard_plan_uri] if getattr(args, "shard_plan_uri", None) else [])]

    def required_dirs(self, args) -> list[str]:
        return [f"{args.prefix}/inputs/"]

    def projection(self, args, units: list[str], vms: int, slots: int):
        """evalspec.project over the files these units carry (a resume costs only what is left)."""
        share = len(units) / args.shards_total
        return budget_spec.project(math.ceil(args.files_counted * share), shards=len(units),
                                   machine_type=args.machine_type, max_run_hours=args.max_run_hours)


V3 = V3Profile(
    name="v3", meta="v3", purpose="v3-eval", managed_by="v3-eval", deployment_id=DEPLOYMENT_ID,
    vm_tag="v3e", prefix_required="gs://dev-crsv3-", prefix_owner="a crsv3-owned bucket",
    default_prefix=PREFIX, default_max_run_hours=3, arms=tuple(evalspec.ARMS),
    kinds=evalspec.KINDS, run_arm=None, model_secret=evalspec.MODEL_SECRET,
    stage_hint="run stage.py")
V4 = V4Profile(
    name="v4", meta="v4", purpose="v4-eval", managed_by="v4-eval", deployment_id=DEPLOYMENT_ID,
    vm_tag="v4e", prefix_required="gs://dev-crsv3-", prefix_owner="an existing eval bucket",
    default_prefix=V4_PREFIX, default_max_run_hours=8, arms=("reprobe",), kinds=("pool",),
    run_arm="devtools/validation/v4_eval/run_arm.py", model_secret=None,
    stage_hint="stage it first (Task 8c)")
PROFILES = {p.name: p for p in (V3, V4)}


def profile_of(args) -> Profile:
    """The profile an args namespace selects; one without a `profile` is v3 (the pre-profile shape)."""
    return PROFILES[getattr(args, "profile", "v3")]


def results_root(arm: str, commit: str, bucket: str = BUCKET) -> str:
    """THE DETACHED RESULTS ROOT: every shard of one arm at one engine commit writes under
    <bucket>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/, so collect.py needs GCS and nothing else."""
    return wave_spec.results_root(arm, commit, bucket)


def out_uri(arm: str, commit: str, kind: str, i: int, n: int, bucket: str = BUCKET) -> str:
    return f"{results_root(arm, commit, bucket)}/{kind}/shard-{i}-of-{n}"


def code_uri(commit: str, prefix: str = PREFIX) -> str:
    return f"{prefix}/code-{commit[:12]}/code.tgz"


#: Labels the launcher derives from the wave itself; `--label` may not replace them (collect, the watch and the
#: resume logic read them back).
DERIVED_LABELS = frozenset({"commit", "launch", "shards", "arms", "app", "managed-by"})
_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")


def parse_label_overrides(items: list[str] | None) -> dict[str, str]:
    """`--label KEY=VALUE` (repeatable) as a dict. Only identity labels may be replaced (deployment-id, purpose) or
    added (owner, ...); a derived label or a value GCE would reject is a ValueError."""
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _LABEL_KEY_RE.match(key) or not _LABEL_VALUE_RE.match(value):
            raise ValueError(f"--label wants KEY=VALUE in GCE label syntax (lowercase), not {item!r}")
        if key in DERIVED_LABELS:
            raise ValueError(f"--label {key} is derived from the wave and cannot be overridden")
        out[key] = value
    return out


def label_overrides(args) -> dict[str, str]:
    return dict(getattr(args, "label_overrides", None) or {})


def purpose_of(args, profile: Profile) -> str:
    """The purpose label this launch's VMs carry, and so the filter that finds them (live units, resume)."""
    return label_overrides(args).get("purpose", profile.purpose)


def deployment_id_of(args, profile: Profile) -> str:
    return label_overrides(args).get("deployment-id", profile.deployment_id)


def vm_name(launch_id: str, j: int, m: int, profile: Profile = V3, deployment_id: str | None = None) -> str:
    name = f"{deployment_id or profile.deployment_id}-{profile.vm_tag}-{launch_id}-{j}of{m}"
    if len(name) > 63:
        raise ValueError(f"{name} is longer than 63 characters")
    return name


def labels(commit: str, launch_id: str, units: list[str], profile: Profile = V3,
           overrides: dict[str, str] | None = None) -> str:
    arms = sorted({profile.arm_short(u.split(":")[0]) for u in units})
    out = {"deployment-id": profile.deployment_id, "purpose": profile.purpose, "commit": commit[:12],
           "launch": launch_id, "shards": str(len(units)), "arms": "-".join(arms), "app": "hexera",
           "managed-by": profile.managed_by}
    for key, value in (overrides or {}).items():
        if key in DERIVED_LABELS:
            raise ValueError(f"label {key} is derived from the wave and cannot be overridden")
        out[key] = value
    return ",".join(f"{k}={v}" for k, v in out.items())


def create_command(args, launch_id: str, j: int, m: int, units: list[str]) -> list[str]:
    profile = profile_of(args)
    k = profile.meta
    metadata = "|".join([
        f"{k}-image={args.image}", f"{k}-commit={args.commit}",
        f"{k}-image-commit={args.image_commit}", f"{k}-units={','.join(units)}",
        f"{k}-bucket={BUCKET}", f"{k}-prefix={args.prefix}",
        f"{k}-code={code_uri(args.commit, args.prefix)}",
        f"{k}-machine-type={args.machine_type}",
        *([f"{k}-model-secret={profile.model_secret}"] if profile.model_secret else []),
        *profile.extra_metadata(args),
    ])
    return [
        "gcloud", "compute", "instances", "create", vm_name(launch_id, j, m, profile, deployment_id_of(args, profile)),
        f"--project={PROJECT}", f"--zone={args.zone}", f"--machine-type={args.machine_type}",
        "--provisioning-model=STANDARD", "--instance-termination-action=DELETE",
        f"--max-run-duration={args.max_run_hours}h",
        "--image-family=ubuntu-2204-lts", "--image-project=ubuntu-os-cloud",
        "--boot-disk-size=100GB", "--boot-disk-type=pd-balanced",
        "--network=default", f"--subnet=projects/{PROJECT}/regions/{REGION}/subnetworks/default",
        "--no-address", f"--service-account={SERVICE_ACCOUNT}", "--scopes=cloud-platform",
        f"--labels={labels(args.commit, launch_id, units, profile, label_overrides(args))}",
        "--metadata-from-file=" + ",".join([
            f"startup-script={HERE / 'vm_startup.sh'}",
            f"shutdown-script={HERE / 'vm_shutdown.sh'}"]),
        f"--metadata=^|^{metadata}",
    ]


def validate(parser: argparse.ArgumentParser, args) -> None:
    profile = profile_of(args)
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
    if not args.prefix.startswith(profile.prefix_required):
        parser.error(f"--prefix must be {profile.prefix_owner} ({profile.prefix_required}...)")
    slots = evalspec.vcpus_of(args.machine_type)
    try:
        shards = profile.shard_count(args.shards_total)
    except ValueError as exc:
        parser.error(str(exc))
    vms = -(-shards // slots)
    if vms * slots > evalspec.MAX_TOTAL_VCPU:
        parser.error(f"{shards} shards on {args.machine_type} need {vms} VMs = "
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


def v4_cache_problems(metadata: list[str]) -> list[str]:
    """A wave with no shared verdict cache is a cost control silently off. Checked on the `v4-cache-uri` item the
    VMs are actually handed (not re-derived), so a launcher that stops emitting it is refused too."""
    uris = [m.split("=", 1)[1] for m in metadata if m.startswith("v4-cache-uri=")]
    if len(uris) != 1:
        return [f"the wave hands its shards {len(uris)} v4-cache-uri items, not exactly one"]
    problem = wave_spec.shared_cache_problem(uris[0])
    return [f"verdict cache: {problem}"] if problem else []


def live_checks(args, vcpus: int) -> tuple[list[str], list[str]]:
    """Read-only state checks before anything is created. Returns (problems, warnings)."""
    problems, warnings = [], []
    profile = profile_of(args)
    try:
        desc = _gcloud_json(["gcloud", "artifacts", "docker", "images", "describe", args.image])
        tags = (desc.get("image_summary") or {}).get("tags") or []
        if args.image_commit[:12] not in tags:
            # A WARNING, not a refusal: the code runs from the archive, the image is the toolchain.
            warnings.append(f"the image carries tags {tags}, not {args.image_commit[:12]}")
    except RuntimeError as exc:
        # FAIL CLOSED: an image the registry will not describe is one the VMs may not be able to pull.
        problems.append(f"cannot describe the image: {exc}")
    more, notes = profile.wave_preflight(args)
    problems += more
    warnings += notes
    for obj in profile.required_objects(args):
        if subprocess.run(["gcloud", "storage", "ls", obj], capture_output=True).returncode:
            problems.append(f"{obj} is not staged ({profile.stage_hint})")
    for kind, path in zip(profile.kinds, profile.required_dirs(args), strict=True):
        if subprocess.run(["gcloud", "storage", "ls", path], capture_output=True).returncode:
            problems.append(f"no {kind} inputs staged under {path}")
    # Workers upload results to BUCKET (create_command's <profile>-bucket), whatever --prefix the inputs
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


def done_units(commit: str, profile: Profile = V3, arms: tuple[str, ...] | None = None) -> set[str]:
    """Unit ids whose shard wrote DONE, from one recursive listing per arm."""
    done = set()
    kinds = "|".join(profile.kinds)
    for arm in (arms if arms is not None else profile.arms):
        out = subprocess.run(["gcloud", "storage", "ls", "-r", results_root(arm, commit)],
                             capture_output=True, text=True)
        for line in out.stdout.splitlines():
            m = re.search(rf"/({kinds})/shard-(\d+)-of-(\d+)/DONE$", line.strip())
            if m:
                done.add(f"{arm}:{m.group(1)}:{m.group(2)}/{m.group(3)}")
    return done


def running_units(commit: str, profile: Profile = V3, purpose: str | None = None) -> tuple[set[str], list[str]]:
    """Unit ids claimed by eval VMs that still exist AND run `commit`, read off their v3-units and
    v3-commit metadata (v4-* for the v4 profile). Unit ids (arm:kind:I/N) carry no commit, so a VM still running an older
    commit's shards must not mark this commit's same-named shards as claimed. Every live eval VM
    is still returned by name, for the operator."""
    vms = _gcloud_json(["gcloud", "compute", "instances", "list", f"--project={PROJECT}",
                        f"--filter=labels.purpose={purpose or profile.purpose}"]) or []
    claimed: set[str] = set()
    for vm in vms:
        meta = {item.get("key"): item.get("value", "")
                for item in (vm.get("metadata") or {}).get("items") or []}
        if meta.get(f"{profile.meta}-commit") != commit:
            continue
        claimed.update(u for u in (meta.get(f"{profile.meta}-units") or "").split(",") if u)
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


def store_unreachable_errors() -> tuple[type[BaseException], ...]:
    """The exceptions that mean "the wave bucket could not be reached from this host" and nothing else.

    Deliberately NOT included: `google.auth.exceptions.RefreshError` (an expired or revoked ADC). It is not a
    `DefaultCredentialsError`, so on a dry run it is a refusal, not the UNCHECKED warning; that is the safe
    direction (the operator re-authenticates before spending). If the google libraries are not importable the
    tuple narrows to OSError: the ImportError raised by the gate code itself is then a refusal (exit 1), never
    a traceback out of the `except` clause that calls this."""
    try:
        from google.api_core.exceptions import GoogleAPICallError
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        return (OSError,)
    return (OSError, GoogleAPICallError, DefaultCredentialsError)


def default_gates():
    """(marker store, backend id) for the wave bucket over this host's ADC. Opening is lazy; the bucket is first
    touched when the gates read it."""
    return marker_probe.open_markers(f"gcs:{MARKER_BUCKET}")


def v4_gate_problems(args, markers, backend: str, *, claim: bool) -> list[str]:
    """Everything between a v4 wave and its first VM, as refusals: the marker store is the wave bucket, the probe
    has passed against it, and the wave-count limits allow this wave. With `claim`, a first launch takes its
    checkpoint / held-out slot (nothing is written without it)."""
    expected = MARKER_BACKEND
    problems: list[str] = []
    # NOT a binding: `backend` and `expected` both derive from MARKER_BUCKET, so no comparison of them can fail.
    # The binding is `marker_probe.probe_problem`, which reads the probe record through the store and refuses
    # unless the record names exactly `expected`.
    if backend != expected:  # a consistency check only (see above); it cannot fail on the real path
        problems.append(f"the marker store on this host is {backend!r}, not the wave bucket {expected!r}: what "
                        "it says about conditional create says nothing about the wave bucket")
    problem = marker_probe.probe_problem(markers, expected_backend=expected)
    if problem:
        problems.append(problem)
    if not problems:
        refusal = wave_limits.check_wave_count(markers, args.wave_type, args.wave_id, claim=claim)
        if refusal:
            problems.append(refusal)
    if not problems and getattr(args, "wave", None) is not None:
        # an ARM wave: plan_wave claims the slot the REPORT checks (`slot/...`), alongside the launcher's own count.
        # Without `claim` the slot is only looked at, never taken.
        wave_def = budget_spec.wave(args.wave)
        if claim:
            try:
                args.plan = budget_spec.plan_wave(wave_def, wave_id=args.wave_id, n_shards=args.shards_total,
                                                  markers=markers)
            except (budget_spec.WaveSlotSpent, ValueError) as exc:
                problems.append(str(exc))
        else:
            refusal = budget_spec.wave_slot_problem(wave_def, args.wave_id, markers)
            if refusal:
                problems.append(refusal)
    return problems


def create_wave(cmds: list[list[str]], projection, *, allow_over_budget: bool = False,
                notes: list[str] | None = None, gate=None, profile: Profile | None = None) -> None:
    """THE ONLY PLACE A VM IS CREATED, for both profiles. `enforce_budget` runs first and raises
    BudgetRefused, so a wave that fails the gate reaches no `gcloud compute instances create`.
    `allow_over_budget` waives the VM-hour ceiling only (the vCPU and run-duration limits stand).
    `gate`, when given (the v4 wave controls), runs after the budget and before the first create and raises
    WaveRefused to stop the wave; it is where a checkpoint / held-out slot is CLAIMED. `profile` is REQUIRED
    to be given, and a v4 wave without a gate is refused: forgetting it must not skip the probe and the
    wave-count limits."""
    if profile is None:
        raise TypeError("create_wave needs the profile of the wave it creates")
    if profile is V4 and gate is None:
        raise wave_limits.WaveRefused("a v4 wave is not created without its probe and wave-count gate")
    if projection is None:
        raise budget_spec.BudgetRefused("no cost projection: a wave is not created without one")
    budget_spec.enforce_budget(projection, allow_over_budget=allow_over_budget)
    if gate is not None:
        gate()
    for n, cmd in enumerate(cmds):
        print(f"create   {cmd[4]}  {(notes or [''] * len(cmds))[n]}".rstrip(), flush=True)
        subprocess.run(cmd, check=True)


def build_parser() -> argparse.ArgumentParser:
    """The launcher's command line. One definition: `main` parses with it and tests build their args from it,
    so a hand-listed namespace cannot drift from the flags the launcher reads."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="v3",
                        help="which evaluation to launch: v3 (the verdict wave, the default) or v4 "
                             "(the re-probe wave)")
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True,
                        help="the code that runs: its src/ and devtools/validation (code.tgz)")
    parser.add_argument("--image-commit", required=True,
                        help="the commit the image was built from (its tag); toolchain only")
    parser.add_argument("--allow-dep-drift", action="store_true")
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--shards-total", type=int, default=96)
    parser.add_argument("--zone", default=f"{REGION}-a")
    parser.add_argument("--max-run-hours", type=int, default=None,
                        help="the VMs' --max-run-duration (default: 3 for v3, 8 for v4)")
    parser.add_argument("--prefix", default=None,
                        help="where the code and inputs are staged (default: the profile's)")
    parser.add_argument("--only", default="",
                        help="resume: launch only these unit ids (arm:kind:I/N, comma-separated)")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="skip the host settings preflight (the container still runs it)")
    parser.add_argument("--allow-over-budget", action="store_true",
                        help="waive the ~600 c2d VM-hour ceiling for a deliberate wave (the vCPU "
                             "ceiling and the run-duration limit still apply)")
    parser.add_argument("--sources", default=None,
                        help="v4 only: comma list of pool sources to run (default: the cleared "
                             "ones; an uncleared source is refused)")
    parser.add_argument("--pool", default=None,
                        help="v4 only: the pool manifest the file count is read from")
    parser.add_argument("--files", type=int, default=None,
                        help="v4 only: the file count to cost the wave at (default: the pool's)")
    parser.add_argument("--wave-type", choices=sorted(wave_limits.WAVE_CAPS_USD), default=None,
                        help="v4 only, REQUIRED: the wave type; its model-spend cap comes from the ledger's "
                             "WAVE_CAPS_USD table (normal, checkpoint <= 3 waves, held_out exactly one)")
    parser.add_argument("--wave", choices=sorted(budget_spec.WAVES), default=None,
                        help="v4 only: run a named ARM wave (evalspec.WAVES): its type, split and arms come from the "
                             "definition and the launch goes through evalspec.plan_wave, which claims the once-only "
                             "slot and binds the cap. Without it the launch is the re-probe wave.")
    parser.add_argument("--wave-id", default=None,
                        help="v4 only, REQUIRED: this wave's id (lowercase letters, digits, dashes); reuse it to "
                             "resume the same wave, never for a different one")
    parser.add_argument("--arms", default=None,
                        help="v4 only: run these ARMS (comma list of input_only, staged, search_det, search_ai) over "
                             "a split instead of the re-probe; the shard runs them all under ONE ledger. Needs "
                             "--wave --split-uri --h-mm")
    parser.add_argument("--split-uri", default=None, help="v4 --arms only: gs:// URI of the split json to stage")
    parser.add_argument("--shard-plan-uri", default=None,
                        help="v4 only: gs:// URI of a cost-balanced shard plan (evalspec.build_shard_plan); every shard "
                             "is cut from it instead of the hash partition. Its n must equal --shards-total")
    parser.add_argument("--h-mm", type=float, default=None, help="v4 --arms only: element size for the search arms")
    parser.add_argument("--model-secret", default=None,
                        help="v4 --arms only: the Secret Manager secret holding OPENAI_API_KEY, REQUIRED when an arm "
                             "calls a model. There is no default: the v4 VM carries no model key of its own")
    parser.add_argument("--label", action="append", default=None, metavar="KEY=VALUE",
                        help="v4 only, repeatable: replace an identity label (deployment-id, purpose; deployment-id "
                             "also names the VMs) or add one (owner=...). Derived labels (commit, launch, shards, "
                             "arms, app, managed-by) cannot be replaced")
    parser.add_argument("--dry-run", action="store_true")
    # derived in main (_resolve_v4_arms); declared so the parser names every attribute the launcher reads
    parser.set_defaults(arms_list=None, unit_arm="reprobe", split=None)
    parser.set_defaults(plan=None)  # the WavePlan plan_wave returns at the claim; never a flag
    parser.set_defaults(label_overrides=None)  # parsed from --label in main
    return parser


def _resolve_v4_arms(parser: argparse.ArgumentParser, args) -> None:
    """Settle the v4 ARMS wave flags (or their absence: the re-probe). A model arm launches only with a named secret
    and the wave's own ledger inputs; the unit's arm label is unique to the wave so two waves of one commit cannot
    share a results directory."""
    args.arms_list, args.unit_arm, args.split = None, "reprobe", None
    given = [f for f, v in (("--arms", args.arms), ("--wave", args.wave), ("--split-uri", args.split_uri),
                            ("--h-mm", args.h_mm), ("--model-secret", args.model_secret)) if v is not None]
    if args.arms is None:
        if given:
            parser.error(f"{', '.join(given)} need --arms (the re-probe takes none of them)")
        return
    missing = [f for f, v in (("--wave", args.wave), ("--split-uri", args.split_uri)) if v is None]
    if missing:
        parser.error(f"--arms needs {', '.join(missing)}")
    try:
        wave = budget_spec.wave(args.wave)
        names = [budget_spec.arm(a.strip()).name for a in args.arms.split(",") if a.strip()]
    except ValueError as exc:
        parser.error(str(exc))
    bad = [a for a in names if a not in budget_spec.DISPATCHABLE_ARMS or a not in wave.arms]
    if not names or bad:
        parser.error(f"--arms {bad or 'is empty'}: must be dispatchable arms of wave {wave.name!r} "
                     f"({[a for a in wave.arms if a in budget_spec.DISPATCHABLE_ARMS]})")
    if wave.wave_type != args.wave_type:
        parser.error(f"--wave-type {args.wave_type} disagrees with wave {wave.name!r} ({wave.wave_type})")
    if not args.split_uri.startswith("gs://"):
        parser.error("--split-uri must be a gs:// object")
    if any(a in ("search_det", "search_ai") for a in names) and not (args.h_mm and args.h_mm > 0):
        parser.error("the search arms need --h-mm > 0: there is no default element size")
    if any(budget_spec.ARMS[a].uses_model for a in names) and not args.model_secret:
        parser.error("search_ai calls a model and the v4 VM carries no model key: name the Secret Manager secret "
                     "holding OPENAI_API_KEY with --model-secret (and make sure the VM service account may read it)")
    args.arms_list, args.split = names, wave.split
    args.unit_arm = "arms_" + re.sub(r"[^a-z0-9]", "_", args.wave_id)


def main(argv: list[str] | None = None, *, gates=None) -> int:
    """`gates` is (marker store, backend id) for the v4 wave controls; None means the wave bucket's own
    (default_gates). Tests inject a directory store; a v3 launch never reads it."""
    parser = build_parser()
    args = parser.parse_args(argv)
    profile = profile_of(args)
    if args.prefix is None:
        args.prefix = profile.default_prefix
    if args.max_run_hours is None:
        args.max_run_hours = profile.default_max_run_hours
    if profile is V3:
        stray = [f for f, v in (("--wave", args.wave), ("--sources", args.sources), ("--pool", args.pool),
                                ("--files", args.files), ("--wave-type", args.wave_type),
                                ("--wave-id", args.wave_id), ("--shard-plan-uri", args.shard_plan_uri),
                                ("--label", args.label))
                 if v is not None]
        if stray:
            parser.error(f"{', '.join(stray)} apply to --profile v4 only")
    else:
        if args.wave is not None:
            wave_def = budget_spec.wave(args.wave)
            if args.wave_type not in (None, wave_def.wave_type):
                parser.error(f"--wave-type {args.wave_type} disagrees with wave {args.wave!r}, which is a "
                             f"{wave_def.wave_type} wave: the type comes from the wave's definition")
            args.wave_type = wave_def.wave_type   # the cap is the ledger's for THIS type, never a typed one
        if args.wave_type is None or args.wave_id is None:
            parser.error("--profile v4 needs --wave-type and --wave-id: the model-spend cap comes from the "
                         "wave type and every shard's ledger is keyed by the wave id")
        if not wave_limits.WAVE_ID_RE.match(args.wave_id):
            parser.error(f"--wave-id {args.wave_id!r} must be 3-40 lowercase letters, digits or dashes")
        try:
            args.sources = budget_spec.resolve_sources(
                sorted(budget_spec.CLEARED_SOURCES) if args.sources is None
                else args.sources.split(","))
        except ValueError as exc:
            parser.error(str(exc))
        args.pool = args.pool or str(REPO / "devtools" / "validation" / "corpora" / "v4" / "pool.json")
        if args.files is None and not Path(args.pool).is_file():
            parser.error(f"cannot count the files: no pool manifest at {args.pool} (--pool or --files)")
        args.files_counted = V4.pool_files(args)
        _resolve_v4_arms(parser, args)
        try:
            args.label_overrides = parse_label_overrides(args.label)
        except ValueError as exc:
            parser.error(str(exc))
    validate(parser, args)
    args.prefix = args.prefix.rstrip("/")
    slots = evalspec.vcpus_of(args.machine_type)
    units = (profile.units(args.shards_total, args.unit_arm) if profile is V4 else profile.units(args.shards_total))
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
        for arm in profile.preflight_arms():
            problems += preflight_settings(arm)
    skipped: list[str] = []
    if not args.dry_run:
        try:
            done = done_units(args.commit, profile, (args.unit_arm,) if profile is V4 else None)
            claimed, live_vms = running_units(args.commit, profile, purpose_of(args, profile))
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

    gate_ctx = None
    if profile is V4 and units:
        try:
            wave_limits.shard_assignments(units, args.shards_total)
        except wave_limits.WaveRefused as exc:
            problems.append(str(exc))
        # The read-only half (claim=False) runs on a dry run too, so a broken wave-count gate shows up for free
        # instead of only on a live run. A bad answer refuses; an UNREACHABLE store on a dry run is a warning
        # (a dry run is often run without bucket access); on a live run it is a refusal.
        try:
            gate_ctx = gates if gates is not None else default_gates()
            problems += v4_gate_problems(args, *gate_ctx, claim=False)
        except Exception as exc:  # noqa: BLE001 - only a reachability failure is ever downgraded, below
            # Only a store that genuinely cannot be reached downgrades to a dry-run warning. A TypeError or
            # KeyError out of the gate code is a BUG: a dry run must fail on it (exit 1), not exit 0 and leave
            # it for a paid live run. On a live run every exception is a refusal.
            if args.dry_run and isinstance(exc, store_unreachable_errors()):
                warnings.append(f"dry run: the marker store could not be reached ({exc}); the probe record and "
                                "wave-count limits are UNCHECKED and will be enforced on a live launch")
            else:
                problems.append(f"cannot open or read the marker store, or the gate code failed: "
                                f"{type(exc).__name__}: {exc}")

    if profile is V4 and units:  # local, so a dry run reaches it: what the shards are HANDED, not what we derive
        problems += v4_cache_problems(profile.extra_metadata(args))

    # THE BUDGET GATE, in the report: the same check create_wave enforces before any VM exists.
    projection = profile.projection(args, units, len(vms), slots) if units else None
    if projection is not None:
        reasons = budget_spec.check_budget(projection, allow_over_budget=args.allow_over_budget)
        if args.allow_over_budget and max(projection.vm_hours, projection.worst_vm_hours) \
                > budget_spec.MAX_VM_HOURS:
            warnings.append(f"over the {budget_spec.MAX_VM_HOURS:g} VM-hour ceiling: overridden "
                            f"(--allow-over-budget)")
        problems += reasons

    print("\n".join(profile.plan_lines(args)))
    print(f"launch: {len(units)} shard(s) on {len(vms)} VM(s) = {len(vms) * slots} vCPU "
          f"(ceiling {evalspec.MAX_TOTAL_VCPU}); skipped {len(skipped)} done/running; code "
          f"{args.commit[:12]} (src/ from the archive), image built from "
          f"{args.image_commit[:12]} (toolchain); {profile.launch_tail(args)}")
    if projection is not None:
        print(f"cost: {projection.describe()}")
    for w in warnings:
        print(f"WARN    {w}", file=sys.stderr)
    for p in problems:
        print(f"REFUSE  {p}", file=sys.stderr)

    launch_id = time.strftime("%m%d%H%M", time.gmtime())
    cmds = [create_command(args, launch_id, j, len(vms), vm_units)
            for j, vm_units in enumerate(vms, start=1)]
    if args.dry_run or problems:
        for cmd in cmds:
            line = " ".join(shlex.quote(c) for c in cmd)
            print(f"{'DRY-RUN  ' if args.dry_run else 'WOULD    '}{line}")
    else:
        def claim_slot() -> None:
            if gate_ctx is None:
                raise wave_limits.WaveRefused("the v4 wave controls were not evaluated: refusing to launch")
            refused = v4_gate_problems(args, *gate_ctx, claim=True)
            if refused:
                raise wave_limits.WaveRefused("; ".join(refused))
            # the slot is known only now: re-render every VM's metadata with the plan's provenance, in place, so the
            # creates that follow carry what was claimed and not what was assumed
            cmds[:] = [create_command(args, launch_id, j, len(vms), vm_units)
                       for j, vm_units in enumerate(vms, start=1)]
            if args.wave is not None and getattr(args, "plan", None) is None:
                raise wave_limits.WaveRefused("an arm wave was not planned through plan_wave: refusing to launch")
        try:
            create_wave(cmds, projection, allow_over_budget=args.allow_over_budget,
                        notes=[f"({len(u)} shards)" for u in vms],
                        gate=claim_slot if profile is V4 else None, profile=profile)
        except wave_limits.WaveRefused as exc:
            print(f"REFUSE  {exc}", file=sys.stderr)
            return 1
    if problems:
        print(f"\n{len(problems)} problem(s): "
              f"{'nothing would launch' if args.dry_run else 'nothing launched'}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\nlaunched {len(vms)} VM(s). Watch: gcloud compute instances list "
              f"--project={PROJECT} --filter=labels.purpose={purpose_of(args, profile)} ; status: collect.py "
              f"--commit {args.commit} --status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
