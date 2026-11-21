# Responsibility: a region whose cells live in per-rank meshes must be measured across those
# ranks and described by the builders that read them.
# Boundaries: the command string, which boundary files are rewritten, and which builder is called.
# No OpenFOAM, no subprocess - the one thing never faked is the command itself.
"""A decomposed region is measured where its cells are, and described from them.

WHY THIS EXISTS, and it is the gap that would have made the whole decomposed path pointless. With
`CONJUGATE_MERGE_REGIONS=0` the chain never reconstructs, so `constant/<region>/polyMesh` carries
the patch table `_publish_decomposed_boundaries` wrote and no cells at all. Three things then read
that empty directory and have to be sent somewhere else instead:

  - `checkMesh -constant -region X` finds a patch table and no mesh. The run reaches the reviewer
    with no verdict, no `mesh_ok` and none of the three fields the viewer's colours come from -
    which reads as a bad mesh and is a measurement that never ran.
  - `write_index` refuses by name, so the detail window refuses every region of the delivery.
  - `write_quality` has nothing to reduce.

WHAT A MERGED DELIVERY KEEPS. Every corpus part was measured with the serial command and the
serial builders, and a setting that changed what an already-measured part builds would make every
past measurement unreadable. So the rank count reaches `check_mesh` only for the regions
`_publish_decomposed_boundaries` NAMED - empty on every run that reconstructed - and the builder
is chosen by whether the region's own `faces` file is there. Both arms are pinned here.

THE PARALLEL RETYPE IS THE PART THAT WAS GOT WRONG BEFORE. Each rank loads its own
`processorN/constant/<region>/polyMesh`, and `splitMeshRegions -parallel` wrote the mapped pair on
to every one of them, so retyping only the root leaves every rank carrying `sampleRegion` and
every rank taking the same fatal after its own summary.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from foam_fixtures import write_decomposed_row_of_hexes, write_row_of_hexes
from meshpipeline.engines.cfmesh_conjugate import foam_exec, native
from meshpipeline.engines.cfmesh_conjugate.conjugate import _publish_decomposed_boundaries
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import rank_of

MAPPED = """FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}

2
(
    wall
    {
        type            wall;
        nFaces          8;
        startFace       2;
    }
    fluid_to_solid
    {
        type            mappedWall;
        inGroups        1(mappedPatch);
        sampleMode      nearestPatchFace;
        sampleRegion    solid;
        samplePatch     solid_to_fluid;
        nFaces          1;
        startFace       1;
    }
)
"""


def _recording_run(calls):
    """Stand in for `run_guarded`, keeping the command and writing a verdict to its log."""
    def fake(argv, **kw):
        calls.append(argv[-1])
        fh = kw.get("stdout")
        if fh is not None:
            fh.write("Checking geometry...\n"
                     "    Mesh non-orthogonality Max: 1.0 average: 0.1\n"
                     "    Max skewness = 0.5 OK.\n"
                     "    Max aspect ratio = 1.0 OK.\n"
                     "Mesh OK.\nEnd\n")
            fh.flush()
        return None
    return fake


# -- the command ---------------------------------------------------------------------------

def test_a_decomposed_region_is_measured_across_its_ranks(tmp_path, monkeypatch):
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=4, n_cells=8, region="fluid")
    _publish_decomposed_boundaries(case)
    calls: list[str] = []
    monkeypatch.setattr(foam_exec, "run_guarded", _recording_run(calls))
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    q = foam_exec.check_mesh(case, region="fluid", write_fields=True, ranks=4)
    assert q["mesh_ok"] is True
    cmd = calls[0]
    assert "mpirun" in cmd and "-np 4" in cmd and cmd.rstrip().endswith("-parallel")
    assert "checkMesh -constant -region fluid" in cmd
    # `-noZero` IS MEASURED KNOWLEDGE, not caution. `preparePar` and `splitMeshRegions -parallel`
    # both leave a `processorN/0`, and a `-parallel` utility given `-constant` walks `constant`
    # and then `0` - which is how `topoSet -parallel -constant` hung at rc=124 on a 300 s
    # ceiling, twice. checkMesh would reach a time with no mesh in it AFTER a measurement that
    # had already succeeded, which is the worst place there is to fail.
    assert "-noZero" in cmd
    # AND THE FLAGS THAT MAKE A RANK START. Set in the launching shell they reach mpirun and
    # nothing else, so without the -x they stay at controlDict's default in every rank.
    assert "MPI_BUFFER_SIZE" in cmd and "-x MPI_BUFFER_SIZE" in cmd
    assert "OMP_NUM_THREADS" in cmd and "-x OMP_NUM_THREADS" in cmd


def test_a_merged_region_keeps_the_command_every_corpus_part_was_measured_with(tmp_path,
                                                                               monkeypatch):
    """Byte for byte. "+15.1% with the fields and every verdict line byte-identical" is a claim
    about THIS string, and a setting that changed it would make every past figure unreadable."""
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")
    calls: list[str] = []
    monkeypatch.setattr(foam_exec, "run_guarded", _recording_run(calls))
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    foam_exec.check_mesh(case, region="fluid", write_fields=True)
    cmd = calls[0]
    assert cmd.endswith("checkMesh -constant -region fluid -allGeometry -writeSets vtk "
                        "-writeFields '(nonOrthoAngle skewness aspectRatio)'")
    assert "mpirun" not in cmd and "-parallel" not in cmd and "-noZero" not in cmd


def test_the_chain_passes_the_ranks_only_for_the_regions_that_have_no_serial_mesh():
    """The publisher names them, so nothing asks the question twice and gets a second answer."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import conjugate

    src = inspect.getsource(conjugate)
    assert "published = _publish_decomposed_boundaries(ws)" in src
    assert "ranks=int(ranks) if name in published else 1" in src


