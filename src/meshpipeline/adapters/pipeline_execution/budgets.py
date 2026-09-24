# Responsibility: State every deadline on the geometry paths, and the one law that orders them.
# Owns: the Celery time limits for the measurement and the look, and the arithmetic they are derived from.
# Boundaries: numbers and the rule that relates them; it starts nothing, waits on nothing and imports no task.

# THE LAW, in one sentence: AN INNER DEADLINE MUST ALWAYS BE SHORTER THAN THE OUTER ONE THAT KILLS IT.
#
# WHAT WENT WRONG WITHOUT IT. `measure_source` carried `soft_time_limit=1200, time_limit=1500`, and
# those two numbers were written as if 1500 s were the longest the work inside could take. It is not.
# The measurement runs in a CHILD PROCESS and the package retries that child on a native fault or an
# out-of-memory kill: `geometry_agent.facts.measure` has ISOLATED_RETRIES = 2 and RETRY_WAIT_S = 3.0,
# and `_measure_child` applies the caller's `timeout_s` to EACH attempt. So with the default
# GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS of 900 the real budget inside the task is
#
#     3 attempts x 900 s  +  2 waits x 3 s  =  2706 s
#
# against a hard kill at 1500 s. A part that faulted twice was therefore killed by Celery PART WAY
# THROUGH its second retry, which leaves whatever a half-retry leaves: a child process orphaned mid
# tessellation, a cache directory half written, and no row saying what happened, because the code that
# writes the row is after the point the kill lands. The per-attempt deadline looked like a deadline and
# was a per-attempt deadline, and nothing in the file said so.
#
# WHY THE OUTER NUMBERS ARE DERIVED AND NOT WRITTEN DOWN. Two numbers that have to satisfy an
# inequality will not keep satisfying it if a human maintains both. GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS
# is an operator's setting: somebody who sets it to 1800 means one attempt may take 1800 s, and it is
# not this module's place to quietly halve it. So the INNER budget is authoritative and the OUTER kill
# is computed from it. That direction cannot lie. The other direction - capping the per-attempt deadline
# so the packaged budget fits a shorter outer kill - needs the attempt COUNT to be a parameter of
# `measure_isolated`, which it is not today (see the report's NEEDS FROM OTHERS).
#
# WHY THE FLOORS EXIST. Each path keeps the outer limit it already had as a FLOOR, so this change can
# only ever lengthen a budget. Shortening one would be a new way to fail a job that used to finish, and
# nesting the budgets is not worth introducing that.
from __future__ import annotations

import math

import meshpipeline.settings.policy as polcfg

#: The number of times the package runs the measurement child before it gives up, and the pause between
#: them. These mirror `geometry_agent.facts.measure.ISOLATED_RETRIES + 1` and `RETRY_WAIT_S`, and they
#: are stated here rather than imported because this module must be importable with the geometry agent
#: absent - the task module it serves is imported by every worker, including one in an image that
#: carries no agent. `tests/unit/worker/test_geometry_task_budgets.py` reads the REAL constants out
#: of the installed package and fails if these drift from them, so the copy is checked and not trusted.
ISOLATED_ATTEMPTS = 3
ISOLATED_RETRY_WAIT_S = 3.0

#: The platform's own work inside one task, around the part the package does: reading the row, pulling
#: the uploaded bytes out of object storage into a temporary file, composing the document and writing it
#: back. It is not measured at the tail, because the tail is a large upload on a slow link rather than
#: anything this code does; two minutes is the allowance, and a task that spends longer than that on
#: storage has a storage problem the soft limit should report.
PLATFORM_OVERHEAD_S = 120.0

#: Between the soft kill and the hard one. The soft limit raises inside the task so the row can record
#: what happened; the hard limit kills the process and records nothing. The gap is what the recording
#: gets to use, and it only has to cover a database write.
SOFT_TO_HARD_GRACE_S = 60.0

#: The limits these tasks carried before the budgets were nested. Kept as floors, BOTH of them, so
#: nothing here can shorten a budget that already worked. Without the hard floor the look's hard kill
#: would have moved from 900 s to 660 s, which is a change to when a job dies that has nothing to do
#: with nesting the budgets and no measurement behind it.
MEASURE_SOFT_FLOOR_S = 1200.0
MEASURE_HARD_FLOOR_S = 1500.0
LOOK_SOFT_FLOOR_S = 600.0
LOOK_HARD_FLOOR_S = 900.0


