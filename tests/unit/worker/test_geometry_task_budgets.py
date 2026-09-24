# Responsibility: Prove every inner deadline on the geometry tasks is shorter than the outer one that kills it.
# Boundaries: it reads the declared budgets, the package's real retry constants and the task's declared limits; it runs no task.
#
# THE DEFECT. `measure_source` carried `soft_time_limit=1200, time_limit=1500`. The measurement runs in
# a CHILD PROCESS and the package retries that child twice on a native fault or an out-of-memory kill,
# applying the configured deadline to EACH attempt: 3 x 900 + 2 x 3 = 2706 s of work under a 1500 s
# kill. A part that faulted twice was killed part way through its second retry, which leaves a child
# orphaned mid tessellation and writes no row saying what happened, because the line that writes the
# row is after the point the kill lands.
#
# WHY THE PACKAGE'S NUMBERS ARE READ FROM THE PACKAGE. budgets.py keeps a copy of ISOLATED_RETRIES and
# RETRY_WAIT_S, because it has to be importable in an image with no geometry agent. A copy nobody
# checks is how two numbers drift apart, and the drift here would be silent in exactly the direction
# that matters: the package raising its retry count would widen the real budget past a kill that still
# looked correct. So one test below reads the installed package and compares.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tests._surveyor_package import require

from meshpipeline.adapters.pipeline_execution import budgets

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_timeout_nesting as gate  # noqa: E402, I001 - the gate lives outside the package


# ---------------------------------------------------------------- the arithmetic

def test_the_packaged_budget_is_every_attempt_and_every_wait():
    # The number the audit measured, reproduced from the parts rather than asserted as a total.
    assert budgets.packaged_measurement_budget_s(900.0) == pytest.approx(2706.0)
    assert budgets.ISOLATED_ATTEMPTS == 3
    assert budgets.ISOLATED_RETRY_WAIT_S == pytest.approx(3.0)


def test_a_per_attempt_deadline_of_zero_is_not_a_budget():
    # Zero means "no clock" in the settings catalogue, and the platform then takes the un-isolated path:
    # one attempt, in process, no timeout. There is nothing to bound, and inventing a budget for it
    # would put a kill in front of work that was deliberately left unbounded.
    assert budgets.packaged_measurement_budget_s(0.0) == 0.0


def test_the_outer_kill_always_outlives_what_it_kills():
    for per_attempt in (1.0, 12.0, 60.0, 900.0, 1800.0, 7200.0):
        inner = budgets.packaged_measurement_budget_s(per_attempt)
        soft, hard = budgets.outer_kill_s(inner, budgets.MEASURE_SOFT_FLOOR_S, budgets.MEASURE_HARD_FLOOR_S)
        assert inner < soft < hard, (
            f"at {per_attempt}s per attempt the package can spend {inner}s and the kills are "
            f"{soft}/{hard}; a job would be killed part way through a retry")


def test_the_floors_mean_nothing_gets_a_shorter_budget_than_it_had():
    # The pre-existing limits. Nesting the budgets must not become a new way to fail a job that used to
    # finish, so every derived number is at least what it replaced.
    assert budgets.MEASURE_SOFT_TIME_LIMIT_S >= 1200
    assert budgets.MEASURE_TIME_LIMIT_S >= 1500
    assert budgets.LOOK_SOFT_TIME_LIMIT_S >= 600
    assert budgets.LOOK_TIME_LIMIT_S >= 900


def test_the_inverse_gives_a_per_attempt_deadline_that_fits():
    # What a caller with a fixed outer bound needs, for instance an HTTP request that will not wait.
    for outer in (30.0, 60.0, 300.0):
        per_attempt = budgets.largest_per_attempt_within(outer)
        assert budgets.packaged_measurement_budget_s(per_attempt) <= outer + 1e-9
    assert budgets.largest_per_attempt_within(60.0) == pytest.approx(18.0)
    # An outer bound smaller than the waits alone leaves no room at all, and says so rather than
    # returning a negative deadline that would read as "no clock".
    assert budgets.largest_per_attempt_within(1.0) == 0.0


# ---------------------------------------------------------------- against the real package

def test_the_copied_retry_constants_match_the_installed_package():
    measure = require("geometry_agent.facts.measure",
                      needs="the real retry count the measurement child is given")
    assert budgets.ISOLATED_ATTEMPTS == measure.ISOLATED_RETRIES + 1, (
        f"budgets.py assumes {budgets.ISOLATED_ATTEMPTS} attempts and the package makes "
        f"{measure.ISOLATED_RETRIES + 1}. Every outer kill is derived from that number, so a drift "
        f"here puts the Celery limits back underneath the work they are killing.")
    assert budgets.ISOLATED_RETRY_WAIT_S == pytest.approx(measure.RETRY_WAIT_S)


