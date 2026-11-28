# Responsibility: a region the engine obliges is offered, or the volume that covers for it is kept.
# Boundaries: `render_adapter.expand_region_targets` and `review_surface.review_volume`.
# Collaborates with: cfmesh_conjugate/native._drop_cut_volume, which may delete that volume.
"""Why four delivered meshes were never judged, measured off job 40e4e578's own workspace.

THE RECORD. 40e4e578, 606af55e, b8f09ae9 and 110daca9 each came back `succeeded` with
`delivered_types` mesh, mesh_bundle, repair_report, viewer_data - and each carried

    {"kind": "review_inconclusive", "marker": "reviewer_evidence_missing", "reruns": 0}

Four meshes shipped that nobody judged.

WHAT IT WAS NOT. Replaying 40e4e578's saved `mesh_manifest.json` through the production path:
`validate_plan` returns ok with no problems, and `deterministic_evidence_complete` returns
complete with MISSING `[]` - all 8 gating metrics present, all 9 advisory ones measured, no
tolerated gaps. The deterministic evidence gate at visual.py:320 never fired.

WHAT IT WAS. `expand_region_targets`. The engine obliges exactly two regions for this purpose,
`region:across_the_flow` and `region:through_the_plate`, and a region is offered only when the
volume is available OR one of its own frames can count cells. The measured frames:

    across_the_flow   whole  counts_cells False   (138.67 um/px: 0.18 px across a 25.16 um cell)
    across_the_flow   zoom   counts_cells False   resolves cells, but NO METAL between passages
    through_the_plate whole  counts_cells False   (same sub-pixel reason)
    through_the_plate zoom   counts_cells True
    along_the_flow    whole  counts_cells False
    along_the_flow    zoom   counts_cells False   (window too narrow for the pitch)

`across_the_flow`'s zoom is the interesting one. In the engine's own words: "the NARROWEST metal
between two passages is 0.0 px - 0.00 cell(s) ... No column of this frame along that axis is bare,
so there is no metal in it at all - this is a field of coolant, whatever its section does." A
picture with no wall in it cannot be a picture of a wall, so it cannot discharge the region.

AND THE VOLUME THAT WOULD HAVE COVERED FOR IT WAS DELETED. The manifest names
`quality["review_volume"] = "VTK/fluid/decomposed_0/internal.vtu"`; the `.vtm` beside it still
points at that file; the directory is EMPTY. So `review_volume()` returned None,
`volume_available` was False, `region:across_the_flow` was never offered, and visual.py refused
with [EXPECTED_TARGET_MISSING] before the model was asked anything.

The arithmetic, reproduced below: with the volume, nothing is absent. Without it,
`region:across_the_flow` is.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.sandbox import render_adapter as RA

PURPOSE = "count cells across a passage"

#: The measured `counts_cells` of every frame job 40e4e578 published, in publication order.
MEASURED: dict[str, tuple[bool, ...]] = {
    "across_the_flow": (False, False),
    "through_the_plate": (False, True),
    "along_the_flow": (False, False),
}
#: What `spec.expected_target_obligations` demands for conjugate_heat_transfer on this part.
OBLIGED = ("region:across_the_flow", "region:through_the_plate")


class _Cut:
    def __init__(self, counts: bool):
        self.counts_cells = counts


class _Region:
    def __init__(self, rid: str):
        self.region_id = rid


def offered(volume: bool, measured=MEASURED) -> set[str]:
    regions = {n: _Region(n) for n in measured}
    cuts = {n: tuple(_Cut(c) for c in flags) for n, flags in measured.items()}
    got = RA.expand_region_targets(regions, purpose=PURPOSE, required=True,
                                   volume_available=volume, cuts=cuts)
    return {t.target_id for t in got}


# ------------------------------------------------------------------ the refusal, reproduced

def test_without_its_volume_the_obliged_region_is_absent():
    """THE REFUSAL. This is what [EXPECTED_TARGET_MISSING] fired on."""
    got = offered(volume=False)
    assert got == {"region:through_the_plate"}
    absent = sorted(set(OBLIGED) - got)
    assert absent == ["region:across_the_flow"]


def test_with_its_volume_nothing_is_absent():
    """AND THE FIX. `_reduce_to_review_slabs` puts a slab back at the same path, so
    `review_volume` finds a file and this is the branch that runs."""
    got = offered(volume=True)
    assert set(OBLIGED) <= got
    assert sorted(set(OBLIGED) - got) == []


def test_the_region_that_could_count_cells_never_needed_the_volume():
    """`through_the_plate` is offered either way - the decision is per region, not per job."""
    assert "region:through_the_plate" in offered(volume=False)
    assert "region:through_the_plate" in offered(volume=True)


def test_a_region_whose_frames_all_fail_is_offered_only_on_the_volume():
    assert "region:along_the_flow" not in offered(volume=False)
    assert "region:along_the_flow" in offered(volume=True)


def test_one_countable_frame_anywhere_in_a_region_is_enough():
    """`through_the_plate` had a useless whole frame and a good zoom, and that is sufficient:
    the rule is `any`, over the region's frames."""
    assert offered(volume=False, measured={"r": (False, True)}) == {"region:r"}
    assert offered(volume=False, measured={"r": (True, False)}) == {"region:r"}
    assert offered(volume=False, measured={"r": (False, False)}) == set()


