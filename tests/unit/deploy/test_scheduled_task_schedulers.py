# Responsibility: Prove every periodic task the application schedules has exactly one scheduler in every deployment target.
# Boundaries: it drives the gate over files it writes and over this tree; it starts no beat and calls no cloud.
#
# THE DEFECT. celery_app.conf.beat_schedule declares four periodic tasks - purge-expired-workspaces,
# reap-stalled-jobs, purge-expired-geometry-sources and reconcile-orphan-artifacts - and a periodic
# task only happens because something runs `celery beat` to publish it. docker-compose.yml has a beat
# service; the GCP deployment, which is the only one a customer reaches, had none. So none of the four
# had ever fired in production: expired workspaces were never purged, uploaded geometry outlived
# UPLOAD_RETENTION_DAYS for ever, orphaned artifacts were never reconciled, and a job whose worker
# died stayed RUNNING - because the reaper that marks it failed is itself one of the four.
#
# WHY IT NEEDED A GATE RATHER THAN CARE. Nothing raises. The tasks are registered, the router knows
# their queue, the utility worker drains that queue and waits, and no message is ever published. It is
# the undrained-queue failure with the ends swapped, and just as silent.
#
# AND WHY THE GATE IS NOT ALLOWED TO COUNT `celery ... beat` LINES AND STOP THERE. The fleet's startup
# script holds exactly one such line, and that script runs on every instance in an autoscaled group -
# so the count is one and the running number is whatever the autoscaler feels like. Two beats runs
# every periodic task twice, which is worse than none. The tests below therefore include the case
# where a beat invocation IS present and the gate still has to fail.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_scheduled_tasks as gate  # noqa: E402 - the gate lives outside the package

GATE = REPO / "devtools" / "quality" / "check_scheduled_tasks.py"

#: What the application schedules, as the gate's own reader would report it. Used wherever the point
#: of the test is the DEPLOYMENT side: this tier stubs celery, so the real reader cannot answer here
#: and a test that let it try would be measuring the stub.
_FOUR = {
    "purge-expired-workspaces": "tasks.cleanup.purge_expired_workspaces",
    "reap-stalled-jobs": "tasks.cleanup.reap_stalled_jobs",
    "purge-expired-geometry-sources": "tasks.cleanup.purge_expired_geometry_sources",
    "reconcile-orphan-artifacts": "tasks.cleanup.reconcile_orphan_artifacts",
}

#: The fleet's singleton evidence, as the real target declares it. A fabricated target that reused
#: this is checking the same rule the deployment is checked by.
_FLEET_SINGLETONS = gate.targets()[0].singletons


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)


# ------------------------------------------------------------------ what the application schedules

def test_the_application_schedules_the_four_maintenance_tasks():
    # Asked of a fresh interpreter, because THIS one has a celery stub with no schedule at all (see
    # tests/unit/conftest.py). A test that asked the stub would be told there is nothing periodic and
    # would agree with a deployment that runs no scheduler - the exact shape of defect this file is
    # about.
    program = ("from meshpipeline.adapters.pipeline_execution.celery_app import celery_app;"
               "print(' '.join(sorted(celery_app.conf.beat_schedule)))")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)
    assert done.returncode == 0, f"the beat schedule would not read:\n{done.stderr}"
    entries = done.stdout.split()
    for name in ("purge-expired-workspaces", "reap-stalled-jobs", "reconcile-orphan-artifacts"):
        assert name in entries, f"{name} is not scheduled at all: {entries}"


def test_reading_the_schedule_refuses_to_answer_with_nothing():
    # The blind spot the whole gate rests on: an empty schedule would make every deployment trivially
    # compliant. This tier's celery stub IS that situation, so asking here is the real case.
    with pytest.raises(RuntimeError, match="no beat schedule"):
        gate.scheduled()


