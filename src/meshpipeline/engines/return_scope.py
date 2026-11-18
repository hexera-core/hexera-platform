# Responsibility: Keep the workspace a mesh run sends home inside the size every extractor accepts, by leaving out what the delivery can do without.
# Owns: the estimate of the returned archive, the list of optional heavy files, and the record of what was left out.
# Boundaries: runs on the mesher's side, after the engine and before the workspace is archived; it never touches the mesh, its quality record or anything a gate reads.
# Collaborates with: engines/dispatch.run_engine_local (calls it), adapters/mesh_execution/gcs_exchange (writes the archive this measures, at RESULT_ARCHIVE_LEVEL), parallel_gzip (compresses on every core, here and there, and prices the cut), sandbox/safe_extract (the limits it keeps inside), engines/*/finalize (which treat a missing volume export as a real, optional state).
"""WHAT RIDES HOME WITH THE MESH.

The mesher's workspace goes back to the caller as one gzip archive, and the caller extracts it
through the bounded extractor, under `sandbox/result_limits.result_extraction_limits`: the generic
512 MiB of sandbox/safe_extract is a FLOOR under that, and the bound is the cell budget's worth
(800 MB at the default 8 M) or the measured mesh's, whichever is larger. See archive_ceiling. Twelve
dev-side runs of the verify-5fix sweep built their mesh and then lost it on the way back - 550 MB
to 1,005 MB archives, "archive is ... bytes, over the 536870912 cap" - the fclib spray-can nozzle
among them. The heaviest thing in such a workspace after the polyMesh itself is the VOLUME export
the reviewer may slice (foamToVTK's internal.vtu, a second copy of every cell): measured on a 1 M
hex block, 16 MB of a 42 MB archive, and the review treats it as optional ("a valid job may ship
no volume export, and its surface review must still open").

So, before the workspace is archived:
  * scratch directories the engines leave (a rescue's candidate meshes) are removed;
  * when the archive would come out over the ceiling, the optional volume exports are left out,
    largest first, until it fits - and the run says so (RETURN_SCOPE_FILE, `return_scope`).
Nothing a gate, the solver deliverable or the surface deliverable reads is ever left out: the
polyMesh, the boundary .vtp files, the quality, passage and layer records all stay.

WHAT THE MEASURE COSTS, which is the other half of the job. Every byte of the workspace is
compressed here to decide, and then again by the archiver to send - so the measure must not cost
what the archive costs. Job 575f76e6 (two-scale coupon, execution hexera-mesh-semi-vh29x,
2026-10-29) logged its decision at 13:56 and the worker was still waiting past 14:20 for an
archive of 689,484,779 bytes. Most of that tail was the ARCHIVE's own level, now
gcs_exchange.RESULT_ARCHIVE_LEVEL. Measured on that coupon's own 1.12 GB workspace, the two
passes together took 665 s - 106.9 s to decide and 557.8 s to send. The archive's level came down,
and BOTH passes are now spread over every core the job has (parallel_gzip):

    fast measure, level 1      303,383,687 bytes    24.2 s on one core     4.3 s on 32
    exact measure, level 6     245,023,809 bytes   106.9 s on one core    22.1 s on 32
    the archive, level 6       244,703,534 bytes   111.8 s on one core     7.4 s on 32

So the return is 11.7 s where the cheap measure settles it and 29.5 s where it does not, against
the 665 s that started this. The measure's share is paid here, and it is paid at the CHEAPEST
level that can still answer the question:

  * raw bytes, the archive's framing and what its cutting costs, when the workspace fits even
    uncompressed - nothing can push it over;
  * FAST_LEVEL, when that shows the archive clear of the bar - a workspace this far under
    cannot reach it, and the exact figure would change nothing;
  * MEASURE_LEVEL, the exact measure, only when the answer is actually in doubt - which is also
    the only case in which a file is given up, so the file given up is never given up on a guess.

The measure must never read LOW. That is what loses a built mesh on the way back.
"""
from __future__ import annotations

import json
import logging
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

