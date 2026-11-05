# Responsibility: Keep a big mesh result's delivery-only files in the exchange bucket, so the worker that collects the run never holds them on its disk or in its memory.
# Owns: which files stay in the bucket, the record of them that rides home (KEPT_FILE), the light stand-ins the worker reads instead, and the byte streams delivery builds its downloads from.
# Boundaries: no storage client - the caller hands in a writer or a reader for one object; it decides no verdict and changes no mesh.
# Collaborates with: adapters/mesh_execution/gcs_exchange (the mesher side: keep, then archive the rest), adapters/mesh_execution/cloud_run_client (the worker side: collect), application/artifact_uploader (delivery streams), engines/gmsh/viewer_surface + engines/manifest (the stand-ins).
"""BIG RESULTS STAY IN THE BUCKET (RUN-7).

A mesh run on a big machine sends its workspace home to a small worker (4 CPUs, 16 GB of memory,
about 90 GB of disk). A 34 M-element FEA mesh is about 37 GB of files: the CalculiX, Nastran and
Ansys decks (about 860 bytes an element together) and gmsh's own mesh (about 210). The worker only
ever asks whether the decks are THERE; it reads none of them. It does read gmsh's mesh, twice (the
3D view and the reviewer's pictures), and gmsh opening a 34 M-element file needs more memory than
the worker has.

And the worker used to compress the delivery bundle itself: a 107 M-cell polyMesh (22 GB) took
93 minutes at gzip level 9 on its 4 CPUs.

So on the mesher, before the workspace is archived, every file of PACK_FROM_BYTES or more goes to
the exchange bucket on its own, already packed as the bundle needs it - raw DEFLATE of its tar
data (its bytes and the tar padding), level 1, compressed on all the mesher's CPUs, with its CRC-32
so the bundle's single gzip member gets its trailer without the worker reading it:
  * a file no worker step reads past its presence (the decks; an FEA plan's gmsh mesh), when it is
    KEEP_FROM_BYTES or more, is KEPT there; when gmsh's mesh is one, the mesher - which has the
    memory - first writes the light stand-ins the worker reads instead: the viewer's surface
    (VIEWER_MESH_FILE, thinned for a big mesh) and a surface-only mesh for the reviewer
    (REVIEW_SURFACE_FILE);
  * every other big file (a flow mesh's polyMesh, which the worker's checks read) is FETCHED: the
    worker unpacks it to its place at collection;
  * the record of both (KEPT_FILE) rides home in the archive, which carries only the small files.

On the worker: a kept file counts as present (`present`), and the delivery streams every download
straight from the bucket into the artifact store (`raw_chunks`, `bundle_chunks`) - the bundle's big
members, kept or fetched and unchanged, pass through as the mesher packed them, so nothing big is
compressed on the worker or written to its disk - and the objects are deleted once the delivery is
stored (`release`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

#: The rule this module is (docs/RULES.md).
RULE = "RUN-7"
#: The record of the kept files, beside the mesh (it rides home in the archive).
KEPT_FILE = "kept_in_bucket.json"
#: The viewer's surface of a kept gmsh mesh: its boundary, as engines/gmsh/viewer_surface reads it.
VIEWER_MESH_FILE = "viewer_mesh.npz"
#: A surface-only copy of a kept gmsh mesh, with its named groups, for the reviewer's pictures.
REVIEW_SURFACE_FILE = "review_surface.msh"

#: A file this big stays in the bucket when no worker step reads it. Below it the file rides home
#: as before: a few hundred MB is no burden, and the regression meshes all stay exactly as they are.
KEEP_FROM_BYTES = 256 * 1024 * 1024
#: Files no worker step reads past their presence: the solver decks of any mesh.
DECKS = ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.unv")
#: gmsh's own mesh, kept only for an FEA plan (its worker readers have stand-ins - the viewer and
#: the reviewer). A flow mesh's mesh.msh is the boundary the worker's gates read: it comes home.
FEA_MESH = "mesh.msh"
FEA_PLAN = "fea_plan.json"

#: A file this big is PACKED on the mesher, whatever it is: sent home on its own, already in the
#: bundle's own form, so the worker never compresses it. A file the worker reads (a flow mesh's
#: polyMesh) is FETCHED - unpacked to its place at collection; one it never reads stays KEPT.
PACK_FROM_BYTES = 64 * 1024 * 1024
#: Kept here so the worker can tell a fetched file it changed since (then packed again) from one
#: it did not (its packed bytes go into the bundle as they are).
FETCHED_FILE = "fetched_from_bucket.json"
#: Everything this module writes into a workspace: cleared before a new result lands, so a record
#: or a stand-in never outlives the result it describes.
STAND_INS = (KEPT_FILE, VIEWER_MESH_FILE, REVIEW_SURFACE_FILE, FETCHED_FILE)

#: How each file is packed: DEFLATE at level 1 (the delivery's bundle at level 9 took 93 minutes on
#: the worker for a 22 GB polyMesh; the mesher packs it once, fast, on all its CPUs), in pieces of
#: PACK_PIECE_BYTES, each ending on a sync flush so the pieces join as one stream (the same bytes for
#: the same file, so a retried delivery stores the same bundle), compressed in parallel, in order.
GZIP_LEVEL = 1
PACK_PIECE_BYTES = 64 * 1024 * 1024
PACK_THREADS = 8
_CHUNK = 8 * 1024 * 1024
_BLOCK = tarfile.BLOCKSIZE          # 512
_RECORD = tarfile.RECORDSIZE        # 20 blocks: tar pads its end to this


class KeptFileError(RuntimeError):
    """The record of kept files is not one this worker can trust."""


@dataclass(frozen=True)
class Kept:
    rel: str            # the file's path in the workspace, posix
    bucket: str
    key: str            # the gzip member's object key
    bytes: int          # the file's own size
    gz_bytes: int       # the object's size
    gz_md5: str         # the object's md5, hex
    mode: int           # the file's permission bits
    fetch: bool = False  # the worker unpacks it to its place at collection (it reads it)
    crc32: int = 0      # CRC-32 of its tar data (the bytes and the padding)


# ------------------------------------------------------------------------------------------ mesher

def keepable(workspace) -> list[Path]:
    """The files of `workspace` that stay in the bucket: big (KEEP_FROM_BYTES) and in KEEPABLE -
    the decks always, gmsh's mesh for an FEA plan. Largest first."""
    ws = Path(workspace)
    names = list(DECKS) + ([FEA_MESH] if (ws / FEA_PLAN).is_file() else [])
    out = []
    for name in names:
        p = ws / name
        if p.is_file() and not p.is_symlink() and p.stat().st_size >= KEEP_FROM_BYTES:
            out.append(p)
    return sorted(out, key=lambda p: -p.stat().st_size)


