"""D21: the oracle sweep. For each failing file: which deterministic op/params, 1 or 2 steps deep, makes it clean or
adds full-panel mesher passes - and which files nothing touches. No AI. Design: lane-d21-oracle-design.md.

    oracle.py shard   --manifest M.json --shard I/N --inputs DIR --out DIR [--parallel P] [--file-wall-s S]
    oracle.py file    --entry-json '{...}' --inputs DIR --out DIR [--file-wall-s S]   (one file, parent)
    oracle.py one     --entry-json '{...}' --inputs DIR --out DIR                     (one file, the child)
    oracle.py collect --manifest M.json --rows DIR --out TABLE.jsonl                   (refuses unless N rows)

Scars designed in: (1) N inputs -> N rows or the collector refuses; a killed or crashed file is still a row.
(2) The PARENT kills the child at the file wall; candidates are streamed to JSONL so a kill keeps its evidence.
(3) Every candidate's own output is measured against the ORIGINAL (layer 1 before -> after, volume, tight box), and
size-relative thresholds use the lineage's ORIGINAL diagonal. (4) Where it runs is the launcher's business.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FILE_WALL_S = 7200.0
OP_TIMEOUT_S = 300.0
DEPTH2_PARENTS = 6
MAX_SITES = 6
#: the owner's gaming rule: a clean result with more volume change or more bbox shrink than this is flagged
GAMING_VOLUME = 0.25
GAMING_BBOX_SHRINK = 0.10
ROW_SUFFIX = ".row.json"


# ------------------------------------------------------------------ the op's own output

def measure(brep_path: str) -> dict:
    """Volume, TIGHT bounding box (no tolerance padding: `AddOptimal`), solid and face counts, in-process."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Builder
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, brep_path, BRep_Builder())

    def count(kind: Any) -> int:
        n, e = 0, TopExp_Explorer(shape, kind)
        while e.More():
            n, _ = n + 1, e.Next()
        return n

    props = GProp_GProps()
    try:
        BRepGProp.VolumeProperties_s(shape, props)
        volume = abs(float(props.Mass()))
    except Exception:  # noqa: BLE001
        volume = None
    box = Bnd_Box()
    try:
        BRepBndLib.AddOptimal_s(shape, box, False, False)
        x0, y0, z0, x1, y1, z1 = box.Get()
        extent = [x1 - x0, y1 - y0, z1 - z0]
        diag = math.sqrt(sum(v * v for v in extent))
    except Exception:  # noqa: BLE001
        extent, diag = None, None
    return {"volume": volume, "extent": extent, "diag": diag, "solids": count(TopAbs_SOLID), "faces": count(TopAbs_FACE)}


def against(now: dict, orig: dict) -> dict:
    """Ratios of `now` to the ORIGINAL: volume, tight-box diagonal, and the smallest per-axis extent ratio."""
    out: dict = {"faces": now.get("faces"), "solids": now.get("solids")}
    if now.get("volume") is not None and orig.get("volume"):
        out["volume_ratio"] = now["volume"] / orig["volume"]
    if now.get("diag") and orig.get("diag"):
        out["bbox_diag_ratio"] = now["diag"] / orig["diag"]
    if now.get("extent") and orig.get("extent"):
        ratios = [a / b for a, b in zip(now["extent"], orig["extent"], strict=True) if b > 0]
        out["bbox_min_axis_ratio"] = min(ratios) if ratios else None
    return out


def gamed(metrics: dict) -> list[str]:
    flags = []
    v = metrics.get("volume_ratio")
    if v is not None and abs(v - 1.0) > GAMING_VOLUME:
        flags.append(f"volume x{v:.3g}")
    b = metrics.get("bbox_min_axis_ratio")
    if b is not None and b < 1.0 - GAMING_BBOX_SHRINK:
        flags.append(f"bbox axis x{b:.3g}")
    return flags


def summary(v: Any) -> dict:
    return {"clean": bool(v.clean), "valid": bool(v.valid.ok),
            "passes": sum(1 for m in v.panel if m.status == "pass"),
            "failing": [f"{m.name}:{m.error_kind or m.status}" for m in v.panel if m.status != "pass"],
            "hard": [f.code for f in v.valid.hard]}


# ------------------------------------------------------------------ the child: one file

