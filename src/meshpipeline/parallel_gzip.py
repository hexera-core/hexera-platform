# Responsibility: Compress one byte stream on every core the job has, as a SINGLE gzip member.
# Owns: the chunking, the dictionary each chunk is primed with, the gzip frame around them, and what a cut costs an estimate.
# Boundaries: bytes in, bytes out - it knows nothing of meshes, workspaces or what is being sent, and reads no setting.
# Boundaries (2): it imports NOTHING from meshpipeline - a leaf at the package root like artifact_keys, so the engine side may use it without reaching into an adapter. The cores come in as a number.
# Collaborates with: adapters/mesh_execution/gcs_exchange (writes the returned archive through it), engines/return_scope (measures with it, and charges cut_cost so its estimate still reads high), engines/determinism.job_cpus at both call sites (the cores the JOB has, never the host's).
"""A PARALLEL DEFLATE THAT STILL WRITES ONE GZIP MEMBER.

A mesh's workspace goes home as one gzip archive, gzip is one serial deflate, and so the last
thing a mesh run does is compress its whole workspace on a single core while the other seven idle.
Measured on the two-scale coupon's workspace (job 575f76e6, 1,121,116,160 bytes of tar): 111.8 s
at level 6 for 244,704,856 bytes, 10.0 MB/s. At the customer plate's 336,666,582 cells that same
pass is hours, and the mesh has been finished and idle for all of them.

HOW. The stream is cut into CHUNK_BYTES pieces; each is compressed in its own thread as RAW
deflate, primed with the previous WINDOW_BYTES of the STREAM as its dictionary and ended on a byte
boundary with Z_SYNC_FLUSH; the pieces are concatenated in order behind one gzip header. A
decompressor holds the last 32 KiB it emitted in its window, which is exactly what each chunk was
primed with, so the concatenation is ONE valid deflate stream rather than a sequence of members.
zlib releases the GIL while it compresses, so the threads are cores. This is pigz's method.

    one thread, level 6                            244,704,856 bytes  111.8 s
    this, level 6, 32 workers                      244,703,534 bytes    7.6 s   14.7x
    pigz -6 -p 32, the same bytes                  244,643,351 bytes    8.4 s   13.3x

There is therefore no case for putting pigz in the image, which is the only reason this is written
out by hand: it is as fast, on the same bytes, with nothing added to install.

WHY ONE MEMBER, AND NOT THE EASY SHAPE. Independent gzip MEMBERS concatenated are also a valid
gzip file, and compressing each chunk as its own member is the obvious parallel archive - no
dictionaries, no ordering care. But `tarfile`'s STREAMING reader stops at the first member's end,
and that reader is the one a mesh actually comes home through (sandbox/safe_extract uses
mode "r|gz"). Measured on a three-member archive: `gzip.decompress` returns all of it and `r|gz`
raises `ReadError: unexpected end of data`. The easy shape would have built the mesh, archived it
faster, and lost it on arrival - the same failure engines/return_scope exists to prevent, arriving
by a different road. Whatever is changed here, the output must stay a single member; the tests
hold a multi-member archive against the real reader to say so.

WHAT A CUT COSTS, which is the other side of the bargain. engines/return_scope decides what rides
home by estimating this archive BEFORE it is written, and an estimate that reads low loses a built
mesh. Cutting the stream can only move the size by terminating a Huffman block early and paying
for the next one's tree: measured over seven synthetic inputs at three chunk sizes (repetitive
ASCII, an owner-like cycle, formatted floats, zeros, random, half-and-half, one long line), the
worst a single cut cost was 20.6 bytes, and on the coupon's real workspace the cuts GAINED 19.7
bytes each - a boundary re-fits the tree, which pays off when the data's statistics change.
CUT_BYTES is charged anyway, at 195 times that worst case, because a measured worst case is not a
proof and this charge is invisible: 4 KiB against a 16 MiB chunk is 0.024% of the stream.
"""
from __future__ import annotations

import io
import struct
import zlib
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import BinaryIO

