"""Past the per-process octree ceiling, the block is meshed across processes.

WHY THIS EXISTS. cfMesh's octree addressing holds about twelve 32-bit labels per box, so ONE
process runs out of label space at (2^31 - 1) / 12 boxes. The run this was measured on asked for
181,673,384 cells - the Corintis plate at 22.00 um with its cells stretched 6x along the channels
- and died 231 s in, inside `meshOctreeAddressing::createNodeLabels`:

    --> FOAM FATAL ERROR: Negative size requested.
        From void Foam::Module::LongList<T, Offset>::allocateSize(Foam::label)

Not out of memory. Out of int, on a box with 251 GB and 402 GB of free scratch. budget.py has
priced that ceiling since before the parallel chain existed and `refuse_for_octree` has named the
way past it in words all along; nothing drove it, so every configuration above the ceiling was
refused at planning time and the one that slipped through aborted.

PROVEN ON A REAL MESH, 2026-10-29, on mesh-bench (8 vCPU, Open MPI 4.1.2), the ported two-scale
plate at 0.5 mm: preparePar laid out 8 processor directories, `mpirun -np 8 cartesianMesh
-parallel` meshed 2,317,572 cells in each of them in 68.29 s, reconstructParMesh merged them, and
checkMesh read the merged mesh back as

    points 6,220,725   faces 18,420,912   internal faces 18,181,584   cells 6,100,416   Mesh OK.

ONLY THE MESHER GOES PARALLEL. The ceiling belongs to cfMesh's octree and nothing downstream
shares it, so the mesh is merged the moment it exists and the cell-zone split, the two-region
check and the port cut are the same code on the same kind of mesh they have always run on.
"""
import inspect

import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import plan as PL
from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    DECOMPOSE_METHOD,
    MPI_FLAGS,
    OCTREE_BOXES_PER_PROCESS,
    PREPARE_PAR,
    ConjugateError,
    build,
    mpi_command,
    ranks_for,
    render_decompose_pardict,
)

#: The configuration that aborted, and the one that delivered, both measured on the same part.
ABORTED_CELLS = 181_673_384
DELIVERED_CELLS = 102_630_992


#   -- how many processes a mesh needs -------------------------------------------------------

def test_the_ceiling_is_the_label_arithmetic_and_not_a_round_number():
    """Twelve labels a box out of a signed 32-bit label. A number chosen for tidiness here would
    either refuse meshes that build or start ones that abort an hour in."""
    assert OCTREE_BOXES_PER_PROCESS == (2 ** 31 - 1) // 12
    assert OCTREE_BOXES_PER_PROCESS == 178_956_970


def test_the_mesh_that_was_delivered_needs_one_process():
    """102,630,992 cells built serially on this exact part, so nothing about this may change it."""
    assert ranks_for(DELIVERED_CELLS) == 1


def test_the_mesh_that_aborted_needs_two():
    """2.7 million cells over the ceiling is still over it; a rank count is not a round-up."""
    assert ranks_for(ABORTED_CELLS) == 2


@pytest.mark.parametrize("cells, ranks", [
    (1, 1),
    (OCTREE_BOXES_PER_PROCESS, 1),            # exactly the ceiling still fits in one
    (OCTREE_BOXES_PER_PROCESS + 1, 2),        # one box over needs a second
    (2 * OCTREE_BOXES_PER_PROCESS, 2),
    (634_530_269, 4),                         # the 11.00 um rung of the measured ladder
])
def test_the_count_is_what_the_ceiling_demands(cells, ranks):
    assert ranks_for(cells) == ranks


def test_a_cap_only_applies_when_a_caller_names_one():
    """A plan that needs more ranks than the box has cores is still a plan that needs them, and
    saying so is more use than quietly meshing something smaller. `available` is the caller's."""
    assert ranks_for(10 ** 10) > 8
    assert ranks_for(10 ** 10, available=8) == 8
    assert ranks_for(DELIVERED_CELLS, available=8) == 1, "a cap never raises the count"


def test_the_budget_and_the_engine_agree_on_the_ceiling():
    """Two numbers for one boundary is how a run gets planned above what it can build. budget.py
    prices it for the planner and conjugate.py enforces it for the runner."""
    assert B.octree_ceiling_cells(1) == OCTREE_BOXES_PER_PROCESS
    assert B.octree_ceiling_cells(8) == 8 * OCTREE_BOXES_PER_PROCESS
    for n in (1, 2, 4, 8):
        assert ranks_for(B.octree_ceiling_cells(n)) == n


#   -- the dictionary preparePar reads ---------------------------------------------------------

def test_the_dictionary_says_how_many_ways_and_by_what_rule():
    d = render_decompose_pardict(8)
    assert "numberOfSubdomains 8;" in d
    assert f"method          {DECOMPOSE_METHOD};" in d
    assert 'object decomposeParDict;' in d
    assert 'location "system";' in d, "OpenFOAM reads the location off the header"


