#!/usr/bin/env python3
"""After the shards: report.py and triage.py for both waves, IN the image, with the commit's own code (/w).

If a report REFUSES because named files' input verdicts disagree with the split (the Task 49 / v1 flip), it is not
worked around: the refusal is kept, and a SENSITIVITY report is also run over the wave minus exactly those files
(`<wave_id>-minus<k>`, split `<split>_minus<k>.json`), labelled as such. Every exit code lands in <out>/reports/*.rc.
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
    for k in ("out", "splits", "dev-wave", "clean-wave", "dev-id", "clean-id"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--dev-n", type=int, required=True)
    p.add_argument("--clean-n", type=int, required=True)
    a = p.parse_args()
    out, sp = Path(a.out), Path(a.splits)
    rep = out / "reports"
    rep.mkdir(exist_ok=True)
    summary: dict = {}
    for split, wave, wid, n in (("dev", a.dev_wave, a.dev_id, a.dev_n), ("clean", a.clean_wave, a.clean_id, a.clean_n)):
        src = out / wid
        rc = report(src, wave, n, sp / f"{split}.json", sp / f"plan_{split}.json", rep / f"report_{split}")
        summary[split] = {"report_rc": rc, "triage_rc": triage(src, n, sp / f"{split}.json", rep / f"triage_{split}")}
        if rc == 1:
            names = {m.group(1) for m in REFUSED_FILE.finditer((rep / f"report_{split}.err").read_text())}
            if names:
                k = len(names)
                dst = out / f"{wid}-minus{k}"
                split_out = rep / f"{split}_minus{k}.json"
                minus(src, dst, sp / f"{split}.json", names, split_out)
                rc2 = report(dst, wave, n, split_out, sp / f"plan_{split}.json", rep / f"report_{split}_minus{k}")
                summary[split]["sensitivity"] = {"minus": sorted(names), "report_rc": rc2,
                                                 "stem": f"report_{split}_minus{k}"}
    (rep / "post_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
