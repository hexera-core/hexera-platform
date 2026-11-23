#!/usr/bin/env python3
# Responsibility: The CRS v4 benchmark's candidate pool manifest: every file that MIGHT enter the
#                 benchmark, with its source, sha256, URL and licence, before any checker has run.
# Owns: `PoolEntry`, the per-source adapters (ABC, NIST, Fusion 360 Gallery, injected defects),
#       `build_pool` (merge, check licence, dedupe by sha256) and `write_pool`.
# Boundaries: the committed pool.json carries names, hashes, URLs and licences - never CAD. Fetching
#             (NIST, F360) goes through an injected `Download`, so everything here is testable
#             offline; only `urllib_download` touches the network. Evaluating CAD (the split into
#             dev / held-out / clean-control) is a later stage and does not live here.
"""Build the candidate pool.

    pool.py build                      # ABC + OCCT from the committed manifests only (no network)
    pool.py build --nist --f360        # also fetch the NIST zips and the Fusion 360 archive

Sources, in precedence order (the first source to supply a sha256 keeps it):
  abc_fail / occt  corpora/mesh-failure-real.json (the harvest's real mesh failures)
  abc_pass         evidence/2026-11-19-corpus-harvest/clean-samples.json (the harvest's passing sample)
  nist             NIST MBE PMI CTC/FTC/STC models, from two zips reached through nist.gov
                   `/document/<slug>` redirects (the page itself links only to drawing PDFs). Every
                   export variant is kept; variants of one part share a `group` (e.g. nist-ctc-01),
                   so the variants of a part can be kept together. CAVEAT: these are NOT distinct vendor
                   translations of one native part. They are 3-4 AP203/AP242 and PMI-level STEP
                   exports per part from NIST's own reference pipeline (the 5 STC parts have a
                   single STEP each), so cross-kernel diversity must be measured from checker
                   results, not assumed from the group. The STC zip holds native CAD only (CATPart/prt/ipt), no
                   STEP, so the STC STEP files come from the PMI STEP zip.
  f360             Fusion 360 Gallery segmentation extended STEP set (s2.0.1) - a non-Onshape origin
                   (ABC and DeepCAD are both Onshape-derived). Its STEP files are scaled x10.
  inject           injected-defect variants, made by inject_pool.py (recipes in injected.json); `build`
                   carries the existing ones forward. They are direct files, not archive members.

Archives are streamed to a temp file and hashed in chunks; members are read from the zip one at a
time, never extracted to disk. Every hash for a file this module downloads is computed here from the
bytes. Hashes carried by the
committed ABC/OCCT manifests were computed by the harvest and are recorded as-is.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TRACK = REPO / "devtools/validation/corpora/mesh-failure-real.json"
CLEAN = REPO / "devtools/validation/evidence/2026-11-19-corpus-harvest/clean-samples.json"
OUT = REPO / "devtools/validation/corpora/v4/pool.json"

Source = Literal["abc_fail", "abc_pass", "occt", "nist", "f360", "inject"]
#: Download `url` (following redirects) into the given path.
Download = Callable[[str, Path], None]

LICENCE = {
    "abc": ("ABC dataset: copyright of the CAD models is owned by their creators; for licensing details "
            "see Onshape Terms of Use 1.g.ii (https://deep-geometry.github.io/abc-dataset/); "
            "authors give no warranties"),
    # The OCCT string carries its UNVERIFIED marker into the manifest; confirm before publication.
    "occt": ("UNVERIFIED - Open CASCADE Technology 7.8.0 sample dataset (opencascade-dataset-7.8.0, "
             "https://github.com/Open-Cascade-SAS/OCCT/releases/tag/V7_8_0); the release notes give "
             "LGPL 2.1 with exception for OCCT itself, and dataset-specific terms were not confirmed"),
    "nist": ("NIST Disclaimer on the download page: \"The test cases, CAD models, and STEP files can be "
             "used without any restrictions\" (https://www.nist.gov/el/systems-integration-division-73400/"
             "mbe-pmi-validation-and-conformance-testing/download-free-cad)"),
    "f360": ("Fusion 360 Gallery Dataset License (Autodesk; non-commercial research use, no "
             "redistribution of the Dataset in its entirety): "
             "https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/LICENSE.md"),
}

# Each is a nist.gov redirect to a zip (verified 2026-11); the final system/files/... URL carries a
# dated path and is deliberately not hardcoded.
# The STC zip (https://www.nist.gov/document/nist-stc-pmi-v4) is deliberately absent: it holds only
# CATPart/prt/ipt/pdf, no STEP; the STC STEP files are in nist-pmi-step-files.
NIST_ARCHIVES = (
    "https://www.nist.gov/document/nist-pmi-step-files",
    "https://www.nist.gov/document/nist-ftc-ctc-pmi-cad-models",
)
F360_ARCHIVE = ("https://fusion-360-gallery-dataset.s3.us-west-2.amazonaws.com/"
                "segmentation/s2.0.1/s2.0.1_extended_step.zip")

# What a source must yield: a fetch below this is a wrong guess about the remote layout, not a small
# dataset. The F360 count is the documented size of the s2.0.1 extended STEP set. NIST: the PMI STEP
# zip alone holds 33 STEP files.
NIST_MIN_ENTRIES = 30
F360_MIN_ENTRIES = 42_912

#: Facts a consumer of the pool must not discover by surprise, written to the manifest per source.
SOURCE_NOTES = {
    "nist": ("Per part, 3-4 AP203/AP242 and PMI-level STEP exports from NIST's own reference pipeline "
             "(CTC 01-05 and FTC 06-11); NOT distinct vendor translations of one native part. The 5 STC "
             "parts carry a single STEP each (the STC zip holds native CAD only)."),
    "f360": ("STEP files are scaled by a factor of 10 by the dataset's data-exchange process (bounding "
             "box longest side in [-10, 10]); NOT rescaled here. Absolute-length fidelity bounds such "
             "as min(0.1 mm, 0.001 x diagonal) mean something different on these parts."),
}

#: The note an `inject` source carries when the manifest holds inject entries but no note for them. Owned here so
#: `build` can fall back to it; inject_pool.py re-exports it.
INJECT_NOTE = ("Injected-defect variants of clean parts (the five structural kinds only; the four quarantined kinds "
               "are excluded because they read back as valid solids). Each entry is a direct file whose recipe "
               "(clean source sha256, kind, seed, label, OCCT version, mesh image digest) is in injected.json; the "
               "hosted file, addressed by its sha256, is the canonical artifact and regeneration is an audit under "
               "the pinned image. The label is in injected.json, not here: join on sha256. Fidelity for these is "
               "measured against the damaged input, never the clean source. The licence is the clean source's. "
               "They stand in for customer translation defects that no public source reproduces: they are "
               "SYNTHETIC, not real vendor translations.")

_STEP_SUFFIXES = (".stp", ".step")


@dataclass(frozen=True)
class PoolEntry:
    name: str
    source: Source
    sha256: str
    url: str
    archive_sha256: str | None
    licence: str
    group: str | None = None  # variants of one part (NIST translators) share a group
    # `inject` entries only: the injector's own label (`label["kind"]`, `label["status"]`), carried so the
    # quarantine gate reads what the injector declared instead of guessing from the file name. None elsewhere.
    kind: str | None = None
    kind_status: str | None = None


class PoolSource(Protocol):
    def entries(self) -> list[PoolEntry]: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def urllib_download(url: str, dest: Path) -> None:
    # nist.gov answers 403 to urllib's default User-Agent; a plain client name is enough.
    request = urllib.request.Request(url, headers={"User-Agent": "v4-eval-pool-builder/1"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out, 1 << 20)


def _zip_step_entries(archive: Path, url: str, source: Source, licence: str,
                      group_of: Callable[[str], str | None] = lambda name: None) -> list[PoolEntry]:
    """Every STEP member of a zip as an entry; each member is hashed from its bytes, one at a time."""
    archive_sha = sha256_file(archive)
    out: list[PoolEntry] = []
    with zipfile.ZipFile(archive) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            if info.is_dir() or not info.filename.lower().endswith(_STEP_SUFFIXES):
                continue
            digest = hashlib.sha256()
            with z.open(info) as member:
                for chunk in iter(lambda: member.read(1 << 20), b""):
                    digest.update(chunk)
            out.append(PoolEntry(info.filename, source, digest.hexdigest(), url, archive_sha, licence,
                                 group_of(info.filename)))
    return out


# --------------------------------------------------------------------------- sources

#: Pool sources an injected variant may be derived from (a clean part with a cleared licence).
INJECT_SOURCES: frozenset[str] = frozenset({"abc_pass", "occt", "nist"})


def inject_group(source: PoolEntry) -> str:
    """The group every injected variant of `source` carries; it is also how a pool entry names its clean source."""
    return f"inject-{source.sha256[:12]}"


def inject_entry(source: PoolEntry, *, name: str, sha256: str, url: str, label: dict[str, Any]) -> PoolEntry:
    """THE adapter for an injected-variant pool entry; nothing else may construct an `inject` PoolEntry.

    An injected variant of a part is governed by that part's terms, so the licence is DERIVED from `source`
    verbatim and cannot be passed: there is no licence parameter. The group is derived too (`inject_group`), so
    the entry names its clean source. The kind and its evidence status come from the injector's own label
    (`label["kind"]`, `label["status"]`); a label missing either carries None, and the quarantine gate refuses
    an `inject` entry with either missing. `check_inject_licences` re-proves the derivation on every manifest
    write, so an entry built by hand with another licence is refused there."""
    if source.source not in INJECT_SOURCES:
        raise ValueError(f"{name}: cannot derive an injected variant from a {source.source!r} entry; "
                         f"clean sources are {sorted(INJECT_SOURCES)}")
    return PoolEntry(name, "inject", sha256, url, None, source.licence, inject_group(source),
                     kind=label.get("kind"), kind_status=label.get("status"))


def inject_entry_from_case(source: PoolEntry, case: Any, *, url: str) -> PoolEntry:
    """`inject_entry` for one `inject()` output (a file on disk plus its label)."""
    return inject_entry(source, name=case.path.name, sha256=sha256_file(case.path), url=url, label=case.label)


def check_inject_licences(entries: Iterable[PoolEntry]) -> None:
    """Refuse an `inject` entry whose licence differs from its clean source's (found by its group). An entry
    whose source is not among `entries` is not judged here (a partial pool; `inject_pool.check_pool` reports
    that case); one whose licence is wrong is always refused."""
    entries = list(entries)
    clean = {inject_group(e): e for e in entries if e.source != "inject"}
    for e in entries:
        if e.source != "inject":
            continue
        source = clean.get(e.group or "")
        if source is not None and e.licence != source.licence:
            raise ValueError(f"{e.name}: licence differs from its clean source {source.name}'s; an injected "
                             f"variant is governed by its source's terms")


class StaticSource:
    """Entries already in hand (also the home of `inject` entries from the injector)."""

    def __init__(self, entries: Iterable[PoolEntry]) -> None:
        self._entries = list(entries)

    def entries(self) -> list[PoolEntry]:
        return list(self._entries)


class AbcSource:
    """ABC failures, ABC passes and the OCCT dataset, from the harvest's committed manifests.

    `track` is mesh-failure-real.json (sources + failing files); `clean` is clean-samples.json
    (chunk -> [{name, sha256}]). Each entry's URL is its chunk's archive, with the archive sha256."""

    def __init__(self, track: dict, clean: dict) -> None:
        self.track = track
        self.clean = clean

    def entries(self) -> list[PoolEntry]:
        archives = {s["name"]: s for s in self.track.get("sources") or []}
        out: list[PoolEntry] = []
        for row in self.track.get("files") or []:
            archive = archives[row["source"]]
            occt = row["source"].startswith("occt")
            out.append(PoolEntry(row["name"], "occt" if occt else "abc_fail", row["sha256"],
                                 archive["archive_url"], archive["archive_sha256"],
                                 LICENCE["occt" if occt else "abc"]))
        for chunk, items in self.clean.items():
            archive = archives[f"abc-chunk-{chunk}"]
            for item in items:
                out.append(PoolEntry(item["name"], "abc_pass", item["sha256"],
                                     archive["archive_url"], archive["archive_sha256"], LICENCE["abc"]))
        return out


_NIST_PART = re.compile(r"(ctc|ftc|stc)[-_ ]?0*(\d+)", re.IGNORECASE)


def nist_group(filename: str) -> str | None:
    """`nist_ctc_01_asme1_ap203.stp` or `CTC 01` -> `nist-ctc-01`: the part id, whichever translator wrote it."""
    match = _NIST_PART.search(filename)
    if match is None:
        return None
    return f"nist-{match.group(1).lower()}-{int(match.group(2)):02d}"


def _nist_group_required(member: str) -> str:
    group = nist_group(member.rsplit("/", 1)[-1])
    if group is None:
        raise ValueError(f"NIST file {member!r}: no CTC/FTC/STC part id in the name, cannot group it")
    return group


class NistSource:
    """NIST MBE PMI models: each `/document/<slug>` URL is followed (redirect) to a zip, which is
    downloaded, hashed, and walked for STEP members. Raises when an archive holds no STEP file, when
    the total is below `min_entries`, or when a member's part id cannot be read."""

    def __init__(self, archives: Iterable[str] = NIST_ARCHIVES, download: Download = urllib_download,
                 min_entries: int = NIST_MIN_ENTRIES) -> None:
        self.archives = list(archives)
        self.download = download
        self.min_entries = min_entries

    def entries(self) -> list[PoolEntry]:
        out: list[PoolEntry] = []
        with tempfile.TemporaryDirectory(prefix="v4pool-nist-") as tmp:
            for url in self.archives:
                dest = Path(tmp) / "archive.zip"
                self.download(url, dest)
                found = _zip_step_entries(dest, url, "nist", LICENCE["nist"], _nist_group_required)
                if not found:
                    raise ValueError(f"NIST archive {url} holds no STEP files")
                out.extend(found)
                dest.unlink()
        if len(out) < self.min_entries:
            raise ValueError(f"NIST archives yielded {len(out)} STEP files, expected at least "
                             f"{self.min_entries}")
        return out


