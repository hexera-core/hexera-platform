#!/usr/bin/env python3
# Responsibility: Hold the mypy baseline so it can only shrink.
# Boundaries: a cured error loses its licence: the baseline is re-keyed rather than grown, and a new finding fails.
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "mypy_baseline.txt"
TARGET = "src/meshpipeline"
_LINE = re.compile(r"^(?P<path>[^:]+):\d+:(?:\d+:)?\s*error:\s*(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")

# Deps whose PRESENCE changes what mypy reports. If any is missing, this environment sees a
# strict subset of CI's errors and must not be allowed to rewrite the baseline.
_CANONICAL_DEPS = ("pyvista", "vtk", "langgraph", "celery", "minio", "google.cloud.storage")

# Why --update refuses in a thin environment. Held here rather than inline so the only thing
# interpolated at the call site is the dependency list.
_THIN_ENV_GUIDANCE = (
    "  mypy reports FEWER errors when a dependency is absent, so updating here "
    "would drop\n  errors CI can see and turn them into 'new' errors on the next "
    "run. Regenerate with the\n  full dependency set installed:\n\n"
    "      pip install -c requirements/constraints.txt -r requirements/runtime.txt \\\n"
    "        && python devtools/quality/mypy_ratchet.py --update\n"
)


def _missing_canonical_deps() -> list[str]:
    missing = []
    for mod in _CANONICAL_DEPS:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except (ImportError, ValueError):
            missing.append(mod)
    return missing


class MypyDidNotRun(RuntimeError):
    # mypy could not analyse the tree at all. Distinct from "mypy found nothing", which is a
    # verdict; this says there is no verdict.
    pass


def collect() -> set[str]:
    env = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "PATH": __import__("os").environ.get("PATH", "")}
    proc = subprocess.run([sys.executable, "-m", "mypy", TARGET],
                          cwd=ROOT, capture_output=True, text=True, env=env)
    out = set()
    for ln in proc.stdout.splitlines():
        m = _LINE.match(ln.strip())
        if m:
            out.add(f"{m['path']}::{m['code'] or '?'}::{m['msg'].strip()}")
    # mypy exits 0 with no errors, 1 with errors, and 2 when it could not run - a usage error, an
    # unwritable cache directory, or an internal crash. Only 0 and 1 are verdicts. Treating 2 as
    # "no errors found" is what turns a crashed run into "every baseline error was cured", which
    # reads as a ratchet event and hides the real failure: a read-only checkout did exactly that.
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise MypyDidNotRun(
            f"mypy exited {proc.returncode} without producing a verdict: "
            f"{detail[0] if detail else 'no output'}")
    return out


def main() -> int:
    try:
        return _run()
    except MypyDidNotRun as why:
        print(f"CANNOT CHECK: the type ratchet reached no verdict\n\n  - {why}\n\n"
              "  mypy needs a WRITABLE checkout for its cache. Run it from a normal working copy\n"
              "  (`make typecheck`) or through `make validate`, which copies the tree first. The\n"
              "  baseline is unchanged and nothing was concluded about it.")
        return 2


def _run() -> int:
    missing = _missing_canonical_deps()
    if "--update" in sys.argv:
        if missing:
            print("REFUSING to update the baseline: this environment is missing "
                  f"{', '.join(missing)}.\n" + _THIN_ENV_GUIDANCE)
            return 2
        current = collect()
        BASELINE.write_text("\n".join(sorted(current)) + "\n")
        print(f"mypy baseline updated: {len(current)} normalized errors")
        return 0
    current = collect()
    baseline = set(BASELINE.read_text().splitlines()) if BASELINE.exists() else set()

    # A baseline entry whose FILE no longer exists is corruption, not debt: after a rename or
    # delete, the same errors resurface at the new path as "new" while the stale entries sit
    # unmatched. In a thin environment that mismatch hides behind the missing-deps note below
    # and only CI pays - which is exactly how a sandbox.py -> backend.py rename shipped a red
    # CI nobody could see locally. Fail EVERYWHERE, immediately, dependency set or not.
    ghosts = sorted({e.split("::", 1)[0] for e in baseline if not (ROOT / e.split("::", 1)[0]).is_file()})
    if ghosts:
        print("FAIL: the baseline names files that do not exist - it was not re-keyed after a "
              f"rename/delete: {ghosts}\n  Fix the entries (or cure the errors) and shrink the "
              "baseline; do not leave ghost paths in it.")
        return 1

    new = sorted(current - baseline)
    stale = sorted(baseline - current)
    if stale:
        if missing:
            print(f"note: {len(stale)} baseline error(s) are invisible to this environment "
                  f"(missing {', '.join(missing)}) - expected, not a problem to fix here.")
        else:
            # A ratchet that only SUGGESTS shrinking does not ratchet. Four entries sat here for
            # rounds after their errors were cured, and a stale entry is not inert: it is a
            # standing licence for that exact error to return unnoticed at that exact site.
            # Enforced only in a COMPLETE environment - where `missing` is non-empty the same
            # entries are merely unobservable, which is the expected thin-env case handled above.
            print(f"\nFAIL: {len(stale)} baseline error(s) no longer occur. A cured error must "
                  "leave the baseline, or it silently re-permits itself:\n")
            for e in stale:
                print("  " + e.replace("::", "  "))
            print("\n  Remove these entries (or run --update) and commit the smaller baseline.")
            return 1
    if new:
        print(f"\nFAIL: {len(new)} NEW mypy error(s) not in the baseline:\n")
        for e in new:
            print("  " + e.replace("::", "  "))
        return 1
    print(f"OK: {len(current)} mypy error(s), all in the checked-in baseline (0 new).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
