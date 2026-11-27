# Responsibility: the finished bundle is read through once, and its digest is taken on the way out.
# Boundaries: `engines/bundled_delivery.build_bundle` and the list `verify_archive` hands back.
# Collaborates with: application/artifact_uploader.verify_archive, which does the one read.
"""I wrote `build_bundle` so that it inflated the finished bundle TWICE.

`verify_archive` has to walk every member header to check the paths, and for a gzip member
walking the headers means inflating the whole stream. It already held the member list. My next
line opened the same file and called `getnames()` again - a second pass over 123 GB of contents,
about 300 s on the delivered plate, for a list that had just been read. On top of that the sha256
read all 23.1 GiB back off disk, 22 s at the 1,134 MB/s this box measures, for bytes the writer
had just produced.

Both are gone. These tests pin the three things that could regress: the single pass, the digest's
value, and the fact that `verify_archive` is still what verifies.
"""
from __future__ import annotations

import hashlib
import inspect
import tarfile

import pytest

from meshpipeline.application.artifact_uploader import verify_archive
from meshpipeline.engines import bundled_delivery as BD
from meshpipeline.engines.bundled_delivery import _Hashed

# ------------------------------------------------------------------ the single pass

def test_build_bundle_opens_the_tar_once():
    """Twice is the defect. The write is the only `tarfile.open` left."""
    src = inspect.getsource(BD.build_bundle)
    assert src.count("tarfile.open(") == 1


def test_the_member_list_comes_from_the_call_that_verified_it():
    """Not from a second read, and not from a list built before the file was verified."""
    src = inspect.getsource(BD.build_bundle)
    assert "cells_in_archive(verify_archive(" in src


def test_verify_archive_hands_back_the_names_it_read():
    assert inspect.signature(verify_archive).return_annotation in ("list[str]", list)
    body = inspect.getsource(verify_archive)
    assert body.rstrip().endswith("return names")


def test_the_digest_is_not_read_back_off_disk():
    src = inspect.getsource(BD.build_bundle)
    assert "_DIGEST_BLOCK" not in src
    assert 'open("rb")' not in src and "open('rb')" not in src
    assert "_Hashed(" in src


# ------------------------------------------------------------------ the digest itself

@pytest.mark.parametrize("blocks", [1, 3, 17])
def test_hashing_in_flight_is_the_file_s_own_sha256(tmp_path, blocks):
    """THE LOAD-BEARING PROPERTY. The record's sha256 is what tells a same-attempt retry the same
    content from different content, so a faster build that changed the number would break that
    guard silently."""
    payload = b"".join(bytes([i % 251]) * 4096 for i in range(blocks * 7))
    p = tmp_path / "b.bin"
    sha = hashlib.sha256()
    with p.open("wb") as raw, _Hashed(raw, sha) as fh:
        for i in range(0, len(payload), 1024):
            fh.write(payload[i:i + 1024])
    assert p.read_bytes() == payload
    assert sha.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_the_passthrough_holds_nothing(tmp_path):
    """It is not a tee and not a buffer: the bytes reach the file as they are written, so a
    23.1 GiB bundle is never in memory."""
    p = tmp_path / "b.bin"
    sha = hashlib.sha256()
    with p.open("wb") as raw, _Hashed(raw, sha) as fh:
        fh.write(b"first")
        fh.flush()
        # already on disk, before the context exits
        assert p.read_bytes() == b"first"
        fh.write(b"second")
    assert p.read_bytes() == b"firstsecond"


def test_the_passthrough_does_not_close_the_file(tmp_path):
    """The file has its own `with`, outside this one. Closing it here would close it before
    ParallelGzipWriter had flushed its last block."""
    p = tmp_path / "b.bin"
    raw = p.open("wb")
    try:
        with _Hashed(raw, hashlib.sha256()) as fh:
            fh.write(b"x")
        assert not raw.closed, "the passthrough closed a file it does not own"
        raw.write(b"y")          # still usable, which is the point
    finally:
        raw.close()
    assert p.read_bytes() == b"xy"


def test_write_reports_what_it_wrote(tmp_path):
    """tarfile and the gzip writer both read the return value of write()."""
    p = tmp_path / "b.bin"
    with p.open("wb") as raw, _Hashed(raw, hashlib.sha256()) as fh:
        assert fh.write(b"12345") == 5


# ------------------------------------------------------------------ end to end

def test_a_bundle_written_through_the_passthrough_is_a_readable_tar(tmp_path):
    """The whole point is that nothing about the archive changes."""
    from meshpipeline.parallel_gzip import ParallelGzipWriter

    (tmp_path / "mesh").mkdir()
    (tmp_path / "mesh" / "owner").write_bytes(b"owner\n" * 500)
    out = tmp_path / "b.tar.gz"
    sha = hashlib.sha256()
    with out.open("wb") as raw, _Hashed(raw, sha) as fh, \
            ParallelGzipWriter(fh, level=6, worker_count=2) as gz, \
            tarfile.open(fileobj=gz, mode="w|") as tf:
        tf.add(str(tmp_path / "mesh"), arcname="mesh")
    with tarfile.open(out) as tf:
        assert "mesh/owner" in tf.getnames()
    assert sha.hexdigest() == hashlib.sha256(out.read_bytes()).hexdigest()
