#!/usr/bin/env python3
"""The held-out run's FREEZE GUARD (lane C14). The held-out wave runs once, on a frozen commit, and nothing else.

    freeze_guard.py --repo <repo> --commit <sha or ref> [--pattern 'crs-v4-freeze-*'] [--require <sha> ...]

REFUSES (exit 1, the reason on stderr) unless:
  1. at least one git tag matches --pattern, and
  2. the run commit IS the commit one of those tags points at (an annotated tag is peeled to its commit), and
  3. every --require commit (e.g. B18's anytime rule, 39f52d73) is an ancestor of the run commit.
Prints `FROZEN <tag> <commit>` on success. Not a warning: a held-out run on an unfrozen or untagged commit measures
code nobody can name afterwards, and the held-out files are spent the moment they are run.
stdlib + git only, so launch.sh can call it before anything else, dry run included.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys

DEFAULT_PATTERN = "crs-v4-freeze-*"


def _git(repo: str, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def check(repo: str, commit: str, pattern: str = DEFAULT_PATTERN, require: tuple[str, ...] = ()) -> tuple[bool, str]:
    """(ok, message). The message is the reason when not ok, `FROZEN <tag> <sha>` when ok."""
    rc, sha = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if rc != 0 or not sha:
        return False, f"REFUSED: cannot resolve the run commit {commit!r}"
    rc, tags = _git(repo, "tag", "--list")
    matching = [t for t in tags.splitlines() if t and fnmatch.fnmatchcase(t, pattern)]
    if not matching:
        return False, (f"REFUSED: no git tag matches {pattern!r}. The held-out run needs a FREEZE TAG on the commit "
                       f"it runs (e.g. `git tag -a {pattern.replace('*', 'YYYYMMDD')} {sha[:12]} -m '...'`), so the "
                       "measurement names code that can be found again.")
    at: dict[str, str] = {}
    for t in matching:
        rc, tsha = _git(repo, "rev-parse", "--verify", f"{t}^{{commit}}")
        if rc == 0:
            at[t] = tsha
    hit = sorted(t for t, s in at.items() if s == sha)
    if not hit:
        shown = ", ".join(f"{t} -> {s[:12]}" for t, s in sorted(at.items()))
        return False, (f"REFUSED: the run commit {sha[:12]} is not the commit of any freeze tag ({shown}). Run the "
                       "held-out wave on a freeze tag's commit, or tag this commit as the freeze.")
    for req in require:
        rc, rsha = _git(repo, "rev-parse", "--verify", f"{req}^{{commit}}")
        if rc != 0:
            return False, f"REFUSED: required commit {req!r} does not exist in this repository"
        rc, _ = _git(repo, "merge-base", "--is-ancestor", rsha, sha)
        if rc != 0:
            return False, f"REFUSED: the run commit {sha[:12]} does not contain required commit {rsha[:12]} ({req})"
    return True, f"FROZEN {hit[0]} {sha}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--pattern", default=DEFAULT_PATTERN)
    p.add_argument("--require", action="append", default=[])
    a = p.parse_args(argv)
    ok, msg = check(a.repo, a.commit, a.pattern, tuple(a.require))
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
