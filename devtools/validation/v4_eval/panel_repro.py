#!/usr/bin/env python3
# Responsibility: Is the panel reproducible? Run every panel member on the same files, in a FRESH process per run,
#                 on BOTH forms the eval judges: the original STEP (what the re-probe meshes) and the STEP re-exported
#                 from the workspace BREP (what every arm meshes, `search._judged` -> `stages.step_for`).
# Owns: the per-run record (status, min SICN, elements, error kind per member per form) and the comparison.
# Boundaries: no cache (every run meshes), no model, no cloud. It changes no verdict logic.
"""Run inside the mesh image with PYTHONPATH=<checkout>/src:

    panel_repro.py run --files near.json --inputs DIR --out runs/run-3.jsonl     # one run, one fresh process
    panel_repro.py compare runs/run-*.jsonl                                      # identical across runs?
    panel_repro.py agree --files near.json --inputs DIR --out agree.jsonl       # re-probe row == arms' input?
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _members() -> list:
    from meshpipeline.cad.repair_v4.verdict.assemble import default_members

    return default_members("full")


def _judge_form(pool, step: str, timeout_s: float) -> list[dict]:
    out = []
    for m in _members():
        t0 = time.monotonic()
        r = m.run(pool, step, timeout_s=timeout_s)
        out.append({"member": r.name, "status": r.status, "error_kind": r.error_kind,
                    "min_sicn": r.min_sicn, "elements": r.elements, "seconds": round(time.monotonic() - t0, 2)})
    return out


def cmd_run(args: argparse.Namespace) -> int:
    import arms

    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    files = json.loads(Path(args.files).read_text())
    # a loaded host can take >30 s to start a kernel worker (Task 44's flake); start-up time is not a verdict
    pool = WorkerPool(1, start_timeout_s=args.start_timeout_s)
    env = {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OMP_PROC_BIND", "MKL_NUM_THREADS")}
    try:
        with Path(args.out).open("w") as handle:
            for f in files:
                step = str(next((Path(args.inputs) / f["sha256"]).iterdir()))
                rec = {"sha256": f["sha256"], "name": f["name"], "pid": os.getpid(), "env": env}
                rec["original"] = _judge_form(pool, step, args.timeout_s)
                with tempfile.TemporaryDirectory(prefix="repro_ws_") as wd:
                    ws = arms.RealEngine().open_workspace(pool, step, wd)
                    rec["round_trip"] = _judge_form(pool, step_for(pool, ws.brep_path), args.timeout_s)
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
                handle.flush()
                print(f["name"][-50:], [(m["member"], m["status"], m["min_sicn"]) for m in rec["original"]],
                      [(m["member"], m["status"], m["min_sicn"]) for m in rec["round_trip"]], flush=True)
    finally:
        pool.close()
    return 0


def cmd_agree(args: argparse.Namespace) -> int:
    """The re-probe's verdict (what labels a split) against the input verdict every arm computes
    (`RealEngine.open_workspace` -> `default_judge(...).full`): the same per-member status and min SICN, and the same
    `clean`, on every file - or the split label and the arm disagree about the same file."""
    import arms
    import reprobe as rp

    from meshpipeline.cad.repair_v4.search import default_judge
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    files = json.loads(Path(args.files).read_text())
    pool = WorkerPool(1, start_timeout_s=args.start_timeout_s)
    disagree = 0

    class Entry:
        def __init__(self, f: dict) -> None:
            self.name, self.sha256, self.source, self.group = f["name"], f["sha256"], "abc_fail", None

    try:
        with Path(args.out).open("w") as handle:
            for f in files:
                step = str(next((Path(args.inputs) / f["sha256"]).iterdir()))
                def here(_entry: object, path: str = step) -> str:
                    return path

                [row] = rp.reprobe([Entry(f)], tier="full", pool=pool, resolve=here)
                with tempfile.TemporaryDirectory(prefix="agree_ws_") as wd:
                    ws = arms.RealEngine().open_workspace(pool, step, wd)
                    v = default_judge(h_mm=1.0).full(pool, ws, step)
                probe = [(m["name"], m["status"], m["min_sicn"]) for m in row.verdict_summary["panel"]]
                arm = [(m.name, m.status, m.min_sicn) for m in v.panel]
                same = probe == arm and bool(row.verdict_summary["clean"]) == bool(v.clean)
                disagree += not same
                rec = {"name": f["name"], "same": same, "reprobe": probe, "arm_input": arm,
                       "reprobe_clean": row.verdict_summary["clean"], "arm_clean": bool(v.clean)}
                handle.write(json.dumps(rec) + "\n")
                handle.flush()
                print(("SAME " if same else "DIFFERS ") + f["name"], flush=True)
    finally:
        pool.close()
    print(f"{len(files) - disagree} of {len(files)} files: the re-probe and the arms' input verdict agree")
    return 1 if disagree else 0


def compare(paths: list[str]) -> dict:
    """{file: {form: {member: sorted distinct (status, min_sicn, elements)}}} plus the files/forms that vary."""
    seen: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for p in paths:
        for line in Path(p).read_text().splitlines():
            rec = json.loads(line)
            for form in ("original", "round_trip"):
                for m in rec[form]:
                    seen[rec["name"]][form][m["member"]].add((m["status"], m["min_sicn"], m["elements"]))
    varies = sorted((name, form, member) for name, forms in seen.items() for form, members in forms.items()
                    for member, outs in members.items() if len(outs) > 1)
    status_varies = sorted((name, form, member) for name, forms in seen.items() for form, members in forms.items()
                           for member, outs in members.items() if len({o[0] for o in outs}) > 1)
    form_differs = sorted((name, member) for name, forms in seen.items() for member in forms["original"]
                          if {o[0] for o in forms["original"][member]} != {o[0] for o in forms["round_trip"][member]})
    return {"runs": len(paths), "files": len(seen), "varies_across_runs": varies,
            "status_varies_across_runs": status_varies, "original_vs_round_trip_status_differs": form_differs,
            "detail": {n: {f: {m: sorted(map(list, o)) for m, o in ms.items()} for f, ms in fs.items()}
                       for n, fs in seen.items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--files", required=True)
    r.add_argument("--inputs", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--timeout-s", type=float, default=300.0)
    r.add_argument("--start-timeout-s", type=float, default=600.0)
    a = sub.add_parser("agree")
    a.add_argument("--files", required=True)
    a.add_argument("--inputs", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--start-timeout-s", type=float, default=600.0)
    c = sub.add_parser("compare")
    c.add_argument("paths", nargs="+")
    c.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "agree":
        return cmd_agree(args)
    doc = compare(args.paths)
    text = json.dumps({k: v for k, v in doc.items() if k != "detail"}, indent=1)
    print(text)
    if args.json:
        Path(args.json).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return 1 if doc["status_varies_across_runs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
