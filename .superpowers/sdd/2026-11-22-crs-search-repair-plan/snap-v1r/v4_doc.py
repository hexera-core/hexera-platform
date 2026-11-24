#!/usr/bin/env python3
"""Write a snapshot doc for a TWO-REGION (snap-v4) run with the same analyze.py every earlier wave used.

    v4_doc.py --run <snap-v4 run dir> --doc <out .md> [--notes notes.md] [--baseline-run DIR --baseline-label L]

snap-v4 writes per-arm group waves and REGROUPS them into out/merged-<split>/ (one wave per split, the gates of
report.py intact). analyze.py reads one wave per split from run.env's WAVE_ID_DEV/CLEAN and a v1r-shaped manifest; this
builds exactly that view in <run>/docview/ (out/ and ledger/ are symlinks: nothing is copied or rewritten), fetches the
shard ledgers from the sync prefix (the v4 launcher collects out/ only), then runs analyze.py. stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def env_of(path: Path) -> dict[str, str]:
    return {k: v.strip().strip('"') for k, v in (ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--doc", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--baseline-run", default="")
    p.add_argument("--baseline-label", default="baseline")
    a = p.parse_args()
    run = Path(a.run)
    man = json.loads((run / "launch_manifest.json").read_text())
    env = env_of(run / "run.env")
    regroup = json.loads((run / "out" / "regroup_summary.json").read_text())
    view = run / "docview"
    view.mkdir(exist_ok=True)
    for name in ("out", "ledger"):
        link = view / name
        if not link.exists():
            os.symlink(run / name, link)
    if not (run / "ledger").exists() or not any((run / "ledger").iterdir()):
        (run / "ledger").mkdir(exist_ok=True)
        subprocess.call(["gcloud", "storage", "rsync", "-r", "-q", env["SYNC"].replace("/out", "") + "/ledger",
                         str(run / "ledger")])
    waves = {}
    for split in ("dev", "clean"):
        model_groups = [g for g in man["groups"].values() if g["split"] == split and any("ai" in x for x in g["arms"])]
        got = regroup[split]
        waves[split] = {"wave": man["waves"][split], "n_shards": got["shards"],
                        "files_per_shard": f"merged from groups {got.get('groups')}",
                        "plan_digest": got["plan_digest"],
                        "caps_usd": [c for g in model_groups for c in g.get("caps_usd", [])]}
    view_man = {"file_timeout_s": man["file_timeout_s"], "splits": man["splits"], "waves": waves,
                "total_caps_usd": round(sum(sum(w["caps_usd"]) for w in waves.values()), 2)}
    (view / "launch_manifest.json").write_text(json.dumps(view_man, indent=1))
    vms = man.get("vms", [])
    machines = ", ".join(f"{v['name']} ({v['machine']}, {v['region']})" for v in vms)
    (view / "run.env").write_text("\n".join([
        f"LABEL={env.get('LABEL')}", f"COMMIT={env.get('COMMIT')}", f"COMMIT_ARG={env.get('COMMIT')}",
        f"IMAGE_REF={env.get('IMAGE_REF')}", "IMAGE_COMMIT=1d6ed735f7284609a269ca314c15ff6b0e359d4b",
        f"MACHINE={len(vms)} VMs: {machines}", f"OPS={env.get('OPS') or '(the commit default)'}",
        f"FILE_USD_CAP={env.get('FILE_USD_CAP')}", f"ROUTE={env.get('ROUTE')}", f"CACHE={env.get('CACHE')}",
        f"WAVE_ID_DEV={regroup['dev']['merged_dir']}", f"WAVE_ID_CLEAN={regroup['clean']['merged_dir']}"]) + "\n")
    (view / "vm.json").write_text(json.dumps({"name": f"{len(vms)} VMs", "zone": "two-region", "vms": vms}))
    cmd = [sys.executable, "-I", str(HERE / "analyze.py"), "--run", str(view), "--doc", a.doc]
    if a.notes:
        cmd += ["--notes", a.notes]
    if a.baseline_run:
        cmd += ["--baseline-run", a.baseline_run, "--baseline-label", a.baseline_label]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
