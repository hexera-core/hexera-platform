# Responsibility: A16 - no call starts whose worst case exceeds what is left of the file wall, a clipped panel is
#                 never cached, and a clipped panel cannot certify.
# Boundaries: hermetic; a fake runner and a fake clock. The measured shape it is built from is real:
#             263d5e2d5bab paid ONE 929.95 s full panel against a 900 s search wall and ran 2050 s against a
#             1200 s file wall.
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4.verdict import wall


@pytest.fixture(autouse=True)
def _no_deadline():
    wall.set_file_deadline(None)
    yield
    wall.set_file_deadline(None)


def test_with_no_deadline_every_call_runs_exactly_as_before() -> None:
    """A caller that never sets a wall must lose nothing: the timeout is the one it asked for, unclipped."""
    assert wall.remaining_s() is None
    assert wall.clip_member_timeout(4, 300.0) == (300.0, False)
    assert wall.fits(1e9) is True


def test_a_panel_that_fits_keeps_its_exact_timeout_so_the_cache_key_cannot_move() -> None:
    """The key carries MEMBER_TIMEOUT_S. A full-budget panel must come out at exactly 300.0, not 299.97."""
    wall.set_file_deadline(time.monotonic() + 10_000.0)
    timeout, clipped = wall.clip_member_timeout(4, 300.0)
    assert (timeout, clipped) == (300.0, False)


def test_the_measured_shape_one_930_s_panel_against_a_900_s_wall_is_clipped() -> None:
    """4 members at 300 s plus the retry slots is a 1800 s worst case. With 900 s left it cannot run as asked."""
    wall.set_file_deadline(time.monotonic() + 900.0)
    timeout, clipped = wall.clip_member_timeout(4, 300.0)
    assert clipped is True
    assert timeout == pytest.approx(900.0 / 6, rel=0.02)      # 6 slots: 4 members + 2 retry
    # and the whole clipped panel now fits where the unclipped one did not
    assert timeout * 6 <= 900.0 + 1.0


def test_a_call_with_no_useful_time_left_refuses_instead_of_guaranteeing_an_overrun() -> None:
    wall.set_file_deadline(time.monotonic() + 5.0)
    with pytest.raises(wall.TooLateToStart) as why:
        wall.clip_member_timeout(4, 300.0)
    assert "left of the file wall" in str(why.value)


def test_the_floor_holds_so_a_fail_is_never_a_statement_about_the_clock() -> None:
    """Just over the floor for ONE member but not for the panel's slots: the panel is NOT started (CRS v5 step 4,
    owner decision 2026-11-24 "every file finishes"). Flooring each member at MIN_MEMBER_S used to start a panel
    that overshot the remainder - a file stopping past its wall; the members are never given less than the floor."""
    wall.set_file_deadline(time.monotonic() + wall.MIN_MEMBER_S + 1.0)
    with pytest.raises(wall.TooLateToStart):
        wall.clip_member_timeout(4, 300.0)


def test_remaining_never_goes_negative_and_a_passed_deadline_refuses() -> None:
    wall.set_file_deadline(time.monotonic() - 50.0)
    assert wall.remaining_s() == 0.0
    assert wall.fits(0.1) is False
    with pytest.raises(wall.TooLateToStart):
        wall.clip_member_timeout(4, 300.0)


# --- the assembled verdict: a clipped panel is not cached, and cannot certify ---------------------------------------


def _member(name: str = "gmsh_default"):
    return SimpleNamespace(name=name)


def _result(name: str, status: str = "pass"):
    return SimpleNamespace(name=name, status=status, min_sicn=0.3, error_kind=None, located=[], message="",
                           cause=None)


class _Cache:
    """A cache that records whether it was asked to compute-and-store."""

    def __init__(self) -> None:
        self.single_flight_calls = 0

    def single_flight(self, sha, config, compute, *, lease_s):  # noqa: ANN001, ARG002
        self.single_flight_calls += 1
        return compute()

    def note_error(self) -> None:
        pass