def one(entry: dict, inputs: str, out: str, *, op_timeout: float = OP_TIMEOUT_S, parents: int = DEPTH2_PARENTS,
        max_sites: int = MAX_SITES) -> dict:
    sys.path.insert(0, str(HERE))
    import arms

    from meshpipeline.cad.repair.identity import run_grid
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed
    from meshpipeline.cad.repair_v4.stages import STAGES, diagonal_mm
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    sha = entry["sha256"]
    src = str(Path(inputs) / sha / Path(entry["name"]).name)
    odir = Path(out) / sha[:16]
    odir.mkdir(parents=True, exist_ok=True)
    jsonl = open(odir / "candidates.jsonl", "a", buffering=1)
    row: dict = {"sha256": sha, "name": entry["name"], "status": "running", "planned": 0, "recorded": 0}
    progress = odir / "progress.json"

    def note(stage: str) -> None:
        progress.write_text(json.dumps({"stage": stage, "planned": row["planned"], "recorded": row["recorded"],
                                        "t": time.time()}))

    pool = WorkerPool(1)
    engine = arms.RealEngine()
    try:
        note("open")
        ws_in = engine.open_workspace(pool, src, str(odir / "work"))
        d0 = float(diagonal_mm(pool, ws_in))
        orig = measure(ws_in.brep_path)
        row.update(d0_mm=d0, original=orig)
        with run_grid(d0):
            h = S.h_mm_for(pool, ws_in)
            judge = engine.judge(h)
            note("input_verdict")
            v_in = judge.full(pool, ws_in, src)
            row["input"] = summary(v_in)
            if v_in.clean:
                row["status"] = "clean_at_input"
                return row
            note("staged")
            staged = arms._run_staged(engine, pool, ws_in, v_in)
            arms.confirm_staged(pool, ws_in, staged, full=lambda w: judge.full(pool, w, src), input_verdict=v_in)
            root = staged.ws
            v_root = judge.full(pool, root, src) if root.brep_path != ws_in.brep_path else v_in
            row["staged"] = {**summary(v_root), "changed": root.brep_path != ws_in.brep_path,
                             **against(measure(root.brep_path), orig)}
            row["root"] = "staged"   # LEAD-2 (a): the root is the staged output only, the state the arms start from
            if v_root.clean:
                # the staged healer alone made it clean: every arm already wins this file, there is nothing to sweep
                row["status"] = "staged_clean"
                return row
            cheap_fn = S.adaptive_cheap(judge, v_root)
            root_cheap = judge.score(cheap_fn(pool, root, src))
            gen = S.DetGenerator(diagonal=lambda ws: d0, whole_part_defeature=True)
            gen.bind_pool(pool)
            try:
                import oracle_grid  # type: ignore[import-not-found,unused-ignore]  # lane C's; absent = none
                extra = oracle_grid.extra_rows
            except ImportError:
                extra = None
            seen = {S._fingerprint(root)}

            def candidates(ws: Any, verdict: Any, depth: int) -> list:
                sites = judge.sites(pool, ws, verdict)[:max_sites]
                st = S.SearchState.__new__(S.SearchState)
                st.ws, st.sites, st.lineage, st.depth = ws, sites, (), depth
                cands = list(gen.propose(st, 10**6))
                cands += [S.Candidate(S.RUN_STAGE, [], {"stage": s.name}, "oracle", f"run stage {s.name}")
                          for s in STAGES]
                if extra is not None:   # lane C: extra_rows(state, d0_mm) -> list[search.Candidate]
                    cands += list(extra(st, d0))
                return cands

            def evaluate(ws: Any, c: Any, depth: int, path: list, base_cheap: tuple, base_valid: bool) -> dict:
                rec: dict = {"depth": depth, "path": path, "op": c.op, "targets": list(c.targets),
                             "params": c.params, "why": c.rationale}
                t0 = time.monotonic()
                new = None
                try:
                    errs = S._validate(c, ws)
                    if errs:
                        rec["outcome"], rec["reason"] = "invalid", errs[:3]
                    elif c.op == S.RUN_STAGE:
                        o = S._stage_outcome(pool, ws, c, 0, op_timeout)
                        rec["outcome"] = "applied" if o.kind == "ok" else o.kind
                        rec["reason"] = o.errors[:3]
                        new = o.ws if o.kind == "ok" else None
                    else:
                        new, diff = REGISTRY[c.op].apply(pool, ws.fork(), list(c.targets), dict(c.params),
                                                         timeout_s=op_timeout)
                        declined = diff.params.get("declined") if isinstance(diff.params, dict) else None
                        if declined is not None:
                            rec["outcome"], rec["reason"], new = "declined", [str(declined)], None
                        else:
                            rec["outcome"] = "applied"
                except OpDeclined as exc:
                    rec["outcome"], rec["reason"] = "declined", [exc.reason]
                except OpFailed as exc:
                    rec["outcome"], rec["reason"] = "failed", [f"{exc.status}: {exc.message}"[:300]]
                except Exception as exc:  # noqa: BLE001 - one candidate never ends the file
                    rec["outcome"], rec["reason"] = "error", [f"{type(exc).__name__}: {exc}"[:300]]
                rec["op_s"] = round(time.monotonic() - t0, 2)
                if new is not None:
                    try:
                        v = cheap_fn(pool, new, src)
                        sc = judge.score(v)
                        rec["cheap"] = list(sc)
                        rec["valid_before"], rec["valid_after"] = base_valid, bool(v.valid.ok)
                        rec["lost_layer1"] = base_valid and not v.valid.ok
                        rec["own"] = against(measure(new.brep_path), orig)
                        rec["beats_parent"] = tuple(sc) > tuple(base_cheap)
                        fp = S._fingerprint(new)
                        rec["duplicate"] = fp in seen
                        seen.add(fp)
                        if rec["beats_parent"]:
                            vf = judge.full(pool, new, src)
                            rec["full"] = summary(vf)
                            if vf.clean:
                                rec["gaming"] = gamed(rec["own"])
                        rec["_ws"] = new
                    except Exception as exc:  # noqa: BLE001 - an unjudgeable child is recorded, not fatal
                        rec["judge_error"] = f"{type(exc).__name__}: {exc}"[:300]
                rec["s"] = round(time.monotonic() - t0, 2)
                return rec

            def run_level(ws: Any, verdict: Any, depth: int, path: list, base_cheap: tuple, base_valid: bool) -> list:
                note(f"depth{depth}_generate")
                cands = candidates(ws, verdict, depth)
                row["planned"] += len(cands)
                done = []
                for i, c in enumerate(cands):
                    note(f"depth{depth} {i + 1}/{len(cands)} {c.op}")
                    rec = evaluate(ws, c, depth, path, base_cheap, base_valid)
                    jsonl.write(json.dumps({k: v for k, v in rec.items() if k != "_ws"}, default=str) + "\n")
                    row["recorded"] += 1
                    done.append(rec)
                return done

            level1 = run_level(root, v_root, 1, [], root_cheap, bool(v_root.valid.ok))
            # depth-2 parents: changed shapes that kept layer 1 (or gained it), best cheap score first; neutral moves
            # are in, because a multi-op repair (fill, then sew) is neutral after its first step
            pool_ok = [r for r in level1 if r.get("_ws") is not None and not r.get("duplicate")
                       and r.get("valid_after", False) >= bool(v_root.valid.ok) and "cheap" in r]
            pool_ok.sort(key=lambda r: tuple(r["cheap"]), reverse=True)
            for r in pool_ok[:parents]:
                ws1 = r["_ws"]
                v1 = judge.full(pool, ws1, src)
                run_level(ws1, v1, 2, [{"op": r["op"], "params": r["params"], "targets": r["targets"]}],
                          tuple(r["cheap"]), bool(v1.valid.ok))
        row["status"] = "complete" if row["planned"] == row["recorded"] else "incomplete"
        return row
    except Exception as exc:  # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"[:500]
        row["tb"] = traceback.format_exc()[-2000:]
        return row
    finally:
        jsonl.close()
        try:
            pool.close()
        except Exception:  # noqa: BLE001
            pass


