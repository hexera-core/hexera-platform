# Responsibility: THE GOAL, owner decision 2026-11-24: clean = layer 1 VALID + layer 2 PANEL. Layer 3 (faithful) is
#                 still computed and reported on every row, and never gates clean nor refuses an edit.
# Boundaries: hermetic; no OCCT. The real-geometry proof is tests/native/test_native_repair_v4_faithful.py.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.assemble import clean_of, score
from meshpipeline.cad.repair_v4.verdict.cache import VERDICT_CODE_VERSION


def _m(status: str = "pass", sicn: float = 0.3):
    return SimpleNamespace(name=f"m{status}", status=status, min_sicn=sicn, error_kind=None)


def _valid(ok: bool = True):
    return SimpleNamespace(ok=ok, hard=[], soft=[], unbounded=False)


def _faith(ok: bool, **kw):
    base = {"ok": ok, "max_dev_mm": 0.0 if ok else 9.9, "reasons": [] if ok else ["deviation_exceeds_bound"],
            "declared_added": [], "declared_removed": [], "volume_ratio": 1.0, "area_ratio": 1.0,
            "bbox_ratio": 1.0, "bodies_before": 1, "bodies_after": 1}
    return SimpleNamespace(**{**base, **kw})


def _rule(**over):
    args = {"tier": "full", "inconclusive": False, "valid": _valid(True), "panel": [_m(), _m()],
            "faithful": _faith(True)}
    args.update(over)
    return clean_of(**args)


def test_clean_is_valid_and_panel_and_nothing_else() -> None:
    assert _rule() is True
    assert _rule(valid=_valid(False)) is False
    assert _rule(panel=[_m(), _m("fail")]) is False
    assert _rule(panel=[]) is False
    assert _rule(tier="cheap") is False          # a partial panel can never certify
    assert _rule(inconclusive=True) is False     # says nothing about the file


@pytest.mark.parametrize("faithful", [
    None,
    "unfaithful",
    "moved_a_lot",
])
def test_layer_three_never_gates_clean(faithful) -> None:
    """The decision: an unfaithful output that is valid and meshes everywhere IS clean."""
    f = {None: None, "unfaithful": _faith(False),
         "moved_a_lot": _faith(False, volume_ratio=3.0, bbox_ratio=0.2, max_dev_mm=50.0)}[faithful]
    assert _rule(faithful=f) is True


def test_the_score_gate_is_valid_alone_so_the_search_never_refuses_an_edit() -> None:
    def v(valid: bool, faithful, passes: int = 2):
        return SimpleNamespace(valid=_valid(valid), panel=[_m() for _ in range(passes)] + [_m("fail")],
                               faithful=faithful, inconclusive=False)

    unfaithful = score(v(True, _faith(False)))
    faithful = score(v(True, _faith(True)))
    assert unfaithful[0] == faithful[0] == 1          # key 0 is layer 1 only
    assert score(v(False, _faith(True)))[0] == 0
    # deviation stays a LOW tiebreaker: among equals, less movement still wins
    near = score(v(True, _faith(True, max_dev_mm=0.01)))
    far = score(v(True, _faith(True, max_dev_mm=5.0)))
    assert near > far and near[:3] == far[:3]


def test_layer_three_does_not_rank_states_that_are_not_valid_yet() -> None:
    """A tie-break needs a tie. On the cheap tier an unfinished repair scores (0, 0, UNMEASURED) whatever it did,
    so deviation was the only key left and ordered the frontier by itself - and since every op moves the part, it
    put the state that repaired LESS first. Measured on the three-hole part: it stopped reaching clean at all
    (`tests/native/test_native_repair_v4_waste.py`). Invalid states therefore tie on keys 3 and 4."""
    def v(valid: bool, dev: float, churn: int = 0, passes: int = 0):
        f = _faith(True, max_dev_mm=dev, declared_added=["F1"] * churn)
        return SimpleNamespace(valid=_valid(valid), panel=[_m() for _ in range(passes)], faithful=f,
                               inconclusive=False)

    moved_more, moved_less = score(v(False, 1.5)), score(v(False, 1.42))
    assert moved_more == moved_less == (0, 0, assemble.UNMEASURED_SICN, 0.0, 0)
    assert score(v(False, 1.5, churn=7)) == moved_less          # churn does not rank an invalid state either
    # ... and among VALID states it still decides, both ways round
    assert score(v(True, 0.01)) > score(v(True, 5.0))
    assert score(v(True, 0.5)) > score(v(True, 0.5, churn=3))


def test_a_file_with_no_faithful_measurement_can_still_be_clean() -> None:
    assert _rule(faithful=None) is True


def test_the_verdict_code_version_is_bumped_past_the_gating_definition() -> None:
    # past .4 (the gating definition); .5 is lane D17's located-padding fix, which bumps it again
    assert int(VERDICT_CODE_VERSION.rsplit(".", 1)[1]) >= 4


def test_faithful_is_measured_even_when_layer_one_fails_and_never_changes_the_verdict(monkeypatch) -> None:
    """Reported on every row: an invalid output still gets its fidelity numbers when they can be measured."""
    calls: list = []

    def fake_faithful(pool, original, step, **kw):
        calls.append(step)
        return _faith(False, volume_ratio=0.5)

    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: _valid(False))
    monkeypatch.setattr(assemble, "check_faithful_steps", fake_faithful)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "x"})
    v = assemble.verdict(object(), "s.step", "o.step", members=[object()],
                         run_panel=lambda *a, **k: [_m(), _m()])
    assert calls == ["s.step"]                 # measured although layer 1 failed
    assert v.faithful is not None and v.faithful.ok is False
    assert v.clean is False and v.valid.ok is False   # ... and it changed nothing