class F360Source:
    """Fusion 360 Gallery segmentation extended STEP set (s2.0.1): one zip streamed to a temp file
    and walked member by member; STEP files live under `breps/step`. Not rescaled (see SOURCE_NOTES)."""

    def __init__(self, archive_url: str = F360_ARCHIVE, download: Download = urllib_download,
                 min_entries: int = F360_MIN_ENTRIES) -> None:
        self.archive_url = archive_url
        self.download = download
        self.min_entries = min_entries

    def entries(self) -> list[PoolEntry]:
        with tempfile.TemporaryDirectory(prefix="v4pool-f360-") as tmp:
            dest = Path(tmp) / "archive.zip"
            self.download(self.archive_url, dest)
            out = _zip_step_entries(dest, self.archive_url, "f360", LICENCE["f360"])
        if len(out) < self.min_entries:
            raise ValueError(f"Fusion 360 archive {self.archive_url} yielded {len(out)} STEP files, expected "
                             f"at least {self.min_entries}: nested archive or a different layout/suffix")
        return out


# --------------------------------------------------------------------------- build

def merge_pool(sources: Iterable[PoolSource]) -> tuple[list[PoolEntry], list[tuple[PoolEntry, PoolEntry]]]:
    """Merge sources; a missing licence is an error; one entry per sha256 (first source wins).

    Returns the entries and the `(kept, dropped)` pairs the dedupe collapsed."""
    seen: dict[str, PoolEntry] = {}
    dropped: list[tuple[PoolEntry, PoolEntry]] = []
    for source in sources:
        for entry in source.entries():
            if not entry.licence.strip():
                raise ValueError(f"{entry.name}: licence is required on every pool entry")
            if entry.sha256 in seen:
                dropped.append((seen[entry.sha256], entry))
            else:
                seen[entry.sha256] = entry
    return list(seen.values()), dropped


