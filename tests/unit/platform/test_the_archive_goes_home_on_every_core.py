# Responsibility: Verify the mesh's returned archive is compressed on every core the job has and is still ONE gzip member - the only shape the reader it comes home through can read.
from __future__ import annotations

import gzip
import io
import os
import tarfile
import zlib
from pathlib import Path

import pytest

import meshpipeline.parallel_gzip as PG
from meshpipeline.adapters.mesh_execution.gcs_exchange import (
    RESULT_ARCHIVE_LEVEL,
    write_result_archive,
)
from meshpipeline.sandbox.safe_extract import safe_extract_tar

#: Small enough that a test workspace is cut many times over. At the production CHUNK_BYTES
#: (16 MiB) every workspace here would be a single chunk and the cutting would go untested.
SMALL_CHUNK = 64 * 1024


@pytest.fixture()
def cut_often(monkeypatch):
    monkeypatch.setattr(PG, "CHUNK_BYTES", SMALL_CHUNK)


def _ws(root: Path) -> Path:
    """A workspace shaped like a mesh's: ASCII polyMesh that compresses hard, a volume export that
    barely does, a nested directory, and an empty file."""
    pm = root / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("\n".join(str(i % 977) for i in range(120_000)))
    (pm / "points").write_text("\n".join(f"({i * 0.001:.6f} 0.1 0.2)" for i in range(40_000)))
    (pm / "boundary").write_text("")
    vtk = root / "VTK" / "case_0"
    vtk.mkdir(parents=True)
    (vtk / "internal.vtu").write_bytes(os.urandom(300_000))
    (root / "mesh_quality.json").write_text('{"cells": 1}')
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _members(archive: Path) -> list[str]:
    # mode "r|gz" is the STREAMING reader, which is the one sandbox/safe_extract uses
    with tarfile.open(str(archive), mode="r|gz") as tf:
        return [m.name for m in tf]


def test_the_archive_comes_home_through_the_reader_that_really_reads_it(tmp_path, cut_often):
    """The mesh is extracted by safe_extract_tar on arrival, so that is what the archive has to
    satisfy - not `gzip`, not `tar`, and not whatever happens to read it in a test."""
    ws = _ws(tmp_path / "ws")
    archive = tmp_path / "output.tar.gz"
    assert write_result_archive(ws, archive) == archive.stat().st_size

    safe_extract_tar(path=archive, dest=tmp_path / "back")
    assert _tree(tmp_path / "back") == _tree(ws)


def test_the_archive_is_one_gzip_member(tmp_path, cut_often):
    """Every chunk after the first is primed with the stream before it and flushed to a byte
    boundary, so the pieces are one deflate stream and not a member each."""
    ws = _ws(tmp_path / "ws")
    archive = tmp_path / "output.tar.gz"
    write_result_archive(ws, archive)

    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    body = d.decompress(archive.read_bytes())
    assert d.unused_data == b"", "a second member starts where the first ends"
    assert d.eof and body
    # gzip.decompress checks the CRC and the length, which is what crc32_combine has to get right
    assert gzip.decompress(archive.read_bytes()) == body


def test_separate_gzip_members_are_why_it_has_to_be_one(tmp_path):
    """WHY THE PRIMING IS WORTH THE TROUBLE. Compressing each chunk as its own gzip MEMBER needs
    no dictionary and no ordering care, and the result is still a valid gzip file that `gzip`
    reads whole - but the reader a mesh actually comes home through stops at the first member's
    end. The easy shape would have built the mesh, archived it fast, and lost it on arrival."""
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w|") as tf:
        for name in ("owner", "points", "faces"):
            ti = tarfile.TarInfo(name)
            ti.size = 100_000
            tf.addfile(ti, io.BytesIO(b"7" * 100_000))
    body = plain.getvalue()
    half = len(body) // 2
    multi = gzip.compress(body[:half]) + gzip.compress(body[half:])

    assert gzip.decompress(multi) == body, "the easy shape is a valid gzip file"
    with pytest.raises(tarfile.ReadError):
        with tarfile.open(fileobj=io.BytesIO(multi), mode="r|gz") as tf:
            [m.name for m in tf]


def test_the_charge_counts_exactly_as_many_cuts_as_the_writer_makes(tmp_path):
    """cut_cost is what engines/return_scope pays for the cutting before the archive exists, so it
    has to count the same cuts the writer will go on to make."""
    body = os.urandom(5 * SMALL_CHUNK + 7)
    buf = io.BytesIO()
    with PG.ParallelGzipWriter(buf, level=RESULT_ARCHIVE_LEVEL, worker_count=4,
                               chunk_bytes=SMALL_CHUNK) as gz:
        gz.write(body)

    assert gz.cuts == 6, "five full chunks and the remainder"
    assert PG.cut_cost(len(body), chunk_bytes=SMALL_CHUNK) == gz.cuts * PG.CUT_BYTES
    assert gzip.decompress(buf.getvalue()) == body


