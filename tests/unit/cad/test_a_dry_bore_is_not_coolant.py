"""A hole drilled through dry metal is not a coolant passage, however big the bracket.

WHY THIS TEST EXISTS. The only thing standing between a two-mouthed dry bore and the coolant total
was MIN_PASSAGE_SHARE, which is a share of the VOLUME - and a clamping bore's volume grows with the
bracket it is drilled in, so on a big enough bracket it clears any share bar. Measured 2026-10-28:
the buried_mouths test part read 6,623.599 mm3 against 6,241.896 mm3 declared, and the whole
381.703 mm3 excess was one 9 mm clamping bore at 6.1% of the coolant, three times the 2% bar. The
same mechanism was worse on a committed fixture: a Class 1500 spool's sixteen bolt holes came to
32.7% of its bore.

NO SHARE BAR SEPARATES THEM, which is why the fix is a different axis and not a bigger number: a
real second passage in a customer assembly is 2.9% of its coolant and the spool's bolt holes are
2.045%. What separates them is SHAPE - a duct is longer than it is wide and a bore is not.

WHY THE EXISTING TESTS DID NOT CATCH IT. test_one_solid_coolant.py's bore test covers a ONE-mouthed
bore, which never reaches this code: it is refused earlier, at MIN_PASSAGE_MOUTHS. Nothing anywhere
exercised the share rule with a two-mouthed dry bore big enough to clear 2%.
"""
import pytest

from meshpipeline.cad import fluid_volume as fv


class _Port:
    """Just the two numbers _is_duct reads off a mouth."""

    def __init__(self, hole, diameter):
        self.hole = hole
        self.diameter = diameter


class _Passage:
    def __init__(self, volume, ports):
        self.volume = volume
        self.ports = ports


def _round_hole(diameter_m, length_m):
    """A straight round through-hole: two mouths of the same bore, that far apart."""
    import math

    area = math.pi * (diameter_m / 2.0) ** 2
    return _Passage(area * length_m, [_Port(area, diameter_m), _Port(area, diameter_m)])


def test_a_bore_shorter_than_it_is_wide_is_not_a_duct():
    """The defect, at the size it was measured: 9 mm across, 6 mm of metal."""
    assert not fv._is_duct(_round_hole(0.009, 0.006))


def test_a_channel_longer_than_it_is_wide_is_a_duct():
    """A real coolant channel. The nearest real passage measured 3.31 on this scale."""
    assert fv._is_duct(_round_hole(0.002, 0.040))


@pytest.mark.parametrize("ratio, duct", [
    (0.5, False),    # half as long as wide
    (1.0, False),    # as long as wide - a bore, and the bar sits here
    (8.0, True),     # the shortest thing any real part in the set measured
    (100.0, True),   # a microchannel
])
def test_the_bar_sits_between_the_two_kinds_of_hole(ratio, duct):
    """One bar fits because the two kinds are a factor of five apart at the nearest: the bluntest
    real passage measured 3.31 and the sharpest dry bore 0.68."""
    assert fv._is_duct(_round_hole(0.004, 0.004 * ratio)) is duct


def test_a_passage_nothing_could_be_measured_on_is_kept():
    """Saying nothing is the safe answer: this rule only ever DROPS a passage, so a failed reading
    must not be allowed to lose real coolant."""
    assert fv._is_duct(_Passage(1e-6, []))
    assert fv._is_duct(_Passage(1e-6, [_Port(0.0, 0.0)]))


def test_the_rule_is_declared_rather_than_inlined():
    """The bar is a named constant with its measurements beside it, not a literal in a condition."""
    assert fv.MIN_PASSAGE_SLENDERNESS == 1.0