#: The record of what was left out, beside the mesh.
RETURN_SCOPE_FILE = "return_scope.json"
# WHERE THE CEILING COMES FROM (carried_ceiling, below). It is the submitting side's cell budget,
# carried to this process as a per-run container override - contracts/mesh_execution.
# RETURN_BUDGET_ENV, which owns both halves of that seam and carries the measurement.
#
# WHY IT HAS TO BE CARRIED AND CANNOT BE COMPUTED HERE. `archive_ceiling` works the number out
# from `result_extraction_limits`, which is `CELL_HARD_LIMIT` cells at RESULT_BYTES_PER_CELL and
# the measured count at RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL, clamped to twice CELL_HARD_LIMIT.
# CELL_HARD_LIMIT is `optional_env("CELL_HARD_LIMIT", "8000000")` (settings/policy), and the mesh
# VM IS NEVER PASSED IT. So on the VM the whole calculation runs off the 8,000,000 default while
# the side that will actually do the extracting runs off the real one, and the two disagree by
# whatever the operator set.
#
# MEASURED, job 0ff6f9e5, 2026-11-16, on a 283,410,955-cell plate:
#
#     max(536870912, 8e6 x 100, min(283410955, 16e6) x 200) = 3,200,000,000   on the VM
#     283410955 x 200                                       = 56,682,191,000  at home
#
# 17.7 times larger. The VM gave up the 10,366,611,525-byte fluid interior to get under a ceiling
# that was never the real one, `return_scope.json` recorded "fits": false because the prune did
# not even work, and the 11.4 GB archive was then sent and accepted anyway. The interior was
# deleted for nothing, and with it the only artifact the conjugate review can reach a verdict
# from.
#
# NOT FIXED BY CHANGING CELL_HARD_LIMIT, which is a cell budget and a gating bar on seven
# engines; raising it to make an archive fit would quietly admit meshes the budget refuses.
#
# IT WAS A FILE IN THE SUBMITTED WORKSPACE AND IS NOT ANY MORE. `return_ceiling.json` was added to
# the submission tar at archive time, which put a member in the upload that the submission's own
# payload declaration did not cover - and that declaration is what scopes BOTH the tar and the
# claim's payload digest, by construction (contracts/mesh_execution.submission_payload_files). The
# budget is the submitting side's CONFIGURATION rather than part of the case, so it now rides on
# the channel that side's other per-run configuration already rides on, and the archive is the
# case plus the facts again.

#: Directories no caller reads: an engine's own scratch (engines/cfmesh/rescue.py).
SCRATCH_DIRS = ("_rescue",)
#: Files the delivery can do without, in the order they are given up: the volume exports.
OPTIONAL_GLOBS = ("VTK/**/internal.vtu",)
#: The archive is kept this far under the ceiling.
HEADROOM = 0.9
#: How the archive's size is measured: every file compressed whole, plus the framing the archive
#: carries and the files do not (frame_bytes). It reads HIGH against the archive at this same
#: level (gcs_exchange.RESULT_ARCHIVE_LEVEL) - but only just: 45,843 bytes in 244,703,534 on the
#: coupon's workspace, 0.019%, before cut_bytes widens it to 320,275. All the slack there is comes
#: from charging each member's header raw and from the dictionary the archive carries across
#: members and this does not. That margin is the whole safety of the module, so nothing may be
#: shaved off it on the grounds that it looks generous. No sampling: a file can compress unevenly.
MEASURE_LEVEL = 6
#: The same measure done cheaply, to settle a workspace that CLEARLY fits without paying for the
#: exact one. Measured on the coupon's own workspace (1,120,883,923 bytes over 105 files):
#: 303,383,687 bytes against the exact measure's 245,023,809 - 24% high - in 4.3 s against 22.1 s
#: on 32 cores, and 24.2 s against 106.9 s on one. Still no sampling; still every byte.
FAST_LEVEL = 1
#: ...but the fast measure is not guaranteed to read high FILE BY FILE, so it is only allowed to
#: settle the question this far clear of the bar. Level 1 beat level 6 on one of the coupon's 62
#: files of 4 KiB or more - a boundary .vtp, at 0.9937 of its level-6 size - while the workspace's
#: total read 1.2385 of it. This margin is eight times that worst file, HEADROOM sits on top of
#: it, and anything inside it is measured exactly instead of guessed.
FAST_TRUST = 0.95
#: tar's header and padding per member
_MEMBER_BYTES = 512
#: tar's end-of-archive marker (two empty blocks) and a full record of padding behind it, plus
#: gzip's own header and trailer. Charged whole and uncompressed, which over-states it heavily:
#: every byte of it is zero.
_ARCHIVE_TAIL = 1024 + 10 * 1024 + 64
_CHUNK = 4 * 1024 * 1024


