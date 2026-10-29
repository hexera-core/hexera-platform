"""A picture of nothing is not evidence, and half a conjugate surface is not the surface.

A conjugate mesh that was built correctly came back `verdict=passed ... concerns=[]` while the
reviewer had seen neither of its two regions properly. Measured, live, on job 575f76e6:

    render backend (PyVista): loaded .../mesh.msh - patches: ['solid']
    render backend: loaded volume .../VTK/fluid/cr_ws_n41vtoe6_1/internal.vtu (0 cells)
    Reviewer: eligibility accepted - verdict=passed rounds=8 blocking=[] improve=[] concerns=[]

The mesh itself was fine: 2,520,586 fluid cells and 4,061,885 solid cells, both passing checkMesh,
interface 346,456 faces on each side.

WHAT THE VOLUME ACTUALLY WAS. Not empty - TRUNCATED. 197,566,474 bytes whose own header declared
`NumberOfPoints='2777505' NumberOfCells='2520586'`, 6,718 bytes short of its closing tags, ending
mid-base64. `pv.read` does not raise on that: it warns and returns `n_cells=0`. So the header lies
and there is no cheap check - the read's own count is the only honest signal, and it was being
logged and thrown away (`self._volume.n_cells` appeared exactly once in the codebase, inside the
log format string).

THE CHAIN THAT TURNED THAT INTO A PASS. The volume was published on `is_file()`; it resolved on
`is_file()` plus a 512-byte `<?xml` sniff; `has_volume` is `volume is not None`, so both region
targets were advertised; the cuts through it returned blank frames because `if sl.n_cells:` decided
only whether to DRAW; the tool text said the cut "exposed interior cells" regardless;
`record_operation_evidence` filed `image_ok=True, covered=True` as literals because a PNG path was
non-empty; and the only emptiness check anywhere in the path is the PNG's byte size, which a blank
1280x960 frame clears by several KB.

These tests pin the two cuts that close it at their sources.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

FLUID, SOLID = "fluid", "solid"


def _delivered(ws: Path, *regions: str) -> None:
    """A workspace whose polyMesh says which regions the MESH has."""
    for r in regions:
        d = ws / "constant" / r / "polyMesh"
        d.mkdir(parents=True, exist_ok=True)
        (d / "boundary").write_text("0()")


# #
# half a surface is not published
# #
def test_the_regions_are_read_off_the_delivered_mesh_and_not_the_export(tmp_path):
    """The export is the thing that failed. Asking it which regions exist is asking it to grade
    itself."""
    _delivered(tmp_path, FLUID, SOLID)
    (tmp_path / "VTK" / "fluid").mkdir(parents=True)        # an export of one region only
    assert RS.delivered_regions(tmp_path) == [FLUID, SOLID]


def test_the_root_polymesh_is_not_a_region(tmp_path):
    """splitMeshRegions leaves the pre-split block at constant/polyMesh and it is not a region of
    the delivered mesh."""
    _delivered(tmp_path, FLUID, SOLID)
    root = tmp_path / "constant" / "polyMesh"
    root.mkdir(parents=True)
    (root / "boundary").write_text("0()")
    assert RS.delivered_regions(tmp_path) == [FLUID, SOLID]


def test_a_workspace_with_no_regions_names_none(tmp_path):
    """Every unit fixture that fakes a VTK tree without a polyMesh: nothing is required of it and
    it is published as before."""
    assert RS.delivered_regions(tmp_path) == []


def test_a_surface_missing_a_region_is_refused_in_words():
    """The source check, because reaching the publication needs gmsh and pyvista and a real
    export. The refusal names both counts and both region lists, so a reader of the log knows
    which half they would have been shown."""
    src = inspect.getsource(RS.build_region_msh)
    assert "delivered_regions(ws)" in src
    assert "missing = [r for r in want if r not in groups]" in src
    assert "logger.error" in src.split("missing:")[1][:400]
    assert "NOT published" in src
    # and it returns the same nothing as "no VTK at all", which finalize already treats as a real
    # optional state
    tail = src.split("missing:")[1][:900]
    assert "return {}, None" in tail


def test_the_refusal_is_the_state_finalize_already_handles():
    """`native._review_aids` leaves out["surface"] as None when build_region_msh returns nothing,
    and the manifest then publishes no surface - which raises EVIDENCE_MISSING at the review. That
    is the honest outcome and it is what the run with the PRUNED volume already did."""
    from meshpipeline.engines.cfmesh_conjugate import native as N

    src = inspect.getsource(N._review_aids)
    assert "groups, bbox = build_region_msh(ws)" in src
    assert "if groups:" in src, "a falsy groups must leave the surface unpublished"


# #
# a picture of nothing is not evidence
# #
def test_a_volume_that_reads_as_no_cells_is_not_a_volume():
    """The only honest signal, because the header lies: 197,566,474 bytes declaring 2,520,586
    cells read back as 0."""
    from meshpipeline.sandbox import backend as B

    src = inspect.getsource(B.MeshRenderBackend._load_volume)
    assert "if not got.n_cells:" in src
    assert "self._volume = None" in src.split("if not got.n_cells:")[1][:400]
    assert "truncated" in src


def test_the_three_ways_a_cut_can_produce_nothing_all_return_no_image():
    """It used to return take_screenshot() for two of them and a blank frame for the third -
    three pictures that are not sections, handed back where a section was asked for."""
    from meshpipeline.sandbox import backend as B

    src = inspect.getsource(B.MeshRenderBackend.inspect_region)
    assert "self.take_screenshot()" not in src, "a screenshot is not a section"
    # every empty return carries its reason: three of them, and no bare `return ""` anywhere
    assert src.count('return "", ') == 3, "no readable volume, a slice that raised, an empty cut"
    # EVERY return out of this method is two-valued - the three empty ones and the success,
    # whose reason is the empty half. A one-value return anywhere would tell the adapter nothing
    # about why there is no section.
    rets = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("return")]
    assert len(rets) == 4, rets
    assert len([r for r in rets if r.startswith('return "", ')]) == 3, rets
    assert len([r for r in rets if r.endswith(', ""')]) == 1, rets
    assert "if not sl.n_cells:" in src


def test_the_cut_is_only_drawn_once_it_is_known_to_have_cells():
    """`if sl.n_cells:` used to decide whether to DRAW, and the frame was captured and returned
    either way. By the time add_mesh is reached the slice has cells."""
    from meshpipeline.sandbox import backend as B

    src = inspect.getsource(B.MeshRenderBackend.inspect_region)
    at_guard = src.index("if not sl.n_cells:")
    at_draw = src.index("p.add_mesh(")
    assert at_guard < at_draw
    assert "if sl.n_cells:\n" not in src


def test_the_reason_rides_in_the_return_and_adds_no_public_surface():
    """test_the_public_surface_of_the_backend_is_unchanged_by_the_split pins that surface, and it
    is right to: the adapter is the only caller and it is the thing that needs the words."""
    from meshpipeline.sandbox import backend as B

    public = {n for n in dir(B.MeshRenderBackend) if not n.startswith("_")}
    assert "last_inspect_note" not in public
    sig = inspect.signature(B.MeshRenderBackend.inspect_region)
    assert "tuple[str, str]" in str(sig.return_annotation) or sig.return_annotation is not None


def test_a_cut_with_no_section_is_a_refusal_and_not_an_image():
    """The rule the adapter already states for a patch named wrong - "A failure, never a
    screenshot: a returned image would let the coverage floor count a patch as 'inspected'" - now
    holds for a region too. And the RETURN is checked before last_shot_path, because a stale path
    from an earlier screenshot would otherwise be filed as this region's section."""
    from meshpipeline.sandbox import render_adapter as RA

    src = inspect.getsource(RA.BackendRenderSession._inspect_target)
    assert "cut, why = self._backend.inspect_region(region)" in src
    assert "if not cut:" in src
    assert "self._failed(" in src
    # the CODE, not the prose: `last_shot_path` is named in the comment above the call too, and
    # an index into the raw source finds that first
    at_call = src.index("cut, why = self._backend.inspect_region(region)")
    at_path = src.index("shot = self._backend.last_shot_path")
    assert at_call < at_path, "the return decides, not a path that may be stale"


def test_a_refusal_covers_no_target(tmp_path):
    """Why the refusal is enough: _failed carries no image, and an EvidenceItem with no image has
    its covers_target cleared - so the ledger cannot file it as an inspection of the region."""
    from meshpipeline.sandbox import render_adapter as RA

    src = inspect.getsource(RA.BackendRenderSession._evidence)
    assert 'covers_target=covers_target if image else ""' in src
    failed = inspect.getsource(RA.BackendRenderSession._failed)
    assert 'image_ref=""' in failed
    assert 'covers_target=""' in failed
