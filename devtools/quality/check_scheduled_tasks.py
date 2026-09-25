#!/usr/bin/env python3
# Responsibility: Prove every periodic task this application schedules has exactly one scheduler in every deployment target.
# Owns: what counts as a scheduler, and the rule that one is required and two are a failure.
# Boundaries: a repository gate; it reads the application's own configuration and the deployment files, and calls nothing.

# THE DEFECT THIS EXISTS TO STOP, stated plainly.
#
# celery_app.conf.beat_schedule declares four periodic tasks: purge-expired-workspaces,
# reap-stalled-jobs, purge-expired-geometry-sources and reconcile-orphan-artifacts. A periodic task
# only happens because something runs `celery beat` to publish it on its interval. docker-compose.yml
# has a beat service; the GCP deployment - the only deployment a customer ever reaches - had NOTHING.
# So on the deployed platform not one of the four had ever fired: expired workspaces were never
# purged, uploaded geometry outlived UPLOAD_RETENTION_DAYS for ever, orphaned artifacts were never
# reconciled, and a job whose worker died stayed RUNNING instead of being failed after
# STALLED_JOB_TIMEOUT_HOURS - so the mechanism that turns a dead worker into a visible failure was
# itself missing.
#
# NOTHING RAISED. That is the property that matters, and it is the undrained-queue failure with the
# ends swapped: there a publisher had no consumer, here the consumers had no publisher. The tasks are
# registered, the router knows their queue, a worker drains that queue and sits there ready, and no
# message ever arrives. There is no exception to catch, no metric that moves and no test that fails.
# A silence needs a check by name.
#
# WHAT THE CHECK IS. Per deployment target:
#
#   SCHEDULED   the beat entries, read off the live Celery configuration with the task modules
#               imported, so it is what the application really schedules rather than a list somebody
#               remembered to update. Every entry's task must also be REGISTERED: a beat entry naming
#               a task nobody defined publishes a message the worker rejects as unregistered, which
#               is the same silence one layer down.
#   SCHEDULERS  every `celery -A <app> beat` invocation in that target's own files.
#
# EXACTLY ONE SCHEDULER PER TARGET. Zero means every periodic task never runs. TWO MEANS EVERY
# PERIODIC TASK RUNS TWICE, which is worse than the bug: two reapers racing the same stalled job, two
# purges deleting the same workspace, and the reconciliation sweep burning two of its five
# RECONCILE_MAX_RETRIES attempts per interval. So this gate fails in both directions, not just on
# absence.
#
# HOW IT AVOIDS THE BLIND SPOT IT IS CHECKING FOR. This project's signature failure is a check with
# the same hole as the thing it checks, and the obvious way to write this one has two of them.
#
#   1. An empty SCHEDULED set would make every target trivially compliant, so reading the schedule
#      refuses rather than returning nothing - a stubbed Celery app (the hermetic test tier's) must
#      never read as "there is nothing to schedule".
#   2. COUNTING `celery ... beat` INVOCATIONS IS NOT COUNTING SCHEDULERS. The gcp fleet's startup
#      script contains exactly one, and that script runs on EVERY instance the deployment starts -
#      so the count is one and the running number is however many instances the autoscaler feels like
#      having. A target whose scheduler is decided by configuration rather than by being written once
#      therefore declares the literals that make it a singleton, and they are counted in the target's
#      own files: the role that starts beat, the role the group's members get, and the guard around
#      the container. See `Target.singletons`.
#
#   python devtools/quality/check_scheduled_tasks.py            # every target, verbose report
#   python devtools/quality/check_scheduled_tasks.py --quiet     # only the verdict
#
# Exit 0 when every target schedules everything exactly once, 1 when one does not, 2 when the check
# could not run.
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Scheduler:
    """One `celery beat` invocation found in a deployment target."""

    where: str
    #: The rest of the invocation, kept so the report can show which scheduler was found rather than
    #: only that a count was met.
    args: str