def frame_bytes(workspace) -> int:
    """What the archive holds that is NOT a file's contents: a 512-byte header and padding for
    every member, directories included, and the archive's own tail.

    A sum of file sizes has none of this and the archive has all of it, so `raw_bytes` alone is
    not an upper bound on the archive - on two hundred one-byte cellSets it is off by a hundred
    times the files themselves. Measured on the test workspace of six files in five directories,
    the real archive came out 1,490 bytes ABOVE the exact measure of it until this was counted:
    the measure reading LOW, which is the one way to lose a built mesh on the way home. It went
    unseen while the archiver still wrote level 9 against a level-6 measure, because that gap
    happened to be wider than this.
    """
    return sum(_MEMBER_BYTES for _ in Path(workspace).rglob("*")) + _ARCHIVE_TAIL


def cut_bytes(workspace) -> int:
    """What the archiver's PARALLEL CUT may add, which the files' own bytes know nothing about.

    The archive is no longer one deflate stream: it is written on every core the job has, which
    means the stream is cut into chunks and each cut can move the size a little
    (parallel_gzip.cut_cost - 20.6 bytes at worst measured, charged at 4,096). How many cuts there
    are follows from how long the UNCOMPRESSED stream is, so it is
    charged off the raw size and the framing, both of which are a stat walk and not a compression.

    That length is slightly UNDER-counted - tar pads each member's content up to 512 bytes and
    this does not count the padding - so the cut count can be a hair low on a workspace of very
    many tiny files. The charge is two hundred times the worst cut measured, which is where the
    room for that lives.
    """
    from meshpipeline.parallel_gzip import cut_cost
    return cut_cost(raw_bytes(workspace) + frame_bytes(workspace))


def compressed_size(path: Path, level: int = MEASURE_LEVEL) -> int:
    """What `path` adds to a gzip archive, measured by compressing it whole."""
    z = zlib.compressobj(level)
    out = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            out += len(z.compress(chunk))
    return out + len(z.flush()) + _MEMBER_BYTES


def estimate_archive_bytes(workspace, *, level: int = MEASURE_LEVEL,
                           into: dict | None = None) -> int:
    """How large the gzip archive of `workspace` will be (compressed_size of every file).

    `into` collects what each file measured, so a file this walk has already compressed is never
    compressed a second time to decide whether to leave it out.

    ON EVERY CORE THE JOB HAS, like the archive it is measuring. Every file is compressed on its
    own either way, so the threads change which core does the work and NOTHING about the number
    that comes out: the same files at the same level are the same bytes. That matters at the scale
    this module exists for - with the archive itself spread over the cores (parallel_gzip) a
    serial measure is the LONGER half of the return, and
    the whole point of the cheap levels above was that the deciding pass must not cost what the
    sending pass costs. Biggest file first, so the last core to pick up work is not the one the
    others wait on. The floor is one file: a workspace whose bytes are nearly all in a single
    `owner` cannot be measured faster than that file compresses.
    """
    from meshpipeline.engines.determinism import job_cpus
    from meshpipeline.parallel_gzip import workers
    total = _ARCHIVE_TAIL + cut_bytes(workspace)
    files = []
    for p in Path(workspace).rglob("*"):
        if p.is_file():
            files.append(p)
        else:
            total += _MEMBER_BYTES      # a directory is a member too; compressed_size charges files
    files.sort(key=lambda p: -p.stat().st_size)
    with ThreadPoolExecutor(max_workers=workers(job_cpus()),
                            thread_name_prefix="measure") as pool:
        shares = list(pool.map(lambda p: compressed_size(p, level), files))
    for p, share in zip(files, shares):
        if into is not None:
            into[p] = share
        total += share
    return total


def raw_bytes(workspace) -> int:
    return sum(p.stat().st_size for p in Path(workspace).rglob("*") if p.is_file())


