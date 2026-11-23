#!/usr/bin/env python3
# Responsibility: Stage the CRS v4 benchmark's inputs from the pool manifest as
#                 <inputs>/<sha256>/<basename>, the layout run_arm reads.
# Owns: the source allow-list (the Fusion 360 licence hold), the idempotent / resumable fetch, and
#       the sha256 verification of every staged file.
# Boundaries: it reads the manifest through pool.read_pool and fetches through an injected Download;
#             only `resumable_download` touches the network. It writes a LOCAL directory - uploading
#             that to the transfer bucket, the fleet launch and the preflight are other tasks.
"""Stage the v4 inputs.

    stage.py --inputs /data/v4-inputs                  # pool.json -> <inputs>/<sha256>/<basename>
    stage.py --inputs DIR --pool P --scratch /big/disk

Layout: `<inputs>/<sha256>/<basename>`, the sha256 being the manifest's and the basename
`Path(entry.name).name` (what v3_eval/stage.py and run_arm.clean_shard_dir use).

Every manifest entry is a member of an archive (ABC 7z chunks, the OCCT tar.xz, the NIST zips), or,
when the manifest carries no archive hash, a single file at its URL. Each archive is downloaded ONCE,
hashed against `archive_sha256`, and walked once; a member is identified by the sha256 of its BYTES,
not by its path, because the pool's ABC failure names are harvest renames (`abc/<chunk>_<basename>`),
not member paths. A wanted hash that no member matches is a hard failure.

Idempotent: a staged file whose bytes hash to the manifest is left alone, and an archive none of whose
members is missing is never fetched. Resumable: every file lands via a `.partial` sibling and an atomic
rename, so a killed run never leaves a truncated file at a final path; archives are fetched into
`<scratch>/*.part` (ranged continuation) and finished archives are skipped on the next run because
their members are already staged. A hash mismatch on a download (archive or direct file) is a
StageError, never a warning; every archive is still tried and ALL problems are reported at the end.
A file already on disk that no longer hashes to the manifest (bit rot, a reclaim) is renamed
`*.corrupt` and re-staged from a verified source: what lands is always checked against the manifest,
so a bad file can neither wedge the run nor be trusted. `--check` runs the preflight (bsdtar, scratch
space, per-source counts) with no download.

The Fusion 360 Gallery licence is not cleared (non-commercial research, no redistribution). The
allow-list `ALLOWED_SOURCES` is enforced by `stage()` itself: it takes no parameter to widen it, an
entry of any other source refuses the whole run before a byte is fetched, and the download choke
point refuses F360 URLs whatever an entry claims to be. Adding F360 later is a reviewed edit to
`ALLOWED_SOURCES` and `HELD_SOURCES` - a licence decision, not a flag.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


pool: Any = _load("v4_pool", HERE / "pool.py")
PoolEntry = pool.PoolEntry
Download = Callable[[str, Path], None]
#: Yields (member name, opener) for every file in an archive; the opener returns a fresh binary stream.
Walk = Callable[[Path, Path], Iterator[tuple[str, Callable[[], BinaryIO]]]]

#: The sources this stager will fetch. The Fusion 360 Gallery licence is not cleared, so `f360` is
#: absent on purpose. This is data in the module, not an argument: nothing a caller passes widens it.
ALLOWED_SOURCES: frozenset[str] = frozenset({"abc_fail", "abc_pass", "occt", "nist", "inject"})
#: Sources known to exist and deliberately not staged, with the reason (the CLI reports them).
HELD_SOURCES: dict[str, str] = {
    "f360": "Fusion 360 Gallery licence not cleared (non-commercial research use, redistribution barred)",
}
#: URL fragments that are Fusion 360 Gallery data whatever the entry's `source` says.
_HELD_URL_MARKERS = ("fusion-360-gallery-dataset", "fusion360gallery", "s2.0.1_extended_step")

_CHUNK = 1 << 20


class StageError(RuntimeError):
    """A sha256 mismatch or a missing member: the staged inputs cannot be trusted."""


class HeldSourceError(StageError):
    """An entry or URL outside the allow-list (Fusion 360 and anything unknown)."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- the licence hold

def refuse_unallowed(entries: list[Any]) -> None:
    """Raise HeldSourceError for the first entry whose source is not on the allow-list."""
    for e in entries:
        if e.source not in ALLOWED_SOURCES:
            why = HELD_SOURCES.get(e.source, "not on the stager's allow-list")
            raise HeldSourceError(f"{e.name}: source {e.source!r} is not on the allow-list ({why}); "
                                  f"refusing the whole run, nothing was fetched")


