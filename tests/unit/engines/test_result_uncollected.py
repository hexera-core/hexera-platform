# Responsibility: Verify a remote mesh whose result could not be collected is reported as exactly that,
# with the remote's own summary and the repair that applies - never as a run that never started.
# Job 3cd77f85: a 7 M-cell blade-row passage came back as a 557 MB archive, tripped the 512 MiB
# extraction cap, and every attempt read "INFRASTRUCTURE failure - the mesh run never started".
from __future__ import annotations

from meshpipeline.adapters.mesh_execution.cloud_run_client import (
    RESULT_UNCOLLECTED_MARKER,
    _fail,
    _uncollected,
)
from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE
from meshpipeline.engines.snappy.judge import _repair_message

_REMOTE = {"rc": 0, "timed_out": False,
           "quality": {"cells": 7004293, "faces": 22029824, "mesh_ok": False}}


class _Over(Exception):
    pass


def test_an_uncollected_result_keeps_the_infrastructure_rc_but_says_what_happened():
    r = _uncollected("snappy", _REMOTE, _Over("archive is 557411851 bytes, over the 536870912 cap"))
    assert r["rc"] == RC_INFRASTRUCTURE
    assert r["log_tail"].startswith(RESULT_UNCOLLECTED_MARKER)
    assert "the mesh ran" in r["log_tail"] and "cells=7004293" in r["log_tail"]
    assert "over the 536870912 cap" in r["log_tail"]
    assert r["remote_quality"]["cells"] == 7004293


def test_a_failure_before_any_result_is_still_the_plain_marker():
    r = _fail("snappy", "ConnectionError: no route")
    assert r["rc"] == RC_INFRASTRUCTURE and r["log_tail"].startswith("[CLOUD_RUN_FAILED]")
    assert RESULT_UNCOLLECTED_MARKER not in r["log_tail"]


def test_the_judge_tells_the_planner_to_shrink_the_mesh_when_the_archive_was_over_cap():
    r = _uncollected("snappy", _REMOTE, _Over("archive is 557411851 bytes, over the 536870912 cap"))
    msg = _repair_message("rc", r, {})
    assert "TOO LARGE TO COLLECT" in msg and "REDUCE max_cells" in msg
    assert "never started" not in msg


def test_the_judge_keeps_the_never_started_advice_for_a_true_dispatch_failure():
    msg = _repair_message("rc", _fail("snappy", "ConnectionError: no route"), {})
    assert "never started" in msg and "Resubmit this plan unchanged" in msg


def test_an_uncollected_result_for_another_reason_asks_for_an_unchanged_resubmit():
    r = _uncollected("snappy", _REMOTE, OSError("disk full"))
    msg = _repair_message("rc", r, {})
    assert "RESULT NOT COLLECTED" in msg and "resubmit this plan unchanged" in msg