def packable(workspace) -> list[Path]:
    """Every file of `workspace` sent home on its own: PACK_FROM_BYTES or more (the kept ones among
    them). Largest first."""
    ws = Path(workspace)
    own = {ws / n for n in STAND_INS}
    out = [p for p in ws.rglob("*") if p.is_file() and not p.is_symlink() and p not in own
           and p.stat().st_size >= PACK_FROM_BYTES]
    return sorted(out, key=lambda p: -p.stat().st_size)


def object_key(output_key: str, rel: str, run: str) -> str:
    """Where a kept file lives: beside the run's output archive, under this mesher run's own token
    (two passes of one job may share the output key; a pass kept for later must never be
    overwritten by the next), under its own name."""
    return f"{output_key}.kept/{run}/{rel}.gz"


def _is_object_of(key: str, output_key: str, rel: str) -> bool:
    head, tail = f"{output_key}.kept/", f"/{rel}.gz"
    if not (key.startswith(head) and key.endswith(tail)):
        return False
    run = key[len(head):len(key) - len(tail)]
    return bool(run) and "/" not in run and run not in (".", "..")


def _padding(n: int) -> bytes:
    rem = n % _BLOCK
    return b"\0" * (_BLOCK - rem) if rem else b""


def _deflate_piece(data: bytes) -> bytes:
    """`data` as raw DEFLATE ending on a sync flush: byte-aligned, not final, so pieces made apart -
    in parallel, on another machine - joined end to end are ONE deflate stream (pigz's method). The
    bundle wraps them in a single gzip member: every reader reads it, a stream reader included
    (Python's tarfile "r|gz" refuses a file of many gzip members)."""
    z = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, -15)
    return z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)