def _log_dedupe(dropped: list[tuple[PoolEntry, PoolEntry]]) -> None:
    """Say what the dedupe dropped. A drop that collapses two members of one group means a
    'translator variant' was byte-identical to another - not a variant at all - which is itself a
    finding, so it is logged by name."""
    for kept, gone in dropped:
        if kept.group is not None and kept.group == gone.group:
            log.warning("group %s: %s is byte-identical to %s (sha256 %s); collapsed to one entry",
                        kept.group, gone.name, kept.name, kept.sha256)
    if dropped:
        log.warning("dedupe by sha256 dropped %d duplicate entr%s", len(dropped),
                    "y" if len(dropped) == 1 else "ies")


def build_pool(sources: Iterable[PoolSource]) -> list[PoolEntry]:
    """`merge_pool`, logging what the dedupe dropped."""
    entries, dropped = merge_pool(sources)
    _log_dedupe(dropped)
    return entries


def carry_forward_inject(path: Path) -> list[PoolEntry]:
    """The inject entries already in the manifest at `path` ([] if there is none). `build` rewrites the manifest
    from scratch, and no source here fetches injected files, so without this a rebuild would delete them."""
    if not path.exists():
        return []
    return [e for e in read_pool(path) if e.source == "inject"]


def _licence_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _archive_id(url: str, archive_sha256: str | None) -> str:
    return hashlib.sha256(f"{url}\n{archive_sha256}".encode()).hexdigest()[:12]


