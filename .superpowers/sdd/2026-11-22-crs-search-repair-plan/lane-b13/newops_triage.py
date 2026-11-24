"""B13: triage a snapshot wave's rows - for every file still failing, what still fails and what the NEW ops did.

Usage:  python newops_triage.py OUT_PREFIX ROWS_GLOB [ROWS_GLOB ...] [--traces TRACE_GLOB ...]
        e.g. python newops_triage.py /tmp/v2 'snap-v2-dev/*/rows.jsonl' 'snap-v2-clean/*/rows.jsonl' \
                 --traces 'snap-v2-traces/**/*.json'
Writes OUT_PREFIX.json and OUT_PREFIX.md. Reads the rows and, WHERE PRESENT, the per-file search traces (no kernel, no
model, no VM); safe to re-run as shards land - each run is a snapshot of whatever exists.

TRACES. A trace (`Trace.to_dict()`: {"events": [...]}) is matched to a search row when its PATH contains the row's arm
(`search_det` / `search_ai`) AND either the first 12 hex of the row's sha256 or the stem of the row's file name. A row
with a trace is triaged from the trace (every candidate event); a row without one falls back to the row's op table.
From a trace, per new op:
  - conformalise is split by mode: `conformalise:fragment` / `conformalise:cut_by` (the candidate's params);
  - every evaluated candidate: cheap score vs its parent's score -> applied->better / equal / worse (no cap);
  - `kept_on_best_path`: the candidate produced a state on `stop.best_path`. That is the BEST state's path; whether it
    was DELIVERED is the delivery gate's call, read off the row (out vs in), not the trace;
  - `full_confirmed->better/equal/worse`: the child was one of the top-k the search confirmed with the FULL panel
    (`confirm` events), its full score vs the parent's score;
  - `never_proposed:site_present` vs `never_proposed:no_site`: whether a site the op is proposed for (overlap for
    fuse/conformalise, unbounded for trim_unbounded, DEFEATURE_SITE_KINDS for defeature) was open at any expanded
    state;
  - defeature: candidates by params.source (site / recognised) and the recognised features per call (len(groups), and
    the holes/blends the rationale names).
STILL IMPOSSIBLE from traces, and not inferred here:
  - a full-panel verdict for EVERY candidate: only the top-k children per expansion are confirmed, and the best state
    is; every other candidate has a cheap score only (and member names are not in any score);
  - the measured cut depth / bound of a conformalise cut: it is in the op's Diff params, not in the trace;
  - why the det sweep did NOT propose an op: its skip reasons live in the generator (`skipped`, `notes`), keyed by
    reason, never written to the trace; only the site-present / no-site split above is derivable.

Per failing (arm, file) row:
  failure kind   - layer 1 first: `invalid: <sorted distinct hard codes>`; else `panel: <sorted failing member/error_kind>`.
  new-op outcome - for each of defeature, fuse, conformalise (cut_by at any depth / fragment; the row does not split
                   the modes), trim_unbounded:
                     never_proposed  the op is absent from the row's op table (not proposed, or declined in advance
                                     by the sweep - the row's `not_proposed` counter is keyed by reason, not by op)
                     declined:<code> / failed:<code> / invalid:<code>  rejected, with the op's own first reason token
                     applied->better / ->equal / ->worse   evaluated, its cheap score vs its parent's (from `deltas`,
                                     which keeps only the first 8 evaluated candidates of the whole search)
                     applied->unscored  evaluated beyond the deltas cap
                     duplicate / inconclusive / skipped_budget
Grouped by failure kind (and per individual code / member), so it shows where each new op bites and where it does
nothing."""
from __future__ import annotations

import glob
import json
import re
import sys
from collections import Counter, defaultdict
from typing import Any

NEW_OPS = ("defeature", "fuse", "conformalise", "trim_unbounded")
#: trace-level split of conformalise by mode; the row op table cannot split it
TRACE_OPS = ("defeature", "fuse", "conformalise:cut_by", "conformalise:fragment", "trim_unbounded")
#: the site kinds each op is proposed at (search.DetGenerator; DEFEATURE_SITE_KINDS copied from search.py)
SITE_KINDS = {"defeature": {"sliver", "proximity", "pole", "self_overlap", "intersection", "timeout"},
              "fuse": {"overlap"}, "conformalise:cut_by": {"overlap"}, "conformalise:fragment": {"overlap"},
              "conformalise": {"overlap"}, "trim_unbounded": {"unbounded"}}
SEARCH_ARMS = ("search_det", "search_ai")