# -- the retype --------------------------------------------------------------------------

def test_every_rank_is_measured_with_the_interface_as_a_plain_wall(tmp_path, monkeypatch):
    """`checkMesh -region fluid` asks a mapped patch for its sample mesh, which was never
    registered, and dies AFTER the summary and BEFORE the verdict. Each rank loads its own
    boundary file, so each rank takes that fatal unless each one was retyped."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    _publish_decomposed_boundaries(case)
    ranks = [case / f"processor{r}" / "constant" / "fluid" / "polyMesh" / "boundary"
             for r in range(3)]
    for p in ranks:
        p.write_text(MAPPED)
    root = case / "constant" / "fluid" / "polyMesh" / "boundary"
    root.write_text(MAPPED)

    seen: list[dict] = []

    def fake(argv, **kw):
        seen.append({str(p): p.read_text() for p in [*ranks, root]})
        kw["stdout"].write("Mesh OK.\n")
        return None

    monkeypatch.setattr(foam_exec, "run_guarded", fake)
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    q = foam_exec.check_mesh(case, region="fluid", ranks=3)

    during = seen[0]
    for p in ranks:
        assert "mappedWall" not in during[str(p)], f"{p} was still mapped while it was measured"
        assert "sampleRegion" not in during[str(p)]
    # THE ROOT IS LEFT ALONE, deliberately: no rank reads it, and on a decomposed delivery it is
    # the PUBLISHED patch table - the file `open_index` stamps and cross-checks the sidecar
    # against. One fewer file rewritten is one fewer way to leave the coupling broken.
    assert "mappedWall" in during[str(root)]
    assert q["checked_as_walls"] == ["fluid_to_solid"]
    # AND THE COUPLING IS THE DELIVERABLE, so it is back on every rank.
    for p in [*ranks, root]:
        assert p.read_text() == MAPPED


def test_the_coupling_goes_back_on_every_rank_even_when_the_measurement_dies(tmp_path,
                                                                             monkeypatch):
    """A mesh left with its interface retyped to a wall is a mesh no conjugate solver can run,
    broken by the step that only measures."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    _publish_decomposed_boundaries(case)
    ranks = [case / f"processor{r}" / "constant" / "fluid" / "polyMesh" / "boundary"
             for r in range(3)]
    for p in ranks:
        p.write_text(MAPPED)

    def dies(argv, **kw):
        raise TimeoutError("checkMesh did not finish")

    monkeypatch.setattr(foam_exec, "run_guarded", dies)
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    q = foam_exec.check_mesh(case, region="fluid", ranks=3)
    assert q["unmeasured"] is True
    for p in ranks:
        assert p.read_text() == MAPPED


