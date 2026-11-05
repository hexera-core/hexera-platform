# Responsibility: Verify archive extraction bounds every dimension and refuses every hostile entry shape.
from __future__ import annotations

import io
import tarfile

import pytest

from meshpipeline.sandbox.safe_extract import (
    ExtractionLimits,
    WorkspaceExtractionError,
    default_limits,
    safe_extract_tar,
    validate_limits,
)


def _tar(entries, *, mode="w:gz"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for spec in entries:
            name, payload = spec
            if isinstance(payload, bytes):
                ti = tarfile.TarInfo(name)
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
            else:                                   # payload is a (TarInfo)->None mutator
                ti = tarfile.TarInfo(name)
                payload(ti)
                tf.addfile(ti)
    buf.seek(0)
    return buf


_SMALL = ExtractionLimits(
    max_archive_bytes=10_000_000, max_entries=100, max_total_uncompressed_bytes=10_000_000,
    max_file_bytes=1_000_000, max_depth=10, max_path_len=200, max_ratio=100.0)


# happy path
def test_valid_archive_extracts(tmp_path):
    buf = _tar([("system/meshDict", b"maxCellSize 0.1;\n"), ("constant/x", b"nu 1;\n")])
    written = safe_extract_tar(fileobj=buf, dest=tmp_path / "ws", limits=_SMALL)
    assert (tmp_path / "ws" / "system" / "meshDict").read_bytes() == b"maxCellSize 0.1;\n"
    assert written == len(b"maxCellSize 0.1;\n") + len(b"nu 1;\n")


def test_exact_limits_pass(tmp_path):
    lim = ExtractionLimits(max_archive_bytes=10**9, max_entries=2, max_total_uncompressed_bytes=20,
                           max_file_bytes=10, max_depth=10, max_path_len=200, max_ratio=10**6)
    buf = _tar([("a", b"0123456789"), ("b", b"0123456789")])   # exactly 2 entries, 20 bytes, 10 each
    safe_extract_tar(fileobj=buf, dest=tmp_path / "ws", limits=lim)
    assert (tmp_path / "ws" / "a").read_bytes() == b"0123456789"


# each bound, one over
def _expect_reject(tmp_path, buf, limits, needle):
    dest = tmp_path / "ws"
    with pytest.raises(WorkspaceExtractionError) as ei:
        safe_extract_tar(fileobj=buf, dest=dest, limits=limits)
    assert not dest.exists() or not any(dest.iterdir()), "partial tree not removed"
    assert needle in str(ei.value).lower()


def test_over_total_bytes(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_total_uncompressed_bytes": 15})
    _expect_reject(tmp_path, _tar([("a", b"0" * 20)]), lim, "total")


def test_over_entry_count(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_entries": 1})
    _expect_reject(tmp_path, _tar([("a", b"x"), ("b", b"y")]), lim, "cap")


def test_oversized_individual_file(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_file_bytes": 5})
    _expect_reject(tmp_path, _tar([("a", b"0" * 100)]), lim, "per-file")


def test_high_compression_ratio(tmp_path):
    # make ratio the binding constraint: per-file + total generous, ratio tight
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_ratio": 2.0,
                              "max_total_uncompressed_bytes": 10**9, "max_file_bytes": 10**9})
    buf = _tar([("a", b"0" * 5_000_000)])   # highly compressible → big ratio
    _expect_reject(tmp_path, buf, lim, "ratio")


def test_duplicate_path(tmp_path):
    _expect_reject(tmp_path, _tar([("a", b"x"), ("a", b"y")]), _SMALL, "duplicate")


def test_traversal_path(tmp_path):
    _expect_reject(tmp_path, _tar([("../evil", b"x")]), _SMALL, "traversal")


def test_absolute_path(tmp_path):
    _expect_reject(tmp_path, _tar([("/etc/passwd", b"x")]), _SMALL, "absolute")


def test_symlink_rejected(tmp_path):
    def _sym(ti):
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
    _expect_reject(tmp_path, _tar([("link", _sym)]), _SMALL, "symlink")


def test_hardlink_rejected(tmp_path):
    def _lnk(ti):
        ti.type = tarfile.LNKTYPE
        ti.linkname = "a"
    _expect_reject(tmp_path, _tar([("a", b"x"), ("h", _lnk)]), _SMALL, "hard link")


def test_device_metadata_rejected(tmp_path):
    def _dev(ti):
        ti.type = tarfile.CHRTYPE
        ti.devmajor, ti.devminor = 1, 3
    _expect_reject(tmp_path, _tar([("dev", _dev)]), _SMALL, "device")


def test_setuid_rejected(tmp_path):
    import stat as _stat
    def _suid(ti):
        ti.size = 1
        ti.mode = 0o755 | _stat.S_ISUID
    # a setuid regular file
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("x"); ti.size = 1; ti.mode = 0o755 | _stat.S_ISUID
        tf.addfile(ti, io.BytesIO(b"x"))
    buf.seek(0)
    _expect_reject(tmp_path, buf, _SMALL, "setuid")


def test_deeply_nested_path(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_depth": 3})
    _expect_reject(tmp_path, _tar([("a/b/c/d/e", b"x")]), lim, "depth")


def test_very_long_name(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_path_len": 20})
    _expect_reject(tmp_path, _tar([("a" * 300, b"x")]), lim, "longer")


def test_over_archive_bytes(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_archive_bytes": 10})
    _expect_reject(tmp_path, _tar([("a", b"0" * 100000)]), lim, "over")


def test_corrupt_archive(tmp_path):
    _expect_reject(tmp_path, io.BytesIO(b"not a tar at all, definitely garbage bytes"),
                   _SMALL, "")   # any WorkspaceExtractionError message


def test_truncated_archive(tmp_path):
    full = _tar([("a", b"0" * 1000)]).getvalue()
    _expect_reject(tmp_path, io.BytesIO(full[: len(full) // 2]), _SMALL, "")


def test_many_tiny_files_over_count(tmp_path):
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_entries": 50})
    _expect_reject(tmp_path, _tar([(f"f{i}", b"x") for i in range(200)]), lim, "over")


# extracting over a destination that already holds the caller's files
def _occupied(tmp_path):
    dest = tmp_path / "ws"
    (dest / "system").mkdir(parents=True)
    (dest / "system" / "meshDict").write_bytes(b"caller plan")
    (dest / "records.json").write_bytes(b"caller records")
    return dest


def _staging_left(parent) -> list[str]:
    # the extractor stages beside the destination as .<name>.extract_*; none may outlive the call
    return [p.name for p in parent.iterdir() if ".extract_" in p.name]


def _tree(dest):
    return {p.relative_to(dest).as_posix(): p.read_bytes() if p.is_file() else None
            for p in sorted(dest.rglob("*"))}


@pytest.mark.parametrize(("buf", "over"), [
    pytest.param(lambda: _tar([("a", b"0" * 100000)]), {"max_archive_bytes": 10}, id="archive"),
    pytest.param(lambda: _tar([("a", b"x"), ("b", b"y")]), {"max_entries": 1}, id="entries"),
    pytest.param(lambda: _tar([("system/meshDict", b"0" * 100)]), {"max_file_bytes": 5},
                 id="per-file"),
    pytest.param(lambda: _tar([("a", b"0" * 20)]), {"max_total_uncompressed_bytes": 15},
                 id="total"),
    pytest.param(lambda: _tar([("system/meshDict", b"x"), ("../evil", b"x")]), {},
                 id="traversal"),
    pytest.param(lambda: io.BytesIO(b"not a tar at all, definitely garbage bytes"), {},
                 id="corrupt"),
])
def test_a_refusal_leaves_an_occupied_destination_exactly_as_it_was(buf, over, tmp_path):
    dest = _occupied(tmp_path)
    before = _tree(dest)
    with pytest.raises(WorkspaceExtractionError):
        safe_extract_tar(fileobj=buf(), dest=dest, limits=ExtractionLimits(
            **{**_SMALL.__dict__, **over}))
    assert _tree(dest) == before, "a refused archive changed or removed the caller's files"
    assert not _staging_left(tmp_path), "the staging directory was left"


def test_a_failure_moving_into_place_is_refused_and_staging_removed(tmp_path, monkeypatch):
    import meshpipeline.sandbox.safe_extract as se
    dest = _occupied(tmp_path)

    def _no_space(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(se.shutil, "move", _no_space)
    with pytest.raises(WorkspaceExtractionError, match="moving members into place"):
        safe_extract_tar(fileobj=_tar([("new", b"x")]), dest=dest, limits=_SMALL)
    assert (dest / "records.json").read_bytes() == b"caller records"
    assert not _staging_left(tmp_path)


def test_a_valid_archive_merges_over_an_occupied_destination(tmp_path):
    dest = _occupied(tmp_path)
    written = safe_extract_tar(fileobj=_tar([("system/meshDict", b"remote plan"),
                                             ("constant/polyMesh/points", b"pts")]),
                               dest=dest, limits=_SMALL)
    assert written == len(b"remote plan") + len(b"pts")
    assert _tree(dest) == {
        "constant": None, "constant/polyMesh": None, "constant/polyMesh/points": b"pts",
        "records.json": b"caller records",                  # not in the archive: kept
        "system": None, "system/meshDict": b"remote plan",  # in the archive: replaced
    }
    assert not _staging_left(tmp_path)


@pytest.mark.parametrize("entries", [
    pytest.param([("records.json/inner", b"x")], id="directory-over-file"),
    pytest.param([("system", b"x")], id="file-over-directory"),
])
def test_a_type_conflict_with_the_destination_is_refused_before_anything_moves(entries, tmp_path):
    dest = _occupied(tmp_path)
    before = _tree(dest)
    with pytest.raises(WorkspaceExtractionError, match="would replace"):
        safe_extract_tar(fileobj=_tar([("zz_new", b"n"), *entries]), dest=dest, limits=_SMALL)
    assert _tree(dest) == before
    assert not _staging_left(tmp_path)


def test_a_symlink_already_in_the_destination_is_never_written_through(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = _occupied(tmp_path)
    (dest / "constant").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceExtractionError, match="symlink"):
        safe_extract_tar(fileobj=_tar([("constant/polyMesh/points", b"pts")]), dest=dest,
                         limits=_SMALL)
    assert list(outside.iterdir()) == []
    assert (dest / "constant").is_symlink()


def test_an_empty_destination_is_still_removed_on_refusal(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    with pytest.raises(WorkspaceExtractionError):
        safe_extract_tar(fileobj=_tar([("a", b"x"), ("b", b"y")]), dest=dest,
                         limits=ExtractionLimits(**{**_SMALL.__dict__, "max_entries": 1}))
    assert not dest.exists()


# error hygiene + config validation
def test_error_does_not_leak_archive_contents(tmp_path):
    secret = b"SUPER_SECRET_TOKEN_abc"
    lim = ExtractionLimits(**{**_SMALL.__dict__, "max_file_bytes": 3})
    with pytest.raises(WorkspaceExtractionError) as ei:
        safe_extract_tar(fileobj=_tar([("a", secret)]), dest=tmp_path / "ws", limits=lim)
    assert b"SUPER_SECRET" not in str(ei.value).encode()


@pytest.mark.parametrize("bad", ["max_entries", "max_total_uncompressed_bytes", "max_file_bytes",
                                 "max_archive_bytes", "max_depth", "max_path_len"])
def test_nonpositive_limit_is_rejected(bad):
    with pytest.raises(WorkspaceExtractionError):
        validate_limits(ExtractionLimits(**{**_SMALL.__dict__, bad: 0}))


def test_default_limits_are_sane():
    lim = default_limits()
    validate_limits(lim)
    assert lim.max_total_uncompressed_bytes > lim.max_file_bytes > 0
    assert not lim.allow_symlinks and not lim.allow_hardlinks
