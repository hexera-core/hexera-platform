#!/usr/bin/env python3
# Responsibility: The 100-part clean control for the CRS v3 verdict: which parts (a seeded draw of
#                 the harvest's 1% passing sample), where each one lives (ABC chunk archive, URL and
#                 archive sha256), and fetching exactly those parts into the eval staging bucket.
# Owns: `build_manifest` (pure) and the `fetch` path (download a chunk, extract only the drawn
#       members, check every sha256, upload).
# Boundaries: the committed manifest carries names, hashes, sources and probe metadata - never CAD.
#             Fetching is the only thing here that touches the network or a bucket, and --dry-run
#             prints those commands instead of running them.
"""Build or fetch the clean control.

    # (re)build the committed manifest from the harvest record (no network)
    clean_control.py build

    # fetch the 100 parts from the ABC archive and stage them (on a VM or the host)
    clean_control.py fetch --work /tmp/clean --gcs gs://<bucket>/eval/v3-verdict/inputs/clean
    clean_control.py fetch ... --dry-run     # print what would run

WHY THESE 100. The harvest (evidence/2026-11-19-corpus-harvest) probed every STEP file of ten ABC
chunks and kept a seeded 1% of the PASSING files per chunk, by name and sha256: 887 parts the same
gmsh probe meshed. The control is a seeded draw of 100 of those 887, sorted by sha256 first so the
draw is the seed's alone. Every drawn part names its chunk, the chunk archive's URL and the archive
sha256 recorded in the track manifest, so the files can be re-fetched and checked by anybody.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
POOL = REPO / "devtools/validation/evidence/2026-11-19-corpus-harvest/clean-samples.json"
TRACK = REPO / "devtools/validation/corpora/mesh-failure-real.json"
OUT = REPO / "devtools/validation/corpora/clean-control-100.json"
LANDED = Path.home() / "archductor/harvest-scratch/landed"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")

#: Probe fields carried over from the harvest's per-file results, when the results are on hand.
_META = ("solids", "faces", "diag_mm", "bytes", "seconds")


def sources_by_chunk(track: dict) -> dict[str, dict]:
    """chunk -> {archive_url, archive_sha256}, from the track manifest's `abc-chunk-NNNN` sources."""
    out = {}
    for source in track.get("sources") or []:
        name = str(source.get("name") or "")
        if name.startswith("abc-chunk-"):
            out[name[len("abc-chunk-"):]] = {"archive_url": source["archive_url"],
                                             "archive_sha256": source["archive_sha256"]}
    return out


def build_manifest(pool: dict, track: dict, *, n: int, seed: int,
                   metadata: dict[str, dict] | None = None) -> dict:
    """`n` parts drawn from the harvest's clean sample, each with its source. Pure.

    `pool` is clean-samples.json (chunk -> [{name, sha256}]); `metadata` optionally maps sha256 to
    the harvest's probe row, whose `_META` fields are copied beside the entry."""
    sources = sources_by_chunk(track)
    entries = []
    for chunk, items in pool.items():
        if chunk not in sources:
            raise ValueError(f"chunk {chunk} has no abc-chunk-{chunk} source in the track manifest")
        for item in items:
            entries.append({"name": item["name"], "sha256": item["sha256"], "chunk": chunk,
                            **sources[chunk]})
    entries.sort(key=lambda e: e["sha256"])
    if len({e["sha256"] for e in entries}) != len(entries):
        raise ValueError("the clean pool repeats a sha256")
    if n > len(entries):
        raise ValueError(f"the clean pool holds {len(entries)} parts, fewer than {n}")
    drawn = sorted(random.Random(seed).sample(entries, n), key=lambda e: e["sha256"])
    for entry in drawn:
        row = (metadata or {}).get(entry["sha256"]) or {}
        for key in _META:
            if row.get(key) is not None:
                entry[f"harvest_{key}"] = row[key]
    return {
        "track": "clean-control",
        "about": ("seeded draw of the 2026-11-19 harvest's 1% PASSING sample (clean-samples.json): "
                  "parts the harvest gmsh probe meshed. Names, hashes and sources only - no CAD."),
        "seed": seed, "n": n, "pool": str(POOL.relative_to(REPO)), "pool_size": len(entries),
        "probe": track.get("probe"),
        "files": drawn,
    }


