#!/usr/bin/env python3
# Responsibility: The CRS v4 re-probe wave as data: the harness flags (including the per-file wall
#                 kill), the shard plan, and the cost projection that refuses an over-budget wave.
# Owns: HARNESS_FLAGS, `harness_argv`, `shard_of`, `project`, `check_budget`.
# Boundaries: PURE. No gcloud, no docker, no CAD. The launcher, the in-container runner and the tests
#             all load this file.
"""The v4 wave, stated once.

THE KILL. Every harness invocation carries `--file-timeout-s`. `repair_corpus.py` has the flag (Task
8a) but nothing in the v3 fleet passed it, so the protection was off in exactly the runs that need it;
a hung native call then burns the shard. `harness_argv` cannot be built without it.

THE COST CONTROL. ~600 c2d VM-hours is the owner's ceiling for the sprint. `project` turns a wave
(files, tier mix, shards, machine, max run hours) into projected VM-hours and wall-clock hours;
`check_budget` refuses a wave whose expected OR worst-case VM-hours exceed the ceiling, or one whose wall exceeds the VM's --max-run-duration
(the VM would be DELETED mid-run), unless the caller passes `allow_over_budget`.

    wall_hours = SKEW x cpu_hours / shards    # shards run one per vCPU; the SLOWEST shard bills
    vm_hours = VMs x wall_hours               # expected
    worst_vm_hours = VMs x max_run_hours      # what the VMs can bill before --max-run-duration deletes them
    cpu_hours = files x full_min / 60          # the full verdict on EVERY file: there is no cheap pass

The per-file minutes are Task 43's x86 measurement (n=73, a tail-heavy sample): refresh them from the
wave's PROVENANCE after it runs (collect.py --measure).
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEED = 20261122
#: The owner's compute ceiling for the sprint, in c2d VM-hours.
MAX_VM_HOURS = 600.0
DEFAULT_MACHINE = "c2d-standard-8"
MAX_TOTAL_VCPU = 100
#: Per-file wall-clock kill, seconds. One file's checker calls are individually bounded (900 s each, the ONE
#: definition is `meshpipeline.cad.repair_v4.limits.CHECKER_CALL_TIMEOUT_S`); this bounds their SUM, so it must
#: stay larger than that. A file that hits it is EXCLUDED from the counts (reprobe.Excluded), is never a failure,
#: and is reported as its own stratum by collect.py (`report()["excluded_stratum"]`).
#:
#: 900.0 until 2026-11-23 (task 55). RAISED to 1800.0 from the measured distribution, because at 900 s the file
#: wall plus the 300 s call wall excluded ~8.7% of the pool (pool-weighted by file size) against collect.py's 2%
#: limit, so the wave would have ended at exit 2 with no split. Where 1800 comes from:
#:   - MEASURED (Lane D, 59 diagonal-stratified files, corpora/v4/measure_lane_d_20261123/files.jsonl): the
#:     slowest file that FINISHED took 643 s; 3 of 59 (4.7% pool-weighted) were killed at 900 s.
#:   - PROJECTED over the 10,887-file pool with project_reprobe.py's size model (log wall = -1.263 + 0.360 log
#:     bytes, R^2 0.21, residuals resampled): p90 309 s, p95 604 s, p98 1602 s, so 1800 s is the p98.2 and
#:     excludes a projected 1.8% of the pool (vs 3.4% at 900 s).
#:   - BOUNDED ABOVE by the VM's run duration, not by taste: re-running project_reprobe.py at each candidate cap
#:     over 96 cost-balanced shards gives a simulated wall of 5.2 h (p90 5.6) at 900 s, 6.7 h (p90 7.4) at
#:     1800 s, 7.6 h (p90 8.4) at 2400 s and 9.0 h (p90 10.2) at 3600 s. 8.0 h is the --max-run-hours
#:     `check_budget` is asked about by default (collect.py --measure, launch_reprobe.py), and a wave whose wall
#:     passes it has its VMs deleted mid-shard. 1800 s is the largest candidate whose simulated p90 still fits
#:     that 8 h; 2400 s is the first that does not. (A launch run with --max-run-hours 12, as the RUNBOOK's
#:     command does, has more room - the 8 h default is the number used here, stated so it can be argued with.)
#:   - THE COST OF THE RAISE: 335 -> 400 vCPU-h, 54.8 -> 68.5 VM-h, $20.7 -> $25.8 (96 shards, LPT, $0.377/VM-h).
#:     Per shard the worst case a single pathological file can burn doubles, from 900 s to 1800 s - about 7.4% of
#:     a 6.7 h shard instead of 4.8% of a 5.2 h one. That is what the wall is for and the price of widening it.
DEFAULT_FILE_TIMEOUT_S = 1800.0

#: Minutes of one vCPU per file for the FULL verdict (layer 1 + the panel + layer 3). The re-probe runs it on
#: every file (plan amendment 2026-11-23 19:15: the cheap-first pass is dropped). MEASURED by lane D on 2026-11-23 at
#: commit 5eefb130 (Lane A's 2.6x layer 1/3 speed-up included): 59 diagonal-stratified files, 8 shards pinned to
#: 4 cores' hyperthread pairs (a c2d-standard-8's shape), --cpus=1, cold cache. Sample median 25.5 s, mean 136 s,
#: p90 620 s; 5.1% hit the then-900 s file wall. One file used exactly one vCPU (cgroup CPU/wall 1.00, 1.2 s
#: throttled in 8,023 s). Re-weighted to the pool's file sizes (project_reprobe.py over the 10,887 staged input
#: sizes) this was 336 vCPU-h = 111 s/file AT THE 900 s WALL. The figure is a function of the wall, so task 55's
#: raise to DEFAULT_FILE_TIMEOUT_S = 1800 s re-derives it from the same sample and the same model at the new cap:
#: 400 vCPU-h for 10,887 files = 132.4 s/file (96 shards, LPT; 400.2-401.2 vCPU-h across the four shard plans).
FULL_MIN_PER_FILE = 132.4 / 60
#: Slowest-shard / mean-shard wall. Hash-partitioned shards are balanced in file COUNT, not in time:
#: file-wall timeouts and heavy parts make a long tail, and the slowest shard sets the billed time.
SKEW = 1.5

#: THE SOURCE LIST, as data. Every pool source label (pool.Source) a wave may name, and the subset the
#: owner has cleared for use. Fusion 360 Gallery is registered but NOT cleared: its licence is
#: non-commercial research only with redistribution barred, and the owner has not ruled. Clearing it
#: later is one edit to CLEARED_SOURCES; the launcher, the VM startup script and run_arm take the list
#: as an argument and need no change.
POOL_SOURCES: tuple[str, ...] = ("abc_fail", "abc_pass", "occt", "nist", "f360", "inject")
CLEARED_SOURCES: tuple[str, ...] = ("abc_fail", "abc_pass", "occt", "nist", "inject")


def resolve_sources(names: list[str] | tuple[str, ...],
                    cleared: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """The pool sources a wave may run over, de-duplicated in the order given. An unknown label, an
    empty list, or a source that is not cleared is a ValueError: a wave never quietly pulls in a
    dataset the owner has not signed off."""
    allowed = CLEARED_SOURCES if cleared is None else cleared
    wanted = tuple(dict.fromkeys(n.strip() for n in names if n.strip()))
    if not wanted:
        raise ValueError("no pool sources named")
    unknown = [n for n in wanted if n not in POOL_SOURCES]
    if unknown:
        raise ValueError(f"unknown pool source(s) {unknown}; the sources are {', '.join(POOL_SOURCES)}")
    blocked = [n for n in wanted if n not in allowed]
    if blocked:
        raise ValueError(f"pool source(s) {blocked} not cleared for use (licence not ruled on by the "
                         f"owner); cleared: {', '.join(allowed)}")
    return wanted


HARNESS_FLAGS: tuple[str, ...] = ("--mesh-probe", "--force-repair", "--goal", "benchmark",
                                  "--isolate-files", "--meshability")


def harness_argv(*, inputs: str, out: str, shard: str, file_timeout_s: float = DEFAULT_FILE_TIMEOUT_S,
                 extra: tuple[str, ...] = ()) -> list[str]:
    """repair_corpus.py's argv for one shard. `--file-timeout-s` is always present and positive."""
    if not file_timeout_s or file_timeout_s <= 0:
        raise ValueError("--file-timeout-s must be positive: a run without the wall kill can hang a shard")
    return [inputs, "--shard", shard, *HARNESS_FLAGS, "--file-timeout-s", f"{file_timeout_s:g}",
            "--json", f"{out}/rows.json", "--bundle", f"{out}/bundle", *extra]


