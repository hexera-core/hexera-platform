#!/usr/bin/env python3
# Responsibility: ONE command for a CRS v4 wave: preflight -> stage -> launch (cost-balanced shards) -> watch
#                 (relaunch only failed shards, inside the VM backstop) -> collect -> triage -> report.
# Owns: the order of the steps, the wave's own refusals (VM-hour ceiling, model cap, shared cache, frozen commit), the
#       cost-balanced shard plan for THIS wave, the watch loop, and the wave directory (local + mirrored to the bucket).
# Boundaries: every step is the existing tool, called as a subprocess with the arguments it already takes:
#             preflight.py, launch_fleet.py (the only thing that creates a VM, behind its own gates), collect.py,
#             triage.py, report.py. A Backend is the only thing that touches gcloud; LocalBackend runs the same chain
#             against a directory "bucket" and local shard processes, so the chain is testable without a cloud.
"""Run a v4 wave end to end.

    wave.py --kind reprobe --commit <40-hex> --image <ref@sha256:..> --image-commit <sha> --wave-id reprobe-full-1
    wave.py --kind dev --wave dev_search --arms search_det,search_ai --model-usd-cap 50 --h-mm 2.0 \\
            --model-secret <secret> --commit <sha> --image ... --image-commit ... --wave-id dev-search-7
    wave.py --kind heldout --frozen-commit <sha> --commit <same sha> ...      # once, $250 cap
    wave.py ... --dry-run          # every step that can run without creating a VM; the launch is a launcher --dry-run

Steps, in order; each is recorded in <wave-dir>/steps.jsonl and the wave dir is mirrored to
<bucket>/eval/waves/<wave-id>/ after every step:
  1 preflight  the wave's own refusals (below), then preflight.py (IAM, image, staged inputs, cache) and the launcher's
               own gate as a --dry-run (budget, vCPU, marker probe, wave-count, cache URI)
  2 stage      code.tgz for --commit, the split json (arm waves), the shard plan (largest-first by predicted cost);
               inputs are staged once by stage.py and only CHECKED here (preflight's inputs_sources)
  3 launch     launch_fleet.py --profile v4 --shard-plan-uri ...
  4 watch      poll DONE / FAILED per shard; a FAILED shard, or one with no DONE and no live VM, is relaunched (at most
               --max-relaunch times); nothing is relaunched after launch + --max-run-hours (the VMs' own
               --max-run-duration backstop deletes them then), and the watch ends there
  5 collect    reprobe: collect.py --pull --report-only (the splits are cut by an explicit collect.py run, by a human);
               arm waves: the shard files are pulled for triage and report
  6 triage     triage.py (arm waves; the re-probe has no arm rows to triage)
  7 report     report.py (arm waves); the re-probe's report is collect-report.json

The wave's own refusals (before any gcloud call):
  - the VM-hour ceiling (evalspec.check_budget on the projection, worst case included)
  - --model-usd-cap: required when an arm calls a model; it must EQUAL the cap the shards' ledgers enforce for the
    wave type (model_ledger.WAVE_CAPS_USD): a lower cap would be printed but not enforced, so it is refused
  - the verdict cache URI is a shared gs:// prefix
  - held-out: --frozen-commit is required, 40-hex, equal to --commit, and an existing commit
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
import run_arm as _ra  # noqa: E402  (evalspec / reprobe / pool under their stable names)

evalspec = _ra.evalspec
reprobe_mod = _ra.reprobe_mod
pool_mod = _ra.pool_mod
wave_spec = _ra.wave_spec_mod
wave_limits = _ra.wave_limits
LAUNCHER = REPO / "devtools/validation/v3_eval/launch_fleet.py"
KIND_WAVE = {"dev": "dev_search", "checkpoint": "dev_checkpoint", "heldout": "heldout"}
KINDS = ("reprobe", *KIND_WAVE)
#: `cheap.jsonl` is here because `collect.REQUIRED_FILES` lists it and collect's re-probe path reads it
#: unconditionally: a wave that did not pull it crashed collect AFTER a successful watch (lane A9). An arm wave never
#: writes it, and a missing object just leaves the file absent, so pulling it is safe for every kind.
SHARD_FILES = ("PROVENANCE.json", "rows.json", "EXCLUDED.json", "cheap.jsonl", "DONE")
#: A shard with no DONE and no live VM is dead only after this long: a VM spends minutes booting and copying inputs.
BOOT_GRACE_S = 1800.0


class WaveRefused(RuntimeError):
    pass


# --------------------------------------------------------------------------- backends

@dataclasses.dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""


class Backend:
    """Everything that leaves this host. GcsBackend is production; LocalBackend maps gs:// onto a directory and runs
    shards as local processes."""

    name = "abstract"

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> Result:
        try:
            done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return Result(124, "", f"timeout: {exc}")
        return Result(done.returncode, done.stdout, done.stderr)

    def sizes(self, prefix: str) -> dict[str, int]:  # sha256 -> bytes of every staged input
        raise NotImplementedError

    def exists(self, uri: str) -> bool:
        raise NotImplementedError

    def put(self, local: Path, uri: str) -> None:
        raise NotImplementedError

    def shard_state(self, shard_uri: str) -> str:  # DONE / FAILED / running / missing
        raise NotImplementedError

    def live_units(self, commit: str, purpose: str = "v4-eval") -> set[str]:
        raise NotImplementedError

    def pull(self, shard_uri: str, dest: Path) -> None:
        raise NotImplementedError

    def mirror(self, local: Path, uri: str) -> None:
        raise NotImplementedError

    def preflight(self, w: WaveArgs) -> Result:
        raise NotImplementedError

    def launch(self, w: WaveArgs, *, dry_run: bool, only: Sequence[str] = ()) -> Result:
        raise NotImplementedError


class GcsBackend(Backend):
    name = "gcs"

    def sizes(self, prefix: str) -> dict[str, int]:
        # the flat glob (preflight.py: --recursive takes >600 s over ~11k prefixes)
        done = self.run(["gcloud", "storage", "ls", "-l", prefix.rstrip("/") + "/**"], timeout=600)
        if done.rc:
            raise WaveRefused(f"cannot list {prefix}: {done.err.strip()[:300]}")
        out: dict[str, int] = {}
        for line in done.out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit() and parts[-1].startswith("gs://"):
                rel = parts[-1][len(prefix.rstrip("/")) + 1:]
                out[rel.split("/", 1)[0]] = int(parts[0])
        return out

    def exists(self, uri: str) -> bool:
        return self.run(["gcloud", "storage", "ls", uri], timeout=120).rc == 0

    def put(self, local: Path, uri: str) -> None:
        done = self.run(["gcloud", "storage", "cp", str(local), uri], timeout=600)
        if done.rc:
            raise WaveRefused(f"cannot upload {local} to {uri}: {done.err.strip()[:300]}")

    def shard_state(self, shard_uri: str) -> str:
        done = self.run(["gcloud", "storage", "ls", shard_uri.rstrip("/") + "/"], timeout=120)
        names = {line.rstrip("/").rsplit("/", 1)[-1] for line in done.out.splitlines()}
        return "DONE" if "DONE" in names else "FAILED" if "FAILED" in names else "running" if names else "missing"

    def live_units(self, commit: str, purpose: str = "v4-eval") -> set[str]:
        done = self.run(["gcloud", "compute", "instances", "list", f"--project={wave_spec.PROJECT}",
                         f"--filter=labels.purpose={purpose} AND labels.commit={commit[:12]}",
                         "--format=json(name,metadata.items)"], timeout=120)
        units: set[str] = set()
        for vm in json.loads(done.out or "[]") if done.rc == 0 else []:
            for item in (vm.get("metadata") or {}).get("items", []):
                if item.get("key") == "v4-units":
                    units.update(u for u in item.get("value", "").split(",") if u)
        return units

    def pull(self, shard_uri: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for name in SHARD_FILES:
            self.run(["gcloud", "storage", "cp", f"{shard_uri}/{name}", str(dest / name)], timeout=600)

    def mirror(self, local: Path, uri: str) -> None:
        self.run(["gcloud", "storage", "rsync", "-r", "-x", r"shards/.*|collected/.*", str(local), uri], timeout=1800)

    def preflight(self, w: WaveArgs) -> Result:
        return self.run(preflight_argv(w), timeout=1200)

    def launch(self, w: WaveArgs, *, dry_run: bool, only: Sequence[str] = ()) -> Result:
        return self.run(launcher_argv(w, dry_run=dry_run, only=only), timeout=3600)


class LocalBackend(Backend):
    """gs://<bucket>/<path> is <root>/<bucket>/<path>; a "VM" is a local `shard_cmd` process per unit, run to
    completion inside launch(). preflight.py and the launcher are not run (they are gcloud); the wave's own refusals
    and every later step (plan, watch, collect, triage, report) are the real code."""

    name = "local"

    def __init__(self, root: Path, shard_cmd: Callable[[WaveArgs, str, Path], list[str]],
                 fail_once: Sequence[str] = ()) -> None:
        self.root, self.shard_cmd, self.fail_once = root, shard_cmd, set(fail_once)
        self.launches: list[list[str]] = []

    def path(self, uri: str) -> Path:
        if not uri.startswith("gs://"):
            raise ValueError(f"not a gs:// uri: {uri}")
        return self.root / uri[len("gs://"):]

    def sizes(self, prefix: str) -> dict[str, int]:
        base = self.path(prefix)
        return {p.parent.name: p.stat().st_size for p in base.glob("*/*") if p.is_file()}

    def exists(self, uri: str) -> bool:
        return self.path(uri).exists()

    def put(self, local: Path, uri: str) -> None:
        dest = self.path(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(local.read_bytes())

    def shard_state(self, shard_uri: str) -> str:
        d = self.path(shard_uri)
        return ("DONE" if (d / "DONE").exists() else "FAILED" if (d / "FAILED").exists()
                else "running" if d.exists() else "missing")

    def live_units(self, commit: str, purpose: str = "v4-eval") -> set[str]:
        return set()  # local shards run to completion inside launch()

    def pull(self, shard_uri: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for name in SHARD_FILES:
            src = self.path(shard_uri) / name
            if src.exists():
                (dest / name).write_bytes(src.read_bytes())

    def mirror(self, local: Path, uri: str) -> None:
        dest = self.path(uri)
        for p in local.rglob("*"):
            if p.is_file() and p.relative_to(local).parts[0] not in ("shards", "collected"):
                (dest / p.relative_to(local)).parent.mkdir(parents=True, exist_ok=True)
                (dest / p.relative_to(local)).write_bytes(p.read_bytes())

    def preflight(self, w: WaveArgs) -> Result:
        return Result(0, "local backend: preflight.py (IAM / registry / bucket) not run", "")

    def launch(self, w: WaveArgs, *, dry_run: bool, only: Sequence[str] = ()) -> Result:
        units = list(only) or units_of(w)
        self.launches.append(units)
        if dry_run:
            return Result(0, "\n".join(f"DRY-RUN local shard {u}" for u in units))
        for u in units:
            out = self.path(shard_uri(w, u))
            out.mkdir(parents=True, exist_ok=True)
            for marker in ("DONE", "FAILED"):
                (out / marker).unlink(missing_ok=True)
            if u in self.fail_once:  # a shard whose VM died: no DONE, a FAILED marker
                self.fail_once.discard(u)
                (out / "FAILED").write_text("simulated infrastructure failure\n")
                continue
            done = self.run(self.shard_cmd(w, u, out), timeout=3600)
            (out / "run.log").write_text(done.out + done.err)
            (out / ("DONE" if done.rc == 0 else "FAILED")).write_text("")
        return Result(0, f"ran {len(units)} local shard(s)")


# --------------------------------------------------------------------------- the wave

@dataclasses.dataclass
class WaveArgs:
    kind: str
    wave_id: str
    commit: str
    image: str
    image_commit: str
    shards: int
    max_run_hours: float
    zone: str
    prefix: str
    pool: Path
    wave: str | None = None
    arms: tuple[str, ...] = ()
    split: Path | None = None
    model_usd_cap: float | None = None
    model_secret: str | None = None
    h_mm: float | None = None
    frozen_commit: str | None = None
    machine_type: str = evalspec.DEFAULT_MACHINE
    labels: tuple[str, ...] = ()  # launch_fleet --label KEY=VALUE overrides (deployment-id, purpose, owner)

    @property
    def purpose(self) -> str:
        """The purpose label the wave's VMs carry: the watch finds live shards by it."""
        for item in self.labels:
            key, _, value = item.partition("=")
            if key.strip() == "purpose":
                return value.strip()
        return "v4-eval"

    @property
    def unit_arm(self) -> str:
        return "reprobe" if self.kind == "reprobe" else "arms_" + re.sub(r"[^a-z0-9]", "_", self.wave_id)

    @property
    def wave_type(self) -> str:
        return "normal" if self.kind == "reprobe" else evalspec.wave(self.wave or KIND_WAVE[self.kind]).wave_type

    @property
    def wave_def(self) -> Any:
        return None if self.kind == "reprobe" else evalspec.wave(self.wave or KIND_WAVE[self.kind])

    @property
    def plan_uri(self) -> str:
        return f"{self.prefix}/shard-plans/{self.wave_id}.json"

    @property
    def split_uri(self) -> str:
        return f"{self.prefix}/splits/{self.wave_id}.json"

    @property
    def results(self) -> str:
        return f"{wave_spec.results_root(self.unit_arm, self.commit)}/pool"


