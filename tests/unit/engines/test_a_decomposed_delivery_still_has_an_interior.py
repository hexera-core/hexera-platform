"""A decomposed delivery still has an interior, and the review is entitled to it.

Job 110daca9, 2026-11-24, meshed the Corintis cold plate to 279,281,786 cells, passed all eight
gates, and shipped `review_inconclusive`. The whole of the cause is one command:

    foamToVTK -constant -noZero -noFaceZones -allRegions
    --> FOAM FATAL ERROR: Cannot find file "points" in directory "fluid/polyMesh"

This engine delivers the regions decomposed across N ranks with no `reconstructParMesh`, so
`constant/<region>/polyMesh` carries a patch table and no cells, and the serial export cannot read
it. Everything downstream is a consequence, not a second fault:

    export_volume_vtk -> None
    native._review_aids  has nothing to pv.read, so not one cut is drawn
    manifest._regions_published  falls to [] for want of BOTH a volume and a picture
    the reviewer is asked for one region target and the mesh produces none
    reviewer_evidence_missing

The engine's own note rules out the obvious repair: "`foamToVTK -parallel` is not the way out
because every consumer of its output is written for the serial layout" (native.py). A processorN
directory IS a serial case, though, so each rank is exported on its own and the pieces are merged
into the one serial-layout file every consumer already expects.

MEASURED against the real thing, not only here. On a 4-rank blockMesh of 8,000 cells the merged
export is 8,000 cells exactly - none lost at a rank boundary, none counted twice. On two of job
110daca9's own eight delivered ranks it merged 21,710,181 real cells into a 574,340,404-byte file
in 451 s at 4.09 GB of peak RSS, and a plane through it cut 1,170 cells.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE

REGION = "fluid"


class _FakeMesh:
    """Enough of a pyvista mesh for the merge: a cell count, a merge and a save."""

    def __init__(self, cells: int) -> None:
        self.n_cells = cells

    def merge(self, other: _FakeMesh) -> _FakeMesh:
        return _FakeMesh(self.n_cells + other.n_cells)

    def save(self, path: str) -> None:
        # THE COUNT IS WHAT IS WRITTEN, so the merged file can be read back and checked. A save
        # that wrote a placeholder let the arithmetic of the merge go untested.
        Path(path).write_text(str(self.n_cells), encoding="utf-8")


class _FakePv:
    """`pv.read` of a rank piece. The cell count rides in the file so the merge is checkable."""

    @staticmethod
    def read(path: str) -> _FakeMesh:
        return _FakeMesh(int(Path(path).read_text(encoding="utf-8").strip() or 0))


def _lay_out_a_decomposed_delivery(root: Path, *, ranks: int = 3, region: str = REGION) -> Path:
    """The shape a delivered decomposed mesh has, and the shape that broke the review.

    The root region mesh has its `boundary` - it is the PUBLISHED patch table, which is why
    nothing here may rewrite it - and no points. The cells are in the processor directories.
    """
    (root / "constant" / region / "polyMesh").mkdir(parents=True)
    (root / "constant" / region / "polyMesh" / "boundary").write_text("3(...)", encoding="utf-8")
    (root / "system").mkdir(exist_ok=True)
    for r in range(ranks):
        (root / f"processor{r}" / "constant" / region / "polyMesh").mkdir(parents=True)
        (root / f"processor{r}" / "constant" / region / "polyMesh" / "points").write_text(
            "x", encoding="utf-8")
    return root


def _lay_out_a_merged_delivery(root: Path, *, region: str = REGION) -> Path:
    """A reconstructed case: the root region mesh has cells, so the serial command is right."""
    (root / "constant" / region / "polyMesh").mkdir(parents=True)
    for f in ("boundary", "points", "faces", "owner", "neighbour"):
        (root / "constant" / region / "polyMesh" / f).write_text("x", encoding="utf-8")
    (root / "system").mkdir(exist_ok=True)
    return root


def _foam_that_writes(cells_per_rank: int = 1000, *, skip: str = "", raises: bool = False):
    """A stand-in for foamToVTK that leaves behind what foamToVTK leaves behind.

    The export globs for `internal.vtu` AFTER the command returns, so a fake that only records
    the command tests half of it. `skip` names a rank that exports nothing, which is the case
    that must not become a merged mesh missing a piece of itself.
    """
    calls: list[dict] = []

    def fake(cmd, **kw):
        text = cmd[-1]
        calls.append({"cmd": text, "timeout": kw.get("timeout")})
        if raises:
            raise TimeoutError("stopped")
        case = text.split("-case '", 1)[1].split("'", 1)[0] if "-case '" in text else ""
        name = Path(case).name
        if not case or name == skip:
            return None
        out = Path(case) / FE.RANK_VTK_DIRNAME / REGION / f"{name}_0"
        out.mkdir(parents=True, exist_ok=True)
        (out / "internal.vtu").write_text(str(cells_per_rank), encoding="utf-8")
        out.with_suffix(FE.VTM_SUFFIX).write_text("<VTKFile>", encoding="utf-8")
        return None

    fake.calls = calls           # type: ignore[attr-defined]
    return fake


def _a_hundred_seconds_a_rank(monkeypatch, foam):
    """One worker, and a clock that moves only when a rank is exported - 100 s per rank.

    BOTH HALVES ARE NEEDED TO SEE A SHARED BUDGET AT ALL. Run the ranks four at a time and they
    read the clock at the same instant and all get the same figure, which proves nothing either
    way; so the pool is forced down to one and the exports are put in a line. And a clock driven by
    the NUMBER OF CALLS to `monotonic` would be nondeterministic here, because the worker thread
    and the main loop both read it and the interleaving is the pool's business - so the fake export
    is what advances it, and every reader in between sees the same time.
    """
    clock = [0.0]

    def fake(cmd, **kw):
        try:
            return foam(cmd, **kw)
        finally:
            clock[0] += 100.0

    fake.calls = foam.calls          # type: ignore[attr-defined]
    monkeypatch.setattr(FE, "run_guarded", fake)
    monkeypatch.setattr(FE, "export_workers", lambda n: 1)
    monkeypatch.setattr(FE.time, "monotonic", lambda: clock[0])
    return fake


@pytest.fixture
def pv_patched(monkeypatch):
    """The merge runs for real against a fake pyvista, through the import the code actually uses."""
    import meshpipeline.sandbox.backend as backend
    monkeypatch.setattr(backend, "pv", _FakePv(), raising=False)
    return backend


def test_a_decomposed_delivery_does_not_take_the_serial_command(tmp_path, monkeypatch, pv_patched):
    """The command that exited FATAL on job 110daca9 must not be the one that runs."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    for call in foam.calls:
        assert "-allRegions" not in call["cmd"], (
            "-allRegions is what reads constant/<region>/polyMesh, which on this path has no cells")
        assert "-case '" in call["cmd"], "each rank is exported as its own serial case"
        assert f"-region {REGION}" in call["cmd"]