def parse_shard(spec: str) -> tuple[int, int]:
    index, _, count = str(spec).partition("/")
    try:
        i, n = int(index), int(count)
    except ValueError as exc:
        raise ValueError(f"shard wants I/N, not {spec!r}") from exc
    if n < 1 or not 1 <= i <= n:
        raise ValueError(f"shard wants I/N with 1 <= I <= N, not {spec!r}")
    return i, n


def shard_of(sha256: str, i: int, n: int) -> bool:
    """Whether a file belongs to shard i of n: by hash, so the partition does not depend on the order
    of the manifest and every shard of a resumed wave takes the same files."""
    return int(hashlib.sha256(f"{SEED}:{sha256}".encode()).hexdigest()[:12], 16) % n == i - 1


# --------------------------------------------------------------------------- cost-balanced shards
#
# The hash partition (shard_of) balances file COUNT, not time: a few wall-killed files decide the slowest shard. A SHARD PLAN
# assigns files largest-first by PREDICTED cost to the currently lightest shard (LPT). The predictor is file size, the
# one feature known for every pool file before any CAD is read. A plan is a file; run_arm cuts from it, collect and
# report check against it, and every shard records its digest so a check against another plan (or none) refuses.

SHARD_PLAN_VERSION = 1
#: log(wall_s) = a + b * log(bytes), fitted by project_reprobe.py on measure_reprobe.py's sample. Only the ORDER of
#: predicted costs matters to LPT; the absolute scale is for the printed projection.
COST_MODEL: tuple[float, float] = (-1.263, 0.360)  # lane D, 59 files, R^2 0.21 (timeouts are not size-predictable)