def packaged_measurement_budget_s(per_attempt_s: float) -> float:
    """The longest the measurement package can spend on one file, retries and waits included.

    `per_attempt_s` is what the platform passes as `timeout_s`, and the package applies it to EACH
    attempt rather than to the whole call. That is the sentence the old numbers were missing.
    """
    if per_attempt_s <= 0:
        # Zero means "no clock" in the settings catalogue, and the platform then calls `measure` rather
        # than `measure_isolated`: one attempt, in-process, with no timeout at all. There is no budget
        # to compute, so the caller gets its floor.
        return 0.0
    return ISOLATED_ATTEMPTS * float(per_attempt_s) + (ISOLATED_ATTEMPTS - 1) * ISOLATED_RETRY_WAIT_S


def largest_per_attempt_within(outer_s: float) -> float:
    """The largest per-attempt deadline whose whole packaged budget still fits inside `outer_s`.

    The inverse of the function above, for a caller whose outer bound is fixed by something other than
    this module - a customer waiting on an HTTP request, for instance. Nothing in the platform calls it
    yet; it is here because the inline upload path needs exactly this number and cannot be changed from
    this file (see the report).
    """
    room = float(outer_s) - (ISOLATED_ATTEMPTS - 1) * ISOLATED_RETRY_WAIT_S
    return max(0.0, room / ISOLATED_ATTEMPTS)


def outer_kill_s(inner_total_s: float, soft_floor_s: float, hard_floor_s: float) -> tuple[int, int]:
    """The (soft, hard) Celery limits for an outer kill that must outlive `inner_total_s`.

    Both are whole seconds and both are strictly greater than what they contain, in that order:
    inner work < soft kill < hard kill. Neither ever comes out below the floor it is given.
    """
    soft = int(math.ceil(max(float(soft_floor_s), float(inner_total_s) + PLATFORM_OVERHEAD_S)))
    hard = int(math.ceil(max(float(hard_floor_s), soft + SOFT_TO_HARD_GRACE_S)))
    return soft, hard


#: THE MEASUREMENT. Inner: GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS per attempt (900 by default), three
#: attempts, two 3 s waits - 2706 s. Outer: that plus the platform's own two minutes, then a minute of
#: grace for the hard kill. 2826 and 2886 at the default setting, against 1200 and 1500 before.
MEASURE_PER_ATTEMPT_S = float(polcfg.GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS)
MEASURE_PACKAGED_BUDGET_S = packaged_measurement_budget_s(MEASURE_PER_ATTEMPT_S)
MEASURE_SOFT_TIME_LIMIT_S, MEASURE_TIME_LIMIT_S = outer_kill_s(
    MEASURE_PACKAGED_BUDGET_S, MEASURE_SOFT_FLOOR_S, MEASURE_HARD_FLOOR_S)

#: THE LOOK. Inner: GEOMETRY_VISION_TIMEOUT_SECONDS (180 by default), and that one IS a whole-call
#: deadline - `geometry_agent.vision.look` runs the description on a thread and joins it with exactly
#: that limit, so the call returns at the deadline whatever the provider is doing. There are no
#: packaged retries outside it to account for. The look's old 600 and 900 already outlived it, so the
#: floor is what applies here and nothing changes; the numbers are derived anyway, because an operator
#: who raises the vision timeout past 480 s would otherwise move the inner budget above the outer kill
#: and get the measurement's bug in the look.
LOOK_INNER_BUDGET_S = float(polcfg.GEOMETRY_VISION_TIMEOUT_SECONDS)
LOOK_SOFT_TIME_LIMIT_S, LOOK_TIME_LIMIT_S = outer_kill_s(
    LOOK_INNER_BUDGET_S, LOOK_SOFT_FLOOR_S, LOOK_HARD_FLOOR_S)

__all__ = [
    "ISOLATED_ATTEMPTS",
    "ISOLATED_RETRY_WAIT_S",
    "LOOK_INNER_BUDGET_S",
    "LOOK_SOFT_TIME_LIMIT_S",
    "LOOK_TIME_LIMIT_S",
    "MEASURE_PACKAGED_BUDGET_S",
    "MEASURE_PER_ATTEMPT_S",
    "MEASURE_SOFT_TIME_LIMIT_S",
    "MEASURE_TIME_LIMIT_S",
    "PLATFORM_OVERHEAD_S",
    "SOFT_TO_HARD_GRACE_S",
    "largest_per_attempt_within",
    "outer_kill_s",
    "packaged_measurement_budget_s",
]
