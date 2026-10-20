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
* NO HAND RELABELS: a run counts as it came out. A defect found after the runs is fixed by
  re-running the case on the fixed code, never by rewriting its outcome (the old --recount, and any
  row an earlier table relabelled `recounted:`, are gone).
* a run whose mesh came out INVALID (checkMesh's fatal defects: negative-volume cells, faces
  pointing the wrong way) is marked `invalid` - engines/fitness.py reads it before speed.

THE END-TO-END SWEEPS (--e2e, repeatable: a sweep's results/ directory) add the runs the corpus
loop made through the whole product path - the check, the intake's defaults, the engine the
recommendation picked, the mesh and the reviewer's deterministic evidence - on public CFD CAD the
lab never held (detailed cars, high-lift aircraft, turbines). Each e2e result carries its own
measured traits (the recommendation's measurement of that upload), so nothing is measured again.
They count when their commit is an ancestor of --e2e-base (default HEAD - the lab's --base can be
a lineage commit that main's squash merges never descend from); a delivered mesh (pass, or delivered with concerns) is a pass, a
mesh that failed or ran out of time in the mesh stage is a failure, and everything that stopped
before an engine ran (refused, the check, the intake) or on our side (a harness error, a workspace
that could not be extracted) is left out. A lab run of the same shape, engine and format is
superseded by the e2e run: it is the newer, and the product's own path. Only CLEAN (gate tier)
ORIGINAL files count (--e2e-index): a file not verified clean fails for its own reasons as often
as for the engine's, and a format copy of a shape is the same geometry as its original.

DEV SIDE ONLY (--split, required). The frozen held-out test set (30% of geometry families, kept
aside so its pass rate means "files we never tuned on") must never teach the engine choice. Every
row is matched to the frozen split by the sha256 of its input file's bytes - a renamed copy is
still its file - and only DEV rows are written. A held-out row is dropped and only COUNTED (never
named); so is a row whose file is not in the split at all (a file nobody gave a side is not known
to be dev). Each kept row carries `input_sha256` and `split: "dev"`, and the digests of every
held-out file go to devtools/fitness/held_out.sha256, so a test fails the moment a held-out run
reaches the committed table.

ONLY RUNS WHERE A MESHER RAN. A run that stopped before any engine meshed (`no_mesh_run:*`,
`geometry_refused:*`, a refusal, the check, the intake, our own harness) says nothing about an
engine, whatever its stage label says.

THE TABLE IS VERSIONED. The header carries `rules_version` (these row rules), `rows_sha256` (a
digest of the rows as written) and the split it was cut from. docs/engines/fitness-changelog.md
must have an entry for the table's version and digest, so a rebuilt table only lands through a
reviewed PR that says what changed.

Usage (from the repository root, in the venv that has OCP and pyvista):
    PYTHONPATH=src python devtools/fitness/build_engine_fitness.py \\
        --out /home/areen/overnight/lab/out --since a4595577 \\
        --cases /home/areen/overnight/lab/testset/cases --cases /home/areen/overnight/recommend/cases \\
        --cases /home/areen/overnight/vmtk-tubular/cases --root /home/areen/overnight/lab/testset \\
        --e2e /home/areen/overnight/e2e/sweeps/baseline-main/results \\
        --e2e-index /home/areen/overnight/e2e/index.jsonl \\
        --split /home/areen/overnight/e2e/holdout_files.json

To add e2e sweeps to the lab record a table already ships with (the lab's out/ has moved on, and
re-reading it would change rows the build is not about):
    PYTHONPATH=src python devtools/fitness/build_engine_fitness.py \\
        --lab-rows-from src/meshpipeline/engines/fitness_table.json \\
        --lab-results /home/areen/overnight/lab/out --root /home/areen/overnight/lab/testset \\
        --cases /home/areen/overnight/lab/testset/cases ... \\
        --e2e /home/areen/overnight/e2e/sweeps/baseline-main/results \\
        --e2e-index /home/areen/overnight/e2e/index.jsonl \\
        --split /home/areen/overnight/e2e/holdout_files.json
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
#: The digests of every held-out file of the frozen split the table was cut from (one sha256 of the
#: file's bytes per line). Written by every build; read by the table's tests.
HELD_OUT_DIGESTS = REPO / "devtools" / "fitness" / "held_out.sha256"
#: The fitness changelog: one entry per committed table version.
CHANGELOG = REPO / "docs" / "engines" / "fitness-changelog.md"
#: THE ROW RULES' version. Bump it when what may become a row changes (not when the rows do).
#:   2: dev side of the frozen split only; only runs where a mesher ran; no hand relabels.
RULES_VERSION = 2
#: Failure classes of runs where no mesher ran - never evidence about an engine.
NEVER_MESHED = ("no_mesh_run", "geometry_refused")


def shape_id(case_id: str) -> str:
    """The GEOMETRY a case is a variant of, for counting distinct shapes and for holding a shape
    out: the workstreams' prefixes, the file format and the fluid/wall/assembly twin suffixes
    removed. Bookkeeping only - the ranking never reads a name."""
    s = case_id.lower()
    # an e2e case keeps its file's name ("drivaer_1.stl.stl", "...config.stp")
    s = re.sub(rf"(\.{_FORMAT_SUFFIX})+$", "", s)
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


_FATAL_IN_REASON = re.compile(r"fatal=\[([^\]]*)\]")


def _fatal(result: dict, reason: str = "") -> list[str]:
    """The fatal checkMesh defects of a run's mesh: the quality report's own list, else the ones
    the engine's verdict names ("[CFMESH] polyMesh cells=... fatal=['negative-volume cells']")."""
    q = result.get("quality")
    listed = q.get("fatal") if isinstance(q, dict) else None
    if isinstance(listed, list) and listed:
        return [str(x) for x in listed]
    m = _FATAL_IN_REASON.search(reason or str(result.get("reason") or ""))
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


def never_meshed(failure_class: str) -> bool:
    """Whether a run's failure class says no mesher ran (it stopped before, or was refused)."""
    return str(failure_class or "").split(":", 1)[0] in NEVER_MESHED


def _status(result: dict) -> str | None:
    s = str(result.get("status") or "")
    if never_meshed(str(result.get("failure_class") or "")):
        return None
    if s == "pass":
        return "pass"
    if s in ("fail", "timeout"):
        return "fail"
    if s == "error":
        return None if str(result.get("failure_class") or "").startswith(_RUNNER_CRASHES) else "fail"
    return None          # refused: the capability filter's business, not fitness


#: e2e failures that are our side, not the engine's: never evidence about an engine
_E2E_INFRASTRUCTURE = ("workspaceextractionerror", "harness:", "timeout:wallclock", "crash:")


def e2e_status(result: dict) -> tuple[str | None, str]:
    """(pass | fail | None, why) for one e2e result: a delivered mesh passes, a mesh-stage failure
    or time-out fails, anything that stopped before an engine ran or on our side does not count."""
    outcome = str(result.get("outcome") or "")
    stage = str(result.get("stage") or "")
    failure = str(result.get("failure_class") or "")
    if outcome in ("pass", "concerns"):
        return "pass", ""
    if outcome not in ("failed", "timeout"):
        return None, f"e2e {outcome or 'no outcome'}"
    if not stage.startswith("mesh"):
        return None, f"e2e stopped at {stage.split(':', 1)[0] or 'an unknown stage'}"
    if never_meshed(failure):
        # the stage label says "mesh", but no mesher ran: the run stopped while preparing it
        return None, "e2e never meshed"
    if any(m in failure for m in _E2E_INFRASTRUCTURE):
        return None, "e2e failure on our side"
    return "fail", ""


def e2e_row(result: dict) -> tuple[dict | None, str]:
    """One table row from one e2e result (see the module doc), or (None, why it does not count).
    The commit rule is the caller's."""
    from meshpipeline.cad.shape_traits import TRAITS_VERSION
    from meshpipeline.engines.fitness import brief_facts
    status, why = e2e_status(result)
    if status is None:
        return None, why
    rec = result.get("recommend") if isinstance(result.get("recommend"), dict) else {}
    traits = rec.get("traits") if isinstance(rec.get("traits"), dict) else None
    if not traits or not traits.get("n_triangles"):
        return None, "e2e geometry not measured"
    if int(traits.get("version") or 0) != TRAITS_VERSION:
        return None, "e2e traits measured by another traits version"
    engine = str(result.get("engine") or "")
    if not engine:
        return None, "e2e run names no engine"
    mesh = result.get("mesh") if isinstance(result.get("mesh"), dict) else {}
    reason = str(mesh.get("reason") or result.get("reason") or "")
    fatal = _fatal(mesh, reason)
    quality = dict(mesh.get("quality") or {}) if isinstance(mesh.get("quality"), dict) else {}
    if fatal:
        quality["fatal"] = fatal
    intake = result.get("intake") if isinstance(result.get("intake"), dict) else {}
    patches = [p for p in (intake.get("patches") or []) if isinstance(p, dict)]
    case_id = str(result.get("case_id") or "")
    row = {
        "shape": shape_id(case_id), "case": case_id, "family": str(result.get("category") or ""),
        "engine": engine, "format": str(result.get("format") or ""),
        "form": str(traits.get("form") or ""), "status": status,
        "failure_class": "" if status == "pass" else str(result.get("failure_class") or ""),
        "cells": result.get("cells"),
        "seconds": _num((mesh.get("timings") or {}).get("total")),
        "layers_pct": _layers_pct(mesh), "cells_across_p05": None, "quality": quality,
        "tag": str(result.get("tag") or ""),
        "commit": str((result.get("git") or {}).get("commit") or "")[:8],
        "brief": brief_facts(request_txt=str(intake.get("request_txt") or ""), patches=patches),
        "source": "e2e", "traits": traits}
    if status == "fail" and fatal:
        row["invalid"] = True
    return row, ""


def _render(table: dict) -> str:
    """The table as committed: the header readable, one run per line (a diff shows runs)."""
    head = json.dumps({k: v for k, v in table.items() if k != "rows"}, indent=1)
    rows = ",\n".join("  " + json.dumps(r, sort_keys=True) for r in table["rows"])
    return head[:-2] + ',\n "rows": [\n' + rows + "\n ]\n}\n"


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


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# #
# the frozen split
# #

class Split:
    """The frozen held-out split (the e2e loop's holdout_files.json): every file's side, by the
    sha256 of its bytes, with the e2e case ids it was indexed under."""

    def __init__(self, data: dict) -> None:
        self.version = str(data.get("version") or "")
        self.manifest = str(data.get("manifest_sha256") or "")
        self.side_of: dict[str, str] = {}
        self.by_case: dict[str, str] = {}
        for side in ("holdout", "dev"):
            for e in data.get(side) or ():
                if not isinstance(e, dict) or not e.get("sha256"):
                    continue
                self.side_of[e["sha256"]] = side
                for c in e.get("case_ids") or ():
                    self.by_case[c] = e["sha256"]

    @classmethod
    def read(cls, path: str) -> Split:
        return cls(json.loads(Path(path).read_text()))

    @property
    def held_out(self) -> list[str]:
        return sorted(h for h, s in self.side_of.items() if s == "holdout")

    def side(self, sha256: str) -> str:
        """'dev', 'holdout' or '' (not in the split)."""
        return self.side_of.get(sha256 or "", "")


def _lab_result_input(tag: str, results: Path, roots: list[Path]) -> Path | None:
    """The input file of the lab run `tag` (its result in the lab's out/ directory)."""
    try:
        r = json.loads((results / f"{tag}.json").read_text())
    except (OSError, ValueError):
        return None
    return _input_path(r, roots) if isinstance(r, dict) else None


#: The exclusion counts the dev-side rule writes: counted again on every build, never carried over.
_ROW_RULE_KEYS = ("hand relabel (re-run the case instead)", "never meshed", "input file not found",
                  "held out (frozen split)", "file not in the frozen split")


def keep_dev_rows(rows: list[dict], split: Split, excluded: Counter, *,
                  input_of=None) -> list[dict]:
    """THE DEV-SIDE RULE: each row's file is found in the split by its bytes' sha256 (a row that
    already carries `input_sha256` keeps it; else `input_of(row)` gives it). Held-out rows, rows
    whose file is not in the split, rows where no mesher ran and rows an earlier table relabelled
    by hand are dropped and only counted. The rows kept carry `split: "dev"`."""
    kept = []
    for row in rows:
        failure = str(row.get("failure_class") or "")
        if failure.startswith("recounted:"):
            excluded[_ROW_RULE_KEYS[0]] += 1
            continue
        if never_meshed(failure):
            excluded[_ROW_RULE_KEYS[1]] += 1
            continue
        digest = str(row.get("input_sha256") or "")
        if not digest and input_of is not None:
            digest = input_of(row) or ""
        if not digest:
            excluded[_ROW_RULE_KEYS[2]] += 1
            continue
        side = split.side(digest)
        if side == "holdout":
            excluded[_ROW_RULE_KEYS[3]] += 1
            continue
        if side != "dev":
            excluded[_ROW_RULE_KEYS[4]] += 1
            continue
        kept.append({**row, "input_sha256": digest, "split": "dev"})
    return kept


def rows_digest(rows: list[dict]) -> str:
    """The sha256 of the rows as the table writes them: what `rows_sha256` records."""
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _measure(job: dict) -> tuple[str, dict]:
    from meshpipeline.cad.shape_traits import measure_file
    t = measure_file(Path(job["path"]), flow=job["flow"], input_kind=job["input_kind"],
                     patches=job["patches"], surface_unit_to_m=job["unit_to_m"],
                     flow_axis=job["flow_axis"])
    return job["key"], t.as_dict()


def _lab_rows(args, base: str, since: str) -> tuple[list, Counter, Counter, Counter]:
    """(rows, excluded, commits, prefixes) from the lab's out/ directory."""
    from meshpipeline.cad.shape_traits import TRAITS_VERSION
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.fitness import brief_facts

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
        fatal = _fatal(r) if status == "fail" else []
        quality = dict(r.get("quality") or {}) if isinstance(r.get("quality"), dict) else {}
        if fatal:
            quality["fatal"] = fatal
        row = {
            "shape": shape_id(case_id), "case": case_id, "family": case.get("family") or "",
            "engine": engine, "format": fmt, "form": traits.get("form") or "",
            "status": status, "failure_class": failure,
            "cells": r.get("cells"), "seconds": _num((r.get("timings") or {}).get("total")),
            "layers_pct": _layers_pct(r), "cells_across_p05": _cells_across(r),
            "quality": quality, "tag": tag, "commit": commit[:8],
            "input_sha256": _sha256(Path(_input_path(r, roots) or f)),
            # what the case's own brief says (layers asked for, ground, budget): the held-out check
            # recommends with it, as the product does with the user's brief
            "brief": brief_facts(request_txt=str(case.get("request_txt") or ""),
                                 patches=[p for p in (case.get("patches") or []) if isinstance(p, dict)],
                                 cell_budget=case.get("max_cells")),
            "traits": traits}
        if fatal:
            row["invalid"] = True
        rows.append(row)

    return rows, excluded, commits, prefixes


def _prior_lab_rows(path: str) -> tuple[list, Counter, Counter, Counter, dict]:
    """The lab rows of an earlier table, as they are - for a build that adds the e2e sweeps to
    the lab record the table shipped with, without re-reading an out/ directory that has moved
    on since (newer re-runs there would change rows this build is not about)."""
    prior = json.loads(Path(path).read_text())
    prov = prior.get("provenance") or {}
    rows = [dict(r) for r in prior.get("rows") or () if isinstance(r, dict) and r.get("source") != "e2e"]
    # the lab side's own exclusions only: an earlier build's e2e counts are counted again here
    excluded = Counter({k: v for k, v in (prov.get("excluded") or {}).items()
                        if not k.startswith(("e2e", "lab run superseded")) and k not in _ROW_RULE_KEYS})
    # a table that was itself built this way passes on where ITS lab rows came from
    origin = dict(prov.get("lab_rows_from") or {}) or {
        "version": prior.get("version"), "command": prior.get("command"),
        "base_commit": prov.get("base_commit"), "since_commit": prov.get("since_commit")}
    return (rows, excluded, Counter(prov.get("commits") or {}), Counter(prov.get("tag_prefixes") or {}),
            {**origin, "table": path, "runs": len(rows)})


def _changelog_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="", help="the lab's out/ directory")
    ap.add_argument("--lab-rows-from", default="",
                    help="instead of --out: keep the lab rows of this earlier table as they are "
                         "(its code rule and provenance come with them) and add the --e2e runs")
    ap.add_argument("--cases", action="append", default=[], help="a directory of case JSONs (repeatable)")
    ap.add_argument("--root", action="append", default=[], help="a root relative input files resolve against")
    ap.add_argument("--since", default="", help="oldest commit whose runs count (with --out)")
    ap.add_argument("--base", default="HEAD", help="the code the table ships with (default HEAD)")
    ap.add_argument("--prefix", action="append", default=[], help="only result tags matching this glob")
    ap.add_argument("--cache", default="", help="traits cache file (JSON) to reuse measurements")
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--lab-results", default="",
                    help="with --lab-rows-from: the lab's out/ directory, to find each lab row's input "
                         "file (by its tag) when the earlier table did not record its sha256")
    ap.add_argument("--split", default="",
                    help="REQUIRED: the frozen held-out split (holdout_files.json); only dev rows are kept")
    ap.add_argument("--changelog", default=str(CHANGELOG))
    ap.add_argument("--e2e", action="append", default=[],
                    help="an e2e sweep's results/ directory (repeatable)")
    ap.add_argument("--e2e-base", default="HEAD",
                    help="e2e runs count when their commit is an ancestor of this (default HEAD: the "
                         "code the table ships with; the lab's --base may be a lineage commit)")
    ap.add_argument("--e2e-since", default="", help="...and, when given, descends from this")
    ap.add_argument("--e2e-index", default="",
                    help="with --e2e: the e2e loop's index.jsonl; only clean (gate) ORIGINAL files count")
    args = ap.parse_args(argv)
    if bool(args.out) == bool(args.lab_rows_from):
        ap.error("give exactly one of --out (with --since) or --lab-rows-from")
    if args.out and not args.since:
        ap.error("--out needs --since")
    if args.e2e and not args.e2e_index:
        ap.error("--e2e needs --e2e-index (only clean original files count)")
    if not args.split:
        ap.error("--split is required: the table is built from the dev side of the frozen split only")
    split = Split.read(args.split)
    if not split.side_of:
        ap.error(f"--split {args.split}: no files in it")

    from meshpipeline.cad.shape_traits import TRAITS_VERSION

    base = _git("rev-parse", args.base)
    since = _git("rev-parse", args.since) if args.since else ""
    lab_from: dict = {}
    if args.lab_rows_from:
        rows, excluded, commits, prefixes, lab_from = _prior_lab_rows(args.lab_rows_from)
        base = str(lab_from.get("base_commit") or base)
        since = str(lab_from.get("since_commit") or since)
    else:
        rows, excluded, commits, prefixes = _lab_rows(args, base, since)
    roots = [Path(r) for r in args.root] + [Path(d) for d in args.cases]
    results = Path(args.lab_results) if args.lab_results else None
    _digests: dict[str, str] = {}

    def _lab_input(row: dict) -> str:
        if results is None:
            return ""
        p = _lab_result_input(str(row.get("tag") or ""), results, roots)
        if p is None:
            return ""
        if str(p) not in _digests:
            _digests[str(p)] = _sha256(p)
        return _digests[str(p)]

    rows = keep_dev_rows(rows, split, excluded, input_of=_lab_input)

    # THE END-TO-END SWEEPS: newest result per (case, engine, format), by the same code rule
    e2e_latest: dict[tuple, tuple[float, dict]] = {}
    e2e_sweeps: Counter = Counter()
    e2e_base = _git("rev-parse", args.e2e_base) if args.e2e else ""
    e2e_since = _git("rev-parse", args.e2e_since) if args.e2e and args.e2e_since else ""
    e2e_index: dict[str, dict] = {}
    for line in (Path(args.e2e_index).read_text().splitlines() if args.e2e_index else ()):
        if line.strip():
            entry = json.loads(line)
            e2e_index[str(entry.get("id") or "")] = entry
    for d in args.e2e:
        for f in sorted(Path(d).glob("*.json")):
            try:
                res = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(res, dict) or "case_id" not in res:
                continue
            commit = str((res.get("git") or {}).get("commit") or "")
            if not commit or not is_ancestor(commit, e2e_base) or (
                    e2e_since and not is_ancestor(e2e_since, commit)):
                excluded["e2e code not in since..base"] += 1
                continue
            entry = e2e_index.get(str(res["case_id"]))
            if entry is None:
                excluded["e2e case not in --e2e-index"] += 1
                continue
            if entry.get("tier") != "gate":
                # a file not verified clean fails for its own reasons as often as for the engine's
                excluded["e2e file not in the clean (gate) tier"] += 1
                continue
            if entry.get("is_variant"):
                # a format copy of a shape is the same geometry: its original is the one run
                # counted, so one shape never gets a vote per file format
                excluded["e2e format copy (its original counts)"] += 1
                continue
            row, why = e2e_row(res)
            if row is None:
                excluded[why] += 1
                continue
            # the e2e case's file, by the case id the split indexed it under
            dev = keep_dev_rows([{**row, "input_sha256": split.by_case.get(row["case"], "")}],
                                split, excluded)
            if not dev:
                continue
            row = dev[0]
            key = (row["case"], row["engine"], row["format"])
            mtime = f.stat().st_mtime
            if key in e2e_latest:
                excluded["e2e older duplicate"] += 1
                if mtime <= e2e_latest[key][0]:
                    continue
            e2e_latest[key] = (mtime, row)
            e2e_sweeps[str(res.get("sweep") or Path(d).parent.name)] += 1
    e2e_rows = [row for _, row in e2e_latest.values()]
    if e2e_rows:
        newer = {(r["shape"], r["engine"], r["format"]) for r in e2e_rows}
        kept = [r for r in rows if (r["shape"], r["engine"], r["format"]) not in newer]
        excluded["lab run superseded by an e2e run"] += len(rows) - len(kept)
        rows = kept + sorted(e2e_rows, key=lambda r: (r["case"], r["engine"], r["format"]))
        # counted from the rows the table keeps: a superseded lab run no longer counts
        commits = Counter(str(r.get("commit") or "") for r in rows)
        prefixes = Counter(str(r.get("tag") or "").split("-", 1)[0] for r in rows)

    from meshpipeline.cad.shape_traits import ShapeTraits
    from meshpipeline.engines.fitness import detail_of, shape_class, table_from
    summary: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in rows:
        t = ShapeTraits.from_dict(row["traits"])
        cls = shape_class(t) or "(unplaced)"
        detail = detail_of(t)
        flow = row["traits"].get("flow") or ""
        cell = summary[f"{flow} / {cls}{f' ({detail})' if detail else ''} / {row['form']}"][row["engine"]]
        cell[0] += row["status"] == "pass"
        cell[1] += 1
    if any(r.get("split") != "dev" or split.side(str(r.get("input_sha256"))) != "dev" for r in rows):
        raise SystemExit("a row that is not on the dev side reached the table - refusing to write it")
    digest = rows_digest(rows)
    table = {
        "version": f"{dt.date.today().isoformat()}-{base[:8]}-{digest[:8]}",
        "rules_version": RULES_VERSION,
        "rows_sha256": digest,
        "split": {"version": split.version, "manifest_sha256": split.manifest,
                  "held_out_files": len(split.held_out)},
        "traits_version": TRAITS_VERSION,
        "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "devtools/fitness/build_engine_fitness.py " + " ".join(argv or sys.argv[1:]),
        "provenance": {
            "base_commit": base, "since_commit": since,
            "rule": "runs whose commit descends from since_commit and is an ancestor of base_commit; "
                    "newest result per (case, engine, format); refusals and lab-runner crashes left out; "
                    "e2e sweep runs whose commit is an ancestor of e2e.base_commit count when an "
                    "engine ran (delivered = pass, a mesh-stage failure or time-out = fail) and "
                    "supersede a lab run of the same shape, engine and format; only DEV-side files of "
                    "the frozen split (by the sha256 of the input's bytes); only runs where a mesher "
                    "ran; no hand relabels",
            "e2e": {"dirs": list(args.e2e), "runs": len(e2e_rows), "base_commit": e2e_base,
                    "since_commit": e2e_since, "sweeps": dict(sorted(e2e_sweeps.items()))},
            "invalid": sum(1 for r in rows if r.get("invalid")),
            **({"lab_rows_from": lab_from} if lab_from else {}),
            "runs": len(rows), "shapes": len({r["shape"] for r in rows}),
            "cases": len({r["case"] for r in rows}),
            "commits": dict(sorted(commits.items())), "tag_prefixes": dict(sorted(prefixes.items())),
            "excluded": dict(sorted(excluded.items())),
        },
        "summary": {k: {e: f"{p}/{n}" for e, (p, n) in sorted(v.items())}
                    for k, v in sorted(summary.items())},
        "rows": rows,
    }
    Path(args.table).write_text(_render(table))
    HELD_OUT_DIGESTS.write_text(
        f"# the held-out files of the frozen split {split.version} (manifest {split.manifest}):\n"
        "# one sha256 of the file's bytes per line. No engine fitness row may come from these.\n"
        + "".join(f"{h}\n" for h in split.held_out))
    if table["version"] not in _changelog_text(args.changelog):
        print(f"NOTE: add an entry for {table['version']} (rows_sha256 {digest}) to {args.changelog}",
              file=sys.stderr)
    t = table_from(table)
    print(f"wrote {args.table}: {len(rows)} runs on {table['provenance']['shapes']} shapes "
          f"({len(t.runs)} readable); excluded {dict(excluded)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