def predicted_cost_s(size_bytes: int, model: tuple[float, float] = COST_MODEL) -> float:
    a, b = model
    return math.exp(a + b * math.log(max(int(size_bytes), 1)))


def build_shard_plan(costs: Mapping[str, float], n: int, *, model: tuple[float, float] = COST_MODEL) -> dict:
    """Largest-first: each file, by descending predicted cost (ties by sha256), onto the lightest shard (ties by
    index). Deterministic, so a relaunch or a second host rebuilds the same plan from the same costs."""
    if n < 1:
        raise ValueError("a shard plan needs n >= 1")
    heap = [(0.0, i) for i in range(1, n + 1)]
    assign: dict[str, int] = {}
    for sha, cost in sorted(costs.items(), key=lambda kv: (-float(kv[1]), kv[0])):
        load, i = heapq.heappop(heap)
        assign[sha] = i
        heapq.heappush(heap, (load + float(cost), i))
    return {"version": SHARD_PLAN_VERSION, "n": n, "model": list(model), "assign": dict(sorted(assign.items()))}


def plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_shard_plan(path: str | Path) -> dict:
    plan = json.loads(Path(path).read_text())
    if plan.get("version") != SHARD_PLAN_VERSION or not isinstance(plan.get("assign"), dict):
        raise ValueError(f"{path} is not a v{SHARD_PLAN_VERSION} shard plan")
    n = int(plan["n"])
    if any(not 1 <= int(i) <= n for i in plan["assign"].values()):
        raise ValueError(f"{path}: a shard index outside 1..{n}")
    return plan


def vcpus_of(machine_type: str) -> int:
    try:
        return int(machine_type.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"cannot read a vCPU count off {machine_type!r}") from exc


@dataclass(frozen=True)
class Projection:
    files: int
    full_files: int
    shards: int
    vms: int
    vcpus_per_vm: int
    cpu_hours: float
    wall_hours: float
    vm_hours: float
    max_run_hours: float
    worst_vm_hours: float = 0.0
    skew: float = SKEW

    def describe(self) -> str:
        return (f"{self.files} files ({self.full_files} on the full panel) on {self.shards} shards = "
                f"{self.vms} VM(s) x {self.vcpus_per_vm} vCPU; {self.cpu_hours:.0f} vCPU-h of work, "
                f"~{self.wall_hours:.1f} h wall for the slowest shard (skew x{self.skew:g}; VM cap "
                f"{self.max_run_hours:g} h); PROJECTED {self.vm_hours:.0f} c2d VM-hours, WORST CASE "
                f"{self.worst_vm_hours:.0f} (VMs x cap), ceiling {MAX_VM_HOURS:g}")