def harvest_metadata(landed: Path) -> dict[str, dict]:
    """sha256 -> the harvest's probe row, from <landed>/<chunk>/results.jsonl if present."""
    out: dict[str, dict] = {}
    for results in sorted(landed.glob("*/results.jsonl")):
        for line in results.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out[row["sha256"]] = row
    return out


def build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="clean_control.py build")
    parser.add_argument("--n", type=int, default=evalspec.CLEAN_SIZE)
    parser.add_argument("--seed", type=int, default=evalspec.SEED)
    parser.add_argument("--landed", default=str(LANDED),
                        help="harvest scratch with <chunk>/results.jsonl, for probe metadata")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    meta = harvest_metadata(Path(args.landed)) if Path(args.landed).is_dir() else {}
    manifest = build_manifest(json.loads(POOL.read_text()), json.loads(TRACK.read_text()),
                              n=args.n, seed=args.seed, metadata=meta)
    for entry in manifest["files"]:
        row = meta.get(entry["sha256"])
        if meta and (row is None or row.get("status") != "pass"):
            raise SystemExit(f"{entry['name']} is not a passing file in the harvest results")
    Path(args.out).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    chunks = sorted({e["chunk"] for e in manifest["files"]})
    print(f"wrote {args.out}: {len(manifest['files'])} parts from {len(chunks)} chunk(s) "
          f"{', '.join(chunks)}; probe metadata {'attached' if meta else 'absent'}")
    return 0


# --------------------------------------------------------------------------- fetch

def _run(cmd: list[str], *, dry: bool) -> None:
    print(("DRY-RUN  " if dry else "") + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if not dry:
        subprocess.run(cmd, check=True)


def fetch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="clean_control.py fetch")
    parser.add_argument("--manifest", default=str(OUT))
    parser.add_argument("--work", required=True, help="scratch directory for archives")
    parser.add_argument("--gcs", required=True,
                        help="destination prefix; each part lands at <prefix>/<sha256>/<basename>")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    harvest = _load("corpus_harvest", REPO / "devtools/validation/corpus_harvest.py")
    manifest = json.loads(Path(args.manifest).read_text())
    by_chunk: dict[str, list[dict]] = {}
    for entry in manifest["files"]:
        by_chunk.setdefault(entry["chunk"], []).append(entry)
    work = Path(args.work)
    for chunk, entries in sorted(by_chunk.items()):
        url, want = entries[0]["archive_url"], entries[0]["archive_sha256"]
        archive = work / chunk / f"abc_{chunk}_step_v00.7z"
        into = work / chunk / "x"
        print(f"chunk {chunk}: {len(entries)} part(s) from {url}")
        if args.dry_run:
            print(f"DRY-RUN  download {url} -> {archive} (resumable curl), check sha256 {want}")
            _run(["bsdtar", "-xf", str(archive), "-C", str(into),
                  *[e["name"] for e in entries]], dry=True)
        else:
            archive.parent.mkdir(parents=True, exist_ok=True)
            harvest.download(url, archive)
            got = harvest._sha256(archive)
            if got != want:
                raise SystemExit(f"chunk {chunk}: archive sha256 {got} != recorded {want}")
            into.mkdir(parents=True, exist_ok=True)
            # ONLY THE DRAWN MEMBERS are extracted; the other ~9,900 files never touch disk.
            _run(["bsdtar", "-xf", str(archive), "-C", str(into),
                  *[e["name"] for e in entries]], dry=False)
        for entry in entries:
            local = into / entry["name"]
            if not args.dry_run:
                got = harvest._sha256(local)
                if got != entry["sha256"]:
                    raise SystemExit(f"{entry['name']}: sha256 {got} != manifest {entry['sha256']}")
            _run(["gcloud", "storage", "cp", "--no-clobber", str(local),
                  f"{args.gcs.rstrip('/')}/{entry['sha256']}/{Path(entry['name']).name}"],
                 dry=args.dry_run)
        if not args.dry_run:
            archive.unlink(missing_ok=True)
    print(f"{'would stage' if args.dry_run else 'staged'} {len(manifest['files'])} clean part(s) "
          f"under {args.gcs}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("build", "fetch"):
        print(__doc__)
        return 2
    return build(argv[1:]) if argv[0] == "build" else fetch(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