def pack(src: Path, sink: BinaryIO, *, threads: int | None = None) -> tuple[int, int, str, int]:
    """Write `src` to `sink` as raw DEFLATE pieces of its tar data (the bytes, then the padding to
    the next 512-byte block): one piece per PACK_PIECE_BYTES, compressed `threads` at a time and
    written in order. Returns (the file's bytes, the object's bytes, the object's md5, the CRC-32
    of the tar data - what the bundle's gzip trailer is made from)."""
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    md5 = hashlib.md5()  # noqa: S324 - an integrity check against the store's own md5, not a secret
    raw = out = crc = 0
    size = src.stat().st_size
    workers = max(1, int(threads or PACK_THREADS))

    def put(b: bytes) -> None:
        nonlocal out
        if b:
            sink.write(b)
            md5.update(b)
            out += len(b)

    with src.open("rb") as fh, ThreadPoolExecutor(max_workers=workers) as pool:
        pending: deque = deque()
        while chunk := fh.read(PACK_PIECE_BYTES):
            raw += len(chunk)
            if raw >= size:
                chunk += _padding(raw)          # the last piece carries tar's padding
            crc = zlib.crc32(chunk, crc)
            pending.append(pool.submit(_deflate_piece, chunk))
            while len(pending) >= 2 * workers:  # bounded: only so many pieces in memory
                put(pending.popleft().result())
        while pending:
            put(pending.popleft().result())
    if raw != size:
        raise KeptFileError(f"{src.name} changed while it was packed")
    if raw == 0:
        put(_deflate_piece(b""))
    return raw, out, md5.hexdigest(), crc


def write_record(workspace, kept: Iterable[Kept]) -> None:
    rows = {k.rel: {"bucket": k.bucket, "key": k.key, "bytes": k.bytes, "gz_bytes": k.gz_bytes,
                    "gz_md5": k.gz_md5, "mode": k.mode, "fetch": bool(k.fetch), "crc32": k.crc32}
            for k in kept}
    (Path(workspace) / KEPT_FILE).write_text(json.dumps({"version": 1, "rule": RULE, "files": rows}, indent=1,
                                                        sort_keys=True))


def write_stand_ins(workspace) -> list[str]:
    """The light copies the worker reads in place of a kept gmsh mesh: the viewer's surface and the
    reviewer's surface-only mesh. Written while the mesh is still here; each one that cannot be
    made is logged and left out (the worker then says the view is missing - the mesh still ships)."""
    ws = Path(workspace)
    if not (ws / FEA_MESH).is_file():
        return []
    made = []
    try:
        from meshpipeline.engines.fea_results import viewer_face_names
        from meshpipeline.engines.gmsh.viewer_surface import read_msh, save_viewer_mesh
        mesh = read_msh(ws, by_face=True, face_names=viewer_face_names(ws))
        if mesh:
            save_viewer_mesh(ws / VIEWER_MESH_FILE, mesh)
            made.append(VIEWER_MESH_FILE)
    except Exception:  # noqa: BLE001 - a stand-in is a view, never a reason to lose the mesh
        logger.warning("kept files: the viewer's surface was not written", exc_info=True)
    try:
        from meshpipeline.engines.gmsh.viewer_surface import write_surface_only
        if write_surface_only(ws / FEA_MESH, ws / REVIEW_SURFACE_FILE):
            made.append(REVIEW_SURFACE_FILE)
    except Exception:  # noqa: BLE001
        logger.warning("kept files: the reviewer's surface was not written", exc_info=True)
    return made