def summarise(row: dict, jsonl_path: Path) -> dict:
    """Fold the candidate records into the row: winners, layer-1 losses, outcome counts. Works on a killed file's
    partial JSONL too."""
    recs = []
    if jsonl_path.is_file():
        for line in jsonl_path.read_text().splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass   # a line cut by the kill
    outcomes: dict[str, int] = {}
    for r in recs:
        outcomes[r.get("outcome", "?")] = outcomes.get(r.get("outcome", "?"), 0) + 1
    base_v = row.get("input") if row.get("root") == "input" else (row.get("staged") or row.get("input"))
    base = (base_v or {}).get("passes", 0)
    winners = [{k: r.get(k) for k in ("depth", "path", "op", "params", "targets", "full", "own", "gaming", "why")}
               for r in recs if r.get("full") and (r["full"]["clean"] or r["full"]["passes"] > base
                                                  or (r["full"]["valid"] and not (base_v or {}).get("valid", True)))]
    row["recorded_lines"] = len(recs)
    row["outcomes"] = outcomes
    row["lost_layer1"] = sum(1 for r in recs if r.get("lost_layer1"))
    row["winners"] = winners
    row["clean_found"] = any(w["full"]["clean"] for w in winners)
    row["clean_ungamed"] = any(w["full"]["clean"] and not w.get("gaming") for w in winners)
    return row