def test_a_stream_that_fits_one_chunk_is_still_charged_for_a_cut():
    """It too ends on a sync flush and an added final block rather than on deflate's own."""
    assert PG.cut_cost(0) == PG.CUT_BYTES
    assert PG.cut_cost(PG.CHUNK_BYTES) == PG.CUT_BYTES
    assert PG.cut_cost(PG.CHUNK_BYTES + 1) == 2 * PG.CUT_BYTES


def test_the_cut_costs_the_archive_almost_nothing(tmp_path, cut_often):
    """The whole bargain: the archive is cut so it can be written on every core, and the cut is
    paid for in bytes. Measured on the coupon's real workspace the 67 cuts GAINED 1,322 bytes (a
    boundary re-fits the Huffman tree, which pays when the data's statistics change) - but it is
    not guaranteed to, and here the chunks are 256 times smaller than production's."""
    ws = _ws(tmp_path / "ws")
    archive = tmp_path / "output.tar.gz"
    parallel = write_result_archive(ws, archive)

    serial = io.BytesIO()
    with tarfile.open(fileobj=serial, mode="w|gz", compresslevel=RESULT_ARCHIVE_LEVEL) as tf:
        for child in sorted(ws.iterdir()):
            tf.add(str(child), arcname=child.name)
    cuts = PG.cut_cost(sum(len(b) for b in _tree(ws).values()), chunk_bytes=SMALL_CHUNK)
    assert parallel <= len(serial.getvalue()) + cuts


def test_the_cores_are_the_job_s_and_never_the_host_s(tmp_path, monkeypatch):
    """os.cpu_count() reports the HOST's cores inside a Cloud Run container, which is the trap
    engines/determinism.job_cpus exists for. parallel_gzip holds no opinion about the machine - it
    is handed a number, so that it can stay a leaf nothing has to import an adapter to reach. What
    there is to check is that the archiver hands it the job's number and not the host's."""
    import meshpipeline.engines.determinism as D
    monkeypatch.setattr(D, "job_cpus", lambda: 3)
    seen = []
    real = PG.ParallelGzipWriter

    class Spy(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen.append(self.worker_count)
    monkeypatch.setattr(PG, "ParallelGzipWriter", Spy)

    write_result_archive(_ws(tmp_path / "ws"), tmp_path / "output.tar.gz")
    assert seen == [3], "the archive was not written on the cores the job has"
    assert PG.workers(10_000) == PG.MAX_WORKERS, "the cap is the memory in flight, not a taste"
    assert PG.workers(0) == 1


def test_crc32_combine_is_the_crc_of_the_joined_bytes():
    """Each chunk's CRC is taken in the thread that already holds it and the pieces are combined,
    so the combination has to be exactly what zlib would have said about the whole stream. An
    archive whose trailer disagrees is refused by every reader AFTER it has been uploaded."""
    for a, b in [(b"", b""), (b"x", b"y"), (os.urandom(1_000), os.urandom(70_000)),
                 (b"ab" * 50_000, b"cd" * 3)]:
        assert PG.crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)) == zlib.crc32(a + b)


def test_two_archives_of_one_workspace_are_the_same_bytes(tmp_path, cut_often):
    """Threads do not make it vary: the cuts fall by byte count, each chunk is compressed from
    fixed inputs, and the gzip frame carries mtime 0 rather than the clock."""
    ws = _ws(tmp_path / "ws")
    first, second = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    write_result_archive(ws, first)
    write_result_archive(ws, second)
    assert first.read_bytes() == second.read_bytes()


def test_the_archive_holds_the_children_and_no_dot_root(tmp_path, cut_often):
    """Same rule as the input side, and the reason it is a rule: a "." root fails safe_extract one
    step later, after the mesh has already been paid for."""
    ws = _ws(tmp_path / "ws")
    archive = tmp_path / "output.tar.gz"
    write_result_archive(ws, archive)

    names = _members(archive)
    assert "." not in names and not any(n.startswith("./") for n in names)
    assert {"constant", "VTK", "mesh_quality.json"} <= set(names)


def test_an_empty_workspace_still_makes_an_archive_that_reads(tmp_path):
    """Nothing to cut at all: the stream is the tar's end-of-archive blocks and the writer still
    has to close a valid member around them."""
    ws = tmp_path / "ws"
    ws.mkdir()
    archive = tmp_path / "output.tar.gz"
    write_result_archive(ws, archive)
    assert gzip.decompress(archive.read_bytes()) == bytes(tarfile.RECORDSIZE)
    assert _members(archive) == []
