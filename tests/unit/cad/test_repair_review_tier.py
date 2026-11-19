# Responsibility: Pin the tier decision - which packages the auto-accept tier may take, and which
#                 it must hand to the model reviewer.
# Boundaries: pure. No kernel, no model, no clock, no network: `review_tier.decide` reads numbers
#             and sets and answers.
"""Every threshold assertion here is a TABLE across several values in BOTH directions. A one-sided
assertion (`auto` for a small deviation only) silently passes a mutated constant, and has already
done so on this plan: the boundary and the value just past it must both be stated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import package_review as pr
from meshpipeline.cad.repair import review_tier as rt


@dataclass(frozen=True)
class _V:
    """Just enough of an OpVerdict for the tier: what it was, and whether it survived."""

    op: str = "fill_planar_holes"
    kept: bool = True
    revealed: frozenset = frozenset()


@dataclass(frozen=True)
class _M:
    """Just enough of a Measure for `declaration_covers`."""

    surface_area_mm2: float = 1000.0
    volume_mm3: float = 1000.0


def _measured(mm: float) -> dict:
    return {"deviation_mm": mm, "deviation_samples": 12, "deviation_faces": 3}


def _decide(monkeypatch, *, posture: str = "live", verdicts=(_V(),),
            reclassified=frozenset(), revealed=frozenset(), declared_matches: bool = True,
            deviation_mm: float = 0.01, status: str = pr.DEVIATION_MEASURED,
            threshold_mm: float | None = 0.05) -> tuple[str, str]:
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", posture, raising=False)
    return rt.decide(verdicts=list(verdicts), reclassified=reclassified, revealed=revealed,
                     declared_matches=declared_matches, deviation=_measured(deviation_mm),
                     deviation_status=status, threshold_mm=threshold_mm)


# THE HAPPY PATH, AND THE THRESHOLD AS A TABLE


def test_small_accounted_change_auto_accepts(monkeypatch):
    tier, reason = _decide(monkeypatch, deviation_mm=0.01, threshold_mm=0.05)
    assert tier == "auto", reason
    assert "0.01" in reason and "0.05" in reason


def test_the_threshold_is_a_boundary_in_both_directions(monkeypatch):
    # A TABLE, not a single small value: a mutated comparison (<= to <, or the threshold scaled)
    # changes at least one of these rows.
    table = (
        (0.0, 0.05, "auto"),      # no measurable deviation at all
        (0.0499, 0.05, "auto"),
        (0.05, 0.05, "auto"),     # exactly at the threshold is WITHIN it
        (0.0501, 0.05, "reviewer"),
        (0.5, 0.05, "reviewer"),
        (50.0, 0.05, "reviewer"),
        (0.5, 1.0, "auto"),       # the same deviation, a looser customer
        (1.0, 1.0, "auto"),
        (1.0001, 1.0, "reviewer"),
    )
    for measured, threshold, expected in table:
        tier, reason = _decide(monkeypatch, deviation_mm=measured, threshold_mm=threshold)
        assert tier == expected, (measured, threshold, tier, reason)


def test_a_non_finite_deviation_goes_to_a_person(monkeypatch):
    for value in (math.nan, math.inf, -math.inf):
        tier, reason = _decide(monkeypatch, deviation_mm=value, threshold_mm=1e9)
        assert tier == "reviewer", (value, reason)


# THE VETOES, EACH ALONE


def test_no_threshold_means_no_auto_accept(monkeypatch):
    # INERT WITHOUT A CUSTOMER THRESHOLD - and a zero or negative one is not a threshold either.
    for threshold in (None, 0.0, -0.0, -1.0, math.nan):
        tier, reason = _decide(monkeypatch, threshold_mm=threshold)
        assert tier == "reviewer", (threshold, reason)
    assert _decide(monkeypatch, threshold_mm=1e-9, deviation_mm=0.0)[0] == "auto"


def test_the_posture_fails_closed(monkeypatch):
    for posture in ("off", "", "Live", "live ", "on", "shadow", "true", None):
        tier, reason = _decide(monkeypatch, posture=posture)
        assert tier == "reviewer", (posture, reason)
    assert _decide(monkeypatch, posture="live")[0] == "auto"


def test_vertex_moving_ops_always_go_to_the_reviewer(monkeypatch):
    assert rt.VERTEX_MOVING_OPS == frozenset({"close_wire_gaps", "rebuild_face"})
    for op in sorted(rt.VERTEX_MOVING_OPS):
        # AT ANY DEVIATION AND UNDER ANY THRESHOLD, including a zero deviation and a huge
        # threshold - the one combination every other veto would wave through.
        for measured, threshold in ((0.0, 1e9), (0.001, 0.05), (0.04, 0.05)):
            tier, reason = _decide(monkeypatch, verdicts=(_V(op=op),),
                                   deviation_mm=measured, threshold_mm=threshold)
            assert tier == "reviewer", (op, measured, threshold)
            assert op in reason
        # ... and beside a harmless op in the same package, it still vetoes the package
        tier, _ = _decide(monkeypatch, verdicts=(_V(), _V(op=op)), deviation_mm=0.0)
        assert tier == "reviewer", op
    # A ROLLED-BACK vertex move added nothing to the shape, so it vetoes nothing.
    assert _decide(monkeypatch, verdicts=(_V(), _V(op="rebuild_face", kept=False)))[0] == "auto"


def test_unmeasured_deviation_goes_to_the_reviewer(monkeypatch):
    for status in (pr.DEVIATION_PROBE_OFF, pr.DEVIATION_NO_TIME, pr.DEVIATION_FAILED,
                   pr.DEVIATION_NO_FACES, "", "measured ", "Measured"):
        tier, reason = _decide(monkeypatch, status=status, deviation_mm=0.0)
        assert tier == "reviewer", (status, reason)
    assert _decide(monkeypatch, status=pr.DEVIATION_MEASURED)[0] == "auto"


def test_a_measured_status_with_no_value_goes_to_the_reviewer(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", "live", raising=False)
    tier, reason = rt.decide(verdicts=[_V()], reclassified=frozenset(), revealed=frozenset(),
                             declared_matches=True, deviation={},
                             deviation_status=pr.DEVIATION_MEASURED, threshold_mm=0.05)
    assert tier == "reviewer" and "no value" in reason


def test_revealed_or_reclassified_goes_to_the_reviewer(monkeypatch):
    assert _decide(monkeypatch, reclassified=frozenset({"edge:a"}))[0] == "reviewer"
    assert _decide(monkeypatch, revealed=frozenset({("face:b", "mesh_overlapping_facets")}))[0] \
        == "reviewer"
    assert _decide(monkeypatch, reclassified=frozenset({"edge:a"}),
                   revealed=frozenset({("face:b", "x")}))[0] == "reviewer"
    assert _decide(monkeypatch)[0] == "auto"


def test_declaration_mismatch_goes_to_the_reviewer(monkeypatch):
    assert _decide(monkeypatch, declared_matches=False)[0] == "reviewer"
    assert _decide(monkeypatch, declared_matches=True)[0] == "auto"


def test_the_reason_names_the_first_failed_condition(monkeypatch):
    tier, reason = _decide(monkeypatch, verdicts=(_V(op="rebuild_face"),),
                           reclassified=frozenset({"edge:a"}), declared_matches=False,
                           status=pr.DEVIATION_FAILED)
    assert tier == "reviewer" and "moves vertices" in reason


def test_it_never_raises(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", "live", raising=False)
    for bad in ({"deviation_mm": "not a number"}, {"deviation_mm": object()}):
        tier, reason = rt.decide(verdicts=[_V()], reclassified=frozenset(),
                                 revealed=frozenset(), declared_matches=True, deviation=bad,
                                 deviation_status=pr.DEVIATION_MEASURED, threshold_mm=0.05)
        assert tier == "reviewer", reason
    # A verdict object that is nothing like an OpVerdict is still an answer, not a crash.
    assert rt.decide(verdicts=[object()], reclassified=frozenset(), revealed=frozenset(),
                     declared_matches=True, deviation=_measured(0.0),
                     deviation_status=pr.DEVIATION_MEASURED, threshold_mm=0.05)[0] == "auto"


# THE DECLARATION COMPARISON IS THE CAPS', NOT A SECOND COPY


def test_declaration_coverage_uses_the_caps_residuals(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.05, raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.05, raising=False)
    before = _M(surface_area_mm2=1000.0, volume_mm3=1000.0)
    # declared, measured-after, covered?  - a table on BOTH sides of the 5% residual
    table = (
        (0.0, 1000.0, True),
        (10.0, 1010.0, True),      # declared exactly what moved
        (0.0, 1049.0, True),       # 4.9% unexplained, inside the residual
        (0.0, 1051.0, False),      # 5.1% unexplained, outside it
        (50.0, 1051.0, True),      # the same measurement, declared
        (0.0, 800.0, False),       # a large loss nobody declared
        (-200.0, 800.0, True),     # the same loss, declared
    )
    for declared, after_area, covered in table:
        assert rt.declaration_covers(before=before, after=_M(surface_area_mm2=after_area),
                                     declared_area_mm2=declared,
                                     declared_volume_mm3=0.0) is covered, (declared, after_area)
    for declared, after_volume, covered in table:
        assert rt.declaration_covers(before=before, after=_M(volume_mm3=after_volume),
                                     declared_area_mm2=0.0,
                                     declared_volume_mm3=declared) is covered, (declared,
                                                                                after_volume)


def test_an_unreadable_measurement_is_not_covered():
    assert rt.declaration_covers(before=_M(), after=_M(surface_area_mm2=math.nan),
                                 declared_area_mm2=0.0, declared_volume_mm3=0.0) is False
    assert rt.declaration_covers(before=None, after=_M(), declared_area_mm2=0.0,
                                 declared_volume_mm3=0.0) is False


# THE POSTURE AND ITS SETTING


def test_the_setting_defaults_off_and_a_typo_reads_off(monkeypatch, caplog):
    import logging
    assert rcfg.CAD_REPAIR_AUTO_ACCEPT == "off"
    for raw, want in (("live", "live"), ("off", "off"), ("Live", "off"), ("on", "off"),
                      ("shadow", "off"), ("true", "off")):
        monkeypatch.setenv("CAD_REPAIR_AUTO_ACCEPT", raw)
        with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
            caplog.clear()
            assert rcfg._auto_accept_posture() == want, raw
        warned = any("CAD_REPAIR_AUTO_ACCEPT" in r.getMessage() for r in caplog.records)
        assert warned is (raw not in ("live", "off")), raw


def test_auto_accept_without_a_review_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        caplog.clear()
        rcfg._warn_inert_auto_accept("live", "off")
        assert any("never consulted" in r.getMessage() for r in caplog.records)
        caplog.clear()
        rcfg._warn_inert_auto_accept("live", "live")
        rcfg._warn_inert_auto_accept("off", "off")
        assert not caplog.records


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
