#!/usr/bin/env python3
# Responsibility: A SECOND OPINION on the off state. With every geometry flag at its shipped default, is what the
#                 intake model and the mesh planner are handed byte for byte what it is on another commit?
# Boundaries: a developer gate. It reads two checkouts and calls the platform's own functions; it writes nothing
#             into either working copy and needs no database, no network and no model.
#
# WHY A SECOND ONE. `check_vision_off_is_byte_identical.py` is the gate of record and covers six configurations.
# This one exists because that gate is the only witness to the most important claim this repository makes, and a
# ruler with the same blind spot as the thing it measures proves nothing. It is written from the other end: its own
# fixtures, its own digests, and two probes the other gate does not take.
#
#   planner_system   the planner's SYSTEM prompt. The other gate hashes only the user turn, so a geometry flag
#                    that leaked a sentence into the system prompt would pass it.
#   planner_block    the typed block itself. With every flag off there must be no block at all, and "no block" is
#                    not visible in any text digest: an empty dict and a missing argument hash the same downstream.
#
# It also reports which geometry flags it found ON rather than assuming they are off, because the whole claim is
# conditional on that and a policy module that defaulted one to true would otherwise pass silently.
#
#     python devtools/quality/off_state_second_opinion.py --against main
#     python devtools/quality/off_state_second_opinion.py --against main --tree main=/tmp/main
#
# `--tree rev=path` is for a git worktree whose `.git` names a Windows path that the Linux git cannot follow:
# extract on the Windows side and name the directory here. Run it on an interpreter that has the platform's own
# dependencies (fastapi, sqlalchemy); the measurement package's dependencies are not needed, because nothing here
# opens a geometry file.
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: The two fixtures, supplied to BOTH checkouts from this one, so the only variable is the code. A measurement
#: that kept its facts, and the survey row an answered conversation leaves behind carrying the geometry agent's
#: plan: with every flag off, nothing may read either of them.
DOC = Path(__file__).with_name("vision_off_gate_measurement.json")
ROW = Path(__file__).with_name("geometry_step_gate_row.json")
PROBES = ("intake_system", "intake_tools", "planner_user", "planner_system", "planner_block", "request_txt")

EMIT = Path(__file__).with_name("_off_state_second_opinion_emit.py")


def _extract(rev: str, into: Path, supplied: dict[str, Path]) -> Path:
    if rev in supplied:
        return supplied[rev]
    into.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", rev], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)
    return into


def _copy_here(into: Path) -> Path:
    """This working copy, so an UNCOMMITTED change is what gets measured. `.git` and the caches stay behind."""
    def skip(_dir, names):
        return [n for n in names if n in (".git", "__pycache__", ".venv", "node_modules", ".pytest_cache")]
    shutil.copytree(ROOT, into, ignore=skip, dirs_exist_ok=True)
    return into


def _digests(tree: Path, label: str, python: str, agent_src: str) -> dict[str, str]:
    where = tree / "tests" / "unit" / "test_zz_off_state_second_opinion.py"
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(EMIT.read_text(encoding="utf-8"), encoding="utf-8")
    env = {"VOFF_WS": str(tree / ".off_ws"), "VOFF_DOC": str(DOC), "VOFF_ROW": str(ROW),
           "PYTHONPATH": f"{tree / 'src'}{';' if sys.platform == 'win32' else ':'}"
                         f"{agent_src}{';' if sys.platform == 'win32' else ':'}{tree}",
           "PYTHONUTF8": "1"}
    import os
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GEOMETRY_")}
    got = subprocess.run([python, "-m", "pytest", str(where.relative_to(tree)), "-s", "-q",
                          "-p", "no:randomly", "-p", "no:cacheprovider"],
                         cwd=tree, env={**clean, **env}, capture_output=True, text=True)
    out: dict[str, str] = {}
    for line in got.stdout.splitlines():
        if line.startswith("VOFF "):
            parts = line.split()
            out[parts[1]] = parts[2]
    if not out:
        print(got.stdout[-3000:], file=sys.stderr)
        raise SystemExit(f"{label}: the emitter printed no digests")
    print(f"  {label:10s} " + "  ".join(f"{k}={out.get(k, '?')}" for k in PROBES))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--against", default="main")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--agent-src", default="", help="the measurement package's src, for both trees alike")
    ap.add_argument("--tree", action="append", default=[], metavar="REV=PATH")
    args = ap.parse_args()
    supplied = {}
    for item in args.tree:
        rev, _, path = item.partition("=")
        supplied[rev] = Path(path)
    with tempfile.TemporaryDirectory(prefix="off-state-second-") as tmp:
        here = _copy_here(Path(tmp) / "here")
        there = _extract(args.against, Path(tmp) / "there", supplied)
        print("Every geometry flag at its shipped default. Fixtures supplied to both from this checkout.")
        mine = _digests(here, "this tree", args.python, args.agent_src)
        theirs = _digests(there, args.against, args.python, args.agent_src)
    if mine.get("flags_on", "NONE") != "NONE" or theirs.get("flags_on", "NONE") != "NONE":
        print(f"\nREFUSED: a geometry flag is ON ({mine.get('flags_on')} / {theirs.get('flags_on')}); "
              "this gate says nothing about a tree whose defaults are not off")
        return 2
    moved = [k for k in PROBES if mine.get(k) != theirs.get(k)]
    print(f"\n{'SAME' if not moved else 'DIFFERENT'}: {len(PROBES) - len(moved)} of {len(PROBES)} probes identical")
    for k in moved:
        print(f"  {k}: this tree {mine.get(k)}, {args.against} {theirs.get(k)}")
    return 0 if not moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
