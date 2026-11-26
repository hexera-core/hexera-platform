#!/usr/bin/env python3
# Responsibility: The tuning loop's instrument. For a collected arm wave, say per arm WHY the outputs that are not clean
#                 are not clean, in aggregate: how each search stopped, which site kinds were still open, what became of
#                 each op (proposed, evaluated, declined / failed / invalid and why), which panel members still fail
#                 and how, how much of the deviation budget was used, and for search_ai the turns, tool errors and $.
# Owns: `triage_wave` (rows -> the JSON), `render_markdown`, the CLI.
# Boundaries: no CAD, no model, no bucket. Reads the rows a wave already wrote (each row's `triage` block, written by
#             `arms.py`) and EXCLUDED.json. It computes NO rate the report owns: report.py says how many files are
#             clean and whether the numbers may be read; this says where to look next. A diagnostic, so it reads a
#             partial wave too (it says so), where report.py would refuse.
"""Failure triage for a collected arm wave.

    triage.py --from <dir with shard-I-of-N/> [--shards N] [--split corpora/v4/dev.json] \\
        [--json triage.json] [--md triage.md] [--arms search_det,search_ai]

Every count is a count of FILES (one file, one arm, one row) unless it says "candidates". A file is "not clean" when
its row is conclusive and its output is not clean; inconclusive rows and EXCLUDED files are counted on their own and
never mixed into the "why" tables, because nothing about the part made them so.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as _report  # noqa: E402  (loads collect -> run_arm -> evalspec, reprobe)

_collect = _report._collect
rp = _report.rp

FATES = ("evaluated", "duplicate", "inconclusive", "skipped_budget")
REASONED = ("declined", "failed", "invalid")
#: panel.CAPPED_KIND, mirrored so triage reads rows without importing the engine (lane C16): a member that ran after
#: the panel was already doomed and hit the doomed cap. Counted apart, never as a mesher defect.
CAPPED_KIND = "capped_after_doom"
AI_SUMS = ("turns", "tool_errors", "candidate_errors", "declared_unfixable", "truncated_rounds")
#: how many entries a "top" list keeps in the JSON (the markdown shows fewer)
TOP_N = 10


def _top(counter: Mapping[str, int], n: int = TOP_N) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def _quantiles(xs: list[float]) -> dict[str, float | None]:
    if not xs:
        return {"n": 0, "median": None, "p90": None, "max": None}
    s = sorted(xs)
    return {"n": len(s), "median": statistics.median(s), "p90": s[min(len(s) - 1, int(0.9 * len(s)))], "max": s[-1]}


class _Ops:
    def __init__(self) -> None:
        self.ops: dict[str, dict[str, Any]] = {}

    def add(self, ops: Mapping[str, Mapping[str, Any]]) -> None:
        for op, d in ops.items():
            a = self.ops.setdefault(op, {"files": 0, "det": 0, "ai": 0, "ops_applied": 0,
                                         **dict.fromkeys(FATES, 0),
                                         **{k: Counter() for k in REASONED}})
            a["files"] += 1
            for k in ("det", "ai", "ops_applied", *FATES):
                a[k] += int(d.get(k) or 0)
            for k in REASONED:
                a[k].update(d.get(k) or {})

    def result(self) -> dict[str, dict[str, Any]]:
        out = {}
        for op, a in sorted(self.ops.items(), key=lambda kv: -(kv[1]["det"] + kv[1]["ai"])):
            out[op] = {**{k: a[k] for k in ("files", "det", "ai", "ops_applied", *FATES)},
                       **{k: sum(a[k].values()) for k in REASONED},
                       **{f"{k}_reasons": _top(a[k]) for k in REASONED}}
        return out


def _arm_block(rows: list[Any], excluded: list[dict], kinds: Mapping[str, str]) -> dict[str, Any]:
    conclusive = [r for r in rows if not r.inconclusive]
    clean = [r for r in conclusive if r.clean]
    bad = [r for r in conclusive if not r.clean]
    untriaged = [r for r in bad if not r.triage]
    stop: Counter[str] = Counter()
    ended: Counter[str] = Counter()
    unmeasured_stop: Counter[str] = Counter()
    site_files: Counter[str] = Counter()
    site_total = 0
    ops, not_proposed = _Ops(), Counter[str]()
    members: dict[str, dict[str, Any]] = defaultdict(lambda: {"files": 0, "status": Counter(), "error_kind": Counter()})
    failing_count: Counter[int] = Counter()
    capped: Counter[str] = Counter()
    hard: Counter[str] = Counter()
    used: list[float] = []
    dev_reasons: Counter[str] = Counter()
    faithful_fail = over_bound = 0
    kept: Counter[str] = Counter()
    rolled: Counter[str] = Counter()
    ai: dict[str, float] = {**dict.fromkeys(AI_SUMS, 0), "files": 0, "spent_usd": 0.0}
    ai_stop: Counter[str] = Counter()
    ai_capped = 0
    by_kind = Counter(kinds.get(r.sha256, "?") for r in bad)
    for r in rows:
        if r.inconclusive and r.triage and r.triage.get("search"):
            unmeasured_stop[str(r.triage["search"]["stop"].get("reason"))] += 1
    for r in bad:
        t = r.triage or {}
        s = t.get("search")
        if not s and r.reason is not None:  # a row written before triage existed still names how its search stopped
            stop[str(r.reason)] += 1
        if s:
            stop[str(s["stop"].get("reason"))] += 1
            ended[str(s["stop"].get("ended_by"))] += 1
            sites = s.get("open_sites") or {}
            site_total += sum(sites.values())
            site_files.update(sites.keys())
            ops.add(s.get("ops") or {})
            not_proposed.update(s.get("not_proposed") or {})
            if s.get("ai"):
                a = s["ai"]
                ai["files"] += 1
                for k in AI_SUMS:
                    ai[k] += int(a.get(k) or 0)
                ai["spent_usd"] += float(a.get("spent_usd") or 0.0)
                ai_stop[str(a.get("stop_reason"))] += 1
                ai_capped += bool(a.get("ai_capped"))
        failing = t.get("panel_failing")
        if failing is not None:
            real = [m for m in failing if m.get("error_kind") != CAPPED_KIND]
            capped.update(m["name"] for m in failing if m.get("error_kind") == CAPPED_KIND)
            failing_count[len(real)] += 1
            for m in real:
                e = members[m["name"]]
                e["files"] += 1
                e["status"][m.get("status")] += 1
                e["error_kind"][str(m.get("error_kind"))] += 1
        if t.get("valid"):
            hard.update(t["valid"].get("hard") or [])
        d = t.get("deviation")
        if d:
            faithful_fail += not d.get("ok")
            dev_reasons.update(d.get("reasons") or [])
            if d.get("used") is not None:
                used.append(float(d["used"]))
                over_bound += float(d["used"]) > 1.0
        if t.get("stages"):
            kept.update(t["stages"].get("kept") or [])
            rolled.update(t["stages"].get("rolled_back") or [])
    return {
        "files": len(rows) + len(excluded), "conclusive": len(conclusive), "clean": len(clean), "not_clean": len(bad),
        "inconclusive_rows": len(rows) - len(conclusive), "excluded": len(excluded),
        "excluded_reasons": _top(Counter(str(x.get("reason", "")).split(":")[0] for x in excluded)),
        "unmeasured_stop_reasons": _top(unmeasured_stop),
        "not_clean_without_triage": len(untriaged),
        "not_clean_by_defect_kind": _top(by_kind) if kinds else None,
        "stop_reasons": _top(stop), "ended_by": _top(ended),
        "open_sites": {"files_with_kind": _top(site_files), "total_sites": site_total},
        "ops": ops.result(), "not_proposed": _top(not_proposed),
        "panel": {"members_failing_per_file": {str(k): v for k, v in sorted(failing_count.items())},
                  "capped_after_doom": _top(capped),
                  "by_member": {name: {"files": e["files"], "status": _top(e["status"]),
                                       "error_kind": _top(e["error_kind"])}
                                for name, e in sorted(members.items(), key=lambda kv: -kv[1]["files"])}},
        "valid_hard": _top(hard),
        "deviation": {"faithful_failed": faithful_fail, "over_bound": over_bound, "reasons": _top(dev_reasons),
                      "budget_used": _quantiles(used)},
        "stages": {"kept": _top(kept), "rolled_back": _top(rolled)} if (kept or rolled) else None,
        "ai": ({**ai, "stop_reasons": _top(ai_stop), "ai_capped": ai_capped} if ai["files"] else None),
    }


def _leads(arms: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The biggest single bucket of each kind, per arm, largest first: where an agent should look next."""
    out: list[dict[str, Any]] = []

    def lead(arm: str, what: str, key: str, files: int, of: int) -> None:
        if files:
            out.append({"arm": arm, "what": what, "key": key, "files": files, "of_not_clean": of})

    for arm, b in arms.items():
        n = b["not_clean"]
        for what, counter in (("stop reason", b["stop_reasons"]), ("open site kind", b["open_sites"]["files_with_kind"]),
                              ("layer-1 hard finding", b["valid_hard"]), ("deviation reason", b["deviation"]["reasons"])):
            if counter:
                key, files = next(iter(counter.items()))
                lead(arm, what, key, files, n)
        for name, m in b["panel"]["by_member"].items():
            kind, files = next(iter(m["error_kind"].items()))
            lead(arm, "panel member still failing", f"{name}: {kind}", files, n)
            break
        for op, o in b["ops"].items():
            for fate in REASONED:
                if o[f"{fate}_reasons"]:
                    reason, cands = next(iter(o[f"{fate}_reasons"].items()))
                    out.append({"arm": arm, "what": f"op {fate}", "key": f"{op}: {reason}", "candidates": cands,
                                "files": o["files"], "of_not_clean": n})
        if b["not_proposed"]:
            key, cands = next(iter(b["not_proposed"].items()))
            out.append({"arm": arm, "what": "det sweep declined to propose", "key": key, "candidates": cands,
                        "files": None, "of_not_clean": n})
    return sorted(out, key=lambda x: -(x["files"] or 0))