def project(files: int, *, shards: int, machine_type: str = DEFAULT_MACHINE, max_run_hours: float = 3.0,
            full_min: float = FULL_MIN_PER_FILE, skew: float = SKEW) -> Projection:
    if files < 0 or shards < 1:
        raise ValueError("files must be >= 0 and shards >= 1")
    vcpus = vcpus_of(machine_type)
    full_files = files  # every file gets the full verdict
    cpu_hours = files * full_min / 60.0
    vms = math.ceil(shards / vcpus)
    wall = skew * cpu_hours / shards
    return Projection(files, full_files, shards, vms, vcpus, cpu_hours, wall, vms * wall, max_run_hours,
                      vms * max_run_hours, skew)


class BudgetRefused(RuntimeError):
    pass


def check_budget(p: Projection, *, allow_over_budget: bool = False, ceiling: float = MAX_VM_HOURS) -> list[str]:
    """Reasons to refuse the wave (empty = go). Over the ceiling, over the 100-vCPU limit, or a wall
    that outlasts the VM's --max-run-duration (the VM is deleted with its shards unfinished).
    `allow_over_budget` waives the VM-hours ceiling ONLY; the vCPU limit and the run-duration limit
    are physical, not budgetary."""
    reasons: list[str] = []
    billed = max(p.vm_hours, p.worst_vm_hours)
    if billed > ceiling and not allow_over_budget:
        reasons.append(f"projected {p.vm_hours:.0f} / worst-case {p.worst_vm_hours:.0f} VM-hours exceeds the "
                       f"{ceiling:g} VM-hour ceiling (--allow-over-budget to override)")
    if p.vms * p.vcpus_per_vm > MAX_TOTAL_VCPU:
        reasons.append(f"{p.vms} VMs x {p.vcpus_per_vm} vCPU = {p.vms * p.vcpus_per_vm} vCPU, over the "
                       f"{MAX_TOTAL_VCPU}-vCPU ceiling")
    if p.wall_hours > p.max_run_hours:
        reasons.append(f"projected wall {p.wall_hours:.1f} h exceeds --max-run-hours {p.max_run_hours:g}: "
                       "the VMs would be deleted before their shards finish")
    return reasons


def enforce_budget(p: Projection, *, allow_over_budget: bool = False, ceiling: float = MAX_VM_HOURS) -> None:
    reasons = check_budget(p, allow_over_budget=allow_over_budget, ceiling=ceiling)
    if reasons:
        raise BudgetRefused("; ".join(reasons))



# ======================================================================================== arms and waves
#
# An ARM is a named way of treating each input; a WAVE is a named run of arms over ONE split, of one wave
# TYPE, and the type is what binds the spend cap. Both are data, here, once: run_arm, the launcher and
# report.py take names from this file and spell nothing out.

_REPO = Path(__file__).resolve().parents[3]


def _model_ledger() -> Any:
    """The wave-ledger module, imported lazily so this file stays loadable on a host with no `src` on the
    path (the launcher). The caps live THERE (`WAVE_CAPS_USD`); nothing in this file restates one."""
    try:
        from meshpipeline.cad.repair_v4 import model_ledger
    except ImportError:
        sys.path.insert(0, str(_REPO / "src"))
        from meshpipeline.cad.repair_v4 import model_ledger
    return model_ledger


def wave_cap_usd(wave_type: str) -> float:
    """The owner's cap for a wave TYPE. A type the ledger does not price is an error, never "no cap"."""
    caps = _model_ledger().WAVE_CAPS_USD
    if wave_type not in caps:
        raise ValueError(f"unknown wave type {wave_type!r}: it would get no spending cap; the wave types "
                         f"are {sorted(caps)}")
    cap = float(caps[wave_type])
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"wave type {wave_type!r} has cap {cap!r}: a cap must be finite and positive")
    return cap


