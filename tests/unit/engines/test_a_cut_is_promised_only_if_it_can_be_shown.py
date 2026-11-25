"""A cut is promised only if its own pictures can discharge it.

Job 110daca9 delivered 279,281,786 cells, passed all eight gates, and reached no verdict. The
first cause was that no interior was exported at all. With that fixed the engine drew its three
cuts and the review still ended `reviewer_evidence_missing`, for a second and quieter reason:

    render adapter: 2 region(s) not offered - no volume artifact, and no prepared frame a cell is
    countable in, for across_the_flow, along_the_flow

    Reviewer: expected targets not produced ["expected region 'region:across_the_flow' was not
    produced", "expected region 'region:along_the_flow' was not produced"]

MEASURED through the engine's own `_review_aids`, re-aiming included, on that mesh:

    across_the_flow    normal x   zoom structure False,  0 repeats   20 placements tried
    through_the_plate  normal y   zoom structure True,  39 repeats    5 placements tried
    along_the_flow     normal z   zoom structure False,  1 repeat    20 placements tried

    _render_cuts: rendered 6, usable 1

The passages are prismatic along y, so a plane cut ALONG them shows slots and not a periodic
array: two of those three cuts can never hold one, however they are placed. Naming all three as
`exact_ids` demanded three be PRODUCED, and the adapter will not offer a region it has no
countable frame for - an obligation nothing could discharge, on a mesh with nothing wrong with it.

The pictures of the refused cuts still travel; only the obligation is not created.
"""
from __future__ import annotations

from meshpipeline.contracts.evidence_ledger import TargetKind
from meshpipeline.engines.registry import get_spec

SPEC = get_spec("cfmesh_conjugate")

#: The three cuts as the engine actually drew them on job 110daca9, with the figures above.
MEASURED_CUTS = [
    {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
     "frames": [{"scope": "whole", "um_per_pixel": 138.6719,
                 "content": {"shows_structure": False, "repeats": 2}},
                {"scope": "zoom", "um_per_pixel": 6.2895,
                 "content": {"shows_structure": False, "repeats": 0}}]},
    {"name": "through_the_plate", "kind": "slice", "normal": [0.0, 1.0, 0.0],
     "frames": [{"scope": "whole", "um_per_pixel": 138.6719,
                 "content": {"shows_structure": False, "repeats": 15}},
                {"scope": "zoom", "um_per_pixel": 6.2895,
                 "content": {"shows_structure": True, "repeats": 39}}]},
    {"name": "along_the_flow", "kind": "slice", "normal": [0.0, 0.0, 1.0],
     "frames": [{"scope": "whole", "um_per_pixel": 105.2083,
                 "content": {"shows_structure": False, "repeats": 4}},
                {"scope": "zoom", "um_per_pixel": 6.2895,
                 "content": {"shows_structure": False, "repeats": 1}}]},
]

REGION_ROWS = [{"name": "fluid", "type": "fluid", "cells": 87194075},
               {"name": "solid", "type": "solid", "cells": 192087711}]


def _manifest(cuts):
    return {"mesh_mode": "cfmesh_conjugate",
            "quality": {"regions": REGION_ROWS, "cells": 279281786},
            "patches": {"wall": [], "mouth_2": [], "mouth_3": []},
            "inspection_regions": cuts}


def _regions(manifest):
    obs = SPEC.expected_target_obligations(manifest, {}, "conjugate_heat_transfer")
    return [o for o in obs if o.kind is TargetKind.REGION]


def test_only_the_cut_that_holds_the_array_is_demanded():
    """The measured case: three drawn, one usable, one named."""
    (region,) = _regions(_manifest(MEASURED_CUTS))
    assert region.exact_ids == ("region:through_the_plate",), region.exact_ids


def test_a_cut_its_own_producer_refused_is_not_demanded():
    """`render_adapter` will not offer it, so demanding it can only end in no verdict."""
    (region,) = _regions(_manifest(MEASURED_CUTS))
    assert "region:across_the_flow" not in region.exact_ids
    assert "region:along_the_flow" not in region.exact_ids


def test_every_cut_demanded_is_one_the_adapter_could_offer():
    """The property, stated once: nothing is named that the producer refused outright."""
    (region,) = _regions(_manifest(MEASURED_CUTS))
    refused = {f"region:{c['name']}" for c in MEASURED_CUTS
               if not any((f.get("content") or {}).get("shows_structure")
                          for f in c["frames"])}
    assert not (set(region.exact_ids) & refused), refused


def test_all_three_still_travel_with_their_pictures():
    """The obligation is filtered; `inspection_regions` is not. A reader still sees what was
    drawn and the figure that refused it - which native.py calls strictly stronger than hiding
    it, and this change must not undo."""
    man = _manifest(MEASURED_CUTS)
    _regions(man)
    assert [r["name"] for r in man["inspection_regions"]] == \
        ["across_the_flow", "through_the_plate", "along_the_flow"]
    assert all(f.get("image") is None for r in man["inspection_regions"]
               for f in r["frames"])  # none were added or removed


def test_when_every_cut_is_refused_it_falls_back_to_the_floor():
    """Naming none is not promising none: the kind-only floor still asks for one interior."""
    refused = [c for c in MEASURED_CUTS if c["name"] != "through_the_plate"]
    (region,) = _regions(_manifest(refused))
    assert region.exact_ids == ()
    assert region.required_count() == 1
    assert "coolant" in region.provenance


def test_a_cut_with_no_frames_is_named_as_it_always_was():
    """The older shape: geometry-derived slices with no pictures beside them. Nothing refused
    them, and they are only published when there is a volume at home to cut them from."""
    plain = [{"name": "across_the_board", "kind": "slice", "normal": [1.0, 0.0, 0.0]},
             {"name": "through_the_hot_part", "kind": "slice", "normal": [0.0, 1.0, 0.0]}]
    (region,) = _regions(_manifest(plain))
    assert region.exact_ids == ("region:across_the_board", "region:through_the_hot_part")


def test_a_frame_with_no_content_block_does_not_veto_its_cut():
    """A frame that carries no measurement has refused nothing. Only a measured False vetoes."""
    one = [{"name": "across_the_board", "frames": [{"scope": "whole"}]},
           {"name": "refused", "frames": [{"scope": "zoom",
                                           "content": {"shows_structure": False}}]}]
    (region,) = _regions(_manifest(one))
    assert region.exact_ids == ("region:across_the_board",)


def test_the_patch_obligation_is_untouched():
    """This change is about cuts. The two delivered regions' boundaries are demanded as before."""
    obs = SPEC.expected_target_obligations(_manifest(MEASURED_CUTS), {},
                                           "conjugate_heat_transfer")
    (patch,) = [o for o in obs if o.kind is TargetKind.PATCH]
    assert patch.exact_ids == ("patch:fluid", "patch:solid")
    assert patch.inspect_at_least >= 1