# ------------------------------------------------------------------ the parent: one file, under a wall

def run_file(entry: dict, inputs: str, out: str, wall_s: float, *, _cmd: list[str] | None = None) -> dict:
    sha = entry["sha256"]
    odir = Path(out) / sha[:16]
    odir.mkdir(parents=True, exist_ok=True)
    row_path = Path(out) / f"{sha[:16]}{ROW_SUFFIX}"
    child_row = odir / "child_row.json"
    t0 = time.monotonic()
    cmd = _cmd or [sys.executable, str(Path(__file__).resolve()), "one", "--entry-json", json.dumps(entry),
                   "--inputs", inputs, "--out", out, "--row-out", str(child_row)]
    with open(odir / "child.log", "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.wait(timeout=wall_s)
            killed = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)   # the child AND its worker pool / mesher grandchildren
            proc.wait()
            killed = True
    if child_row.is_file() and not killed:
        row = json.loads(child_row.read_text())
    else:
        row = {"sha256": sha, "name": entry["name"],
               "status": "killed" if killed else "error",
               "error": f"file wall {wall_s:.0f}s" if killed else f"child exited {proc.returncode} without a row"}
        if (odir / "progress.json").is_file():
            row["stopped_at"] = json.loads((odir / "progress.json").read_text())
    row["wall_s"] = round(time.monotonic() - t0, 1)
    row = summarise(row, odir / "candidates.jsonl")
    row_path.write_text(json.dumps(row, default=str))
    return row


# ------------------------------------------------------------------ shard and collect

def shard_entries(manifest: list[dict], spec: str) -> list[dict]:
    i, n = (int(x) for x in spec.split("/"))
    if not 1 <= i <= n:
        raise SystemExit(f"--shard {spec}: need 1 <= I <= N")
    return [e for k, e in enumerate(manifest) if k % n == i - 1]


def collect(manifest: list[dict], rows_dir: str) -> tuple[list[dict], list[str]]:
    """Every manifest sha must have exactly one row; anything else is a refusal, with the reason."""
    rows: dict[str, dict] = {}
    problems: list[str] = []
    for p in Path(rows_dir).rglob(f"*{ROW_SUFFIX}"):
        r = json.loads(p.read_text())
        if r["sha256"] in rows:
            problems.append(f"two rows for {r['sha256'][:12]}")
        rows[r["sha256"]] = r
    want = [e["sha256"] for e in manifest]
    missing = [s for s in want if s not in rows]
    extra = [s for s in rows if s not in set(want)]
    if missing:
        problems.append(f"{len(missing)} of {len(want)} inputs have NO row: {[s[:12] for s in missing[:10]]}")
    if extra:
        problems.append(f"{len(extra)} rows are not in the manifest: {[s[:12] for s in extra[:10]]}")
    return [rows[s] for s in want if s in rows], problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("one", "file"):
        p = sub.add_parser(name)
        p.add_argument("--entry-json", required=True)
        p.add_argument("--inputs", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--file-wall-s", type=float, default=FILE_WALL_S)
        p.add_argument("--row-out")
    p = sub.add_parser("shard")
    p.add_argument("--manifest", required=True)
    p.add_argument("--shard", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--file-wall-s", type=float, default=FILE_WALL_S)
    p = sub.add_parser("collect")
    p.add_argument("--manifest", required=True)
    p.add_argument("--rows", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "one":
        row = one(json.loads(a.entry_json), a.inputs, a.out)
        Path(a.row_out).write_text(json.dumps(row, default=str))
        return 0
    if a.cmd == "file":
        row = run_file(json.loads(a.entry_json), a.inputs, a.out, a.file_wall_s)
        print(json.dumps({"sha": row["sha256"][:12], "status": row["status"], "wall_s": row.get("wall_s")}))
        return 0
    if a.cmd == "shard":
        entries = shard_entries(json.load(open(a.manifest)), a.shard)
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / f"shard-{a.shard.replace('/', '-of-')}.manifest.json").write_text(json.dumps(entries))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            for row in ex.map(lambda e: run_file(e, a.inputs, a.out, a.file_wall_s), entries):
                print(json.dumps({"sha": row["sha256"][:12], "status": row["status"], "wall_s": row.get("wall_s")}),
                      flush=True)
        return 0
    if a.cmd == "collect":
        manifest = json.load(open(a.manifest))
        rows, problems = collect(manifest, a.rows)
        if problems:
            for problem in problems:
                print("REFUSED:", problem, file=sys.stderr)
            return 2
        with open(a.out, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
        print(json.dumps({"rows": len(rows), "inputs": len(manifest)}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