@dataclass
class Target:
    """One place this application is deployed, and every scheduler that place starts."""

    name: str
    why: str
    files: tuple[Path, ...]
    #: (why it is load-bearing, literal, how many times it must appear in this target's CODE).
    #:
    #: This is how "exactly one scheduler" is checked for a target whose scheduler is decided by
    #: configuration. The gcp fleet's startup script holds one beat invocation and runs on every
    #: instance in an autoscaled group, so counting invocations there proves nothing at all; what
    #: makes it a singleton is that the container is behind a role guard, that the group's template
    #: hands out the other role, and that exactly one instance is created with the scheduler role.
    #:
    #: COUNTED IN CODE ONLY. These files explain themselves at length and both roles are named in
    #: prose, so a raw count over the whole text would be a count of the comments. Full-line comments
    #: are stripped before counting, which is why each literal is written the way the code writes it.
    singletons: tuple[tuple[str, str, int], ...] = ()
    schedulers: list[Scheduler] = field(default_factory=list)


#: A celery beat invocation, wherever it is written - a compose `command:`, a `docker run ... celery
#: ... beat` line, a Dockerfile CMD. `beat` as the word after `celery -A <app>`, which is what
#: distinguishes it from the `worker` invocations in the same files.
_BEAT = re.compile(r"celery\s+-A\s+\S+\s+beat\b(?P<args>.*?)(?=celery\s+-A|\Z)", re.DOTALL)

#: Shell line continuations and YAML folded scalars both mean "this command carries on", so both are
#: flattened before the command is matched. Without this the flags of a wrapped invocation read as
#: separate lines belonging to nothing.
_CONTINUATION = re.compile(r"\\\s*\n\s*")


def _flatten(text: str) -> str:
    text = _CONTINUATION.sub(" ", text)
    return re.sub(r"\n\s+", " ", text)


