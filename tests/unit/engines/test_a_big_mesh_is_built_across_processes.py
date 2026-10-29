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
    omp_threads,
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
    cmd = mpi_command("cartesianMesh", 8, threads=1)
    assert cmd == ("MPI_BUFFER_SIZE=1000000000 OMP_NUM_THREADS=1 mpirun --allow-run-as-root "
                   "--oversubscribe -x MPI_BUFFER_SIZE -x OMP_NUM_THREADS "
                   "-np 8 cartesianMesh -parallel")
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
    rc 0, both regions' internal.vtu, 9 boundary files, the same as with it.

    ONE MERGE PER REGION, NAMED, and the two forms that read like it do not work. Measured three
    ways on the same processor directories of a 182,046,234-cell build (mesh-bench, 2026-10-29) -
    the controlled comparison, because the disk could not afford a second mesh:

        -regions '(fluid solid)'   2,039 s   constant/polyMesh 39 GB   and NEITHER region
        -region fluid                824 s   constant/fluid    16 GB
        -region solid              1,169 s   constant/solid    23 GB

    `-allRegions` means "Use all regions in regionProperties", and build() writes that file at the
    END, after the merge - so it falls back to every mesh it can find, the pre-split block first.
    `-regions` is accepted and ignored, which OpenFOAM says in one line: "Warning: No
    regionProperties, assume default region". Both reach the regions eventually, so every parallel
    build delivered; both spent the block's time first, and in processor0 the block is 5.7 GB
    against fluid's 2.4 and solid's 2.9.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import (
        FLUID_REGION, RECONSTRUCT_REGIONS, SOLID_REGION)

    assert RECONSTRUCT_REGIONS.startswith("reconstructParMesh")
    assert RECONSTRUCT_REGIONS.count("-constant") == 2
    assert f"-region {FLUID_REGION}" in RECONSTRUCT_REGIONS
    assert f"-region {SOLID_REGION}" in RECONSTRUCT_REGIONS
    assert "-allRegions" not in RECONSTRUCT_REGIONS
    assert "-regions" not in RECONSTRUCT_REGIONS, (
        "accepted and ignored: 'Warning: No regionProperties, assume default region'")


def test_a_failed_first_merge_stops_the_second():
    """`&&`, not `;`. The stage's exit code has to be the one that failed, or a half-merged
    delivery walks into the region check with a story about what went wrong."""
    from meshpipeline.engines.cfmesh_conjugate.conjugate import RECONSTRUCT_REGIONS

    assert " && " in RECONSTRUCT_REGIONS
    assert ";" not in RECONSTRUCT_REGIONS


def test_the_two_merges_are_one_stage_because_the_customer_is_watching():
    """`engines/mesher_stage` maps the stage NAME to what is shown while it runs. Splitting this
    into two chain entries would say "joining the pieces back together" twice."""
    import inspect

    from meshpipeline.engines.mesher_stage import _TOOLS

    assert _TOOLS["reconstructParMesh"][1] == "joining the pieces back together"
    src = inspect.getsource(build)
    par = src.split("if int(ranks) > 1:", 1)[1].split("else:", 1)[0]
    assert par.count("RECONSTRUCT_REGIONS") == 1, "one entry, so the stage is announced once"


def test_the_merge_runs_before_the_file_that_would_tell_it_the_regions():
    """Why `-allRegions` cannot name the two regions here: regionProperties is the LAST thing
    build() writes, so anything in the chain that reads it reads nothing. Written down because it
    is the thing a future fix has to deal with - either move this write, or name the regions in a
    way the tool honours."""
    import inspect

    src = inspect.getsource(build)
    at_merge = src.index("RECONSTRUCT_REGIONS")
    at_props = src.index("REGION_PROPERTIES")
    assert at_merge < at_props, "the merge runs before regionProperties exists"


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


#   -- the ranks do not fight over the cores ----------------------------------------------------

