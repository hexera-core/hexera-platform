#!/usr/bin/env python3
"""Snapshot v4, after the shards: REGROUP the per-arm groups into one wave per split, then report.py and triage.py
on the merged view, IN the image, with the commit's own code (/w).

    post.py --out <out dir> --splits <splits dir> --manifest <launch_manifest.json>

v1r's post.py took one wave id and one shard count per split. v4 runs one group per arm, so a split's rows are
spread over several group directories with different shard counts. Rather than teach report.py about groups - its
partition and arms checks are what catch a wave cut from two plans, or an arm that silently did not run - regroup.py
presents the groups as ONE wave per split, and every gate below is the same code v1r ran.

A report that REFUSES is still not worked around: the refusal is kept and a SENSITIVITY report is run over the wave
minus exactly the named files, labelled as such, exactly as v1r did. Every exit code lands in <out>/reports/*.rc.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

V4 = Path(os.environ.get("V4_DIR", "/w/devtools/validation/v4_eval"))
HERE = Path(__file__).resolve().parent
REFUSED_FILE = re.compile(r"^REFUSED (.+?) \[(\w+)\]: .*split says", re.M)


def run(cmd: list[str], stem: Path) -> int:
    with open(f"{stem}.txt", "w") as o, open(f"{stem}.err", "w") as e:
        rc = subprocess.call(cmd, stdout=o, stderr=e)
    Path(f"{stem}.rc").write_text(f"{rc}\n")
    return rc


def report(src: Path, wave: str, n: int, split: Path, plan: Path, stem: Path) -> int:
    return run([sys.executable, str(V4 / "report.py"), "--from", str(src), "--wave", wave, "--shards", str(n),
                "--split", str(split), "--shard-plan", str(plan), "--json", f"{stem}.json"], stem)


def triage(src: Path, n: int, split: Path, stem: Path) -> int:
    return run([sys.executable, str(V4 / "triage.py"), "--from", str(src), "--shards", str(n), "--split", str(split),
                "--json", f"{stem}.json", "--md", f"{stem}.md"], stem)


def minus(src: Path, dst: Path, split: Path, names: set[str], split_out: Path) -> None:
    body = json.loads(split.read_text())
    body["files"] = [f for f in body["files"] if f["name"] not in names]
    body["sensitivity_minus"] = sorted(names)
    split_out.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    for s in sorted(src.glob("shard-*")):
        d = dst / s.name
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(s / "PROVENANCE.json", d)
        rows = [r for r in json.loads((s / "rows.json").read_text()) if r["name"] not in names]
        (d / "rows.json").write_text(json.dumps(rows))
        (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        exc = [e for e in json.loads((s / "EXCLUDED.json").read_text()) if e["name"] not in names]
        (d / "EXCLUDED.json").write_text(json.dumps(exc))


def main() -> int:
    p = argparse.ArgumentParser()
    for k in ("out", "splits", "manifest"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--merged-prefix", default="merged")
    a = p.parse_args()
    out, sp = Path(a.out), Path(a.splits)
    rep = out / "reports"
    rep.mkdir(exist_ok=True)
    manifest = json.loads(Path(a.manifest).read_text())

    regroup_rc = run([sys.executable, str(HERE / "regroup.py"), "--out", str(out), "--splits", str(sp),
                      "--manifest", a.manifest, "--merged-prefix", a.merged_prefix], rep / "regroup")
    summary: dict = {"regroup_rc": regroup_rc}
    if regroup_rc != 0:
        # a group that did not finish is NOT reported as a whole wave; the refusal and its evidence stand
        (rep / "post_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
        print(json.dumps(summary, sort_keys=True))
        return regroup_rc
    merged = json.loads((out / "regroup_summary.json").read_text())

    for split, got in sorted(merged.items()):
        wave = manifest["waves"][split]
        src = out / got["merged_dir"]
        n = int(got["shards"])
        plan = sp / got["plan"]
        rc = report(src, wave, n, sp / f"{split}.json", plan, rep / f"report_{split}")
        summary[split] = {"report_rc": rc, "merged": got,
                          "triage_rc": triage(src, n, sp / f"{split}.json", rep / f"triage_{split}")}
        if rc == 1:
            names = {m.group(1) for m in REFUSED_FILE.finditer((rep / f"report_{split}.err").read_text())}
            if names:
                k = len(names)
                dst = out / f"{got['merged_dir']}-minus{k}"
                split_out = rep / f"{split}_minus{k}.json"
                minus(src, dst, sp / f"{split}.json", names, split_out)
                rc2 = report(dst, wave, n, split_out, plan, rep / f"report_{split}_minus{k}")
                summary[split]["sensitivity"] = {"minus": sorted(names), "report_rc": rc2,
                                                 "stem": f"report_{split}_minus{k}"}
    (rep / "post_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
