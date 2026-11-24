"""D23.2: the full panel's members mesh in parallel (P per call), per lane C16's contract: results in the CALLER's
order, bit-identical to serial where no doom cap applies; after the first non-pass, members still running are cut
to t_doom + cap and convert to CAPPED_KIND; a member that ended before t_doom keeps its result exactly."""
from __future__ import annotations

import time

from meshpipeline.cad.repair_v4.verdict.panel import CAPPED_KIND, MemberResult, run_panel
from meshpipeline.cad.repair_v4.worker_pool import CALL_DEADLINE, Deadline, _poll_until


class Fake:
    """A member that takes `seconds`, honouring the doom clock the way worker_pool.call now does."""

    def __init__(self, name: str, seconds: float, status: str = "pass", kind: str | None = None) -> None:
        self.name, self.seconds, self.status, self.kind = name, seconds, status, kind

    def run(self, pool, step_path: str, *, timeout_s: float) -> MemberResult:
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            cut = CALL_DEADLINE.get()
            if cut is not None and cut.at is not None and time.monotonic() >= cut.at:
                return MemberResult(self.name, "timeout", "timeout", "cut at the doomed cap")
            time.sleep(0.01)
        return MemberResult(self.name, self.status, self.kind, "", min_sicn=0.3 if self.status == "pass" else None)


def test_an_all_pass_panel_in_parallel_is_the_serial_panel_in_the_callers_order() -> None:
    members = [Fake("gmsh_prod", 0.15), Fake("gmsh_default", 0.02), Fake("gmsh_alt", 0.10), Fake("netgen_occ", 0.05)]
    serial = run_panel(None, "x.step", members, timeout_s=10, doomed_cap_s=60)                 # type: ignore[arg-type]
    t0 = time.monotonic()
    parallel = run_panel(None, "x.step", members, timeout_s=10, doomed_cap_s=60, panel_threads=4)  # type: ignore[arg-type]
    assert parallel == serial and [r.name for r in parallel] == ["gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ"]
    assert time.monotonic() - t0 < 0.15 + 0.1          # wall ~ the slowest member, not the sum


def test_after_the_first_failure_a_slow_member_is_cut_to_the_cap_and_capped() -> None:
    members = [Fake("gmsh_prod", 5.0), Fake("gmsh_default", 0.02, "fail", "overlapping_facets"),
               Fake("gmsh_alt", 0.01), Fake("netgen_occ", 5.0)]
    t0 = time.monotonic()
    got = run_panel(None, "x.step", members, timeout_s=10, doomed_cap_s=0.2, panel_threads=4)  # type: ignore[arg-type]
    assert time.monotonic() - t0 < 2.0                  # cut at t_doom + 0.2 s, not 5 s
    by = {r.name: r for r in got}
    assert by["gmsh_default"].error_kind == "overlapping_facets"            # the doom itself, kept
    assert by["gmsh_alt"].status == "pass"                                  # ended BEFORE t_doom: kept exactly
    assert by["gmsh_prod"].status == "fail" and by["gmsh_prod"].error_kind == CAPPED_KIND
    assert by["netgen_occ"].error_kind == CAPPED_KIND


def test_without_a_doom_cap_a_parallel_panel_waits_for_every_member() -> None:
    members = [Fake("gmsh_prod", 0.1, "fail", "other"), Fake("netgen_occ", 0.3)]
    got = run_panel(None, "x.step", members, timeout_s=10, panel_threads=2)  # type: ignore[arg-type]
    assert [r.status for r in got] == ["fail", "pass"]


def test_a_pool_call_honours_a_deadline_moved_earlier() -> None:
    class Conn:
        def poll(self, t: float) -> bool:
            time.sleep(t)
            return False

    d = Deadline()
    tok = CALL_DEADLINE.set(d)
    try:
        d.at = time.monotonic() + 0.1
        t0 = time.monotonic()
        assert _poll_until(Conn(), time.monotonic() + 10) is False
        assert time.monotonic() - t0 < 1.0
    finally:
        CALL_DEADLINE.reset(tok)