def keep(workspace, *, bucket: str, output_key: str,
         writer: Callable[[str, str], Any], parallel: int = 4) -> list[Kept]:
    """Send the big files of `workspace` home on their own (the module text): the stand-ins first,
    then each packable file packed to `writer(bucket, key)` - a context manager giving a binary
    writer - and removed here once stored (the archive then carries the rest); then the record,
    each row marked to be fetched home or kept in the bucket. Returns every row."""
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    ws = Path(workspace)
    files = packable(ws)
    if not files:
        return []
    stays = set(keepable(ws))
    if stays:
        write_stand_ins(ws)
    run = uuid.uuid4().hex

    def one(p: Path) -> Kept:
        rel = p.relative_to(ws).as_posix()
        key = object_key(output_key, rel, run)
        mode = p.stat().st_mode & 0o7777
        with writer(bucket, key) as sink:
            raw, gz, md5, crc = pack(p, sink)
        return Kept(rel=rel, bucket=bucket, key=key, bytes=raw, gz_bytes=gz, gz_md5=md5, mode=mode,
                    fetch=p not in stays, crc32=crc)

    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(files)))) as pool:
        kept = list(pool.map(one, files))      # zlib and the upload release the GIL
    # the record first: a file is only removed here once its record says where it is
    write_record(ws, kept)
    for k in kept:
        (ws / k.rel).unlink()
    logger.info("sent on their own: %s", ", ".join(
        f"{k.rel} ({k.bytes / 1e9:.2f} GB, {'fetched' if k.fetch else 'kept'})" for k in kept))
    return kept


# ------------------------------------------------------------------------------------------ worker

def clear(workspace) -> None:
    """Drop the record and the stand-ins: a new result is about to land, or a new run to start."""
    for name in STAND_INS:
        try:
            (Path(workspace) / name).unlink()
        except FileNotFoundError:
            pass


def read_record(workspace, *, bucket: str | None = None, prefix: str | None = None) -> dict[str, Kept]:
    """The kept files of `workspace` by path, {} when none. With `bucket` and `prefix` (the
    collecting worker's own exchange coordinates) every row must name an object of THIS run - a
    record is the remote's word, and a worker never reads an object it did not give out."""
    p = Path(workspace) / KEPT_FILE
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text())
        rows = doc["files"]
        if not isinstance(rows, Mapping):
            raise TypeError("files is not a mapping")
        out: dict[str, Kept] = {}
        for rel, r in rows.items():
            pure = PurePosixPath(str(rel))
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or "\\" in str(rel):
                raise KeptFileError(f"kept file {rel!r} is not a workspace path")
            k = Kept(rel=pure.as_posix(), bucket=str(r["bucket"]), key=str(r["key"]),
                     bytes=int(r["bytes"]), gz_bytes=int(r["gz_bytes"]), gz_md5=str(r["gz_md5"]),
                     mode=int(r["mode"]) & 0o7777, fetch=bool(r.get("fetch", False)),
                     crc32=int(r["crc32"]) & 0xFFFFFFFF)
            if k.bytes <= 0 or k.gz_bytes <= 0:
                raise KeptFileError(f"kept file {rel!r} is empty")
            if bucket is not None and k.bucket != bucket:
                raise KeptFileError(f"kept file {rel!r} is in another bucket")
            if prefix is not None and not _is_object_of(k.key, prefix, k.rel):
                raise KeptFileError(f"kept file {rel!r} is not this run's object")
            out[k.rel] = k
        return out
    except KeptFileError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise KeptFileError(f"the kept-files record cannot be read: {type(exc).__name__}") from exc


def kept(workspace) -> dict[str, Kept]:
    """read_record, with an unreadable record read as none (logged): for the readers that only ask
    whether a file is there."""
    try:
        return read_record(workspace)
    except KeptFileError:
        logger.warning("kept files: the record is unreadable", exc_info=True)
        return {}


def present(workspace, rel: str) -> bool:
    """Whether `rel` is part of this result: here, non-empty, or kept in the bucket."""
    p = Path(workspace) / rel
    if p.is_file() and p.stat().st_size > 0:
        return True
    return PurePosixPath(rel).as_posix() in kept(workspace)