def load(patterns: list[str]) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            split = path.split("/")[-3] if path.count("/") >= 2 else "?"
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    r["_split"] = split
                    rows[(split, r.get("arm"), r.get("sha256") or r.get("name"))] = r   # a re-run row replaces
    return list(rows.values())


def failure_kind(r: dict) -> tuple[str, list[str]]:
    """(kind, its parts). Validity first: an invalid output is not meshed meaningfully past layer 1."""
    t = r.get("triage") or {}
    valid = t.get("valid") or {}
    if not r.get("out_valid", valid.get("ok", False)):
        codes = sorted(set(valid.get("hard") or [])) or ["(no hard code recorded)"]
        return "invalid: " + ", ".join(codes), [f"valid:{c}" for c in codes]
    members = sorted({f"{m.get('name')}/{m.get('error_kind') or m.get('status')}" for m in t.get("panel_failing") or []})
    members = members or ["(no failing member recorded)"]
    return "panel: " + ", ".join(members), [f"panel:{m}" for m in members]


def _code(reason: str) -> str:
    """The reason's code: the first token, e.g. `nothing_to_fuse`, `invalid_params`, `stage 'x'`."""
    s = str(reason).strip()
    for sep in (":", " - "):
        if sep in s:
            s = s.split(sep)[0]
    return s.strip()[:60] or "(none)"


def _cmp(parent: Any, score: Any) -> str:
    try:
        p, s = list(parent), list(score)
    except TypeError:
        return "unscored"
    try:
        return "better" if s > p else ("equal" if s == p else "worse")
    except TypeError:  # a None inside a score
        return "unscored"


def op_outcomes(r: dict, op: str) -> Counter:
    """Every fate this op met on this row, counted."""
    s = ((r.get("triage") or {}).get("search")) or {}
    stats = (s.get("ops") or {}).get(op)
    out: Counter = Counter()
    if not stats:
        out["never_proposed"] += 1
        return out
    for fate in ("declined", "failed", "invalid"):
        for reason, n in (stats.get(fate) or {}).items():
            out[f"{fate}:{_code(reason)}"] += int(n)
    for fate in ("duplicate", "inconclusive", "skipped_budget"):
        if stats.get(fate):
            out[fate] += int(stats[fate])
    evaluated = int(stats.get("evaluated") or 0)
    scored = [d for d in s.get("deltas") or [] if d.get("op") == op]
    for d in scored[:evaluated]:
        out[f"applied->{_cmp(d.get('parent'), d.get('score'))}"] += 1
    if evaluated > len(scored):
        out["applied->unscored"] += evaluated - len(scored)
    return out