def _assemble_with(monkeypatch, *, left: float | None, statuses=("pass", "pass"), members: int = 2):
    from meshpipeline.cad.repair_v4.verdict import assemble

    seen: dict = {}

    def runner(pool, step, members, *, timeout_s, **kw):  # noqa: ANN001, ARG001
        seen["timeout_s"] = timeout_s
        return [_result(m.name, s) for m, s in zip(members, statuses, strict=False)]

    monkeypatch.setattr(assemble, "check_valid",
                        lambda pool, step, *, original_units: SimpleNamespace(
                            ok=True, hard=[], soft=[], unbounded=False))
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda *a, **k: None)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "x"})
    monkeypatch.setattr(assemble, "file_sha256", lambda step: "sha")
    monkeypatch.setattr(assemble, "panel_config_hash", lambda *a, **k: "cfg")
    monkeypatch.setattr(assemble, "lease_seconds", lambda n, *a: 10.0)
    wall.set_file_deadline(None if left is None else time.monotonic() + left)
    cache = _Cache()
    panel_members = [_member(chr(ord("a") + i)) for i in range(members)]
    v = assemble.verdict(object(), "s.step", "o.step", members=panel_members,
                         cache=cache, run_panel=runner)
    return v, cache, seen


def test_an_unclipped_panel_is_cached_at_the_timeout_the_key_promises(monkeypatch) -> None:
    v, cache, seen = _assemble_with(monkeypatch, left=10_000.0)
    assert seen["timeout_s"] == 300.0
    assert cache.single_flight_calls == 1, "a full-budget panel still goes through the cache"
    assert v.inconclusive is False


def test_a_clipped_panel_is_never_handed_to_the_cache(monkeypatch) -> None:
    """LEAD-3's rule. The key carries 300 s; storing a 40 s-per-member panel under it would hand every later
    reader a fail that is about the clock."""
    v, cache, seen = _assemble_with(monkeypatch, left=120.0)
    assert seen["timeout_s"] < 300.0
    assert cache.single_flight_calls == 0, "a clipped panel must not be stored, claimed, or read"
    assert v.panel and len(v.panel) == 2


def test_a_clipped_panel_whose_member_the_CLOCK_stopped_is_inconclusive(monkeypatch) -> None:
    """A member the clipped clock itself stopped says nothing about the part: that is the surviving concern."""
    v, _cache, _seen = _assemble_with(monkeypatch, left=120.0, statuses=("pass", "timeout"))
    assert v.inconclusive is True and v.clean is False


def test_a_clipped_panel_whose_member_GENUINELY_FAILED_is_still_a_fail(monkeypatch) -> None:
    """THE REGRESSION THIS FILE MISSED. The first version made a clipped panel inconclusive on ANY non-pass
    member. That contradicts this module's own rule - a mesher that overran its limit or failed on the geometry is
    the FILE failing the definition - and on the fast loop it meant NO file was ever measured."""
    v, _cache, _seen = _assemble_with(monkeypatch, left=120.0, statuses=("pass", "fail"))
    assert v.inconclusive is False, "a member that finished inside its clipped slot gave a real verdict"
    assert v.clean is False        # still not clean: a member failed


def test_the_fast_loop_s_OWN_WALLS_still_measure_a_failing_dev_file(monkeypatch) -> None:
    """The arithmetic my earlier tests never used, and the reason this shipped. The fast loop gives 1200 s per
    file; a 4-member panel's worst case is (4 + 2) x 300 = 1800 s, so EVERY input panel on that loop is clipped.
    The dev population is selected for having failing members. If a clipped panel with a failing member is
    inconclusive, every dev file's input verdict says nothing, `_run_search` raises SearchUnavailable, and the
    wave spends its money for no rows - measured as 2 of 2 on real parts before this was narrowed."""
    v, _cache, seen = _assemble_with(monkeypatch, left=1200.0,
                                     statuses=("pass", "fail", "pass", "fail"),
                                     members=4)
    assert seen["timeout_s"] < 300.0, "1200 s of wall against an 1800 s worst case must clip"
    assert v.inconclusive is False, "a clipped panel must still be able to MEASURE a failing dev file"


def test_a_clipped_panel_that_all_passed_is_still_inconclusive_only_if_a_member_failed(monkeypatch) -> None:
    v, _cache, _seen = _assemble_with(monkeypatch, left=120.0, statuses=("pass", "pass"))
    assert v.inconclusive is False, "an all-pass clipped panel is a real pass: every member finished in its slot"


