#!/usr/bin/env python3
# Responsibility: Gather the re-probe shards, refuse anything not comparable (a missing or failed
#                 shard, mixed commits or images), merge their rows, report cheap/full agreement and
#                 counts, and cut the splits.
# Owns: shard discovery, the comparability gate, the merge, `report`, and the corpora/v4 write.
# Boundaries: no CAD, no mesher. Rows come from `reprobe`; an EXCLUDED entry (infrastructure failure)
#             is reported as such and is never added to the failing, unclean or clean counts.
"""Collect the re-probe.

    collect.py --status gs://bucket/eval/reprobe-<c12>/pool [--shards N]     # per shard: DONE / FAILED / running
    collect.py --pull gs://bucket/eval/reprobe-<c12>/pool --to <dir> --shards N   # pull, verify, merge, splits
    collect.py --pull <uri> --to <dir> --measure --wave-shards 96            # ONE measured shard: re-project
    collect.py --from <dir> [--shards N]            # an already-pulled wave in <dir>/shard-I-of-N/
    collect.py --from <dir> --report-only           # numbers only; write no split

`--shards N` is the planned shard count: a wave with fewer shards present is REFUSED, because merging
a partial wave would give every stratum the wrong size. A shard counts only with a DONE marker and
exit_code 0; a FAILED shard, a mixed commit or image, a file filed under the wrong shard, a file in two
shards, a pool file nobody probed and a row the pool does not list are all REFUSED (exit 1). A short
split or a large exclusion exits 2 and writes no split. `--measure` is the one mode that takes fewer
shards than planned, and it writes nothing but a measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_arm as _ra  # noqa: E402  (loads evalspec, reprobe, pool under their module names)

evalspec = _ra.evalspec
rp = _ra.reprobe_mod
pool_mod = _ra.pool_mod

SHARD_RE = re.compile(r"^shard-(\d+)-of-(\d+)$")


class CollectRefused(RuntimeError):
    pass


def load_shards(root: Path, expected: int | None = None, *, partial_ok: bool = False,
                parse_rows: Callable[[list[dict]], list] | None = None) -> list[dict]:
    """Every shard under `root`, with its provenance and rows. Refuses missing, failed or mixed shards.
    `partial_ok` waives ONLY the missing-shard check (the measured-shard mode); nothing else.
    `parse_rows` is the arm-wave seam (report.py): it turns a shard's rows.json into its own row type,
    and the shard then has no cheap.jsonl; every gate below (exit code, shard count, one commit, one
    image, missing shards) is the same code for both kinds of wave."""
    shards: list[dict] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and SHARD_RE.match(p.name)):
        i, n = (int(x) for x in SHARD_RE.match(d.name).groups())  # type: ignore[union-attr]
        prov_path = d / "PROVENANCE.json"
        if not prov_path.is_file():
            raise CollectRefused(f"{d.name}: no PROVENANCE.json (shard unfinished)")
        prov = json.loads(prov_path.read_text())
        if prov.get("exit_code") != 0:
            raise CollectRefused(f"{d.name}: exit_code {prov.get('exit_code')!r}: {str(prov.get('error', ''))[-200:]}")
        raw = json.loads((d / "rows.json").read_text())
        shards.append({"i": i, "n": n, "prov": prov, "dir": d,
                       "rows": parse_rows(raw) if parse_rows is not None else [rp.ProbeRow(**r) for r in raw],
                       "cheap": [] if parse_rows is not None else rp.read_rows(d / "cheap.jsonl"),
                       "excluded": json.loads((d / "EXCLUDED.json").read_text())})
    if not shards:
        raise CollectRefused(f"no shard-I-of-N directories under {root}")
    counts = {s["n"] for s in shards}
    if len(counts) != 1:
        raise CollectRefused(f"shards disagree on the shard count: {sorted(counts)}")
    n = counts.pop()
    if expected is not None and expected != n:
        raise CollectRefused(f"the plan has {expected} shards but the shards say {n}")
    missing = sorted(set(range(1, n + 1)) - {s["i"] for s in shards})
    if missing and not partial_ok:
        raise CollectRefused(f"missing shard(s) {missing[:10]} of {n}: a partial wave would mis-size every stratum")
    for key in ("commit", "image"):
        values = {str(s["prov"].get(key) or "") for s in shards}
        if len(values) != 1 or "" in values:
            raise CollectRefused(f"shards do not share one {key}: {sorted(values)}")
    return shards


def merge(shards: list[dict]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    cheap: dict[str, Any] = {}
    excluded: list[dict] = []
    for s in shards:
        rows.update({r.sha256: r for r in s["rows"]})
        cheap.update({r.sha256: r for r in s["cheap"]})
        excluded += s["excluded"]
    final = [r for r in rows.values() if rp.is_counted(r)]  # an uncounted row never reaches a count
    full = [r for r in final if r.verdict_summary.get("tier") == "full"]
    cheap_failed = {sha for sha, r in cheap.items() if r.first_failing_layer is not None}
    return {"rows": final, "cheap": list(cheap.values()), "full": full, "excluded": excluded,
            "cheap_failed": cheap_failed}


def report(merged: dict[str, Any], splits: Any | None) -> dict:
    rows = merged["rows"]
    failing = [r for r in rows if r.first_failing_layer is not None]
    no_solids = [r for r in failing if rp.is_no_solids(r)]
    return {
        "probed_conclusive": len(rows),
        "excluded_inconclusive": len(merged["excluded"]),
        "excluded_fraction": excluded_fraction(merged),
        "excluded_by_source": dict(Counter(x["source"] for x in merged["excluded"])),
        "excluded_by_reason": dict(Counter(x["reason"].split(":")[0] for x in merged["excluded"])),
        "failing": len(failing) - len(no_solids),
        "no_solids_not_repairable": len(no_solids),
        "by_layer": dict(Counter(str(r.first_failing_layer) for r in rows)),
        "by_source": dict(Counter(r.source for r in rows)),
        "failing_by_stratum": dict(Counter("/".join(rp._stratum(r)) for r in failing if not rp.is_no_solids(r))),
        "agreement": rp.agreement(merged["cheap"], merged["full"]),
        "splits": None if splits is None else {
            "dev": len(splits.dev), "heldout": len(splits.heldout), "clean": len(splits.clean),
            "shortfall": splits.shortfall, "strata": splits.strata},
    }


#: Above this share of the pool excluded, the wave is refused: a timeout excludes the most pathological
#: files, so a large exclusion silently biases the failing set toward easy defects.
MAX_EXCLUDED_FRACTION = 0.02


def excluded_fraction(merged: dict[str, Any]) -> float:
    total = len(merged["rows"]) + len(merged["excluded"])
    return len(merged["excluded"]) / total if total else 0.0


# ------------------------------------------------------------------ the pull

#: What collect reads from a shard. rows.jsonl, logs and transcripts stay in the bucket.
REQUIRED_FILES = ("PROVENANCE.json", "rows.json", "cheap.jsonl", "EXCLUDED.json")


class Transport(Protocol):
    """The only seam to the bucket. Reads only: nothing here can write, move or delete a remote object."""

    def ls(self, uri: str) -> list[str]: ...

    def fetch(self, uri: str, dest: Path) -> None: ...


class GcloudTransport:
    """`gcloud storage ls` and `gcloud storage cp <remote> <local>`; never a mutating verb. `run` is the
    subprocess seam; unit tests inject a fake, and none reaches gcloud."""

    def __init__(self, run: Callable[..., Any] | None = None) -> None:
        self._run = run

    def _call(self, cmd: list[str]) -> Any:
        proc = (self._run or subprocess.run)(cmd, capture_output=True, text=True)
        return proc

    def ls(self, uri: str) -> list[str]:
        cmd = ["gcloud", "storage", "ls", "--recursive", uri.rstrip("/") + "/"]
        proc = self._call(cmd)
        if proc.returncode != 0:
            if "matched no objects" in (proc.stderr or ""):
                return []
            raise CollectRefused(f"{' '.join(cmd)} failed: {(proc.stderr or '').strip()[-300:]}")
        return [line.strip() for line in proc.stdout.splitlines()
                if line.startswith("gs://") and not line.rstrip().endswith((":", "/"))]

    def fetch(self, uri: str, dest: Path) -> None:
        cmd = ["gcloud", "storage", "cp", uri, str(dest)]
        proc = self._call(cmd)
        if proc.returncode != 0:
            raise CollectRefused(f"{' '.join(cmd)} failed: {(proc.stderr or '').strip()[-300:]}")


def survey(src: str, transport: Transport) -> dict[str, set[str]]:
    """{shard dir name: set of file names} for every shard-I-of-N directory under `src`."""
    prefix = src.rstrip("/") + "/"
    found: dict[str, set[str]] = {}
    for uri in transport.ls(src):
        if not uri.startswith(prefix):
            continue
        parts = uri[len(prefix):].split("/")
        if len(parts) == 2 and SHARD_RE.match(parts[0]):
            found.setdefault(parts[0], set()).add(parts[1])
    return found


def shard_state(files: set[str]) -> str:
    if "FAILED" in files:
        return "FAILED"
    return "DONE" if "DONE" in files else "running"


def check_pullable(src: str, found: dict[str, set[str]], dest: Path) -> None:
    """Every reason not to download, decided from the listing alone, before a byte is fetched."""
    if not found:
        raise CollectRefused(f"no shard-I-of-N directories under {src}")
    if dest.is_dir():
        stale = sorted(p.name for p in dest.iterdir() if p.is_dir() and SHARD_RE.match(p.name) and p.name not in found)
        if stale:
            raise CollectRefused(f"{dest} holds shard(s) {stale} that are not in {src}: a stale partial pull; "
                                 "use an empty --to")
    for name in sorted(found):
        files = found[name]
        if "FAILED" in files:
            raise CollectRefused(f"{name}: FAILED marker in the bucket; rerun the shard (see the runbook)")
        if "DONE" not in files:
            raise CollectRefused(f"{name}: no DONE marker (shard unfinished or still uploading)")
        absent = [f for f in REQUIRED_FILES if f not in files]
        if absent:
            raise CollectRefused(f"{name}: DONE but missing {absent}")


def pull(src: str, dest: Path, transport: Transport, *, dry_run: bool = False) -> list[str]:
    """Fetch each finished shard's REQUIRED_FILES into dest/<shard>/. Re-running overwrites (a rerun
    shard replaces its old copy); each file lands via a .partial sibling, so a killed pull leaves no
    truncated file at a final path."""
    found = survey(src, transport)
    check_pullable(src, found, dest)
    plan = [(f"{src.rstrip('/')}/{name}/{f}", dest / name / f) for name in sorted(found)
            for f in (*REQUIRED_FILES, "DONE")]
    if dry_run:
        for uri, path in plan:
            print(f"would fetch {uri} -> {path}")
        return sorted(found)
    for uri, path in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        transport.fetch(uri, partial)
        os.replace(partial, path)
    return sorted(found)


def status(src: str, transport: Transport, planned: int | None) -> int:
    found = survey(src, transport)
    sizes = {int(SHARD_RE.match(n).group(2)) for n in found}  # type: ignore[union-attr]
    if planned is None and len(sizes) == 1:
        planned = sizes.pop()
    if planned is None:
        print(f"cannot tell the planned shard count from {sorted(found)}; pass --shards", file=sys.stderr)
        return 1
    bad = 0
    for i in range(1, planned + 1):
        name = f"shard-{i}-of-{planned}"
        state = shard_state(found[name]) if name in found else "NOT STARTED"
        bad += state != "DONE"
        print(f"{name} {state}")
    return 1 if bad else 0


# ------------------------------------------------------------------ completeness and measurement

def _shas(shard: dict) -> set[str]:
    return {r.sha256 for r in shard["rows"]} | {x["sha256"] for x in shard["excluded"]}


def check_partition(shards: list[dict]) -> None:
    """Every file sits in the shard its hash assigns, and in only one. A violation means the shards were
    cut from different plans or a shard was run twice with different inputs."""
    seen: dict[str, str] = {}
    for s in shards:
        for sha in sorted(_shas(s)):
            if not evalspec.shard_of(sha, s["i"], s["n"]):
                raise CollectRefused(f"{s['dir'].name} holds {sha[:12]}, which belongs to another shard "
                                     "(partition mismatch: the shards were not cut from one plan)")
            if sha in seen:
                raise CollectRefused(f"{sha[:12]} is in {seen[sha]} and {s['dir'].name} (partition mismatch)")
            seen[sha] = s["dir"].name


def check_against_pool(shards: list[dict], pool_path: Path) -> int:
    """Every pool entry the benchmark may measure is accounted for (a row or an EXCLUDED entry), and
    nothing else is. Returns the allowed-pool size."""
    allowed, _dropped = rp.split_allowed(pool_mod.read_pool(pool_path))
    expected = {e.sha256 for e in allowed}
    seen = set().union(*(_shas(s) for s in shards))
    unprobed = sorted(expected - seen)
    if unprobed:
        raise CollectRefused(f"{len(unprobed)} pool file(s) were never probed (first: {unprobed[0][:12]}): "
                             "the wave is incomplete; rerun the shard that owns them")
    extra = sorted(seen - expected)
    if extra:
        raise CollectRefused(f"{len(extra)} row(s) are not in the pool (first: {extra[0][:12]}): "
                             "the shards ran against a different pool or sources")
    return len(expected)


def measure(shards: list[dict]) -> dict[str, Any]:
    """What the shard(s) actually cost, against the sizing estimates. One shard is one vCPU
    (`docker --cpus=1`), so wall minutes are vCPU-minutes. Cheap and full minutes are not separable from
    one wall time, so both estimates are scaled by one factor: the full:cheap ratio stays at the
    estimate's. The re-projection that uses it is only as good as the shard sampled."""
    files = full = 0
    wall_min = 0.0
    failing = cheap_n = 0
    for s in shards:
        p = s["prov"]
        files += int(p.get("shard_files") or 0)
        full += int(p.get("full_rows") or 0)
        wall_min += float(p.get("wall_seconds") or 0.0) / 60.0
        for r in s.get("cheap") or []:
            cheap_n += 1
            failing += r.first_failing_layer is not None
    est = files * evalspec.CHEAP_MIN_PER_FILE + full * evalspec.FULL_MIN_PER_FILE
    if files <= 0 or est <= 0 or wall_min <= 0:
        raise CollectRefused("the shard(s) carry no file count or wall time: nothing to measure")
    scale = wall_min / est
    return {"shards": len(shards), "files": files, "full_files": full, "wall_minutes": round(wall_min, 2),
            "wall_min_per_file": wall_min / files, "scale_vs_estimate": scale,
            "cheap_min_per_file": evalspec.CHEAP_MIN_PER_FILE * scale,
            "full_min_per_file": evalspec.FULL_MIN_PER_FILE * scale,
            "cheap_fail_fraction": failing / cheap_n if cheap_n else None}