def _per_file(rows: list[Any]) -> list[dict[str, Any]]:
    """One line of evidence per NOT-clean conclusive search row: what was open, what each op did, what the verdict
    said about the candidates that were judged. The aggregate says how often; this says which file."""
    out = []
    for r in sorted(rows, key=lambda r: (r.arm, r.name)):
        if r.inconclusive or r.clean or r.arm not in _report.SEARCH_ARMS:
            continue
        t = r.triage or {}
        s = t.get("search") or {}
        ops = {}
        for name, o in (s.get("ops") or {}).items():
            parts = [f"{k} {o[k]}" for k in ("det", "ai", *FATES) if o.get(k)]
            for k in REASONED:
                if o.get(k):
                    parts.append(f"{k} {sum(o[k].values())} ({'; '.join(o[k])})")
            ops[name] = ", ".join(parts)
        out.append({"name": r.name, "arm": r.arm, "stop": (s.get("stop") or {}).get("reason") or r.reason,
                    "open_sites": s.get("open_sites"), "ops": ops, "not_proposed": s.get("not_proposed"),
                    "deltas": [f"{d.get('op')} {d.get('parent')} -> {d.get('score')}" for d in s.get("deltas") or []],
                    "panel_failing": [f"{m['name']}: {m.get('error_kind')}" for m in t.get("panel_failing") or []],
                    "in_out_panel": f"{r.in_panel_passes} -> {r.out_panel_passes}", "triage": bool(r.triage)})
    return out


