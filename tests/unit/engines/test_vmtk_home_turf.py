"""VMTK's home turf reads the census facts of real lab shapes the way the VMTK-TUBULAR sweep found
them: tubes, trees and annuli at home; chambers, stubs and long thin gaps weak; a gap too thin for
the compute limit, a far field, or a passage with one opening outside."""
from __future__ import annotations

import pytest

from meshpipeline.engines.home_turf import verdict
from meshpipeline.engines.registry import ENGINE_CATALOG

VMTK = ENGINE_CATALOG["vmtk"]


@pytest.mark.parametrize("facts,expect", [
    # census values of real shapes (overnight/recommend/census/home_turf_facts.json, v2)
    ({"flow": "internal", "ports": 2, "slenderness": 8.63, "gap_vs_port": 0.996, "neck": 0.755},
     "home"),                                                  # bend_elbow_021
    ({"flow": "internal", "ports": 10, "slenderness": 25.0, "gap_vs_port": 0.775, "neck": 0.298},
     "home"),                                                  # the Fluent aorta
    ({"flow": "internal", "ports": 4, "slenderness": 4.0, "gap_vs_port": 0.997, "neck": 0.284},
     "home"),                                                  # manifold_003
    ({"flow": "internal", "ports": 4, "slenderness": 1.18, "gap_vs_port": 3.88, "neck": 0.208},
     "weak"),                                                  # mini_housing_001: a chamber
    ({"flow": "internal", "ports": 2, "slenderness": 162.3, "gap_vs_port": 0.578, "neck": 1.0},
     "weak"),                                                  # bend_elbow_012: thin and long
    ({"flow": "internal", "ports": 2, "slenderness": 1333.0, "gap_vs_port": 0.498, "neck": 1.0},
     "outside"),                                               # annular_001: 13 mm gap, 820 mm
    ({"flow": "external", "ports": 0}, "outside"),
    ({"flow": "internal", "ports": 1, "slenderness": 5.0}, "outside"),
])
def test_vmtk_turf_on_census_shapes(facts, expect):
    v = verdict(VMTK, facts["flow"], facts)
    assert v.severity == expect, (facts, v)
    assert v.reasons, "every verdict says why"


def test_an_unmeasured_case_claims_nothing():
    v = verdict(VMTK, "internal", {"flow": "internal", "ports": 3})
    assert v.severity == "home" and "slenderness" in v.unmeasured