def test_it_is_a_dictionary_and_not_a_mangled_string():
    """This file has had a backslash eaten by tooling twice (see _NEWLINE), and a decomposeParDict
    whose lines ran together is a dictionary OpenFOAM refuses after the surfaces are staged."""
    lines = [ln for ln in render_decompose_pardict(4).split("\n") if ln.strip()]
    assert len(lines) == 3, lines
    assert lines[0].startswith("FoamFile {") and lines[0].rstrip().endswith("}")
    assert render_decompose_pardict(4).endswith("\n")
    assert "\\n" not in render_decompose_pardict(4)


@pytest.mark.parametrize("ranks", [1, 0, -1])
def test_one_process_is_not_a_parallel_run(ranks):
    """The serial path writes no dictionary at all, so being asked for one here is a caller bug."""
    with pytest.raises(ConjugateError, match="at least two ranks"):
        render_decompose_pardict(ranks)


@pytest.mark.parametrize("method", ["scotch; #include \"evil\"", "", "hier archical", "a/b"])
def test_a_method_that_is_not_a_name_is_refused(method):
    """It is interpolated into a dictionary OpenFOAM reads. The case guard scans for the
    executable constructs; a method name is not the place to start trusting free text."""
    with pytest.raises(ConjugateError, match="decomposition method"):
        render_decompose_pardict(8, method=method)


#   -- the command ------------------------------------------------------------------------------

def test_both_flags_are_there_and_both_were_measured():
    """--allow-run-as-root because the mesh container is root and Open MPI refuses to start.
    --oversubscribe because Open MPI counts the slots it believes it has, not the cores the box
    has: inside the container nproc said 8 and mpirun still refused 8 ranks without it."""
    cmd = mpi_command("cartesianMesh", 8)
    assert cmd == ("MPI_BUFFER_SIZE=1000000000 mpirun --allow-run-as-root "
                   "--oversubscribe -x MPI_BUFFER_SIZE -np 8 cartesianMesh -parallel")
    assert set(MPI_FLAGS) == {"--allow-run-as-root", "--oversubscribe"}


def test_the_utility_is_told_it_is_parallel():
    """Without -parallel each rank runs the whole case independently and writes over the others."""
    assert mpi_command("topoSet", 4).endswith(" -parallel")


#   -- the runner actually does it --------------------------------------------------------------

def test_the_serial_chain_is_untouched_by_any_of_this():
    """Every part in the corpus meshes in one process and must keep doing it exactly as before:
    the mesher, then topoSet, then the split, with no mpirun and nothing to prepare."""
    src = inspect.getsource(build)
    serial = src.split("else:", 1)[1].split("if port_patches", 1)[0]
    assert '("cartesianMesh", "cartesianMesh")' in serial
    assert '("topoSet", "topoSet")' in serial
    assert '" ".join(SPLIT_ARGS)' in serial
    assert "mpi_command" not in serial and "PREPARE_PAR" not in serial
    assert "ranks" in inspect.signature(build).parameters
    assert inspect.signature(build).parameters["ranks"].default == 1


def test_the_parallel_chain_stays_decomposed_to_the_end():
    """THE MERGE MOVED, and this test used to assert the opposite - "the merge before topoSet
    because topoSet runs on one mesh". topoSet runs perfectly well on a decomposed one, and
    merging first put the three stages after the mesher in a single process:

        cartesianMesh        2,399.6 s   on 8 ranks
        reconstructParMesh   4,518.4 s   one process
        topoSet              2,240.2 s   one process
        splitMeshRegions     6,073.3 s   one process

    on the 336,666,582-cell customer build - 4.4x the mesher, after it. So the order is
    preparePar, mesh, tag, split, and THEN one merge, of the two regions rather than the block.
    """
    src = inspect.getsource(build)
    par = src.split("if int(ranks) > 1:", 1)[1].split("else:", 1)[0]
    assert PREPARE_PAR == "preparePar", "cfMesh's own, because there is no mesh to decompose yet"
    order = [par.index(k) for k in ("PREPARE_PAR", '("cartesianMesh"', '("topoSet"',
                                    '("splitMeshRegions"', "RECONSTRUCT_REGIONS")]
    assert order == sorted(order), "preparePar, mesh, tag, split, merge - in that order"
    assert par.count("mpi_command") == 3, "the mesher, topoSet and the split all go parallel"
    assert "RECONSTRUCT_REGIONS" in par and 'RECONSTRUCT,' not in par


def test_the_merge_takes_the_regions_and_not_the_block():
    """The root block mesh is never reconstructed, and nothing downstream wants it: the viewer
    refuses to read it on purpose, and foamToVTK -allRegions was measured on a case without it -
    rc 0, both regions' internal.vtu, 9 boundary files, the same as with it."""
    from meshpipeline.engines.cfmesh_conjugate.conjugate import RECONSTRUCT_REGIONS

    assert RECONSTRUCT_REGIONS.startswith("reconstructParMesh")
    assert "-allRegions" in RECONSTRUCT_REGIONS
    assert "-constant" in RECONSTRUCT_REGIONS


