#!/usr/bin/env python3
# Responsibility: Prove the vendored geometry-agent wheel is the agent source it says it is, and that that
#                 source is the agent source there is.
# Owns: nothing. It reads vendor/wheels/, reads an agent checkout, and reaches a verdict.
# Boundaries: it builds nothing, installs nothing and writes nothing. The agent checkout is READ.
#
# THE DEFECT THIS EXISTS FOR, and it is the reason the file is this long. The wheel in vendor/wheels/ was
# built from agent 0022654e while agent master was 0428ad41, fourteen commits on, and the image therefore
# shipped an agent that did not write `survey["look_state"]` and did not export `hexera.LOOK_STATES` - both of
# which this platform reads. NOTHING SAID SO. PROVENANCE.json recorded the commit faithfully and nothing
# compared it to anything.
#
# WHY THE CHECKS THAT EXISTED DID NOT CATCH IT, because that is the shape to avoid repeating.
# tests/unit/deploy/test_geometry_agent_distribution.py derives what it expects from THIS repository: the
# subpackages src/meshpipeline imports, the functions and keywords it names, the three data files. Every one
# of those was present in the stale wheel. A wheel is stale in the part the platform has not started calling
# yet, or in behaviour rather than in signature - `deliver.survey_block` gaining a key is neither an absent
# module nor an absent argument - so a gate built on this repository's own imports is a gate with the same
# blind spot as the thing it checks. The only source of truth about the agent is the agent.
#
# THE TWO VERDICTS, and they are different questions:
#
#   IDENTITY  is the wheel the commit it NAMES? Every geometry_agent/ entry in the zip is compared against
#             `git show <recorded commit>:src/<path>`. PROVENANCE.json's sha256 cannot answer this: it is the
#             digest of the wheel taken from the wheel, so it proves only that nobody swapped the file
#             afterwards. A wheel built by hand, built from a dirty tree, or built from one commit and
#             relabelled with another passes the sha256 and fails here.
#   CURRENCY  does that commit carry the agent source the checkout has NOW? `git diff --name-only` between
#             the recorded commit and the checkout's HEAD, restricted to what the wheel is built from. This
#             is the fourteen-commit failure, and it names the files that moved rather than a count.
#
# WHAT IT DELIBERATELY DOES NOT DO: measure the distance in commits. A wheel built two commits back from a
# HEAD that only touched the agent's own tests, docs or eval corpus is NOT stale - the wheel would be byte
# for byte the same. So the subject is the SOURCE THE WHEEL IS BUILT FROM (src/geometry_agent and the
# pyproject that packages it), which is what deploy/vendor_geometry_agent.sh copies, and nothing else.
#
# LINE ENDINGS ARE NORMALISED BEFORE COMPARISON, and that is not a fudge. The agent checkout on Windows has
# CRLF working-tree files and `git show` hands back the LF blob, so an exact byte comparison called 97 of 111
# files different on a wheel that was correct. Measured both ways against both commits: normalised, the
# rebuilt wheel matches its own commit 111/111 and differs from the stale one in 16 files. The unnormalised
# comparison cannot tell those two apart, so it is the one that says nothing.
#
# EXIT CODES, the same three `check_dependency_drift.py` uses and for the same reason: a caller that reads
# "could not check" as "clean" ships exactly the drift this exists to catch.
#
#   0  checked, and the wheel is the agent source it says it is
#   1  a real verdict: it is not
#   2  no verdict was reached, because no agent checkout was reachable. `--allow-unavailable` prints that
#      and returns 0, for the one caller that genuinely has no agent checkout: CI, whose runner clones this
#      repository alone. It prints the line either way, so "not checked" is in the log rather than in nobody's
#      head. `make check` and `make dev-up` do NOT pass it.
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "wheels"
PROVENANCE = VENDOR / "PROVENANCE.json"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CANNOT_CHECK = 2

#: What deploy/vendor_geometry_agent.sh copies into the build, and therefore the only paths a difference in
#: can change the wheel. Kept in step with that script by test_the_gate_reads_what_the_vendor_script_builds_from.
WHEEL_IS_BUILT_FROM = ("src/geometry_agent", "pyproject.toml")

