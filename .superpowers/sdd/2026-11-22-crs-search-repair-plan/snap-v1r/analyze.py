#!/usr/bin/env python3
"""Write docs/benchmarks/crs-v4-dev-snapshot-<label>.md from a collected snapshot run. stdlib only (run with -I).

    analyze.py --run <collected run dir> --doc <out .md> [--prior-report <earlier snapshot's report_dev.json>]

The run dir holds: launch_manifest.json, run.env, vm.json, out/<wave_id>/shard-*/{rows,EXCLUDED,PROVENANCE}.json,
out/reports/*, out/*.rc|start|end, ledger/*.jsonl. Every number is computed from the ROWS, so the tables exist even
when report.py refuses; the reports' own verdicts are quoted next to them.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ARMS = ("input_only", "staged", "search_det", "search_ai")
REPEATED = ("timeout_twice", "timeout_after_hang")
CELLS = ("cheap_pass_full_pass", "cheap_pass_full_fail", "cheap_fail_full_pass", "cheap_fail_full_fail")
DEPTH = re.compile(r"'what': 'overlap_depth', 'value': ([-0-9.eE+]+)(?:, 'bound': ([-0-9.eE+]+))?")


def reason_key(reason: str) -> str:
    """A short, DISTINCT key per kind of exclusion: a deadline stop and an exclusion before it are different facts."""
    if reason.startswith("unmeasured: stopped at the wave deadline"):
        return "deadline stop"
    if reason.startswith("excluded before the stop"):
        return "excluded before the stop (reason not on disk)"
    return reason.split(":")[0]


def deadline_section(out: Path, led_total: float, cap: float) -> str:
    f = out / "deadline_summary.json"
    if not f.exists():
        return ""
    d = json.loads(f.read_text())
    stopped = (out / "DEADLINE_STOP").read_text().strip() if (out / "DEADLINE_STOP").exists() else "?"
    tot = {k: sum(v[k] for v in d.values()) for k in ("rows", "excluded_before_stop", "deadline")}
    when, _, why = stopped.partition("; ")
    why = why or "reaper minus 30 min (deadline guard, coordinator-approved)"
    lines = [f"## STOPPED BEFORE IT FINISHED ({when}): {why}", "",
             f"The wave was stopped before every shard finished. Shards that had not finished were stopped; their rows "
             f"written so far are kept. **{tot['deadline']} (file, arm) pairs were never measured because of the stop** "
             f"(in progress or not started), reported as `unmeasured: stopped at the wave deadline`. "
             f"Separately, **{tot['excluded_before_stop']} (file, arm) pairs were excluded BEFORE the stop**; their exact "
             f"reasons are not on disk (run_arm writes EXCLUDED.json only at shard end), so they are a group of their own. "
             f"A stopped shard has no PROVENANCE.json and none was written, so the official report refuses those shards; the "
             f"tables here are computed from the rows.", "",
             f"**The ledger ${led_total:.4f} is therefore a FLOOR on what the full wave would cost.** It is "
             f"{100 * led_total / cap:.1f}% of the ${cap:.2f} wave cap: money did not bound this wave, wall clock did.", "",
             "| shard | rows kept | excluded before the stop | unmeasured (deadline) | had provenance |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {v['rows']} | {v['excluded_before_stop']} | {v['deadline']} | {v['had_provenance']} |"
              for k, v in sorted(d.items())]
    return "\n".join(lines) + "\n"


def short(name: str) -> str:
    return name.split("/")[-1]


def load_wave(root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    rows, exc, prov = [], [], []
    for s in sorted(root.glob("shard-*")):
        if (s / "rows.json").exists():
            rows += json.loads((s / "rows.json").read_text())
        if (s / "EXCLUDED.json").exists():
            exc += json.loads((s / "EXCLUDED.json").read_text())
        if (s / "PROVENANCE.json").exists():
            prov.append(json.loads((s / "PROVENANCE.json").read_text()))
    return rows, exc, prov


GAMING_VOLUME_CHANGE = 0.25   # report.py: |volume_ratio - 1| above this
GAMING_BBOX_SHRINK = 0.10     # report.py: bbox_ratio below 1 - this


def gaming_why(r: dict) -> list[str]:
    why = []
    v, b = r.get("out_volume_ratio"), r.get("out_bbox_ratio")
    if v is not None and abs(float(v) - 1.0) > GAMING_VOLUME_CHANGE:
        why.append(f"volume x{float(v):.3g}")
    if b is not None and float(b) < 1.0 - GAMING_BBOX_SHRINK:
        why.append(f"bbox x{float(b):.3g}")
    return why


def gaming_section(by: dict) -> str:
    out = []
    for a in ARMS:
        rs = [r for r in by[a].values() if not r["inconclusive"] and r["out_clean"]]
        named = [f"`{short(r['name'])}` ({', '.join(gaming_why(r))})" for r in rs if gaming_why(r)]
        unmeasured = sum(1 for r in rs if r.get("out_volume_ratio") is None and r.get("out_bbox_ratio") is None)
        out.append(f"- **{a}**: {len(named)} of {len(rs)} clean outputs flagged" + (f": {'; '.join(named)}" if named else "")
                   + (f" ({unmeasured} clean output(s) carry no volume/bbox ratio: not measured)" if unmeasured else ""))
    return "\n".join(out)


def depth_of(rows: list[dict]) -> dict[str, tuple[float, float | None]]:
    """sha256 -> (largest overlap depth, its bound) seen in any row's triage (a body op's `exceeds_fidelity`
    decline). A file whose body op never ran has no entry: its depth was never measured."""
    out: dict[str, tuple[float, float | None]] = {}
    for r in rows:
        for m in DEPTH.finditer(json.dumps(r.get("triage") or {})):
            v, b = float(m.group(1)), (float(m.group(2)) if m.group(2) else None)
            if r["sha256"] not in out or v > out[r["sha256"]][0]:
                out[r["sha256"]] = (v, b)
    return out


def has_overlap(r: dict) -> bool:
    return "body_overlap" in (((r.get("triage") or {}).get("valid") or {}).get("hard") or [])


def arm_table(rows: list[dict], exc: list[dict], keep=lambda s: True) -> tuple[str, dict]:
    by: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if keep(r["sha256"]):
            by[r["arm"]][r["sha256"]] = r
    ex: dict[str, list[dict]] = defaultdict(list)
    for e in exc:
        if keep(e["sha256"]):
            ex[e["arm"]].append(e)
    stats = {}
    for a in ARMS:
        rs = list(by[a].values())
        c = [r for r in rs if not r["inconclusive"]]
        stats[a] = {
            "conclusive": len(c), "clean": sum(r["out_clean"] for r in c),
            "repaired": [short(r["name"]) for r in c if not r["in_clean"] and r["out_clean"]],
            "regress": [short(r["name"]) for r in c if r["in_clean"] and not r["out_clean"]],
            "fewer": [f"{short(r['name'])} {r['in_panel_passes']}→{r['out_panel_passes']}" for r in c
                      if r["out_panel_passes"] < r["in_panel_passes"]],
            "more": sum(r["out_panel_passes"] > r["in_panel_passes"] for r in c),
            "inconclusive": len(rs) - len(c),
            "excluded": Counter(reason_key(e["reason"]) for e in ex[a]),
            "usd": sum(float(r.get("model_usd") or 0) for r in rs),
            # NEW definition (2026-11-24): clean = valid + full panel. Cross-checked against the row's own out_clean.
            "vp_mismatch": [short(r["name"]) for r in c
                            if bool(r["out_clean"]) != bool(r["out_valid"] and r["out_panel_passes"] == r["panel_size"])],
            "unfaithful_clean": [short(r["name"]) for r in c if r["out_clean"] and r.get("out_faithful_ok") is False],
            "gaming": [f"{short(r['name'])} ({', '.join(gaming_why(r))})" for r in c if r["out_clean"] and gaming_why(r)]}

    def lst(x: list[str]) -> str:
        return f"{len(x)}" + (f" ({', '.join(x[:4])}{', …' if len(x) > 4 else ''})" if x else "")
    lines = ["| | " + " | ".join(ARMS) + " |", "|---" * (len(ARMS) + 1) + "|"]
    for label, f in (("conclusive rows", lambda s: s["conclusive"]), ("clean (of conclusive)", lambda s: s["clean"]),
                     ("repaired (unclean in, clean out)", lambda s: lst(s["repaired"])),
                     ("regressions clean→unclean", lambda s: lst(s["regress"])),
                     ("fewer panel passes than input", lambda s: lst(s["fewer"])),
                     ("more panel passes than input", lambda s: s["more"]),
                     ("inconclusive rows", lambda s: s["inconclusive"]),
                     ("excluded (reason)", lambda s: ", ".join(f"{k} {v}" for k, v in s["excluded"].items()) or "0"),
                     ("clean but NOT faithful (reported, not gating)", lambda s: lst(s["unfaithful_clean"])),
                     ("clean and gaming-flagged (still clean)", lambda s: lst(s["gaming"])),
                     ("out_clean ≠ valid+panel (should be 0)", lambda s: lst(s["vp_mismatch"])),
                     ("model $ (rows)", lambda s: f"{s['usd']:.4f}")):
        lines.append(f"| {label} | " + " | ".join(str(f(stats[a])) for a in ARMS) + " |")
    return "\n".join(lines), {"by": by, "stats": stats}


def ai_marginal(by: dict) -> str:
    d, a = by["search_det"], by["search_ai"]
    both = [s for s in a if s in d and not a[s]["inconclusive"] and not d[s]["inconclusive"]]
    ai_only = [short(a[s]["name"]) for s in both if a[s]["out_clean"] and not d[s]["out_clean"]]
    det_only = [short(d[s]["name"]) for s in both if d[s]["out_clean"] and not a[s]["out_clean"]]
    panel = [f"{short(a[s]['name'])} det {d[s]['out_panel_passes']} / ai {a[s]['out_panel_passes']}" for s in both
             if d[s]["out_panel_passes"] != a[s]["out_panel_passes"]]
    called = [r for r in a.values() if float(r.get("model_usd") or 0) > 0]
    return "\n".join([
        f"Paired over **{len(both)}** files both search arms judged conclusively.",
        f"- **search_ai cleaned, search_det did not ({len(ai_only)}):** {', '.join(ai_only) or 'none'}",
        f"- **search_det cleaned, search_ai did not ({len(det_only)}):** {', '.join(det_only) or 'none'}",
        f"- **AI marginal = {len(ai_only) - len(det_only):+d} files.**",
        f"- different end panel passes ({len(panel)}): {'; '.join(panel) or 'none'}",
        f"- the model was paid on {len(called)} of {len(a)} search_ai rows, "
        f"${sum(float(r['model_usd']) for r in called):.4f} in rows"])


def repeated_timeouts(by: dict) -> str:
    lines = ["| arm | conclusive | files with a repeated member timeout | members | causes |", "|---|---|---|---|---|"]
    for a in ARMS:
        causes: Counter = Counter()
        files = 0
        conc = [r for r in by[a].values() if not r["inconclusive"]]
        for r in conc:
            hit = [str(m.get("cause")) for m in ((r.get("triage") or {}).get("panel_failing") or [])
                   if isinstance(m, dict) and isinstance(m.get("cause"), str)
                   and (m["cause"] in REPEATED or m["cause"].startswith("after_timeout_"))]
            causes.update(hit)
            files += bool(hit)
        lines.append(f"| {a} | {len(conc)} | {files} | {sum(causes.values())} | "
                     f"{', '.join(f'{k} {v}' for k, v in causes.items()) or '-'} |")
    return "\n".join(lines)


def cheap_full(by: dict) -> str:
    lines = ["| arm | states compared | agree | cheap pass, full fail | cheap fail, full pass | rows without data |",
             "|---|---|---|---|---|---|"]
    for a in ("search_det", "search_ai"):
        cells = dict.fromkeys(CELLS, 0)
        missing = 0
        for r in by[a].values():
            cf = r.get("cheap_full")
            if cf is None:
                missing += not r["inconclusive"]
                continue
            for k in CELLS:
                cells[k] += int(cf.get(k) or 0)
        n = sum(cells.values())
        agree = (cells["cheap_pass_full_pass"] + cells["cheap_fail_full_fail"]) / n if n else None
        lines.append(f"| {a} | {n} | {'-' if agree is None else f'{agree:.1%}'} | {cells['cheap_pass_full_fail']} | "
                     f"{cells['cheap_fail_full_pass']} | {missing} |")
    return "\n".join(lines)


def excluded_group(rows: list[dict], exc: list[dict], wall: float) -> str:
    """Every excluded file as its own group: which arms were excluded, why, and what the other arms said. An excluded
    arm is unmeasured, never a failure; the file is listed here instead of being folded into any rate."""
    if not exc:
        return "None."
    by: dict[str, dict] = defaultdict(lambda: {"name": "", "excluded": {}, "measured": {}})
    for e in exc:
        by[e["sha256"]]["name"] = e["name"]
        by[e["sha256"]]["excluded"][e["arm"]] = e["reason"]
    for r in rows:
        if r["sha256"] in by:
            by[r["sha256"]]["measured"][r["arm"]] = ("clean" if r["out_clean"] else "not clean") + (
                " (inconclusive)" if r["inconclusive"] else "")
    reasons = Counter(reason_key(e["reason"]) for e in exc)
    lines = [f"**{len(by)} file(s), {len(exc)} excluded (file, arm) rows** ({', '.join(f'{k} {v}' for k, v in reasons.items())}). "
             f"File wall {wall:.0f} s.", "",
             "| file | excluded arms (reason) | measured arms |", "|---|---|---|"]
    for _s, g in sorted(by.items(), key=lambda kv: kv[1]["name"]):
        ex = "; ".join(f"{a}: {why[:90]}" for a, why in sorted(g["excluded"].items()))
        me = "; ".join(f"{a}: {v}" for a, v in sorted(g["measured"].items())) or "-"
        lines.append(f"| `{short(g['name'])}` | {ex} | {me} |")
    return "\n".join(lines)


def ledger(root: Path) -> tuple[str, float]:
    lines, total = [], 0.0
    kinds: Counter = Counter()
    for f in sorted(root.glob("*.jsonl")):
        rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        settled = sum(r["u"] for r in rows if r["t"] == "settle") / 1e6
        closed = {r["id"] for r in rows if r["t"] in ("settle", "release")}
        open_ = sum(1 for r in rows if r["t"] == "reserve" and r["id"] not in closed)
        kinds.update(r.get("kind", "?") for r in rows if r["t"] == "settle")
        total += settled
        lines.append(f"`{f.stem}` ${settled:.4f} ({sum(r['t'] == 'settle' for r in rows)} settles, {open_} open)")
    return ("; ".join(lines) + f". Settled calls by kind: {dict(kinds)}."), total


def report_line(rep: Path, stem: str) -> str:
    rc = (rep / f"{stem}.rc").read_text().strip() if (rep / f"{stem}.rc").exists() else "missing"
    head = ""
    if (rep / f"{stem}.txt").exists():
        head = next((x for x in (rep / f"{stem}.txt").read_text().splitlines() if x.startswith("# ")), "")
    err = (rep / f"{stem}.err").read_text() if (rep / f"{stem}.err").exists() else ""
    refusals = [x for x in err.splitlines() if x.startswith("REFUSED")][:5]
    warn = ""
    if (rep / f"{stem}.json").exists():
        w = json.loads((rep / f"{stem}.json").read_text()).get("warnings") or []
        warn = ("; warnings: " + " | ".join(str(x)[:200] for x in w[:3])) if w else ""
    return f"exit **{rc}** {head.lstrip('# ')} {'; '.join(refusals)}{warn}"


def leads(md: Path) -> str:
    if not md.exists():
        return "(triage output missing)"
    text = md.read_text()
    m = re.search(r"## Leads.*?\n\n(\|.*?)(?:\n\n|\Z)", text, re.S)
    return m.group(1) if m else "(no leads table)"


def walls(by: dict, exc: list[dict], wall: float) -> str:
    w: dict[str, dict[str, float]] = defaultdict(dict)
    for a in ARMS:
        for s, r in by[a].items():
            w[a][s] = float(r["wall_s"])
    for e in exc:
        w[e["arm"]][e["sha256"]] = wall
    out = []
    for a in ARMS:
        v = sorted(w[a].values())
        if v:
            out.append(f"{a} {statistics.median(v):.1f} / {statistics.mean(v):.1f} / {v[max(0, int(0.9 * len(v)) - 1)]:.0f} s")
    return "; ".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--doc", required=True)
    p.add_argument("--notes", default="", help="a markdown file placed after the header (rulings, relabels)")
    p.add_argument("--moved", default="", help="comma list of file names moved between splits: reported apart")
    a = p.parse_args()
    run = Path(a.run)
    man = json.loads((run / "launch_manifest.json").read_text())
    env = dict(x.split("=", 1) for x in (run / "run.env").read_text().split("\n") if "=" in x)
    env = {k: v.strip().strip('"') for k, v in env.items()}
    vm = json.loads((run / "vm.json").read_text()) if (run / "vm.json").exists() else {}
    out, rep = run / "out", run / "out" / "reports"
    wall = float(man["file_timeout_s"])
    dev_rows, dev_exc, dev_prov = load_wave(out / env["WAVE_ID_DEV"])
    cl_rows, cl_exc, _ = load_wave(out / env["WAVE_ID_CLEAN"])
    image = next((pr.get("image") for pr in dev_prov if pr.get("image")), "?")
    rcs = {f.stem: f.read_text().strip() for f in sorted(out.glob("*.rc"))}
    retried = (out / "RETRIED").read_text().split() if (out / "RETRIED").exists() else []

    def ts(name: str) -> datetime | None:
        f = out / name
        return datetime.fromisoformat(f.read_text().strip().replace("Z", "+00:00")) if f.exists() else None
    t0, t1 = ts("LAUNCHED"), ts("RUN_DONE")
    wave_min = (t1 - t0).total_seconds() / 60 if t0 and t1 else None
    shard_min = {}
    for f in out.glob("*.start"):
        s, e = ts(f.name), ts(f.stem + ".end")
        if s and e:
            shard_min[f.stem] = round((e - s).total_seconds() / 60, 1)
    led_text, led_total = ledger(run / "ledger")

    headline, H = arm_table(dev_rows, dev_exc)
    inp = {r["sha256"]: r for r in dev_rows if r["arm"] == "input_only"}
    ov_by: dict[str, dict] = {}
    for r in dev_rows:  # the overlap is measured on the INPUT, so any arm's row carries it; input_only's first
        o = (r.get("triage") or {}).get("overlap")
        if isinstance(o, dict) and (r["sha256"] not in ov_by or r["arm"] == "input_only"):
            ov_by[r["sha256"]] = o
    hard_ov = {s for s, r in inp.items() if has_overlap(r)}
    overlap = set(ov_by) | hard_ov
    deep = {s for s in overlap if (ov_by.get(s) or {}).get("within_bound") is False}
    errored = sorted(s for s in overlap if (ov_by.get(s) or {}).get("error"))
    no_row = sorted(s for s in hard_ov if s not in ov_by)
    unknown = sorted(short(inp[s]["name"]) for s in overlap
                     if s not in deep and (ov_by.get(s) or {}).get("within_bound") is None)
    names = {r["sha256"]: r["name"] for r in dev_rows + dev_exc}
    cut_ruled, _ = arm_table(dev_rows, dev_exc, keep=lambda s: s not in deep)
    cut_widest, _ = arm_table(dev_rows, dev_exc, keep=lambda s: s not in overlap)
    clean_tab, C = arm_table(cl_rows, cl_exc)
    def ov_line(s: str) -> str:
        o = ov_by.get(s)
        if o is None:
            return f"- `{short(names.get(s, s))}`: layer-1 body_overlap but NO triage.overlap on its rows (kept)"
        if o.get("error"):
            return f"- `{short(names.get(s, s))}`: overlap.error `{str(o['error'])[:120]}` (kept)"
        d, b = o.get("max_depth_mm"), o.get("bound_mm")
        verdict = "DEEP (left out)" if o.get("within_bound") is False else (
            "within bound (kept)" if o.get("within_bound") else "not decidable (kept)")
        return (f"- `{short(names.get(s, s))}`: max_depth_mm {'-' if d is None else f'{d:.4g}'} ({o.get('method')}) vs "
                f"bound_mm {'-' if b is None else f'{b:.4g}'}, within_bound {o.get('within_bound')} → {verdict}; "
                f"{len(o.get('pairs') or [])} pair(s)")
    ov_lines = [ov_line(s) for s in sorted(overlap, key=lambda s: names.get(s, s))]
    post = json.loads((rep / "post_summary.json").read_text()) if (rep / "post_summary.json").exists() else {}
    sens = []
    for split in ("dev", "clean"):
        s = (post.get(split) or {}).get("sensitivity")
        if s:
            sens.append(f"| {split} minus {len(s['minus'])} refused file(s) ({', '.join(short(x) for x in s['minus'])}) "
                        f"(sensitivity, NOT a split) | {report_line(rep, s['stem'])} |")
    wd = man["waves"]
    notes = Path(a.notes).read_text().strip() + "\n" if a.notes else ""
    moved_md = ""
    for name in [x for x in a.moved.split(",") if x]:
        rs = sorted((r for r in dev_rows + cl_rows if r["name"] == name), key=lambda r: ARMS.index(r["arm"]))
        ex = [e for e in dev_exc + cl_exc if e["name"] == name]
        moved_md += (f"\n### Moved file `{short(name)}` (reported apart: not a repair)\n\n| arm | in clean | out clean | "
                     "panel in→out | inconclusive | model $ |\n|---|---|---|---|---|---|\n" + "".join(
                         f"| {r['arm']} | {r['in_clean']} | {r['out_clean']} | {r['in_panel_passes']}→{r['out_panel_passes']} | "
                         f"{r['inconclusive']} | {float(r.get('model_usd') or 0):.4f} |\n" for r in rs)
                     + "".join(f"| {e['arm']} | excluded: {e['reason'][:80]} | | | | |\n" for e in ex))
    doc = f"""# CRS v4 dev snapshot {env['LABEL']} (PROVISIONAL)

