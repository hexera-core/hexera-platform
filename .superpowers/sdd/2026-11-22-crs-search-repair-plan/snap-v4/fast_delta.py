#!/usr/bin/env python3
"""FAST-DEV delta: what changed between two fast-loop runs, per (file, arm). stdlib only.

    fast_delta.py --prev <prev run dir> --cur <cur run dir> [--md out.md]

A run dir is a collected snap-v4 run ($RUN, holding out/merged-<split>/shard-*/{rows.json,EXCLUDED.json}). Every
(file, arm) is one of: clean, valid-not-clean (with its passing members), invalid, inconclusive (unmeasured search,
named), excluded (with the reason), or MISSING (no row and no exclusion: a harness gap, always listed).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def state(run: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for sd in sorted(run.glob("out/merged-*/shard-*")):
        for r in json.loads((sd / "rows.json").read_text()) if (sd / "rows.json").exists() else []:
            name = r["name"].split("/")[-1]
            if r.get("inconclusive"):
                st = {"status": "inconclusive", "why": r.get("reason") or "unmeasured"}
            elif r.get("out_clean"):
                st = {"status": "clean"}
            elif r.get("out_valid"):
                st = {"status": "valid-not-clean", "panel": f"{r.get('out_panel_passes')}/{r.get('panel_size')}"}
            else:
                st = {"status": "invalid"}
            failing = [m.get("name") for m in ((r.get("triage") or {}).get("panel_failing") or [])]
            st.update(name=name, failing=failing, in_clean=r.get("in_clean"), usd=r.get("model_usd") or 0.0)
            out[(r["sha256"][:12], r["arm"])] = st
        for e in json.loads((sd / "EXCLUDED.json").read_text()) if (sd / "EXCLUDED.json").exists() else []:
            out.setdefault((e["sha256"][:12], e["arm"]), {"status": "excluded", "why": e["reason"][:100],
                                                         "name": e["name"].split("/")[-1]})
    return out


RANK = {"clean": 3, "valid-not-clean": 2, "invalid": 1, "inconclusive": 0, "excluded": 0}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prev", default="")
    p.add_argument("--cur", required=True)
    p.add_argument("--md", default="")
    a = p.parse_args()
    cur = state(Path(a.cur))
    prev = state(Path(a.prev)) if a.prev else {}
    commit = lambda d: next((ln.split("=", 1)[1] for ln in (Path(d) / "run.env").read_text().splitlines()  # noqa: E731
                             if ln.startswith("COMMIT=")), "?") if d and (Path(d) / "run.env").exists() else "?"
    def walls(d: str) -> str:
        if not d or not (Path(d) / "run.env").exists():
            return "?"
        e = dict(ln.split("=", 1) for ln in (Path(d) / "run.env").read_text().splitlines() if "=" in ln)
        return f"file wall {e.get('FILE_TIMEOUT_S') or 'default'} s, search wall {e.get('SEARCH_WALL_S') or 'default'} s"
    lines = [f"# FAST-DEV v1 delta: {Path(a.cur).name}", "",
             f"**THIS RUN: {walls(a.cur)}.** Previous run: {walls(a.prev) if a.prev else '-'}. "
             "A result at one wall is not the same measurement as at another.", "",
             f"current commit `{commit(a.cur)}`; previous run `{Path(a.prev).name if a.prev else '(none: baseline)'}` "
             f"at `{commit(a.prev)}`", ""]
    keys = sorted(set(cur) | set(prev))
    better = [k for k in keys if k in cur and k in prev and RANK[cur[k]["status"]] > RANK[prev[k]["status"]]]
    worse = [k for k in keys if k in cur and k in prev and RANK[cur[k]["status"]] < RANK[prev[k]["status"]]]
    missing = [k for k in keys if k not in cur]

    def fmt(k: tuple[str, str], d: dict) -> str:
        x = d[k]
        extra = x.get("panel") or x.get("why") or (",".join(x.get("failing") or []) or "")
        return f"`{k[0]}` {x['name'][:40]} [{k[1]}]: **{x['status']}** {extra}"
    counts: dict[str, dict[str, int]] = {}
    for (_, arm), x in cur.items():
        counts.setdefault(arm, {}).setdefault(x["status"], 0)
        counts[arm][x["status"]] += 1
    lines += ["## Now", ""] + [f"- **{arm}**: " + ", ".join(f"{s} {n}" for s, n in sorted(c.items())) for arm, c in
                               sorted(counts.items())]
    lines += ["", f"- model $ (rows): {sum(x.get('usd', 0) for x in cur.values()):.4f}", ""]
    if prev:
        lines += [f"## Improved ({len(better)})", ""] + [f"- {fmt(k, cur)} (was {prev[k]['status']})" for k in better]
        lines += ["", f"## REGRESSED ({len(worse)})", ""] + [f"- {fmt(k, cur)} (was {prev[k]['status']})" for k in worse]
    still = [k for k in sorted(cur) if cur[k]["status"] != "clean" and k[0] and not cur[k].get("in_clean")]
    lines += ["", f"## Still not clean ({len(still)})", ""] + [f"- {fmt(k, cur)}" for k in still]
    lines += ["", f"## MISSING: no row and no exclusion ({len(missing)})", ""] + [f"- `{k[0]}` [{k[1]}]" for k in missing]
    md = "\n".join(lines) + "\n"
    if a.md:
        Path(a.md).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
