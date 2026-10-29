# Responsibility: Keep the workspace a mesh run sends home inside the size every extractor accepts, by leaving out what the delivery can do without.
# Owns: the estimate of the returned archive, the list of optional heavy files, and the record of what was left out.
# Boundaries: runs on the mesher's side, after the engine and before the workspace is archived; it never touches the mesh, its quality record or anything a gate reads.
# Collaborates with: engines/dispatch.run_engine_local (calls it), sandbox/safe_extract (the limits it keeps inside), engines/*/finalize (which treat a missing volume export as a real, optional state).
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
#: How the archive's size is measured: every file compressed whole. Level 6 is a little LARGER than
#: the archive's own level 9 (tarfile's gzip default) and three times as fast, so the measure errs
#: on the side of leaving the volume out. No sampling: a file can compress unevenly.
MEASURE_LEVEL = 6
#: tar's header and padding per member
_MEMBER_BYTES = 512
_CHUNK = 4 * 1024 * 1024


def compressed_size(path: Path) -> int:
    """What `path` adds to a gzip archive, measured by compressing it whole."""
    z = zlib.compressobj(MEASURE_LEVEL)
    out = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            out += len(z.compress(chunk))
    return out + len(z.flush()) + _MEMBER_BYTES


def estimate_archive_bytes(workspace) -> int:
    """How large the gzip archive of `workspace` will be (compressed_size of every file)."""
    return sum(compressed_size(p) for p in Path(workspace).rglob("*") if p.is_file())


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
    left_out: list[dict] = []
    # a workspace whose raw bytes fit can never compress past the ceiling: nothing to measure.
    # One that does not is measured file by file (about a minute for a few GB of mesh files).
    estimate = raw_bytes(ws)
    if estimate > HEADROOM * cap:
        estimate = estimate_archive_bytes(ws)
    if estimate > HEADROOM * cap:
        optional = sorted({p for g in OPTIONAL_GLOBS for p in ws.glob(g) if p.is_file()},
                          key=lambda p: -p.stat().st_size)
        for p in optional:
            if estimate <= HEADROOM * cap:
                break
            share = compressed_size(p)
            left_out.append({"file": p.relative_to(ws).as_posix(),
                             "bytes": p.stat().st_size, "archive_bytes_est": share})
            p.unlink(missing_ok=True)
            estimate -= share
    if not removed and not left_out:
        return None
    record = {"ceiling_bytes": cap, "archive_bytes_est": int(estimate),
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


__all__ = ["HEADROOM", "MEASURE_LEVEL", "OPTIONAL_GLOBS", "RETURN_SCOPE_FILE", "SCRATCH_DIRS",
           "archive_ceiling", "compressed_size", "estimate_archive_bytes", "raw_bytes",
           "scope_return"]