def units_of(w: WaveArgs) -> list[str]:
    return [f"{w.unit_arm}:pool:{i}/{w.shards}" for i in range(1, w.shards + 1)]


def shard_uri(w: WaveArgs, unit: str) -> str:
    i, n = unit.rsplit(":", 1)[1].split("/")
    return f"{w.results}/shard-{i}-of-{n}"


def preflight_argv(w: WaveArgs) -> list[str]:
    """preflight.py against THIS wave's prefix: a wave staged under `<bucket>/eval/<name>` is checked there (its own
    inputs against its own pool), not under the default re-probe prefix."""
    # `--image-commit` is REQUIRED: preflight's image_commit_label check refuses a launch without it, so a
    # preflight_argv that drops it refuses every wave (lane A9 caught exactly that in this merge).
    argv = [sys.executable, str(HERE / "preflight.py"), "--image", w.image, "--commit", w.commit,
            "--image-commit", w.image_commit, "--pool", str(w.pool)]
    root = f"{wave_spec.BUCKET}/eval/"
    if not w.prefix.startswith(root) or "/" in w.prefix[len(root):]:
        raise WaveRefused(f"--prefix {w.prefix} is not <bucket>/eval/<name>: preflight.py cannot check it")
    return argv + ["--name", w.prefix[len(root):]]


