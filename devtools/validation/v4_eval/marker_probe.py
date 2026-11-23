#!/usr/bin/env python3
# Responsibility: Establish, against the REAL wave bucket, that its conditional create (the model ledger's
#                 `ObjectMarkerStore.create`) is atomic, and record the verdict the launcher gates on.
# Owns: the two-writer probe (sequential + race, read-back of the stored value), the pass record, and
#       `probe_problem`, the launcher's check of that record. Also a directory-backed store (and three broken
#       variants of it) so the probe can be exercised, and proven able to FAIL, without a bucket.
# Boundaries: it writes only under `_probe/` in the marker prefix and a single record at PROBE_KEY. The launcher
#             never runs it; a human runs it once per bucket (see the command in the module docstring). It
#             reaches the bucket only through `MinioStore` (env MINIO_*), so tests never touch a network.
"""The two-writer probe for `MinioStore.create_object_if_absent`.

WHY. Every cross-host guarantee of the model ledger (no two shards share an index, a held-out wave runs once, at
most 3 checkpoint waves) rests on a conditional PUT (`If-None-Match: *` plus `x-goog-if-generation-match: 0`) sent
through a private minio-py method (`gcs:` uses the native `if_generation_match=0` write instead). A backend that IGNORES those headers answers 200 to both writers: both get True,
the original race is back, and nothing errors. MinIO honours `If-None-Match` on PUT only in recent releases and nobody
has established which backend the wave bucket is. This probe establishes it, end to end, through `ObjectMarkerStore`
(not a raw PUT), so what it proves is what the ledger will do.

WHAT IT DOES.
  1. sequential: create a fresh key twice with different payloads; the first must be True, the second False, and the
     stored value must still be the FIRST writer's.
  2. race: two real processes (`--role writer`, A and B) are released together by a wall-clock barrier and, for each
     of ROUNDS fresh keys, both call create with DIFFERENT payloads. Exactly one may get True and one False, and the
     object read back must hold the winner's payload (this is what catches a silent overwrite).
  3. on success - and only if at least MIN_OVERLAPPING_ROUNDS rounds had the two calls overlap in time, so the
     writers provably raced - it writes the pass record the launcher reads (PROBE_KEY: backend, version, time,
     rounds). The overlap count is printed.
A failure writes a `passed: false` record, so a regression revokes an earlier pass.

RUN IT (controller, once per bucket; the host's Application Default Credentials need object write on it):
    python devtools/validation/v4_eval/marker_probe.py --store gcs:<wave bucket name> --rounds 50
Two hosts (the strongest form): on host 1 and host 2, at the same instant T (epoch seconds, a minute ahead):
    python marker_probe.py --store gcs:<bucket> --role writer --writer A --run-id R --start-at T   # host 1
    python marker_probe.py --store gcs:<bucket> --role writer --writer B --run-id R --start-at T   # host 2
    python marker_probe.py --store gcs:<bucket> --role verify --run-id R                           # anywhere, after
Exit 0 only when the probe passed and the record was written.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

PROBE_KEY = "_probe/conditional-create"
PROBE_VERSION = 1
PROBE_ROUNDS = 50
#: A pass needs at least this many rounds in which the two create calls OVERLAPPED in time. A non-atomic
#: check-then-write passes if the writers never raced; 10 of the default 50 rounds is a fifth of them, far
#: above what a backend that really serialises needs and far below what two barrier-released writers manage.
MIN_OVERLAPPING_ROUNDS = 10
PROBE_MAX_AGE_S = 14 * 86400      # a backend can be reconfigured: the pass expires
WRITERS = ("A", "B")


class LocalDirStore:
    """An object store over a directory, with the surface ObjectMarkerStore needs. `mode` makes it a correct one
    (`honour`) or one of three BROKEN ones, which are the probe's negative controls:
      ignores-precondition   the dangerous one: every create writes and answers True
      overwrites-silently    answers False to a second create but overwrites the object anyway
      never-creates          answers False to every create and stores nothing"""

    MODES = ("honour", "ignores-precondition", "overwrites-silently", "never-creates")

    def __init__(self, root: str | Path, mode: str = "honour") -> None:
        if mode not in self.MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self._root, self._mode = Path(root), mode

    def _path(self, key: str) -> Path:
        return self._root / key

    def exists(self, *, object_key: str) -> bool:
        return self._path(object_key).is_file()

    def get_bytes(self, *, object_key: str) -> bytes:
        return self._path(object_key).read_bytes()

    def upload_file(self, *, local_path: Path, object_key: str, content_type: str | None = None) -> None:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        tmp.write_bytes(Path(local_path).read_bytes())
        tmp.replace(path)

    def create_object_if_absent(self, *, local_path: Path, object_key: str, content_type: str | None = None) -> bool:
        import os
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = Path(local_path).read_bytes()
        if self._mode == "ignores-precondition":
            path.write_bytes(data)
            return True
        if self._mode == "never-creates":
            return False
        if self._mode == "overwrites-silently":
            existed = path.exists()
            path.write_bytes(data)
            return not existed
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return True


def open_markers(spec: str) -> tuple[Any, str]:
    """(ObjectMarkerStore, backend id) for a store spec: `gcs:<bucket>` (the native GCS conditional write over
    this host's ADC - what the shards use; backend id `gcs/<bucket>`), `minio` (MINIO_* env, kept for
    non-GCS deployments) or `localdir[-<mode>]:<dir>` (a directory, for the probe's own tests)."""
    from meshpipeline.cad.repair_v4.model_ledger import GcsObjectStore, ObjectMarkerStore
    if spec.startswith("gcs:") and len(spec) > 4:
        store = GcsObjectStore(spec[4:])
        return ObjectMarkerStore(store), store.backend_id
    if spec == "minio":
        import meshpipeline.settings.providers as provcfg
        from meshpipeline.adapters.object_storage.minio import MinioStore
        return ObjectMarkerStore(MinioStore()), f"{provcfg.MINIO_ENDPOINT}/{provcfg.MINIO_BUCKET}"
    head, _, root = spec.partition(":")
    if head.startswith("localdir") and root:
        mode = head[len("localdir-"):] if head.startswith("localdir-") else "honour"
        return ObjectMarkerStore(LocalDirStore(root, mode)), f"localdir:{Path(root).resolve()}"
    raise ValueError(f"unknown store spec {spec!r}: use `gcs:<bucket>`, `minio` or `localdir[-mode]:<dir>`")


# --- the checks -----------------------------------------------------------------------------------------

def sequential_check(markers: Any, run_id: str) -> list[str]:
    key = f"_probe/{run_id}/seq"
    first, second = f"A|{run_id}|seq|{uuid.uuid4().hex}", f"B|{run_id}|seq|{uuid.uuid4().hex}"
    fails: list[str] = []
    if markers.create(key, first) is not True:
        fails.append("sequential: the first create of a fresh key did not return True")
    if markers.create(key, second) is not False:
        fails.append("sequential: the second create of the same key did not return False (the precondition "
                     "was ignored)")
    stored = markers.get(key)
    if stored != first:
        fails.append(f"sequential: the object holds {stored!r} after the second create, not the first writer's "
                     f"{first!r} (a silent overwrite)")
    return fails


def write_rounds(markers: Any, run_id: str, writer: str, rounds: int, start_at: float, interval_s: float) -> dict:
    """One racing writer: for each round wait for its slot on the wall clock, then create the round's key with
    this writer's payload. The record of what it saw goes to the store for `verify`."""
    out: list[dict] = []
    for r in range(rounds):
        slot = start_at + r * interval_s
        while (left := slot - time.time()) > 0:
            time.sleep(min(left, 0.05) if left > 0.05 else 0)
        payload = f"{writer}|{run_id}|{r}|{uuid.uuid4().hex}"
        t0 = time.time()
        try:
            got: Any = markers.create(f"_probe/{run_id}/race-{r}", payload)
        except Exception as exc:  # noqa: BLE001 - an error is a result the verifier must see
            got = f"error: {type(exc).__name__}: {exc}"
        out.append({"round": r, "payload": payload, "created": got, "t0": t0, "t1": time.time(),
                    "late_s": round(t0 - slot, 4)})
    markers.put(f"_probe/{run_id}/result-{writer}", json.dumps({"writer": writer, "rounds": out}))
    return {"writer": writer, "rounds": out}


def verify(markers: Any, run_id: str, rounds: int) -> tuple[list[str], int]:
    """(failures, rounds whose two create calls overlapped in time) from the writers' records and the objects."""
    fails: list[str] = []
    results: dict[str, dict[int, dict]] = {}
    for w in WRITERS:
        raw = markers.get(f"_probe/{run_id}/result-{w}")
        if raw is None:
            fails.append(f"writer {w} left no result record: it did not finish")
            continue
        results[w] = {e["round"]: e for e in json.loads(raw)["rounds"]}
    overlapping = 0
    for r in range(rounds):
        entries = [e for w in WRITERS if w in results and (e := results[w].get(r)) is not None]
        if len(entries) < len(WRITERS):
            fails.append(f"round {r}: a writer has no result for it")
            continue
        got = [e["created"] for e in entries]
        errors = [g for g in got if not isinstance(g, bool)]
        if errors:
            fails.append(f"round {r}: a create raised instead of answering: {errors[0]}")
            continue
        winners = [e for e in entries if e["created"] is True]
        if len(winners) != 1:
            fails.append(f"round {r}: {len(winners)} writers got True (exactly one must win)")
            continue
        stored = markers.get(f"_probe/{run_id}/race-{r}")
        if stored != winners[0]["payload"]:
            fails.append(f"round {r}: the object holds {stored!r}, not the winner's {winners[0]['payload']!r} "
                         "(a silent overwrite)")
        if max(e["t0"] for e in entries) < min(e["t1"] for e in entries):
            overlapping += 1
    return fails, overlapping


@dataclass
class Report:
    passed: bool
    rounds: int
    sequential_ok: bool
    overlapping_rounds: int
    backend: str
    run_id: str
    failures: list[str] = field(default_factory=list)

    def describe(self) -> str:
        head = (f"PROBE {'PASSED' if self.passed else 'FAILED'} on {self.backend}: {self.rounds} race rounds "
                f"({self.overlapping_rounds} with overlapping calls), sequential "
                f"{'ok' if self.sequential_ok else 'FAILED'}")
        return "\n".join([head, *[f"  - {f}" for f in self.failures[:20]]])


def record_pass(markers: Any, *, backend: str, run_id: str, rounds: int, at: float | None = None,
                passed: bool = True) -> None:
    markers.put(PROBE_KEY, json.dumps({"passed": passed, "version": PROBE_VERSION, "backend": backend,
                                       "run_id": run_id, "rounds": rounds,
                                       "at": time.time() if at is None else at}, sort_keys=True))


def conclude(markers: Any, backend: str, run_id: str, rounds: int, *,
             min_overlap: int = MIN_OVERLAPPING_ROUNDS, write_pass: bool = True) -> Report:
    """Judge a run. A FAILURE record is written at once (it revokes an earlier pass); a PASS record only when
    `write_pass` - `run_local` defers it until the writers' exit codes are checked too."""
    seq = sequential_check(markers, run_id)
    fails, overlapping = verify(markers, run_id, rounds)
    if overlapping < min_overlap:
        fails.append(f"only {overlapping} of {rounds} rounds had overlapping create calls, fewer than the "
                     f"{min_overlap} required: the writers did not provably race, so an atomic create is "
                     "unproven (rerun with more rounds or a tighter --lead-s)")
    report = Report(passed=not seq and not fails, rounds=rounds, sequential_ok=not seq,
                    overlapping_rounds=overlapping, backend=backend, run_id=run_id, failures=[*seq, *fails])
    if write_pass or not report.passed:
        record_pass(markers, backend=backend, run_id=run_id, rounds=rounds, passed=report.passed)
    return report


def run_local(spec: str, *, rounds: int = PROBE_ROUNDS, interval_s: float = 0.2, lead_s: float = 5.0,
              run_id: str | None = None) -> Report:
    """Both writers as real processes on this host, released by one wall-clock barrier."""
    run_id = run_id or f"p{int(time.time())}-{uuid.uuid4().hex[:6]}"
    start_at = time.time() + lead_s
    procs = [subprocess.Popen(
        [sys.executable, str(HERE / "marker_probe.py"), "--store", spec, "--role", "writer", "--writer", w,
         "--run-id", run_id, "--rounds", str(rounds), "--start-at", repr(start_at),
         "--interval-s", repr(interval_s)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for w in WRITERS]
    outputs = [p.communicate(timeout=lead_s + rounds * interval_s + 300)[0] for p in procs]
    markers, backend = open_markers(spec)
    report = conclude(markers, backend, run_id, rounds, write_pass=False)
    for w, p, text in zip(WRITERS, procs, outputs, strict=True):
        if p.returncode != 0:
            report.passed = False
            report.failures.insert(0, f"writer {w} exited {p.returncode}: {text.strip()[-300:]}")
    record_pass(markers, backend=backend, run_id=run_id, rounds=rounds, passed=report.passed)
    return report


# --- the launcher's side ----------------------------------------------------------------------------------

def probe_problem(markers: Any, *, expected_backend: str, now: float | None = None) -> str | None:
    """None if a fresh, passing probe record exists for exactly this backend; otherwise why the wave may not
    start. Reads the record through the same marker store the wave will use."""
    raw = markers.get(PROBE_KEY)
    if raw is None:
        return (f"no probe record for {expected_backend}: run marker_probe.py against the wave bucket first "
                "(the cross-host shard-index and wave-count guarantees are unproven until it passes)")
    try:
        rec = json.loads(raw)
        passed, version, backend, at = rec["passed"], rec["version"], rec["backend"], float(rec["at"])
    except (ValueError, KeyError, TypeError):
        return f"the probe record at {PROBE_KEY} is unreadable: refusing to treat the bucket as proven"
    if version != PROBE_VERSION:
        return f"the probe record is version {version}, this launcher needs {PROBE_VERSION}: rerun the probe"
    if backend != expected_backend:
        return (f"the probe record was written against a different backend ({backend!r}) than this wave's "
                f"({expected_backend!r}): it proves nothing about this bucket")
    if passed is not True:
        return f"the last probe of {backend} did not pass: the bucket's conditional create is not atomic"
    age = (now if now is not None else time.time()) - at
    if age > PROBE_MAX_AGE_S:
        return f"the probe record is older than {PROBE_MAX_AGE_S // 86400} days: rerun the probe"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", required=True, help="`gcs:<bucket>` (the wave bucket, ADC), `minio` (MINIO_* env) or localdir:<dir>")
    parser.add_argument("--role", choices=("local", "writer", "verify"), default="local")
    parser.add_argument("--rounds", type=int, default=PROBE_ROUNDS)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--writer", choices=WRITERS)
    parser.add_argument("--start-at", type=float, default=None, help="epoch seconds the first round starts")
    parser.add_argument("--interval-s", type=float, default=0.2)
    parser.add_argument("--lead-s", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    markers, backend = open_markers(args.store)
    if args.role == "writer":
        if not args.writer or not args.run_id or args.start_at is None:
            parser.error("--role writer needs --writer, --run-id and --start-at")
        write_rounds(markers, args.run_id, args.writer, args.rounds, args.start_at, args.interval_s)
        return 0
    if args.role == "verify":
        if not args.run_id:
            parser.error("--role verify needs --run-id")
        report = conclude(markers, backend, args.run_id, args.rounds)
    else:
        report = run_local(args.store, rounds=args.rounds, interval_s=args.interval_s, lead_s=args.lead_s,
                           run_id=args.run_id)
    print(report.describe())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
