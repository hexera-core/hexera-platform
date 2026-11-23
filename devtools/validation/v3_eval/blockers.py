#!/usr/bin/env python3
# Responsibility: Put every failing file of one v3_full wave into exactly ONE blocker bucket (what
#                 stops it from being repaired), and compare two waves bucket by bucket.
# Owns: the bucket order (first match wins), the real-intersection / gap split, the per-bucket edit
#       tally read from the transcripts.
# Boundaries: ANALYSIS ONLY. Reads collect.py's output (rows-v3_full-failing.json and
#             transcripts-v3_full-failing.jsonl) and lane G's clash scan
#             (supporting/blockers/laneG-clash.json). No CAD is read, nothing is meshed, no product
#             code is imported.
"""Blocker buckets for a v3_full wave.

    blockers.py --collect <wave>/collect --out buckets-<wave>.json
    blockers.py --collect <wave4>/collect --compare supporting/blockers/buckets-wave3-17d5f55e.json

"In region" uses lane G's kernel regions plus probe-located faces (a lenient box rule), not the
wave's own region fingerprints. A clash pair is a REAL intersection when its B-rep distance is
<= 1e-6 x the part diagonal; otherwise it is a GAP, reported with its size as a share of the probe
mesh size (diagonal / 40).
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CLASH = HERE / "supporting" / "blockers" / "laneG-clash.json"
REAL = "1 real intersection (in region)"
REAL_UNMEASURED = "1b clash in region, gap unmeasured"


def _edit_tally(transcripts: Path) -> tuple[dict, dict]:
    """Per file: Counter of (op, result) over every edit call, and the regions' tool-call counts."""
    edits: dict = collections.defaultdict(collections.Counter)
    calls: dict = collections.defaultdict(list)
    for line in transcripts.read_text().splitlines():
        t = json.loads(line)
        calls[t["file"]].append(t["spent"]["tool_calls"])
        pending: list = []
        for m in t["messages"]:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn["arguments"]) if fn["arguments"] else {}
                    except (TypeError, ValueError):
                        args = {}
                    pending.append((fn["name"], args))
            elif m.get("role") == "tool" and pending:
                name, args = pending.pop(0)
                if name != "edit" or args.get("reset") is True:
                    continue
                c = json.loads(m["content"]) if isinstance(m["content"], str) else m["content"]
                tr = c.get("tool_result", c)
                d = tr.get("data") or {}
                msg = tr.get("error") or str(d.get("reason") or d.get("status") or "")
                if d.get("kept"):
                    k = "kept"
                elif not tr.get("ok"):
                    k = "refused"
                elif msg.startswith("target_remains"):
                    k = "target_remains"
                elif msg.startswith("declined"):
                    k = "guard"
                elif msg.startswith("cap"):
                    k = "cap"
                else:
                    k = "other"
                edits[t["file"]][(args.get("op"), k)] += 1
    return edits, calls


def bucket(collect: Path, clash_path: Path) -> tuple[dict, dict, dict, list]:
    rows = json.loads((collect / "rows-v3_full-failing.json").read_text())
    clash = json.loads(clash_path.read_text())["files"]
    edits, calls = _edit_tally(collect / "transcripts-v3_full-failing.jsonl")
    out: dict = {}
    sub: dict = collections.Counter()
    ops: dict = collections.defaultdict(collections.Counter)
    gaps: list = []
    for r in rows:
        f = r["file"]
        # Investigator packages live in the outcomes; tournament and rules-author packages only in
        # row.packages (rows from 043101eb on). Read both.
        pkgs = [o["package"] for o in (r.get("investigator") or {}).get("outcomes", [])
                if o.get("package")] + list(r.get("packages") or [])
        kind = r.get("mesh_kind_before") or ""
        if r.get("meshes_after") is True and r.get("meshes_before") is False:
            b = "0 FIXED"
        elif r.get("meshes_before") is True:
            b = "0 not failing at baseline"
        elif "signal" in (r.get("error") or "") or r.get("ending") is None:
            b = "8 crash"
        elif any(p.get("reviewed") == "reject" for p in pkgs):
            b = "5 reviewer reject"
        elif any(p.get("status") in ("reverted", "committed") for p in pkgs):
            b = "6 keep-check revert / kept but still unmeshable"
        elif r.get("ending") == "scan_incomplete" or r.get("stopped_because") == "time_budget":
            b = "7 budget (scan/time, no regions worked)"
        elif kind not in ("overlapping_facets", "edge_through_face"):
            b = "9 other mesh failure: " + (kind or "?")
        elif f not in clash:
            b = "4 overlap/PLC, not in lane G data"
        elif not clash[f]["gclash"]:
            b = "3 clash not located"
        else:
            g = clash[f]
            pairs = g.get("pairs") or []
            diag = g.get("diag_mm") or 0.0
            real = any(d <= 1e-6 * diag for d, _ in pairs)
            inreg = any(v for c in g["gclash"] for v in (c["in_kernel_region"] or []) if v) or \
                any(v for c in g["gclash"] for v in c["in_probe_located"])
            if pairs and not real:
                b = "2 gap below mesh size"
                gaps.append(round(min(d for d, _ in pairs) / (diag / 40.0), 4))
            elif not inreg:
                b = "4 clash faces outside every region"
            else:
                b = REAL if pairs else REAL_UNMEASURED
                e = edits.get(f, collections.Counter())
                tried = {op for op, _ in e}
                regs = calls.get(f, [])
                if regs and sum(1 for x in regs if x == 0) >= len(regs) / 2 and not e:
                    s = "budget (regions starved)"
                elif not e:
                    s = "no edit tried"
                elif tried & {"heal_self_intersections", "rebuild_face"}:
                    s = "cure op tried, failed: " + ",".join(sorted(
                        {k for op, k in e if op in ("heal_self_intersections", "rebuild_face")}))
                else:
                    s = "only other ops tried"
                sub[s] += 1
        out[f] = b
        for key, v in edits.get(f, {}).items():
            ops[b][key] += v
    return out, dict(sub), ops, sorted(gaps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--collect", type=Path, required=True)
    ap.add_argument("--clash", type=Path, default=DEFAULT_CLASH)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", type=Path, help="an earlier wave's --out file")
    a = ap.parse_args()
    buckets, sub, ops, gaps = bucket(a.collect, a.clash)
    now = collections.Counter(buckets.values())
    before = None
    if a.compare:
        prev = json.loads(a.compare.read_text())
        before = collections.Counter(prev.values())
    for k in sorted(set(now) | set(before or {})):
        line = f"{now.get(k, 0):4d}  {k}"
        if before is not None:
            line = f"{before.get(k, 0):4d} -> {line}"
        print(line)
    print("real-intersection sub-buckets:", sub)
    print("gap / mesh size:", gaps)
    for b in sorted(ops):
        if b[0] in "1234":
            print(b, ops[b].most_common(6))
    if before is not None:
        moved = collections.Counter((prev.get(f, "-"), b) for f, b in buckets.items()
                                    if prev.get(f) != b)
        for (x, y), n in moved.most_common():
            print(f"  moved {n}: {x}  ->  {y}")
    if a.out:
        a.out.write_text(json.dumps(buckets, indent=0, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
