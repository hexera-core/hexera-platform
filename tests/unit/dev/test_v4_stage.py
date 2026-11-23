"""v4_eval stager: layout, idempotence, resume, sha256 rejection, archive handling and the F360
hold. Everything runs against local fixtures with an injected transport; nothing reaches the network."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


pool = _load("v4_pool", _ROOT / "pool.py")
stage = _load("v4_stage", _ROOT / "stage.py")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def tar_xz_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeNet:
    """The injected transport: url -> bytes. Counts every fetch so a test can say how many happened."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.calls: list[str] = []
        self.fail_on: set[str] = set()

    def __call__(self, url: str, dest: Path) -> None:
        self.calls.append(url)
        if url in self.fail_on:
            raise OSError(f"interrupted: {url}")
        dest.write_bytes(self.blobs[url])


def entry(name: str, data: bytes, url: str, archive: bytes | None, source: str = "nist"):
    return pool.PoolEntry(name, source, sha(data), url, sha(archive) if archive is not None else None,
                          "lic", None)


def run(entries, inputs: Path, net: Callable[[str, Path], None]):
    return stage.stage(entries, inputs, download=net, scratch=inputs.parent / "scratch")


# ------------------------------------------------------------------ layout and shapes

def test_direct_file_is_staged_as_sha_slash_basename(tmp_path: Path) -> None:
    data = b"solid-1"
    net = FakeNet({"https://x/a.step": data})
    run([entry("abc/0050_a.step", data, "https://x/a.step", None, "abc_fail")], tmp_path / "in", net)
    assert (tmp_path / "in" / sha(data) / "0050_a.step").read_bytes() == data


def test_zip_members_staged_and_archive_fetched_once(tmp_path: Path) -> None:
    m = {"NIST/CTC/a.stp": b"AAA", "NIST/CTC/b.stp": b"BBB", "NIST/CTC/c.stp": b"CCC", "NIST/x.pdf": b"PDF"}
    z = zip_bytes(m)
    url = "https://nist/document/pmi"
    net = FakeNet({url: z})
    entries = [entry(n, d, url, z) for n, d in m.items() if n.endswith(".stp")]
    run(entries, tmp_path / "in", net)
    assert net.calls == [url]  # three members, ONE download
    for n, d in m.items():
        if n.endswith(".stp"):
            assert (tmp_path / "in" / sha(d) / Path(n).name).read_bytes() == d


def test_tar_xz_members_found_by_content_when_manifest_name_is_not_the_member_path(tmp_path: Path) -> None:
    # abc_fail names are harvest renames (`abc/<chunk>_<basename>`), not member paths.
    data = b"STEPDATA"
    t = tar_xz_bytes({"0050xxxx/0050xxxx_h_step_000.step": data, "0050xxxx/other.step": b"other"})
    url = "https://x/chunk.tar.xz"
    net = FakeNet({url: t})
    run([entry("abc/0050_0050xxxx_h_step_000.step", data, url, t, "abc_fail")], tmp_path / "in", net)
    assert (tmp_path / "in" / sha(data) / "0050_0050xxxx_h_step_000.step").read_bytes() == data


