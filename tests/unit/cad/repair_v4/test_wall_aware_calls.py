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
    """Just over the floor: the members get MIN_MEMBER_S each rather than 3 s each, even though that overshoots
    the remainder - a member under the floor cannot read the file, so its 'fail' would mean nothing."""
    wall.set_file_deadline(time.monotonic() + wall.MIN_MEMBER_S + 1.0)
    timeout, clipped = wall.clip_member_timeout(4, 300.0)
    assert clipped is True and timeout == pytest.approx(wall.MIN_MEMBER_S)


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


def _assemble_with(monkeypatch, *, left: float | None, statuses=("pass", "pass")):
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
    monkeypatch.setattr(assemble, "lease_seconds", lambda n: 10.0)
    wall.set_file_deadline(None if left is None else time.monotonic() + left)
    cache = _Cache()
    v = assemble.verdict(object(), "s.step", "o.step", members=[_member("a"), _member("b")],
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


def test_a_clipped_panel_with_a_failing_member_is_inconclusive_not_a_fail(monkeypatch) -> None:
    """A member that ran out of a clipped slot says nothing about the part. `clean_of` refuses an inconclusive
    verdict and `score` raises on one, so nothing ranks or ships on the strength of a clock."""
    v, _cache, _seen = _assemble_with(monkeypatch, left=120.0, statuses=("pass", "fail"))
    assert v.inconclusive is True and v.clean is False


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