def test_a_panel_refused_for_time_is_infrastructure_not_a_bad_part(monkeypatch) -> None:
    """`TooLateToStart` reaches callers as `CheckerUnavailable`, which the search already treats as inconclusive
    and `repair_v4` as a file it did not measure. A part must never be called bad because a clock ran out."""
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    with pytest.raises(CheckerUnavailable) as why:
        _assemble_with(monkeypatch, left=3.0)
    assert "not starting a panel" in str(why.value)


# --- the leak that made the unit directory flaky (A16 follow-up) ----------------------------------------------------


def test_a_failed_pool_start_does_not_LEAK_the_file_deadline(monkeypatch, tmp_path) -> None:
    """THE CAUSE of A16's flaky directory. `set_file_deadline` ran before the pool's try/except, so a pool that
    failed to start raised `SearchUnavailable` past the only `finally` that cleared it. The deadline then stayed
    set for the rest of the PROCESS: every later panel saw a shrinking remainder, got clipped, and - with any
    non-pass member - came back inconclusive instead of a real verdict. Whether a given test tripped over it
    depended on how far into the run it was and how fast the machine was, which is exactly the 28/40/29/28/29/29/30
    pattern LEAD-3 measured.
    """
    import meshpipeline.cad.repair_v4 as v4

    src = tmp_path / "in.step"
    src.write_text("ISO-10303-21;\n")

    def no_pool(*a, **kw):  # noqa: ANN001, ANN003, ARG001
        raise RuntimeError("no worker for you")

    # `_run_search` imports WorkerPool locally, so it must be patched where it is DEFINED, not on this module
    import meshpipeline.cad.repair_v4.worker_pool as wp

    monkeypatch.setattr(wp, "WorkerPool", no_pool)
    monkeypatch.setattr(v4, "ARM", "search_det")
    wall.set_file_deadline(None)
    with pytest.raises(v4.SearchUnavailable):
        v4.repair_v4(str(src), str(tmp_path / "out.step"),
                     budget=v4.Budget(wall_s=10.0, ops=1, model_calls=0, model_usd=0.0),
                     deadline_s=time.monotonic() + 600.0)
    assert wall.file_deadline() is None, (
        "a failed run must leave no deadline behind: the next call in this process would be clipped by a wall that "
        "belongs to a file that never ran")


def test_an_expired_deadline_returns_early_and_sets_nothing(monkeypatch, tmp_path) -> None:
    """The other early exit: a deadline already past returns `not_measured` before the wall is set at all."""
    import meshpipeline.cad.repair_v4 as v4

    src = tmp_path / "in.step"
    src.write_text("ISO-10303-21;\n")
    monkeypatch.setattr(v4, "ARM", "search_det")
    wall.set_file_deadline(None)
    out = v4.repair_v4(str(src), str(tmp_path / "out.step"),
                       budget=v4.Budget(wall_s=10.0, ops=1, model_calls=0, model_usd=0.0),
                       deadline_s=time.monotonic() - 1.0)
    assert out.verdict_summary.get("reason") == "deadline_expired"
    assert wall.file_deadline() is None


def test_the_op_s_own_seconds_reach_the_row_so_a_worst_case_can_be_measured() -> None:
    """A16 item (1) needs per-op worst cases, and no stored row carried them: a candidate event had only the
    BATCH's timestamp, shared by every candidate in it. The kernel already measures each op (`Diff.seconds`); it
    was simply not written down. Summed, because a `run_stage` macro applies a chain."""
    from types import SimpleNamespace as NS

    from meshpipeline.cad.repair_v4.search import _op_seconds

    assert _op_seconds(NS(diffs=[NS(seconds=1.25)])) == 1.25
    assert _op_seconds(NS(diffs=[NS(seconds=1.5), NS(seconds=2.25)])) == 3.75   # a chain pays for every link
    assert _op_seconds(NS(diffs=[])) is None                                     # applied nothing, measured nothing
    assert _op_seconds(NS()) is None
    assert _op_seconds(NS(diffs=[NS(seconds=None), NS(seconds=0.5)])) == 0.5     # an unmeasured link is skipped
