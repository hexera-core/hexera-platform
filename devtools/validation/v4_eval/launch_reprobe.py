#!/usr/bin/env python3
# Responsibility: The launch PREFLIGHT for the re-probe wave: plan the shards, print the projected
#                 VM-hours and wall, and REFUSE an over-budget wave.
# Owns: argument handling and the gate. It prints the plan; it creates NO VM.
# Boundaries: the fleet launch itself (image digest, staging, gcloud create) is launch_fleet.py's and
#             the controller's; this gate is what must pass first.
"""Preflight the re-probe wave.

    launch_reprobe.py --files 53799 --shards 96 --machine-type c2d-standard-8 --max-run-hours 24

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=int, help="pool size (default: read the pool manifest)")
    parser.add_argument("--pool", default=str(_ra.pool_mod.OUT))
    parser.add_argument("--shards", type=int, default=96)
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--max-run-hours", type=float, default=3.0)
    parser.add_argument("--allow-over-budget", action="store_true",
                        help="waive the VM-hour ceiling (not the vCPU or run-duration limits)")
    args = parser.parse_args(argv)
    files = args.files if args.files is not None else len(_ra.pool_mod.read_pool(Path(args.pool)))
    projection = evalspec.project(files, shards=args.shards, machine_type=args.machine_type,
                                  max_run_hours=args.max_run_hours)
    print(projection.describe())
    reasons = evalspec.check_budget(projection, allow_over_budget=args.allow_over_budget)
    if args.allow_over_budget and projection.vm_hours > evalspec.MAX_VM_HOURS:
        print(f"WARN    over the {evalspec.MAX_VM_HOURS:g} VM-hour ceiling: overridden", file=sys.stderr)
    for r in reasons:
        print(f"REFUSE  {r}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