def test_the_gate_passes_on_this_tree_with_the_real_celery():
    done = _run_gate()
    assert done.returncode == 0, (
        "the scheduler gate does not pass on this tree, or could not run at all. Exit 2 means it "
        f"could not read the schedule, which is a failure and not a pass:\n{done.stdout}{done.stderr}")
    # The report has to NAME what it found, or a reader cannot tell a real pass from a vacuous one.
    assert "reap-stalled-jobs" in done.stdout, done.stdout
    for target in ("gcp-worker-fleet", "docker-compose"):
        assert target in done.stdout, done.stdout


# --------------------------------------------------------------------- the gate cannot be fooled

def test_the_gate_fails_when_a_target_starts_no_scheduler(tmp_path, monkeypatch, capsys):
    # The GCP deployment exactly as it shipped: two workers, no beat anywhere. The gate has to name
    # the tasks that then never fire, or it is decoration.
    script = tmp_path / "startup.sh"
    script.write_text("docker run img celery -A meshpipeline.runtime.celery_worker worker "
                      "--queues cleanup_tasks --concurrency 2\n", encoding="utf-8")
    monkeypatch.setattr(gate, "targets", lambda: [
        gate.Target(name="pre-fix-fleet", why="the fleet as it shipped", files=(script,))])
    monkeypatch.setattr(gate, "scheduled", lambda: dict(_FOUR))
    assert gate.check(quiet=True) == 1
    out = capsys.readouterr().out
    assert "no `celery ... beat` invocation found" in out, out
    assert "reap-stalled-jobs" in out, out


def test_the_gate_fails_when_a_target_starts_two_schedulers(tmp_path, monkeypatch, capsys):
    # Two beats is not half a fix, it is a different bug: two reapers racing one stalled job, two
    # purges on one workspace. A gate that only looked for absence would call this healthy.
    script = tmp_path / "startup.sh"
    script.write_text("docker run img celery -A app beat --loglevel info\n"
                      "docker run img2 celery -A app beat --loglevel info\n", encoding="utf-8")
    monkeypatch.setattr(gate, "targets", lambda: [
        gate.Target(name="double-fleet", why="two schedulers", files=(script,))])
    monkeypatch.setattr(gate, "scheduled", lambda: dict(_FOUR))
    assert gate.check(quiet=True) == 1
    out = capsys.readouterr().out
    assert "2 `celery ... beat` invocations" in out, out
    assert "twice" in out, out


def test_a_beat_invocation_alone_does_not_pass_the_fleet(tmp_path, monkeypatch, capsys):
    # THE CASE THIS GATE EXISTS FOR, beyond counting. A startup script with an UNGUARDED beat has one
    # beat invocation and runs on every instance of an autoscaled group, so the count is one and the
    # schedulers are however many instances there are. The singleton evidence is what distinguishes
    # them, and it is asserted on the same literals the real fleet declares.
    script = tmp_path / "startup.sh"
    script.write_text("docker run img celery -A app worker --queues cleanup_tasks\n"
                      "docker run img celery -A app beat --loglevel info\n", encoding="utf-8")
    fleet = tmp_path / "create-worker-fleet.sh"
    fleet.write_text('gcloud compute instance-templates create tpl --metadata "worker-image=img"\n',
                     encoding="utf-8")
    monkeypatch.setattr(gate, "targets", lambda: [
        gate.Target(name="unguarded-fleet", why="a beat on every instance in the group",
                    files=(script, fleet), singletons=_FLEET_SINGLETONS)])
    monkeypatch.setattr(gate, "scheduled", lambda: dict(_FOUR))
    assert gate.check(quiet=True) == 1
    out = capsys.readouterr().out
    assert "WORKER_ROLE" in out, out
    assert "runs on every instance the deployment starts" in out, out


def test_the_gate_refuses_rather_than_passes_when_it_cannot_read_the_schedule(monkeypatch, capsys):
    # Exit 2, not 0. An interpreter without celery, or a stubbed one, tells us nothing about the
    # deployment, and "I could not tell" must never read as "there is nothing periodic to run".
    def no_answer():
        raise RuntimeError("there is no beat schedule to read")

    monkeypatch.setattr(gate, "scheduled", no_answer)
    assert gate.check(quiet=True) == 2
    assert "could not be read" in capsys.readouterr().out


