# Responsibility: Keep the workspace a mesh run sends home inside the size every extractor accepts, by leaving out what the delivery can do without.
# Owns: the estimate of the returned archive, the list of optional heavy files, and the record of what was left out.
# Boundaries: runs on the mesher's side, after the engine and before the workspace is archived; it never touches the mesh, its quality record or anything a gate reads.
# Collaborates with: engines/dispatch.run_engine_local (calls it), adapters/mesh_execution/gcs_exchange (writes the archive this measures, at RESULT_ARCHIVE_LEVEL), sandbox/safe_extract (the limits it keeps inside), engines/*/finalize (which treat a missing volume export as a real, optional state).
"""WHAT RIDES HOME WITH THE MESH.

The mesher's workspace goes back to the caller as one gzip archive, and the caller extracts it
through the bounded extractor, under `cloud_run_client.result_extraction_limits`: the generic
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
passes together took 671 s - 113.4 s to decide and 557.8 s to send; at the levels now written
they take 147 s, for an archive 0.25% larger. The measure's share is paid here, and it is paid
at the CHEAPEST level that can still answer the question:

  * raw bytes and the archive's framing, when the workspace fits even uncompressed - nothing
    can push it over;
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
from pathlib import Path

logger = logging.getLogger(__name__)

#: The record of what was left out, beside the mesh.
RETURN_SCOPE_FILE = "return_scope.json"
#: Directories no caller reads: an engine's own scratch (engines/cfmesh/rescue.py).
SCRATCH_DIRS = ("_rescue",)
#: Files the delivery can do without, in the order they are given up: the volume exports.
OPTIONAL_GLOBS = ("VTK/**/internal.vtu",)
#: The archive is kept this far under the ceiling.
HEADROOM = 0.9
#: How the archive's size is measured: every file compressed whole, plus the framing the archive
#: carries and the files do not (frame_bytes). It reads HIGH against the archive at this same
#: level (gcs_exchange.RESULT_ARCHIVE_LEVEL) - but only just: 19,881 bytes in 244,704,856 on the
#: coupon's workspace, 0.008%. All the slack there is comes from charging each member's header raw
#: and from the dictionary the archive carries across members and this does not. That margin is
#: the whole safety of the module, so nothing may be shaved off it on the grounds that it looks
#: generous. No sampling: a file can compress unevenly.
MEASURE_LEVEL = 6
#: The same measure done cheaply, to settle a workspace that CLEARLY fits without paying for the
#: exact one. Measured on the coupon's own workspace (1,120,883,923 bytes over 105 files):
#: 303,084,615 bytes in 26.0 s against the exact measure's 244,724,737 in 114.1 s - 24% high and
#: 4.4 times as fast. Still no sampling; still every byte.
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
    """
    total = _ARCHIVE_TAIL
    for p in Path(workspace).rglob("*"):
        if not p.is_file():
            total += _MEMBER_BYTES      # a directory is a member too; compressed_size charges files
            continue
        share = compressed_size(p, level)
        if into is not None:
            into[p] = share
        total += share
    return total


def raw_bytes(workspace) -> int:
    return sum(p.stat().st_size for p in Path(workspace).rglob("*") if p.is_file())


def archive_ceiling(result: dict | None = None) -> int:
    """The ceiling the CALLER will extract this archive under, which is the only one that matters.

    NOT `default_limits()`, which is what this read until it was measured. The caller extracts a
    returned mesh with `cloud_run_client.result_extraction_limits` - "a returned mesh workspace may
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
    """
    from meshpipeline.adapters.mesh_execution.cloud_run_client import result_extraction_limits
    return int(result_extraction_limits(result).max_archive_bytes)


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
    cap = int(ceiling if ceiling is not None else archive_ceiling(result))
    bar = HEADROOM * cap
    left_out: list[dict] = []
    #: what the figure below actually is, for the record: "raw", "fast" or "exact"
    measure = "raw"
    exact: dict[Path, int] = {}
    # THE CHEAPEST MEASURE THAT CAN ANSWER (see the module text). A workspace whose raw bytes and
    # framing fit can never compress past the bar; one the fast level puts clear of the bar cannot
    # reach it; only what is left is compressed at the exact level, and only that can give a file
    # up.
    estimate = raw_bytes(ws) + frame_bytes(ws)
    if estimate > bar:
        measure = "fast"
        estimate = estimate_archive_bytes(ws, level=FAST_LEVEL)
        if estimate > FAST_TRUST * bar:
            measure = "exact"
            estimate = estimate_archive_bytes(ws, level=MEASURE_LEVEL, into=exact)
    if estimate > bar:
        optional = sorted({p for g in OPTIONAL_GLOBS for p in ws.glob(g) if p.is_file()},
                          key=lambda p: -p.stat().st_size)
        for p in optional:
            if estimate <= bar:
                break
            # already compressed by the exact walk above, which is the only walk that reaches here
            share = exact.get(p) or compressed_size(p)
            left_out.append({"file": p.relative_to(ws).as_posix(),
                             "bytes": p.stat().st_size, "archive_bytes_est": share})
            p.unlink(missing_ok=True)
            estimate -= share
    if not removed and not left_out:
        return None
    record = {"ceiling_bytes": cap, "archive_bytes_est": int(estimate), "measure": measure,
              "scratch_removed": removed, "left_out": left_out,
              "fits": estimate <= cap}
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
           "RETURN_SCOPE_FILE", "SCRATCH_DIRS", "archive_ceiling", "compressed_size",
           "estimate_archive_bytes", "frame_bytes", "raw_bytes", "scope_return"]