def launcher_argv(w: WaveArgs, *, dry_run: bool, only: Sequence[str] = ()) -> list[str]:
    argv = [sys.executable, str(LAUNCHER), "--profile", "v4", "--image", w.image, "--commit", w.commit,
            "--image-commit", w.image_commit, "--shards-total", str(w.shards), "--max-run-hours", f"{w.max_run_hours:g}",
            "--zone", w.zone, "--machine-type", w.machine_type, "--wave-id", w.wave_id, "--prefix", w.prefix,
            "--pool", str(w.pool), "--shard-plan-uri", w.plan_uri]
    if w.kind == "reprobe":
        argv += ["--wave-type", "normal"]
    else:
        argv += ["--wave", w.wave_def.name, "--split-uri", w.split_uri]
        if w.arms:
            argv += ["--arms", ",".join(w.arms)]
        if w.h_mm is not None:
            argv += ["--h-mm", f"{w.h_mm:g}"]
        if w.model_secret:
            argv += ["--model-secret", w.model_secret]
    for item in w.labels:
        argv += ["--label", item]
    if only:
        argv += ["--only", ",".join(only)]
    if dry_run:
        argv.append("--dry-run")
    return argv


def wave_files(w: WaveArgs) -> list[Any]:
    """The pool entries this wave runs: the cleared pool (re-probe) or the split's files."""
    allowed, _ = reprobe_mod.split_allowed(pool_mod.read_pool(w.pool))
    if w.kind == "reprobe":
        return allowed
    by_sha = {e.sha256: e for e in allowed}
    rows = reprobe_mod.read_split(w.split)
    missing = [r.sha256 for r in rows if r.sha256 not in by_sha]
    if missing:
        raise WaveRefused(f"the split names {len(missing)} file(s) the cleared pool does not carry")
    return [by_sha[r.sha256] for r in rows]