def archive_ceiling(result: dict | None = None, *, cell_hard_limit: int | None = None) -> int:
    """The ceiling the CALLER will extract this archive under, which is the only one that matters.

    NOT `default_limits()`, which is what this read until it was measured. The caller extracts a
    returned mesh with `sandbox/result_limits.result_extraction_limits` - "a returned mesh workspace may
    be as large as a mesh the cell budget allows" - which is CELL_HARD_LIMIT cells at
    RESULT_BYTES_PER_CELL (800 MB at the default 8 M), and more again when the result document says
    how many cells the mesh HAS (RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL, 200 a cell). The generic
    512 MiB is a floor under those, not the bound.

    So this was pruning - and then refusing - against a number three times smaller than the one it
    was keeping the archive inside of. Measured on 2026-10-29: the coupon plate's conjugate mesh
    gave up its volume export (725 MB) and still reported

        the workspace is still about 570 MB compressed, over the 512 MiB ceiling, with nothing
        optional left to leave out

    while the extractor waiting for it would have taken 800 MB on the budget alone and 1.34 GB
    once it read the mesh's 6.7 M cells. Nothing was too big. The same two labs in this module's
    own header - a 557 MB snappy passage and a 560 MB aorta - are why
    `result_extraction_limits` exists at all, and this never asked it.

    `result` is the engine's own result dict when the caller has one, so the MEASURED cell count
    counts: the mesher knows exactly how many cells it just wrote.

    `cell_hard_limit` is the SUBMITTING side's cell budget when this run was told it
    (contracts/mesh_execution.RETURN_BUDGET_ENV). It is the only input to that calculation the
    mesh VM is never given, and passing it here keeps the formula in one place instead of copying
    it across the boundary.

    THE FORMULA IS NOT THE TRANSPORT'S. This read it out of
    `adapters/mesh_execution/cloud_run_client`, which is an engine reaching into a concrete
    adapter to ask what the caller's rules are - the thing
    tests/unit/hygiene/test_architecture_boundaries forbids. The rule now lives beside the limits
    it is built from (sandbox/result_limits), which both this side and the transport read.
    """
    from meshpipeline.sandbox.result_limits import result_extraction_limits
    return int(result_extraction_limits(result, cell_hard_limit=cell_hard_limit)
               .max_archive_bytes)


def carried_ceiling(result: dict | None = None) -> int | None:
    """The extraction ceiling worked out from the budget the submitting side carried in, or None.

    See WHERE THE CEILING COMES FROM, above the constants, for why computing it here from this
    process's own settings gets the wrong number on the mesh VM - and what that cost job 0ff6f9e5.

    WHAT IS CARRIED IS THE BUDGET AND NOT A FINISHED CEILING, and that is the point. The real
    ceiling is CELL_HARD_LIMIT cells at a rate, OR the mesh's own measured cell count at another
    rate, whichever is larger - and the second term is only knowable HERE, after the mesh is
    built. A finished number computed at submission time is the FLOOR of the real one and nothing
    more: on job 0ff6f9e5 it would have been 800,000,000 against a true 56,682,191,000. So this
    side finishes the sum by calling the submitting side's own function with the one input it was
    missing, and there is no second copy of the formula to go stale.

    None for anything that is not a positive whole number (contracts/mesh_execution reads it that
    way), so a truncated or hand-set value falls back to the computed ceiling rather than pruning
    against nonsense: a ceiling read as 0 would give up every optional file in the workspace,
    which is the exact damage this exists to stop.
    """
    from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV
    from meshpipeline.runtime.mesh_invocation import carried_cell_budget
    budget = carried_cell_budget()
    if budget is None:
        return None
    try:
        return archive_ceiling(result, cell_hard_limit=budget)
    except Exception:                     # noqa: BLE001
        logger.warning("return scope: %s carries a cell budget this side could not price; "
                       "computing a ceiling instead", RETURN_BUDGET_ENV, exc_info=True)
        return None


