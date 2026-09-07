# Responsibility: Verify the intake's declared-area rule understands an annular opening stated as
# bore diameter plus centre-body diameter (the model states the two numbers, never the ring's area).
from __future__ import annotations

import math

import pytest

from meshpipeline.agents.intake.validation import _declared_area_mm2


def test_bore_plus_centre_body_is_the_ring_area():
    a = _declared_area_mm2({"diameter_mm": 453.3, "inner_diameter_mm": 268.88})
    assert a == pytest.approx(math.pi * ((453.3 / 2) ** 2 - (268.88 / 2) ** 2), rel=1e-9)


def test_without_a_centre_body_the_bore_is_a_disc():
    assert _declared_area_mm2({"diameter_mm": 453.3}) == pytest.approx(
        math.pi * (453.3 / 2) ** 2, rel=1e-9)


def test_a_centre_body_that_is_not_inside_the_bore_is_ignored():
    disc = math.pi * (453.3 / 2) ** 2
    for bad in (0, -1, 453.3, 900, True, "268.88"):
        assert _declared_area_mm2({"diameter_mm": 453.3, "inner_diameter_mm": bad}) == \
            pytest.approx(disc, rel=1e-9)


def test_the_canonical_payload_carries_the_centre_body():
    from meshpipeline.agents.intake import admission_token as at

    src = (at.__file__ and open(at.__file__, encoding="utf-8").read()) or ""
    assert '"inner_diameter_mm": p.get("inner_diameter_mm")' in src, (
        "the admission token must carry inner_diameter_mm or the declaration loses the ring")
