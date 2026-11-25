"""B16: where do search_ai and search_det diverge, and is the AI's edge ACCESS or JUDGEMENT?

Usage:
  python divergence.py OUT_PREFIX --rows 'ROWS_GLOB' [...] --traces 'TRACE_GLOB' [...]

Pairs the two search arms per file (both must be CONCLUSIVE rows), then reads both traces and reports, per file:
  * the start:       whether the two arms started from the same staged state (S0's score and site kinds)
  * first divergence: the first depth at which the two best paths took a different op, with each arm's op, its
                      source (det / ai), the child's CHEAP score, and - decisively - whether the OTHER arm had the
                      same op as a candidate at that state
  * the end:         each arm's confirmed best score, clean, and stop reason, and which path ended better
  * the AI's turn:   every ai-sourced candidate and what became of it (evaluated / duplicate / declined, with the
                      reason), and whether any ai candidate is on the delivered path

ACCESS vs JUDGEMENT, the question the aggregate answers. For each ai-sourced candidate that was EVALUATED:
  ACCESS     no det candidate at that state names that op -> the det sweep could not reach it at all
  JUDGEMENT  det named the same op at that state and the search took something else first -> the AI re-ordered
             work det already had
  DUPLICATE  the AI named a state the search already held: by construction it changed nothing

WHAT THE TRACES GIVE: every candidate event (op, targets, params, source, the proposer's own rationale string,
outcome, the first decline reason, ops_applied), the child's CHEAP score and its parent's score, `confirm` events
with the FULL score of confirmed states, `expand` events with each state's sites (kind, entities,
meshers_failing), the reprompt events (D9: the AI asked again on an empty frontier), and stop.best_path.
WHAT THEY STILL CANNOT: a full-panel verdict for an unconfirmed candidate (cheap only, and no member names in a
score); the op's own measured numbers (a cut's depth, a fill's deviation - those live in the Diff, not the trace);
the staged healer's internal transactions (the row's triage.stages has those); and any counterfactual - whether an
op NOT proposed would have worked. A score is comparable to another only at the same tier, so cheap children are
compared with cheap children and confirmed states with confirmed states.
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import Counter, defaultdict
from typing import Any

ARMS = ("search_det", "search_ai")


def load_rows(patterns: list[str]) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path) as fh:
                for line in fh:
                    if line.strip():
                        r = json.loads(line)
                        if r.get("arm") in ARMS:
                            rows[(r["arm"], r.get("sha256"))] = r
    return rows


def load_traces(patterns: list[str]) -> dict[tuple[str, str], list[dict]]:
    """(arm, sha12) -> events. Matched on the PATH: the arm name and the sha's first 12 hex (B13's naming)."""
    out: dict[tuple[str, str], list[dict]] = {}
    for pat in patterns:
        for path in sorted(glob.glob(pat, recursive=True)):
            arm = next((a for a in ARMS if a in path), None)
            sha12 = next((p[:12] for p in path.replace("/", " ").replace(".", " ").split()
                          if len(p) >= 12 and all(c in "0123456789abcdef" for c in p[:12])), None)
            if arm is None or sha12 is None:
                continue
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            events = data.get("events") if isinstance(data, dict) else data
            if isinstance(events, list):
                out[(arm, sha12)] = events
    return out


class Run:
    """One arm's search over one file, read off its trace."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.stop = next((e for e in reversed(events) if e.get("kind") == "stop"), {})
        self.path: list[str] = list(self.stop.get("best_path") or [])
        self.expand = {e.get("state"): e for e in events if e.get("kind") == "expand"}
        self.confirmed = {e.get("state"): e.get("score") for e in events if e.get("kind") == "confirm"}
        self.reprompts = sum(1 for e in events if e.get("kind") == "reprompt")
        self.cands = [e for e in events if e.get("kind") == "candidate"]
        self.by_state: dict[str, list[dict]] = defaultdict(list)
        for c in self.cands:
            self.by_state[c.get("state")].append(c)
        self.via: dict[str, dict] = {c.get("child"): c for c in self.cands if c.get("child")}

    # --- the path -------------------------------------------------------------------------------
    def step(self, depth: int) -> dict | None:
        """The candidate that produced the path's state at `depth` (depth 0 is the start: no step)."""
        if depth <= 0 or depth >= len(self.path):
            return None
        return self.via.get(self.path[depth])

    def start_shape(self) -> dict:
        e = self.expand.get(self.path[0]) if self.path else {}
        return {"score": (e or {}).get("score"),
                "sites": sorted(str(s.get("kind")) for s in (e or {}).get("sites") or [])}

    def end(self) -> dict:
        best = self.stop.get("best")
        return {"best": best, "score": self.stop.get("best_score"), "clean": bool(self.stop.get("clean")),
                "reason": self.stop.get("reason"), "ended_by": self.stop.get("ended_by"),
                "depth": max(len(self.path) - 1, 0), "spent": self.stop.get("spent")}

    def proposed_at(self, state: str, op: str) -> list[dict]:
        return [c for c in self.by_state.get(state, []) if c.get("op") == op]

    def ops_by_source(self) -> dict[str, set]:
        out: dict[str, set] = defaultdict(set)
        for c in self.cands:
            out[str(c.get("source"))].add(str(c.get("op")))
        return out