def test_every_rank_is_exported(tmp_path, monkeypatch, pv_patched):
    """All of them: a mesh missing rank 5 is not the mesh that was delivered.

    THE SET AND NOT THE SEQUENCE, because the ranks are exported several at a time now and the
    order they reach `run_guarded` in is the thread pool's business. What must still be ordered is
    the MERGE, which is asserted on its own below - that is where order changes the bytes.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    cases = [c["cmd"].split("-case '", 1)[1].split("'", 1)[0] for c in foam.calls]
    assert sorted(Path(c).name for c in cases) == ["processor0", "processor1", "processor2",
                                                   "processor3"]


def test_the_pieces_are_merged_in_rank_order(tmp_path, monkeypatch):
    """The ranks FINISH in whatever order they finish; they are MERGED in rank order.

    This is the one place concurrency could have changed the delivered bytes. `pieces` is built by
    indexing `done` with `ranks`, not by appending as futures complete, and this is the assertion
    that says so: the reads happen in processor0..3 order however the exports raced.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    read: list[str] = []

    class _Recording:
        @staticmethod
        def read(path: str) -> _FakeMesh:
            read.append(Path(path).parts[-5])          # processorN/VTKRANK/fluid/<time>/internal
            return _FakeMesh(int(Path(path).read_text(encoding="utf-8").strip() or 0))

    import meshpipeline.sandbox.backend as backend
    monkeypatch.setattr(backend, "pv", _Recording(), raising=False)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert read == ["processor0", "processor1", "processor2", "processor3"], read


def test_a_merged_delivery_still_takes_the_command_it_was_measured_with(tmp_path, monkeypatch):
    """A reconstructed case is untouched by this: one command, -allRegions, no -case."""
    ws = _lay_out_a_merged_delivery(tmp_path)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert len(foam.calls) == 1
    assert "-allRegions" in foam.calls[0]["cmd"]
    assert "-case '" not in foam.calls[0]["cmd"]