def triage_wave(rows: Iterable[Any], excluded: Iterable[dict], *, kinds: Mapping[str, str] | None = None,
                arms: Iterable[str] | None = None, source: str = "", complete: bool = True,
                per_file: bool = False) -> dict[str, Any]:
    rows, excluded = list(rows), list(excluded)
    names = list(arms) if arms else sorted({str(r.arm) for r in rows} | {str(x["arm"]) for x in excluded if x.get("arm")})
    blocks = {a: _arm_block([r for r in rows if r.arm == a], [x for x in excluded if x.get("arm") == a], kinds or {})
              for a in names}
    t: dict[str, Any] = {"source": source, "complete_wave": complete,
                         "unit": "files (one row per file and arm) unless 'candidates'", "arms": blocks,
                         "leads": _leads(blocks)}
    if per_file:
        t["per_file"] = _per_file([r for r in rows if r.arm in names])
    return t


# ------------------------------------------------------------------ markdown

def _cell(counter: Mapping[str, int] | None, n: int = 3) -> str:
    if not counter:
        return "-"
    return "; ".join(f"{k} ({v})" for k, v in list(counter.items())[:n]).replace("|", "/")


def render_markdown(t: Mapping[str, Any]) -> str:
    out = [f"# Failure triage: {t['source']}" + ("" if t["complete_wave"] else "  (PARTIAL WAVE: shards missing)"),
           "", f"Counts are {t['unit']}. Only conclusive, NOT clean outputs are explained; inconclusive and excluded "
           "files are counted apart, because nothing about the part made them so.", "", "## Leads (largest first)", "",
           "| arm | what | key | files | of not clean | candidates |", "|---|---|---|---|---|---|"]
    for x in t["leads"][:25]:
        out.append(f"| {x['arm']} | {x['what']} | {str(x['key']).replace('|', '/')} | {x['files'] if x['files'] is not None else '-'} "
                   f"| {x['of_not_clean']} | {x.get('candidates', '-')} |")
    for arm, b in t["arms"].items():
        out += ["", f"## {arm}", "",
                f"files {b['files']}: clean {b['clean']}, NOT clean {b['not_clean']}, inconclusive rows "
                f"{b['inconclusive_rows']}, excluded {b['excluded']} ({_cell(b['excluded_reasons'])})"
                + (f"; {b['not_clean_without_triage']} not-clean row(s) carry NO triage block" if b["not_clean_without_triage"] else "")]
        if b["not_clean_by_defect_kind"]:
            out.append(f"not clean by defect kind: {_cell(b['not_clean_by_defect_kind'], 8)}")
        if b["unmeasured_stop_reasons"]:
            out.append(f"unmeasured searches stopped on: {_cell(b['unmeasured_stop_reasons'])}")
        if b["stop_reasons"]:
            out += ["", f"stop reasons: {_cell(b['stop_reasons'], 8)} (ended by: {_cell(b['ended_by'], 8)})",
                    f"open site kinds at stop (files): {_cell(b['open_sites']['files_with_kind'], 8)}; "
                    f"{b['open_sites']['total_sites']} sites open in all"]
        if b["ops"]:
            out += ["", "| op | files | proposed det | proposed ai | ops charged | evaluated | declined | failed "
                        "| invalid | duplicate | skipped (budget) | top declined | top failed | top invalid |",
                    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for op, o in b["ops"].items():
                out.append(f"| {op} | {o['files']} | {o['det']} | {o['ai']} | {o['ops_applied']} "
                           f"| {o['evaluated']} | {o['declined']} "
                           f"| {o['failed']} | {o['invalid']} | {o['duplicate']} | {o['skipped_budget']} "
                           f"| {_cell(o['declined_reasons'], 1)} | {_cell(o['failed_reasons'], 1)} "
                           f"| {_cell(o['invalid_reasons'], 1)} |")
        if b["not_proposed"]:
            out.append(f"det sweep declined to propose (candidates): {_cell(b['not_proposed'], 6)}")
        if b["panel"]["by_member"]:
            out += ["", f"members failing per file: {b['panel']['members_failing_per_file']}", "",
                    "| panel member | files failing | status | error kinds |", "|---|---|---|---|"]
            for name, m in b["panel"]["by_member"].items():
                out.append(f"| {name} | {m['files']} | {_cell(m['status'])} | {_cell(m['error_kind'], 5)} |")
        if b["panel"].get("capped_after_doom"):
            out += ["", f"capped after doom (the panel had already failed; NOT a mesher defect, not in the table above): "
                        f"{_cell(b['panel']['capped_after_doom'])}"]
        d = b["deviation"]
        q = d["budget_used"]
        out += ["", f"layer-1 hard findings: {_cell(b['valid_hard'], 6)}",
                f"deviation: faithful failed on {d['faithful_failed']}, over the bound on {d['over_bound']}; budget used "
                + ("-" if not q["n"] else f"median {q['median']:.2f}, p90 {q['p90']:.2f}, max {q['max']:.2f} (1.0 = the bound)")
                + f"; reasons {_cell(d['reasons'])}"]
        if b["stages"]:
            out.append(f"stages kept: {_cell(b['stages']['kept'], 10)}; rolled back: {_cell(b['stages']['rolled_back'], 10)}")
        if b["ai"]:
            a = b["ai"]
            out.append(f"AI on {a['files']} not-clean files: {a['turns']} turns, {a['tool_errors']} tool errors "
                       f"({a['candidate_errors']} candidates refused), {a['declared_unfixable']} sites declared "
                       f"unfixable, {a['truncated_rounds']} truncated rounds, ${a['spent_usd']:.2f}; stopped on "
                       f"{_cell(a['stop_reasons'])}; ai_capped {a['ai_capped']}")
    if t.get("per_file"):
        out += ["", "## Per file (not-clean search rows)", "",
                "| file | arm | stop | panel in->out | open sites | ops | deltas | failing members |",
                "|---|---|---|---|---|---|---|---|"]
        for x in t["per_file"]:
            out.append("| " + " | ".join(str(v).replace("|", "/") for v in (
                x["name"][-48:], x["arm"], x["stop"], x["in_out_panel"], _cell(x["open_sites"], 8),
                "; ".join(f"{k}: {v}" for k, v in x["ops"].items()) or "-", "; ".join(x["deltas"]) or "-",
                ", ".join(x["panel_failing"]) or "-")) + " |")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ CLI

def load(root: Path, n_shards: int | None) -> tuple[list[Any], list[dict], bool]:
    shards = _collect.load_shards(root, n_shards, partial_ok=True, parse_rows=_report.parse_arm_rows)
    n = shards[0]["n"]
    complete = {s["i"] for s in shards} == set(range(1, n + 1))
    return [r for s in shards for r in s["rows"]], [x for s in shards for x in s["excluded"]], complete


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="src", required=True, help="directory holding shard-I-of-N/")
    p.add_argument("--shards", type=int, default=None, help="the planned shard count (a partial wave is labelled)")
    p.add_argument("--split", default="", help="the split file, to break 'not clean' down by defect kind")
    p.add_argument("--arms", default="", help="comma list (default: every arm in the rows)")
    p.add_argument("--json", default="")
    p.add_argument("--md", default="")
    p.add_argument("--per-file", action="store_true", help="also list every not-clean search row's evidence")
    args = p.parse_args(argv)
    try:
        rows, excluded, complete = load(Path(args.src), args.shards)
    except (_collect.CollectRefused, _report.ReportRefused) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    kinds = {r.sha256: _report.defect_kind(r) for r in rp.read_split(args.split)} if args.split else None
    t = triage_wave(rows, excluded, kinds=kinds, arms=[a for a in args.arms.split(",") if a] or None,
                    source=str(args.src), complete=complete, per_file=args.per_file)
    text = render_markdown(t)
    if args.json:
        Path(args.json).write_text(json.dumps(t, indent=1, sort_keys=True) + "\n")
    if args.md:
        Path(args.md).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
