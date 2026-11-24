#!/bin/bash
# CONTROLLED STOP before the reaper (run ON the VM, only when the coordinator says so). Keeps every measured row:
# stops the driver and the shards, turns each unfinished shard's rows.partial.jsonl into rows.json, lists every
# (file, arm) with no row in EXCLUDED.json as `unmeasured: stopped at the wave deadline`, runs report/triage in the
# image, then writes ALLDONE so the launcher collects, deletes the VM and writes the doc as usual. Arms that were
# excluded BEFORE the stop keep their own group (reason inferred from timing, stated as such); no provenance is written.
set -u
T=$HOME/snap; source $T/run.env
date -u +%FT%TZ > $T/out/DEADLINE_STOP
pkill -f 'tools/vm_run.sh' ; sleep 1
sudo docker ps --format '{{.Names}}' | grep '^snap-' | xargs -r sudo docker stop -t 20 > $T/out/deadline_stopped.txt
python3 - "$T" <<'PY'
import json, sys
from pathlib import Path
T = Path(sys.argv[1]); out = T / "out"; sp = T / "splits"
env = dict(l.split("=", 1) for l in (T / "run.env").read_text().splitlines() if "=" in l)
ARMS = ("input_only", "staged", "search_det", "search_ai")
DEADLINE = "unmeasured: stopped at the wave deadline (this arm was in progress or never started)"
summary = {}
for split, wid in (("dev", env["WAVE_ID_DEV"]), ("clean", env["WAVE_ID_CLEAN"])):
    plan = json.loads((sp / f"plan_{split}.json").read_text())
    files = {f["sha256"]: f for f in json.loads((sp / f"{split}.json").read_text())["files"]}
    for sd in sorted((out / wid).glob("shard-*")):
        if (sd / "rows.json").exists():
            continue                      # finished normally: its own rows, EXCLUDED.json and PROVENANCE.json stand
        i = int(sd.name.split("-")[1])
        mine = [s for s, j in plan["assign"].items() if int(j) == i]
        rows = [json.loads(l) for l in open(sd / "rows.partial.jsonl") if l.strip()] if (sd / "rows.partial.jsonl").exists() else []
        have = {(r["sha256"], r["arm"]) for r in rows}
        # which (file, arm) STARTED (its work dir exists) and their order: arms run one at a time, so the order of the
        # dirs' last writes is the order they ran in; the newest is the one in progress at the stop
        started = {}
        for s in mine:
            for a in ARMS:
                d = sd / "work" / s[:12] / a
                if d.is_dir():
                    started[(s, a)] = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()])
        order = sorted(started, key=started.get)
        current = order[-1] if order else None
        exc, groups = [], {"excluded_before_stop": 0, "deadline": 0}
        for s in mine:
            for a in ARMS:
                if (s, a) in have:
                    continue
                rec = {"name": files[s]["name"], "sha256": s, "source": files[s].get("source"), "arm": a}
                if (s, a) in started and (s, a) != current:
                    rec["reason"] = ("excluded before the stop; the exact reason is NOT on disk (run_arm writes "
                                     "EXCLUDED.json only at shard end): this arm started and a later arm of the shard "
                                     "started after it, with no row - a file-wall timeout or an excluding error")
                    groups["excluded_before_stop"] += 1
                else:
                    rec["reason"] = DEADLINE
                    groups["deadline"] += 1
                exc.append(rec)
        (sd / "rows.json").write_text(json.dumps(rows))
        (sd / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (sd / "EXCLUDED.json").write_text(json.dumps(exc))
        # NO provenance is written: a stopped shard has none, and the official report will refuse it (as it should)
        (sd / "DEADLINE_STOPPED").write_text(json.dumps({"rows": len(rows), **groups,
                                                          "current_at_stop": None if current is None else
                                                          {"sha12": current[0][:12], "arm": current[1]}}) + "\n")
        summary[f"{wid}/{sd.name}"] = {"rows": len(rows), **groups,
                                       "had_provenance": (sd / "PROVENANCE.json").exists()}
(out / "deadline_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
print(json.dumps(summary))
PY
date -u +%FT%TZ > $T/out/RUN_DONE
shred -u $T/secret.env 2>/dev/null
sudo docker run --rm -v $T/code:/w:ro -v $T/tools:/tools:ro -v $T/splits:/eval/splits:ro -v $T/out:/eval/out \
  -w /w -e PYTHONPATH=/w/src meshpipeline-mesh:native python /tools/post.py --out /eval/out --splits /eval/splits \
  --dev-wave $DEV_WAVE --clean-wave $CLEAN_WAVE --dev-id $WAVE_ID_DEV --clean-id $WAVE_ID_CLEAN \
  --dev-n $DEV_N --clean-n $CLEAN_N > $T/out/post.log 2>&1
echo $? > $T/out/post.rc
date -u +%FT%TZ > $T/out/ALLDONE