#: The splits. `heldout` is reported once and NEVER used to tune, pick a prompt, fit a threshold or build a
#: memory; `clean` is the control (every input is already clean; the arms must not break one).
SPLITS: tuple[str, ...] = ("dev", "heldout", "clean")
HELDOUT = "heldout"
HELDOUT_WAVE_TYPE = "held_out"  # the ledger's name for the once-only $250 wave
#: How many checkpoint waves the owner allows. Enforced by `claim_wave_slot`; the ledger cannot.
MAX_CHECKPOINT_WAVES = 3


@dataclass(frozen=True)
class Budget:
    """What ONE file may cost one arm. wall and ops are shared by every arm so that an arm's win is not a
    bigger budget; `model_usd` is the model arm's extra, reported separately (AI marginal at equal wall
    and ops).

    `ops` counts APPLIED OPS, not candidates: a `run_stage` candidate is charged every op its stage body
    applied (`search.Budget`, Task 53), so neither arm can buy a chain of geometry work for one budget
    unit by routing through a multi-op stage."""

    wall_s: float = 600.0
    ops: int = 60
    model_usd: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.wall_s) or self.wall_s <= 0:
            raise ValueError(f"Budget.wall_s must be finite and positive, got {self.wall_s!r}")
        if self.ops < 1:
            raise ValueError(f"Budget.ops must be >= 1, got {self.ops!r}")
        if not math.isfinite(self.model_usd) or self.model_usd < 0:
            raise ValueError(f"Budget.model_usd must be finite and >= 0 (inf is refused), got {self.model_usd!r}")


@dataclass(frozen=True)
class Arm:
    name: str
    describes: str
    budget: Budget
    uses_model: bool  # makes paid model calls: needs the ledger, and its spend is reported
    #: whether THIS ledger sees the arm's spend. v3 calls a model but its spend is v3's own: its row says $0 and
    #: that is "not metered", never "free"; the report renders it as such.
    spend_metered: bool = True

    def __post_init__(self) -> None:
        if self.uses_model != (self.budget.model_usd > 0):
            raise ValueError(f"arm {self.name!r}: uses_model={self.uses_model} but model_usd="
                             f"{self.budget.model_usd}: a model arm needs a per-file model ceiling and "
                             "no other arm may have one")


SHARED_BUDGET = Budget()
#: Adding an arm is one line here.
ARMS: dict[str, Arm] = {a.name: a for a in (
    Arm("input_only", "the input passed through unchanged (the floor)", SHARED_BUDGET, False),
    Arm("staged", "the deterministic staged healer alone", SHARED_BUDGET, False),
    Arm("search_det", "best-first search, deterministic proposers only", SHARED_BUDGET, False),
    Arm("search_ai", "best-first search with the AI proposer",
        Budget(SHARED_BUDGET.wall_s, SHARED_BUDGET.ops, model_usd=1.00), True),
    # B24, owner direction 2026-11-24: one defect at a time, one repair each, LOCAL checks, one full panel per
    # round. Same wall and op cap as the other arms, so the comparison is of method and not of budget. It is
    # det-only for now (`uses_model` False): the AI pick decides only a table tie, and turning it on needs a
    # per-file model ceiling on this line, which is the owner's call and not a default.
    Arm("search_seq", "sequential defect repair: one defect, one repair, local checks, one panel per round",
        SHARED_BUDGET, False),
    Arm("v3_meshability_pass_ai", "v3 as it ships (meshability pass + AI), run once for comparison",
        SHARED_BUDGET, False, spend_metered=False),  # calls a model; its spend is v3's, not this ledger's
)}
#: v3 as it ships runs under v3_eval's own run_arm; every other arm is dispatched by v4_eval/run_arm.py.
V3_ARM = "v3_meshability_pass_ai"
DISPATCHABLE_ARMS: tuple[str, ...] = tuple(n for n in ARMS if n != V3_ARM)
#: The plan/spec names above are CANONICAL. These aliases exist only for the brief's wording; do not "tidy"
#: by deleting the real names (input_only is the do-nothing control every other arm is measured against).
ARM_ALIASES: dict[str, str] = {"baseline": "v3_meshability_pass_ai", "heal_det": "staged"}