def test_a_serial_run_still_retypes_the_one_file_it_opens(tmp_path, monkeypatch):
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")
    root = case / "constant" / "fluid" / "polyMesh" / "boundary"
    root.write_text(MAPPED)
    seen = []
    monkeypatch.setattr(foam_exec, "run_guarded",
                        lambda argv, **kw: (seen.append(root.read_text()),
                                            kw["stdout"].write("Mesh OK.\n"))[0])
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    foam_exec.check_mesh(case, region="fluid")
    assert "mappedWall" not in seen[0]
    assert root.read_text() == MAPPED


def test_without_a_region_nothing_is_retyped(tmp_path):
    """`checkMesh` with no -region reads the pre-split mesh, which has no mapped patch to trip
    over, so there is nothing to rewrite and no window in which the deliverable is broken."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4)
    assert foam_exec._boundaries_to_measure(case, region="", ranks=2) == []
    assert foam_exec._boundaries_to_measure(case, region="", ranks=1) == []


def test_the_ranks_boundaries_are_what_a_parallel_run_rewrites(tmp_path):
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=4, n_cells=8, region="fluid")
    _publish_decomposed_boundaries(case)
    got = foam_exec._boundaries_to_measure(case, region="fluid", ranks=4)
    assert [rank_of(p) for p in got] == [f"processor{r}" for r in range(4)]
    serial = foam_exec._boundaries_to_measure(case, region="fluid", ranks=1)
    assert serial == [case / "constant" / "fluid" / "polyMesh" / "boundary"]


# -- the poor-cell sets ------------------------------------------------------------------

def test_each_ranks_poor_cells_are_kept_rather_than_the_last_rank_to_be_moved(tmp_path):
    """A parallel checkMesh writes `skewFaces.vtk` on EVERY rank that found one. Moved to one
    name, seven ranks' worth of poor cells vanish into a filename collision - and the highlight
    then shows an eighth of the problem with the whole count printed beside it."""
    case = tmp_path / "ws"
    for r in range(3):
        d = case / f"processor{r}" / "postProcessing" / "skewFaces" / "constant"
        d.mkdir(parents=True)
        (d / "skewFaces.vtk").write_text(f"rank {r}\n")
    kept = foam_exec.collect_poor_sets(case, region="fluid")
    assert len(kept) == 3, "one per rank"
    assert sorted(Path(k["path"]).name for k in kept) == [
        "processor0_skewFaces.vtk", "processor1_skewFaces.vtk", "processor2_skewFaces.vtk"]
    for r in range(3):
        landed = case / Path([k["path"] for k in kept
                              if f"processor{r}_" in k["path"]][0])
        assert landed.read_text() == f"rank {r}\n"


def test_a_serial_set_keeps_its_own_name(tmp_path):
    case = tmp_path / "ws"
    d = case / "postProcessing" / "skewFaces" / "constant"
    d.mkdir(parents=True)
    (d / "skewFaces.vtk").write_text("one\n")
    kept = foam_exec.collect_poor_sets(case, region="fluid")
    assert [Path(k["path"]).name for k in kept] == ["skewFaces.vtk"]


# -- which builder the engine calls ------------------------------------------------------

def test_the_engine_asks_once_and_hands_the_answer_to_both_sidecars():
    """The index and the quality array are numbered against each other face for face, so an index
    built from the ranks beside a quality array reduced from a serial mesh would paint one mesh's
    numbers on another's faces and look entirely plausible. One question, one answer."""
    import inspect

    src = inspect.getsource(native._index_regions)
    assert src.count("index_is_decomposed(ws, polymesh)") == 1, (
        "asked once, so the two sidecars cannot be given different answers")
    assert "write_index_decomposed(ws, region=name) if decomposed" in src
    assert "case=ws if decomposed else None" in src


def test_the_engine_indexes_a_decomposed_delivery(tmp_path):
    """End to end through the engine's own entry point, with no serial mesh anywhere."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    assert _publish_decomposed_boundaries(case) == ["fluid"]
    out = native._boundary_indexes(case)
    got = out["regions"]["fluid"]
    assert got["ok"] is True, got.get("why")
    assert got["decomposed"] is True
    assert got["faces"] == 26, "inlet 1, outlet 1, wall 24"
    # NO COLOURS HERE AND THAT IS NOT A FAILURE OF THE MESH: checkMesh wrote no fields in this
    # fixture, so the quality sidecar says why and the index stands on its own.
    assert got["quality"]["ok"] is False
    assert "checkMesh" in got["quality"]["why"]


def test_the_engine_still_indexes_a_merged_delivery_the_old_way(tmp_path):
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")
    out = native._boundary_indexes(case)
    got = out["regions"]["fluid"]
    assert got["ok"] is True, got.get("why")
    assert got["decomposed"] is False
    assert got["faces"] == 14


def test_a_merged_delivery_with_its_ranks_still_on_disk_is_not_decomposed(tmp_path):
    """The reconstruct runs LAST and the per-rank copies are removed after it, so there is a
    window in which both are there. "Are there ranks" is true in it and says nothing."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")      # as if reconstructed
    out = native._boundary_indexes(case)
    got = out["regions"]["fluid"]
    assert got["ok"] is True, got.get("why")
    assert got["decomposed"] is False
    assert got["faces"] == 14, "the reconstructed mesh's own faces, not the ranks' 14"