def test_a_merged_case_that_also_has_processor_dirs_stays_serial(tmp_path, monkeypatch):
    """Processor directories alone do not decide it - the root mesh having cells does.

    A case can carry both, and the serial export is right whenever there is a root mesh to read.
    """
    ws = _lay_out_a_merged_delivery(tmp_path)
    (ws / "processor0" / "constant" / REGION / "polyMesh").mkdir(parents=True)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert len(foam.calls) == 1
    assert "-case '" not in foam.calls[0]["cmd"]


def test_the_merged_file_lands_where_the_serial_export_would_have_left_one(
        tmp_path, monkeypatch, pv_patched):
    """`newest()` globs VTK/<region>/*/internal.vtu, so the merged file is one of those."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    out = FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert out == f"VTK/{REGION}/{FE.MERGED_TIME_DIRNAME}/internal.vtu"
    assert (ws / out).is_file()
    assert list((ws / "VTK" / REGION).glob("*/internal.vtu")) == [ws / out]


def test_the_merged_export_carries_the_finished_marker(tmp_path, monkeypatch, pv_patched):
    """A merged export dies partway exactly as foamToVTK's does, so it earns the same marker."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    out = FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    time_dir = (ws / out).parent
    assert FE.finished_export(time_dir), "prefer_finished would otherwise fall back blindly"
    assert FE.prefer_finished([time_dir]) == [time_dir]
    assert "internal.vtu" in time_dir.with_suffix(FE.VTM_SUFFIX).read_text(encoding="utf-8")


def test_every_rank_counts_toward_the_merged_mesh(tmp_path, monkeypatch, pv_patched):
    """Measured for real at 8,000 of 8,000 cells; here the arithmetic is the assertion."""
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes(cells_per_rank=1234))
    out = FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert int((ws / out).read_text(encoding="utf-8")) == 4 * 1234, (
        "a rank dropped at the merge is a coolant with a piece missing, which reviews as a "
        "coolant")


def test_the_rank_pieces_are_removed_once_they_are_merged(tmp_path, monkeypatch, pv_patched):
    """They are a second copy of every cell in the region - 7.2 GB at this plate's size."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert not list(ws.glob(f"processor*/{FE.RANK_VTK_DIRNAME}"))


def test_a_rank_that_exported_nothing_is_not_half_a_mesh(tmp_path, monkeypatch, pv_patched, caplog):
    """Seven eighths of a coolant would review as a coolant. It must not be published as one."""
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes(skip="processor2"))
    with caplog.at_level(logging.ERROR):
        assert FE.export_volume_vtk(ws, all_regions=True, region=REGION) is None
    assert "processor2" in caplog.text
    assert not (ws / "VTK" / REGION / FE.MERGED_TIME_DIRNAME).exists()


def test_a_rank_that_stops_never_raises_out_of_the_export(tmp_path, monkeypatch, pv_patched,
                                                          caplog):
    """A review aid may never cost a mesh that is already built.

    THE RANK IS NAMED, AND WHICH ONE IS THE POOL'S BUSINESS. Every rank raises here, and the first
    failure the loop observes is whichever future completes first - so the assertion is that a rank
    is named at all, which is the thing that was missing when job 110daca9's review was lost without
    a line about it. `test_a_rank_that_exported_nothing_is_not_half_a_mesh` names a specific rank,
    because there exactly one fails.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes(raises=True))
    with caplog.at_level(logging.ERROR):
        assert FE.export_volume_vtk(ws, all_regions=True, region=REGION) is None
    assert any(f"processor{r}" in caplog.text for r in range(3)), caplog.text
    assert "the mesh stands" in caplog.text


def test_a_decomposed_case_with_no_region_named_runs_nothing(tmp_path, monkeypatch, caplog):
    """The serial command would spend 90 s reaching the same FATAL. Said once instead."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    with caplog.at_level(logging.ERROR):
        assert FE.export_volume_vtk(ws, all_regions=True, region="") is None
    assert not foam.calls
    assert "decomposed" in caplog.text


def test_the_ranks_share_one_timeout(tmp_path, monkeypatch, pv_patched):
    """Four exports inside one budget, not four budgets: every rank's life is derived from it.

    DRIVEN THROUGH ONE WORKER AND A CLOCK THAT IS TOLD WHAT TIME IT IS, because that is the only
    way the sequence is observable at all now - run four at a time and they all read the clock at
    the same instant and all get the same budget, which proves nothing either way. One worker and a
    clock advancing 100 s a call puts the four exports in a line, and then a budget that failed to
    carry over would show up as a figure growing back.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    foam = _a_hundred_seconds_a_rank(monkeypatch, _foam_that_writes())
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION, timeout=600.0)
    seen = [c["timeout"] for c in foam.calls]
    assert seen == [600.0, 500.0, 400.0, 300.0], seen
    assert seen == sorted(seen, reverse=True), f"the budget must not grow back: {seen}"
    assert seen[-1] < seen[0], f"a fresh budget per rank would be four 600s: {seen}"


