"""Build a prep.py --prior-rows dir (<wave>/shard-*/rows.json + EXCLUDED.json) from a wave's shard dirs, taking
rows.json when a shard finished and rows.partial.jsonl when it has not. Only FILES WITH ALL FOUR ARMS are useful to
prep (it costs a file by its four-arm sum), so a partial shard contributes its finished files."""
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
n = 0
for sd in sorted(src.glob("*/shard-*")):
    rows = (json.loads((sd / "rows.json").read_text()) if (sd / "rows.json").exists() else
            [json.loads(l) for l in open(sd / "rows.partial.jsonl") if l.strip()] if (sd / "rows.partial.jsonl").exists() else [])
    exc = json.loads((sd / "EXCLUDED.json").read_text()) if (sd / "EXCLUDED.json").exists() else []
    d = dst / sd.parent.name / sd.name
    d.mkdir(parents=True, exist_ok=True)
    (d / "rows.json").write_text(json.dumps(rows)); (d / "EXCLUDED.json").write_text(json.dumps(exc))
    n += len(rows)
print(f"prior: {n} rows from {src}")
