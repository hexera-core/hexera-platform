#!/usr/bin/env python3
# Responsibility: Stage the CRS v3 verdict's inputs in the crsv3 transfer bucket: the 120 sampled
#                 failing parts (resolved locally by repair_corpus's own sampler and resolver), the
#                 sample.json that proves every arm ran them, the clean-control manifest, and the
#                 harness code at the run's commit.
# Owns: the GCS layout under <bucket>/eval/v3-verdict/ and the local sample.json write.
# Boundaries: it SAMPLES NOTHING ITSELF - repair_corpus.manifest_files draws and resolves, and
#             repair_corpus.write_bundle_sample writes the sample file. --dry-run prints every
#             upload and runs none; the local corpus is only read.
"""Stage the v3 verdict inputs.

    stage.py --commit <40-hex> --dry-run      # resolve, write sample.json, print the uploads
    stage.py --commit <40-hex>                # ... and run them

Layout (prefix defaults to gs://dev-crsv3-transfer-<projectnumber>/eval/v3-verdict):

    <prefix>/inputs/failing/<sha256>/<basename>   the 120 sampled failing parts
    <prefix>/inputs/clean/<sha256>/<basename>     the 100 clean parts (clean_control.py fetch)
    <prefix>/sample.json                          name + sha256, sorted - identical for every arm
    <prefix>/clean-control-100.json               the clean manifest
    <prefix>/code-<commit12>/code.tgz             `git archive <commit> src devtools/validation`
    <bucket>/eval/<arm>-<commit12>/<kind>/shard-I-of-N/  what each VM writes back (results root)

The inputs do not depend on the commit, so they are staged once; the code and the outputs are
per commit, so two commits can never write into one run.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROJECT = "hexera-dev"
PROJECT_NUMBER = "224734058693"
BUCKET = f"gs://dev-crsv3-transfer-{PROJECT_NUMBER}"
PREFIX = f"{BUCKET}/eval/v3-verdict"
CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                             str(Path.home() / "archductor/mesh-fail-corpus")))
VERDICT_DIR = REPO / "devtools/validation/evidence/2026-11-20-crs-v3-verdict"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")


def _run(cmd: list[str], *, dry: bool) -> None:
    print(("DRY-RUN  " if dry else "") + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if not dry:
        subprocess.run(cmd, check=True)


def resolve_sample(corpus: Path) -> tuple[list[Path], list[dict]]:
    """The sample's local files and its manifest entries, by repair_corpus's own code path:
    stratified_sample over the track manifest, then sha256 resolution under `corpus`."""
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    files, entries = rc.manifest_files(REPO / evalspec.MANIFEST, [corpus],
                                       sample=evalspec.SAMPLE_SIZE, seed=evalspec.SEED)
    return files, entries


def write_sample(entries: list[dict], directory: Path) -> Path:
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    written = rc.write_bundle_sample(directory, entries, seed=evalspec.SEED)
    if written is None:
        raise SystemExit("the sampler drew nothing")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", required=True,
                        help="the CODE commit (full 40-hex): its src/ and devtools/validation are "
                             "archived into code.tgz and run on the VMs (PYTHONPATH=src)")
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--corpus", default=str(CORPUS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if len(args.commit) != 40 or any(c not in "0123456789abcdef" for c in args.commit):
        parser.error("--commit must be the full 40-hex sha")
    prefix = args.prefix.rstrip("/")

    files, entries = resolve_sample(Path(args.corpus))
    if len(files) != len(entries) or len(entries) != evalspec.SAMPLE_SIZE:
        print(f"only {len(files)} of {len(entries)} sampled file(s) resolved under {args.corpus}; "
              f"refusing to stage a partial sample", file=sys.stderr)
        return 2
    sample = write_sample(entries, VERDICT_DIR)
    digest = evalspec.sample_sha256(sample)
    print(f"sample.json  {sample.relative_to(REPO)}  sha256 {digest}  ({len(entries)} files)")

    # manifest_files returns the paths in ENTRY ORDER when every entry resolved (checked above), so
    # the pairs line up; each upload is keyed by the manifest's sha256, re-verified here.
    rc = sys.modules["repair_corpus"]
    for path, entry in zip(files, entries, strict=True):
        got = rc._load_sibling("mesh_probe")._sha256(path)
        if got != entry["sha256"]:
            print(f"{path}: sha256 {got} != manifest {entry['sha256']}", file=sys.stderr)
            return 2
        _run(["gcloud", "storage", "cp", "--no-clobber", str(path),
              f"{prefix}/inputs/failing/{entry['sha256']}/{path.name}"], dry=args.dry_run)
    _run(["gcloud", "storage", "cp", str(sample), f"{prefix}/sample.json"], dry=args.dry_run)
    _run(["gcloud", "storage", "cp", str(REPO / evalspec.CLEAN_MANIFEST),
          f"{prefix}/clean-control-100.json"], dry=args.dry_run)

    # THE HARNESS AT THE COMMIT, not the working tree: the image carries the wheel built from the
    # commit, and the devtools it runs must be the same commit's or the arm measured a hybrid.
    code = Path(tempfile.gettempdir()) / f"v3-eval-code-{args.commit[:12]}.tgz"
    _run(["git", "-C", str(REPO), "archive", "--format=tar.gz", f"--output={code}", args.commit,
          "src", "devtools/validation"], dry=args.dry_run)
    _run(["gcloud", "storage", "cp", str(code), f"{prefix}/code-{args.commit[:12]}/code.tgz"],
         dry=args.dry_run)
    print(f"\n{'would stage' if args.dry_run else 'staged'} {len(files)} failing part(s), the "
          f"sample (sha256 {digest[:16]}), the clean manifest and code.tgz at {args.commit[:12]} "
          f"under {prefix}\nthe clean parts themselves: clean_control.py fetch --gcs "
          f"{prefix}/inputs/clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