def arm(name: str) -> Arm:
    key = ARM_ALIASES.get(name, name)
    if key not in ARMS:
        raise ValueError(f"unknown arm {name!r}; the arms are {sorted(ARMS)} (aliases {sorted(ARM_ALIASES)})")
    return ARMS[key]


@dataclass(frozen=True)
class WaveDef:
    """A named run of arms over one split. The wave TYPE is required and checked against the ledger's
    caps at construction, so a wave that would get no cap cannot be written down."""

    name: str
    wave_type: str
    split: str
    arms: tuple[str, ...]

    def __post_init__(self) -> None:
        wave_cap_usd(self.wave_type)
        if self.split not in SPLITS:
            raise ValueError(f"wave {self.name!r}: unknown split {self.split!r}; the splits are {SPLITS}")
        if not self.arms or len(set(self.arms)) != len(self.arms):
            raise ValueError(f"wave {self.name!r}: arms must be non-empty and unique, got {self.arms!r}")
        object.__setattr__(self, "arms", tuple(arm(a).name for a in self.arms))
        if (self.split == HELDOUT) != (self.wave_type == HELDOUT_WAVE_TYPE):
            raise ValueError(f"wave {self.name!r}: the held-out split runs in the held_out wave type and "
                             f"nothing else does (split {self.split!r}, type {self.wave_type!r})")

    @property
    def cap_usd(self) -> float:
        return wave_cap_usd(self.wave_type)

    @property
    def has_model_arm(self) -> bool:
        return any(ARMS[a].uses_model for a in self.arms)


_ALL = ("input_only", "staged", "search_det", "search_ai", "v3_meshability_pass_ai")
WAVES: dict[str, WaveDef] = {w.name: w for w in (
    WaveDef("dev_deterministic", "normal", "dev", ("input_only", "staged")),
    WaveDef("dev_search", "normal", "dev", ("search_det", "search_ai")),
    WaveDef("dev_checkpoint", "checkpoint", "dev", _ALL[:4]),
    WaveDef("heldout", HELDOUT_WAVE_TYPE, HELDOUT, _ALL),
    # Lane C14: the held-out run as the sprint ends it - the FOUR v4 arms every snapshot ran (no v3 arm), one
    # held_out slot, the held_out cap. `heldout` above keeps the v3 arm for whoever still wants it; only ONE of the
    # two can ever run, because both take the single `slot/held_out/1`.
    WaveDef("heldout_v4", HELDOUT_WAVE_TYPE, HELDOUT, _ALL[:4]),
    WaveDef("clean_control", "normal", "clean", _ALL),
    # Task 49 (T2 smoke): the three arms that call no model, over a split; search_ai is run separately on a subset
    # under `dev_search`, because a wave must have a row for every (file, arm).
    WaveDef("t2_smoke_dev", "normal", "dev", ("input_only", "staged", "search_det")),
    WaveDef("t2_smoke_clean", "normal", "clean", ("input_only", "staged", "search_det")),
    # Dev snapshot v1: all four v4 arms over a split in a NORMAL wave, so no checkpoint slot is spent.
    WaveDef("snap_v1_dev", "normal", "dev", _ALL[:4]),
    WaveDef("snap_v1_clean", "normal", "clean", _ALL[:4]),
    # Dev snapshot v1r: the same 30 + 8 files, relabelled on the engine's form (partial_reprobe _v1r splits).
    WaveDef("snap_v1r_dev", "normal", "dev", _ALL[:4]),
    WaveDef("snap_v1r_clean", "normal", "clean", _ALL[:4]),
    # reprobe-800: the provisional v2 splits (corpora/v4/provisional_v2/PROVISIONAL_{dev,clean}_v2.json, passed with
    # --split-file). NORMAL waves, not checkpoint: checkpoint slots are limited to three.
    WaveDef("snap_v2_dev", "normal", "dev", _ALL[:4]),
    WaveDef("snap_v2_clean", "normal", "clean", _ALL[:4]),
    # FAST-DEV v1 (the fast loop): the frozen 15 + 5 files of corpora/v4/fast_dev_v1, SEARCH ARMS ONLY, normal waves
    WaveDef("fast_v1_dev", "normal", "dev", ("search_det", "search_ai", "search_seq")),
    WaveDef("fast_v1_clean", "normal", "clean", ("search_det", "search_ai", "search_seq")),
)}


