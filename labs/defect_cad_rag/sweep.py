"""Independent action sweep: every applicable operation, tried alone on the same pre-repair state.

WHY NOT THE CORPUS RUN'S OWN LOG. `repair_corpus.py` records what the rules planner chose, in the
order it chose it, on a shape earlier operations had already changed. That is observational: an
operation the planner never reached has no label, and a later attempt's outcome depends on the
earlier ones. Here every (defect, operation) pair starts from the same snapshot, so each outcome
is a statement about that operation on that geometry alone.

NOTHING IS WEAKENED. Each attempt goes through `RepairSession.try_op`, so caps, rollback,
re-inspection and the progress rule judge it exactly as they would in a run. The paired-rim guard
lives in the planner, not the session, so it is re-applied here before any fill is tried.

CRASH ISOLATION. `main` runs every file in its own interpreter (`--one`). OCCT can kill the
process; a killed file becomes a `crashed` row with its exit code, and records already flushed for
that file survive.

Usage:
    PYTHONPATH=src:. .venv/bin/python -m labs.defect_cad_rag.sweep ~/cadcorpus/occt \
        --out /tmp/dcr-sweep --workers 6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

#: Lab posture: every default-off geometry operation is switched ON so the sweep learns what each
#: one would do. These are environment for the CHILD only, never for any customer path.
LAB_FLAGS = {
    "CAD_REPAIR_ENABLED": "true",
    "CAD_REPAIR_AUTONOMOUS": "true",
    "CAD_REPAIR_FILL_PLANAR_HOLES": "true",
    "CAD_REPAIR_FILL_NONPLANAR_HOLES": "true",
    "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "true",
    "CAD_REPAIR_FIX_SMALL_FACES": "true",
    "CAD_REPAIR_REVIEW_ENABLED": "false",
}

#: Per file. A file with 476 located defects is not 476 times the evidence of one with one; it is
#: one part. Sampling is stratified by defect code so a rare code is not drowned by a common one.
MAX_DEFECTS_PER_FILE = 24
#: Inner budget for attempts in one file; the outer subprocess timeout is a little longer.
FILE_BUDGET_S = 150.0
SUFFIXES = {".stp", ".step", ".STP", ".STEP"}


def family_of(name: str, known: set[str] | None = None) -> str:
    """The part family a file belongs to, for grouped splits.

    A HEURISTIC, and documented as one: the OCCT corpus has no family manifest. The first token
    of the stem is the family (`bm2_ec_t4-A` -> `bm2`, `bug25013_25013face` -> `bug25013`); a
    file whose later token is itself a known family joins that family instead
    (`bug21802_as1-oc-214` -> `as1`), because a bug report about a part is that part.
    """
    tokens = [t for t in re.split(r"[_\-.\s]+", Path(name).stem.lower()) if t]
    if not tokens:
        return "unknown"
    if known:
        for token in tokens[1:]:
            if token in known:
                return token
    return tokens[0]


def _stratified(defects, limit: int) -> list[int]:
    """Indices into `defects`, round-robin over codes, each code ordered by a stable hash."""
    by_code: dict[str, list[int]] = {}
    for i, d in enumerate(defects):
        by_code.setdefault(d.code.value, []).append(i)
    for code in by_code:
        by_code[code].sort(key=lambda i: hashlib.sha1(
            f"{defects[i].fingerprint}:{i}".encode()).hexdigest())
    out: list[int] = []
    while len(out) < limit and any(by_code.values()):
        for code in sorted(by_code):
            if by_code[code] and len(out) < limit:
                out.append(by_code[code].pop(0))
    return sorted(out)


def _effects(base, found) -> dict:
    """What a kept attempt removed and introduced, by (fingerprint, code)."""
    before = {(d.fingerprint, d.code.value) for d in base}
    after = {(d.fingerprint, d.code.value) for d in found}
    return {"removed": len(before - after), "introduced": len(after - before)}


def sweep_file(path: Path, emit) -> dict:
    """Sweep one file, calling `emit(record)` per defect as soon as it is done."""
    import meshpipeline.settings.cad_repair as rcfg
    from labs.defect_cad_rag.patch import DESCRIPTOR_VERSION, _Topology, describe, isolate_patch, scale_band
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.operations import FILL_OPS, OPERATIONS
    from meshpipeline.cad.repair.plan import paired_rim_fingerprints
    from meshpipeline.cad.repair.session import RepairSession

    started = time.monotonic()
    shape = _read_shape(path)
    session = RepairSession(shape)
    defects = session.defects()
    tolerance = min(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM,
                    max(session.original.diagonal_mm, 1.0) * rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO)
    rims = paired_rim_fingerprints(shape, defects)
    fill_guard = ("rims_unknown" if getattr(rims, "unknown", False)
                  else "paired_rim" if rims else "")
    base = (session.shape, session._defects, session.complete_scan, session.accounting)
    topo = _Topology(shape)
    ops = tuple(sorted(OPERATIONS, key=lambda op: op.scope))
    chosen = _stratified(defects, MAX_DEFECTS_PER_FILE)
    truncated = 0

    for i in chosen:
        defect = defects[i]
        t0 = time.monotonic()
        try:
            patch = isolate_patch(shape, defect, topology=topo)
            t1 = time.monotonic()
            vector = describe(patch, topology=topo)
            t2 = time.monotonic()
            patch_error = ""
        except Exception as exc:  # noqa: BLE001 - an unisolatable defect is a finding, not a crash
            patch, vector, patch_error = None, None, f"{type(exc).__name__}: {exc}"
            t1 = t2 = time.monotonic()

        attempts: list[dict] = []
        rules_op = ""
        for op in ops:
            if defect.code not in op.answers:
                continue
            try:
                targets = op.targets_for(defect)
            except Exception as exc:  # noqa: BLE001
                attempts.append({"op": op.name, "status": "no_targets",
                                 "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if not targets:
                continue
            if op.name in FILL_OPS and fill_guard:
                attempts.append({"op": op.name, "status": "guarded", "reason": fill_guard})
                continue
            rules_op = rules_op or op.name
            if time.monotonic() - started > FILE_BUDGET_S:
                attempts.append({"op": op.name, "status": "budget"})
                truncated += 1
                continue
            # RESTORE THE SNAPSHOT, so every attempt sees the same pre-repair state. Private
            # fields, on purpose: this is the lab, and the alternative - a fresh session per
            # attempt - re-scans the original part every time for the same answer.
            session.shape, session._defects, session.complete_scan, session.accounting = base
            a0 = time.monotonic()
            verdict = session.try_op(op, targets, defect.code, tolerance_mm=tolerance)
            attempt = {"op": op.name, "status": verdict.status, "reason": verdict.reason[:160],
                       "basis": verdict.basis, "seconds": round(time.monotonic() - a0, 4)}
            if verdict.kept:
                attempt.update(_effects(base[1], session._defects))
            attempts.append(attempt)
        session.shape, session._defects, session.complete_scan, session.accounting = base

        emit({
            "kind": "defect", "defect_index": i, "code": defect.code.value,
            "entity_type": defect.entity_type, "region": defect.region,
            "fingerprint": defect.fingerprint, "ambiguous": defect.ambiguous,
            "descriptor_version": DESCRIPTOR_VERSION, "vector": vector,
            "patch_error": patch_error,
            "scale_mm": patch.scale_mm if patch else None,
            "part_scale_mm": patch.part_scale_mm if patch else None,
            "scale_band": scale_band(patch) if patch else "",
            "level_sizes": [len(lv) for lv in patch.levels] if patch else [],
            "isolate_s": round(t1 - t0, 5), "describe_s": round(t2 - t1, 5),
            "rules_op": rules_op, "attempts": attempts,
        })

    return {"defects_located": len(defects), "defects_sampled": len(chosen),
            "complete_scan": session.complete_scan, "fill_guard": fill_guard,
            "tolerance_mm": tolerance, "budget_truncated_attempts": truncated,
            "seconds": round(time.monotonic() - started, 3)}


def _one(path: Path, out: Path, family: str) -> None:
    """Child entry point: write one JSONL shard, flushing each record as it is made."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    head = {"file": path.name, "file_sha256": sha, "family": family}
    with out.open("w") as fh:
        def emit(record: dict) -> None:
            fh.write(json.dumps({**head, **record}) + "\n")
            fh.flush()
        try:
            summary = sweep_file(path, emit)
            emit({"kind": "file", "outcome": "swept", **summary})
        except Exception as exc:  # noqa: BLE001
            emit({"kind": "file", "outcome": "error", "error": f"{type(exc).__name__}: {exc}"})


