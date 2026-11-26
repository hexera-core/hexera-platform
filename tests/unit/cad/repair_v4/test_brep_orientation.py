# Responsibility: lane C15 follow-ups (Lane D's review of b0866cf7): the BREP orientation check uses layer 1's own
#                 threshold (<= 0), and a check that could not run is reported (soft `orientation_unchecked`), not silent.
# Boundaries: hermetic; a fake pool and a minimal verdict.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.cad.repair_v4.verdict.assemble import Verdict, with_brep_orientation
from meshpipeline.cad.repair_v4.verdict.valid import SOFT_CODES, ValidReport
from meshpipeline.cad.repair_v4.worker_pool import CallResult


def _v() -> Verdict:
    return Verdict(ValidReport(True, [], [], "mm", 2, True), [], None, True, "full")


class _Pool:
    def __init__(self, result=None, raise_=None) -> None:
        self.result, self.raise_ = result, raise_

    def call(self, fn, payload, *, timeout_s):
        if self.raise_:
            raise self.raise_
        return self.result


def test_a_zero_volume_solid_is_non_positive_like_layer_1() -> None:
    v = with_brep_orientation(_Pool(CallResult(True, {"volumes": [10.0, 0.0]}, None, "ok", 0.0)), "s.brep", _v())
    assert not v.valid.ok and not v.clean and [f.code for f in v.valid.hard] == ["non_positive_volume"]


def test_a_timeout_or_a_dead_pool_is_reported_not_silent() -> None:
    for pool in (_Pool(CallResult(False, None, "deadline", "timeout", 120.0)), _Pool(raise_=RuntimeError("gone"))):
        v = with_brep_orientation(pool, "s.brep", _v())
        assert v.valid.ok and v.clean                                  # validity unchanged
        (f,) = v.valid.soft
        assert f.code == "orientation_unchecked" and f.detail["reason"]
    assert "orientation_unchecked" in SOFT_CODES
