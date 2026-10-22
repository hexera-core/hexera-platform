#!/usr/bin/env python3
# Responsibility: The publication bundle shape shared by every benchmark runner - CAD repair and
#                 mesh alike. Owns the manifest fields, the on-disk layout, and the loader that
#                 re-reads a bundle for aggregation.
# Boundaries: pure files-and-dicts. No CAD kernel, no network, no database. Runners decide what a
#             row contains; this module only guarantees the envelope is complete and re-readable.

# WHY THIS EXISTS.
#
# docs/research/public-benchmarks.md commits every published number to a reproducible evidence
# bundle: provenance, redistribution status, input hashes, frozen settings, engine commit, and the
# per-case rows the aggregate was computed from. If each runner invented its own layout, the press
# table and the evidence would drift apart. One writer, one loader, one layout.
#
# LAYOUT. A bundle is a directory:
#   manifest.json   - provenance and freeze: everything needed to re-run, nothing about outcomes.
#   rows.jsonl      - one JSON object per case, written by the runner, append-ordered.
#   summary.json    - aggregate the loader recomputes from rows; stored for convenience only.
#   settings.json   - the exact runner settings dict, verbatim.
#   run.log         - free-form runner log, optional but reserved.
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BUNDLE_FILES = ("manifest.json", "rows.jsonl", "summary.json", "settings.json")

# Redistribution statuses the publication rules in public-benchmarks.md recognise. A bundle with
# any other string is refused at write time so a typo cannot launder a non-redistributable corpus.
REDISTRIBUTION_STATUSES = ("redistributable", "non-redistributable", "review-pending", "internal")


@dataclass
class BundleManifest:
    """Provenance and freeze for one benchmark run. Outcome-free by design."""

    benchmark: str                      # e.g. "cad-repair" | "mesh-engine"
    track: str                          # e.g. "occt-real-defect", "clean-geometry"
    corpus_name: str                    # human name of the corpus
    corpus_provenance: str              # where the inputs came from, with fetch instructions
    redistribution_status: str          # one of REDISTRIBUTION_STATUSES
    input_hashes: dict[str, str]        # relative input path -> sha256
    engine_commit: str                  # git commit of the engine under test
    settings_digest: str                # sha256 of settings.json content
    hardware_class: str                 # e.g. "dev-host-arm64", "cloudrun-cpu4"
    timeout_seconds: float | None = None
    random_seed: int | None = None
    image_digest: str | None = None     # container image digest when run containerised
    release_artifact: str | None = None # artifact another engineer re-runs from
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: int = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(root: Path, files: list[Path]) -> dict[str, str]:
    """Hash corpus inputs keyed by path relative to the corpus root, so the manifest is portable."""
    return {str(path.relative_to(root)): sha256_file(path) for path in sorted(files)}


def current_engine_commit(repo_root: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


class BundleWriter:
    """Writes one bundle directory. Rows stream in append order; the manifest seals the run."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._rows_path = directory / "rows.jsonl"
        self._row_count = 0

    def write_row(self, row: dict) -> None:
        with self._rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        self._row_count += 1

    def seal(self, manifest: BundleManifest, settings: dict, summary: dict) -> Path:
        if manifest.redistribution_status not in REDISTRIBUTION_STATUSES:
            raise ValueError(
                f"unknown redistribution_status {manifest.redistribution_status!r}; "
                f"expected one of {REDISTRIBUTION_STATUSES}")
        settings_text = json.dumps(settings, indent=2, sort_keys=True)
        manifest.settings_digest = hashlib.sha256(settings_text.encode()).hexdigest()
        (self.directory / "settings.json").write_text(settings_text + "\n", encoding="utf-8")
        (self.directory / "summary.json").write_text(
            json.dumps({**summary, "row_count": self._row_count}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (self.directory / "manifest.json").write_text(
            json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if not self._rows_path.exists():
            self._rows_path.touch()
        return self.directory


def load_bundle(directory: Path) -> tuple[dict, list[dict], dict, dict]:
    """Re-read a sealed bundle. Raises if any required file is missing - a partial bundle is not
    evidence. Returns (manifest, rows, summary, settings)."""
    missing = [name for name in BUNDLE_FILES if not (directory / name).exists()]
    if missing:
        raise FileNotFoundError(f"bundle at {directory} is missing {missing}")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    settings_text = (directory / "settings.json").read_text(encoding="utf-8")
    settings = json.loads(settings_text)
    expected = hashlib.sha256(settings_text.rstrip("\n").encode()).hexdigest()
    if manifest.get("settings_digest") != expected:
        raise ValueError(f"settings.json does not match manifest settings_digest in {directory}")
    rows = [json.loads(line) for line in
            (directory / "rows.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if summary.get("row_count") != len(rows):
        raise ValueError(
            f"summary row_count {summary.get('row_count')} != rows.jsonl rows {len(rows)}")
    return manifest, rows, summary, settings
