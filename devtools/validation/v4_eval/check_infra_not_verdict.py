#!/usr/bin/env python3
# Responsibility: The end-to-end check that closes Task 24: a small REAL dev wave in which one shard has no gmsh, run
#                 through `run_arm --mode harness` and read by `report.load_wave`, asserting that infrastructure is not a
#                 verdict (the dead file's row says inconclusive, it is left out of the clean rate, and input_only's
#                 in_clean equals the split's own verdict).
# Owns: the fixture files (real STEPs), the split truth (a real full-tier verdict per file, gmsh present), the two shards
#       and every assertion. Nothing here is imported by the harness or the report.
# Boundaries: needs the kernel, gmsh and Netgen: run it in the MESH image (the native tier), never on the host. It calls
#             no model, no GCS and no VM. The per-file arm runner is the one thing it supplies itself (input_only, a real
#             `verdict`); once Task 22c's arm dispatch is adapted onto `run_arm.ARM_RUNNER`, pass it with --runner and the
#             same assertions run against the real dispatch.
"""Infrastructure is not a verdict, end to end.

    docker run --rm -v $PWD:/w -w /w meshpipeline-mesh:native bash -lc \\
        'PYTHONPATH=/w/src python devtools/validation/v4_eval/check_infra_not_verdict.py --work /tmp/infra-check'

    --runner pkg.module:factory   use this arm runner instead of the built-in input_only one. `factory(pool_fn)` gets a
                                  zero-argument function returning the shard's WorkerPool and returns an ArmRunner
                                  `(entry, arm, source) -> (row | None, excluded reason | None)`.

What it does, in order:
  0. writes real STEP files (boxes via OCP, plus a real defective ABC part), steering each file's sha256 into the shard it
     is meant for (a header comment; the geometry is untouched);
  1. judges every file with the REAL full-tier verdict, gmsh present: that is the split's own verdict (dev.json);
  2. runs shard 1 of 2 normally and shard 2 of 2 with gmsh made UNIMPORTABLE in the worker processes (a shadow `gmsh.py` that
     raises ImportError), through `run_arm.main(["--mode", "harness", "--plan-provenance", ...])`;
  3. loads the two shards with `report.load_wave` and asserts the outcome. A naive harness that wrote `inconclusive: false`
     for the dead file is also fed to the row contract, and must be refused.
Exit 0 only if every assertion held; 1 and the failing assertion otherwise; 2 if the environment cannot run it.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))

BOX_CODE = """
import sys
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
dx, dy, dz, out = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
w = STEPControl_Writer()
w.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), dx, dy, dz).Shape(), STEPControl_AsIs)
assert w.Write(out) == IFSelect_RetDone
"""
DEFECT = REPO / "tests/fixtures/geometry/abc_overlapping_facets_00500400.step"
SHIM = "raise ImportError('gmsh was removed for the infrastructure check (check_infra_not_verdict.py)')\n"


class CheckFailed(AssertionError):
    pass


def need(cond: bool, what: str) -> None:
    if not cond:
        raise CheckFailed(what)
    print(f"  ok   {what}")


def make_box(path: Path, dims: tuple[float, float, float]) -> bytes:
    done = subprocess.run([sys.executable, "-c", BOX_CODE, *map(str, dims), str(path)], capture_output=True, text=True,
                          timeout=300)
    if done.returncode:
        raise RuntimeError(f"cannot write a box STEP: {done.stderr[-400:]}")
    return path.read_bytes()


def steered(data: bytes, shard: int, n: int, shard_of: Any) -> bytes:
    """`data` with a header comment chosen so its sha256 lands in `shard` of `n` (geometry untouched)."""
    import hashlib

    for k in range(10_000):
        body = data.replace(b"HEADER;", f"/* shard {k} */\nHEADER;".encode(), 1) if k else data
        if shard_of(hashlib.sha256(body).hexdigest(), shard, n):
            return body
    raise RuntimeError("could not steer a file into its shard")


def build_files(work: Path, ev: Any, healthy_boxes: int) -> list[tuple[str, bytes, int]]:
    """(name, bytes, shard) for the real defective part, `healthy_boxes` boxes (shard 1) and ONE box in shard 2."""
    scratch = work / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, bytes, int]] = []
    if not DEFECT.is_file():
        raise FileNotFoundError(DEFECT)
    files.append(("abc_overlapping_facets.step", DEFECT.read_bytes(), 1))
    for i in range(healthy_boxes + 1):
        dims = (10.0 + i, 8.0 + (i % 3), 6.0 + (i % 4))
        files.append((f"box{i}.step", make_box(scratch / f"box{i}.step", dims), 1 if i < healthy_boxes else 2))
    return [(name, steered(data, shard, 2, ev.shard_of), shard) for name, data, shard in files]


def input_only_runner(pool_fn: Any, errors: list[dict]) -> Any:
    """input_only on a real file: the full-tier verdict of the input against itself. A checker that could not judge is an
    INCONCLUSIVE row (no verdict fields), never a conclusive one."""
    from meshpipeline.cad.repair_v4.verdict import verdict
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    def run(entry: Any, arm: str, source: str) -> tuple[dict | None, str | None]:
        base = {"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": arm}
        t0 = time.monotonic()
        try:
            v = verdict(pool_fn(), source, source)
        except CheckerUnavailable as exc:
            errors.append({"name": entry.name, "error": f"CheckerUnavailable: {exc}"})
            return {**base, "inconclusive": True, "model_usd": 0.0, "wall_s": round(time.monotonic() - t0, 3)}, None
        if not v.counts_for_scoreboard:  # a panel member failed for pool reasons: the verdict says nothing
            errors.append({"name": entry.name, "error": "inconclusive verdict"})
            return {**base, "inconclusive": True, "model_usd": 0.0, "wall_s": round(time.monotonic() - t0, 3)}, None
        passes = sum(1 for r in v.panel if r.status == "pass")
        ok = bool(v.faithful is not None and v.faithful.ok)
        return {**base, "inconclusive": False, "model_usd": 0.0, "wall_s": round(time.monotonic() - t0, 3),
                "in_clean": bool(v.clean), "in_panel_passes": passes, "in_valid": bool(v.valid.ok), "in_faithful_ok": ok,
                "out_valid": bool(v.valid.ok), "out_faithful_ok": ok, "out_panel_passes": passes,
                "panel_size": len(v.panel), "out_tier": v.tier, "out_clean": bool(v.clean)}, None

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", default=None, help="a scratch directory (default: a new temp dir)")
    parser.add_argument("--boxes", type=int, default=9, help="healthy box files in shard 1 (plus 1 defective part)")
    parser.add_argument("--runner", default=None, help="pkg.module:factory (see --help text)")
    args = parser.parse_args(argv)
    try:
        import gmsh  # noqa: F401
        import OCP  # noqa: F401  (the fixtures are written with it, in a subprocess)
    except ImportError as exc:
        print(f"cannot run here: {exc}. Run this in the mesh image.", file=sys.stderr)
        return 2
    work = Path(args.work or tempfile.mkdtemp(prefix="infra-check-")).resolve()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    import report
    import run_arm as ra

    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    ev, rp = ra.evalspec, ra.reprobe_mod
    print(f"work dir {work}")
    files = build_files(work, ev, args.boxes)
    inputs, shard_of_name = work / "inputs", {}
    entries = []
    import hashlib

    for name, data, shard in files:
        sha = hashlib.sha256(data).hexdigest()
        (inputs / sha).mkdir(parents=True)
        (inputs / sha / name).write_bytes(data)
        entries.append(ra.pool_mod.PoolEntry(name, "abc_fail", sha, "u", None, "lic"))
        shard_of_name[name] = shard
    need(len(entries) == args.boxes + 2, f"{len(entries)} real STEP files written ({args.boxes} boxes + 1 defective + 1 box for the gmsh-less shard)")
    ra.pool_mod.write_pool(entries, work / "pool.json", sources_included=["abc_fail"])

    # ---- 1. the split's own verdict: the real full-tier checker, gmsh present
    pool = WorkerPool(1)
    try:
        probed: list[Any] = []
        rows = rp.reprobe(entries, tier="full", pool=pool, resolve=lambda e: str(inputs / e.sha256 / e.name),
                          excluded=probed)
    finally:
        pool.close()
    need(not probed and len(rows) == len(entries), f"the control verdict judged all {len(rows)} files (none excluded: {probed})")
    truth = {r.sha256: bool(r.verdict_summary["clean"]) for r in rows}
    need(len(set(truth.values())) == 2, f"the split has both clean ({sum(truth.values())}) and unclean "
         f"({len(truth) - sum(truth.values())}) inputs, so 'in_clean matches the split' cannot hold by accident")
    splits_dir = work / "splits"
    splits_dir.mkdir()
    (splits_dir / "dev.json").write_text(json.dumps({"split": "dev", "files": [asdict(r) for r in rows]}))
    rp.SPLITS_DIR = splits_dir  # run_harness reads <SPLITS_DIR>/<plan split>.json

    # ---- 2. the wave: shard 1 as is, shard 2 with gmsh unimportable
    wave = ev.WaveDef("infra_check", "normal", "dev", ("input_only",))
    plan = ev.plan_wave(wave, wave_id="infra-check-1", n_shards=2, markers=FileMarkerStore(work / "markers"))
    shim = work / "no-gmsh"
    shim.mkdir()
    (shim / "gmsh.py").write_text(SHIM)
    errors: dict[int, list[dict]] = {1: [], 2: []}
    holder: dict[str, Any] = {}

    def pool_fn() -> Any:
        if "pool" not in holder:
            holder["pool"] = WorkerPool(1)
        return holder["pool"]

    saved = os.environ.get("PYTHONPATH", "")
    wave_dir = work / "wave"
    for shard in (1, 2):
        env_path = f"{shim}{os.pathsep}{saved}" if shard == 2 else saved
        os.environ["PYTHONPATH"] = env_path  # the worker is spawned after this: it inherits the shadow gmsh
        if args.runner:
            mod, _, fn = args.runner.partition(":")
            ra.ARM_RUNNER = getattr(importlib.import_module(mod), fn)(pool_fn)
        else:
            ra.ARM_RUNNER = input_only_runner(pool_fn, errors[shard])
        out = wave_dir / f"shard-{shard}-of-2"
        code = ra.main(["--mode", "harness", "--shard", f"{shard}/2", "--inputs", str(inputs), "--out", str(out),
                        "--pool", str(work / "pool.json"), "--sources", "abc_fail", "--commit", "c" * 40,
                        "--image", "img@sha256:" + "d" * 64, *plan.provenance_argv()])
        if "pool" in holder:
            holder.pop("pool").close()
        prov = json.loads((out / "PROVENANCE.json").read_text())
        need(code == 0, f"shard {shard}/2 exited 0 ({prov.get('error', '')[-300:]})")
    os.environ["PYTHONPATH"] = saved

    # ---- 3. what the rows and the report say
    by_shard = {s: json.loads((wave_dir / f"shard-{s}-of-2/rows.json").read_text()) for s in (1, 2)}
    need(all(shard_of_name[r["name"]] == s for s in (1, 2) for r in by_shard[s]), "every file ran in the shard its sha256 assigns it")
    dead = by_shard[2]
    need(len(dead) == 1 and dead[0]["inconclusive"] is True,
         f"the file whose checker had no gmsh reads inconclusive: true ({dead[0] if dead else 'no row'})")
    need(not errors[2] or "gmsh" in errors[2][0]["error"].lower() or "CheckerUnavailable" in errors[2][0]["error"],
         f"... and it was the missing gmsh that made it so ({errors[2]})")
    need({"in_clean", "out_clean", "out_panel_passes"}.isdisjoint(dead[0]), "... and it carries no verdict fields at all")
    live = by_shard[1]
    need(all(r["inconclusive"] is False for r in live), f"the {len(live)} files in the healthy shard all have a verdict")
    need(all(r["in_clean"] == truth[r["sha256"]] for r in live),
         "input_only's in_clean equals the split's own verdict on every judged file")
    need(not ({r["sha256"] for r in live} & {r["sha256"] for r in dead}), "no file is in both shards")

    result = report.load_wave(wave_dir, wave, rp.read_split(splits_dir / "dev.json"), n_shards=2)
    rep = report.build_report(result)
    arm = rep["arms"]["input_only"]
    healthy_clean = sum(truth[r["sha256"]] for r in live)
    dead_truth = truth[dead[0]["sha256"]]
    need(arm["files"] == len(entries) and arm["inconclusive"] == 1 and arm["conclusive"] == len(live),
         f"the report counts 1 inconclusive and {len(live)} conclusive files of {len(entries)}")
    need(arm["measured"] is True, f"1 of {len(entries)} inconclusive is under the {report.MAX_INCONCLUSIVE_FRACTION:.0%} limit: the arm is measured")
    need(arm["clean_out"] == arm["clean_in"] == healthy_clean,
         f"clean count {arm['clean_out']} = the split's clean count over the judged files ({healthy_clean})")
    need(arm["clean_rate"] == healthy_clean / len(live),
         f"the clean rate is {healthy_clean}/{len(live)} (the dead file is EXCLUDED)")
    need(dead_truth is True, "the dead file really is a clean one, so counting it would have changed the rate")
    need(arm["clean_rate"] not in ((healthy_clean + 1) / len(entries), healthy_clean / len(entries)),
         "... neither folded in as clean nor as a conclusive failure over all files")
    live_unclean = [r for r in live if not truth[r["sha256"]]]
    need(bool(live_unclean), "... and an unclean input in the live shard stays unclean and counted")
    need(rep["arms"]["input_only"]["regressions"]["either"] == 0, "no regression is read into the dead file")

    # a harness that wrote the dead file as a conclusive row (the false green this check guards) is refused by the row contract
    naive = {**live[0], "name": dead[0]["name"], "sha256": dead[0]["sha256"]}
    naive = {k: v for k, v in naive.items() if k not in ("in_clean", "out_clean", "out_panel_passes", "in_panel_passes")}
    try:
        report.parse_arm_rows([naive])
    except report.ReportRefused as exc:
        need(True, f"a conclusive row with no verdict (what a naive harness writes) is refused: {str(exc)[:90]}")
    else:
        raise CheckFailed("a conclusive row with no verdict fields was accepted")
    print("\nPASS: infrastructure is not a verdict, end to end")
    print(json.dumps({"files": len(entries), "healthy_clean": healthy_clean, "arm": {k: arm[k] for k in (
        "files", "conclusive", "inconclusive", "measured", "clean_in", "clean_out", "clean_rate")}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailed as failure:
        print(f"\nFAIL: {failure}", file=sys.stderr)
        raise SystemExit(1) from failure