def test_archive_is_not_left_on_disk_after_staging(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    net = FakeNet({"u": z})
    run([entry("a.stp", b"A", "u", z)], tmp_path / "in", net)
    assert not any(p.is_file() for p in (tmp_path / "scratch").rglob("*"))


# ------------------------------------------------------------------ idempotence

def test_second_run_downloads_nothing(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A", "b.stp": b"B"})
    direct = b"D"
    net = FakeNet({"u": z, "d": direct})
    entries = [entry("a.stp", b"A", "u", z), entry("b.stp", b"B", "u", z),
               entry("abc/0050_d.step", direct, "d", None, "abc_fail")]
    first = run(entries, tmp_path / "in", net)
    assert len(net.calls) == 2 and first.staged == 3
    second = run(entries, tmp_path / "in", net)
    assert len(net.calls) == 2, "second run re-downloaded something already staged"
    assert second.staged == 0 and second.already_staged == 3


def test_idempotence_probe_is_sensitive(tmp_path: Path) -> None:
    """The control for the test above: with a staged file REMOVED the same run does download. If
    this ever passes while the previous one is vacuous, the counter is not counting."""
    z = zip_bytes({"a.stp": b"A"})
    net = FakeNet({"u": z})
    entries = [entry("a.stp", b"A", "u", z)]
    run(entries, tmp_path / "in", net)
    (tmp_path / "in" / sha(b"A") / "a.stp").unlink()
    run(entries, tmp_path / "in", net)
    assert len(net.calls) == 2


def test_only_archives_with_missing_members_are_refetched(tmp_path: Path) -> None:
    z1, z2 = zip_bytes({"a.stp": b"A"}), zip_bytes({"b.stp": b"B"})
    net = FakeNet({"u1": z1, "u2": z2})
    entries = [entry("a.stp", b"A", "u1", z1), entry("b.stp", b"B", "u2", z2)]
    run(entries, tmp_path / "in", net)
    (tmp_path / "in" / sha(b"B") / "b.stp").unlink()
    net.calls.clear()
    run(entries, tmp_path / "in", net)
    assert net.calls == ["u2"]


# ------------------------------------------------------------------ resume

def test_interrupted_run_resumes_without_refetching_finished_archives(tmp_path: Path) -> None:
    z1, z2 = zip_bytes({"a.stp": b"A"}), zip_bytes({"b.stp": b"B"})
    net = FakeNet({"u1": z1, "u2": z2})
    entries = [entry("a.stp", b"A", "u1", z1), entry("b.stp", b"B", "u2", z2)]
    net.fail_on = {"u2"}
    with pytest.raises(OSError, match="interrupted"):
        run(entries, tmp_path / "in", net)
    assert (tmp_path / "in" / sha(b"A") / "a.stp").exists()
    assert not (tmp_path / "in" / sha(b"B")).exists()
    net.fail_on = set()
    net.calls.clear()
    run(entries, tmp_path / "in", net)
    assert net.calls == ["u2"]
    assert (tmp_path / "in" / sha(b"B") / "b.stp").read_bytes() == b"B"


def test_crash_mid_member_never_leaves_a_truncated_final_file(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"AAAA", "b.stp": b"BBBB"})
    net = FakeNet({"u": z})
    entries = [entry("a.stp", b"AAAA", "u", z), entry("b.stp", b"BBBB", "u", z)]
    real_walk = stage.default_walk

    def crashing(archive: Path, work: Path):
        for i, (name, opener) in enumerate(real_walk(archive, work)):
            if i == 1:
                raise RuntimeError("vm reclaimed")
            yield name, opener

    with pytest.raises(RuntimeError, match="reclaimed"):
        stage.stage(entries, tmp_path / "in", download=net, walk=crashing, scratch=tmp_path / "scratch")
    for p in (tmp_path / "in").rglob("*"):
        if p.is_file():
            assert sha(p.read_bytes()) == p.parent.name, f"{p} is not a whole, verified file"
    net.calls.clear()
    run(entries, tmp_path / "in", net)
    assert (tmp_path / "in" / sha(b"BBBB") / "b.stp").read_bytes() == b"BBBB"


def _key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


class AppendingNet(FakeNet):
    """Behaves like resumable_download: it CONTINUES whatever is already in dest."""

    def __call__(self, url: str, dest: Path) -> None:
        self.calls.append(url)
        have = dest.stat().st_size if dest.exists() else 0
        with dest.open("ab") as out:
            out.write(self.blobs[url][have:])


def test_stale_partial_of_a_different_upstream_version_is_refetched_fresh_once(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A" * 100})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / f"{_key('u')}.part").write_bytes(b"hybrid-from-an-older-upstream")  # the REAL key
    net = AppendingNet({"u": z})
    run([entry("a.stp", b"A" * 100, "u", z)], tmp_path / "in", net)
    assert net.calls == ["u", "u"]  # resumed attempt mismatched, one fresh refetch succeeded
    assert (tmp_path / "in" / sha(b"A" * 100) / "a.stp").exists()


def test_a_genuinely_wrong_archive_still_fails_after_the_single_retry(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / f"{_key('u')}.part").write_bytes(b"x")
    net = AppendingNet({"u": zip_bytes({"a.stp": b"B"})})
    with pytest.raises(stage.StageError, match="archive"):
        run([entry("a.stp", b"A", "u", z)], tmp_path / "in", net)
    assert net.calls == ["u", "u"]  # not an unbounded retry loop


def test_stale_member_partials_are_swept(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    d = tmp_path / "in" / sha(b"A")
    d.mkdir(parents=True)
    (d / "a.stp.partial").write_bytes(b"half")
    run([entry("a.stp", b"A", "u", z)], tmp_path / "in", FakeNet({"u": z}))
    assert not list((tmp_path / "in").rglob("*.partial"))


def test_crash_inside_the_copy_leaves_no_truncated_final_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    z = zip_bytes({"a.stp": b"AAAA", "b.stp": b"BBBB"})
    net = FakeNet({"u": z})
    entries = [entry("a.stp", b"AAAA", "u", z), entry("b.stp", b"BBBB", "u", z)]
    real = stage.shutil.copyfile
    n = {"i": 0}

    def dying(src, dst, *a, **k):
        n["i"] += 1
        if n["i"] == 2:  # the second PLACE: write half, then the VM goes away
            Path(dst).write_bytes(Path(src).read_bytes()[:2])
            raise RuntimeError("vm reclaimed")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(stage.shutil, "copyfile", dying)
    with pytest.raises(RuntimeError, match="reclaimed"):
        run(entries, tmp_path / "in", net)
    for p in (tmp_path / "in").rglob("*"):
        if p.is_file() and p.suffix != ".partial":
            assert sha(p.read_bytes()) == p.parent.name
    monkeypatch.setattr(stage.shutil, "copyfile", real)
    run(entries, tmp_path / "in", net)
    assert (tmp_path / "in" / sha(b"BBBB") / "b.stp").read_bytes() == b"BBBB"
    assert not list((tmp_path / "in").rglob("*.partial"))


def test_all_problems_are_reported_together_and_the_rest_is_still_staged(tmp_path: Path) -> None:
    z1, z2, z3 = zip_bytes({"a.stp": b"A"}), zip_bytes({"b.stp": b"B"}), zip_bytes({"c.stp": b"C"})
    net = FakeNet({"u1": zip_bytes({"a.stp": b"X"}), "u2": z2, "u3": z3})
    entries = [entry("a.stp", b"A", "u1", z1), entry("b.stp", b"B", "u2", z2),
               entry("c.stp", b"C", "u3", z3),
               pool.PoolEntry("m.stp", "nist", sha(b"M"), "u2", sha(z2), "lic", None)]  # not in u2
    with pytest.raises(stage.StageError) as err:
        run(entries, tmp_path / "in", net)
    msg = str(err.value)
    assert "2 problem" in msg and "u1" in msg and "u2" in msg
    assert (tmp_path / "in" / sha(b"C") / "c.stp").exists()  # u3 was still staged after u1/u2 failed


def test_fsync_runs_before_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(stage.os, "fsync", lambda fd: order.append("fsync"))
    real_replace = stage.os.replace

    def replace(a, b):
        order.append("replace")
        real_replace(a, b)

    monkeypatch.setattr(stage.os, "replace", replace)
    z = zip_bytes({"a.stp": b"A"})
    run([entry("a.stp", b"A", "u", z)], tmp_path / "in", FakeNet({"u": z}))
    assert order.index("fsync") < order.index("replace")


# ------------------------------------------------------------------ sha256 verification REJECTS

def test_corrupted_direct_download_is_a_hard_failure_and_stages_nothing(tmp_path: Path) -> None:
    good = b"good bytes"
    net = FakeNet({"u": b"good byteS"})  # one byte off
    with pytest.raises(stage.StageError, match="sha256"):
        run([entry("abc/0050_a.step", good, "u", None, "abc_fail")], tmp_path / "in", net)
    assert not list((tmp_path / "in").rglob("a.step"))
    assert not (tmp_path / "in" / sha(good)).exists()


def test_corrupted_archive_is_rejected_against_the_archive_hash(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    bad = zip_bytes({"a.stp": b"A", "evil.stp": b"x"})
    net = FakeNet({"u": bad})
    with pytest.raises(stage.StageError, match="archive"):
        run([entry("a.stp", b"A", "u", z)], tmp_path / "in", net)
    assert not (tmp_path / "in" / sha(b"A")).exists()


def test_member_whose_bytes_differ_from_the_manifest_is_never_staged(tmp_path: Path) -> None:
    # The archive's own hash is right (the manifest was built from it), but the member the manifest
    # names has other bytes than the manifest's sha: no member matches, so it is a hard failure.
    z = zip_bytes({"a.stp": b"tampered"})
    e = pool.PoolEntry("a.stp", "nist", sha(b"original"), "u", sha(z), "lic", None)
    with pytest.raises(stage.StageError, match="not found"):
        run([e], tmp_path / "in", FakeNet({"u": z}))
    assert not (tmp_path / "in" / sha(b"original")).exists()
    assert not (tmp_path / "in" / sha(b"tampered")).exists()


def test_corrupted_already_staged_file_is_quarantined_and_restaged_from_a_verified_source(
        tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    net = FakeNet({"u": z})
    entries = [entry("a.stp", b"A", "u", z)]
    run(entries, tmp_path / "in", net)
    target = tmp_path / "in" / sha(b"A") / "a.stp"
    target.write_bytes(b"A-but-bitrotted")
    report = run(entries, tmp_path / "in", net)
    assert report.quarantined == 1 and net.calls == ["u", "u"]
    assert target.read_bytes() == b"A"  # replaced by bytes verified against the manifest
    assert (target.parent / "a.stp.corrupt").read_bytes() == b"A-but-bitrotted"  # kept as evidence


def test_a_restage_that_is_itself_corrupt_is_still_a_hard_failure(tmp_path: Path) -> None:
    z = zip_bytes({"a.stp": b"A"})
    entries = [entry("a.stp", b"A", "u", z)]
    run(entries, tmp_path / "in", FakeNet({"u": z}))
    (tmp_path / "in" / sha(b"A") / "a.stp").write_bytes(b"rot")
    with pytest.raises(stage.StageError, match="archive"):
        run(entries, tmp_path / "in", FakeNet({"u": zip_bytes({"a.stp": b"other"})}))
    assert not (tmp_path / "in" / sha(b"A") / "a.stp").exists()  # the bad bytes were NOT put back


def test_good_file_passes_the_same_verification(tmp_path: Path) -> None:
    # the accept side of the rejection tests above, so they cannot be passing for an unrelated reason
    data = b"good bytes"
    run([entry("abc/0050_a.step", data, "u", None, "abc_fail")], tmp_path / "in", FakeNet({"u": data}))
    assert (tmp_path / "in" / sha(data) / "0050_a.step").read_bytes() == data


# ------------------------------------------------------------------ the Fusion 360 hold

def test_f360_entry_is_refused_before_anything_is_fetched(tmp_path: Path) -> None:
    z = zip_bytes({"s/a.stp": b"F"})
    net = FakeNet({"u": z})
    f = entry("s/a.stp", b"F", "u", z, "f360")
    with pytest.raises(stage.HeldSourceError, match="f360"):
        run([f], tmp_path / "in", net)
    assert net.calls == []
    assert not (tmp_path / "in").exists() or not any((tmp_path / "in").iterdir())


def test_one_f360_entry_among_allowed_ones_refuses_the_whole_run(tmp_path: Path) -> None:
    ok = b"ok"
    net = FakeNet({"u": ok, "f": b"F"})
    entries = [entry("abc/0050_ok.step", ok, "u", None, "abc_fail"),
               entry("s/a.stp", b"F", "f", None, "f360")]
    with pytest.raises(stage.HeldSourceError):
        run(entries, tmp_path / "in", net)
    assert net.calls == []


def test_unknown_source_is_refused_too(tmp_path: Path) -> None:
    net = FakeNet({"u": b"x"})
    with pytest.raises(stage.HeldSourceError, match="allow-list"):
        run([entry("a", b"x", "u", None, "mystery")], tmp_path / "in", net)
    assert net.calls == []


def test_f360_url_is_refused_at_the_download_choke_point_whatever_the_label(tmp_path: Path) -> None:
    url = "https://fusion-360-gallery-dataset.s3.us-west-2.amazonaws.com/segmentation/s2.0.1/x.zip"
    z = zip_bytes({"a.stp": b"A"})
    net = FakeNet({url: z})
    with pytest.raises(stage.HeldSourceError, match="Fusion"):
        run([entry("a.stp", b"A", url, z, "nist")], tmp_path / "in", net)
    assert net.calls == []


def test_allow_list_is_data_in_the_module_not_a_parameter() -> None:
    assert "f360" not in stage.ALLOWED_SOURCES
    assert {"abc_fail", "abc_pass", "occt", "nist", "inject"} == set(stage.ALLOWED_SOURCES)
    import inspect
    assert "allowed" not in inspect.signature(stage.stage).parameters


def test_cli_stages_allowed_entries_from_the_manifest_and_reports_the_held_ones(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ok = b"ok"
    z = zip_bytes({"s/a.stp": b"F"})
    entries = [entry("abc/0050_ok.step", ok, "u", None, "abc_fail"),
               entry("s/a.stp", b"F", "https://fusion/zip", z, "f360")]
    manifest = tmp_path / "pool.json"
    pool.write_pool(entries, manifest, sources_included=["abc", "f360"])
    net = FakeNet({"u": ok})
    rc = stage.main(["--pool", str(manifest), "--inputs", str(tmp_path / "in")], download=net)
    out = capsys.readouterr()
    assert rc == 0
    assert (tmp_path / "in" / sha(ok) / "0050_ok.step").exists()
    assert net.calls == ["u"]
    assert "f360" in out.out + out.err and "held" in (out.out + out.err).lower()


def test_cli_exits_nonzero_on_a_hash_mismatch(tmp_path: Path) -> None:
    e = entry("abc/0050_a.step", b"good", "u", None, "abc_fail")
    manifest = tmp_path / "pool.json"
    pool.write_pool([e], manifest, sources_included=["abc"])
    rc = stage.main(["--pool", str(manifest), "--inputs", str(tmp_path / "in")],
                    download=FakeNet({"u": b"bad!"}))
    assert rc == 2


# ------------------------------------------------------------------ the default transport (no network)

class _Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int) -> None:
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resumable_download_continues_from_the_partial_file(tmp_path: Path) -> None:
    full = b"0123456789"
    dest = tmp_path / "x.part"
    dest.write_bytes(full[:4])
    seen: list[str | None] = []

    def opener(req, timeout=0):
        seen.append(req.get_header("Range"))
        return _Resp(full[4:], 206)

    stage.resumable_download("https://x", dest, opener=opener)
    assert seen == ["bytes=4-"] and dest.read_bytes() == full


def test_resumable_download_restarts_when_the_server_ignores_range(tmp_path: Path) -> None:
    full = b"0123456789"
    dest = tmp_path / "x.part"
    dest.write_bytes(b"junk")
    stage.resumable_download("https://x", dest, opener=lambda req, timeout=0: _Resp(full, 200))
    assert dest.read_bytes() == full


def test_7z_archives_go_through_bsdtar_and_are_staged_by_content(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No 7z writer in the unit tier: a file with the 7z magic stands in for the archive and bsdtar is
    # faked to "extract" known members. This pins the dispatch, the argv and the cleanup, not bsdtar.
    archive = b"7z\xbc\xaf\x27\x1c" + b"fake-chunk"
    data = b"STEP-IN-7Z"
    calls: list[list[str]] = []

    def fake_bsdtar(argv: list[str], check: bool) -> None:
        calls.append(argv)
        into = Path(argv[argv.index("-C") + 1])
        (into / "00500350").mkdir(parents=True)
        (into / "00500350" / "x_step_000.step").write_bytes(data)
        (into / "00500350" / "other.step").write_bytes(b"other")

    monkeypatch.setattr(stage.subprocess, "run", fake_bsdtar)
    monkeypatch.setattr(stage.shutil, "which", lambda n: "/bin/bsdtar")
    net = FakeNet({"u": archive})
    run([entry("abc/0050_x_step_000.step", data, "u", archive, "abc_fail")], tmp_path / "in", net)
    assert calls and calls[0][0] == "bsdtar" and calls[0][1] == "-xf"
    assert (tmp_path / "in" / sha(data) / "0050_x_step_000.step").read_bytes() == data
    assert not list((tmp_path / "scratch").rglob("*.step"))  # extraction removed


def test_a_file_that_is_no_known_archive_is_a_hard_failure(tmp_path: Path) -> None:
    junk = b"this is not an archive"
    with pytest.raises(stage.StageError, match="not a zip"):
        run([entry("a.stp", b"A", "u", junk)], tmp_path / "in", FakeNet({"u": junk}))


# ------------------------------------------------------------------ preflight, loud empties, real 7z

def test_preflight_names_a_missing_bsdtar_before_any_download(tmp_path: Path) -> None:
    net = FakeNet({"https://x/c.7z": b"7z"})
    e = entry("abc/0050_a.step", b"A", "https://x/c.7z", b"7z", "abc_fail")
    with pytest.raises(stage.StageError, match="apt-get install libarchive-tools"):
        stage.stage([e], tmp_path / "in", download=net, scratch=tmp_path / "s", which=lambda n: None,
                    min_free_bytes=0)
    assert net.calls == []


def test_preflight_refuses_when_scratch_is_too_small_before_any_download(tmp_path: Path) -> None:
    probs = stage.preflight(["https://x/c.7z"], tmp_path, which=lambda n: "/bin/bsdtar",
                            min_free_bytes=1 << 62)
    assert len(probs) == 1 and "free" in probs[0]
    assert stage.preflight(["https://x/c.zip"], tmp_path, which=lambda n: None) == []  # zip needs neither


def test_bsdtar_failure_is_a_stage_error_not_a_traceback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    archive = b"7z\xbc\xaf\x27\x1c" + b"x"

    def boom(argv, check):
        raise subprocess.CalledProcessError(2, argv)

    monkeypatch.setattr(stage.subprocess, "run", boom)
    monkeypatch.setattr(stage.shutil, "which", lambda n: "/bin/bsdtar")
    with pytest.raises(stage.StageError, match="bsdtar failed"):
        run([entry("a.step", b"A", "u", archive)], tmp_path / "in", FakeNet({"u": archive}))


def test_check_mode_downloads_nothing(tmp_path: Path) -> None:
    e = entry("abc/0050_a.step", b"A", "u", None, "abc_fail")
    manifest = tmp_path / "pool.json"
    pool.write_pool([e], manifest, sources_included=["abc"])
    net = FakeNet({"u": b"A"})
    assert stage.main(["--pool", str(manifest), "--inputs", str(tmp_path / "in"), "--check"],
                      download=net) == 0
    assert net.calls == []


def test_cli_makes_an_empty_allowed_source_loud(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    e = entry("abc/0050_a.step", b"A", "u", None, "abc_fail")
    manifest = tmp_path / "pool.json"
    pool.write_pool([e], manifest, sources_included=["abc"])
    args = ["--pool", str(manifest), "--inputs", str(tmp_path / "in")]
    assert stage.main(args, download=FakeNet({"u": b"A"})) == 0
    err = capsys.readouterr()
    assert "inject 0" in err.out and "ZERO manifest entries" in err.err and "inject" in err.err
    assert stage.main([*args, "--strict-sources"], download=FakeNet({"u": b"A"})) == 3


def test_entry_with_no_url_and_no_archive_hash_is_a_clear_error_not_a_crash(tmp_path: Path) -> None:
    e = pool.PoolEntry("inj.step", "inject", sha(b"I"), "", None, "lic", None)
    net = FakeNet({})
    with pytest.raises(stage.StageError, match="no URL"):
        run([e], tmp_path / "in", net)
    assert net.calls == []


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed in this environment")
def test_real_7z_archive_is_staged_through_real_bsdtar(tmp_path: Path) -> None:
    src = tmp_path / "src" / "00500350"
    src.mkdir(parents=True)
    data = b"ISO-10303-21; real 7z member"
    (src / "x_step_000.step").write_bytes(data)
    (src / "decoy.step").write_bytes(b"decoy")
    archive = tmp_path / "chunk.7z"
    subprocess.run(["bsdtar", "--format", "7zip", "-cf", str(archive), "-C", str(tmp_path / "src"),
                    "00500350"], check=True)
    blob = archive.read_bytes()
    assert blob[:6] == b"7z\xbc\xaf\x27\x1c"
    net = FakeNet({"https://x/abc_0050.7z": blob})
    e = entry("abc/0050_x_step_000.step", data, "https://x/abc_0050.7z", blob, "abc_fail")
    stage.stage([e], tmp_path / "in", download=net, scratch=tmp_path / "scratch", min_free_bytes=0)
    assert (tmp_path / "in" / sha(data) / "0050_x_step_000.step").read_bytes() == data
    assert not list((tmp_path / "scratch").rglob("*.step"))


def test_staged_tree_is_byte_identical_whichever_extractor_or_member_order_produced_it(
        tmp_path: Path) -> None:
    """Members are identified by content hash and written as <sha>/<basename>, so the extractor
    (stdlib zip, tar, bsdtar on any host) and the archive's member order cannot change the output."""
    m = {"d1/a.stp": b"AAA", "d2/b.stp": b"BBB", "d3/c.stp": b"CCC"}
    zipped, tarred = zip_bytes(m), tar_xz_bytes(dict(reversed(list(m.items()))))
    trees = []
    for i, blob in enumerate((zipped, tarred)):
        entries = [entry(n, d, "u", blob) for n, d in m.items()]
        root = tmp_path / f"in{i}"
        run(entries, root, FakeNet({"u": blob}))
        trees.append({str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
    assert trees[0] == trees[1] and len(trees[0]) == 3
