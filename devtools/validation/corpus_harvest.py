#!/usr/bin/env python3
# Responsibility: Harvest real mesh failures from an ABC chunk: download, extract, probe every STEP
#                 file with gmsh, keep the failures, record a per-chunk summary, drop the bulk.
# Owns: the child probe protocol, outcome classification, manifest merging and the chunk summary.
# Boundaries: gmsh runs only in a child interpreter per file (never forked into a threaded parent).
#             CAD files go to the local corpus dir only; the committed track manifest carries
#             names and hashes, never geometry.
"""Probe pattern (same as the original corpus build): gmsh OCC import, NumThreads 1,
MeshSizeMax = diagonal / 40, generate(2) then generate(3), 120 s budget, one interpreter per file.

    .venv/bin/python devtools/validation/corpus_harvest.py --chunk 0051 \\
        --scratch ~/archductor/harvest-scratch --corpus ~/archductor/mesh-fail-corpus

A restart resumes: finished files are read back from <scratch>/<chunk>/results.jsonl.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import concurrent.futures.process
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

BUDGET_S = 120.0
WORKERS = 7
SIZE_DIVISOR = 40.0
SAMPLE_FRACTION = 0.01
NYU = "https://archive.nyu.edu/retrieve"
#: chunk -> /retrieve/<id>/ path, read off each chunk's handle page (handle 2451/44318 + chunk up to 0057, 44319 + chunk after).
#: Chunk 0058 has no step archive on its handle page (only ofs), so it is absent.
CHUNK_IDS = {"0050": 88830, "0051": 88834, "0052": 88838, "0053": 88842, "0054": 88846,
             "0055": 88850, "0056": 88854, "0057": 88858, "0059": 88873, "0060": 88877,
             "0061": 88881}
PROBE = {"gmsh": "4.15.2", "num_threads": 1, "mesh_size_max": "diagonal / 40",
         "generate": [2, 3], "pass": "more than zero volume elements", "budget_s": 120}

# --------------------------------------------------------------------------- child probe


def _child(path: str) -> int:
    """Runs in its own interpreter. Prints STAGE:<n> markers, then one RESULT:<json> line."""
    import gmsh

    def stage(name: str) -> None:
        sys.stdout.write(f"STAGE:{name}\n")
        sys.stdout.flush()

    result: dict[str, Any] = {"solids": None, "faces": None, "diag_mm": None,
                              "volume_elements": 0, "error": None, "stage": "import",
                              "log_surface": None}
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.logger.start()
        stage("import")
        try:
            gmsh.model.occ.importShapes(path)
            gmsh.model.occ.synchronize()
            result["solids"] = len(gmsh.model.getEntities(3))
            result["faces"] = len(gmsh.model.getEntities(2))
            x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
            diag = float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)
            result["diag_mm"] = round(diag, 1)
            gmsh.option.setNumber("Mesh.MeshSizeMax", diag / SIZE_DIVISOR)
            if result["solids"]:
                result["stage"] = "2d"
                stage("2d")
                gmsh.model.mesh.generate(2)
                result["stage"] = "3d"
                stage("3d")
                gmsh.model.mesh.generate(3)
                _t, tags, _n = gmsh.model.mesh.getElements(3)
                result["volume_elements"] = sum(len(t) for t in tags)
        except Exception as exc:  # noqa: BLE001 - a gmsh failure is the answer
            result["error"] = str(exc)
            result["log"] = [ln for ln in gmsh.logger.get() if "Meshing surface" in ln][-1:]
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write("RESULT:" + json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------- classification


# Mirrors meshpipeline.cad.repair.mesh_probe.classify (kind only): the tool is standalone, so it
# shares no code with the package and runs on a bare host with only the gmsh wheel.
_OVERLAP = re.compile(r"overlapping facets\)?\s+on surface\s+(\d+)", re.I)
_PERIODIC = re.compile(r"periodic surface\s+(\d+)", re.I)
_PLC = re.compile(r"segment and a facet intersect", re.I)
_OPEN_LOOP = re.compile(r"not to be forming a closed loop", re.I)


def classify_error(error: str) -> str:
    """The failure family named by a gmsh error message; `other` when none matches."""
    if _OVERLAP.search(error):
        return "overlapping_facets"
    if _PERIODIC.search(error):
        return "periodic_surface"
    if _PLC.search(error):
        return "edge_through_face"
    if _OPEN_LOOP.search(error):
        return "open_loop"
    return "other"


def _last_stage(output: str) -> str:
    stages = [ln[6:] for ln in output.splitlines() if ln.startswith("STAGE:")]
    return stages[-1] if stages else ""


def interpret_child(output: str, returncode: int | None, timed_out: bool) -> dict[str, Any]:
    """Turn a child's stdout and exit into a row. Pure; never raises.

    status: pass | fail | timeout | no_solids. A fail's family is the gmsh failure kind, or
    `crash` when the child died without answering.
    """
    row: dict[str, Any] = {"status": "fail", "family": None, "stage": "", "gmsh_error": None,
                           "solids": None, "faces": None, "diag_mm": None}
    if timed_out:
        row.update(status="timeout", family="timeout", stage=_last_stage(output),
                   gmsh_error="FAIL: timeout rc=124")
        return row
    payload = None
    for line in output.splitlines():
        if line.startswith("RESULT:"):
            try:
                payload = json.loads(line[7:])
            except ValueError:
                payload = None
    if not isinstance(payload, dict):
        row.update(family="crash", stage=_last_stage(output),
                   gmsh_error=f"FAIL: crash rc={returncode}")
        return row
    row.update(solids=payload.get("solids"), faces=payload.get("faces"),
               diag_mm=payload.get("diag_mm"))
    error = payload.get("error")
    if error is not None:
        stage = str(payload.get("stage") or "")
        kind = classify_error(str(error))
        first = str(error).strip().splitlines()[0] if str(error).strip() else "gmsh error"
        row.update(family=kind, stage=stage, gmsh_error=f"FAIL: {first}"[:300])
        return row
    if not payload.get("solids"):
        row.update(status="no_solids")
        return row
    if int(payload.get("volume_elements") or 0) > 0:
        row.update(status="pass")
        return row
    row.update(family="no_volume", stage="3d", gmsh_error="FAIL: no volume elements")
    return row


# --------------------------------------------------------------------------- manifests


def track_entry(row: dict[str, Any], chunk: str) -> dict[str, Any]:
    return {"name": f"abc/{chunk}_{Path(row['name']).name}", "source": f"abc-chunk-{chunk}",
            "sha256": row["sha256"], "solids": row.get("solids"), "faces": row.get("faces"),
            "diag_mm": row.get("diag_mm"), "gmsh_error": row.get("gmsh_error"),
            "diag_source": "gmsh"}


def corpus_entry(row: dict[str, Any], chunk: str, archive_url: str) -> dict[str, Any]:
    return {"file": f"abc/{chunk}_{Path(row['name']).name}", "source": f"abc-chunk-{chunk}",
            "archive_url": archive_url, "sha256": row["sha256"], "solids": row.get("solids"),
            "faces": row.get("faces"), "diag_mm": row.get("diag_mm"),
            "gmsh_error": row.get("gmsh_error")}


def _merge(manifest: dict, entries: list[dict], key: str) -> tuple[dict, bool]:
    out = dict(manifest)
    files = list(out.get("files", []))
    seen = {f["sha256"] for f in files}
    added = False
    for entry in entries:
        if entry["sha256"] not in seen:
            seen.add(entry["sha256"])
            files.append(entry)
            added = True
    out["files"] = files
    return out, added


def merge_track(track: dict, entries: list[dict], source: dict, *, note: str) -> dict:
    """Add new failures (deduped by sha256) and the chunk's source; bump `version` on change."""
    out, added = _merge(track, entries, "name")
    sources = list(out.get("sources", []))
    new_source = source["name"] not in {s["name"] for s in sources}
    if new_source:
        sources.append(source)
    out["sources"] = sources
    if added or new_source:
        out["version"] = int(track.get("version", 1)) + 1
        out["notes"] = [*track.get("notes", []), note]
    return out