def scope_return(workspace, *, ceiling: int | None = None,
                 result: dict | None = None) -> dict | None:
    """Trim the workspace a mesh run returns (see the module text). Returns the record of what
    was left out, or None when nothing was."""
    import shutil
    ws = Path(workspace)
    if not ws.is_dir():
        return None
    removed: list[str] = []
    for name in SCRATCH_DIRS:
        d = ws / name
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(name + "/")
    # THE CALLER'S NUMBER FIRST, then the one the submitting side carried, then a computed one.
    # An explicit `ceiling=` is a test or a caller that knows better and stays the authority.
    carried = None if ceiling is not None else carried_ceiling(result)
    cap = int(ceiling if ceiling is not None else (carried if carried is not None
                                                   else archive_ceiling(result)))
    bar = HEADROOM * cap
    left_out: list[dict] = []
    #: what the figure below actually is, for the record: "raw", "fast" or "exact"
    measure = "raw"
    exact: dict[Path, int] = {}
    # THE CHEAPEST MEASURE THAT CAN ANSWER (see the module text). A workspace whose raw bytes and
    # framing fit can never compress past the bar; one the fast level puts clear of the bar cannot
    # reach it; only what is left is compressed at the exact level, and only that can give a file
    # up.
    estimate = raw_bytes(ws) + frame_bytes(ws) + cut_bytes(ws)
    if estimate > bar:
        measure = "fast"
        estimate = estimate_archive_bytes(ws, level=FAST_LEVEL)
        if estimate > FAST_TRUST * bar:
            measure = "exact"
            estimate = estimate_archive_bytes(ws, level=MEASURE_LEVEL, into=exact)
    declined = ""
    if estimate > bar:
        optional = sorted({p for g in OPTIONAL_GLOBS for p in ws.glob(g) if p.is_file()},
                          key=lambda p: -p.stat().st_size)
        # WHAT GIVING UP EVERYTHING WOULD ACHIEVE, worked out before anything is unlinked. When
        # even that cannot get the archive under the ceiling, the files buy nothing by going: the
        # archive is refused either way and the only difference is whether the evidence was
        # destroyed on the way. That is what happened on job 0ff6f9e5 - the 10.4 GB fluid interior
        # was unlinked, "fits" came out false, and the archive went home and was accepted anyway
        # against a ceiling 17.7x larger than the one it had been pruned against.
        #
        # AGAINST `cap`, NOT AGAINST `bar`, and the difference is a whole mesh. In the band between
        # them the loop below prunes, lands under cap and IS accepted by the extractor; a check
        # written against bar would call that case hopeless, keep the files, and ship an archive
        # over cap that `sandbox/safe_extract` then rejects outright - losing the polyMesh and
        # everything else with it. HEADROOM is the margin the loop aims for, never the test for
        # whether to try.
        best = estimate - sum((exact.get(p) or compressed_size(p)) for p in optional)
        if optional and best > cap:
            declined = (
                f"giving up every optional file would still leave about {best / 1e6:.0f} MB, over "
                f"the {cap / 2**20:.0f} MiB ceiling, so nothing was given up: the archive is "
                "refused either way and the volume export the reviewer slices is worth more than "
                "a prune that cannot work")
            logger.warning("return scope: %s", declined)
            optional = []
        for p in optional:
            if estimate <= bar:
                break
            # already compressed by the exact walk above, which is the only walk that reaches here
            share = exact.get(p) or compressed_size(p)
            left_out.append({"file": p.relative_to(ws).as_posix(),
                             "bytes": p.stat().st_size, "archive_bytes_est": share})
            p.unlink(missing_ok=True)
            estimate -= share
    # A DECLINED PRUNE IS A DECISION AND IS REPORTED. Returning None here would say "nothing to
    # report" about the one run that most needs explaining: an archive over the ceiling, still
    # holding its volume export, on purpose.
    if not removed and not left_out and not declined:
        return None
    record = {"ceiling_bytes": cap, "archive_bytes_est": int(estimate), "measure": measure,
              "scratch_removed": removed, "left_out": left_out,
              "fits": estimate <= cap}
    if carried is not None:
        # WHERE THE CEILING CAME FROM, so a reader never has to guess whether this run pruned
        # against the real number or against the VM's 8,000,000-cell default.
        from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV
        record["ceiling_source"] = RETURN_BUDGET_ENV
    elif ceiling is None:
        from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV
        record["ceiling_source"] = (
            f"computed on the mesher's side (no {RETURN_BUDGET_ENV} was carried)")
    if declined:
        record["prune_declined"] = declined
    if left_out:
        record["note"] = (
            "The mesh was too large to send back whole within the transfer limit "
            f"({cap / 2**20:.0f} MiB), so the volume export the reviewer slices was left out "
            f"({sum(x['bytes'] for x in left_out) / 1e6:.0f} MB); the mesh, its boundary and "
            "every measurement are all there.")
        logger.warning("return scope: %s", record["note"])
    if not record["fits"]:
        logger.warning("return scope: the workspace is still about %.0f MB compressed, over the "
                       "%.0f MiB ceiling, with nothing optional left to leave out",
                       estimate / 1e6, cap / 2**20)
    try:
        (ws / RETURN_SCOPE_FILE).write_text(json.dumps(record))
    except OSError:
        pass
    return record


__all__ = ["FAST_LEVEL", "FAST_TRUST", "HEADROOM", "MEASURE_LEVEL", "OPTIONAL_GLOBS",
           "RETURN_SCOPE_FILE", "SCRATCH_DIRS", "archive_ceiling", "carried_ceiling", "compressed_size",
           "cut_bytes", "estimate_archive_bytes", "frame_bytes", "raw_bytes", "scope_return"]