def own_refusals(w: WaveArgs, *, commit_exists: Callable[[str], bool]) -> list[str]:
    """The wave's refusals that need no cloud: ceiling, model cap, cache, held-out freeze."""
    reasons: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{40}", w.commit):
        reasons.append("--commit must be the full 40-hex sha")
    if not re.fullmatch(r"[0-9a-f]{40}", w.image_commit or ""):
        reasons.append("--image-commit must be the full 40-hex sha the image was built from (image_build.py prints "
                       "it); preflight checks it against the image's org.opencontainers.image.revision label")
    if not wave_limits.WAVE_ID_RE.match(w.wave_id):
        reasons.append(f"--wave-id {w.wave_id!r} must be 3-40 lowercase letters, digits or dashes")
    if w.kind != "reprobe" and (w.split is None or not Path(w.split).is_file()):
        reasons.append("an arm wave needs --split <split json>")
    files = len(wave_files(w)) if not reasons else 0
    projection = evalspec.project(files, shards=w.shards, machine_type=w.machine_type, max_run_hours=w.max_run_hours)
    reasons += [f"budget: {r}" for r in evalspec.check_budget(projection)]
    problem = wave_spec.shared_cache_problem(wave_spec.verdict_cache_uri(w.prefix))
    if problem:
        reasons.append(f"verdict cache: {problem}")
    if w.kind != "reprobe":
        arms = w.arms or w.wave_def.arms
        model = [a for a in arms if evalspec.arm(a).uses_model]
        cap = wave_limits.WAVE_CAPS_USD[w.wave_type]
        if model and w.model_usd_cap is None:
            reasons.append(f"arms {model} call a model: --model-usd-cap is required (the {w.wave_type} cap is ${cap:g})")
        elif w.model_usd_cap is not None and not math.isclose(w.model_usd_cap, cap):
            reasons.append(f"--model-usd-cap ${w.model_usd_cap:g} is not the ${cap:g} the shards' ledgers enforce for a "
                           f"{w.wave_type} wave: a different cap would be printed but not enforced")
        if model and not w.model_secret:
            reasons.append("a model arm needs --model-secret (the VMs carry no key of their own)")
    if w.kind == "heldout":
        if not w.frozen_commit or not re.fullmatch(r"[0-9a-f]{40}", w.frozen_commit):
            reasons.append("held-out needs --frozen-commit <40-hex>: it runs once, at a frozen commit")
        elif w.frozen_commit != w.commit:
            reasons.append(f"held-out runs the frozen commit {w.frozen_commit[:12]}, not --commit {w.commit[:12]}")
        elif not commit_exists(w.frozen_commit):
            reasons.append(f"the frozen commit {w.frozen_commit[:12]} is not in this repository")
    return reasons


