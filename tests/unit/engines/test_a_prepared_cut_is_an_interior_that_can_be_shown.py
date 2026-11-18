# Responsibility: Verify the region obligation is waived only when the interior truly cannot be shown.
# Boundaries: engines/base.expected_target_obligations, read from the manifest's CLAIM and no file.
from __future__ import annotations

from pathlib import Path

from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.registry import ENGINE_CATALOG

REGIONS = [{"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
            "origin": [0.027, 0.041, 0.042]}]
CUT = {"scope": "zoom", "image": "review/cuts/across_zoom.png",
       "window_m": [0.0080505, 0.0060379], "metres_per_pixel": 6.2894e-06}


def _manifest(*, volume=False, cuts=False) -> dict:
    regions = [dict(REGIONS[0])]
    if cuts:
        regions[0]["frames"] = [dict(CUT)]
    return {"mesh_units": "m",
            "mesh_paths": {"surface": "mesh.msh", **({"volume": "internal.vtu"} if volume
                                                     else {})},
            "inspection_regions": regions}


def _kinds(engine: str, manifest: dict) -> set:
    return {o.kind for o in ENGINE_CATALOG[engine].expected_target_obligations(manifest, {}, "")}


def test_the_waiver_still_applies_when_there_is_nothing_to_look_inside_with():
    """Unchanged, and load-bearing: demanding a region nobody can show ended the review before
    the model was asked (EXPECTED_TARGET_MISSING) on a mesh that had passed every gate."""
    assert TargetKind.REGION not in _kinds("snappy", _manifest())


def test_a_claimed_cut_keeps_the_region_obligation_without_a_volume():
    """THE WAIVER'S PREMISE IS THAT A REGION IS A CUT THROUGH A VOLUME, so no volume means
    nothing to look at. That is false once the cut has been taken where the mesh was built and
    the frames came home instead of the cells: the region IS inspectable, and waiving it would
    stop anything downstream from asking to see inside a mesh whose inside is right there."""
    assert TargetKind.REGION in _kinds("snappy", _manifest(cuts=True))


def test_a_volume_claim_keeps_it_as_it_always_did():
    assert TargetKind.REGION in _kinds("snappy", _manifest(volume=True))


def test_the_conjugate_engine_never_waives_either_way():
    """review_requires_the_interior stays True: a conformal split and one merged block agree on
    every number the gates print and differ only in a cut through the coolant, so a run with no
    interior has produced nothing this review can judge - stated as missing evidence rather than
    waived into a pass."""
    spec = ENGINE_CATALOG["cfmesh_conjugate"]
    assert spec.review_requires_the_interior is True
    for man in (_manifest(), _manifest(cuts=True), _manifest(volume=True)):
        assert TargetKind.REGION in _kinds("cfmesh_conjugate", man)


def test_a_claim_is_a_claim_even_when_the_file_is_not_there():
    """THE CLAIM AND THE FILE ARE TWO AUTHORITIES, and this one is the claim. It runs in the API
    process, where there is no workspace to look in: the frame named here exists nowhere on this
    machine and the obligation still stands, for the render session to meet or for the review to
    refuse as missing evidence. (The dangerous half is the other one - waiving the obligation
    means nothing downstream ever asks to see inside the mesh at all.)"""
    man = _manifest(cuts=True)
    assert not Path(man["inspection_regions"][0]["frames"][0]["image"]).exists()
    assert TargetKind.REGION in _kinds("snappy", man)


def test_a_cut_entry_with_no_file_named_is_not_a_claim():
    man = _manifest(cuts=True)
    del man["inspection_regions"][0]["frames"][0]["image"]
    assert TargetKind.REGION not in _kinds("snappy", man)
