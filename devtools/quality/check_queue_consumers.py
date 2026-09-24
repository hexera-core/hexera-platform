#!/usr/bin/env python3
# Responsibility: Prove every queue this application publishes to is drained by some worker in every deployment target.
# Owns: what counts as a deployment target, and how a worker's consumed queues are read out of one.
# Boundaries: a repository gate; it reads the application's own configuration and the deployment files, and calls nothing.

# THE DEFECT THIS EXISTS TO STOP, stated plainly because it is the largest one the audit found.
#
# adapters/pipeline_execution/geometry_tasks.py published the Surveyor's look to `geometry_look`. The
# only worker fleet any real deployment starts is deploy/gcp/worker/startup.sh, and it ran celery with
# `--queues simulation_jobs`. The single consumer of `geometry_look` in the entire repository was a
# docker-compose service, which exists on a developer's laptop and nowhere else. So on the deployed
# platform every look was published, accepted by the broker, logged as "geometry look queued", and
# never run by anything. Every place the Surveyor would have named comes from the look, so the placed
# findings the docs celebrate were zero in production.
#
# NOTHING RAISED. That is the property that matters. A queue with no consumer is not an error
# anywhere: the publish succeeds, the broker holds the message, the caller's log line says the work
# was queued, and the only evidence is a row that never changes. There is no exception to catch, no
# metric that moves, and no test that fails - which is why it survived a live deployment, a corpus
# run and eleven audits. It is silent by nature, so it needs a check by name.
#
# WHAT THE CHECK IS. Two sets, per deployment target:
#
#   PUBLISHED   queues.published_queues() - read off the live Celery configuration, with the task
#               modules imported, so it is the set the application actually publishes to and not a
#               list somebody remembered to update.
#   CONSUMED    every `--queues` list on every celery worker invocation in that target's own files.
#
# PUBLISHED must be a subset of CONSUMED, in EVERY target. A queue in neither set is fine; a queue in
# PUBLISHED and not in CONSUMED is work that disappears.
#
# HOW IT AVOIDS THE BLIND SPOT IT IS CHECKING FOR. This project's signature failure is a check with
# the same hole as the thing it checks, and the obvious way to write this one has that hole: a parser
# that finds no worker invocation, or an import that yields no queue, would compare two empty sets and
# pass. So both halves refuse to be empty. A target that yields no celery worker at all is a FAILURE
# naming that target, and published_queues() raises rather than returning an empty set. "I could not
# tell" never reads as "nothing to drain".
#
#   python devtools/quality/check_queue_consumers.py            # every target, verbose report
#   python devtools/quality/check_queue_consumers.py --quiet     # only the verdict
#
# Exit 0 when every target drains everything, 1 when one does not, 2 when the check could not run.
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Worker:
    """One celery worker invocation found in a deployment target."""

    where: str
    queues: tuple[str, ...]


@dataclass
class Target:
    """One place this application is deployed, and every worker that place starts."""

    name: str
    why: str
    files: tuple[Path, ...]
    workers: list[Worker] = field(default_factory=list)

    @property
    def consumed(self) -> frozenset[str]:
        return frozenset(q for w in self.workers for q in w.queues)


#: A celery worker command, wherever it is written. The shape is the same in a compose `command:`, a
#: `docker run ... celery ... worker` line and a Dockerfile CMD: the word `worker` after
#: `celery -A <app>`, then a `--queues` list somewhere after it. Matched over the whole file text
#: with newlines and shell continuations already flattened, because every one of those files wraps
#: the invocation across lines.
_WORKER = re.compile(r"celery\s+-A\s+\S+\s+worker\b(?P<args>.*?)(?=celery\s+-A|\Z)", re.DOTALL)
_QUEUES = re.compile(r"--queues[=\s]+(?P<list>[A-Za-z0-9_,]+)")

#: Shell line continuations and YAML folded scalars both mean "this command carries on", so both are
#: flattened before the command is matched. Without this the `--queues` of a wrapped invocation reads
#: as a separate line belonging to nothing.
_CONTINUATION = re.compile(r"\\\s*\n\s*")