# -- and the fields are cleared from wherever they were written -------------------------

def test_the_raw_fields_are_cleared_from_every_rank(tmp_path):
    """4.25 GB across both regions of the customer plate becomes 319 MB, and on a decomposed run
    those bytes are spread over the ranks instead of gathered - the same bytes in more places and
    exactly as worth clearing. The workspace is archived and returned whole."""
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    got = native._field_dirs(case / "constant" / "fluid" / "polyMesh", case=case, region="fluid")
    assert [p.parent.parent.name for p in got] == [f"processor{r}" for r in range(3)]
    assert all(p.name == "fluid" for p in got)


def test_a_merged_run_clears_the_one_directory_it_wrote(tmp_path):
    case = tmp_path / "ws"
    pm = case / "constant" / "fluid" / "polyMesh"
    write_row_of_hexes(pm)
    assert native._field_dirs(pm, case=None, region="fluid") == [pm.parent]


@pytest.mark.parametrize("ranks", [1, 8])
def test_the_rank_count_never_reaches_a_region_with_no_name(tmp_path, monkeypatch, ranks):
    """The pre-split mesh at constant/polyMesh is not a region and is never measured per-rank."""
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "polyMesh")
    calls: list[str] = []
    monkeypatch.setattr(foam_exec, "run_guarded", _recording_run(calls))
    monkeypatch.setattr(foam_exec, "scan_case_dicts", lambda ws: "")
    foam_exec.check_mesh(case, region="", ranks=ranks)
    assert ("mpirun" in calls[0]) is (ranks > 1), (
        "the flag follows the caller, and the caller only passes it for a published region")

# -- and the order the two non-fatal passes run in -----------------------------------------

def test_a_decomposed_delivery_is_recognised_as_one(tmp_path):
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    _publish_decomposed_boundaries(case)
    assert native._delivered_decomposed(case) is True


def test_a_merged_delivery_is_not(tmp_path):
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")
    assert native._delivered_decomposed(case) is False


def test_a_workspace_with_nothing_delivered_is_not(tmp_path):
    assert native._delivered_decomposed(tmp_path) is False


def test_the_order_is_not_worth_failing_a_mesh_over(tmp_path, monkeypatch):
    """Getting the ORDER of two non-fatal passes wrong is not a reason to lose a mesh, so an
    unreadable workspace keeps the order that has always been measured."""
    monkeypatch.setattr("meshpipeline.render.detail_window.delivered_regions",
                        lambda ws: (_ for _ in ()).throw(OSError("gone")))
    assert native._delivered_decomposed(tmp_path) is False


def test_a_decomposed_delivery_writes_its_index_before_the_pass_that_reads_it(tmp_path):
    """`build_region_msh` falls back to the boundary index when there is no .vtp tree, and a
    decomposed delivery HAS no tree and can have none: foamToVTK reads
    `constant/<region>/polyMesh`, which carries a patch table and no cells, and `-parallel` is not
    the way out because every consumer of its output is written for the serial layout.

    `mesh_paths.surface` is required=True, so a sidecar that does not exist yet when the fallback
    looks for it is the whole review - which is exactly what job 57b613d7 cost to learn once.

    READ OFF A REAL WORKSPACE, not off the source. This was a source test first, and a mutation
    that pinned the condition to False left it passing: it could see the branch and not the order.
    """
    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    _publish_decomposed_boundaries(case)
    assert native._tail_passes(case) == (native.TAIL_INDEX, native.TAIL_REVIEW)