def test_a_faithful_measurement_that_cannot_be_made_is_not_an_error(monkeypatch) -> None:
    def boom(pool, original, step, **kw):
        raise ValueError("original shape has no extent")

    monkeypatch.setattr(assemble, "check_valid", lambda pool, step, *, original_units: _valid(True))
    monkeypatch.setattr(assemble, "check_faithful_steps", boom)
    monkeypatch.setattr(assemble, "mesher_versions", lambda pool: {"gmsh": "x"})
    v = assemble.verdict(object(), "s.step", "o.step", members=[object()],
                         run_panel=lambda *a, **k: [_m(), _m()])
    assert v.faithful is None and v.clean is True   # unmeasurable fidelity cannot block a clean verdict


# ------------------------------------------------------------------ the delivery gate: no worse in valid OR panel

def _verdict(valid: bool, passes: int, clean: bool = False):
    return SimpleNamespace(valid=_valid(valid), panel=[_m() for _ in range(passes)] + [_m("fail")],
                           faithful=_faith(True), inconclusive=False, clean=clean)


def _result(changed: bool, best_verdict, best="ws"):
    return SimpleNamespace(changed=changed, best_verdict=best_verdict, best=best)


def test_the_delivery_gate_refuses_an_output_that_lost_validity() -> None:
    from meshpipeline.cad.repair_v4.search import deliverable

    # same panel passes, but layer 1 regressed: not deliverable, however good the mesh looks
    assert deliverable(_result(True, _verdict(False, 3)), _verdict(True, 3)) is None
    # layer 1 gained: deliverable
    assert deliverable(_result(True, _verdict(True, 3)), _verdict(False, 3)) == "ws"


def test_the_delivery_gate_still_refuses_fewer_panel_passes() -> None:
    from meshpipeline.cad.repair_v4.search import deliverable

    assert deliverable(_result(True, _verdict(True, 2)), _verdict(True, 3)) is None
    assert deliverable(_result(True, _verdict(True, 3)), _verdict(True, 3)) == "ws"


def test_the_delivery_gate_ignores_fidelity() -> None:
    from meshpipeline.cad.repair_v4.search import deliverable

    moved = _verdict(True, 3)
    moved.faithful = _faith(False, volume_ratio=4.0, bbox_ratio=0.1, max_dev_mm=99.0)
    assert deliverable(_result(True, moved), _verdict(True, 3)) == "ws"


def test_a_clean_input_is_never_replaced_and_an_unchanged_search_ships_nothing() -> None:
    from meshpipeline.cad.repair_v4.search import deliverable

    assert deliverable(_result(True, _verdict(True, 4)), _verdict(True, 4, clean=True)) is None
    assert deliverable(_result(False, _verdict(True, 4)), _verdict(True, 2)) is None


# ------------------------------------------------------------------ the fidelity numbers the report flags on

def test_the_faithful_kernel_reports_the_bbox_it_measured() -> None:
    """The gaming flag needs the repaired shape's extent, so the kernel returns it beside the ratios."""
    from meshpipeline.cad.repair_v4.verdict import faithful_kernel as fk

    assert "bbox_ratio" in fk.REPORTED and "diag_after_mm" in fk.REPORTED


def test_faithful_report_carries_the_bbox_ratio() -> None:
    from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport

    r = FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1)
    assert r.bbox_ratio is None            # not measured: None, never a fabricated 1.0
    r2 = FaithfulReport(True, 0.0, 0.1, 1.0, 0.5, 1, 1, bbox_ratio=0.5, diag_after_mm=50.0)
    assert r2.bbox_ratio == 0.5 and r2.diag_after_mm == 50.0


# ------------------------------------------------------------------ _clean_with, now mine (LEAD-2, 2026-11-24)

def test_clean_with_cannot_change_the_answer_under_the_current_definition() -> None:
    """The search replaces layer 3 with a declared-edits re-run and re-derives clean through the ONE rule. Since
    the rule no longer reads layer 3, that re-derivation is an identity: it is kept as the seam so a future change
    to the definition flows through `clean_of` alone, and this pins that it is inert today."""
    from dataclasses import replace as dc_replace

    from meshpipeline.cad.repair_v4 import search
    from meshpipeline.cad.repair_v4.verdict.assemble import Verdict

    for valid in (True, False):
        for passes, fails in ((2, 0), (1, 1), (0, 0)):
            panel = [_m() for _ in range(passes)] + [_m("fail") for _ in range(fails)]
            for tier in ("full", "cheap"):
                for inconclusive in (False, True):
                    v = Verdict(_valid(valid), panel, _faith(True), False, tier, inconclusive)
                    v = dc_replace(v, clean=assemble.clean_of(tier=tier, inconclusive=inconclusive,
                                                              valid=v.valid, panel=panel, faithful=v.faithful))
                    for report in (None, _faith(True), _faith(False, volume_ratio=9.0)):
                        assert search._clean_with(v, report) is v.clean, (valid, passes, tier, inconclusive)


def test_clean_with_no_longer_cites_the_decision_it_used_to_escalate() -> None:
    """Its docstring described the solid-less layer-3 deadlock as an OPEN owner decision and pointed at code that
    no longer exists. The decision is made; a stale comment is the next reader's bug."""
    from pathlib import Path

    from meshpipeline.cad.repair_v4 import search

    doc = search._clean_with.__doc__ or ""
    assert "OPEN OWNER DECISION" not in doc and "_SOLIDLESS_OK_REASONS" not in doc
    assert "2026-11-24" in doc
    assert "_SOLIDLESS_OK_REASONS" not in Path(search.__file__).read_text()