def test_a_region_with_no_cuts_at_all_is_offered_only_on_the_volume():
    assert offered(volume=False, measured={"r": ()}) == set()
    assert offered(volume=True, measured={"r": ()}) == {"region:r"}


# ------------------------------------------------------------------ where the volume must be

def test_review_volume_reads_the_exact_path_the_slab_replaces(tmp_path: Path):
    """THE TWO HALVES HAVE TO MEET. `_reduce_to_review_slabs` ends in `tmp.replace(src)`, so the
    slab lands on the interior's own filename. `review_volume` returns a path only when that
    filename `is_file()`. If either side moved, the slab would be written where nothing looks and
    this whole class of refusal would come back silently."""
    from meshpipeline.engines.cfmesh_conjugate.review_surface import review_volume

    d = tmp_path / "VTK" / "fluid" / "decomposed_0"
    d.mkdir(parents=True)
    # the empty directory job 40e4e578 actually had
    assert review_volume(tmp_path) is None, "an empty region dir must not read as a volume"

    (d / "internal.vtu").write_bytes(b"<VTKFile/>")
    got = review_volume(tmp_path)
    assert got == "VTK/fluid/decomposed_0/internal.vtu", got


def test_no_vtk_directory_is_no_volume(tmp_path: Path):
    from meshpipeline.engines.cfmesh_conjugate.review_surface import review_volume

    assert review_volume(tmp_path) is None


def test_the_slab_is_written_onto_the_interiors_own_name():
    """Read from the producer, so a rename on either side fails here rather than in a 2.5-hour
    run: the temp file carries `.slab` inside the stem precisely because pyvista picks its writer
    off the extension, and then it replaces `src`."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import native as N

    src = inspect.getsource(N._reduce_to_review_slabs)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert 'src.with_name(src.stem + ".slab" + src.suffix)' in code
    assert "tmp.replace(src)" in code, "the slab no longer lands on the interior's own name"


# ------------------------------------------------------------------ the reason is reported

def test_the_regions_not_offered_are_named_in_the_log(caplog):
    """An operator has to be able to tell "no cut came home" from "a cut came home that nothing
    can be counted in" - the fixes are different."""
    import logging

    with caplog.at_level(logging.INFO):
        offered(volume=False)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "across_the_flow" in said and "along_the_flow" in said
    assert "through_the_plate" not in said, "a region that WAS offered was reported as unshowable"


@pytest.mark.parametrize("vol", [True, False])
def test_the_four_other_engines_are_unaffected(vol):
    """They pass a volume and no cuts, and their behaviour must be unchanged to the line."""
    regions = {"r": _Region("r")}
    got = RA.expand_region_targets(regions, purpose=PURPOSE, required=True,
                                   volume_available=vol)
    assert ({t.target_id for t in got} == {"region:r"}) is vol