def size(workspace, rel: str) -> int:
    p = Path(workspace) / rel
    if p.is_file():
        return int(p.stat().st_size)
    k = kept(workspace).get(PurePosixPath(rel).as_posix())
    return int(k.bytes) if k else 0


#: A reader of one kept object: (bucket, key) -> a binary file object, read to its end.
Opener = Callable[[str, str], BinaryIO]


def _inflate(raw: BinaryIO) -> Iterator[bytes]:
    """The decompressed bytes of a kept object: raw DEFLATE pieces end to end (see _deflate_piece)."""
    d = zlib.decompressobj(-15)
    while chunk := raw.read(_CHUNK):
        out = d.decompress(chunk)
        if out:
            yield out
    tail = d.flush()
    if tail:
        yield tail


def raw_chunks(k: Kept, opener: Opener) -> Iterator[bytes]:
    """The kept file's own bytes, streamed out of its object: decompressed, the tar padding cut,
    and its size checked - a short or long object is refused, never delivered as the file."""
    left = k.bytes
    with opener(k.bucket, k.key) as fh:
        for out in _inflate(fh):
            if left <= 0:
                if out.strip(b"\0"):
                    raise KeptFileError(f"{k.rel}: the stored object is longer than the file")
                continue
            take = out[:left]
            left -= len(take)
            yield take
    if left:
        raise KeptFileError(f"{k.rel}: the stored object is {left} bytes short of the file")


#: How close to the fetch a file's last change may be before its stamp alone cannot prove the file
#: unchanged. A file system keeps time in ticks (a few ms on Linux, up to two seconds on some), so a
#: same-size rewrite inside the tick of the fetch leaves the stamp exactly as it was. Then the
#: content decides: its CRC-32, which the record already carries (git's "racy" index rule).
RACY_NS = 3 * 1_000_000_000


def _stat(p: Path) -> os.stat_result:
    return os.stat(p, follow_symlinks=False)


def _stamp(p: Path) -> list[int]:
    """What a fetched file must still match to count as unchanged: its size, its content and
    status-change times, and its inode (a file swapped in by rename, or its time set back, differs)."""
    st = _stat(p)
    return [int(st.st_size), int(st.st_mtime_ns), int(st.st_ctime_ns), int(st.st_ino)]


def _tar_crc(p: Path) -> int:
    """The CRC-32 of a file's tar data (its bytes, then the padding): what pack() recorded."""
    crc = n = 0
    with p.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            n += len(chunk)
            crc = zlib.crc32(chunk, crc)
    return zlib.crc32(_padding(n), crc) & 0xFFFFFFFF


def _unchanged(p: Path, k: Kept, record: Mapping[str, Any]) -> bool:
    """Whether the fetched file at `p` is still exactly what was fetched, so its packed bytes may go
    into the bundle as they are. A file the worker changed must never ship stale: its stamp must
    match the one taken at the fetch, and when its times are too close to that moment for the stamp
    to tell (RACY_NS), its content is read and must match the packed file's CRC-32. A big file is
    fetched long before the record is taken, so only the last few files ever need that read."""
    files = record.get("files")
    was = files.get(k.rel) if isinstance(files, Mapping) else None
    if not isinstance(was, list) or len(was) != 4 or was[0] != k.bytes:
        return False
    try:
        if p.is_symlink() or not p.is_file() or _stamp(p) != was:
            return False
        at = record.get("at")
        if isinstance(at, int) and max(was[1], was[2]) < at - RACY_NS:
            return True     # last changed well before the stamp: any change since shows in it
        return _tar_crc(p) == k.crc32
    except OSError:
        return False