def _sig(c: dict | None) -> str:
    if not c:
        return "-"
    params = c.get("params") or {}
    keep = {k: params[k] for k in sorted(params) if k in ("mode", "continuity", "source", "pad_fraction")}
    return f"{c.get('op')}{'' if not keep else json.dumps(keep, sort_keys=True)}"


def _same_op(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and str(a.get("op")) == str(b.get("op"))


def _outcome(c: dict) -> str:
    out = str(c.get("outcome"))
    if out == "rejected":
        errors = c.get("errors") or ["(no reason)"]
        stage = str(c.get("stage") or "failed")
        first = " ".join(str(errors[0]).split())[:80]
        return f"{stage}: {first}"
    return out


def ai_turn(ai: Run) -> dict:
    """Every ai-sourced candidate, what became of it, and - for the evaluated ones - ACCESS or JUDGEMENT."""
    items = []
    verdicts: Counter = Counter()
    for c in ai.cands:
        if c.get("source") != "ai":
            continue
        state, op = c.get("state"), str(c.get("op"))
        det_same = [d for d in ai.proposed_at(str(state), op) if d.get("source") == "det"]
        on_path = c.get("child") in set(ai.path)
        outcome = str(c.get("outcome"))
        if outcome == "evaluated":
            kind = "JUDGEMENT" if det_same else "ACCESS"
        elif outcome == "duplicate":
            kind = "DUPLICATE"
        else:
            kind = "NO-OP(" + _outcome(c).split(":")[0] + ")"
        verdicts[kind] += 1
        items.append({"state": state, "op": _sig(c), "outcome": _outcome(c), "kind": kind,
                      "on_best_path": on_path, "det_also_proposed_here": bool(det_same),
                      "score": c.get("score"), "parent_score": c.get("parent_score"),
                      "rationale": " ".join(str(c.get("rationale") or "").split())[:140]})
    return {"items": items, "verdicts": dict(verdicts.most_common()),
            "ai_ops_on_best_path": [i["op"] for i in items if i["on_best_path"]]}


def compare(det: Run, ai: Run) -> dict:
    same_start = det.start_shape() == ai.start_shape()
    depth = 0
    first = None
    for d in range(1, max(len(det.path), len(ai.path))):
        a, b = det.step(d), ai.step(d)
        if _sig(a) == _sig(b) and _sig(a) != "-":
            depth = d
            continue
        first = {"depth": d,
                 "det": {"op": _sig(a), "source": (a or {}).get("source"), "score": (a or {}).get("score"),
                         "state": det.path[d - 1] if d - 1 < len(det.path) else None,
                         "other_arm_had_this_op_here": bool(a) and bool(
                             ai.proposed_at(ai.path[d - 1], str(a.get("op"))) if d - 1 < len(ai.path) else []),
                         "rationale": " ".join(str((a or {}).get("rationale") or "").split())[:120]},
                 "ai": {"op": _sig(b), "source": (b or {}).get("source"), "score": (b or {}).get("score"),
                        "state": ai.path[d - 1] if d - 1 < len(ai.path) else None,
                        "other_arm_had_this_op_here": bool(b) and bool(
                            det.proposed_at(det.path[d - 1], str(b.get("op"))) if d - 1 < len(det.path) else []),
                        "rationale": " ".join(str((b or {}).get("rationale") or "").split())[:120]}}
        break
    de, ae = det.end(), ai.end()

    def better(x: Any, y: Any) -> str:
        try:
            if list(x) > list(y):
                return "det"
            if list(y) > list(x):
                return "ai"
            return "equal"
        except TypeError:
            return "unscored"
    return {"same_start": same_start, "start_det": det.start_shape(), "start_ai": ai.start_shape(),
            "shared_prefix_depth": depth, "first_divergence": first,
            "end_det": de, "end_ai": ae, "better_end": better(de["score"], ae["score"]),
            "reprompts_ai": ai.reprompts, "ai_turn": ai_turn(ai),
            "ops_only_ai_proposed": sorted(ai.ops_by_source()["ai"] - ai.ops_by_source()["det"]),
            "ops_only_det_proposed_in_ai_arm": sorted(ai.ops_by_source()["det"] - ai.ops_by_source()["ai"]),
            "ops_det_arm_proposed": sorted(det.ops_by_source()["det"])}


def run(rows: dict, traces: dict) -> dict:
    shas = {s for (_a, s) in rows}
    report: dict = {"files": [], "missing": [], "aggregate": {}}
    agg: dict[str, Counter] = {k: Counter() for k in
                               ("better_end", "divergence_depth", "ai_verdicts", "only_ai_ops", "only_det_ops",
                                "clean", "stop_det", "stop_ai")}
    for sha in sorted(shas):
        pair = {a: rows.get((a, sha)) for a in ARMS}
        if not all(pair.values()):
            report["missing"].append({"sha": sha[:12], "have": [a for a in ARMS if pair[a]], "why": "one arm only"})
            continue
        if any(p.get("inconclusive") for p in pair.values()):
            report["missing"].append({"sha": sha[:12], "why": "inconclusive: "
                                      + ",".join(a for a in ARMS if pair[a].get("inconclusive"))})
            continue
        tr = {a: traces.get((a, sha[:12])) for a in ARMS}
        if not all(tr.values()):
            report["missing"].append({"sha": sha[:12], "why": "no trace for "
                                      + ",".join(a for a in ARMS if not tr[a])})
            continue
        got = compare(Run(tr["search_det"]), Run(tr["search_ai"]))
        got["name"] = pair["search_ai"].get("name")
        got["sha"] = sha[:12]
        got["rows"] = {a: {"clean": pair[a].get("out_clean"), "valid": pair[a].get("out_valid"),
                           "panel": f"{pair[a].get('in_panel_passes')}->{pair[a].get('out_panel_passes')}"
                                    f"/{pair[a].get('panel_size')}", "usd": pair[a].get("model_usd"),
                           "wall_s": pair[a].get("wall_s")} for a in ARMS}
        report["files"].append(got)
        agg["better_end"][got["better_end"]] += 1
        agg["divergence_depth"][str((got["first_divergence"] or {}).get("depth", "none"))] += 1
        agg["ai_verdicts"].update(got["ai_turn"]["verdicts"])
        agg["only_ai_ops"].update(got["ops_only_ai_proposed"])
        agg["only_det_ops"].update(got["ops_only_det_proposed_in_ai_arm"])
        agg["clean"][f"det={got['end_det']['clean']},ai={got['end_ai']['clean']}"] += 1
        agg["stop_det"][str(got["end_det"]["reason"])] += 1
        agg["stop_ai"][str(got["end_ai"]["reason"])] += 1
    report["aggregate"] = {k: dict(v.most_common()) for k, v in agg.items()}
    report["aggregate"]["paired_files"] = len(report["files"])
    report["aggregate"]["ai_ops_on_a_delivered_path"] = sum(
        1 for f in report["files"] if f["ai_turn"]["ai_ops_on_best_path"])
    return report


def markdown(rep: dict) -> str:
    a = rep["aggregate"]
    out = ["# B16: search_ai against search_det, per paired file", "",
           f"Paired files (both arms conclusive, both traces present): **{a.get('paired_files', 0)}**. "
           f"Files where an AI-proposed op is on the delivered path: **{a.get('ai_ops_on_a_delivered_path', 0)}**.",
           "", "| file | start same | diverge at | det took | ai took | det had ai's op | better end | "
           "det clean | ai clean | AI candidates |", "|---|---|---|---|---|---|---|---|---|---|"]
    for f in rep["files"]:
        d = f["first_divergence"]
        out.append(
            f"| {str(f['name'] or f['sha'])[-30:]} | {'yes' if f['same_start'] else 'NO'} | "
            f"{(d or {}).get('depth', 'never')} | {(d or {}).get('det', {}).get('op', '-')} | "
            f"{(d or {}).get('ai', {}).get('op', '-')} "
            f"({(d or {}).get('ai', {}).get('source', '-')}) | "
            f"{(d or {}).get('ai', {}).get('other_arm_had_this_op_here', '-')} | {f['better_end']} | "
            f"{f['end_det']['clean']} | {f['end_ai']['clean']} | "
            f"{', '.join(f'{k} {v}' for k, v in f['ai_turn']['verdicts'].items()) or 'none'} |")
    out += ["", "## Aggregate", "",
            f"- better end: {a.get('better_end')}",
            f"- clean pairs (det,ai): {a.get('clean')}",
            f"- first divergence depth: {a.get('divergence_depth')}",
            f"- AI candidate verdicts: {a.get('ai_verdicts')}  (ACCESS = det never named that op at that state; "
            "JUDGEMENT = det named it there too and the search took something else first)",
            f"- ops ONLY the AI proposed: {a.get('only_ai_ops')}",
            f"- ops only det proposed inside the AI arm: {a.get('only_det_ops')}",
            f"- stop reasons det: {a.get('stop_det')}; ai: {a.get('stop_ai')}", ""]
    if rep["missing"]:
        out += [f"Not paired: {len(rep['missing'])} - "
                + "; ".join(f"{m['sha']} {m['why']}" for m in rep["missing"][:12]), ""]
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--rows", nargs="+", required=True)
    p.add_argument("--traces", nargs="+", required=True)
    p.add_argument("--note", default="")
    args = p.parse_args()
    rep = run(load_rows(args.rows), load_traces(args.traces))
    rep["note"] = args.note
    with open(args.out + ".json", "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    md = markdown(rep)
    if args.note:
        md = md.replace("\n\nPaired files", f"\n\n{args.note}\n\nPaired files", 1)
    with open(args.out + ".md", "w") as fh:
        fh.write(md)
    print(f"{rep['aggregate'].get('paired_files', 0)} paired files, {len(rep['missing'])} unpaired -> {args.out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
