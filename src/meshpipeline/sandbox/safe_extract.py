# Responsibility: Extract an untrusted archive without letting it escape or exhaust the machine.
# Boundaries: bounded by size, entry count, compression ratio and member type, and confined to the destination.
from __future__ import annotations

import shutil
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NoReturn

from meshpipeline.settings.env import optional_env


class WorkspaceExtractionError(Exception):
    pass


@dataclass(frozen=True)
class ExtractionLimits:
    max_archive_bytes: int              # compressed archive size ceiling
    max_entries: int                    # total member count
    max_total_uncompressed_bytes: int   # cumulative expanded size
    max_file_bytes: int                 # any single regular file
    max_depth: int                      # path component depth
    max_path_len: int                   # characters in a member name
    max_ratio: float                    # uncompressed / compressed (decompression-bomb guard)
    allow_symlinks: bool = False
    allow_hardlinks: bool = False


def default_limits() -> ExtractionLimits:
    return ExtractionLimits(
        max_archive_bytes=int(optional_env("WORKSPACE_ARCHIVE_MAX_BYTES", str(512 * 1024 * 1024))),
        max_entries=int(optional_env("WORKSPACE_ARCHIVE_MAX_ENTRIES", "20000")),
        max_total_uncompressed_bytes=int(
            optional_env("WORKSPACE_ARCHIVE_MAX_TOTAL_BYTES", str(4 * 1024 * 1024 * 1024))),
        max_file_bytes=int(optional_env("WORKSPACE_ARCHIVE_MAX_FILE_BYTES", str(1024 * 1024 * 1024))),
        max_depth=int(optional_env("WORKSPACE_ARCHIVE_MAX_DEPTH", "40")),
        max_path_len=int(optional_env("WORKSPACE_ARCHIVE_MAX_PATH_LEN", "1024")),
        max_ratio=float(optional_env("WORKSPACE_ARCHIVE_MAX_RATIO", "200.0")),
    )


def validate_limits(limits: ExtractionLimits) -> None:
    for name, v in (("max_archive_bytes", limits.max_archive_bytes),
                    ("max_entries", limits.max_entries),
                    ("max_total_uncompressed_bytes", limits.max_total_uncompressed_bytes),
                    ("max_file_bytes", limits.max_file_bytes),
                    ("max_depth", limits.max_depth),
                    ("max_path_len", limits.max_path_len)):
        if int(v) <= 0:
            raise WorkspaceExtractionError(f"extraction limit {name} must be positive, got {v!r}")
    if float(limits.max_ratio) <= 0:
        raise WorkspaceExtractionError(f"extraction limit max_ratio must be positive, got "
                                       f"{limits.max_ratio!r}")


def _reject(msg: str) -> NoReturn:
    raise WorkspaceExtractionError(msg)


def _safe_member_path(dest: Path, name: str, limits: ExtractionLimits) -> Path:
    if not name or name in (".", "./"):
        _reject("archive entry has an empty name")
    if len(name) > limits.max_path_len:
        _reject(f"archive entry path is longer than {limits.max_path_len} chars: {name[:60]}…")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):   # absolute / windows drive
        _reject(f"archive entry has an absolute path: {name!r}")
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        _reject(f"archive entry contains a '..' traversal: {name!r}")
    if len(parts) > limits.max_depth:
        _reject(f"archive entry exceeds max depth {limits.max_depth}: {name!r}")
    target = (dest / Path(*parts)).resolve()
    if not (target == dest.resolve() or dest.resolve() in target.parents):
        _reject(f"archive entry resolves outside the destination: {name!r}")
    return target


def _validate_members(members: list[tarfile.TarInfo], dest: Path, limits: ExtractionLimits,
                      archive_bytes: int) -> None:
    if len(members) > limits.max_entries:
        _reject(f"archive has {len(members)} entries, over the {limits.max_entries} cap")
    total = 0
    seen: set[str] = set()
    for m in members:
        _safe_member_path(dest, m.name, limits)                 # path safety (raises on violation)
        norm = m.name.replace("\\", "/").rstrip("/")
        if norm in seen:
            _reject(f"archive has a duplicate entry path: {m.name!r}")
        seen.add(norm)
        if m.issym() and not limits.allow_symlinks:
            _reject(f"archive contains a symlink (disallowed): {m.name!r}")
        if m.islnk() and not limits.allow_hardlinks:
            _reject(f"archive contains a hard link (disallowed): {m.name!r}")
        if m.ischr() or m.isblk() or m.isfifo() or m.isdev():
            _reject(f"archive contains a device/FIFO special file: {m.name!r}")
        if m.mode & (stat.S_ISUID | stat.S_ISGID):
            _reject(f"archive entry has setuid/setgid bits: {m.name!r}")
        if m.isreg():
            if m.size > limits.max_file_bytes:
                _reject(f"archive entry {m.name!r} is {m.size} bytes, over the "
                        f"{limits.max_file_bytes} per-file cap")
            total += m.size
    if total > limits.max_total_uncompressed_bytes:
        _reject(f"archive expands to {total} bytes, over the "
                f"{limits.max_total_uncompressed_bytes} total cap (possible decompression bomb)")
    if archive_bytes > 0 and (total / archive_bytes) > limits.max_ratio:
        _reject(f"archive compression ratio {total / archive_bytes:.1f}x exceeds the "
                f"{limits.max_ratio}x cap (possible decompression bomb)")


def safe_extract_tar(*, fileobj: BinaryIO | None = None, path: str | Path | None = None,
                     dest: str | Path, limits: ExtractionLimits | None = None) -> int:
    lim = limits or default_limits()
    validate_limits(lim)
    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)

    # compressed archive size (for the ratio/decompression-bomb guard)
    archive_bytes = 0
    if path is not None:
        archive_bytes = Path(path).stat().st_size
    elif fileobj is not None:
        try:
            cur = fileobj.tell()
            fileobj.seek(0, 2)
            archive_bytes = fileobj.tell()
            fileobj.seek(cur)
        except (OSError, ValueError):
            archive_bytes = 0
    if archive_bytes > lim.max_archive_bytes:
        _reject(f"archive is {archive_bytes} bytes, over the {lim.max_archive_bytes} cap")

    written = 0
    try:
        with tarfile.open(name=str(path) if path is not None else None,
                          fileobj=fileobj, mode="r:gz") as tf:
            try:
                members = tf.getmembers()
            except (tarfile.TarError, OSError, EOFError) as exc:
                _reject(f"archive is malformed or truncated: {type(exc).__name__}")
            _validate_members(members, dest_p, lim, archive_bytes)

            for m in members:
                target = _safe_member_path(dest_p, m.name, lim)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not m.isreg():
                    continue                                    # non-reg already rejected in validation
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    _reject(f"archive entry {m.name!r} could not be read")
                remaining = m.size
                with open(target, "wb") as out:
                    while remaining > 0:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                        written += len(chunk)
                        if written > lim.max_total_uncompressed_bytes:
                            _reject("archive wrote more than the total-bytes cap mid-stream")
                actual = target.stat().st_size
                if actual != m.size:
                    _reject(f"archive entry {m.name!r} wrote {actual} bytes, expected {m.size}")
    except WorkspaceExtractionError:
        shutil.rmtree(dest_p, ignore_errors=True)               # remove the partial tree, fail closed
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(dest_p, ignore_errors=True)
        raise WorkspaceExtractionError(
            f"archive extraction failed: {type(exc).__name__}") from exc
    return written


__all__ = ["WorkspaceExtractionError", "ExtractionLimits", "default_limits", "validate_limits",
           "safe_extract_tar"]