def fetch(workspace, opener: Opener, *, threads: int = 4) -> int:
    """At collection: unpack every row marked fetch to its place in `workspace` (a temporary name,
    then renamed over), its size checked, its permission bits restored; then, once every file is in
    place, note each one's stamp and the moment it was taken (FETCHED_FILE), so the bundle passes
    its packed bytes through only while the file is as fetched (_unchanged). Returns the bytes
    written."""
    from concurrent.futures import ThreadPoolExecutor
    ws = Path(workspace)
    rows = [k for k in read_record(ws).values() if k.fetch]
    if not rows:
        return 0

    def one(k: Kept) -> int:
        dest = ws / k.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.fetching")
        try:
            with tmp.open("wb") as fh:
                for chunk in raw_chunks(k, opener):
                    fh.write(chunk)
            tmp.chmod(k.mode)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return k.bytes

    with ThreadPoolExecutor(max_workers=max(1, min(threads, len(rows)))) as pool:
        total = sum(pool.map(one, rows))
    files = {k.rel: _stamp(ws / k.rel) for k in rows}
    at = time.time_ns()                 # after every stamp: a change from here on may share its tick
    (ws / FETCHED_FILE).write_text(json.dumps({"version": 2, "at": at, "files": files}, sort_keys=True))
    return total


