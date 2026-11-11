from __future__ import annotations

from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.package import Predicate, evaluate, specificity

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _changed(**fields) -> Measure:
    return Measure(**{**_SOUND.to_dict(), **fields})


def _p(cleared=(), reclassified=(), unchanged=(), area=10.0, volume=0.0) -> Predicate:
    return Predicate(cleared=frozenset(cleared), reclassified=frozenset(reclassified),
                     unchanged=frozenset(unchanged), max_area_delta_mm2=area,
                     max_volume_delta_mm3=volume)


def test_predicate_holds_when_named_defects_cleared_and_others_untouched():
    p = _p(cleared={"edge:a"}, unchanged={"face:b"})
    assert evaluate(p, before=_SOUND, after=_changed(surface_area_mm2=1005.0),
                    defects_before=("edge:a", "face:b"), defects_after=("face:b",)) == ""


def test_predicate_fails_when_a_cleared_defect_remains():
    p = _p(cleared={"edge:a"})
    assert "edge:a" in evaluate(p, before=_SOUND, after=_SOUND,
                                defects_before=("edge:a",), defects_after=("edge:a",))


def test_predicate_fails_when_an_unchanged_defect_disappeared():
    # collateral damage: the author said it would not touch this, and it did
    p = _p(unchanged={"face:b"})
    assert "face:b" in evaluate(p, before=_SOUND, after=_SOUND,
                                defects_before=("face:b",), defects_after=())


def test_predicate_fails_when_area_moved_past_the_claim():
    p = _p(cleared={"edge:a"}, area=1.0)
    assert "area" in evaluate(p, before=_SOUND, after=_changed(surface_area_mm2=1100.0),
                              defects_before=("edge:a",), defects_after=())


def test_predicate_fails_when_area_shrank_past_the_claim():
    p = _p(cleared={"edge:a"}, area=1.0)
    assert "area" in evaluate(p, before=_SOUND, after=_changed(surface_area_mm2=900.0),
                              defects_before=("edge:a",), defects_after=())


def test_predicate_fails_when_volume_moved_past_the_claim():
    p = _p(cleared={"edge:a"}, volume=1.0)
    msg = evaluate(p, before=_SOUND, after=_changed(volume_mm3=8100.0),
                   defects_before=("edge:a",), defects_after=())
    assert "volume" in msg and "area" not in msg


def test_a_delta_exactly_at_the_claim_holds():
    p = _p(cleared={"edge:a"}, area=5.0, volume=100.0)
    assert evaluate(p, before=_SOUND,
                    after=_changed(surface_area_mm2=1005.0, volume_mm3=8100.0),
                    defects_before=("edge:a",), defects_after=()) == ""


def test_reclassified_defect_may_remain_and_still_hold():
    p = _p(reclassified={"face:b"}, area=0.0)
    assert evaluate(p, before=_SOUND, after=_SOUND,
                    defects_before=("face:b",), defects_after=("face:b",)) == ""


def test_reclassified_defect_may_also_disappear_and_still_hold():
    p = _p(reclassified={"face:b"}, area=0.0)
    assert evaluate(p, before=_SOUND, after=_SOUND,
                    defects_before=("face:b",), defects_after=()) == ""


def test_reclassifying_a_defect_that_was_never_there_fails():
    # a discharge for a defect nobody found is a free pass, not a judgement
    p = _p(reclassified={"face:ghost"})
    assert "face:ghost" in evaluate(p, before=_SOUND, after=_SOUND,
                                    defects_before=("edge:a",), defects_after=("edge:a",))


def test_first_failure_wins_in_the_fixed_order():
    p = _p(cleared={"edge:a"}, unchanged={"face:b"}, reclassified={"face:ghost"},
           area=0.0, volume=0.0)
    bad = _changed(surface_area_mm2=2000.0, volume_mm3=1.0)
    kw = {"before": _SOUND, "after": bad, "defects_before": ("edge:a", "face:b"),
          "defects_after": ("edge:a",)}
    assert "edge:a" in evaluate(p, **kw)                      # cleared first
    p2 = _p(unchanged={"face:b"}, reclassified={"face:ghost"}, area=0.0)
    assert "face:b" in evaluate(p2, **kw)                     # then unchanged
    p3 = _p(reclassified={"face:ghost"}, area=0.0)
    assert "face:ghost" in evaluate(p3, **kw)                 # then reclassified
    p4 = _p(area=0.0, volume=0.0)
    assert "area" in evaluate(p4, **kw)                       # then area, before volume


def test_message_is_deterministic_across_several_failures():
    p = _p(cleared={"b", "a", "c"})
    kw = {"before": _SOUND, "after": _SOUND, "defects_before": ("a", "b", "c"),
          "defects_after": ("c", "b", "a")}
    assert evaluate(p, **kw) == "cleared defects still present: a, b, c"


def test_vacuous_predicate_scores_zero_specificity():
    assert specificity(Predicate(frozenset(), frozenset(), frozenset(), 1e9, 1e9)) == 0.0


def test_specificity_rises_with_entities_committed_to():
    one = Predicate(frozenset({"a"}), frozenset(), frozenset(), 1.0, 0.0)
    two = Predicate(frozenset({"a"}), frozenset(), frozenset({"b"}), 1.0, 0.0)
    assert specificity(two) > specificity(one) > 0.0