def _flatten(text: str) -> str:
    text = _CONTINUATION.sub(" ", text)
    return re.sub(r"\n\s+", " ", text)


def _workers_in(path: Path) -> list[Worker]:
    """Every celery worker invocation in one file, with the queues it consumes.

    A worker invocation with NO `--queues` consumes celery's default queue and none of ours, which is
    recorded as an empty tuple rather than skipped: a target whose only worker names no queue is a
    target that drains nothing, and that has to be visible in the report.
    """
    text = _flatten(path.read_text(encoding="utf-8"))
    out: list[Worker] = []
    for match in _WORKER.finditer(text):
        args = match.group("args")
        queues = tuple(q for m in _QUEUES.finditer(args) for q in m.group("list").split(",") if q)
        out.append(Worker(where=_where(path), queues=queues))
    return out


def _where(path: Path) -> str:
    """A path to print. Repository-relative when it is in the repository, absolute when it is not."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def targets() -> list[Target]:
    """The deployment targets, named explicitly.

    A target is a PLACE THIS RUNS, not a file. Adding a deployment arrangement without adding it here
    would leave it unchecked, so `test_queue_consumers.py` asserts that every file in the repository
    that starts a celery worker belongs to one of these targets.
    """
    return [
        Target(
            name="gcp-worker-fleet",
            why="the managed instance group, which is the only worker fleet a real deployment starts",
            files=(ROOT / "deploy" / "gcp" / "worker" / "startup.sh",),
        ),
        Target(
            name="docker-compose",
            why="the local stack, and the arrangement the fleet's split is copied from",
            files=(ROOT / "docker-compose.yml",),
        ),
    ]


def published() -> frozenset[str]:
    """The queues the application publishes to, from the application itself.

    Imported rather than parsed. A regex over `queue=` would have its own opinion about what a queue
    is and would drift from the router the way the deployment files drifted from the tasks.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from meshpipeline.adapters.pipeline_execution.queues import published_queues

    return published_queues()


def check(*, quiet: bool = False) -> int:
    try:
        want = published()
    except Exception as exc:  # noqa: BLE001 - an unrunnable check is not a passing one
        print(f"FAILED: the published queues could not be read: {type(exc).__name__}: {exc}")
        print("  This gate cannot say anything without them, so it refuses rather than passing.")
        return 2

    problems: list[str] = []
    for target in targets():
        missing_files = [p for p in target.files if not p.is_file()]
        if missing_files:
            problems.append(f"{target.name}: file(s) absent: {', '.join(_where(p) for p in missing_files)}")
            continue
        for path in target.files:
            target.workers.extend(_workers_in(path))
        if not target.workers:
            problems.append(
                f"{target.name}: no celery worker invocation found in "
                f"{', '.join(_where(p) for p in target.files)}. A target that "
                f"starts no worker drains nothing, and an empty consumed set must never read as a pass.")
            continue
        undrained = sorted(want - target.consumed)
        if undrained:
            problems.append(
                f"{target.name}: {', '.join(undrained)} "
                f"{'is' if len(undrained) == 1 else 'are'} published but drained by no worker there. "
                f"Work sent to a queue nobody consumes is accepted by the broker, logged as queued, "
                f"and never runs - nothing raises. Add the queue to a worker's --queues in "
                f"{', '.join(_where(p) for p in target.files)}.")
        if not quiet:
            print(f"{target.name}  ({target.why})")
            for worker in target.workers:
                names = ", ".join(worker.queues) or "<none: celery's default queue only>"
                print(f"  worker in {worker.where}: {names}")
            verdict = "every published queue is drained" if not undrained else f"UNDRAINED: {', '.join(undrained)}"
            print(f"  -> {verdict}")

    if not quiet:
        print(f"\npublished by the application: {', '.join(sorted(want))}")
    if problems:
        print("\nFAILED: a published queue is not consumed in every deployment target")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\nOK: {len(want)} published queue(s) drained in all {len(targets())} deployment target(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="print the verdict only")
    args = ap.parse_args(argv)
    return check(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