def test_the_gate_reads_a_beat_across_shell_and_yaml_line_breaks(tmp_path):
    # Both real invocations wrap: startup.sh over backslashes, docker-compose.yml over a folded
    # scalar. A parser that read one line at a time would find the `celery ... beat` and none of its
    # flags, or miss it entirely.
    shell = tmp_path / "startup.sh"
    shell.write_text("docker run img \\\n  celery -A app beat \\\n    --loglevel info\n",
                     encoding="utf-8")
    found = gate._schedulers_in(shell)
    assert len(found) == 1 and "--loglevel info" in found[0].args, found
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("    command: >\n      celery -A app beat\n      --loglevel info\n",
                       encoding="utf-8")
    assert len(gate._schedulers_in(compose)) == 1


def test_a_beat_named_in_a_comment_is_not_a_scheduler(tmp_path):
    # Both files explain at length why the scheduler is where it is, and the explanations quote the
    # command. A gate that counted prose would report two schedulers in the fixed fleet and fail it.
    script = tmp_path / "startup.sh"
    script.write_text("# it used to run: docker run img celery -A app beat --loglevel info\n"
                      "docker run img celery -A app beat --loglevel info\n", encoding="utf-8")
    assert len(gate._schedulers_in(script)) == 1


# ------------------------------------------------------------------- what each deployment schedules

def test_both_deployment_targets_start_exactly_one_scheduler():
    # The same relation the gate checks, read from the deployment files alone, so a regression in the
    # gate's own plumbing cannot be hidden by it agreeing with itself.
    for target in gate.targets():
        found = [s for p in target.files for s in gate._schedulers_in(p)]
        assert len(found) == 1, (
            f"{target.name} starts {len(found)} celery beat(s). None means no periodic task ever "
            f"runs there; two means every one of them runs twice: {found}")


def test_the_whole_gate_passes_on_this_tree_against_a_known_schedule(monkeypatch, capsys):
    # The gate driven over the REAL deployment files, with only the celery half supplied - so the
    # deployment side of the verdict is checked in this tier too, and not only in the subprocess above
    # that needs an installed celery to say anything at all.
    monkeypatch.setattr(gate, "scheduled", lambda: dict(_FOUR))
    assert gate.check(quiet=True) == 0, capsys.readouterr().out


def test_the_gcp_fleet_keeps_its_scheduler_off_the_autoscaled_group():
    # Said on its own because this is the exact regression. The deployment's only fleet is a managed
    # instance group of one to five instances, so the beat has to be behind a role its members do not
    # have, and exactly one instance must be given that role.
    fleet = gate.targets()[0]
    code = "\n".join(gate._code(p.read_text(encoding="utf-8")) for p in fleet.files)
    for why, literal, expected in fleet.singletons:
        assert code.count(literal) == expected, f"{literal!r} is not there {expected}x, and {why}"


# --------------------------------------------------------------------- no scheduler escapes a target

#: Where a celery beat could be started from. Scanned rather than trusted, so a new deployment
#: arrangement cannot be added in a file the gate does not look at.
_SEARCHED = ("deploy", "docker-compose.yml", "Dockerfile", "Makefile")


def test_every_file_that_starts_a_beat_belongs_to_a_declared_target():
    declared = {p.resolve() for t in gate.targets() for p in t.files}
    stray = []
    for entry in _SEARCHED:
        path = REPO / entry
        candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "celery" in text and gate._BEAT.search(gate._flatten(gate._code(text))):
                if candidate.resolve() not in declared:
                    stray.append(candidate.relative_to(REPO).as_posix())
    assert not stray, (
        f"these files start a celery beat and belong to no deployment target, so whether they are a "
        f"second scheduler is unchecked: {sorted(stray)}. Add them to "
        f"check_scheduled_tasks.targets().")