def reproject(m: dict[str, Any], *, files: int, shards: int, machine_type: str, max_run_hours: float) -> Any:
    kw: dict[str, Any] = {}
    if m["cheap_fail_fraction"] is not None:
        kw["cheap_fail_fraction"] = m["cheap_fail_fraction"]
    return evalspec.project(files, shards=shards, machine_type=machine_type, max_run_hours=max_run_hours,
                            cheap_min=m["cheap_min_per_file"], full_min=m["full_min_per_file"], **kw)


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="src", help="directory holding shard-I-of-N/")
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--dev", type=int, default=300)
    parser.add_argument("--heldout", type=int, default=200)
    parser.add_argument("--clean", type=int, default=150)
    parser.add_argument("--allow-shortfall", action="store_true", help="write splits that are short")
    parser.add_argument("--allow-excluded", action="store_true",
                        help=f"proceed although more than {MAX_EXCLUDED_FRACTION:.0%} of the pool was excluded")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--out-dir", default=str(rp.SPLITS_DIR))
    parser.add_argument("--pull", default="", help="gs://bucket/eval/reprobe-<c12>: fetch, then collect")
    parser.add_argument("--to", default="", help="local directory the --pull fills (required with --pull)")
    parser.add_argument("--status", default="", help="gs://... : per-shard DONE / FAILED / running; no fetch")
    parser.add_argument("--pool", default=str(pool_mod.OUT), help="the pool manifest every file is checked against")
    parser.add_argument("--no-pool-check", action="store_true",
                        help="skip the pool completeness check (the wave may be missing files)")
    parser.add_argument("--measure", action="store_true",
                        help="the measured-shard mode: accept fewer shards than planned, print the "
                             "measured cost and a re-projection, write nothing else")
    parser.add_argument("--wave-shards", type=int, default=96, help="--measure: shard count of the full wave")
    parser.add_argument("--max-run-hours", type=float, default=8.0, help="--measure: the VM cap to re-check")
    parser.add_argument("--machine-type", default=evalspec.DEFAULT_MACHINE)
    parser.add_argument("--dry-run", action="store_true", help="with --pull: list and check, fetch nothing")
    args = parser.parse_args(argv)
    xport = transport or GcloudTransport()

    try:
        if args.status:
            return status(args.status, xport, args.shards)
        if args.pull:
            if not args.to:
                parser.error("--pull needs --to <dir>")
            pull(args.pull, Path(args.to), xport, dry_run=args.dry_run)
            if args.dry_run:
                return 0
            args.src = args.to
        if not args.src:
            parser.error("--from (or --pull/--status) is required")
        if args.measure:
            return run_measure(args)
        shards = load_shards(Path(args.src), args.shards)
        check_partition(shards)
        if args.no_pool_check:
            print("WARN    --no-pool-check: nothing proves the wave holds every pool file", file=sys.stderr)
        else:
            check_against_pool(shards, Path(args.pool))
        merged = merge(shards)
    except CollectRefused as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    frac = excluded_fraction(merged)
    problems: list[str] = []
    if frac > MAX_EXCLUDED_FRACTION and not args.allow_excluded:
        problems.append(f"{frac:.1%} of the pool was excluded (limit {MAX_EXCLUDED_FRACTION:.0%}): the failing "
                        "set is biased toward files the checker could finish; rerun the excluded shards")
    splits = None
    if not args.report_only:
        try:
            splits = rp.build_splits(merged["rows"], dev=args.dev, heldout=args.heldout, clean=args.clean,
                                     cheap_failed=merged["cheap_failed"])
        except ValueError as exc:
            print(f"REFUSED cannot cut the splits: {exc}", file=sys.stderr)
            return 2
    if splits is not None and splits.shortfall and not args.allow_shortfall:
        problems.append(f"splits are short: {splits.shortfall}")
    rep = report(merged, splits)
    print(json.dumps(rep, indent=1, sort_keys=True))
    write_report(Path(args.src), shards, rep, source=args.pull or str(args.src), accepted=not problems,
                 problems=problems)
    if problems:
        for text in problems:
            print(f"REFUSED {text}; no split was written", file=sys.stderr)
        return 2
    if splits is not None:
        rp.write_splits(splits, args.out_dir)
    return 0