def write_pool(entries: list[PoolEntry], out: Path = OUT, *, sources_included: list[str],
               deduped: int = 0, notes: dict[str, str] | None = None) -> None:
    """Compact JSON with repeated values interned. Two tables, each entry pointing into them:
    `licences` (id -> text, via `licence_id`) and `archives` (id -> {url, archive_sha256}, via
    `archive_id`; one archive URL/hash serves every member it holds). Every entry still carries a
    non-empty licence and url - this is storage only, and an entry lacking either is refused.

    `sources_included` records which flag set built this file, so a partial pool is never mistaken
    for the full one."""
    check_inject_licences(entries)
    licences: dict[str, str] = {}
    archives: dict[str, dict[str, str | None]] = {}
    rows = []
    for e in sorted(entries, key=lambda e: (e.source, e.name)):
        if not e.licence.strip():
            raise ValueError(f"{e.name}: licence is required on every pool entry")
        if not e.url.strip():
            raise ValueError(f"{e.name}: url is required on every pool entry")
        row = asdict(e)
        for optional in ("kind", "kind_status"):  # additive: a pool with no inject entries is byte-identical
            if row[optional] is None:
                del row[optional]
        row["licence_id"] = _licence_id(row.pop("licence"))
        licences[row["licence_id"]] = e.licence
        row["archive_id"] = _archive_id(e.url, e.archive_sha256)
        archives[row["archive_id"]] = {"url": row.pop("url"), "archive_sha256": row.pop("archive_sha256")}
        rows.append(row)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 3, "sources_included": sources_included, "deduped": deduped,
                "source_notes": notes or {}, "licences": licences, "archives": archives, "files": rows}
    out.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n")