def build_plan(w: WaveArgs, sizes: dict[str, int]) -> dict:
    files = wave_files(w)
    missing = [e.sha256 for e in files if e.sha256 not in sizes]
    if missing:
        raise WaveRefused(f"{len(missing)} file(s) of this wave are not staged (e.g. {missing[0][:12]})")
    return evalspec.build_shard_plan({e.sha256: evalspec.predicted_cost_s(sizes[e.sha256]) for e in files}, w.shards)


class Wave:
    def __init__(self, w: WaveArgs, backend: Backend, wave_dir: Path, *, dry_run: bool = False,
                 poll_s: float = 300.0, max_relaunch: int = 1, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.w, self.b, self.dir, self.dry_run = w, backend, wave_dir, dry_run
        self.poll_s, self.max_relaunch, self.clock, self.sleep = poll_s, max_relaunch, clock, sleep
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bucket_dir = f"{wave_spec.BUCKET}/eval/waves/{w.wave_id}" if backend.name == "gcs" else \
            f"{w.prefix}/waves/{w.wave_id}"

    # ---- bookkeeping
    def record(self, step: str, status: str, **detail: Any) -> None:
        rec = {"step": step, "status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **detail}
        with (self.dir / "steps.jsonl").open("a") as h:
            h.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        print(f"[{step}] {status} {json.dumps(detail, default=str)[:400]}", flush=True)
        if not self.dry_run:
            self.b.mirror(self.dir, self.bucket_dir)

    def tool(self, step: str, argv: list[str], *, ok: Sequence[int] = (0,), log: str | None = None) -> Result:
        done = self.b.run(argv, timeout=3600)
        (self.dir / (log or f"{step}.log")).write_text(f"$ {shlex.join(argv)}\n{done.out}\n{done.err}")
        if done.rc not in ok:
            self.record(step, "REFUSED", rc=done.rc, tail=(done.err or done.out)[-600:])
            raise WaveRefused(f"{step}: exit {done.rc}: {(done.err or done.out)[-300:]}")
        return done

    # ---- 1
    def preflight(self) -> None:
        def exists(sha: str) -> bool:
            return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{sha}^{{commit}}"],
                                  capture_output=True).returncode == 0

        reasons = own_refusals(self.w, commit_exists=exists)
        if reasons:
            self.record("preflight", "REFUSED", reasons=reasons)
            raise WaveRefused("; ".join(reasons))
        done = self.b.preflight(self.w)
        (self.dir / "preflight.log").write_text(done.out + done.err)
        if done.rc:
            self.record("preflight", "REFUSED", tail=(done.out + done.err)[-800:])
            raise WaveRefused("preflight.py refused: see preflight.log")
        self.record("preflight", "ok", backend=self.b.name)

    # ---- 2
    def stage(self) -> dict:
        w = self.w
        code = f"{w.prefix}/code-{w.commit[:12]}/code.tgz"
        if not self.b.exists(code):
            tgz = self.dir / "code.tgz"
            subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar.gz", "-o", str(tgz), w.commit, "src",
                            "devtools/validation"], check=True)
            if not self.dry_run:
                self.b.put(tgz, code)
        if w.kind != "reprobe":
            assert w.split is not None  # own_refusals refused an arm wave without one
            (self.dir / "split.json").write_bytes(Path(w.split).read_bytes())
            if not self.dry_run:
                self.b.put(self.dir / "split.json", w.split_uri)
        sizes = self.b.sizes(f"{w.prefix}/inputs")
        plan = build_plan(w, sizes)
        (self.dir / "shard-plan.json").write_text(json.dumps(plan, sort_keys=True) + "\n")
        if not self.dry_run:
            self.b.put(self.dir / "shard-plan.json", w.plan_uri)
        per = [0.0] * w.shards
        for sha, i in plan["assign"].items():
            per[int(i) - 1] += evalspec.predicted_cost_s(sizes[sha])
        self.record("stage", "ok", code=code, plan=w.plan_uri, plan_digest=evalspec.plan_digest(plan),
                    files=len(plan["assign"]), predicted_shard_h={"min": min(per) / 3600, "max": max(per) / 3600})
        return plan

    # ---- 3
    def launch(self) -> float:
        done = self.b.launch(self.w, dry_run=self.dry_run)
        (self.dir / "launch.log").write_text(done.out + done.err)
        if done.rc:
            self.record("launch", "REFUSED", rc=done.rc, tail=(done.err or done.out)[-800:])
            raise WaveRefused("the launcher refused: see launch.log")
        self.record("launch", "dry-run" if self.dry_run else "ok", units=self.w.shards)
        return self.clock()

    # ---- 4
    def watch(self, launched_at: float) -> dict[str, str]:
        w, deadline = self.w, launched_at + self.w.max_run_hours * 3600
        relaunched: dict[str, int] = {}
        while True:
            states = {u: self.b.shard_state(shard_uri(w, u)) for u in units_of(w)}
            if all(s == "DONE" for s in states.values()):
                self.record("watch", "ok", done=len(states), relaunched=relaunched)
                return states
            live = self.b.live_units(w.commit, w.purpose)
            booting = self.clock() - launched_at < BOOT_GRACE_S
            # dead: a FAILED marker, or (after the boot grace) no DONE and no live VM claiming the shard
            dead = [u for u, s in states.items()
                    if s == "FAILED" or (s != "DONE" and u not in live and not booting and self.b.name == "gcs")]
            retry = [u for u in dead if relaunched.get(u, 0) < self.max_relaunch]
            if self.clock() >= deadline:
                self.record("watch", "BACKSTOP", states=states, relaunched=relaunched,
                            note="launch + max-run-hours passed: the VMs' --max-run-duration has deleted them")
                raise WaveRefused(f"backstop: {sum(s != 'DONE' for s in states.values())} shard(s) not DONE")
            if dead and not retry:
                self.record("watch", "GAVE_UP", states=states, relaunched=relaunched)
                raise WaveRefused(f"shard(s) {dead} failed and used their {self.max_relaunch} relaunch(es)")
            if retry:
                for u in retry:
                    relaunched[u] = relaunched.get(u, 0) + 1
                self.record("watch", "relaunch", units=retry)
                done = self.b.launch(w, dry_run=False, only=retry)
                if done.rc:
                    self.record("watch", "REFUSED", tail=(done.err or done.out)[-600:])
                    raise WaveRefused("the relaunch was refused")
                continue
            self.sleep(self.poll_s)

    # ---- 5-7
    def collect(self) -> Path:
        shards = self.dir / "shards"
        for u in units_of(self.w):
            self.b.pull(shard_uri(self.w, u), shards / Path(shard_uri(self.w, u)).name)
        self.record("collect", "ok", into=str(shards))
        return shards

    def collect_reprobe(self, shards: Path) -> None:
        self.tool("collect", [sys.executable, str(HERE / "collect.py"), "--from", str(shards), "--shards",
                              str(self.w.shards), "--pool", str(self.w.pool), "--report-only",
                              "--shard-plan", str(self.dir / "shard-plan.json")], ok=(0, 2))
        self.record("report", "ok", report=str(shards / "collect-report.json"))

    def triage(self, shards: Path) -> None:
        self.tool("triage", [sys.executable, str(HERE / "triage.py"), "--from", str(shards), "--shards",
                             str(self.w.shards), "--split", str(self.w.split), "--json", str(self.dir / "triage.json"),
                             "--md", str(self.dir / "triage.md")])
        self.record("triage", "ok", json=str(self.dir / "triage.json"))

    def report(self, shards: Path) -> int:
        argv = [sys.executable, str(HERE / "report.py"), "--from", str(shards), "--wave", self.w.wave_def.name,
                "--shards", str(self.w.shards), "--split", str(self.w.split), "--json", str(self.dir / "report.json"),
                "--shard-plan", str(self.dir / "shard-plan.json")]
        if self.w.kind == "heldout":
            argv += ["--frozen-commit", str(self.w.frozen_commit)]
        done = self.tool("report", argv, ok=(0, 2))
        self.record("report", "ok" if done.rc == 0 else "UNRELIABLE", json=str(self.dir / "report.json"))
        return done.rc

    def run(self) -> int:
        (self.dir / "wave.json").write_text(json.dumps(dataclasses.asdict(self.w), default=str, indent=1) + "\n")
        self.preflight()
        self.stage()
        if self.dry_run:  # the launcher's own gates, without a VM
            self.launch()
            self.record("watch", "skipped", reason="dry run")
            return 0
        launched = self.launch()
        self.watch(launched)
        shards = self.collect()
        if self.w.kind == "reprobe":
            self.record("triage", "skipped", reason="the re-probe writes ProbeRows, not arm rows")
            self.collect_reprobe(shards)
            return 0
        self.triage(shards)
        return self.report(shards)


