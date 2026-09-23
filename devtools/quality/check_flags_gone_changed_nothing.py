#!/usr/bin/env python3
# Responsibility: Prove by hashing that deleting the five geometry flags changed nothing the models are handed.
# Boundaries: a repository gate; it runs the real intake turn and the real planner read in two checkouts over
#             real stored corpus measurements. It calls no provider, touches no database and writes nothing
#             outside a temporary directory.
#
# WHAT IT REPLACES. `check_vision_off_is_byte_identical.py` hashed the OFF state: with every geometry flag at
# its shipped default, what the intake model and the mesh planner are handed was byte for byte what platform
# main hands them. That gate was what made the feature safe to merge beside a running product, and it was run
# one last time on 2026-09-23, on feat/surveyor-cleanup at a882eed, with the digests recorded in the commit
# that deleted it. It cannot be run again: the flags it set are gone, so there is no off state to hash.
#
# THIS IS THE CLAIM THAT REPLACES IT, and it is the one that matters now that the feature is on: the flags
# carried nothing. For real corpus parts, what the intake model and the mesh planner are handed with no flags
# in the code is byte for byte what the pre-cleanup tree handed them with every flag ON. Same input, same
# output, less code.
#
#   the reference   `surveyor-v1-precleanup` on the platform side, a882eed, with the five deleted gates
#                   set on by the harness
#   this tree       no gates at all; the harness asserts there is none of the five left to set
#
# THE MEASUREMENT PACKAGE'S OWN TWO STAGES are left at their shipped default on both sides. They are still
# settings and were never gates over this platform's feature, so turning one on here would measure the
# stage rather than the deletion.
#
# THE PARTS ARE REAL MEASUREMENTS, not shapes somebody typed out: `tests/fixtures/geometry_survey/*.json` is
# what the measurement package wrote for six corpus parts, one per representation, one of them with an `ok`
# look on it. Five are run as the conversation stands after the upload, with no survey row stored yet; the
# sixth, venturi_orifice_001, is run again with the answered survey row that carries the geometry agent's plan
# (`geometry_step_gate_row.json`), so the write-up in front of the request and the validated handoff are in the
# comparison too. See PARTS below for what the sixth fixture is for and where it came from.
#
# WHAT IS COMPARED, six probes, the union of what the two retired gates took between them:
#   intake_system   the intake model's system prompt
#   intake_tools    the exact tool list intake is offered
#   planner_user    the mesh planner's user message, with the request after its cut
#   planner_system  the mesh planner's SYSTEM prompt
#   planner_block   the typed block itself, or NONE. An empty dict and a missing argument hash the same
#                   downstream, so "a block at all" is its own probe
#   request_txt     the request the driver hands the planner, which is where the agent's write-up lands
#
# IT SAYS WHERE ITS IMPORTS CAME FROM. Both the platform and the measurement package are importable on this
# machine from a tree this gate was not pointed at, and a ruler that measures the wrong tree reports SAME for
# a change it never saw. The emitter asserts both paths before it prints a digest.
#
#   python devtools/quality/check_flags_gone_changed_nothing.py --agent-src /path/to/geometry_agent/src
#
# Run on 2026-09-23 under WSL Ubuntu-24.04 on the platform's own interpreter, with the measurement package
# at `gz_complete`: 36 comparisons, every one identical, and the blindness check moving 5 of the 6 probes.
# The digests are in docs/reference/configuration.md rather than here, because that is where an operator
# reading about the retired gate will be.
#
# ONE AGENT SRC AT A TIME IS THE POINT, AND RUNNING IT TWICE IS HOW YOU GET THE OTHER HALF. This gate holds
# the measurement package still and moves the platform, which is the only way to say the platform's deletion
# carried nothing. Run it a second time with the other package tree on `--agent-src` and compare the two runs'
# digests: if both say SAME and the digests agree run to run, then the pre-cleanup platform with every flag on
# and the pre-cleanup package hands a model exactly what the cleaned platform with no flags and the cleaned
# package hands it, which is the claim about both repos rather than one. The verifier did that on 2026-09-23
# at `gz_complete` and at `gy_clean`: 72 comparisons, every digest identical both ways.
#
# Where `git archive` cannot run from the interpreter that has the dependencies - a Windows worktree whose
# `.git` names a Windows path the Linux git cannot follow - extract the commit by hand and name it:
#
#   git archive a882eed | tar -x -C /tmp/a882eed
#   python devtools/quality/check_flags_gone_changed_nothing.py --tree a882eed=/tmp/a882eed --agent-src ...
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "geometry_survey"
STEP_ROW = Path(__file__).with_name("geometry_step_gate_row.json")
EMIT = Path(__file__).with_name("_flags_gone_emit.py")

