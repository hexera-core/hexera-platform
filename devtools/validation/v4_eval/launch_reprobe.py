#!/usr/bin/env python3
# Responsibility: The launch PREFLIGHT for the re-probe wave: plan the shards, print the projected
#                 VM-hours and wall, and REFUSE an over-budget wave.
# Owns: argument handling and the gate. It prints the plan; it creates NO VM.
# Boundaries: the fleet launch itself (image digest, staging, gcloud create) is launch_fleet.py's and
#             the controller's; this gate is what must pass first.
"""Preflight the re-probe wave.

    launch_reprobe.py --files 53799 --shards 96 --machine-type c2d-standard-8 --max-run-hours 24

`launch(projection, create_vms)` is a gated entry; the fleet launcher itself (launch_fleet.py --profile v4,
the v3 launcher parametrised) gates every VM creation through its own `create_wave` on the same
`evalspec.enforce_budget`.

Exit 0 and a printed projection when the wave is within ~600 c2d VM-hours, inside the 100-vCPU ceiling
and short enough for the VMs' --max-run-duration; exit 1 and the reasons otherwise.
`--allow-over-budget` waives the VM-hour ceiling only, and says so.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_arm as _ra  # noqa: E402

evalspec = _ra.evalspec


def launch(projection, create_vms, *, allow_over_budget: bool = False):
    """THE ONLY WAY a v4 wave creates VMs: `evalspec.enforce_budget` runs first and raises
    BudgetRefused, and `create_vms` (the callable that issues the gcloud creates) is not reached.
    The v4 fleet launcher must call this, not gcloud directly."""
    evalspec.enforce_budget(projection, allow_over_budget=allow_over_budget)
    return create_vms()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=int, help="pool size (default: read the pool manifest)")
    parser.add_argument("--pool", default=str(_ra.pool_mod.OUT))
    parser.add_argument("--shards", type=int, default=96)
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--max-run-hours", type=float, default=3.0)
    parser.add_argument("--create", action="store_true",
                        help="refuses after the gate: the v4 launcher is launch_fleet.py --profile v4")
    parser.add_argument("--allow-over-budget", action="store_true",
                        help="waive the VM-hour ceiling (not the vCPU or run-duration limits)")
    args = parser.parse_args(argv)
    if args.files is not None:
        files = args.files
    else:  # only the sources the benchmark may measure (Fusion 360 is on licence hold)
        files = len(_ra.reprobe_mod.split_allowed(_ra.pool_mod.read_pool(Path(args.pool)))[0])
    projection = evalspec.project(files, shards=args.shards, machine_type=args.machine_type,
                                  max_run_hours=args.max_run_hours)
    print(projection.describe())
    reasons = evalspec.check_budget(projection, allow_over_budget=args.allow_over_budget)
    if args.allow_over_budget and projection.vm_hours > evalspec.MAX_VM_HOURS:
        print(f"WARN    over the {evalspec.MAX_VM_HOURS:g} VM-hour ceiling: overridden", file=sys.stderr)
    for r in reasons:
        print(f"REFUSE  {r}", file=sys.stderr)
    if reasons:
        return 1
    if args.create:
        def no_launcher():
            raise SystemExit("REFUSE  this script only preflights: launch the wave with "
                             "devtools/validation/v3_eval/launch_fleet.py --profile v4, whose "
                             "create_wave runs the same enforce_budget gate before any VM")

        try:
            launch(projection, no_launcher, allow_over_budget=args.allow_over_budget)
        except evalspec.BudgetRefused as exc:  # pragma: no cover - check_budget already returned
            print(f"REFUSE  {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