def test_the_package_still_applies_the_deadline_to_each_attempt_and_not_to_the_call():
    # The sentence the old numbers were missing, read out of the package's own source rather than
    # assumed: the timeout is inside the retry loop, so it bounds an attempt and not the call.
    measure = require("geometry_agent.facts.measure",
                      needs="where the child's timeout sits relative to its retry loop")
    source = Path(measure.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _measure_child"):]
    loop = body.index("while True:")
    assert body.index("timeout=timeout_s") > loop, (
        "the package's child timeout is no longer inside the retry loop; budgets.py multiplies it by "
        "the attempt count on the assumption that it is")


# ---------------------------------------------------------------- what the tasks actually declare

def _declared_limits() -> dict[str, int]:
    """The limits Celery really put on the tasks, from a process with the installed celery.

    This tier stubs `celery`, and the stub's task decorator throws its options away - so in this
    process the tasks carry no limits at all and a test that read them would pass on any number
    whatsoever. Following the call means asking an interpreter where the decorator is real.
    """
    program = (
        "from meshpipeline.adapters.pipeline_execution import geometry_tasks as g;"
        "print(g.measure_source.soft_time_limit, g.measure_source.time_limit,"
        " g.look_at_source.soft_time_limit, g.look_at_source.time_limit)")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)
    assert done.returncode == 0, f"the task module would not import with a real celery:\n{done.stderr}"
    values = [int(float(v)) for v in done.stdout.split()]
    return dict(zip(("measure_soft", "measure_hard", "look_soft", "look_hard"), values, strict=True))


def test_the_tasks_carry_the_derived_limits_and_not_their_own_numbers():
    declared = _declared_limits()
    assert declared["measure_soft"] == budgets.MEASURE_SOFT_TIME_LIMIT_S
    assert declared["measure_hard"] == budgets.MEASURE_TIME_LIMIT_S
    assert declared["look_soft"] == budgets.LOOK_SOFT_TIME_LIMIT_S
    assert declared["look_hard"] == budgets.LOOK_TIME_LIMIT_S


def test_the_measurement_task_outlives_the_whole_packaged_retry_budget():
    # The regression, stated as the thing that was wrong: 2706 > 1500.
    declared = _declared_limits()
    assert budgets.MEASURE_PACKAGED_BUDGET_S < declared["measure_soft"] < declared["measure_hard"], (
        f"the package can spend {budgets.MEASURE_PACKAGED_BUDGET_S:.0f}s on one file and Celery kills "
        f"the task at {declared['measure_soft']}/{declared['measure_hard']}s")
    assert declared["measure_hard"] > 2706


# ---------------------------------------------------------------- the gate

def test_the_gate_passes_on_this_tree():
    assert gate.check(quiet=True) == 0


def test_the_gate_fails_on_the_pair_as_it_shipped(monkeypatch, capsys):
    # The pre-fix declaration, put back: 2706 s of work under a 1500 s kill. If the gate does not fail
    # on this it is decoration.
    def shipped():
        return [gate.Pair(what="the measurement worker as it shipped",
                          inner_s=2706.0, inner_is="3 attempts x 900s + 2 waits x 3s",
                          outer_s=1200.0, outer_is="soft_time_limit=1200")]

    monkeypatch.setattr(gate, "pairs", shipped)
    assert gate.check(quiet=True) == 1
    assert "killed part way through" in capsys.readouterr().out


def test_the_gate_refuses_rather_than_passes_when_it_finds_no_pairs(monkeypatch, capsys):
    # The blind spot. A gate that compared nothing would report a clean tree, which is how a check comes
    # to have the same hole as the thing it checks.
    monkeypatch.setattr(gate, "pairs", lambda: [])
    assert gate.check(quiet=True) == 2
    assert "no nested deadline pairs" in capsys.readouterr().out


def test_the_inline_upload_path_is_measured_whether_or_not_it_is_fixed():
    # The one pair this change cannot fix: the inline upload path lives in application/, and its request
    # wait is shorter than the package's retry budget for the deadline it passes. It ABANDONS rather than
    # kills, so the gate reports it rather than failing on it - but it reports it on every single run,
    # with both numbers and the fix, so it cannot go quiet.
    #
    # THIS TEST MUST NOT FAIL WHEN SOMEBODY FIXES IT. A test that goes red on an improvement teaches
    # people to stop improving. So it asserts that the pair is MEASURED, and that while it is still
    # broken the report carries the fix; the day it nests, this passes unchanged.
    rows = [p for p in gate.pairs() if "inline upload path" in p.what]
    assert len(rows) == 1, f"the inline upload path is no longer among the measured pairs: {gate.pairs()}"
    inline = rows[0]
    assert inline.inner_s > 0 and inline.outer_s > 0, f"neither number was read: {inline}"
    if not inline.nests:
        assert inline.note, "a violation this check does not fix has to carry the fix in its own report"
        assert not inline.owned_here, (
            "a pair this repository owns and does not nest must FAIL the gate, not be reported by it")
