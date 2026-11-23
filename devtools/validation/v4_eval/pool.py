#!/usr/bin/env python3
# Responsibility: The CRS v4 benchmark's candidate pool manifest: every file that MIGHT enter the
#                 benchmark, with its source, sha256, URL and licence, before any checker has run.
# Owns: `PoolEntry`, the per-source adapters (ABC, NIST, Fusion 360 Gallery, injected defects),
#       `build_pool` (merge, check licence, dedupe by sha256) and `write_pool`.
# Boundaries: the committed pool.json carries names, hashes, URLs and licences - never CAD. Fetching
#             (NIST, F360) goes through an injected `Transport`, so everything here is testable
#             offline; only `urllib_transport` touches the network. Evaluating CAD (the split into
#             dev / held-out / clean-control) is a later stage and does not live here.
"""Build the candidate pool.

    pool.py build                      # ABC + OCCT from the committed manifests only (no network)
    pool.py build --nist --f360        # also fetch the NIST page and the Fusion 360 archive

Sources, in precedence order (the first source to supply a sha256 keeps it):
  abc_fail / occt  corpora/mesh-failure-real.json (the harvest's real mesh failures)
  abc_pass         evidence/2026-11-19-corpus-harvest/clean-samples.json (the harvest's passing sample)
  nist             NIST MBE PMI CTC/FTC/STC models. Every translator variant is kept; variants of one
                   part share a `group` (e.g. nist-ctc-01), since the same part exported through
                   several translators is what reproduces real cross-kernel defects.
  f360             Fusion 360 Gallery, segmentation extended STEP set - a non-Onshape origin
                   (ABC and DeepCAD are both Onshape-derived).
  inject           reserved for the injected-defect variants; no fetcher lives here.

Every hash for a file this module downloads is computed here from the bytes. Hashes carried by the
committed ABC/OCCT manifests were computed by the harvest and are recorded as-is.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Literal, Protocol

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TRACK = REPO / "devtools/validation/corpora/mesh-failure-real.json"
CLEAN = REPO / "devtools/validation/evidence/2026-11-19-corpus-harvest/clean-samples.json"
OUT = REPO / "devtools/validation/corpora/v4/pool.json"

Source = Literal["abc_fail", "abc_pass", "occt", "nist", "f360", "inject"]
Transport = Callable[[str], bytes]

# UNVERIFIED against the datasets' own terms text - confirm before the benchmark is published.
LICENCE = {
    "abc": "ABC dataset terms (non-commercial research use): https://deep-geometry.github.io/abc-dataset/",
    "occt": "Open CASCADE Technology 7.8.0 sample dataset: https://dev.opencascade.org/resources/licensing",
    "nist": "NIST MBE PMI models: US Government work, public domain (see NIST download page terms)",
    "f360": "Fusion 360 Gallery Dataset License: https://github.com/AutodeskAILab/Fusion360GalleryDataset",
}

NIST_PAGE = ("https://www.nist.gov/el/systems-integration-division-73400/"
             "mbe-pmi-validation-and-conformance-testing/download-free-cad")
# UNVERIFIED: the documented segmentation-extended STEP archive; confirm the URL on first real build.
F360_ARCHIVE = ("https://fusion-360-gallery-dataset.s3.us-west-2.amazonaws.com/"
                "segmentation/s2.0.0/s2.0.0_extended_step.zip")

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


class PoolSource(Protocol):
    def entries(self) -> list[PoolEntry]: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def urllib_transport(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https dataset hosts
        return bytes(response.read())


# --------------------------------------------------------------------------- sources

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


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


_NIST_PART = re.compile(r"(ctc|ftc|stc)[-_ ]?0*(\d+)", re.IGNORECASE)


def nist_group(filename: str) -> str | None:
    """`nist_ctc_01_asme1_ap203.stp` -> `nist-ctc-01`: the part id, whichever translator wrote it."""
    match = _NIST_PART.search(filename)
    if match is None:
        return None
    return f"nist-{match.group(1).lower()}-{int(match.group(2)):02d}"


class NistSource:
    """NIST MBE PMI models. Reads the download page's links to STEP files (NOT verified against the
    live page: assumes direct .stp/.step hrefs), downloads each, hashes the bytes."""

    def __init__(self, page_url: str = NIST_PAGE, transport: Transport = urllib_transport) -> None:
        self.page_url = page_url
        self.transport = transport

    def entries(self) -> list[PoolEntry]:
        links = _Links()
        links.feed(self.transport(self.page_url).decode("utf-8", errors="replace"))
        out: list[PoolEntry] = []
        for href in sorted(set(links.hrefs)):
            if not href.lower().split("?")[0].endswith(_STEP_SUFFIXES):
                continue
            url = urllib.parse.urljoin(self.page_url, href)
            name = url.split("?")[0].rsplit("/", 1)[-1]
            out.append(PoolEntry(name, "nist", sha256_hex(self.transport(url)), url, None,
                                 LICENCE["nist"], nist_group(name)))
        return out


class F360Source:
    """Fusion 360 Gallery segmentation-extended STEP set: one zip, every STEP member an entry."""

    def __init__(self, archive_url: str = F360_ARCHIVE, transport: Transport = urllib_transport) -> None:
        self.archive_url = archive_url
        self.transport = transport

    def entries(self) -> list[PoolEntry]:
        data = self.transport(self.archive_url)
        archive_sha = sha256_hex(data)
        out: list[PoolEntry] = []
        with zipfile.ZipFile(BytesIO(data)) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                if info.is_dir() or not info.filename.lower().endswith(_STEP_SUFFIXES):
                    continue
                out.append(PoolEntry(info.filename, "f360", sha256_hex(z.read(info)),
                                     self.archive_url, archive_sha, LICENCE["f360"]))
        return out


# --------------------------------------------------------------------------- build

def build_pool(sources: Iterable[PoolSource]) -> list[PoolEntry]:
    """Merge sources; a missing licence is an error; one entry per sha256 (first source wins)."""
    seen: dict[str, PoolEntry] = {}
    for source in sources:
        for entry in source.entries():
            if not entry.licence.strip():
                raise ValueError(f"{entry.name}: licence is required on every pool entry")
            seen.setdefault(entry.sha256, entry)
    return list(seen.values())


def write_pool(entries: list[PoolEntry], out: Path = OUT) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted((asdict(e) for e in entries), key=lambda r: (r["source"], r["name"]))
    out.write_text(json.dumps({"version": 1, "files": rows}, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pool.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build")
    build.add_argument("--nist", action="store_true", help="fetch the NIST page and its STEP files")
    build.add_argument("--f360", action="store_true", help="fetch the Fusion 360 extended STEP archive")
    build.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    sources: list[PoolSource] = [AbcSource(json.loads(TRACK.read_text()), json.loads(CLEAN.read_text()))]
    if args.nist:
        sources.append(NistSource())
    if args.f360:
        sources.append(F360Source())
    entries = build_pool(sources)
    write_pool(entries, Path(args.out))
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1
    print(f"wrote {args.out}: {len(entries)} entries {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