def _stamp() -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  check=False).stdout.strip()
        except OSError:
            return ""
    try:
        from OCP.Standard import Standard_Version  # type: ignore[attr-defined]
        occt = str(Standard_Version.String_s())
    except Exception:  # noqa: BLE001
        occt = ""
    return {"engine_commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "occt": occt, "python": sys.version.split()[0], "flags": LAB_FLAGS,
            "max_defects_per_file": MAX_DEFECTS_PER_FILE, "file_budget_s": FILE_BUDGET_S}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=FILE_BUDGET_S + 60)
    ap.add_argument("--families", type=Path,
                    help="JSON {file name: family}; overrides the name heuristic where present")
    ap.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--family", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.one:
        _one(args.one, args.out, args.family)
        return 0

    files = sorted(p for root in args.roots for p in root.expanduser().rglob("*")
                   if p.suffix in SUFFIXES and p.is_file())
    if args.limit:
        files = files[: args.limit]
    firsts = [family_of(p.name) for p in files]
    known = {f for f in firsts if firsts.count(f) >= 2}
    manifest = json.loads(args.families.read_text()) if args.families else {}
    shards = args.out / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **LAB_FLAGS}

    def run(path: Path) -> dict:
        shard = shards / f"{hashlib.sha1(str(path).encode()).hexdigest()[:16]}.jsonl"
        if shard.exists() and '"kind": "file"' in shard.read_text():
            return {"file": path.name, "outcome": "cached"}
        family = manifest.get(path.name) or family_of(path.name, known)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "labs.defect_cad_rag.sweep", "--out", str(shard),
                 "--one", str(path), "--family", family],
                env=env, capture_output=True, text=True, timeout=args.timeout, check=False)
            code, tail = proc.returncode, proc.stderr[-400:]
        except subprocess.TimeoutExpired:
            code, tail = "timeout", ""
        text = shard.read_text() if shard.exists() else ""
        if '"kind": "file"' not in text:
            # THE CHILD DIED OR RAN OUT OF TIME. Its flushed defect records stay; this row says
            # why the file is incomplete, so the loss is counted rather than silent.
            with shard.open("a") as fh:
                fh.write(json.dumps({"file": path.name, "family": family, "kind": "file",
                                     "outcome": "crashed" if code != "timeout" else "timeout",
                                     "returncode": code, "stderr_tail": tail,
                                     "seconds": round(time.monotonic() - t0, 2)}) + "\n")
        return {"file": path.name, "outcome": "done", "code": code}

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, result in enumerate(pool.map(run, files), 1):
            if n % 25 == 0 or n == len(files):
                print(f"{n}/{len(files)} files  {time.monotonic() - started:.0f}s", flush=True)
            del result

    records = args.out / "records.jsonl"
    with records.open("w") as fh:
        for shard in sorted(shards.glob("*.jsonl")):
            fh.write(shard.read_text())
    (args.out / "stamp.json").write_text(json.dumps(
        {**_stamp(), "files": len(files), "wall_s": round(time.monotonic() - started, 1)},
        indent=2))
    print(f"wrote {records}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