def wave(name: str) -> WaveDef:
    if name not in WAVES:
        raise ValueError(f"unknown wave {name!r}; the waves are {sorted(WAVES)}")
    return WAVES[name]


class WaveSlotSpent(RuntimeError):
    pass


def slot_keys(wave_type: str) -> list[str]:
    """The marker keys a wave TYPE may occupy; empty for a type with no count limit."""
    if wave_type == HELDOUT_WAVE_TYPE:
        return ["slot/held_out/1"]
    if wave_type == "checkpoint":
        return [f"slot/checkpoint/{i}" for i in range(1, MAX_CHECKPOINT_WAVES + 1)]
    return []


def claim_wave_slot(w: WaveDef, wave_id: str, markers: Any) -> str | None:
    """The owner's wave COUNTS, which the ledger says it cannot enforce: the held-out wave runs once, a
    checkpoint wave at most MAX_CHECKPOINT_WAVES times. Atomic create-if-absent on the shared marker
    store. Resuming the SAME wave_id is allowed (a failed shard is rerun); a different wave_id on a spent
    slot is refused. Returns the slot key, or None for a type with no count limit.

    CALLER OBLIGATIONS (not enforceable here): `markers` must be DURABLE and SHARED (the wave bucket); a fresh or
    in-memory store defeats the count, as the tests' dict would. Resuming a wave_id that already COMPLETED is
    allowed by this function, because it cannot know the wave finished: the launcher must not re-launch a
    completed held-out wave_id (the held-out report refuses shards whose slot it cannot match)."""
    keys = slot_keys(w.wave_type)
    if not keys:
        return None
    for key in keys:
        if markers.get(key) == wave_id:
            return key
    for key in keys:
        # `create` False can mean "taken by ANOTHER wave" or "taken a moment ago by a concurrent start of THIS
        # wave_id" (the get above raced it): re-read, or the second start would burn a second checkpoint slot.
        if markers.create(key, wave_id) or markers.get(key) == wave_id:
            return key
    raise WaveSlotSpent(f"every {w.wave_type} slot is taken ({[markers.get(k) for k in keys]}); wave "
                        f"{wave_id!r} cannot run: the held-out files are spent after one run, and "
                        f"checkpoint waves are limited to {MAX_CHECKPOINT_WAVES}")


def wave_slot_problem(w: WaveDef, wave_id: str, markers: Any) -> str | None:
    """`claim_wave_slot` without the claim: why this wave_id could not take (or already does not hold) a slot, or
    None. Writes nothing, so a dry run can ask it."""
    keys = slot_keys(w.wave_type)
    if not keys or any(markers.get(k) == wave_id for k in keys):
        return None
    if any(markers.get(k) is None for k in keys):
        return None
    return (f"every {w.wave_type} slot is taken ({[markers.get(k) for k in keys]}); wave {wave_id!r} cannot run")


_TOKEN = object()