def test_the_regions_dictionaries_are_brought_up_from_the_ranks():
    """splitMeshRegions writes each region's system/<region> beside the mesh it split, which when
    it runs decomposed is inside processor0. Without them the reconstructed case has
    constant/fluid/polyMesh and no system/fluid/fvSchemes, and the next command to open that
    region exits on the missing file - measured: checkMesh -region fluid, "cannot find file
    system/fluid/fvSchemes"."""
    from meshpipeline.engines.cfmesh_conjugate.conjugate import REGION_DICTS

    assert "processor0/system" in REGION_DICTS
    assert " -rn " in REGION_DICTS, (
        "no-clobber, or this overwrites the port cut's own topoSetDict in the same directory")
    src = inspect.getsource(build)
    par = src.split("if int(ranks) > 1:", 1)[1].split("else:", 1)[0]
    assert par.index("RECONSTRUCT_REGIONS") < par.index("REGION_DICTS"), (
        "there is nothing to copy into until the regions exist")


def test_one_cell_may_move_and_the_interface_may_not():
    """What the two chains were measured to agree and disagree about, on the coupon's own block
    and coolant:

                        fluid cells   solid cells   interface   fluid non-ortho   solid
        serial               29,812        29,262      22,534           16.2739   35.3268
        parallel to end      29,813        29,261      22,534           16.2739   35.3442

    Both pass checkMesh. The interface is 22,534 faces on both sides in both chains, which is the
    conformality the whole approach rests on, and the total is 59,074 either way. ONE CELL MOVES
    between the regions, because topoSet classifies a cell on a processor boundary differently
    from how it classifies it with the mesh whole. Recorded here rather than asserted - it needs
    a mesher to reproduce - so that the number is written down where the next person looks.
    """
    src = inspect.getsource(build)
    assert "22,534" in src, "the interface figure belongs beside the chain it justifies"
    assert "ONE CELL" in src and "MOVES between the regions" in src
    assert "59,074" in src


def test_the_dictionary_is_written_before_the_case_is_scanned():
    """scan_case_dicts reads every dictionary in system/ and constant/ and refuses the executable
    constructs. A dictionary written after it is a dictionary nothing checked."""
    src = inspect.getsource(build)
    assert src.index("render_decompose_pardict") < src.index("reason = scan_case_dicts")


def test_the_per_rank_copies_are_removed_once_the_mesh_is_one_mesh():
    """They are a second whole copy of the mesh - hundreds of gigabytes at the sizes that need
    ranks at all - and the workspace is archived and returned whole."""
    src = inspect.getsource(build)
    assert 'glob("processor*")' in src
    assert src.index('glob("processor*")') > src.index("RECONSTRUCT_REGIONS"), (
        "removing them before the merge would delete the mesh")


def test_the_runner_hands_the_plan_s_rank_count_over():
    """THE GAP THIS PACKAGE HAS HAD TWICE. render_meshdict took fine_zones for months while build
    passed none, and a 78-minute run rebuilt an identical mesh and exited 0."""
    from meshpipeline.engines.cfmesh_conjugate import native

    src = inspect.getsource(native._run_conjugate_local)
    assert "ranks=" in src, "the runner has to pass what the plan carries"


#   -- the plan carries it ----------------------------------------------------------------------

def test_a_plan_carries_the_rank_count(tmp_path):
    p = PL.ConjugatePlan(background_cell_m=3.5e-4, levels=1, expect_fluid_volume_m3=5.4e-5,
                         expect_fluid_pieces=1, volume_tol=0.05, narrowest_m=2.1e-3, ranks=8)
    PL.write_plan(tmp_path, p)
    assert PL.read_plan(tmp_path).ranks == 8


def test_a_plan_that_says_nothing_about_ranks_means_one(tmp_path):
    """Every plan written before this existed, and every part that does not need it."""
    p = PL.ConjugatePlan(background_cell_m=3.5e-4, levels=1, expect_fluid_volume_m3=5.4e-5,
                         expect_fluid_pieces=1, volume_tol=0.05, narrowest_m=2.1e-3)
    PL.write_plan(tmp_path, p)
    assert PL.read_plan(tmp_path).ranks == 1


@pytest.mark.parametrize("bad", [0, -1, -8])
def test_a_plan_with_no_process_at_all_is_refused(tmp_path, bad):
    import json

    (tmp_path / PL.PLAN_NAME).write_text(json.dumps({
        "background_cell_m": 3.5e-4, "expect_fluid_volume_m3": 5.4e-5, "narrowest_m": 2.1e-3,
        "ranks": bad}))
    with pytest.raises(ValueError, match="at least one process"):
        PL.read_plan(tmp_path)
