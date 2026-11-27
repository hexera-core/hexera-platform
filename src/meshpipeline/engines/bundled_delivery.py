# Responsibility: Build the engine's delivered bundle where the mesh already is, record what went into it, and let the parts only the bundle needed stay behind.
# Owns: the bundle writer shared by the mesher and the uploader, the record beside it, and the question "were this region's cells delivered".
# Boundaries: it packs, records and narrows; it never decides what a bundle must contain (engines/base.Deliverable) and it never touches the mesh's quality record or anything a gate reads other than through the record it writes.
# Collaborates with: engines/dispatch.run_engine_local (builds it on the mesher, before return_scope), application/artifact_uploader (delivers the file it finds rather than building a second one), engines/cfmesh_conjugate/gates.meshed_regions (reads the record when the ranks are not there), adapters/mesh_execution/gcs_exchange (archives the narrowed workspace).
"""WHERE THE BUNDLE IS BUILT, AND WHY IT IS NOT AT HOME.

MEASURED, job 606af55e (2026-11-27), the first run of this plate to pass every gate, at 2:15:39.
The whole job is 8,139.9 s and it splits like this, from the worker's own log and the job row:

      337 s   the home side before the VM exists (claim, intake events, the driver's plan)
    7,705 s   the VM leg, from "mesh ... runs on a n2-highmem-64 VM" to "rc=0 timed_out=False"
       98 s   the home side after it (reviewer, the two exact surfaces, three uploads)

and the VM leg splits again at the beacon the mesher publishes to the exchange bucket, whose
`progress.json` for this job's operation key (f6e000d0f577ba...) is stamped 10:02:08:

    6,636 s   VM created -> "returning: sending the mesh back"
    1,069 s   "returning" -> the result collected at home

THAT LAST 1,069 s IS THE ARCHIVE, THE UPLOAD, THE DOWNLOAD AND THE EXTRACTION, and the reason it
costs what it does is one number: the workspace that came home is 124 GB, of which 123 GB is the
eight `processor*` trees at 15-16 GB each. Those bytes are compressed on the VM, uploaded,
downloaded, and then written out again at home - and the ONLY thing at home that reads them is the
tar that packs them into the customer's bundle.

AND THAT TAR HAS JUST BECOME THE EXPENSIVE ONE. Until 4558f0cc the delivered bundle had no cells in
it at all (541 MB of viewer sidecars and a decimated surface - the defect that commit closes), so
packing it at home was cheap. With the cells in it the home side has to compress 123 GB through a
4.0-CPU quota, which at the measured level-6 rate of 26.8 MB/s a thread is about nineteen minutes
added to every run. So this is not a tuning change: it is what makes the cells-in-the-bundle fix
affordable at all, and it is why the bundle is built on the machine that has 64 cores and the mesh.

NOTHING ELSE AT HOME NEEDS THE RANKS, checked rather than assumed, because that is exactly the kind
of claim that is wrong:
  * `render/exact_surface` is handed `constant/<region>/polyMesh` and reads the boundary INDEX
    sidecar there (`open_index`), whose own note says "a mesh that is not there at all is allowed
    ... which is how a detail window works on a host the 137 GB workspace was never copied to";
  * `render/boundary_index` and `render/boundary_quality` are the WRITERS of those sidecars and run
    on the mesher, inside the engine's deadline (native._boundary_indexes);
  * `render/detail_window.delivered_regions` globs `constant/*` and never looks at `processor*`,
    which `conjugate.py` says where it publishes the patch tables;
  * `engines/cfmesh_conjugate/pinch`, `polymesh_surface` and `conjugate` read the ranks, and all
    three run on the mesher;
  * `engines/determinism` skips any path with `processor` in it by name (_SKIP_PARTS).
ONE home-side reader was found, and it is the reason this module writes a record at all:
`gates.meshed_regions` proves a region's cells exist by looking for
`processor*/constant/<region>/polyMesh/owner` on EVERY rank, and that gate runs at home
(pipeline/executor.run_gates).

SO THE RECORD IS TAKEN FROM THE ARCHIVE'S OWN MEMBER NAMES, not from the directory listing that is
about to go away. `build_bundle` writes the bundle, runs the engine's own `verify_archive` over it -
which is where `Deliverable.must_contain` refuses a case with no cells in it - and then writes down,
per region, which ranks hold a `polyMesh/owner` INSIDE the file. That is a stronger statement than
the directory check it stands in for: a directory holding an `owner` file proves the mesher wrote
one, while a member inside the verified bundle proves the customer is getting it.

AND THE RECORD IS TIED TO THE BYTES IT SPEAKS FOR. `cells_delivered` answers only when the recorded
bundle is still there at the recorded size, so a workspace whose bundle was moved, truncated or
replaced gets None and the gate falls back to refusing rather than to believing a file that says so
about itself.

WHAT THIS IS NOT. It does not decide what goes into a bundle; the recipe does. It does not make the
download smaller - the cells are in it now, which is the whole point of 4558f0cc. And `narrow` drops
only what the ENGINE declared as `packed_only`, so an engine that declares nothing loses nothing and
this module is inert for it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The record of the bundle this workspace already carries, written beside it.
BUNDLE_RECORD = "mesh_bundle.json"
#: What proves a region's CELLS are in a polyMesh, and the file whose presence per rank is the
#: statement `gates.meshed_regions` makes. `owner` and not `points`: a rank holds points for a patch
#: it merely borders, while `owner` is one label per cell.
CELLS_FILE = "owner"
#: How much is read at a time to digest the bundle. The bundle is tens of gigabytes, so it is
#: digested in a stream and never held in memory.
_DIGEST_BLOCK = 8 * 1024 * 1024


def record_path(workspace) -> Path:
    return Path(workspace) / BUNDLE_RECORD


def worth_building(spec) -> bool:
    """Whether packing the bundle on the MESHER buys this engine anything.

    THE ANSWER IS `packed_only`, AND THAT IS NOT A PROXY. The whole economy is "these members only
    the bundle needed, so they need not travel" - an engine that names none of them pays a second
    compression pass over its workspace and leaves nothing behind for it, which is a cost with no
    benefit attached.

    IT IS ALSO WHAT KEEPS THE CHANGE WHERE IT WAS MEASURED. `dispatch.run_engine_local` is shared by
    every engine, and `test_the_return_watch_changes_nothing_else_a_non_conjugate_run_returns` holds
    it to changing nothing about a cfmesh, gmsh or snappy_multiregion run - correctly: those engines
    were never measured for this and a bundle built from a workspace that cannot satisfy their
    contract is a refusal on a path that used to have none.
    """
    recipe = getattr(spec, "deliverable", None)
    return bool(recipe is not None and tuple(getattr(recipe, "packed_only", ()) or ()))


def cells_in_archive(names, prefix: str) -> dict[str, list[str]]:
    """Per region, the ranks whose cells are inside this archive, read off its member names.

    A member is `<prefix>/processor<N>/constant/<region>/polyMesh/owner` on the decomposed path and
    `<prefix>/constant/<region>/polyMesh/owner` on a merged one, where the rank is recorded as the
    empty string because there is one mesh and no ranks - which is the first of the two cases
    `gates.meshed_regions` already distinguishes.
    """
    out: dict[str, set[str]] = {}
    want = f"{prefix}/"
    tail = f"/polyMesh/{CELLS_FILE}"
    for raw in names:
        name = str(raw)
        if not name.startswith(want) or not name.endswith(tail):
            continue
        parts = name[len(want):].split("/")
        if len(parts) == 5 and parts[0].startswith("processor") and parts[1] == "constant":
            out.setdefault(parts[2], set()).add(parts[0])
        elif len(parts) == 4 and parts[0] == "constant":
            out.setdefault(parts[1], set()).add("")
    return {r: sorted(v) for r, v in sorted(out.items())}


def build_bundle(workspace, spec) -> dict | None:
    """Write this engine's deliverable bundle into the workspace and record what is in it.

    Returns the record, or None when there is nothing to build - which is an engine that declares no
    deliverable. A workspace that already carries one is left alone and its existing record returned.

    RAISES whatever the build or the verification raises, and the caller decides what that means. On
    the mesher it means "send the workspace as it is", because an economy may never cost a mesh that
    is already built.
    """
    recipe = getattr(spec, "deliverable", None)
    if recipe is None:
        return None
    out = Path(workspace) / recipe.bundle
    if out.exists():
        return bundle_record(workspace)

    # THE SAME WRITER THE UPLOADER USES, reached through it rather than copied, because the bundle's
    # checksum is part of its identity and a second implementation would be a second answer. The
    # uploader imports the persistence layer at module level; measured, that import is 0.24 s in an
    # environment with no database, no storage and no settings at all, so the mesher pays nothing
    # for reaching through it.
    from meshpipeline.application.artifact_uploader import BUNDLE_ARCHIVE_LEVEL, verify_archive
    from meshpipeline.engines.determinism import job_cpus
    from meshpipeline.parallel_gzip import ParallelGzipWriter, workers

    def _norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        # DETERMINISTIC, for the reason artifact_uploader gives where it does the same: the same
        # content must yield the same checksum across rebuilds, or a same-attempt retry looks like
        # different content and trips the conflict guard.
        ti.mtime = 0
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    # WRITTEN UNDER A PART NAME AND RENAMED AT THE END, so a run killed during the build leaves no
    # file that `bundled_file` would hand to a customer. The rename is atomic within the workspace.
    #
    # AND THE PART FILE GOES ON EVERY FAILURE, which is not tidiness. The caller treats a failed
    # build as "send the workspace as it is", so anything left behind here TRAVELS: measured on the
    # shared dispatch test, a refused build left a 428,873-byte `.part` in a workspace whose
    # largest file had been 288,889, which is the figure the extractor sizes itself from
    # (`returned_bytes["largest"]` -> `result_extraction_limits`). An economy that cannot complete
    # must cost nothing at all, and a half-written archive in a delivered workspace is not nothing.
    tmp = out.with_name(out.name + ".part")
    try:
        with tmp.open("wb") as fh, \
                ParallelGzipWriter(fh, level=BUNDLE_ARCHIVE_LEVEL,
                                   worker_count=workers(job_cpus())) as gz, \
                tarfile.open(fileobj=gz, mode="w|") as tf:
            for rel in recipe.expand(workspace):
                src = Path(workspace) / rel
                if src.exists():
                    tf.add(src, arcname=f"{recipe.prefix}/{rel}", filter=_norm)
        verify_archive(tmp, spec)
        # READ ONCE MORE FOR THE RECORD, from the same file that was just verified. Listing a gzip
        # member is a whole decompression pass, which is why it happens HERE - on the machine that
        # has the mesh and the cores - and never at home.
        with tarfile.open(tmp) as tf:
            cells = cells_in_archive(tf.getnames(), recipe.prefix)
        sha = hashlib.sha256()
        with tmp.open("rb") as fh:
            for block in iter(lambda: fh.read(_DIGEST_BLOCK), b""):
                sha.update(block)
        tmp.replace(out)
    finally:
        # `missing_ok`: the ordinary path renamed it away, and this is the one line that runs on
        # both paths.
        tmp.unlink(missing_ok=True)
    rec = {"bundle": recipe.bundle, "bytes": out.stat().st_size, "sha256": sha.hexdigest(),
           "engine": str(getattr(spec, "name", "") or ""), "prefix": recipe.prefix,
           "cells": cells}
    record_path(workspace).write_text(json.dumps(rec, indent=1, sort_keys=True))
    logger.info("bundled delivery: %s is %d bytes and carries %s", recipe.bundle, rec["bytes"],
                ", ".join(f"{r} on {len(v)} rank(s)" for r, v in cells.items()) or "no cells")
    return rec


def narrow(workspace, spec) -> list[str]:
    """Remove what the engine declared `packed_only` now that the bundle holds it. Returns what went.

    ONLY WITH A VERIFIED BUNDLE IN HAND, which the caller establishes by having called
    `build_bundle`; this re-checks it through `bundle_record`, because deleting a customer's cells on
    the strength of a file that may not be there is the one mistake that cannot be undone.
    """
    recipe = getattr(spec, "deliverable", None)
    packed = tuple(getattr(recipe, "packed_only", ()) or ()) if recipe is not None else ()
    if not packed or not bundle_record(workspace):
        return []
    ws = Path(workspace)
    gone: list[str] = []
    for pattern in packed:
        for hit in sorted(ws.glob(pattern)):
            # INSIDE THE WORKSPACE OR NOT AT ALL. A pattern is engine-declared and not user input,
            # but a symlink inside the workspace could still point out of it, and this function
            # deletes recursively.
            if not str(hit.resolve()).startswith(str(ws.resolve())):
                logger.warning("bundled delivery: %s resolves outside the workspace; left alone",
                               hit.name)
                continue
            if hit.is_dir() and not hit.is_symlink():
                shutil.rmtree(hit, ignore_errors=True)
            else:
                hit.unlink(missing_ok=True)
            gone.append(hit.relative_to(ws).as_posix())
    if gone:
        logger.info("bundled delivery: %d path(s) stayed behind because the bundle carries them "
                    "(%s)", len(gone), ", ".join(gone[:8]) + (" ..." if len(gone) > 8 else ""))
    return gone


def bundle_record(workspace) -> dict | None:
    """The record, or None when there is not one or it does not describe a file that is here.

    THE SIZE IS CHECKED, because the record's only job is to speak for bytes that are no longer on
    this disk, and a record that outlived its bundle would speak for nothing.
    """
    p = record_path(workspace)
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    name = str(rec.get("bundle") or "")
    want = int(rec.get("bytes") or 0)
    got = Path(workspace) / name
    if not name or "/" in name or not got.is_file():
        return None
    if want and got.stat().st_size != want:
        return None
    return rec


def bundled_file(workspace, spec) -> Path | None:
    """The bundle this workspace already carries for `spec`, ready to deliver, or None.

    The ENGINE is checked as well as the name: a workspace carrying another engine's bundle is not
    carrying this one's, and delivering it would hand a customer the wrong format under the right
    label.
    """
    rec = bundle_record(workspace)
    recipe = getattr(spec, "deliverable", None)
    if not rec or recipe is None:
        return None
    if str(rec.get("bundle") or "") != recipe.bundle:
        return None
    want = str(getattr(spec, "name", "") or "")
    if want and str(rec.get("engine") or "") != want:
        return None
    return Path(workspace) / recipe.bundle


def cells_delivered(workspace) -> dict[str, list[str]] | None:
    """Per region, the ranks whose cells are inside the bundle this workspace carries - or None.

    None means "no record", which is every workspace that still has its own cells on disk, and is
    why `gates.meshed_regions` reads the filesystem first and this only afterwards.
    """
    rec = bundle_record(workspace)
    if not rec:
        return None
    cells = rec.get("cells")
    if not isinstance(cells, dict):
        return None
    return {str(r): [str(x) for x in (v or [])] for r, v in cells.items()}


__all__ = ["BUNDLE_RECORD", "CELLS_FILE", "build_bundle", "bundle_record", "bundled_file",
           "cells_delivered", "cells_in_archive", "narrow", "record_path"]