#: How much of the stream one core takes at a time. Large enough that a chunk is about a second of
#: level-6 work (the coupon's polyMesh compresses at 10-17 MB/s a core), so the thread hand-off and
#: the tree each cut re-fits are lost in the noise; small enough that the bytes in flight stay a
#: rounding error against the mesh job's 32 GiB (contracts/mesh_machine.CLOUD_RUN_GIB).
CHUNK_BYTES = 16 * 1024 * 1024
#: deflate's window - how far back a match may point, and so how much of the stream before a chunk
#: that chunk has to be primed with. Not a tunable: it is the format's.
WINDOW_BYTES = 32 * 1024
#: The cap on threads, which is also the cap on memory: at most MAX_WORKERS * _DEPTH * CHUNK_BYTES
#: is in flight, 1 GiB here. The 8-core Cloud Run mesher holds 256 MiB of that cap.
MAX_WORKERS = 32
#: Chunks in flight per worker, so no core waits on the single thread that writes.
_DEPTH = 2
#: What one cut is CHARGED against an estimate of the archive - see cut_cost and the module text.
#: 195 times the worst cut measured, and above the worst that can be reasoned too: a terminated
#: block costs five bytes of empty stored block, a dynamic Huffman header is a few hundred, and a
#: match broken across the boundary is at most 258 literals.
CUT_BYTES = 4096
#: The empty FINAL deflate block (BFINAL set, fixed Huffman, end-of-block). Z_SYNC_FLUSH never sets
#: BFINAL, so every chunk leaves the stream unfinished and this is what ends it.
_FINAL_BLOCK = b"\x03\x00"
#: magic, deflate, no flags, mtime 0, no extra flags, OS unknown. The mtime is 0 and not the clock
#: because nothing here is a file and two archives of one workspace should be the same bytes.
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
_GF2_BITS = 32


def workers(job_cpus: int) -> int:
    """How many threads to compress on, given the cores THE JOB has.

    Callers read that from `engines.determinism.job_cpus()` and NEVER from `os.cpu_count()`, which
    is the trap that function exists for - "Cloud Run hands the process a wide mask and
    os.cpu_count() reports the host". Compressing on the host's count inside an 8-core container
    buys nothing and oversubscribes the quota.

    The number is passed in rather than fetched so this module imports nothing from meshpipeline;
    the CAP stays here, because the cap is this module's own memory in flight and there has to be
    one answer to it rather than one per caller.
    """
    return max(1, min(MAX_WORKERS, job_cpus))


