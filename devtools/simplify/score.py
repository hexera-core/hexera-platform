# Responsibility: Score the cheap verification tiers (T1 one-solve screen, T2 local check) against the T3 ground truth across every part the bench ran, and say how often a cheap tier would have accepted a removal the ground truth says changed the answer.
# Boundaries: a developer tool; it reads harness/local JSON and prints. It runs nothing and changes nothing.
# Collaborates with: devtools/simplify/harness.py (result.json), devtools/simplify/local.py (local_*.json), docs/simplification/DEVLOG.md (where the tables go).
"""HOW GOOD ARE THE CHEAP CHECKS, ACROSS PARTS?

    python devtools/simplify/score.py results2 [--local local_k3_r2.json] [--budget 0.001]

Per part: the noise floor (same part, second mesh), the features with a ground truth, and for the
compliance change dC: T2's rank and linear agreement with T3, sign agreement, the median T2/T3 ratio,
and T1's rank agreement with |T3| (holes). Pooled over all parts: the same, plus the decision table at
the error budget: a removal is ACCEPTABLE when |T3 dC| <= budget. A FALSE ACCEPT is a removal a tier
calls acceptable that T3 does not - the error that matters, because it ships a changed answer.

Only rows whose |T3| clears 3x the part's own noise floor count as measurements for the agreement
statistics; the rest are listed as inside the noise.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 4:
        return None

    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    return _pearson(ranks(a), ranks(b))


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) if sa and sb else None


def _rows(part: Path, local_name: str) -> tuple[dict, list[dict]]:
    h = json.loads((part / "result.json").read_text())
    lp = part / local_name
    L = json.loads(lp.read_text()) if lp.exists() else {}
    noise = abs(h.get("noise", {}).get("error", {}).get("compliance", float("nan")))
    rows = []
    for fid, f in h["features"].items():
        if "error" not in f:
            continue
        loc = L.get(fid, {})
        rows.append({"part": part.name, "id": fid, "kind": f["kind"], "t3": f["error"]["compliance"],
                     "cells": f.get("cells_saved"), "t2": loc.get("est_compliance") if loc.get("ok") else None,
                     "cut": loc.get("cut_mismatch"), "t1": (loc.get("t1") or {}).get("eta_rel")})
    meta = {"part": part.name, "ok": h.get("original", {}).get("ok", False), "noise": noise,
            "features": h.get("n_features", 0), "skipped": sum("skipped" in f for f in h["features"].values()),
            "why": h.get("original", {}).get("why", "")}
    return meta, rows


def _agree(rows: list[dict]) -> dict:
    t2 = [r for r in rows if r["t2"] is not None]
    t1 = [r for r in rows if r["t1"] is not None]
    nz = [r for r in t2 if r["t3"]]
    return {
        "n": len(rows), "n_t2": len(t2),
        "t2_spearman": _spearman([r["t3"] for r in t2], [r["t2"] for r in t2]),
        "t2_pearson": _pearson([r["t3"] for r in t2], [r["t2"] for r in t2]),
        "t2_sign": (sum(math.copysign(1, r["t2"]) == math.copysign(1, r["t3"]) for r in nz) / len(nz)) if nz else None,
        "t2_ratio": statistics.median(r["t2"] / r["t3"] for r in nz) if nz else None,
        "t1_spearman": _spearman([abs(r["t3"]) for r in t1], [r["t1"] for r in t1]),
        "n_t1": len(t1),
    }


def _sci(v) -> str:
    return "n/a" if v is None else f"{v:.2e}"


def _fmt(v, pct=False):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "  n/a"
    return f"{v:+.3%}" if pct else f"{v:.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", help="one or more results directories (each holds part dirs)")
    ap.add_argument("--local", default="local_k3_r2.json")
    ap.add_argument("--budget", type=float, default=0.001, help="acceptable |dC| (default 0.1%%)")
    a = ap.parse_args(argv)
    parts = sorted(p for r in a.results for p in Path(r).iterdir() if (p / "result.json").exists())
    pooled: list[dict] = []
    print(f"{'part':38} {'noise':>8} {'feat':>4} {'meas':>4} {'T2 rho':>6} {'T2 r':>5} {'sign':>5} {'T2/T3':>5} {'T1 rho':>6} {'max|T3|':>8}")
    for p in parts:
        meta, rows = _rows(p, a.local)
        if not meta["ok"]:
            print(f"{meta['part'][:38]:38} original did not solve: {meta['why'][:60]}")
            continue
        floor = 3 * meta["noise"] if not math.isnan(meta["noise"]) else 0.0
        meas = [r for r in rows if abs(r["t3"]) > floor]
        ag = _agree(meas)
        pooled += [dict(r, floor=floor) for r in rows]
        mx = max((abs(r["t3"]) for r in rows), default=float("nan"))
        print(f"{meta['part'][:38]:38} {_fmt(meta['noise'], True):>8} {len(rows):>4} {len(meas):>4} "
              f"{_fmt(ag['t2_spearman']):>6} {_fmt(ag['t2_pearson']):>5} {_fmt(ag['t2_sign']):>5} "
              f"{_fmt(ag['t2_ratio']):>5} {_fmt(ag['t1_spearman']):>6} {_fmt(mx, True):>8}")
    meas = [r for r in pooled if abs(r["t3"]) > r["floor"]]
    ag = _agree(meas)
    print(f"\npooled: {len(pooled)} removals, {len(meas)} above 3x their part's noise floor")
    print(f"  T2 vs T3: spearman {_fmt(ag['t2_spearman'])}, pearson {_fmt(ag['t2_pearson'])}, sign {_fmt(ag['t2_sign'])}, "
          f"median ratio {_fmt(ag['t2_ratio'])} (n={ag['n_t2']})")
    print(f"  T1 vs |T3| (holes): spearman {_fmt(ag['t1_spearman'])} (n={ag['n_t1']})")
    b = a.budget
    judged = [r for r in pooled if r["t2"] is not None]
    fa = [r for r in judged if abs(r["t2"]) <= b < abs(r["t3"])]
    fr = [r for r in judged if abs(r["t3"]) <= b < abs(r["t2"])]
    ok = [r for r in judged if abs(r["t3"]) <= b]
    print(f"\ndecision at budget |dC| <= {b:.2%} (T2 alone, n={len(judged)}): truly acceptable {len(ok)}, "
          f"FALSE ACCEPTS {len(fa)}, false rejects {len(fr)}")
    for r in sorted(fa, key=lambda r: -abs(r["t3"]))[:12]:
        print(f"  false accept: {r['part'][:30]:30} {r['id']:9} {r['kind']:5} T3 {_fmt(r['t3'], True)} "
              f"T2 {_fmt(r['t2'], True)} cut {_fmt(r['cut'])} T1 {_sci(r['t1'])}")
    saved = [r["cells"] for r in ok if r["cells"] is not None]
    if saved:
        print(f"  cells saved by the truly acceptable removals, each alone: median {statistics.median(saved):+.1%}, "
              f"max {max(saved):+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