def _as_fetched(workspace: Path) -> dict[str, Any]:
    """The fetch record, {} when absent or of another version (every fetched file then counts as
    changed and is packed again here: slower, never stale)."""
    try:
        d = json.loads((workspace / FETCHED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("version") != 2:
        return {}
    return d


def packed_chunks(k: Kept, opener: Opener) -> Iterator[bytes]:
    """The kept object's own bytes (a gzip member of the file's tar data), its size and md5 checked."""
    md5 = hashlib.md5()  # noqa: S324
    n = 0
    with opener(k.bucket, k.key) as fh:
        while chunk := fh.read(_CHUNK):
            md5.update(chunk)
            n += len(chunk)
            yield chunk
    if n != k.gz_bytes or md5.hexdigest() != k.gz_md5:
        raise KeptFileError(f"{k.rel}: the stored object does not match its record")


# ------------------------------------------------------------------------------------------ bundle

#: The bundle's one gzip header: deflate, no flags, no time, no extra flags, OS unknown.
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


def _gf2_times(mat: list[int], vec: int) -> int:
    out, i = 0, 0
    while vec:
        if vec & 1:
            out ^= mat[i]
        vec >>= 1
        i += 1
    return out


def _gf2_square(mat: list[int]) -> list[int]:
    return [_gf2_times(mat, mat[n]) for n in range(32)]


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """The CRC-32 of A followed by B, from A's CRC, B's CRC and B's length (zlib's crc32_combine) -
    the bundle's gzip trailer covers members packed on another machine without reading them."""
    if len2 <= 0:
        return crc1
    odd = [0xEDB88320] + [1 << (n - 1) for n in range(1, 32)]
    even = _gf2_square(odd)
    odd = _gf2_square(even)
    while True:
        even = _gf2_square(odd)
        if len2 & 1:
            crc1 = _gf2_times(even, crc1)
        len2 >>= 1
        if not len2:
            break
        odd = _gf2_square(even)
        if len2 & 1:
            crc1 = _gf2_times(odd, crc1)
        len2 >>= 1
        if not len2:
            break
    return (crc1 ^ crc2) & 0xFFFFFFFF


def _norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _header(ti: tarfile.TarInfo) -> bytes:
    import sys
    return ti.tobuf(tarfile.PAX_FORMAT, sys.getfilesystemencoding(), "surrogateescape")


def bundle_entries(workspace, members: Iterable[str], prefix: str) -> list[tuple[str, Path | None, Kept | None]]:
    """The bundle's entries in tarfile's own order (each member, a directory's children sorted
    below it): (archive name, the local path or None, the kept file or None). A member neither
    here nor kept is left out, as tarfile.add leaves out an optional member that is absent."""
    ws = Path(workspace)
    kept_files = kept(ws)
    as_fetched = _as_fetched(ws)
    out: list[tuple[str, Path | None, Kept | None]] = []

    def walk(rel: str) -> None:
        p = ws / rel
        name = f"{prefix}/{rel}"
        if rel in kept_files and not p.exists():
            out.append((name, None, kept_files[rel]))
            return
        k = kept_files.get(rel)
        if k is not None and k.fetch and _unchanged(p, k, as_fetched):
            # fetched and not changed since: its packed bytes go in as they are (nothing compressed here)
            out.append((name, None, k))
            return
        if p.is_dir() and not p.is_symlink():
            out.append((name, p, None))
            under = {c.name for c in p.iterdir()}
            under |= {PurePosixPath(r).name for r in kept_files if PurePosixPath(r).parent.as_posix() == rel}
            for child in sorted(under):
                walk(f"{rel}/{child}")
            return
        if p.exists():
            out.append((name, p, None))

    for rel in sorted(members):
        walk(rel)
    return out


def bundle_chunks(entries: list[tuple[str, Path | None, Kept | None]], opener: Opener) -> Iterator[bytes]:
    """The deterministic .tar.gz of `entries`, streamed: ONE gzip member whose deflate stream is
    each header and each local file compressed here, each kept file's object passed through as it
    is stored (raw DEFLATE pieces on sync flushes join as one stream), and tar's end; its trailer's
    CRC-32 is combined from each part's own. Every gzip reader reads it - a stream reader too -
    and nothing is written to disk."""
    import io
    import struct
    probe = tarfile.open(fileobj=io.BytesIO(), mode="w")      # builds headers only; writes nothing
    written = crc = 0

    def local(data: bytes) -> bytes:
        nonlocal written, crc
        written += len(data)
        crc = zlib.crc32(data, crc)
        return _deflate_piece(data)

    yield _GZIP_HEADER
    for name, path, k in entries:
        if k is not None:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.type = int(k.bytes), int(k.mode), tarfile.REGTYPE
            yield local(_header(_norm(ti)))
            yield from packed_chunks(k, opener)
            span = k.bytes + len(_padding(k.bytes))
            crc = crc32_combine(crc, k.crc32, span)
            written += span
            continue
        assert path is not None
        ti = _norm(probe.gettarinfo(str(path), arcname=name))
        yield local(_header(ti))
        if ti.isreg():
            z = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, -15)
            n = 0
            with path.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    n += len(chunk)
                    crc = zlib.crc32(chunk, crc)
                    out = z.compress(chunk)
                    if out:
                        yield out
            pad = _padding(n)
            crc = zlib.crc32(pad, crc)
            yield z.compress(pad) + z.flush(zlib.Z_SYNC_FLUSH)
            written += n + len(pad)
    end = b"\0" * (2 * _BLOCK)
    rem = (written + len(end)) % _RECORD
    end += b"\0" * (_RECORD - rem) if rem else b""
    written += len(end)
    crc = zlib.crc32(end, crc)
    last = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, -15)
    yield last.compress(end) + last.flush(zlib.Z_FINISH)           # the stream's final block
    yield struct.pack("<II", crc & 0xFFFFFFFF, written & 0xFFFFFFFF)


def release(workspace, deleter: Callable[[str, str], None]) -> int:
    """Delete the kept objects once their delivery is stored; returns how many went. A failure is
    logged and left to the exchange bucket's own lifecycle (two days)."""
    gone = 0
    for k in kept(workspace).values():
        try:
            deleter(k.bucket, k.key)
            gone += 1
        except Exception as exc:  # noqa: BLE001 - tidying, after the delivery already stands
            logger.warning("kept files: %s was not deleted (%s); the bucket's lifecycle removes it",
                           k.rel, type(exc).__name__)
    return gone


__all__ = ["DECKS", "FEA_MESH", "FETCHED_FILE", "KEEP_FROM_BYTES", "KEPT_FILE", "PACK_FROM_BYTES",
           "REVIEW_SURFACE_FILE", "STAND_INS", "VIEWER_MESH_FILE", "Kept", "KeptFileError", "bundle_chunks",
           "bundle_entries", "clear", "fetch", "keep", "keepable", "kept", "object_key", "pack", "packable",
           "packed_chunks", "present", "raw_chunks", "read_record", "release", "size", "write_record",
           "write_stand_ins"]