def cut_cost(stream_bytes: int, *, chunk_bytes: int | None = None) -> int:
    """What cutting a stream this long may add to its archive, for whoever estimates that archive
    before it is written (engines/return_scope) to CHARGE.

    A stream short enough to be one chunk is still charged one cut: it too ends on Z_SYNC_FLUSH and
    _FINAL_BLOCK rather than on deflate's own last block.

    The chunk size is read HERE and not taken as a default argument, which would freeze it at
    import: the charge and the writer have to be able to move together.
    """
    chunk = CHUNK_BYTES if chunk_bytes is None else chunk_bytes
    return CUT_BYTES * (1 + max(0, stream_bytes - 1) // chunk)


def _gf2_times(mat: list[int], vec: int) -> int:
    out, i = 0, 0
    while vec:
        if vec & 1:
            out ^= mat[i]
        vec >>= 1
        i += 1
    return out


def _gf2_square(square: list[int], mat: list[int]) -> None:
    for n in range(_GF2_BITS):
        square[n] = _gf2_times(mat, mat[n])


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """The CRC of two streams joined, from each one's own CRC and the length of the second.

    zlib has this (crc32_combine) and the Python binding does not expose it, so it is written out:
    it is what lets every chunk's CRC be taken in the thread that already holds the chunk, instead
    of a serial pass over every byte in the one thread that is already the bottleneck. The tests
    hold it against `zlib.crc32` of the joined bytes.
    """
    if len2 <= 0:
        return crc1
    even = [0] * _GF2_BITS
    odd = [0] * _GF2_BITS
    odd[0] = 0xedb88320                                   # the CRC-32 polynomial, reflected
    row = 1
    for n in range(1, _GF2_BITS):
        odd[n] = row
        row <<= 1
    _gf2_square(even, odd)                                # odd^2 = two zero bytes
    _gf2_square(odd, even)                                # four
    while True:
        _gf2_square(even, odd)
        if len2 & 1:
            crc1 = _gf2_times(even, crc1)
        len2 >>= 1
        if len2 == 0:
            break
        _gf2_square(odd, even)
        if len2 & 1:
            crc1 = _gf2_times(odd, crc1)
        len2 >>= 1
        if len2 == 0:
            break
    return (crc1 ^ crc2) & 0xffffffff


def _deflate(level: int, data: bytes, zdict: bytes) -> tuple[bytes, int, int]:
    """One chunk, in a worker thread: raw deflate primed with what came before it, ended on a byte
    boundary so the next chunk's output can simply follow."""
    args = (level, zlib.DEFLATED, -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, zlib.Z_DEFAULT_STRATEGY)
    co = zlib.compressobj(*args, zdict=zdict) if zdict else zlib.compressobj(*args)
    return co.compress(data) + co.flush(zlib.Z_SYNC_FLUSH), zlib.crc32(data), len(data)


class ParallelGzipWriter(io.RawIOBase):
    """A write-only, non-seekable sink that gzips on every core. Hand it to `tarfile.open("w|")`
    and the tar framing is untouched - only the deflate is spread out:

        with path.open("wb") as fh, \\
                ParallelGzipWriter(fh, level=6, worker_count=workers(job_cpus())) as gz, \\
                tarfile.open(fileobj=gz, mode="w|") as tf:
            tf.add(...)

    The nesting is the point: the tar's end-of-archive blocks have to be written before the gzip
    trailer that covers them, and closing in that order is what `with` does. Closing it is what
    finishes the stream, so an exception on the way through leaves a file that is NOT a complete
    archive - which is why the caller writes it to a temporary directory and only sends it after.
    """

    def __init__(self, fileobj: BinaryIO, *, level: int, worker_count: int,
                 chunk_bytes: int | None = None) -> None:
        super().__init__()
        self._fileobj = fileobj
        self._level = level
        # read here rather than defaulted in the signature, which would freeze it at import and
        # leave cut_cost able to be moved while the writer stayed where it was
        self._chunk = CHUNK_BYTES if chunk_bytes is None else chunk_bytes
        #: the threads this is compressing on - stated by the caller, never guessed here
        self.worker_count = max(1, worker_count)
        self._pool = ThreadPoolExecutor(max_workers=self.worker_count, thread_name_prefix="gzip")
        self._depth = self.worker_count * _DEPTH
        self._pending: deque[Future] = deque()
        self._buf = bytearray()
        self._primer = b""
        self._crc = 0
        self._size = 0
        #: how many chunks went out, which is what cut_cost charges for
        self.cuts = 0
        fileobj.write(_GZIP_HEADER)

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._buf += data
        while len(self._buf) >= self._chunk:
            self._submit(bytes(self._buf[:self._chunk]))
            del self._buf[:self._chunk]
        return len(data)

    def _submit(self, data: bytes) -> None:
        # the primer is the tail of the STREAM, not of the previous chunk, so a chunk shorter than
        # the window still leaves the one after it able to reach a full window back
        zdict = self._primer
        self._primer = (self._primer + data)[-WINDOW_BYTES:]
        self._pending.append(self._pool.submit(_deflate, self._level, data, zdict))
        while len(self._pending) > self._depth:
            self._collect()

    def _collect(self) -> None:
        out, crc, size = self._pending.popleft().result()
        self._fileobj.write(out)
        self._crc = crc32_combine(self._crc, crc, size)
        self._size += size
        self.cuts += 1

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._buf:
                self._submit(bytes(self._buf))
                self._buf.clear()
            while self._pending:
                self._collect()
            self._fileobj.write(_FINAL_BLOCK)
            self._fileobj.write(struct.pack("<II", self._crc, self._size & 0xffffffff))
        finally:
            self._pool.shutdown(wait=True)
            super().close()


__all__ = ["CHUNK_BYTES", "CUT_BYTES", "MAX_WORKERS", "WINDOW_BYTES", "ParallelGzipWriter",
           "crc32_combine", "cut_cost", "workers"]
