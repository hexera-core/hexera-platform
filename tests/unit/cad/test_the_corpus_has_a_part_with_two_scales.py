"""The corpus contains a part the scale splitter can actually fire on.

WHY THIS MATTERS. Of the ten parts in the corpus read on 2026-10-28, every one came back
single-scaled: `cad/scales.scales_of_field` found one mode in the width histogram and returned
None. So the scale split, the fine-surface writer, the two-scale budget, the prismatic-axis
measurement and the anisotropic stretch - everything built for two-scale work - had been tested
against geometry that cannot trigger any of it, plus one 47 GB customer file that takes 1.75
hours a run.

`devtools/semiconductor/geometry/two_scale_plate.py` closes that. This checks the corpus carries
the result, and that the thresholds the splitter applies are actually cleared by it, which is
arithmetic off the declared numbers rather than a 134-second field read.

MEASURED ON IT when it was built, 2026-10-29, at a 1.0 mm subdivision:

    narrowest passage   98.0 um      built as 98 um
    split at           244.1 um      trough share 0.002
    fine                98.0 um over 430.4 mm2
    broad              398.3 um over 709.8 mm2
    fine share         0.377         the customer plate is 0.359, the cap is 0.5
    prismatic axis     y at 0.0829   the customer plate is y at 0.0694
"""
import json
import math
from pathlib import Path

import pytest

from meshpipeline.cad import scales as SC

CORPUS = Path(__file__).resolve().parents[3] / "cad" / "semiconductor"
TWO_SCALE = ("two_scale_plate_sealed", "two_scale_plate_ported")


@pytest.fixture(scope="module")
def manifest():
    p = CORPUS / "manifest_two_scale.json"
    if not p.is_file():
        pytest.skip("the two-scale parts have not been exported into this checkout")
    return {e["name"]: e for e in json.loads(p.read_text())}


@pytest.fixture(scope="module")
def parts():
    p = CORPUS / "parts.json"
    if not p.is_file():
        pytest.skip("the corpus index has not been built in this checkout")
    return {e["name"]: e for e in json.loads(p.read_text())}


@pytest.mark.parametrize("name", TWO_SCALE)
def test_the_part_is_in_the_corpus_index(parts, name):
    assert name in parts, f"{name} is not in parts.json; run index_parts.py"
    assert parts[name]["narrowest_um"] == pytest.approx(98.0, abs=0.5)


@pytest.mark.parametrize("name", TWO_SCALE)
def test_its_two_scales_are_far_enough_apart_for_the_splitter(manifest, name):
    """`scales_of_field` needs the two modes at least MIN_OCTAVES_APART. Nothing else in the
    corpus comes close: every other part has one width."""
    e = manifest[name]
    octaves = math.log2(e["manifold_mm"] / (e["smallest_feature_um"] / 1000.0))
    assert octaves >= SC.MIN_OCTAVES_APART, (
        f"{octaves:.1f} octaves between {e['manifold_mm']} mm and "
        f"{e['smallest_feature_um']} um, and the splitter needs {SC.MIN_OCTAVES_APART}")
    assert e["scale_ratio"] == pytest.approx(21.4, abs=0.2), (
        "the ratio is the customer's own, and it is the thing this part stands in for")


@pytest.mark.parametrize("name", TWO_SCALE)
def test_the_slot_pitch_is_the_customers(manifest, name):
    """0.098 mm of coolant per 0.545 mm of pitch is the void fraction their field actually has,
    and the void fraction is what the cell budget is spent against."""
    e = manifest[name]
    assert e["pitch_mm"] == pytest.approx(0.545, abs=1e-6)
    void = (e["smallest_feature_um"] / 1000.0) / e["pitch_mm"]
    assert void == pytest.approx(0.180, abs=0.002)


def test_one_variant_is_sealed_and_one_is_plumbed(manifest):
    """THE DIFFERENCE THAT DECIDES SOLVABILITY, and the reason there are two.

    The customer's inlet and outlet are fitting bores that stop inside the metal, so their
    coolant is sealed and their delivered fluid region has exactly one patch. `sealed` has that
    deliberately, so the flowable gate has something true to refuse; `ported` runs the same bores
    out through the lid so there is something that can be flowed.
    """
    assert manifest["two_scale_plate_sealed"]["reached_faces"] == []
    assert manifest["two_scale_plate_ported"]["reached_faces"] == ["zmax"]
    assert manifest["two_scale_plate_sealed"]["ported"] is False
    assert manifest["two_scale_plate_ported"]["ported"] is True


def test_the_two_variants_differ_in_nothing_else(manifest):
    """A pair that differed in two things would prove nothing about either."""
    a, b = manifest["two_scale_plate_sealed"], manifest["two_scale_plate_ported"]
    for key in ("smallest_feature_um", "slots", "pitch_mm", "manifold_mm",
                "scale_ratio", "channel_height_mm"):
        assert a[key] == b[key], key
    # the ported one is larger by exactly the bores it runs out through the lid
    assert b["fluid_volume_mm3"] > a["fluid_volume_mm3"]
    assert b["fluid_volume_mm3"] - a["fluid_volume_mm3"] < 2.0


@pytest.mark.parametrize("name", TWO_SCALE)
def test_the_corpus_still_closes_on_the_block(parts, manifest, name):
    """The measured volumes and the built ones have to be the same part."""
    e, m = parts[name], manifest[name]
    assert e["fluid_volume_mm3"] == pytest.approx(m["fluid_volume_mm3"], rel=1e-4)
    assert e["solid_volume_mm3"] == pytest.approx(m["solid_volume_mm3"], rel=1e-4)
