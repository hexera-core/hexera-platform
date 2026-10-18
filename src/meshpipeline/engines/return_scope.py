# Responsibility: Keep the workspace a mesh run sends home inside the size every extractor accepts, by leaving out what the delivery can do without.
# Owns: the estimate of the returned archive, the list of optional heavy files, and the record of what was left out.
# Boundaries: runs on the mesher's side, after the engine and before the workspace is archived; it never touches the mesh, its quality record or anything a gate reads.
# Collaborates with: engines/dispatch.run_engine_local (calls it), sandbox/safe_extract (the limits it keeps inside), engines/*/finalize (which treat a missing volume export as a real, optional state).
"""WHAT RIDES HOME WITH THE MESH.

The mesher's workspace goes back to the caller as one gzip archive, and the caller extracts it
through the bounded extractor (sandbox/safe_extract), whose generic ceiling is 512 MiB. Twelve
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


def archive_ceiling() -> int:
    from meshpipeline.sandbox.safe_extract import default_limits
    return int(default_limits().max_archive_bytes)


def scope_return(workspace, *, ceiling: int | None = None) -> dict | None:
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
    cap = int(ceiling if ceiling is not None else archive_ceiling())
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