def read_pool(path: Path = OUT) -> list[PoolEntry]:
    """Inverse of `write_pool`: rehydrates each entry's licence, url and archive_sha256."""
    manifest = json.loads(path.read_text())
    if manifest.get("version") != 3:
        raise ValueError(f"{path}: pool manifest version {manifest.get('version')}, expected 3")
    out = []
    for row in manifest["files"]:
        licence = manifest["licences"][row.pop("licence_id")]
        archive = manifest["archives"][row.pop("archive_id")]
        out.append(PoolEntry(**row, licence=licence, url=archive["url"],
                             archive_sha256=archive["archive_sha256"]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pool.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build")
    build.add_argument("--nist", action="store_true", help="fetch the NIST STEP zips")
    build.add_argument("--f360", action="store_true", help="fetch the Fusion 360 extended STEP archive")
    build.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    sources: list[PoolSource] = [AbcSource(json.loads(TRACK.read_text()), json.loads(CLEAN.read_text()))]
    included = ["abc"]
    if args.nist:
        sources.append(NistSource())
        included.append("nist")
    if args.f360:
        sources.append(F360Source())
        included.append("f360")
    out_path = Path(args.out)
    carried = carry_forward_inject(out_path)
    if carried:
        sources.append(StaticSource(carried))
        included.append("inject")
        print(f"keeping {len(carried)} inject entries already in {out_path} (see inject_pool.py)")
    entries, dropped = merge_pool(sources)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1
    for wanted in included[1:]:
        if not counts.get(wanted):
            raise SystemExit(f"requested source {wanted} contributed no entries; not writing {args.out}")
    _log_dedupe(dropped)
    write_pool(entries, Path(args.out), sources_included=included, deduped=len(dropped),
               notes={**{k: v for k, v in SOURCE_NOTES.items() if k in included},
                      **({"inject": ((json.loads(out_path.read_text()).get("source_notes") or {})
                                     .get("inject", INJECT_NOTE))} if carried else {})})
    print(f"wrote {args.out}: {len(entries)} entries {counts}; sources {included}; "
          f"{len(dropped)} duplicate(s) dropped by sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