def write_report(root: Path, shards: list[dict], rep: dict, *, source: str, accepted: bool,
                 problems: list[str]) -> None:
    """collect-report.json beside the shards: the numbers the report is written from, and what produced
    them. `accepted` is false when collect refused to cut the splits."""
    prov = shards[0]["prov"]
    doc = {"source": source, "commit": prov.get("commit"), "image": prov.get("image"),
           "shards": shards[0]["n"], "machine_type": prov.get("machine_type"),
           "wall_seconds_total": round(sum(float(s["prov"].get("wall_seconds") or 0) for s in shards), 1),
           "accepted": accepted, "problems": problems, "report": rep}
    (root / "collect-report.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


def run_measure(args: argparse.Namespace) -> int:
    shards = load_shards(Path(args.src), args.shards, partial_ok=True)
    m = measure(shards)
    allowed, _ = rp.split_allowed(pool_mod.read_pool(Path(args.pool)))
    machine = str(shards[0]["prov"].get("machine_type") or args.machine_type)
    p = reproject(m, files=len(allowed), shards=args.wave_shards, machine_type=machine,
                  max_run_hours=args.max_run_hours)
    print(json.dumps(m, indent=1, sort_keys=True))
    print(f"measured {m['wall_min_per_file']:.2f} vCPU min/file overall: cheap {m['cheap_min_per_file']:.2f}, "
          f"full {m['full_min_per_file']:.2f} min/file ({m['scale_vs_estimate']:.2f}x the estimate of "
          f"{evalspec.CHEAP_MIN_PER_FILE:g}/{evalspec.FULL_MIN_PER_FILE:g})")
    print(p.describe())
    reasons = evalspec.check_budget(p)
    for r in reasons:
        print(f"REFUSE  {r}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
