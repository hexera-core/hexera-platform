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
    cpu_hours = (files x cheap_min + full_files x full_min) / 60

The per-file minutes are SIZING ESTIMATES, not measurements (the first wave measures them): refresh
them from PROVENANCE after it runs.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

SEED = 20261122
#: The owner's compute ceiling for the sprint, in c2d VM-hours.
MAX_VM_HOURS = 600.0
DEFAULT_MACHINE = "c2d-standard-8"
MAX_TOTAL_VCPU = 100
#: Per-file wall-clock kill, seconds. One file's checker calls are individually bounded (300 s each);
#: this bounds their sum. A file that hits it is EXCLUDED from the counts (reprobe.Excluded).
DEFAULT_FILE_TIMEOUT_S = 900.0

#: Sizing estimates, minutes of one vCPU per file. Cheap: layer 1 + one gmsh member. Full: layer 1 +
#: three members + layer 3. UNMEASURED.
CHEAP_MIN_PER_FILE = 0.5
FULL_MIN_PER_FILE = 2.5
#: Fraction of the pool assumed to fail the cheap tier (sizes the full-tier share). Pessimistic on
#: purpose: the old benchmark's failing share, not the measured one.
ASSUMED_CHEAP_FAIL_FRACTION = 0.5
PASS_SAMPLE_FRACTION = 0.10
#: Slowest-shard / mean-shard wall. Hash-partitioned shards are balanced in file COUNT, not in time:
#: 900 s timeouts and heavy parts make a long tail, and the slowest shard sets the billed time.
SKEW = 1.5

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
            cheap_fail_fraction: float = ASSUMED_CHEAP_FAIL_FRACTION,
            cheap_min: float = CHEAP_MIN_PER_FILE, full_min: float = FULL_MIN_PER_FILE,
            skew: float = SKEW) -> Projection:
    if files < 0 or shards < 1:
        raise ValueError("files must be >= 0 and shards >= 1")
    vcpus = vcpus_of(machine_type)
    failing = math.ceil(files * cheap_fail_fraction)
    full_files = failing + math.ceil((files - failing) * PASS_SAMPLE_FRACTION)
    cpu_hours = (files * cheap_min + full_files * full_min) / 60.0
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
