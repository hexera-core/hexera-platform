# Responsibility: Verify a seed coordinate round-trips exactly across nine orders of magnitude, refusing non-finite.
from __future__ import annotations

import pytest

from meshpipeline.engines.vmtk.vmtk_runner import _seed_args, _seed_coord

#: spans from a capillary to a large structure, in metres
SCALES = [1e-9, 1e-6, 1e-3, 1.0, 1e3, 1e6]


@pytest.mark.parametrize("value", [
    0.0035769651152443363, -0.006803717487004138, 1.2345678901234e-7,
    1e-9, -1e-9, 1e6, 123456.789, 0.1, -0.0, 0.0,
])
def test_a_seed_coordinate_round_trips_exactly(value):
    assert float(_seed_coord(value)) == value


@pytest.mark.parametrize("scale", SCALES)
def test_endpoint_identity_survives_across_nine_orders_of_magnitude(scale):
    source = [0.13 * scale, -0.27 * scale, 0.41 * scale]
    target = [0.23 * scale, -0.17 * scale, 0.51 * scale]
    args = _seed_args({"source_points": source, "target_points": target,
                       "source_ids": [], "target_ids": []})
    got = [float(a) for a in args if a not in ("-seedselector", "pointlist",
                                               "-sourcepoints", "-targetpoints")]
    assert got[:3] == source
    assert got[3:] == target
    assert got[:3] != got[3:], "endpoints collapsed onto each other"


@pytest.mark.parametrize("scale", SCALES)
def test_the_old_fixed_format_would_have_lost_these(scale):
    value = 0.13 * scale
    lost = float(f"{value:.6f}") != value
    if scale <= 1e-6:
        assert lost, "this scale must be shown to break under the old format"
    assert float(_seed_coord(value)) == value


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_seeds_are_refused_rather_than_serialised(bad):
    with pytest.raises(ValueError, match="finite"):
        _seed_coord(bad)


def test_serialised_seeds_never_carry_a_locale_comma_or_a_non_number():
    for scale in SCALES:
        for v in (0.13 * scale, -0.27 * scale):
            s = _seed_coord(v)
            assert "," not in s
            assert "nan" not in s.lower() and "inf" not in s.lower()
            float(s)                                   # parses as a plain float


def test_the_pype_carries_the_seeds_verbatim():
    from meshpipeline.engines.vmtk.vmtk_runner import build_pype

    source = [1.2345678901234e-7, -2.5e-9, 3.0]
    target = [4.0, 5.5, -6.25e-8]
    argv = build_pype({"source_points": source, "target_points": target})
    for v in source + target:
        assert _seed_coord(v) in argv


def test_profile_id_seeding_is_untouched():
    args = _seed_args({"source_points": [], "target_points": [],
                       "source_ids": [0], "target_ids": [1, 2]})
    assert args == ["-seedselector", "profileidlist", "-sourceids", "0", "-targetids", "1", "2"]
