#!/usr/bin/env python3
# Responsibility: Regenerate the committed engine fitness table from the lab's run results, with provenance.
# Boundaries: offline tooling; it reads lab result files and geometry, writes one JSON file, and changes no product code.
"""BUILD THE ENGINE FITNESS TABLE (src/meshpipeline/engines/fitness_table.json).

Every lab run (the overnight lab: production mesh image, the product's own path, one engine pinned)
whose code is part of the code this table ships with becomes one row: the run's outcome, and the
SHAPE TRAITS of its geometry measured by cad/shape_traits.py - the same measurement a user's upload
gets. engines/fitness.py reads the rows; nothing here decides a ranking.

Which runs count (recorded in the table's provenance):
* code: the run's commit is a descendant of --since and an ancestor of --base (default HEAD), so
  the table describes the code it ships with, not a branch that never landed;
* outcome: pass / fail / timeout / crash count; `refused` does not (the capability filter already
  keeps an engine away from a file it refuses) and neither does a crash of the lab runner itself
  (ImportError / NameError: the runner and the code disagreed);
* one row per (case, engine, format): the newest result wins;
* --recount ENGINE:FACT=VALUE[,FACT=VALUE]:REASON turns that engine's passes on cases with those
  facts into failures, for a defect found AFTER the runs that every gate missed (a mesh of the
  wrong region). Facts: input_kind, form, annulus (a declared opening with an inner diameter).
  Every recount is written into the table's provenance with its reason and count.

Usage (from the repository root, in the venv that has OCP and pyvista):
    PYTHONPATH=src python devtools/fitness/build_engine_fitness.py \\
        --out /home/areen/overnight/lab/out --since a4595577 \\
        --cases /home/areen/overnight/lab/testset/cases --cases /home/areen/overnight/recommend/cases \\
        --cases /home/areen/overnight/vmtk-tubular/cases --root /home/areen/overnight/lab/testset
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "src" / "meshpipeline" / "engines" / "fitness_table.json"
#: crashes of the lab runner, not of the product
_RUNNER_CRASHES = ("crash:ImportError", "crash:NameError")
_FORMAT_SUFFIX = r"(step|stp|iges|igs|stl|stl_ascii|ascii|brep|obj|ply|off|vtp|vtu|glb|3mf|msh|bdf|inp)"
_UNIT_TO_MM = {"mm": 1.0, "m": 1000.0, "cm": 10.0, "in": 25.4, "inch": 25.4, "ft": 304.8, "um": 1e-3}


def shape_id(case_id: str) -> str:
    """The GEOMETRY a case is a variant of, for counting distinct shapes and for holding a shape
    out: the workstreams' prefixes, the file format and the fluid/wall/assembly twin suffixes
    removed. Bookkeeping only - the ranking never reads a name."""
    s = case_id.lower()
    s = re.sub(r"^(rc|vt|mx|stl|ht\d*|smoke|turf)_", "", s)
    for _ in range(3):
        s = re.sub(rf"_{_FORMAT_SUFFIX}$", "", s)
        s = re.sub(r"_(body|fluid|capped|open|metal|cht|solid|wall|offset_\d+mm)$", "", s)
    return s


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()


_ANC: dict[tuple[str, str], bool] = {}


def is_ancestor(a: str, b: str) -> bool:
    key = (a, b)
    if key not in _ANC:
        _ANC[key] = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", a, b],
                                   capture_output=True).returncode == 0
    return _ANC[key]


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cells_across(result: dict) -> float | None:
    q = (result.get("manifest") or {}).get("quality")
    if isinstance(q, dict):
        v = (q.get("passage_cells_across_local") or {}).get("p05")
        return _num(v)
    if isinstance(q, str):
        m = re.search(r'"passage_cells_across_local":\s*\{[^}]*"p05":\s*([0-9.]+)', q)
        if m:
            return float(m.group(1))
    return None


def _layers_pct(result: dict) -> float | None:
    lay = result.get("layers") or {}
    for k in ("layer_coverage_pct", "layer_coverage"):
        v = _num(lay.get(k))
        if v is not None:
            return v
    return None


def _status(result: dict) -> str | None:
    s = str(result.get("status") or "")
    if s == "pass":
        return "pass"
    if s in ("fail", "timeout"):
        return "fail"
    if s == "error":
        return None if str(result.get("failure_class") or "").startswith(_RUNNER_CRASHES) else "fail"
    return None          # refused: the capability filter's business, not fitness


def _rule(text: str) -> tuple[str, dict[str, str], str]:
    engine, _, rest = text.partition(":")
    cond, _, reason = rest.partition(":")
    want = dict(c.split("=", 1) for c in cond.split(",") if "=" in c)
    if not engine or not want or not reason:
        raise SystemExit(f"--recount {text!r}: want ENGINE:FACT=VALUE[,FACT=VALUE]:REASON")
    return engine, want, reason


def _case_facts(case: dict, result: dict, traits: dict) -> dict[str, str]:
    annulus = any(isinstance(p, dict) and (p.get("inner_diameter_mm") or p.get("inner_diameter"))
                  for p in (case.get("patches") or []) + (case.get("ports") or []))
    return {"input_kind": str(result.get("input_kind") or case.get("input_kind") or ""),
            "form": str(traits.get("form") or ""), "annulus": "1" if annulus else "0",
            "closed": "1" if traits.get("closed") else "0"}


def _find_case(case_id: str, dirs: list[Path]) -> dict | None:
    for d in dirs:
        p = d / f"{case_id}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return None
    return None


def _input_path(result: dict, roots: list[Path]) -> Path | None:
    f = str(result.get("input_file") or "")
    if not f:
        return None
    p = Path(f)
    if p.is_absolute():
        return p if p.exists() else None
    for r in roots:
        if (r / p).exists():
            return r / p
    return None


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _measure(job: dict) -> tuple[str, dict]:
    from meshpipeline.cad.shape_traits import measure_file
    t = measure_file(Path(job["path"]), flow=job["flow"], input_kind=job["input_kind"],
                     patches=job["patches"], surface_unit_to_m=job["unit_to_m"],
                     flow_axis=job["flow_axis"])
    return job["key"], t.as_dict()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="the lab's out/ directory")
    ap.add_argument("--cases", action="append", default=[], help="a directory of case JSONs (repeatable)")
    ap.add_argument("--root", action="append", default=[], help="a root relative input files resolve against")
    ap.add_argument("--since", required=True, help="oldest commit whose runs count")
    ap.add_argument("--base", default="HEAD", help="the code the table ships with (default HEAD)")
    ap.add_argument("--prefix", action="append", default=[], help="only result tags matching this glob")
    ap.add_argument("--cache", default="", help="traits cache file (JSON) to reuse measurements")
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--recount", action="append", default=[],
                    help="ENGINE:FACT=VALUE[,FACT=VALUE]:REASON - passes that delivered the wrong mesh")
    args = ap.parse_args(argv)

    from meshpipeline.cad.shape_traits import TRAITS_VERSION
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.fitness import brief_facts

    base = _git("rev-parse", args.base)
    since = _git("rev-parse", args.since)
    case_dirs = [Path(d) for d in args.cases]
    roots = [Path(r) for r in args.root] + case_dirs
    excluded: Counter = Counter()
    latest: dict[tuple, tuple[float, dict, Path]] = {}
    for f in sorted(Path(args.out).glob("*.json")):
        if f.name.endswith(".summary.jsonl"):
            continue
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or "case_id" not in r or "engine" not in r:
            continue
        tag = str(r.get("tag") or f.stem)
        if args.prefix and not any(fnmatch.fnmatch(tag, p) for p in args.prefix):
            excluded["not in --prefix"] += 1
            continue
        commit = str((r.get("git") or {}).get("commit") or "")
        if not commit or not (is_ancestor(since, commit) and is_ancestor(commit, base)):
            excluded["code not in since..base"] += 1
            continue
        status = _status(r)
        if status is None:
            excluded[f"status {r.get('status')}"] += 1
            continue
        key = (r["case_id"], r["engine"], r.get("format") or "")
        mtime = f.stat().st_mtime
        if key not in latest or mtime > latest[key][0]:
            if key in latest:
                excluded["older duplicate"] += 1
            latest[key] = (mtime, r, f)
        else:
            excluded["older duplicate"] += 1

    cache: dict = {}
    if args.cache and Path(args.cache).exists():
        cache = json.loads(Path(args.cache).read_text())
    rows, jobs, pending = [], {}, []
    for (case_id, engine, fmt), (_, r, f) in sorted(latest.items()):
        case = _find_case(case_id, case_dirs)
        path = _input_path(r, roots)
        if case is None or path is None:
            excluded["case or geometry file not found"] += 1
            continue
        flow = flow_of(r.get("purpose") or case.get("purpose") or "")
        unit = str(r.get("unit") or case.get("unit") or "mm").lower()
        unit_to_m = _UNIT_TO_MM.get(unit, 1.0) / 1000.0
        patches = [p for p in (case.get("patches") or []) if isinstance(p, dict)]
        input_kind = str(r.get("input_kind") or case.get("input_kind") or "")
        flow_axis = str(case.get("flow_axis") or "")
        tkey = "|".join([_sha1(path), flow, input_kind, json.dumps(patches, sort_keys=True),
                         str(unit_to_m), flow_axis, str(TRAITS_VERSION)])
        if tkey not in cache and tkey not in jobs:
            jobs[tkey] = {"key": tkey, "path": str(path), "flow": flow, "input_kind": input_kind,
                          "patches": patches, "unit_to_m": unit_to_m, "flow_axis": flow_axis}
        pending.append((tkey, case_id, engine, fmt, r, f, case))

    if jobs:
        print(f"measuring {len(jobs)} geometries ...", file=sys.stderr)
        with cf.ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for i, (k, traits) in enumerate(pool.map(_measure, jobs.values()), start=1):
                cache[k] = traits
                if i % 25 == 0:
                    print(f"  {i}/{len(jobs)}", file=sys.stderr)
        if args.cache:
            Path(args.cache).write_text(json.dumps(cache))

    rules = [_rule(x) for x in args.recount]
    recounted: Counter = Counter()
    commits: Counter = Counter()
    prefixes: Counter = Counter()
    for tkey, case_id, engine, fmt, r, f, case in pending:
        traits = cache[tkey]
        if not traits.get("n_triangles"):
            excluded["geometry could not be measured"] += 1
            continue
        tag = str(r.get("tag") or f.stem)
        commit = str((r.get("git") or {}).get("commit") or "")
        commits[commit[:8]] += 1
        prefixes[tag.split("-", 1)[0]] += 1
        status, failure = _status(r), str(r.get("failure_class") or "")
        facts = _case_facts(case, r, traits)
        for eng, want, reason in rules:
            if status == "pass" and engine == eng and all(facts.get(k) == v for k, v in want.items()):
                status, failure = "fail", f"recounted:{reason}"
                recounted[f"{eng}: {reason}"] += 1
        rows.append({
            "shape": shape_id(case_id), "case": case_id, "family": case.get("family") or "",
            "engine": engine, "format": fmt, "form": traits.get("form") or "",
            "status": status, "failure_class": failure,
            "cells": r.get("cells"), "seconds": _num((r.get("timings") or {}).get("total")),
            "layers_pct": _layers_pct(r), "cells_across_p05": _cells_across(r),
            "quality": r.get("quality") or {}, "tag": tag, "commit": commit[:8],
            # what the case's own brief says (layers asked for, ground, budget): the held-out check
            # recommends with it, as the product does with the user's brief
            "brief": brief_facts(request_txt=str(case.get("request_txt") or ""),
                                 patches=[p for p in (case.get("patches") or []) if isinstance(p, dict)],
                                 cell_budget=case.get("max_cells")),
            "traits": traits})

    from meshpipeline.cad.shape_traits import ShapeTraits
    from meshpipeline.engines.fitness import shape_class, table_from
    summary: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in rows:
        cls = shape_class(ShapeTraits.from_dict(row["traits"])) or "(unplaced)"
        flow = row["traits"].get("flow") or ""
        cell = summary[f"{flow} / {cls} / {row['form']}"][row["engine"]]
        cell[0] += row["status"] == "pass"
        cell[1] += 1
    table = {
        "version": f"{dt.date.today().isoformat()}-{base[:8]}",
        "traits_version": TRAITS_VERSION,
        "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "devtools/fitness/build_engine_fitness.py " + " ".join(argv or sys.argv[1:]),
        "provenance": {
            "base_commit": base, "since_commit": since,
            "rule": "runs whose commit descends from since_commit and is an ancestor of base_commit; "
                    "newest result per (case, engine, format); refusals and lab-runner crashes left out",
            "runs": len(rows), "shapes": len({r["shape"] for r in rows}),
            "cases": len({r["case"] for r in rows}),
            "commits": dict(sorted(commits.items())), "tag_prefixes": dict(sorted(prefixes.items())),
            "excluded": dict(sorted(excluded.items())),
            "recounted": dict(sorted(recounted.items())),
        },
        "summary": {k: {e: f"{p}/{n}" for e, (p, n) in sorted(v.items())}
                    for k, v in sorted(summary.items())},
        "rows": rows,
    }
    Path(args.table).write_text(json.dumps(table, indent=1, sort_keys=False) + "\n")
    t = table_from(table)
    print(f"wrote {args.table}: {len(rows)} runs on {table['provenance']['shapes']} shapes "
          f"({len(t.runs)} readable); excluded {dict(excluded)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