def test_a_rank_whose_turn_comes_after_the_budget_is_spent_never_runs(
        tmp_path, monkeypatch, pv_patched, caplog):
    """The budget is a bound on the PASS and not on each rank, so it has to be able to stop one.

    Same single worker, same told clock, and a budget of 150 s against 100 s a rank: the first rank
    runs, the second runs with 50 s left, and the third's turn arrives with nothing left. It must
    not start, and the export must come back None with the rank named rather than publish three
    quarters of a coolant.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    foam = _a_hundred_seconds_a_rank(monkeypatch, _foam_that_writes())
    with caplog.at_level(logging.ERROR):
        assert FE.export_volume_vtk(ws, all_regions=True, region=REGION, timeout=150.0) is None
    assert [c["timeout"] for c in foam.calls] == [150.0, 50.0], (
        "the third rank's turn came with nothing left and it had to not start")
    assert "ran out of its 150 s" in caplog.text and "never started" in caplog.text, caplog.text
    # AND IT MUST NOT SEND A READER TO A FILE THAT DOES NOT EXIST. foamToVTK never ran for that
    # rank, so there is no `foamToVTK_processorN.log` and the generic "... has what it printed"
    # sentence would be false. This assertion is the whole of why `_NeverStarted` is its own type.
    assert "foamToVTK stopped on" not in caplog.text, caplog.text
    assert "has what it printed" not in caplog.text, caplog.text
    for rank in sorted(ws.glob("processor*")):
        assert not (ws / f"foamToVTK_{rank.name}.log").exists() or rank.name in (
            "processor0", "processor1"), (
            f"{rank.name} has a log and never ran")
    assert not (ws / "VTK" / REGION / FE.MERGED_TIME_DIRNAME).exists()


def test_the_ranks_are_exported_at_the_same_time(tmp_path, monkeypatch, pv_patched):
    """THE CHANGE ITSELF. Eight ranks one after another was 549.6 s on job b8f09ae9, of which
    540.81 s was summed rank time - no concurrency at all, on a 64-core machine.

    Asserted by observation and not by a clock: each fake export blocks on a barrier that only
    releases once all four have arrived, so the call can only return if all four were in flight
    together. A serial loop deadlocks here, which is the strongest form this can take.
    """
    import threading

    ws = _lay_out_a_decomposed_delivery(tmp_path, ranks=4)
    writes = _foam_that_writes()
    gate = threading.Barrier(4, timeout=30)

    def fake(cmd, **kw):
        gate.wait()                      # BrokenBarrierError on timeout -> the test fails, not hangs
        return writes(cmd, **kw)

    monkeypatch.setattr(FE, "run_guarded", fake)
    monkeypatch.setattr(FE, "export_workers", lambda n: n)
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert len(writes.calls) == 4


def test_the_concurrency_is_bounded_by_the_cores_and_by_the_free_memory(monkeypatch):
    """`export_workers` is the whole bound, and both halves of it are load-bearing.

    foamToVTK is single-threaded, so the cores say how many make progress; measured at 8.2-8.4 GiB
    of peak RSS a rank (job b8f09ae9's eight exports), the free memory says how many fit. The mesh
    VM is 64 cores and 512 GiB, where the cores bind - a small box is the other way round, and this
    function is reached on a FAILURE path, so taking the machine down for a review aid would be the
    worst trade available.
    """
    gib = 1024 ** 3
    monkeypatch.setattr(FE, "memory_in_use", lambda: None)
    monkeypatch.setattr(FE, "job_cpus", lambda: 64)
    assert FE.export_workers(8) == 8, "the measured VM runs all eight"
    assert FE.export_workers(1) == 1, "never more workers than there are ranks"
    monkeypatch.setattr(FE, "job_cpus", lambda: 4)
    assert FE.export_workers(8) == 4, "four cores, four at a time"
    # 100 GiB free at 9 GiB a rank is eleven, which does not bind at four cores...
    monkeypatch.setattr(FE, "memory_in_use", lambda: (412 * gib, 512 * gib))
    assert FE.export_workers(8) == 4
    # ...and 20 GiB free is two, which does.
    monkeypatch.setattr(FE, "memory_in_use", lambda: (492 * gib, 512 * gib))
    assert FE.export_workers(8) == 2
    # A box with nothing spare still runs one: a serial export is what this replaced, and it is
    # better than no interior for the review.
    monkeypatch.setattr(FE, "memory_in_use", lambda: (512 * gib, 512 * gib))
    assert FE.export_workers(8) == 1
    # And a cgroup that cannot be read is not a bound of zero.
    monkeypatch.setattr(FE, "memory_in_use", lambda: None)
    monkeypatch.setattr(FE, "job_cpus", _raises)
    assert FE.export_workers(8) == 8


def _raises():
    raise OSError("no cgroup here")


def test_the_export_asks_for_neither_fields_nor_boundary(tmp_path, monkeypatch, pv_patched):
    """The consumer slices this file; the boundary reaches the reviewer by the index instead."""
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    foam = _foam_that_writes()
    monkeypatch.setattr(FE, "run_guarded", foam)
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert "-no-fields" in foam.calls[0]["cmd"]
    assert "-no-boundary" in foam.calls[0]["cmd"]
    assert FE.NO_FIELD_TIME in foam.calls[0]["cmd"]


def test_nothing_rewrites_the_published_patch_table(tmp_path, monkeypatch, pv_patched):
    """`reconstructParMesh` is the other repair and this is why it is not taken.

    On a decomposed delivery `constant/<region>/polyMesh/boundary` is the published patch table -
    the file `open_index` stamps the boundary-index sidecar against, where a 64-byte difference
    already dropped a coolant once. The export may not touch it.
    """
    ws = _lay_out_a_decomposed_delivery(tmp_path)
    table = ws / "constant" / REGION / "polyMesh" / "boundary"
    before = table.read_bytes()
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    assert FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert table.read_bytes() == before
    assert not (ws / "constant" / REGION / "polyMesh" / "points").exists()


# ---------------------------------------------------------------------------------------------
# THE CONSUMER SIDE. Everything above is about the writer. The merged file is only worth writing
# if the code that draws the reviewer's interior cuts opens it, and that code does not take the
# path it is handed on trust: `review_surface.review_volume` re-derives one by CONVENTION, from
# the time directories it finds in VTK/<region>. `decomposed_0` satisfies that convention by two
# properties of its NAME which nothing on the consumer side states - it ends in "_<n>", so
# `_time_index` scores it 0 rather than -1, and it is dot-free, so `with_suffix` turns it into
# `decomposed_0.vtm` and the finished marker is found. Rename the constant to "merged" and the
# index becomes -1; both halves still happen to work, and that is exactly the kind of accident
# worth pinning rather than arguing about.


def test_the_name_the_merge_writes_scores_as_a_time_directory():
    from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

    assert RS._time_index(FE.MERGED_TIME_DIRNAME) == 0, (
        f"{FE.MERGED_TIME_DIRNAME!r} does not read as a time directory, so _newest_time would "
        "rank it below any numeric export beside it")


def test_the_review_reads_back_exactly_the_path_the_export_returned(tmp_path, monkeypatch,
                                                                    pv_patched):
    """The one test that spans the seam: write a merged interior, then ask the review for it."""
    from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    wrote = FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    assert wrote == f"VTK/{REGION}/{FE.MERGED_TIME_DIRNAME}/internal.vtu"
    assert RS.review_volume(ws) == wrote, (
        "the interior was written and the review derived a different path for it")


def test_a_merged_export_with_no_marker_is_still_read(tmp_path, monkeypatch, pv_patched):
    """`prefer_finished` prefers and does not require: a missing marker may not cost a readable
    interior. This is the half of the naming accident that is documented as deliberate."""
    from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

    ws = _lay_out_a_decomposed_delivery(tmp_path)
    monkeypatch.setattr(FE, "run_guarded", _foam_that_writes())
    wrote = FE.export_volume_vtk(ws, all_regions=True, region=REGION)
    marker = ws / "VTK" / REGION / f"{FE.MERGED_TIME_DIRNAME}.vtm"
    assert marker.is_file()
    marker.unlink()
    assert RS.review_volume(ws) == wrote