def merge_corpus(manifest: dict, entries: list[dict]) -> dict:
    return _merge(manifest, entries, "file")[0]


# --------------------------------------------------------------------------- summary


def summarise(rows: list[dict], chunk: str, *, sample_fraction: float = SAMPLE_FRACTION,
              seed: int = 0) -> dict[str, Any]:
    failed = [r for r in rows if r["status"] in ("fail", "timeout")]
    passed = sorted((r for r in rows if r["status"] == "pass"), key=lambda r: r["sha256"])
    k = max(1, round(len(passed) * sample_fraction)) if passed else 0
    sample = random.Random(f"{chunk}:{seed}").sample(passed, k) if k else []
    return {
        "chunk": chunk,
        "total": len(rows),
        "passed": sum(r["status"] == "pass" for r in rows),
        "failed": sum(r["status"] == "fail" for r in rows),
        "timeouts": sum(r["status"] == "timeout" for r in rows),
        "no_solids": sum(r["status"] == "no_solids" for r in rows),
        "by_family": dict(Counter(r["family"] for r in failed)),
        "by_stage": dict(Counter(r["stage"] for r in failed if r.get("stage"))),
        "family_by_stage": dict(Counter(f"{r['family']}/{r.get('stage') or '?'}" for r in failed)),
        "clean_sample": sorted(({"name": r["name"], "sha256": r["sha256"]} for r in sample),
                               key=lambda c: c["sha256"]),
    }