#: The platform side of `surveyor-v1-precleanup`: every flag still there, and the feature complete behind them.
REFERENCE = "a882eed"

#: The five gates the cleanup deleted, all on in the reference tree. NOT the measurement package's own two
#: stages: those are still settings, still off by default, and they are left at that default on both sides,
#: because a comparison that turned one on would be measuring the stage and not the deletion.
PRE_CLEANUP_FLAGS = ("GEOMETRY_MEASUREMENT_ENABLED", "GEOMETRY_REPORT_READERS_ENABLED",
                     "GEOMETRY_VISION_ENABLED", "GEOMETRY_SURVEY_ENABLED", "GEOMETRY_AGENT_STEP_ENABLED")

PROBES = ("intake_system", "intake_tools", "planner_user", "planner_system", "planner_block", "request_txt")

#: The parts, and the survey row each is read with. None is the conversation as it stands after the upload.
#:
#: THE SIXTH PART CLOSES TWO HOLES the first five left, found by the verifier on 2026-09-23 and both of them
#: the kind where a ruler is blind to what it measures. The five read `unknown`, `wall_shell`, `wall_shell`,
#: `annular_fluid` and `fluid_domain`, so `external` was never hashed at all; and every one of the five carries
#: `look: not_attempted`, so this gate had never once handed a model anything the look said, while claiming
#: nothing a model is handed had moved. `ahmed_variant_001_external_looked.json` is `external` and carries an
#: `ok` look with its impression, so both are in the comparison now.
#:
#: It is a real measurement like the others, and it was made differently, which is worth saying: the five came
#: out of the measurement package's own stored rows, and this one is `chain.job.survey_document` over the same
#: corpus STEP file `ahmed_variant_001.json` names, with the recorded look of the shipped prompt composed in and
#: the platform's own envelope keys copied from `ahmed_variant_001.json`. Its only edit is that the measurement
#: clock is scrubbed, so it hashes the same on any machine.
PARTS: tuple[tuple[str, str | None], ...] = (
    ("ahmed_variant_001", None),
    ("ahmed_variant_001_external_looked", None),
    ("bend_elbow_001", None),
    ("block_boss_sharp", None),
    ("transition_007_fluid", None),
    ("venturi_orifice_001", None),
    ("venturi_orifice_001 + the answered row with a plan on it", "step_row"))


def _extract(rev: str, into: Path, supplied: dict[str, Path]) -> Path:
    if rev in supplied:
        return supplied[rev]
    into.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", rev], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)
    return into


def _working_copy(into: Path) -> Path:
    """This checkout as it stands, uncommitted edits included, which is the whole point."""
    def skip(_dir, names):
        return [n for n in names if n in {".git", "BETA", "__pycache__", ".venv", "workspaces", ".pytest_cache"}]

    shutil.copytree(ROOT, into, ignore=skip, dirs_exist_ok=True)
    return into