def load_traces(patterns: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for pat in patterns:
        for path in sorted(glob.glob(pat, recursive=True)):
            try:
                with open(path) as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            events = d.get("events") if isinstance(d, dict) else d
            if isinstance(events, list):
                out[path] = events
    return out


def trace_for(r: dict, traces: dict[str, list[dict]]) -> tuple[str, list[dict]] | None:
    arm = str(r.get("arm"))
    sha = str(r.get("sha256") or "")[:12]
    stem = str(r.get("name") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for path, events in traces.items():
        if arm in path and ((sha and sha in path) or (stem and stem in path)):
            return path, events
    return None


def _trace_op(e: dict) -> str:
    op = str(e.get("op"))
    if op == "conformalise":
        mode = str((e.get("params") or {}).get("mode") or "")
        return "conformalise:cut_by" if mode.startswith("cut_by") else f"conformalise:{mode or '?'}"
    return op


def trace_outcomes(events: list[dict]) -> dict[str, Any]:
    """Per new op, every fate it met in this search, from the candidate / confirm / expand / stop events."""
    stop = next((e for e in reversed(events) if e.get("kind") == "stop"), {})
    on_path = set(stop.get("best_path") or [])
    confirmed = {e.get("state"): e.get("score") for e in events if e.get("kind") == "confirm"}
    site_kinds = {str(s.get("kind")) for e in events if e.get("kind") == "expand" for s in e.get("sites") or []}
    ops: dict[str, Counter] = {op: Counter() for op in TRACE_OPS}
    extra: dict[str, Counter] = {"defeature_source": Counter(), "defeature_features_per_call": Counter(),
                                 "defeature_holes": Counter(), "defeature_blends": Counter(), "proposed_by": Counter()}
    for e in events:
        if e.get("kind") != "candidate":
            continue
        op = _trace_op(e)
        if op not in ops:
            continue
        c = ops[op]
        extra["proposed_by"][f"{op}/{e.get('source')}"] += 1
        outcome = e.get("outcome")
        if outcome == "evaluated":
            c[f"applied->{_cmp(e.get('parent_score'), e.get('score'))}"] += 1
            child = e.get("child")
            if child in on_path:
                c["kept_on_best_path"] += 1
            if child in confirmed:
                c[f"full_confirmed->{_cmp(e.get('parent_score'), confirmed[child])}"] += 1
        elif outcome == "rejected":
            errors = e.get("errors") or ["(no reason recorded)"]
            c[f"{_REJECTED_AS.get(str(e.get('stage')), 'failed')}:{_code(errors[0])}"] += 1
        else:
            c[str(outcome)] += 1
        if op == "defeature":
            params = e.get("params") or {}
            extra["defeature_source"][str(params.get("source"))] += 1
            if params.get("source") == "recognised":
                extra["defeature_features_per_call"][str(len(params.get("groups") or []))] += 1
                m = re.search(r"\((\d+) holes, (\d+) blends\)", str(e.get("rationale") or ""))
                if m:
                    extra["defeature_holes"][m.group(1)] += 1
                    extra["defeature_blends"][m.group(2)] += 1
    for op, c in ops.items():
        if not c:
            c["never_proposed:site_present" if site_kinds & SITE_KINDS[op] else "never_proposed:no_site"] += 1
    return {"ops": ops, "extra": extra, "best_path": list(on_path)}


#: the search's rejection stages, as arms.search_triage names them
_REJECTED_AS = {"validate": "invalid", "declined": "declined", "failed": "failed", "error": "failed"}


def triage(rows: list[dict], traces: dict[str, list[dict]] | None = None) -> dict:
    files = Counter((r["_split"], r.get("arm")) for r in rows)
    failing = [r for r in rows if not r.get("inconclusive") and not r.get("out_clean")]
    inconclusive = Counter((r["_split"], r.get("arm")) for r in rows if r.get("inconclusive"))
    groups: dict[str, dict] = {}
    parts: dict[str, dict] = {}
    per_row = []
    n_traced: Counter = Counter()
    defeature_extra: dict[str, Counter] = defaultdict(Counter)
    for r in failing:
        kind, bits = failure_kind(r)
        arm = r.get("arm")
        is_search = arm in SEARCH_ARMS
        found = trace_for(r, traces or {}) if is_search else None
        extra: dict = {}
        if found is not None:
            got = trace_outcomes(found[1])
            outcomes, extra = got["ops"], {k: dict(v) for k, v in got["extra"].items() if v}
            n_traced[arm] += 1
        else:
            outcomes = {op: op_outcomes(r, op) for op in NEW_OPS} if is_search else {}
        open_sites = ((r.get("triage") or {}).get("search") or {}).get("open_sites") or {}
        per_row.append({"split": r["_split"], "arm": arm, "name": r.get("name"), "kind": kind,
                        "panel": f"{r.get('in_panel_passes')}->{r.get('out_panel_passes')}/{r.get('panel_size')}",
                        "valid": f"{r.get('in_valid')}->{r.get('out_valid')}", "open_sites": open_sites,
                        "from": "trace" if found is not None else "row",
                        "trace": found[0] if found is not None else None, "trace_extra": extra,
                        "new_ops": {op: dict(c) for op, c in outcomes.items()},
                        "overlap": (r.get("triage") or {}).get("overlap")})
        for key, table in ((kind, groups), *((b, parts) for b in bits)):
            g = table.setdefault(key, {"rows": Counter(), "files": set(), "ops": defaultdict(Counter),
                                       "rows_touched": Counter(), "open_sites": Counter()})
            g["rows"][arm] += 1
            g["files"].add(r.get("name"))
            g["open_sites"].update(open_sites)
            for op, c in outcomes.items():
                g["ops"][op].update(c)
                if not all(k.startswith("never_proposed") for k in c):
                    g["rows_touched"][op] += 1
        for k, v in extra.items():
            defeature_extra[k].update(v)
    def freeze(table: dict) -> dict:
        return {k: {"rows": dict(v["rows"]), "files": len(v["files"]), "open_sites": dict(v["open_sites"]),
                    "rows_where_proposed": dict(v["rows_touched"]),
                    "ops": {op: dict(c.most_common()) for op, c in v["ops"].items()}}
                for k, v in sorted(table.items(), key=lambda kv: -sum(kv[1]["rows"].values()))}
    # a FILE still failing: conclusive and not clean in every repair arm it ran (input_only is the baseline, not a repair)
    by_file: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        if r.get("arm") != "input_only":
            by_file[(r["_split"], r.get("name"))][r.get("arm")] = r
    still = {}
    for (split, name), arms in by_file.items():
        if any(x.get("out_clean") for x in arms.values()) or all(x.get("inconclusive") for x in arms.values()):
            continue
        last = arms.get("search_ai") or arms.get("search_det") or next(iter(arms.values()))
        still[f"{split}/{name}"] = failure_kind(last)[0]
    return {"files_still_failing": len(still), "files_still_failing_by_kind": dict(Counter(still.values()).most_common()),
            "files_still_failing_list": still,
            "failing_search_rows_from_trace": dict(n_traced),
            "trace_extra_totals": {k: dict(v.most_common()) for k, v in defeature_extra.items()},
            "rows_read": len(rows), "rows_by_split_arm": {f"{s}/{a}": n for (s, a), n in sorted(files.items())},
            "inconclusive_by_split_arm": {f"{s}/{a}": n for (s, a), n in sorted(inconclusive.items())},
            "failing_rows": len(failing), "by_kind": freeze(groups), "by_part": freeze(parts), "rows": per_row}


def _cell(d: dict, n: int = 6) -> str:
    items = list(d.items())
    s = ", ".join(f"{k} {v}" for k, v in items[:n])
    return (s + (f", +{len(items) - n} more" if len(items) > n else "")) or "-"


def markdown(t: dict) -> str:
    out = ["# New-op triage (B13)", "",
           f"Rows read: {t['rows_read']}. Failing rows (conclusive, not clean): {t['failing_rows']}. "
           f"FILES still failing in every repair arm: {t['files_still_failing']} (kind read off search_ai, else "
           "search_det, else staged).", "",
           "| still-failing kind | files |", "|---|---|",
           *[f"| {k} | {n} |" for k, n in t["files_still_failing_by_kind"].items()], "",
           "Rows per split/arm: " + _cell(t["rows_by_split_arm"], 20),
           "Inconclusive per split/arm: " + (_cell(t["inconclusive_by_split_arm"], 20)), ""]
    out += [f"Failing search rows triaged from a TRACE: {_cell(t['failing_search_rows_from_trace'])} "
            "(the rest from the row's op table: no mode split, deltas capped at 8, no best-path or full-confirm).",
            "Trace extras (defeature source / features per call / holes / blends; proposals per op/source): "
            + "; ".join(f"{k}: {_cell(v, 8)}" for k, v in t["trace_extra_totals"].items()), ""]
    dup = sum(int(n) for r in t["rows"] for op, c in r["new_ops"].items()
              if op.startswith("conformalise") for k, n in c.items() if k == "duplicate")
    rej = sum(int(n) for r in t["rows"] for op, c in r["new_ops"].items()
              if op.startswith("conformalise") for k, n in c.items() if k.split(":")[0] in ("declined", "failed", "invalid"))
    out += [f"**C4 check (body-op declines once recorded as duplicates):** conformalise `duplicate` {dup}, "
            f"declined/failed/invalid {rej} on failing search rows. On the tip (C4 fixed) duplicates here are a "
            "FINDING, not the old bug." if dup else
            f"**C4 check:** conformalise `duplicate` 0, declined/failed/invalid {rej} on failing search rows.", ""]
    ops_seen = [op for op in dict.fromkeys((*TRACE_OPS, *NEW_OPS))
                if any(op in g["ops"] for g in t["by_part"].values())]
    for title, key in (("By individual failure (a row counts under each of its codes/members)", "by_part"),
                       ("By full failure kind", "by_kind")):
        out += [f"## {title}", "",
                "| failure | files | rows by arm | open sites at best | " + " | ".join(ops_seen) + " |",
                "|---|---|---|---|" + "---|" * len(ops_seen)]
        for kind, g in t[key].items():
            cells = []
            for op in ops_seen:
                c = g["ops"].get(op, {})
                prop = g["rows_where_proposed"].get(op, 0)
                never = {k: v for k, v in c.items() if k.startswith("never_proposed")}
                seen = {k: v for k, v in c.items() if not k.startswith("never_proposed")}
                cells.append((f"proposed in {prop} rows; {_cell(seen, 5)}" if prop else "never proposed")
                             + (f" [not proposed: {_cell(never, 2)}]" if never and prop else ""))
            out.append(f"| {kind} | {g['files']} | {_cell(g['rows'])} | {_cell(g['open_sites'], 4)} | "
                       + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    args = argv[2:]
    if "--traces" in args:
        i = args.index("--traces")
        row_globs, trace_globs = args[:i], args[i + 1:]
    else:
        row_globs, trace_globs = args, []
    t = triage(load(row_globs), load_traces(trace_globs))
    with open(argv[1] + ".json", "w") as fh:
        json.dump(t, fh, indent=1, default=str)
    with open(argv[1] + ".md", "w") as fh:
        fh.write(markdown(t))
    print(f"{t['rows_read']} rows, {t['failing_rows']} failing -> {argv[1]}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