def test_one_thread_a_rank_when_there_are_as_many_ranks_as_cores():
    """THE RUN THAT PAID FOR THIS. 279,621,370 cells, eight ranks on mesh-bench's 8 vCPU: each
    rank spawned ten threads, 84 in all, every rank got about one core's worth of CPU and spent
    it on OpenMP barriers. 180,053 CPU-seconds in 22,536 s is 8.0 cores saturated, and after six
    hours fifteen the mesher had not finished - against 640 s of ClockTime for a 98,973,937-cell
    build of the same part in ONE process."""
    assert omp_threads(8, cores=8) == 1


def test_a_box_with_cores_to_spare_still_threads_each_rank():
    """The setting must not cost anything where OpenMP was helping: cfMesh gets 2.5x out of 4
    cores in one process, and that is worth having in each rank when the cores exist."""
    assert omp_threads(2, cores=32) == 16
    assert omp_threads(4, cores=8) == 2


def test_more_ranks_than_cores_is_still_one_thread_and_not_zero():
    """`ranks_for` returns what the octree CEILING demands, not what the box has - so a plan can
    ask for more ranks than there are cores, and `cores // ranks` is 0 there. OMP_NUM_THREADS=0
    is not a floor, it is OpenMP being told to pick for itself again."""
    assert omp_threads(16, cores=8) == 1
    assert omp_threads(64, cores=1) == 1


def test_a_serial_build_is_not_touched_by_any_of_this():
    """One process should use every core it can see, which is the default - and the serial chain
    does not go through mpi_command at all."""
    assert omp_threads(1, cores=8) == 8
    # sliced the way the serial-chain test above slices it: maxsplit=1, because `build` guards on
    # the rank count three times and the chunk between two guards is not the branch.
    serial = inspect.getsource(build).split("else:", 1)[1].split("if port_patches", 1)[0]
    assert "OMP_NUM_THREADS" not in serial and "mpi_command" not in serial


def test_the_thread_count_travels_to_the_ranks_and_not_just_to_mpirun():
    """Same trap as MPI_BUFFER_SIZE: an assignment in front of mpirun sets the variable for
    mpirun and for nothing it launches. Each rank reads its own environment."""
    cmd = mpi_command("cartesianMesh", 8, threads=1)
    assert "OMP_NUM_THREADS=1 mpirun" in cmd, "the launching shell has to have it"
    assert "-x OMP_NUM_THREADS" in cmd, "...and it has to be exported to the ranks"


def test_every_stage_of_the_parallel_chain_is_pinned_the_same_way():
    """topoSet and splitMeshRegions are not OpenMP, so this costs them nothing - but they go
    through the same builder, and a stage that quietly did not would be the one to find out."""
    for script in ("cartesianMesh", "topoSet", "splitMeshRegions -cellZonesOnly -overwrite"):
        assert "-x OMP_NUM_THREADS" in mpi_command(script, 8)


def test_the_cores_counted_are_the_jobs_and_not_the_machines(monkeypatch):
    """THE TRAP IS NAMED IN THE HELPER'S OWN DOCSTRING: "Cloud Run hands the process a wide mask
    and os.cpu_count() reports the host". Counting the host there would give every rank as many
    threads as the machine has cores while the cgroup gives the container a handful, which is the
    exact oversubscription this exists to stop, on the platform where it is hardest to see."""
    import meshpipeline.engines.determinism as D

    monkeypatch.setattr(D, "job_cpus", lambda: 4)      # the container's share
    monkeypatch.setattr("os.cpu_count", lambda: 64)    # the host underneath it
    assert omp_threads(4) == 1, "it counted the machine"
    assert omp_threads(2) == 2


def test_a_cpu_count_that_cannot_be_read_still_gives_a_thread_count(monkeypatch):
    """A thread count never costs a build. If the cgroup read raises for any reason the fallback
    is the old answer, not an exception out of command construction."""
    import meshpipeline.engines.determinism as D

    def boom():
        raise OSError("no cgroup here")

    monkeypatch.setattr(D, "job_cpus", boom)
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert omp_threads(8) == 1
    assert omp_threads(1) == 8
