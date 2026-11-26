# Responsibility: Keep what a second look at a delivered mesh needs in the object store, and bring it back onto whichever worker takes the second look.
# Owns: what goes into the review seed (the manifest, the request, the files the reviewer opens, the small records), its object, and its safe restore.
# Boundaries: no verdict, no state - job_service seeds the dispute run from what this restores; artifact_uploader stores the seed with the delivery.
# Collaborates with: application/artifact_uploader (packs it at delivery), application/job_service (restores it before seeding), artifact_keys (its key).
"""A SECOND LOOK WORKS ON ANY WORKER (VER-17).

A delivered run's attempt workspace sits on the disk of the worker that collected it - a mesh made on a
big VM (RUN-10) is sent home to that worker too, and the exchange bucket is emptied once it is. With
several workers, "look at this mesh again" (VER-11) can land on another one, which found no workspace
and failed the run as our fault (dev, 26 Nov: job 6bce9dfd, 641,000 elements, three workers).

So the delivery also stores a REVIEW SEED: one small archive of the delivered attempt - its manifest,
its request and brief, the files the manifest names for the reviewer (the surface it walks, the volume
it slices) and every other small record - never the solver decks or bundles the downloads already
hold. A worker without the workspace restores it to the same place under its own workspace root,
with the manifest's paths pointed at the copy.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import re
import shutil
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg

logger = logging.getLogger(__name__)

#: The rule this module is (docs/RULES.md).
RULE = "VER-17"
#: The seed's artifact row: an internal row of the class the console never offers for download.
SEED_LOGICAL_KEY = "review_seed"
#: Where the seed records which attempt it is and where that attempt was.
SEED_RECORD = "review_seed.json"
#: A file the manifest names for the reviewer (the surface, the volume) is carried up to this size.
NAMED_FILE_MAX_BYTES = 1024 * 1024 * 1024
#: Every other file of the attempt is carried only when it is small: the records, plans and reports.
OTHER_FILE_MAX_BYTES = 32 * 1024 * 1024
#: The most the seed carries in all, before compression.
TOTAL_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: Never carried: archives (the bundle and the solver zips are downloads already) and the record of the
#: files a big result kept in the exchange bucket, which is emptied once the delivery is stored (RUN-7)
_SKIP_SUFFIXES = (".tar.gz", ".tgz", ".zip")
_ATTEMPT = re.compile(r"^generation_\d+/attempt_\d+$")


def _kept_record_name() -> str:
    from meshpipeline.engines.bucket_files import KEPT_FILE
    return KEPT_FILE


def _attempt_rel(job_id: str, workspace: Path) -> str:
    """generation_<g>/attempt_<n> of the attempt, as job_service.final_attempt_workspace finds it."""
    try:
        rel = workspace.resolve().relative_to((Path(rtcfg.WORKSPACE_BASE) / str(job_id)).resolve()).as_posix()
    except ValueError:
        rel = ""
    return rel if _ATTEMPT.match(rel) else "generation_0/attempt_0"


def _named_files(workspace: Path) -> set[Path]:
    """The files the manifest names for the reviewer (mesh_paths: the surface it opens, the volume)."""
    try:
        paths = (json.loads((workspace / "mesh_manifest.json").read_text()) or {}).get("mesh_paths") or {}
    except (OSError, ValueError, AttributeError):
        return set()
    out: set[Path] = set()
    root = workspace.resolve()
    for v in (paths.values() if isinstance(paths, dict) else ()):
        if not v:
            continue
        p = Path(str(v))
        p = (p if p.is_absolute() else workspace / p).resolve()
        if p.is_file() and root in p.parents:
            out.add(p)
    return out


def members(workspace: Path, *, skip: Iterable[Path] = ()) -> list[tuple[str, Path]]:
    """(name in the seed, file) for every file the seed carries, named files first, in a stable order.
    `skip`: files never carried (the delivery's own scratch files)."""
    root = Path(workspace).resolve()
    named = _named_files(root)
    kept_record = _kept_record_name()
    left_out = {Path(s).resolve() for s in skip}
    chosen: list[tuple[str, Path]] = []
    total = 0
    candidates = sorted(named) + sorted(p for p in root.rglob("*") if p not in named)
    for p in candidates:
        if p in left_out:
            continue
        # a link anywhere on the way (the file, or a directory it sits in) is never carried
        if p.resolve() != p or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == kept_record or rel.endswith(_SKIP_SUFFIXES) or any(s.startswith(".") for s in rel.split("/")):
            continue
        size = p.stat().st_size
        cap = NAMED_FILE_MAX_BYTES if p in named else OTHER_FILE_MAX_BYTES
        if size > cap or total + size > TOTAL_MAX_BYTES:
            continue
        chosen.append((rel, p))
        total += size
    return chosen


def pack(job_id: str, workspace: Path, dest: Path, *, skip: Iterable[Path] = ()) -> bool:
    """Write the seed of the delivered attempt to `dest` (a deterministic .tar.gz, so a repeated delivery
    of the same attempt stores the same bytes). False when there is nothing worth a second look (no
    manifest), and never raises past a logged warning: a seed that cannot be made never costs the delivery."""
    ws = Path(workspace)
    try:
        if not (ws / "mesh_manifest.json").is_file():
            return False
        record = json.dumps({"job_id": str(job_id), "attempt": _attempt_rel(job_id, ws),
                             "workspace": str(ws.resolve())}, sort_keys=True).encode()

        def _norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            return ti

        with gzip.GzipFile(filename="", fileobj=open(dest, "wb"), mode="wb", mtime=0,  # noqa: SIM115
                           compresslevel=1) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            info = _norm(tarfile.TarInfo(SEED_RECORD))
            info.size = len(record)
            tar.addfile(info, io.BytesIO(record))
            for rel, p in members(ws, skip=[*skip, dest]):
                tar.add(str(p), arcname=f"attempt/{rel}", recursive=False, filter=_norm)
        return True
    except Exception:  # noqa: BLE001 - the seed is for a later second look; the mesh is the delivery
        logger.warning("review seed: not written for job %s; a second look works only on this worker", job_id,
                       exc_info=True)
        return False


def _canonical(job_id: str) -> str:
    try:
        return str(uuid.UUID(str(job_id)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _repoint_manifest(attempt_dir: Path, old_root: str) -> None:
    """The manifest's mesh_paths named the attempt where it was made; point them at this copy."""
    path = attempt_dir / "mesh_manifest.json"
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    paths = manifest.get("mesh_paths") if isinstance(manifest, dict) else None
    if not isinstance(paths, dict) or not old_root:
        return
    new_root = str(attempt_dir.resolve())
    changed = False
    for k, v in list(paths.items()):
        if isinstance(v, str) and (v == old_root or v.startswith(old_root.rstrip("/") + "/")):
            paths[k] = new_root + v[len(old_root.rstrip("/")):]
            changed = True
    if changed:
        path.write_text(json.dumps(manifest, indent=2, default=str))


def restore(job_id: str, *, store, jlog=None) -> Path | None:
    """Bring the delivered attempt of `job_id` back from its review seed under this worker's workspace root,
    at the place job_service.final_attempt_workspace looks. None when the job has no seed (delivered before
    seeds were kept, or its files were removed after the retention period). A store that cannot be reached
    raises, so the caller can say it was ours."""
    from meshpipeline.artifact_keys import review_seed_key
    from meshpipeline.sandbox.safe_extract import default_limits, safe_extract_tar

    log = jlog or logger
    job = _canonical(job_id)
    if not job:
        return None
    key = review_seed_key(job)
    if not store.exists(object_key=key):
        return None
    root = Path(rtcfg.WORKSPACE_BASE) / job
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".review_seed_", dir=str(root)))
    try:
        archive = staging / "seed.tar.gz"
        store.download_file(object_key=key, destination=archive)
        out = staging / "out"
        limits = replace(default_limits(), max_archive_bytes=max(archive.stat().st_size + 1, 1),
                         max_total_uncompressed_bytes=TOTAL_MAX_BYTES + 1024 * 1024,
                         max_file_bytes=NAMED_FILE_MAX_BYTES)
        safe_extract_tar(path=archive, dest=out, limits=limits)
        record = json.loads((out / SEED_RECORD).read_text())
        rel = str(record.get("attempt") or "")
        if not _ATTEMPT.match(rel) or not (out / "attempt" / "mesh_manifest.json").is_file():
            log.warning("review seed of job %s is not a delivered attempt - not restored", job)
            return None
        target = root / rel
        if not (target / "mesh_manifest.json").is_file():
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(out / "attempt"), str(target))
            _repoint_manifest(target, str(record.get("workspace") or ""))
        log.info("review seed: job %s's delivered attempt restored from the object store (%s)", job, rel)
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = ["NAMED_FILE_MAX_BYTES", "OTHER_FILE_MAX_BYTES", "RULE", "SEED_LOGICAL_KEY", "SEED_RECORD",
           "TOTAL_MAX_BYTES", "members", "pack", "restore"]