def test_a_merged_delivery_keeps_the_order_every_corpus_part_was_measured_in(tmp_path):
    """Its review surface comes from the export, so nothing about it reads the index - and the two
    passes share one budget, so swapping them swaps which one a run out of time loses."""
    case = tmp_path / "ws"
    write_row_of_hexes(case / "constant" / "fluid" / "polyMesh")
    assert native._tail_passes(case) == (native.TAIL_REVIEW, native.TAIL_INDEX)


def test_the_run_walks_whatever_order_it_is_given():
    """Both passes happen either way - this decides which is first, never whether."""
    import inspect

    src = inspect.getsource(native._run_conjugate_local)
    assert "for _pass in _tail_passes(ws):" in src
    assert src.count("_boundary_indexes(ws") == 1, "one call, ordered by the loop"
    assert src.count("_review_aids(ws") == 1


def test_the_review_surface_really_does_come_out_of_the_sidecar(tmp_path):
    """Not an assertion about the source - the surface itself, built from a decomposed index with
    no VTK tree anywhere in the workspace."""
    from meshpipeline.engines.cfmesh_conjugate.review_surface import _patches_from_index

    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    assert _publish_decomposed_boundaries(case) == ["fluid"]
    assert native._boundary_indexes(case)["regions"]["fluid"]["ok"] is True
    assert not list(case.rglob("*.vtp")), "there is no export here and there cannot be one"
    patches = _patches_from_index(case, "fluid")
    assert [p["name"] for p in patches] == ["fluid/inlet", "fluid/outlet", "fluid/wall"]
    assert sum(int(p["face_count"]) for p in patches) == 26


def test_the_raw_fields_are_really_gone_from_every_rank(tmp_path):
    """Not the list of directories - the files. 4.25 GB across both regions of the customer plate
    becomes 319 MB, and on a decomposed run those bytes are spread over the ranks instead of
    gathered: the same bytes in more places, in a workspace that is archived and returned whole.
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from foam_fixtures import write_quality_fields
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import CHECKMESH_LOG

    case = tmp_path / "ws"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    per_rank = {0: {"inlet": 1, "wall": 4}, 1: {"wall": 4}, 2: {"outlet": 1, "wall": 4}}
    for r, patches in per_rank.items():
        write_quality_fields(
            case / f"processor{r}" / "constant" / "fluid", cells=[1.0 + r],
            per_face={"skewness": {p: [0.1] * n for p, n in patches.items()}},
            uniform={"nonOrthoAngle": dict.fromkeys(patches, 0.0)},
            patches=tuple(patches))
    (case / CHECKMESH_LOG.format(suffix="_fluid")).write_text(
        "    Mesh non-orthogonality Max: 3.0 average: 0.1\n"
        "    Max skewness = 0.5 OK.\n    Max aspect ratio = 3.0 OK.\nMesh OK.\n")
    _publish_decomposed_boundaries(case)
    out = native._boundary_indexes(case)
    got = out["regions"]["fluid"]
    assert got["ok"] is True, got.get("why")
    assert got["quality"]["ok"] is True, got["quality"].get("why")
    assert got["quality"]["ranks"] == 3
    assert got["quality"]["fields_cleared_bytes"] > 0
    left = sorted(p.name for r in range(3)
                  for p in (case / f"processor{r}" / "constant" / "fluid").iterdir()
                  if p.is_file())
    assert left == [], f"checkMesh's raw fields are still on the ranks: {left}"
    # AND THE REDUCTION IS STILL THERE, which is the whole point of clearing them.
    assert (case / "constant" / "fluid" / "polyMesh" / "boundaryQuality"
            / "aspect_ratio.f32").is_file()


def test_a_merged_run_still_clears_its_own_one_directory(tmp_path):
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from foam_fixtures import write_quality_fields

    case = tmp_path / "ws"
    pm = case / "constant" / "fluid" / "polyMesh"
    write_row_of_hexes(pm)
    write_quality_fields(pm.parent, cells=[1.0, 2.0, 3.0],
                         per_face={"skewness": {"inlet": [0.1], "outlet": [0.2],
                                                "wall": [0.3] * 12}},
                         uniform={"nonOrthoAngle": {"inlet": 0.0, "outlet": 0.0, "wall": 0.0}})
    got = native._boundary_indexes(case)["regions"]["fluid"]
    assert got["ok"] is True and got["decomposed"] is False
    assert got["quality"]["ok"] is True, got["quality"].get("why")
    assert got["quality"]["fields_cleared_bytes"] > 0
    assert sorted(p.name for p in pm.parent.iterdir() if p.is_file()) == []
