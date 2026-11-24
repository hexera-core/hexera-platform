# Responsibility: Verify B22's catastrophic-loss rule - the thresholds, that a FUSE is not a loss, the delivery
#                 gate, and that the search falls back to its next-best state rather than ranking such a state best.
# Boundaries: no OCCT and no real pool. The kernel's measurement is faked; what is tested is the RULE.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meshpipeline.cad.repair_v4.loss import KEEP_FRACTION, LossReport, check_loss, from_kernel
from meshpipeline.cad.repair_v4.search import deliverable


@dataclass
class _Res:
    """What `deliverable` reads of a result."""
    changed: bool
    best: Any
    best_verdict: Any
    best_loss: LossReport | None = None


class _V:
    def __init__(self, valid: bool, passes: int, clean: bool = False) -> None:
        self.valid, self.passes, self.clean = valid, passes, clean


def _score(v: _V) -> tuple:
    return (1 if v.valid else 0, v.passes, 0.0, 0.0, 0)


def _kernel(bodies: dict[str, float | None], *, part_kept: float, volume_ratio: float = 1.0,
            total: int | None = None, solidless: bool = False) -> dict:
    return {"solidless": solidless, "bodies": bodies, "bodies_total": total if total is not None else len(bodies),
            "part_kept": part_kept, "volume_ratio": volume_ratio}


# --- the rule -----------------------------------------------------------------------------------

def test_the_worked_case_is_refused_on_the_PART_rule() -> None:
    """v4 search_ai on sha 3e7c2a1197c3: volume x0.00069, bodies 8 -> 4. 0.07% of the part is not a repair of it."""
    report = from_kernel(_kernel({f"S{i}": 0.00069 for i in range(1, 9)}, part_kept=0.00069, volume_ratio=0.00069))
    assert report.catastrophic
    assert report.reason == "part_volume_lost"
    assert report.row()["part_kept"] == 0.00069


def test_a_FUSE_is_not_a_loss_however_far_the_body_count_drops() -> None:
    """8 overlapping bodies fused into 4: every input body's volume is still there, so nothing is lost. The total
    volume FALLS (the shared volume was counted twice before and once after), which is why the rule is not a
    volume ratio and never reads the body count."""
    report = from_kernel(_kernel({f"S{i}": 1.0 for i in range(1, 9)}, part_kept=1.0, volume_ratio=0.93))
    assert not report.catastrophic
    assert report.reason is None
    assert report.volume_ratio == 0.93  # reported, and not what the rule read


def test_one_deleted_body_is_refused_even_when_the_part_is_mostly_there() -> None:
    report = from_kernel(_kernel({"S1": 1.0, "S2": 1.0, "S3": 1.0, "S4": 0.0}, part_kept=0.74))
    assert report.catastrophic
    assert report.reason == "body_lost"
    assert report.lost_bodies == ("S4",)


def test_the_threshold_is_UNDER_ten_percent_on_both_rules() -> None:
    assert not from_kernel(_kernel({"S1": KEEP_FRACTION}, part_kept=KEEP_FRACTION)).catastrophic
    assert from_kernel(_kernel({"S1": KEEP_FRACTION - 1e-9}, part_kept=1.0)).catastrophic
    assert from_kernel(_kernel({"S1": 1.0}, part_kept=KEEP_FRACTION - 1e-9)).catastrophic


def test_a_body_whose_boolean_FAILED_is_unmeasured_and_never_a_loss() -> None:
    """"the Boolean did not complete" and "the body is gone" must not look alike to a rule that refuses."""
    report = from_kernel(_kernel({"S1": 1.0, "S2": None}, part_kept=1.0))
    assert not report.catastrophic
    assert report.bodies_measured == 1
    assert report.detail["unmeasured_bodies"] == ["S2"]


def test_a_solidless_input_can_never_trip_the_rule() -> None:
    """A loose-faces or shell input has no volume to keep; a heal that makes it a solid must not be refused."""
    report = from_kernel(_kernel({}, part_kept=None, solidless=True))  # type: ignore[arg-type]
    assert report.measured and report.solidless and not report.catastrophic


def test_a_failed_CALL_refuses_nothing_and_says_so() -> None:
    class Dead:
        def call(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("the pool is gone")

    report = check_loss(Dead(), "/a.brep", "/b.brep")
    assert not report.measured and not report.catastrophic
    assert "RuntimeError" in str(report.unmeasured_why)


# --- the delivery gate --------------------------------------------------------------------------

def test_the_gate_REFUSES_a_catastrophic_state_that_beats_the_input_on_the_panel() -> None:
    lost = from_kernel(_kernel({"S1": 0.0005}, part_kept=0.0005))
    res = _Res(changed=True, best="the-fragment", best_verdict=_V(True, 4), best_loss=lost)
    assert deliverable(res, _V(True, 1), score_fn=_score) is None


def test_the_gate_delivers_the_same_state_when_the_loss_was_NOT_measured() -> None:
    """Fail-open: an unmeasured state ships exactly as it did before this rule existed."""
    res = _Res(changed=True, best="the-output", best_verdict=_V(True, 4),
               best_loss=LossReport(measured=False, unmeasured_why="timeout"))
    assert deliverable(res, _V(True, 1), score_fn=_score) == "the-output"


def test_a_result_with_no_loss_field_at_all_still_delivers() -> None:
    """The staged stand-in and every row written before B22 carry no measurement."""
    class NoLoss:
        changed, best, best_verdict = True, "the-output", _V(True, 4)

    assert deliverable(NoLoss(), _V(True, 1), score_fn=_score) == "the-output"


def test_an_explicit_loss_argument_overrides_the_result_and_refuses() -> None:
    """How the staged arm meets the same gate (`arms._confirmed_or_input`)."""
    class NoLoss:
        changed, best, best_verdict = True, "the-output", _V(True, 4)

    lost = from_kernel(_kernel({"S1": 1.0, "S2": 0.0}, part_kept=0.5))
    assert deliverable(NoLoss(), _V(True, 1), score_fn=_score, loss=lost) is None


def test_a_payload_that_measured_NOTHING_claims_nothing() -> None:
    """A report saying `measured` with no number in it would look like evidence while refusing nothing."""
    report = from_kernel({"solidless": False, "bodies": {}, "bodies_total": 3, "part_kept": None})
    assert not report.measured and not report.catastrophic
    assert report.unmeasured_why == "nothing_measured"