def legacy_family(gmsh_error: str) -> str:
    """Family of a manifest row that predates family/stage fields, from its error text."""
    text = gmsh_error or ""
    if "timeout" in text or "crash" in text:
        return "timeout"
    if "no volume elements" in text:
        return "no_volume"
    return classify_error(text)


def aggregate(rows: list[dict]) -> dict[str, Any]:
    """Family x stage x solid-count distribution over failure rows (family, stage, solids, faces)."""
    import statistics

    families: dict[str, dict[str, Any]] = {}
    faces: dict[str, list[int]] = {}
    for r in rows:
        fam = families.setdefault(r["family"], {
            "total": 0, "stage": {"2d": 0, "3d": 0, "unknown": 0},
            "solids": {"single": 0, "multi": 0, "unknown": 0}})
        fam["total"] += 1
        stage = r.get("stage")
        fam["stage"][stage if stage in ("2d", "3d") else "unknown"] += 1
        n = r.get("solids")
        fam["solids"]["unknown" if n is None else "single" if n <= 1 else "multi"] += 1
        if r.get("faces") is not None:
            faces.setdefault(r["family"], []).append(r["faces"])
    for name, fam in families.items():
        fam["median_faces"] = statistics.median(faces[name]) if name in faces else None
    return {"total": len(rows), "families": dict(sorted(families.items()))}


def chunk_url(chunk: str) -> str:
    return f"{NYU}/{CHUNK_IDS[chunk]}/abc_{chunk}_step_v00.7z"


def load_results(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                done[row["name"]] = row
            except (ValueError, KeyError, TypeError):
                continue
    return done


# --------------------------------------------------------------------------- probing


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def probe_one(task: tuple[str, str]) -> dict[str, Any]:
    """Worker: hash the file, probe it in a child interpreter, return a row. Never raises."""
    name, path = task
    started = time.monotonic()
    row: dict[str, Any] = {"name": name}
    try:
        row["sha256"] = _sha256(Path(path))
        row["bytes"] = Path(path).stat().st_size
        cmd = [sys.executable, "-I", "-W", "ignore", str(Path(__file__).resolve()), "--child", path]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True, text=True,
                                errors="replace")
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=BUDGET_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                out, _ = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out = ""
        finally:
            if proc.poll() is None:
                _kill_group(proc)
        row.update(interpret_child(out or "", proc.returncode, timed_out))
    except Exception as exc:  # noqa: BLE001 - one bad file is a row, not a dead run
        row.update(status="fail", family="crash", stage="", gmsh_error=f"FAIL: harness {exc}")
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


def run_all(tasks: list, workers: int, fn=probe_one) -> list[dict]:
    """Per-future collection; a dead worker breaks the pool, so survivors are kept and the
    unfinished tasks rerun one at a time (a task that kills its own worker is a crash row)."""
    rows: list[dict] = []
    unfinished: list = []
    broken = concurrent.futures.process.BrokenProcessPool

    def _collect(group: list, n: int, retry: bool) -> None:
        with concurrent.futures.ProcessPoolExecutor(max_workers=n) as pool:
            futures = [(pool.submit(fn, t), t) for t in group]
            for future, task in futures:
                try:
                    rows.append(future.result())
                except broken:
                    if retry:
                        rows.append({"name": task[0], "status": "fail", "family": "crash",
                                     "stage": "", "gmsh_error": "FAIL: worker died",
                                     "sha256": _sha256(Path(task[1]))})
                    else:
                        unfinished.append(task)
                except Exception as exc:  # noqa: BLE001
                    rows.append({"name": task[0], "status": "fail", "family": "crash",
                                 "stage": "", "gmsh_error": f"FAIL: harness {exc}",
                                 "sha256": _sha256(Path(task[1]))})

    _collect(list(tasks), workers, retry=False)
    for task in unfinished:
        _collect([task], 1, retry=True)
    return rows


# --------------------------------------------------------------------------- chunk driver