**PROVISIONAL.** {len(inp)} dev + {len({r['sha256'] for r in cl_rows} | {e['sha256'] for e in cl_exc})} clean files. Written by `snap-v1r/analyze.py` from the collected run; every table is computed from the rows.

| | |
|---|---|
| Code | `{env['COMMIT']}` (resolved by launch.sh from `{env.get('COMMIT_ARG', '?')}`) |
| Image | `{env.get('IMAGE_REF', '?')}`, local id `{image}`. Label `org.opencontainers.image.revision` = `{env.get('IMAGE_COMMIT', '?')}`: the image's MESH BUILD (toolchain) is that commit; the RUN commit above is the code bind-mounted at `/w`, and the two may differ |
| VM | `{vm.get('name', '?')}`, {env['MACHINE']}, {vm.get('zone', '?')}, reaper max-run {vm.get('max_run', '?')} → DELETE; created {vm.get('created', '?')}, deleted {vm.get('deleted', '?')} |
| Splits | dev `{man['splits']['dev']['path']}` (sha256 `{man['splits']['dev']['sha256'][:16]}…`, {man['splits']['dev']['files']} files); clean `{man['splits']['clean']['path']}` (sha256 `{man['splits']['clean']['sha256'][:16]}…`, {man['splits']['clean']['files']} files) |
| Waves | `{wd['dev']['wave']}` ({wd['dev']['n_shards']} shards, files {wd['dev']['files_per_shard']}), `{wd['clean']['wave']}` ({wd['clean']['n_shards']} shards, files {wd['clean']['files_per_shard']}); cost-balanced plans (digests `{wd['dev']['plan_digest'][:12]}`, `{wd['clean']['plan_digest'][:12]}`) |
| Flags | `--ops {env['OPS']}` (APPLIED ops), `--file-usd-cap {env['FILE_USD_CAP']}`, `--route {env['ROUTE']}`, `--h-mm auto`, `--workers 1`, file wall {wall:.0f} s (the commit's default), verdict cache `{env['CACHE']}` (fresh) |
| Model caps | dev {wd['dev']['caps_usd']}, clean {wd['clean']['caps_usd']}: total **${man['total_caps_usd']}** |
| Shard exits | {', '.join(f'{k}={v}' for k, v in rcs.items())}{'; retried once after a probe refusal: ' + ', '.join(retried) if retried else ''} |

{notes}
{deadline_section(out, led_total, float(man['total_caps_usd']))}
## Official reports

| report | result |
|---|---|
| dev | {report_line(rep, 'report_dev')} |
| clean | {report_line(rep, 'report_clean')} |
{chr(10).join(sens)}

## Per-arm, dev: body-overlap files counted as failures (headline)

{headline}

## Per-arm, dev: deep body-overlap files left out of the denominator (owner ruling)

Files whose INPUT has a body overlap ({len(overlap)}), from `triage.overlap` (measured on the input: max_depth_mm = the worst pair's cheaper cut, its method, bound_mm, within_bound). overlap.error: {len(errored)}; layer-1 body_overlap with no triage.overlap: {len(no_row)}.

{chr(10).join(ov_lines) or '- none'}

Left out as deep (within_bound false): **{len(deep)}**. Not decidable and kept in: **{len(unknown)}**{' (' + ', '.join(unknown) + ')' if unknown else ''}.

{cut_ruled}

Widest version, for reference: every body-overlap input left out ({len(overlap)} files):

{cut_widest}

## Clean control

{clean_tab}

{moved_md}

## Excluded files, each its own group (not in any rate; the ruling on the exclusion rate is pending)

Dev:

{excluded_group(dev_rows, dev_exc, wall)}

Clean control:

{excluded_group(cl_rows, cl_exc, wall)}

## Gaming flags (clean outputs that moved the part: |volume ratio − 1| > 0.25 or bbox ratio < 0.90; STILL counted clean)

Dev:

{gaming_section(H['by'])}

Clean control:

{gaming_section(C['by'])}

## AI marginal (search_ai − search_det, per file)

{ai_marginal(H['by'])}

## Repeated member timeouts (counted as file failures; lead ruling D5)

{repeated_timeouts(H['by'])}

Clean control:

{repeated_timeouts(C['by'])}

## Cheap/full agreement (search ranking tier vs full panel, confirmed states)

{cheap_full(H['by'])}

## Triage (triage.py, dev): leads

{leads(rep / 'triage_dev.md')}

Full tables: `reports/triage_dev.md`, `reports/triage_clean.md`.

## Money and time

- **Ledger ${led_total:.4f}** (route probes are metered in the shard ledgers on this code). {led_text}
- **Wave wall {f'{wave_min:.1f}' if wave_min is not None else '?'} min** (launch to last shard). Shard minutes: {', '.join(f'{k} {v}' for k, v in sorted(shard_min.items()))}.
- Per-file arm wall, dev (excluded at the {wall:.0f} s wall), median / mean / p90: {walls(H['by'], dev_exc, wall)}.
- VM ~{vm.get('hours', '?')} h ≈ ${vm.get('usd', '?')}.

## Caveats

1. PROVISIONAL split ({man['splits']['dev']['files']} dev files); small and stratified by source, not by defect.
2. The checker's run-to-run behaviour is still under Lane D's investigation; single-file differences are not effects.
3. The image's mesh build is commit `{env.get('IMAGE_COMMIT', '?')}` (its registry label, checked before launch); the run commit is `{env['COMMIT']}`. The code under test is the run commit, bind-mounted; the image supplies only the toolchain.
4. Rows past the file wall are excluded (288755c2); excluded files are unmeasured, not failures.
5. Overlap depth is triage.overlap, measured on each input; files with overlap.error or no overlap row are listed above.
"""
    Path(a.doc).write_text(doc)
    print(f"wrote {a.doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