@dataclass(frozen=True)
class WavePlan:
    """A wave that may launch: its slot is claimed and its cap is the ledger's. Built only by
    `plan_wave`; `open_ledger` is the way a shard gets its spending slice, and there is no other."""

    wave: WaveDef
    wave_id: str
    n_shards: int
    cap_usd: float
    slot: str | None
    _key: object = None

    def __post_init__(self) -> None:
        if self._key is not _TOKEN:
            raise TypeError("a WavePlan is built with plan_wave, which claims the slot and binds the cap")

    @property
    def shard_cap_usd(self) -> float:
        return self.cap_usd / self.n_shards

    def open_ledger(self, shard_index: int, path: Any, markers: Any) -> Any:
        """This shard's WaveLedger (WaveLedger.for_shard: cap = WAVE_CAPS_USD[type] / n_shards)."""
        return _model_ledger().WaveLedger.for_shard(
            self.wave.wave_type, wave_id=self.wave_id, n_shards=self.n_shards, shard_index=shard_index,
            path=path, markers=markers)

    def shard_argv(self, shard_index: int, *, ledger_path: str | None = None,
                   marker_bucket: str | None = None, split_file: str | None = None,
                   h_mm: float | None = None) -> list[str]:
        """The wave's identity as flags `run_arm.build_parser()` accepts (a test parses this through that parser).
        A ledgered shard needs every one of --wave-type --wave-id --n-shards --ledger-path --marker-bucket, so a
        plan with a model arm refuses to emit argv without `ledger_path` and `marker_bucket`: a model arm shard
        launched without its cap is not expressible."""
        if self.wave.has_model_arm and not (ledger_path and marker_bucket):
            raise ValueError(f"wave {self.wave.name!r} runs a model arm: its shards need ledger_path and "
                             "marker_bucket, or no cap would stand between them and the model")
        argv = ["--mode", "arms", "--wave", self.wave.name, "--wave-id", self.wave_id,
                "--wave-type", self.wave.wave_type, "--n-shards", str(self.n_shards),
                "--split", self.wave.split,
                "--arms", ",".join(a for a in self.wave.arms if a in DISPATCHABLE_ARMS),
                "--shard", f"{shard_index}/{self.n_shards}"]
        if ledger_path:
            argv += ["--ledger-path", ledger_path]
        if marker_bucket:
            argv += ["--marker-bucket", marker_bucket]
        if split_file:
            argv += ["--split-file", split_file]
        if h_mm is not None:
            argv += ["--h-mm", str(h_mm)]
        return argv

    def provenance(self) -> dict[str, Any]:
        return {"wave": self.wave.name, "wave_id": self.wave_id, "wave_type": self.wave.wave_type,
                "split": self.wave.split, "arms": list(self.wave.arms), "n_shards": self.n_shards,
                "wave_cap_usd": self.cap_usd, "shard_cap_usd": self.shard_cap_usd, "slot": self.slot}

    def provenance_argv(self) -> list[str]:
        """`--plan-provenance <json>`: what a shard copies into its PROVENANCE.json. Every shard of one plan gets the
        same string, and the report refuses shards whose `slot` is not the one this plan claimed."""
        return ["--plan-provenance", json.dumps(self.provenance(), sort_keys=True)]


def plan_wave(w: WaveDef, *, wave_id: str, n_shards: int, markers: Any) -> WavePlan:
    if not wave_id.strip():
        raise ValueError("a wave needs a wave_id")
    if n_shards < 1:
        raise ValueError(f"n_shards must be >= 1, got {n_shards}")
    cap = w.cap_usd  # raises for a type the ledger does not price
    slot = claim_wave_slot(w, wave_id, markers)
    return WavePlan(w, wave_id, n_shards, cap, slot, _TOKEN)


# ------------------------------------------------------------------ the held-out firewall

class HeldOutInTuning(RuntimeError):
    """The held-out split was reached from a tuning path."""


_HELDOUT_COMPACT = "heldout"


def _compact(text: str) -> str:
    """lower-case with every separator removed: 'held-out', 'Held Out', 'held_out.v2' all become 'heldout'."""
    return re.sub(r"[\s_\-.]+", "", text.lower())


def _names_heldout(item: str) -> bool:
    if _HELDOUT_COMPACT in _compact(item):  # the name, any directory above it, or any spelling of either
        return True
    path = Path(item)
    try:
        if path.is_file() and path.suffix == ".json":  # a file named anything: believe what it says it is
            doc = json.loads(path.read_text())
            return isinstance(doc, dict) and _compact(str(doc.get("split", ""))) == _HELDOUT_COMPACT
    except (OSError, ValueError):
        return False
    return False


def refuse_heldout_for_tuning(*what: str) -> None:
    """Call at the top of any code that tunes (prompt selection, threshold fitting, memory building) with
    the split names or file paths it is about to read. The held-out split is reported, never tuned on.

    Matches 'heldout' under any separator or case ('held-out', 'held out', 'Held_Out') in the name or any
    parent directory, AND a JSON file of any name whose own `split` field says held-out. It is still a NAME
    check plus one content check: a caller that never calls it is not stopped (see report.py's note)."""
    for item in what:
        if _names_heldout(str(item)):
            raise HeldOutInTuning(f"{item!r} is the held-out split: it is used once, for the report, and "
                                  "never for tuning, prompt selection, threshold fitting or memory")
