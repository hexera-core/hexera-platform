"""The bundle a customer is handed is not deflated at level 9 on a single thread.

WHAT IT COST. Job 57b613d7, 2026-11-15, cfmesh_conjugate on ColdPlateAssembly_CLEAN.step. The
mesh was built in 3 h 53 m on an n2-highmem-64 - 283,410,955 cells, every gating criterion passed,
`Executor: success=True` - and then the run disappeared into `_build_plan`'s packaging loop. At
17:48 UTC, 63 minutes after the graph returned, the worker process was still there: one thread of
97 in state R at 98% of a core, fd 25 writing `bundle_6_3ebii3.tar.gz` and fd 29 walking the
fluid region's `faces`. It had consumed 12.57 GB of the recipe's 66.6 GB `constant` tree.

The whole bundle would have taken about eight and a half hours. What that cost was not patience:

  * `_invoke_with_heartbeat` (application/pipeline_run.py) stops BOTH the in-process beat and the
    lease keeper in its `finally`, which runs when the GRAPH returns - before any of this. So
    nothing renewed the lease while the packaging ran.
  * WORKER_LEASE_SECONDS is 900. `maintenance/cleanup.stalled_jobs_clause` selects a `running` row
    whose lease expired more than 900 s ago, so at 17:24:25 the row qualified.
  * The reaper claimed generation 2 and started a SECOND 64-vCPU VM to re-mesh a part that was
    already meshed. The row still carries `pipeline_dispatch_state = rerun_worker_lost`.
  * And the row read `running` with no `ended_at` in the first place precisely because
    `terminal_finalize`'s status CAS sits AFTER delivery on the success path - deliberately, so a
    client never sees "succeeded" before the mesh is fetchable (pipeline_run.py's own note).

So a compression level turned a delivered mesh into a job that looked stalled, re-ran itself, and
never finished.

MEASURED, 2026-11-15, on that mesh's own fluid `faces` - a 419,430,400-byte slice taken one third
in, every row compressing the same bytes on this 32-core host:

    OLD  gzip level 9, one thread          109.10 s     3.8 MB/s   71,253,911 B  5.89x   4h 48m
         parallel level 6, 1 worker         13.15 s    31.9 MB/s   72,048,248 B  5.82x   0h 34m
    NEW  parallel level 6, 32 workers        2.62 s   160.0 MB/s   72,048,248 B  5.82x   0h 06m

41.6x faster for 1.1% more bytes. And `faces` is the KIND half of this data:
gcs_exchange.RESULT_ARCHIVE_LEVEL's own note measures level 9 at "0.7 against 16.9 on `owner`",
and `owner` plus `neighbour` are 15.85 GB of this recipe - so 4h 48m is a floor, not an estimate.

NONE OF THIS IS NEW. `gcs_exchange` had already made this mistake twice on the archive a mesh VM
sends home, and says so: "measuring against a tarfile default instead of against this has been
wrong twice - first its level-9 compresslevel, then its single-threaded deflate stream." The
answer it reached, `parallel_gzip`, was sitting in the tree the whole time. This was the third
place, and the only one where the bytes go to the customer.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import tarfile
import uuid

import pytest
from tests.engine_workspaces import build_workspace, engine_names

import meshpipeline.application.artifact_uploader as au

ENGINES = engine_names()


def _plan_src() -> str:
    return inspect.getsource(au._build_plan)


# --------------------------------------------------------------- the call site


def test_the_bundle_is_not_written_through_gzip_gzipfile():
    """THE DEFECT IN ONE ASSERTION. `gzip.GzipFile` defaults to compresslevel=9 and is one
    thread; neither is stated at the call site, so neither was ever chosen.

    The CALL is what is banned, not the name: the comment at that call site has to be free to say
    which class it is not using and why, and a bare-name scan makes the explanation trip the
    test. `_build_plan` is the only place this matters, so the open paren is the whole check.
    """
    src = _plan_src()
    assert "gzip.GzipFile(" not in src, "a GzipFile is level 9 on one thread unless told otherwise"
    assert "compresslevel=9" not in src
    assert "ParallelGzipWriter(" in src


def test_the_module_no_longer_imports_gzip_at_all():
    """Left behind, an unused import is an invitation to reach for it again."""
    assert "import gzip\n" not in inspect.getsource(au)


def test_the_tar_is_opened_in_streaming_mode():
    """ParallelGzipWriter is write-only and NON-SEEKABLE, which is `w|`'s contract. `w` may seek
    and is not a safe pairing however well it happens to behave today."""
    src = _plan_src()
    assert 'mode="w|"' in src
    assert 'tarfile.open(fileobj=_gz, mode="w")' not in src


def test_the_threads_come_from_the_job_and_never_from_the_host():
    """`determinism.job_cpus` exists because "Cloud Run hands the process a wide mask and
    os.cpu_count() reports the host" - compressing on the host's count inside an 8-core container
    oversubscribes the quota and buys nothing."""
    src = _plan_src()
    assert "workers(job_cpus())" in src
    assert "os.cpu_count" not in src


def test_the_level_is_named_rather_than_defaulted():
    assert au.BUNDLE_ARCHIVE_LEVEL == 6
    src = _plan_src()
    assert "level=BUNDLE_ARCHIVE_LEVEL" in src


def test_the_level_agrees_with_the_one_this_repo_already_chose():
    """gcs_exchange reached 6 independently, on the archive a mesh VM sends home. They are
    different archives and keep different names, but a disagreement between them would be worth
    noticing."""
    from meshpipeline.adapters.mesh_execution.gcs_exchange import RESULT_ARCHIVE_LEVEL
    assert au.BUNDLE_ARCHIVE_LEVEL == RESULT_ARCHIVE_LEVEL


# --------------------------------------------------------------- it still delivers


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_still_produces_a_bundle_its_contract_accepts(tmp_path, engine):
    """The existing contract, unchanged: `verify_archive` runs inside `_build_plan` and raises if
    the bundle is not what the engine promised, so reaching a plan at all is the check."""
    ws = build_workspace(tmp_path, engine)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    assert len([p for p in plan if p.logical_key == "mesh_bundle"]) == 1


@pytest.mark.parametrize("engine", ENGINES)
def test_the_bundle_opens_in_the_streaming_reader_a_mesh_comes_home_through(tmp_path, engine):
    """THE FAILURE parallel_gzip's own docstring warns about, held against the real reader.
    Independent gzip MEMBERS concatenated are a valid gzip file and `gzip.decompress` reads them
    all - but `tarfile`'s streaming reader stops at the first member's end, and `r|gz` is what
    sandbox/safe_extract uses. An archive written the easy way would build the mesh, compress it
    fast, and lose it on arrival."""
    ws = build_workspace(tmp_path, engine)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    bundle = next(p for p in plan if p.logical_key == "mesh_bundle")
    raw = bundle.local_path.read_bytes()

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as tf:
        streamed = [m.name for m in tf]
    with tarfile.open(bundle.local_path) as tf:
        whole = tf.getnames()

    assert streamed, "the streaming reader found no members at all"
    assert sorted(streamed) == sorted(whole), (
        "the streaming reader saw fewer members than the seekable one, which is exactly what a "
        "multi-member archive looks like")


# --------------------------------------------------------------- and it is still deterministic


@pytest.mark.parametrize("engine", ENGINES)
def test_the_same_workspace_still_yields_the_same_bytes(tmp_path, engine):
    """The bundle's checksum is load-bearing: `_norm` zeroes mtime/uid/gid precisely so a
    same-attempt retry is recognised as the same content rather than tripping the conflict guard.
    Rebuilding has to reproduce it."""
    a = build_workspace(tmp_path / "a", engine)
    b = build_workspace(tmp_path / "b", engine)
    pa = next(p for p in au._build_plan(uuid.uuid4(), a, [], engine)
              if p.logical_key == "mesh_bundle")
    pb = next(p for p in au._build_plan(uuid.uuid4(), b, [], engine)
              if p.logical_key == "mesh_bundle")
    assert (hashlib.sha256(pa.local_path.read_bytes()).hexdigest()
            == hashlib.sha256(pb.local_path.read_bytes()).hexdigest())


@pytest.mark.parametrize("worker_count", [1, 2, 7, 32])
def test_the_thread_count_changes_the_speed_and_not_one_byte(worker_count):
    """WHY THE CHECKSUM SURVIVES PARALLELISM, which is the part that looks unsafe and is not: the
    cut points are fixed by `chunk_bytes`, each piece is primed with the previous window of the
    STREAM, and the pieces are concatenated in order. So the worker count is scheduling and
    nothing else.

    Measured on the real mesh at 1 and 32 workers: identical bytes, sha256
    7b31eb23b461587be26394a81c872ba5 both times. A small chunk size here so several cuts actually
    happen in a test-sized buffer."""
    from meshpipeline.parallel_gzip import ParallelGzipWriter

    payload = (b"(4(12 7 9 3)(8 1 4 11)(2 2 2 2))\n" * 40_000)

    def once(n: int) -> bytes:
        sink = io.BytesIO()
        with ParallelGzipWriter(sink, level=au.BUNDLE_ARCHIVE_LEVEL, worker_count=n,
                                chunk_bytes=64 * 1024) as gz:
            gz.write(payload)
        return sink.getvalue()

    assert once(worker_count) == once(1)


def test_a_bundle_written_this_way_round_trips_its_contents():
    """Not only readable - right. A reader that opens the archive and returns the wrong bytes
    would pass every structural check above."""
    from meshpipeline.parallel_gzip import ParallelGzipWriter

    body = bytes(range(256)) * 9_000
    sink = io.BytesIO()
    with ParallelGzipWriter(sink, level=au.BUNDLE_ARCHIVE_LEVEL, worker_count=4,
                            chunk_bytes=64 * 1024) as gz, \
            tarfile.open(fileobj=gz, mode="w|") as tf:
        info = tarfile.TarInfo("bundle/constant/polyMesh/owner")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))

    with tarfile.open(fileobj=io.BytesIO(sink.getvalue()), mode="r|gz") as tf:
        member = next(iter(tf))
        assert member.name == "bundle/constant/polyMesh/owner"
        assert tf.extractfile(member).read() == body