def parse(argv: list[str] | None = None) -> tuple[WaveArgs, argparse.Namespace]:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", required=True, choices=KINDS)
    ap.add_argument("--wave", default=None, choices=sorted(evalspec.WAVES), help="override the kind's default wave")
    ap.add_argument("--split", default=None, help="the split json (arm waves)")
    ap.add_argument("--arms", default="", help="comma list (default: the wave's arms)")
    ap.add_argument("--model-usd-cap", type=float, default=None)
    ap.add_argument("--model-secret", default=None)
    ap.add_argument("--h-mm", type=float, default=None)
    ap.add_argument("--frozen-commit", default=None)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--image-commit", required=True)
    ap.add_argument("--wave-id", required=True)
    ap.add_argument("--shards", type=int, default=96)
    ap.add_argument("--max-run-hours", type=float, default=None,
                    help="the VMs' --max-run-duration and the watch's deadline (default: 12 re-probe, 3 arm waves)")
    ap.add_argument("--zone", default="us-central1-b")
    ap.add_argument("--prefix", default=wave_spec.V4_PREFIX)
    ap.add_argument("--pool", default=str(pool_mod.OUT))
    ap.add_argument("--wave-dir", default=None, help="local wave directory (default: ./waves/<wave-id>)")
    ap.add_argument("--poll-s", type=float, default=300.0)
    ap.add_argument("--max-relaunch", type=int, default=1)
    ap.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                    help="passed to launch_fleet --label (deployment-id, purpose, owner); the watch filters on purpose")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    w = WaveArgs(kind=a.kind, wave_id=a.wave_id, commit=a.commit, image=a.image, image_commit=a.image_commit,
                 shards=a.shards, max_run_hours=a.max_run_hours or (12.0 if a.kind == "reprobe" else 3.0),
                 zone=a.zone, prefix=a.prefix.rstrip("/"), pool=Path(a.pool), wave=a.wave,
                 arms=tuple(x for x in a.arms.split(",") if x), split=Path(a.split) if a.split else None,
                 model_usd_cap=a.model_usd_cap, model_secret=a.model_secret, h_mm=a.h_mm, frozen_commit=a.frozen_commit,
                 labels=tuple(a.label))
    return w, a


def main(argv: list[str] | None = None, *, backend: Backend | None = None) -> int:
    w, a = parse(argv)
    wave = Wave(w, backend or GcsBackend(), Path(a.wave_dir or f"waves/{w.wave_id}"), dry_run=a.dry_run,
                poll_s=a.poll_s, max_relaunch=a.max_relaunch)
    try:
        return wave.run()
    except WaveRefused as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