def partition_entries(entries: list[Any]) -> tuple[list[Any], dict[str, list[Any]]]:
    """Split a manifest into (stageable, held-by-source). A source that is neither allowed nor a
    KNOWN hold is an error, not something to skip."""
    allowed: list[Any] = []
    held: dict[str, list[Any]] = {}
    for e in entries:
        if e.source in ALLOWED_SOURCES:
            allowed.append(e)
        elif e.source in HELD_SOURCES:
            held.setdefault(e.source, []).append(e)
        else:
            refuse_unallowed([e])
    return allowed, held


def _guard_url(url: str) -> None:
    low = url.lower()
    for marker in _HELD_URL_MARKERS:
        if marker in low:
            raise HeldSourceError(f"{url}: Fusion 360 Gallery data is held (licence not cleared); "
                                  f"refusing to download it")


def _guarded(download: Download) -> Download:
    def fetch(url: str, dest: Path) -> None:
        _guard_url(url)
        download(url, dest)
    return fetch


# --------------------------------------------------------------------------- transport

def resumable_download(url: str, dest: Path, *, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
    """Download `url` into `dest`, continuing a partial `dest` with a Range request. A server that
    answers 200 (ignoring Range) restarts the file; 416 means `dest` already holds the whole thing."""
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"User-Agent": "v4-eval-stager/1"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with opener(request, timeout=120) as response:
            resumed = have > 0 and getattr(response, "status", 200) == 206
            with dest.open("ab" if resumed else "wb") as out:
                shutil.copyfileobj(response, out, _CHUNK)
    except urllib.error.HTTPError as exc:
        if exc.code != 416:
            raise


# --------------------------------------------------------------------------- archives

_SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"


def default_walk(archive: Path, work: Path) -> Iterator[tuple[str, Callable[[], BinaryIO]]]:
    """Members of a zip, tar(.xz/.gz) or 7z archive, detected by content (the NIST URLs have no
    suffix). zip and tar are streamed; 7z goes through `bsdtar` into `work` (the ABC harvest did
    the same) and `work` is removed afterwards."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not info.is_dir():
                    yield info.filename, (lambda info=info: z.open(info))  # type: ignore[misc]
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as t:
            for member in t:
                if member.isfile():
                    yield member.name, (lambda member=member: t.extractfile(member))  # type: ignore[misc]
        return
    with archive.open("rb") as handle:
        magic = handle.read(len(_SEVENZ_MAGIC))
    if magic != _SEVENZ_MAGIC:
        raise StageError(f"{archive.name}: not a zip, tar or 7z archive")
    if shutil.which("bsdtar") is None:
        raise StageError("bsdtar is not on PATH; cannot extract a 7z archive (Linux: "
                         "`apt-get install libarchive-tools`, bsdtar >= 3.6)")
    probe = work.parent
    if shutil.disk_usage(probe).free < 2 * archive.stat().st_size:
        raise StageError(f"scratch {probe} has less than 2x the archive's size free; "
                         f"not extracting {archive.name}")
    into = work / "extract"
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    try:
        try:
            subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(into)], check=True)
        except subprocess.CalledProcessError as exc:
            raise StageError(f"bsdtar failed on {archive.name}: exit {exc.returncode}") from exc
        for path in sorted(p for p in into.rglob("*") if p.is_file()):
            yield str(path.relative_to(into)), (lambda path=path: path.open("rb"))  # type: ignore[misc]
    finally:
        shutil.rmtree(into, ignore_errors=True)


# --------------------------------------------------------------------------- staging

#: Free scratch required before a run that has a 7z archive to fetch (archive + full extraction + the
#: member tee, with the largest ABC chunk in mind). Overridable: `--min-free-gb`.
DEFAULT_MIN_FREE_BYTES = 8 << 30


@dataclass
class StageReport:
    staged: int = 0
    already_staged: int = 0
    quarantined: int = 0
    archives_fetched: int = 0
    files_fetched: int = 0


def _final(inputs: Path, e: Any) -> Path:
    return inputs / e.sha256 / Path(e.name).name


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _place(src: Path, final: Path, want_sha: str) -> None:
    """Copy `src` next to `final`, fsync and verify the COPY (not the bytes that were read), then
    rename it into place atomically. A reclaim at any point leaves either nothing or a whole file."""
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".partial")
    shutil.copyfile(src, partial)
    _fsync(partial)
    got = sha256_file(partial)
    if got != want_sha:
        partial.unlink()
        raise StageError(f"{final.name}: sha256 {got} != manifest {want_sha}")
    os.replace(partial, final)
    try:
        _fsync(final.parent)
    except OSError:  # directory fsync is best effort (not every filesystem allows it)
        pass


def _clean_scratch(scratch: Path, inputs: Path) -> None:
    for stale in list(scratch.glob("*.partial")) + list(inputs.glob("*/*.partial")):
        stale.unlink()


def preflight(archive_urls: list[str], scratch: Path, *, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
              which: Callable[[str], str | None] = shutil.which,
              free: Callable[[Path], int] | None = None) -> list[str]:
    """Everything that can be known before the first download. Returns the problems (empty = fine)."""
    problems: list[str] = []
    sevenz = [u for u in archive_urls if u.lower().split("?")[0].endswith(".7z")]
    if sevenz:
        if which("bsdtar") is None:
            problems.append(f"bsdtar is not on PATH but {len(sevenz)} pending archive(s) are 7z "
                            f"(Linux: `apt-get install libarchive-tools`, bsdtar >= 3.6; macOS: /usr/bin/bsdtar "
                            f"ships with the OS, or `brew install libarchive`); without it the run "
                            f"would fail only after a multi-GB download")
        probe = scratch
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        have = free(probe) if free is not None else shutil.disk_usage(probe).free
        if have < min_free_bytes:
            problems.append(f"scratch {scratch} has {have >> 20} MiB free, need {min_free_bytes >> 20} "
                            f"MiB for a 7z archive plus its extraction (--scratch, --min-free-gb)")
    return problems


def _stage_direct(e: Any, inputs: Path, scratch: Path, download: Download, key: str) -> None:
    part = scratch / f"{key}.part"
    part.unlink(missing_ok=True)  # a direct file is verified whole or not at all
    download(e.url, part)
    got = sha256_file(part)
    if got != e.sha256:
        part.unlink()
        raise StageError(f"{e.name}: sha256 {got} != manifest {e.sha256} (from {e.url}); not staged")
    _place(part, _final(inputs, e), e.sha256)
    part.unlink()


def _fetch_verified_archive(url: str, archive_sha: str, part: Path, download: Download) -> None:
    """Download into `part` (continuing a partial one) and check the archive hash. A mismatch after a
    CONTINUED download may just be a hybrid of two upstream versions (Range has no If-Range), so the
    partial is discarded and fetched fresh exactly once before the mismatch is declared."""
    resumed = part.exists() and part.stat().st_size > 0
    for attempt in (0, 1):
        download(url, part)
        got = sha256_file(part)
        if got == archive_sha:
            return
        part.unlink()
        if not (resumed and attempt == 0):
            raise StageError(f"archive {url}: sha256 {got} != manifest archive_sha256 {archive_sha}; "
                             f"nothing staged from it")
        resumed = False


def _stage_archive(url: str, archive_sha: str, wanted: list[Any], inputs: Path, scratch: Path,
                   download: Download, walk: Walk, key: str) -> None:
    part = scratch / f"{key}.part"
    _fetch_verified_archive(url, archive_sha, part, download)
    by_sha: dict[str, list[Any]] = {}
    for e in wanted:
        by_sha.setdefault(e.sha256, []).append(e)
    found: set[str] = set()
    tee = scratch / f"{key}.member.partial"
    try:
        for _name, opener in walk(part, scratch / f"{key}.x"):
            digest = hashlib.sha256()
            with opener() as src, tee.open("wb") as out:
                for chunk in iter(lambda src=src: src.read(_CHUNK), b""):  # type: ignore[misc]
                    digest.update(chunk)
                    out.write(chunk)
            hit = digest.hexdigest()
            if hit in by_sha and hit not in found:
                for e in by_sha[hit]:
                    _place(tee, _final(inputs, e), e.sha256)
                found.add(hit)
    finally:
        tee.unlink(missing_ok=True)
    missing = [e for e in wanted if e.sha256 not in found]
    part.unlink()
    if missing:
        names = ", ".join(e.name for e in missing[:5])
        raise StageError(f"archive {url}: {len(missing)} manifest member(s) not found by sha256 in the "
                         f"archive (e.g. {names}); the archive does not hold what the manifest says")


def stage(entries: Any, inputs: Path, *, download: Download = resumable_download,
          walk: Walk = default_walk, scratch: Path | None = None,
          min_free_bytes: int = DEFAULT_MIN_FREE_BYTES, check_only: bool = False,
          which: Callable[[str], str | None] = shutil.which,
          log: Callable[[str], None] = lambda msg: None) -> StageReport:
    """Stage every entry under `inputs`.

    Raises HeldSourceError before any I/O if any entry is not on the allow-list; StageError before
    any download if the preflight fails; and, AFTER trying every archive and file, one StageError
    listing every hash mismatch / missing member. A staged file that no longer hashes to the manifest
    is renamed `*.corrupt` and re-staged from a verified source (never trusted, never a wedge)."""
    entries = list(entries)
    refuse_unallowed(entries)
    scratch = scratch if scratch is not None else inputs / ".stage-work"
    bad = [e.name for e in entries if e.archive_sha256 is None and not (e.url or "").strip()]
    if bad:
        raise StageError(f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} with no URL and no archive "
                         f"hash cannot be fetched (e.g. {bad[0]}); inject entries need a source")
    inputs.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    _clean_scratch(scratch, inputs)
    fetch = _guarded(download)
    report = StageReport()

    direct: list[Any] = []
    archives: dict[tuple[str, str], list[Any]] = {}
    for e in entries:
        final = _final(inputs, e)
        if final.exists():
            got = sha256_file(final)
            if got == e.sha256:
                report.already_staged += 1
                continue
            quarantine = final.with_name(final.name + ".corrupt")
            os.replace(final, quarantine)
            report.quarantined += 1
            log(f"QUARANTINED {final} (sha256 {got}, manifest {e.sha256}) -> {quarantine.name}; re-staging")
        if e.archive_sha256 is None:
            direct.append(e)
        else:
            archives.setdefault((e.url, e.archive_sha256), []).append(e)

    problems = preflight([u for u, _ in archives], scratch, min_free_bytes=min_free_bytes, which=which)
    if problems:
        raise StageError("preflight failed, nothing was downloaded:\n  " + "\n  ".join(problems))
    if check_only:
        return report

    errors: list[str] = []
    for e in direct:
        try:
            _stage_direct(e, inputs, scratch, fetch, hashlib.sha256(e.url.encode()).hexdigest()[:16])
            report.files_fetched += 1
            report.staged += 1
        except HeldSourceError:
            raise
        except StageError as exc:
            errors.append(str(exc))
    for i, ((url, archive_sha), wanted) in enumerate(archives.items(), 1):
        log(f"archive {i}/{len(archives)}: {url} ({len(wanted)} member(s))")
        try:
            _stage_archive(url, archive_sha, wanted, inputs, scratch, fetch, walk,
                           hashlib.sha256(url.encode()).hexdigest()[:16])
            report.archives_fetched += 1
            report.staged += len(wanted)
        except HeldSourceError:
            raise
        except StageError as exc:
            errors.append(str(exc))
    if errors:
        raise StageError(f"{len(errors)} problem(s); everything else was staged and verified:\n  "
                         + "\n  ".join(errors))
    if not any(scratch.iterdir()):
        scratch.rmdir()
    return report


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None, *, download: Download = resumable_download) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool", default=str(pool.OUT))
    parser.add_argument("--inputs", required=True, help="the directory run_arm's --inputs points at")
    parser.add_argument("--scratch", default=None,
                        help="where archives are downloaded/extracted (default <inputs>/.stage-work; put "
                             "it OUTSIDE a directory that is later synced to a bucket)")
    parser.add_argument("--check", action="store_true",
                        help="preflight only: allow-list, per-source counts, bsdtar, scratch space; "
                             "downloads nothing")
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_BYTES / (1 << 30))
    parser.add_argument("--strict-sources", action="store_true",
                        help="exit 3 if any allowed source has no entries in the manifest")
    args = parser.parse_args(argv)
    try:
        entries = pool.read_pool(Path(args.pool))
        allowed, held = partition_entries(entries)
        for source, rows in sorted(held.items()):
            print(f"HELD {source}: {len(rows)} manifest entr{'y' if len(rows) == 1 else 'ies'} NOT staged "
                  f"- {HELD_SOURCES[source]}", file=sys.stderr)
        counts = dict.fromkeys(sorted(ALLOWED_SOURCES), 0)
        for e in allowed:
            counts[e.source] += 1
        print("manifest entries per allowed source: " + ", ".join(f"{s} {n}" for s, n in counts.items()),
              flush=True)
        empty = [s for s, n in counts.items() if n == 0]
        if empty:
            print(f"WARNING: allowed source(s) with ZERO manifest entries: {', '.join(empty)} - the "
                  f"staged benchmark will be missing that whole class of inputs", file=sys.stderr)
        report = stage(allowed, Path(args.inputs), download=download,
                       scratch=Path(args.scratch) if args.scratch else None,
                       min_free_bytes=int(args.min_free_gb * (1 << 30)), check_only=args.check,
                       log=lambda m: print(m, flush=True))
    except StageError as exc:
        print(f"STAGE FAILED: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"STAGE FAILED (interrupted, rerun to resume): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print("preflight OK; nothing downloaded")
    else:
        print(f"staged {report.staged} new, {report.already_staged} already staged and verified, "
              f"{report.quarantined} corrupt quarantined and re-staged, {report.archives_fetched} "
              f"archive(s) and {report.files_fetched} direct file(s) fetched; "
              f"{sum(len(v) for v in held.values())} held")
    return 3 if (empty and args.strict_sources) else 0


if __name__ == "__main__":
    raise SystemExit(main())