def _code(text: str) -> str:
    """The file with its full-line comments removed.

    Shell and YAML share `#`, and both files this gate reads carry long comments that quote the very
    literals being counted. A count over the prose is not a count of the configuration.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _schedulers_in(path: Path) -> list[Scheduler]:
    text = _flatten(_code(path.read_text(encoding="utf-8")))
    return [Scheduler(where=_where(path), args=" ".join(m.group("args").split())[:120])
            for m in _BEAT.finditer(text)]


def _where(path: Path) -> str:
    """A path to print. Repository-relative when it is in the repository, absolute when it is not."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def targets() -> list[Target]:
    """The deployment targets, named explicitly.

    The same two places check_queue_consumers.py names, because they are the same two places: a
    deployment that drains the queues and never schedules the work is as silent as one that schedules
    it and drains nothing. `test_scheduled_task_schedulers.py` asserts that every file in the
    repository which starts a beat belongs to one of these.
    """
    return [
        Target(
            name="gcp-worker-fleet",
            why="the managed instance group plus the one scheduler instance beside it, which is the "
                "only place a real deployment runs anything periodic",
            files=(ROOT / "deploy" / "gcp" / "worker" / "startup.sh",
                   ROOT / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh"),
            singletons=(
                ("the beat container is started only under the scheduler role, so a group member "
                 "never starts one",
                 'if [ "${WORKER_ROLE}" = "scheduler" ]', 1),
                ("the group's template hands every member the pipeline role",
                 "worker-role=pipeline", 1),
                ("exactly one instance is created with the scheduler role",
                 "worker-role=scheduler", 1),
            ),
        ),
        Target(
            name="docker-compose",
            why="the local stack, and the arrangement the fleet's scheduler is copied from",
            files=(ROOT / "docker-compose.yml",),
            # One file, one service, one process: here the count of invocations IS the count of
            # schedulers, so there is nothing further to state.
        ),
    ]


def scheduled() -> dict[str, str]:
    """Every periodic task this application schedules: beat entry name -> task name.

    Imported rather than parsed, for the reason check_queue_consumers.py imports its queues: a regex
    over this file would have its own opinion about what a schedule is and would drift from it.

    AN EMPTY SCHEDULE IS A REFUSAL, not an answer. The hermetic unit tier replaces `celery` with a
    stub whose conf carries nothing, and a gate built on that answer would report that every
    deployment schedules everything it needs to - which is precisely the silence this gate exists to
    break.

    AND EVERY ENTRY'S TASK MUST BE REGISTERED. beat publishes by NAME, so an entry naming a task
    nobody defined produces a message the worker rejects as unregistered and drops. That is this same
    failure one layer down, and it is checked here because here is where both halves are in hand.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app

    schedule = getattr(celery_app.conf, "beat_schedule", None)
    loader = getattr(celery_app, "loader", None)
    if not schedule or loader is None:
        raise RuntimeError(
            "there is no beat schedule to read - beat_schedule or the loader is absent, which is what "
            "the hermetic test tier's celery stub looks like. Ask this question in a process that "
            "imports the installed celery.")
    entries = {name: str(entry.get("task", "")) for name, entry in schedule.items()
               if isinstance(entry, dict)}
    missing = sorted(name for name, task in entries.items() if not task)
    if missing:
        raise RuntimeError(f"beat entries with no task name: {', '.join(missing)}")
    loader.import_default_modules()
    unregistered = sorted(f"{name} -> {task}" for name, task in entries.items()
                          if task not in celery_app.tasks)
    if unregistered:
        raise RuntimeError(
            "these beat entries name a task that is not registered, so beat would publish a message "
            f"the worker rejects as unregistered and drops: {', '.join(unregistered)}")
    return entries


def check(*, quiet: bool = False) -> int:
    try:
        want = scheduled()
    except Exception as exc:  # noqa: BLE001 - an unrunnable check is not a passing one
        print(f"FAILED: the periodic schedule could not be read: {type(exc).__name__}: {exc}")
        print("  This gate cannot say anything without it, so it refuses rather than passing.")
        return 2

    problems: list[str] = []
    for target in targets():
        missing_files = [p for p in target.files if not p.is_file()]
        if missing_files:
            problems.append(f"{target.name}: file(s) absent: {', '.join(_where(p) for p in missing_files)}")
            continue
        code = "\n".join(_code(p.read_text(encoding="utf-8")) for p in target.files)
        for path in target.files:
            target.schedulers.extend(_schedulers_in(path))
        where = ", ".join(_where(p) for p in target.files)
        if not target.schedulers:
            problems.append(
                f"{target.name}: no `celery ... beat` invocation found in {where}, so none of "
                f"{len(want)} periodic task(s) - {', '.join(sorted(want))} - would ever run there. "
                f"Nothing raises: the tasks stay registered, their queue stays drained, and no "
                f"message is ever published.")
        elif len(target.schedulers) > 1:
            problems.append(
                f"{target.name}: {len(target.schedulers)} `celery ... beat` invocations in {where}. "
                f"Two schedulers run every periodic task twice - two reapers racing one stalled job, "
                f"two purges on one workspace - which is worse than none. There is exactly one.")
        for why, literal, expected in target.singletons:
            found = code.count(literal)
            if found != expected:
                problems.append(
                    f"{target.name}: `{literal}` appears {found} time(s) in the code of {where} and "
                    f"must appear {expected}, because {why}. Without it the single beat invocation "
                    f"there runs on every instance the deployment starts.")
        if not quiet:
            print(f"{target.name}  ({target.why})")
            for scheduler in target.schedulers:
                print(f"  beat in {scheduler.where}: {scheduler.args or '<no arguments>'}")
            for why, literal, expected in target.singletons:
                print(f"  singleton: `{literal}` x{code.count(literal)} (needs {expected}) - {why}")
            print(f"  -> {'one scheduler' if len(target.schedulers) == 1 else f'{len(target.schedulers)} schedulers'}")

    if not quiet:
        print("\nscheduled by the application:")
        for name, task in sorted(want.items()):
            print(f"  {name} -> {task}")
    if problems:
        print("\nFAILED: a periodic task has no scheduler, or more than one, in a deployment target")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\nOK: {len(want)} periodic task(s) scheduled exactly once in all {len(targets())} "
          f"deployment target(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="print the verdict only")
    args = ap.parse_args(argv)
    return check(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