def _digests(tree: Path, label: str, document: Path, row: Path | None, flags: str, python: str,
             agent_src: str, expect: str = "gone") -> dict[str, str]:
    where = tree / "tests" / "unit" / "test_zz_flags_gone.py"
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(EMIT.read_text(encoding="utf-8"), encoding="utf-8")
    sep = os.pathsep
    env = {"FG_WS": str(tree / ".flags-gone-ws"), "FG_DOC": str(document), "FG_ROW": str(row) if row else "",
           "FG_FLAGS": flags, "FG_TREE": str(tree), "FG_AGENT": agent_src, "PYTHONUTF8": "1",
           "FG_EXPECT": expect,
           "PYTHONPATH": sep.join([str(tree / "src"), agent_src, str(tree)])}
    # A GEOMETRY_* EXPORT IN THE SHELL WOULD DECIDE A SIDE. The package's own two variables win over what the
    # platform arms (that is their contract), so a stray export could put the two trees in different states.
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GEOMETRY_")}
    try:
        run = subprocess.run([python, "-m", "pytest", str(where.relative_to(tree)), "-s", "-q",
                              "-p", "no:randomly", "-p", "no:cacheprovider"],
                             cwd=tree, env={**clean, **env}, capture_output=True, text=True)
    finally:
        where.unlink(missing_ok=True)
    out: dict[str, str] = {}
    for line in run.stdout.splitlines():
        if line.startswith("FG "):
            parts = line.split()
            if parts[1] in ("where", "gates", "fell_back"):
                print(f"     {label}: {' '.join(parts[1:])}")
            else:
                out[parts[1]] = parts[2]
    if set(out) != set(PROBES):
        print(run.stdout[-4000:], file=sys.stderr)
        raise SystemExit(f"{label}: the harness did not print every digest")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default=REFERENCE)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--agent-src", required=True, help="the measurement package's src, for both trees alike")
    ap.add_argument("--tree", action="append", default=[], metavar="REV=PATH",
                    help="use an already extracted checkout of REV instead of running git archive")
    args = ap.parse_args()

    supplied: dict[str, Path] = {}
    for item in args.tree:
        rev, _, path = item.partition("=")
        supplied[rev] = Path(path).resolve()
    agent_src = str(Path(args.agent_src).resolve())
    on = ",".join(PRE_CLEANUP_FLAGS)

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="flags-gone-") as tmp:
        tmp_path = Path(tmp)
        here = _working_copy(tmp_path / "working")
        there = _extract(args.reference, tmp_path / "reference", supplied)
        print(f"This tree, no gates, against {args.reference} with all {len(PRE_CLEANUP_FLAGS)} gates ON.")
        print("The fixtures are supplied to both trees from this one, so the only variable is the code.\n")
        for part, row_kind in PARTS:
            name = part.split(" ")[0]
            document = FIXTURES / f"{name}.json"
            if not document.is_file():
                raise SystemExit(f"no stored measurement for {name} at {document}")
            row = STEP_ROW if row_kind == "step_row" else None
            mine = _digests(here, f"{part} (this tree)", document, row, "", args.python, agent_src)
            theirs = _digests(there, f"{part} ({args.reference})", document, row, on, args.python, agent_src)
            moved = [p for p in PROBES if mine[p] != theirs[p]]
            print(f"{'SAME' if not moved else 'DIFF'} {part}: "
                  + "  ".join(f"{p}={mine[p]}" for p in PROBES))
            for p in moved:
                print(f"     moved: {p}: this tree {mine[p]}, {args.reference} {theirs[p]}")
                failures.append(f"{part}: {p}")
        # THE HARNESS HAS TO SEE THE FLAGS. The same reference tree, once with every gate ON and once with
        # every gate at its shipped default: those two must differ, or nothing above is worth reading. A
        # ruler that reports SAME for a change it cannot see is how this project has been misled before.
        blind_doc = FIXTURES / "venturi_orifice_001.json"
        with_on = _digests(there, f"blindness check, gates ON ({args.reference})", blind_doc, STEP_ROW, on,
                           args.python, agent_src)
        with_off = _digests(there, f"blindness check, gates OFF ({args.reference})", blind_doc, STEP_ROW, "",
                            args.python, agent_src, expect="off")
        saw = [p for p in PROBES if with_on[p] != with_off[p]]
        print(f"\n{'DIFF' if saw else 'SAME'} blindness check on {args.reference}: "
              f"the gates move {len(saw)} of {len(PROBES)} probes ({', '.join(saw) or 'none'})")
        if not saw:
            failures.append("blindness check: this harness cannot see the flags it claims to have removed, "
                            "so its other verdicts prove nothing")
    if failures:
        print("\ndeleting the flags changed what a model is handed: " + "; ".join(failures))
        return 1
    print(f"\nevery probe on every part is byte for byte what {args.reference} produced with every flag on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