def download(url: str, dest: Path, *, attempts: int = 30) -> Path:
    """Resumable curl until the on-disk size equals the server's Content-Length."""
    head = subprocess.run(["curl", "-sIL", url], capture_output=True, text=True, check=True).stdout
    sizes = [int(ln.split(":")[1]) for ln in head.splitlines() if ln.lower().startswith("content-length")]
    if not sizes:
        raise RuntimeError(f"no Content-Length from {url}")
    want = sizes[-1]
    for attempt in range(attempts):
        have = dest.stat().st_size if dest.exists() else 0
        if have == want:
            return dest
        if have > want:
            dest.unlink()
        subprocess.run(["curl", "-sL", "-C", "-", "-o", str(dest), url], check=False)
        time.sleep(min(60, 5 * (attempt + 1)))
    raise RuntimeError(f"download of {url} incomplete after {attempts} attempts")


def extract(archive: Path, into: Path) -> list[Path]:
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(into)], check=True)
    return sorted(p for p in into.rglob("*") if p.is_file() and p.suffix.lower() in (".step", ".stp"))


def _log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


def harvest_chunk(chunk: str, *, archive: Path | None, scratch: Path, corpus: Path,
                  track_manifest: Path | None, workers: int, keep: bool) -> dict:
    work = scratch / chunk
    work.mkdir(parents=True, exist_ok=True)
    url = chunk_url(chunk) if chunk in CHUNK_IDS else ""
    if archive is None:
        archive = download(url, work / f"abc_{chunk}_step_v00.7z")
    archive_size = archive.stat().st_size
    archive_sha = _sha256(archive)
    _log(f"{chunk}: archive {archive_size} bytes sha256 {archive_sha}")
    root = work / "extract"
    files = extract(archive, root) if not root.exists() or not any(root.rglob("*")) else sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".step", ".stp"))
    _log(f"{chunk}: {len(files)} STEP files")

    results = work / "results.jsonl"
    done = load_results(results)
    todo = [(str(p.relative_to(root)), str(p)) for p in files if str(p.relative_to(root)) not in done]
    started = time.monotonic()
    step = 200
    for i in range(0, len(todo), step):
        batch = run_all(todo[i:i + step], workers)
        with results.open("a") as handle:
            for row in batch:
                handle.write(json.dumps(row) + "\n")
                done[row["name"]] = row
        fails = sum(r["status"] in ("fail", "timeout") for r in done.values())
        _log(f"{chunk}: {len(done)}/{len(files)} probed, {fails} failed, "
             f"{time.monotonic() - started:.0f}s this run")

    rows = list(done.values())
    summary = summarise(rows, chunk)
    summary.update(archive_url=url, archive_bytes=archive_size, archive_sha256=archive_sha,
                   probe=PROBE, wall_seconds_last_run=round(time.monotonic() - started, 1))
    (work / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")

    failures = [r for r in rows if r["status"] in ("fail", "timeout")]
    dest = corpus / "abc"
    dest.mkdir(parents=True, exist_ok=True)
    for row in failures:
        shutil.copy2(root / row["name"], dest / f"{chunk}_{Path(row['name']).name}")
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "probe": "gmsh 4.15.2, OCC import, MeshSizeMax=diag/40, generate(2)+generate(3), "
                 "120 s timeout, 1 thread", "files": []}
    merged = merge_corpus(manifest, [corpus_entry(r, chunk, url) for r in failures])
    manifest_path.write_text(json.dumps(merged, indent=1) + "\n")
    if track_manifest is not None:
        track = json.loads(track_manifest.read_text())
        source = {"name": f"abc-chunk-{chunk}", "archive_url": url, "archive_sha256": archive_sha}
        out = merge_track(track, [track_entry(r, chunk) for r in failures], source,
                          note=f"abc-chunk-{chunk}: {len(failures)} real failures harvested")
        track_manifest.write_text(json.dumps(out, indent=1) + "\n")
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
        archive.unlink(missing_ok=True)
    _log(f"{chunk}: done {summary['passed']} pass, {len(failures)} fail")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--chunk", help="ABC chunk number, e.g. 0051")
    parser.add_argument("--archive", type=Path, help="an already downloaded chunk archive")
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--corpus", type=Path, default=Path.home() / "archductor/mesh-fail-corpus")
    parser.add_argument("--track-manifest", type=Path)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--keep", action="store_true", help="keep the archive and extracted files")
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child)
    if not args.scratch or not (args.chunk or args.archive):
        parser.error("--scratch and one of --chunk/--archive are required")
    chunk = args.chunk or args.archive.name.split("_")[1]
    harvest_chunk(chunk, archive=args.archive, scratch=args.scratch, corpus=args.corpus,
                  track_manifest=args.track_manifest, workers=args.workers, keep=args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