#: Where an agent checkout is looked for when GEOMETRY_AGENT_REPO does not say, as paths relative to a base.
#: `geometry_agent` beside the platform checkout is how this machine is laid out; the `../` forms are for a
#: git worktree, whose own directory has no agent beside it.
CANDIDATES = ("geometry_agent", "../geometry_agent", "../hexera-platform-v2/geometry_agent")


class Unavailable(RuntimeError):
    """The check could not be PERFORMED. Never a verdict about the wheel."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _is_the_agent(path: Path) -> bool:
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file() or not (path / "src" / "geometry_agent").is_dir():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'name = "hexera-geometry-agent"' in text


def _bases() -> list[Path]:
    """Where to look from: this checkout, and - for a worktree - the checkout its git directory lives in."""
    out = [ROOT]
    common = _git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode == 0:
        main = Path(common.stdout.decode().strip()).parent
        if main.is_dir() and main != ROOT:
            out.append(main)
    return out


def agent_checkout() -> Path:
    """The agent checkout this verdict is reached against. Raises `Unavailable` with what to set."""
    named = (os.environ.get("GEOMETRY_AGENT_REPO") or "").strip()
    if named:
        path = Path(named).expanduser()
        if not path.is_dir():
            raise Unavailable(f"GEOMETRY_AGENT_REPO={named!r} is not a directory")
        if not _is_the_agent(path):
            raise Unavailable(f"GEOMETRY_AGENT_REPO={named!r} is not the agent repository: it has no "
                             f"src/geometry_agent and no pyproject declaring hexera-geometry-agent")
        return path.resolve()
    for base in _bases():
        for rel in CANDIDATES:
            path = (base / rel).resolve()
            if _is_the_agent(path):
                return path
    looked = ", ".join(str((b / r).resolve()) for b in _bases() for r in CANDIDATES)
    raise Unavailable(f"no geometry-agent checkout was found. Looked at: {looked}")


def _normalised(blob: bytes) -> bytes:
    # See the header. The comparison is about CONTENT; a working tree's line endings are not content, and an
    # exact comparison across them cannot tell a stale wheel from a Windows one.
    return blob.replace(b"\r\n", b"\n")


def _recorded() -> dict:
    if not PROVENANCE.is_file():
        raise Unavailable(f"{PROVENANCE.relative_to(ROOT)} does not exist, so the wheel records no commit")
    try:
        doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable(f"{PROVENANCE.relative_to(ROOT)} could not be read: {exc}") from exc
    if not isinstance(doc, dict) or not str(doc.get("agent_commit") or "").strip():
        raise Unavailable(f"{PROVENANCE.relative_to(ROOT)} records no agent_commit")
    return doc


def _the_wheel() -> Path:
    found = sorted(VENDOR.glob("hexera_geometry_agent-*.whl"))
    if len(found) != 1:
        raise Unavailable(f"expected exactly one vendored wheel in {VENDOR.relative_to(ROOT)}, found "
                          f"{[p.name for p in found] or 'none'}")
    return found[0]


def identity(wheel: Path, agent: Path, commit: str) -> list[str]:
    """Why the wheel is not the commit it names, one sentence per reason. Empty means it is."""
    if _git(agent, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        return [f"the agent checkout at {agent} does not have commit {commit}, so what the wheel was built "
                f"from cannot be looked up at all. Fetch the agent, or re-vendor from a checkout that has it."]
    problems, compared, differ, absent = [], 0, [], []
    with zipfile.ZipFile(wheel) as z:
        entries = [n for n in z.namelist() if n.startswith("geometry_agent/") and not n.endswith("/")]
        if not entries:
            return [f"{wheel.name} carries no geometry_agent/ entries at all, so there is nothing to compare"]
        for name in entries:
            got = _git(agent, "show", f"{commit}:src/{name}")
            compared += 1
            if got.returncode != 0:
                absent.append(name)
            elif _normalised(got.stdout) != _normalised(z.read(name)):
                differ.append(name)
    if absent:
        problems.append(f"{len(absent)} file(s) in the wheel are not in commit {commit[:10]} at all "
                        f"({', '.join(absent[:5])}{', ...' if len(absent) > 5 else ''})")
    if differ:
        problems.append(f"{len(differ)} of {compared} file(s) in the wheel differ from commit "
                        f"{commit[:10]}: {', '.join(differ[:8])}{', ...' if len(differ) > 8 else ''}")
    return problems


def currency(agent: Path, commit: str) -> list[str]:
    """Why the recorded commit is not the agent source on disk. Empty means it is."""
    head = _git(agent, "rev-parse", "HEAD")
    if head.returncode != 0:
        return [f"{agent} is a directory but not a git checkout, so what the agent source IS cannot be read"]
    now = head.stdout.decode().strip()
    problems = []
    moved = _git(agent, "diff", "--name-only", commit, now, "--", *WHEEL_IS_BUILT_FROM)
    if moved.returncode != 0:
        return [f"git could not diff {commit[:10]}..{now[:10]} in {agent}: "
                f"{moved.stderr.decode(errors='replace').strip().splitlines()[:1]}"]
    files = [f for f in moved.stdout.decode(errors="replace").split() if f]
    if files:
        count = _git(agent, "rev-list", "--count", f"{commit}..{now}")
        behind = count.stdout.decode().strip() if count.returncode == 0 else "?"
        problems.append(
            f"the vendored wheel is built from {commit[:10]} and the agent checkout's HEAD is {now[:10]}, "
            f"{behind} commit(s) on, with {len(files)} file(s) the wheel is built from changed between them: "
            f"{', '.join(files[:8])}{', ...' if len(files) > 8 else ''}")
    # A working tree ahead of its own HEAD under the same paths is the same staleness one step earlier.
    dirty = _git(agent, "status", "--porcelain", "--", *WHEEL_IS_BUILT_FROM)
    if dirty.returncode == 0:
        edited = [ln[3:].strip() for ln in dirty.stdout.decode(errors="replace").splitlines() if ln.strip()]
        if edited:
            problems.append(f"the agent checkout has {len(edited)} uncommitted change(s) under "
                            f"{'/'.join(WHEEL_IS_BUILT_FROM)}, so no wheel matches its source: "
                            f"{', '.join(edited[:8])}{', ...' if len(edited) > 8 else ''}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--allow-unavailable", action="store_true",
                    help="return 0 when no agent checkout is reachable (CI). The line is printed either way.")
    args = ap.parse_args(argv)

    try:
        doc = _recorded()
        wheel = _the_wheel()
        agent = agent_checkout()
    except Unavailable as exc:
        print(f"CANNOT CHECK: {exc}")
        print("  The vendored wheel is the only copy of the agent source in this repository, so whether it "
              "is current can only be answered against the agent itself.")
        print("  Point at one:  GEOMETRY_AGENT_REPO=/path/to/geometry_agent python "
              "devtools/quality/check_vendored_agent.py")
        print("  Or clone it beside this checkout as ./geometry_agent.")
        if args.allow_unavailable:
            print("  --allow-unavailable was passed, so this is reported and not enforced. NOTHING ABOUT "
                  "THE WHEEL'S CURRENCY WAS CHECKED BY THIS RUN.")
            return EXIT_OK
        return EXIT_CANNOT_CHECK

    commit = str(doc["agent_commit"]).strip()
    problems = identity(wheel, agent, commit) + currency(agent, commit)
    if problems:
        print(f"FAIL: the vendored wheel does not match the agent source it is built from.\n"
              f"  wheel  {wheel.name}\n"
              f"  agent  {agent}")
        for p in problems:
            print(f"  - {p}")
        print("\n  Rebuild it:  bash deploy/vendor_geometry_agent.sh "
              f"{agent}\n  then commit vendor/wheels/ and vendor/wheels/PROVENANCE.json together.")
        return EXIT_DRIFT
    print(f"OK: {wheel.name} is agent {commit[:10]}, and that is the agent source in {agent}.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
