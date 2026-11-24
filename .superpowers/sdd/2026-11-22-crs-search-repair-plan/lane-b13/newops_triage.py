"""B13: triage a snapshot wave's rows - for every file still failing, what still fails and what the NEW ops did.

Usage:  python newops_triage.py OUT_PREFIX ROWS_GLOB [ROWS_GLOB ...]
        e.g. python newops_triage.py /tmp/v2 '/path/snap-v2-dev/*/rows.jsonl' '/path/snap-v2-clean/*/rows.jsonl'
Writes OUT_PREFIX.json and OUT_PREFIX.md. Reads ONLY the rows (no traces, no kernel, no model, no VM); safe to re-run
as shards land - each run is a snapshot of whatever rows exist.

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
import sys
from collections import Counter, defaultdict
from typing import Any

NEW_OPS = ("defeature", "fuse", "conformalise", "trim_unbounded")
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


def triage(rows: list[dict]) -> dict:
    files = Counter((r["_split"], r.get("arm")) for r in rows)
    failing = [r for r in rows if not r.get("inconclusive") and not r.get("out_clean")]
    inconclusive = Counter((r["_split"], r.get("arm")) for r in rows if r.get("inconclusive"))
    groups: dict[str, dict] = {}
    parts: dict[str, dict] = {}
    per_row = []
    for r in failing:
        kind, bits = failure_kind(r)
        arm = r.get("arm")
        is_search = arm in SEARCH_ARMS
        outcomes = {op: op_outcomes(r, op) for op in NEW_OPS} if is_search else {}
        open_sites = ((r.get("triage") or {}).get("search") or {}).get("open_sites") or {}
        per_row.append({"split": r["_split"], "arm": arm, "name": r.get("name"), "kind": kind,
                        "panel": f"{r.get('in_panel_passes')}->{r.get('out_panel_passes')}/{r.get('panel_size')}",
                        "valid": f"{r.get('in_valid')}->{r.get('out_valid')}", "open_sites": open_sites,
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
                if set(c) != {"never_proposed"}:
                    g["rows_touched"][op] += 1
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
    for title, key in (("By individual failure (a row counts under each of its codes/members)", "by_part"),
                       ("By full failure kind", "by_kind")):
        out += [f"## {title}", "",
                "| failure | files | rows by arm | open sites at best | " + " | ".join(NEW_OPS) + " |",
                "|---|---|---|---|" + "---|" * len(NEW_OPS)]
        for kind, g in t[key].items():
            cells = []
            for op in NEW_OPS:
                c = g["ops"].get(op, {})
                prop = g["rows_where_proposed"].get(op, 0)
                cells.append(f"proposed in {prop} rows; {_cell({k: v for k, v in c.items() if k != 'never_proposed'}, 4)}"
                             if prop else "never proposed")
            out.append(f"| {kind} | {g['files']} | {_cell(g['rows'])} | {_cell(g['open_sites'], 4)} | "
                       + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    t = triage(load(argv[2:]))
    with open(argv[1] + ".json", "w") as fh:
        json.dump(t, fh, indent=1, default=str)
    with open(argv[1] + ".md", "w") as fh:
        fh.write(markdown(t))
    print(f"{t['rows_read']} rows, {t['failing_rows']} failing -> {argv[1]}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
