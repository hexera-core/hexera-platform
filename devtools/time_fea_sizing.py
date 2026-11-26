"""Time the FEA mesh sizing the geometry check runs after the stage is shown (PLAN-fea-22, TIME-11), stage by stage.

It runs, in this process, the same staging and sizing the geometry worker's sizing child runs
(engines/fea_plan_options._main: stage_file, then plan_staged - closed shells read as solids, the staged solid
measured, an assembly prepared with the stage's default answers, the full-rules plan and the quicker one), with a
timer on every stage, and prints:

- a table of every stage with its seconds and how many times it ran (nested stages indented under their caller);
- the counts that drive the time: bodies, faces, edges, hot spots, assembly pairs and interfaces, the histogram's
  cubes, the sizing passes;
- when the limit hits (--limit-s, the check's own 1,800 s by default), where it was then (the open stages).

It prints NO coordinates and NO names from the file: only counts and seconds. The measurement cache is not read or
written (every stage is timed from cold) and nothing is stored anywhere. No model is asked.

Run from the worktree with the app's virtualenv (no .env needed):
    PYTHONPATH=src python devtools/time_fea_sizing.py part.step
    PYTHONPATH=src python devtools/time_fea_sizing.py part.step --limit-s 600 --profile /tmp/sizing_profile.txt
    PYTHONPATH=src python devtools/time_fea_sizing.py part.step --to-metres 0.001 --json /tmp/sizing.json

--to-metres is the file's unit in metres (0.001: millimetres, the default; the stage's unit box decides it in the
product). --profile also writes the 25 heaviest functions by cumulative time (cProfile) to that text file.
While it runs it says every 60 s, on stderr, which stage it is in. Exit 0: sized; 3: the limit hit; 1: it failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class LimitHit(BaseException):
    """The --limit-s clock ran out (a BaseException: the product's own `except Exception` never swallows it)."""


class Timers:
    """Inclusive seconds and calls per stage, in the order the stages first ran, with their nesting depth."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.stack: list[str] = []
        self.counts: dict[str, Any] = {}
        self.lock = threading.Lock()

    def wrap(self, label: str, fn: Callable, after: Callable | None = None) -> Callable:
        timers = self

        def timed(*args, **kwargs):
            with timers.lock:
                row = timers.rows.setdefault(label, {"s": 0.0, "n": 0, "depth": len(timers.stack)})
                timers.stack.append(label)
            t0 = time.perf_counter()
            try:
                out = fn(*args, **kwargs)
                if after is not None:
                    try:
                        after(out, args, kwargs)
                    except Exception:  # noqa: BLE001 - a count the harness could not read is left out
                        pass
                return out
            finally:
                with timers.lock:
                    row["s"] += time.perf_counter() - t0
                    row["n"] += 1
                    if timers.stack and timers.stack[-1] == label:
                        timers.stack.pop()
                    elif label in timers.stack:
                        timers.stack.remove(label)

        timed.__wrapped__ = fn  # type: ignore[attr-defined]
        return timed

    def add(self, key: str, v: float) -> None:
        self.counts[key] = self.counts.get(key, 0) + v

    def most(self, key: str, v: float) -> None:
        self.counts[key] = max(self.counts.get(key, 0), v)


def _patch(timers: Timers, owner: Any, attr: str, label: str, after: Callable | None = None) -> None:
    fn = getattr(owner, attr, None)
    if fn is None:
        return
    setattr(owner, attr, timers.wrap(label, fn, after))


def instrument(timers: Timers) -> None:
    """A timer on every stage of the sizing (module attributes, so the product's own calls go through them)."""
    from meshpipeline.cad import assembly_join as AJ
    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad import fea_features as ff
    from meshpipeline.cad import fea_joins as J
    from meshpipeline.cad import fea_measure as M
    from meshpipeline.cad import fea_measure_cache as MC
    from meshpipeline.cad import fold_back as FB
    from meshpipeline.cad import scout as S
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan_options as PO
    from meshpipeline.engines import fea_size_field as SF

    # measured from cold, stored nowhere
    MC.load = lambda key: None
    MC.save = lambda key, entry: None

    def hist_seen(h, _a, _k) -> None:
        timers.add("histogram counts", 1)
        timers.add("histogram cubes (all counts)", int(getattr(h, "cubes", 0) or 0))
        timers.most("histogram cubes (largest count)", int(getattr(h, "cubes", 0) or 0))
        if not getattr(h, "complete", True):
            timers.add("histogram counts stopped early", 1)

    def plan_seen(_p, _a, kwargs) -> None:
        timers.add("plans chosen (full + quicker)", 1)

    def pass_seen(_p, _a, _k) -> None:
        timers.add("sizing passes (make_plan)", 1)

    stages: list[tuple[Any, str, str, Callable | None]] = [
        (PO, "stage_file", "read + stage the CAD (gmsh, as the run stages it)", None),
        (FD, "solids_from_shells", "closed shells read as solids", None),
        (FD, "measure_staged", "the measure (whole)", None),
        (S, "read_cad", "read the staged solid (OCC)", None),
        (M, "measure_cad_surface", "measure: the faces' walls and hot spots", None),
        (M, "_brep_surface", "measure: tessellate the faces", None),
        (M, "_face_arc", "measure: each face's arc", None),
        (M, "face_radius", "measure: each curved face's radius", None),
        (M, "sewn_bodies", "measure: loose faces sewn", None),
        (M, "_record", "measure: walls + hot spots record", None),
        (ff, "edge_pairs", "hot spots: edge pairs", None),
        (ff, "wall_thickness", "walls: ray-traced thickness", None),
        (ff, "relief_groups", "hot spots: raised / sunk detail", None),
        (ff, "merge_coaxial", "hot spots: coaxial arcs merged", None),
        (ff, "sharp_corners", "hot spots: sharp corners", None),
        (ff, "contacts", "hot spots: faces touching", None),
        (M, "cad_cusps", "measure: cusps", None),
        (M, "cad_micro", "measure: micro details", None),
        (FB, "fold_back_edges", "measure: fold-back edges", None),
        (FD, "_suspects_into", "measure: suspected defects (Booleans)", None),
        (FD, "prepare_assembly", "assembly: prepare (whole)", None),
        (A, "solids", "assembly: split the bodies", None),
        (A, "read_body_names", "assembly: read the parts' names", None),
        (A, "global_face_names", "assembly: name every face", None),
        (A, "stage", "assembly: stage the parts (joins / pairs)", None),
        (A, "open_skins", "joins: zero-thickness skins", None),
        (AJ, "join", "joins: fuse what touches", None),
        (A, "joined_interfaces", "joins: interfaces from the surfaces", None),
        (J, "measure", "joins: face pairs measured", None),
        (J, "interfaces", "joins: interfaces", None),
        (J, "check", "joins: fuse checked against surfaces", None),
        (A, "interfaces_of", "joins: interfaces of the fuse", None),
        (A, "pairwise_interfaces", "joins: pairwise interfaces", None),
        (A, "tangent_meets", "joins: tangent meets", None),
        (PO, "options_and_plans", "the sizing (full + quicker)", None),
        (FD, "choose_plan", "sizing: choose a plan", plan_seen),
        (FD, "make_plan", "sizing: a pass (make_plan)", pass_seen),
        (FD, "target_plan", "sizing: aim at a target", None),
        (FD, "_coarsen_group", "sizing: coarsen a group (quicker)", None),
        (FD, "_histogram", "sizing: the zones' histogram", None),
        (PO, "decide", "the estimate + the choice (decide)", None),
        (PO, "row", "estimate: a mesh's row (elements, machine, time)", None),
    ]
    for owner, attr, label, after in stages:
        _patch(timers, owner, attr, label, after)
    # the size field's own steps, on the class
    _patch(timers, SF.SizeField, "__init__", "sizing: build the size field")
    _patch(timers, SF.SizeField, "histogram", "sizing: count the octree", hist_seen)


def shape_counts(ws: Path) -> dict[str, int]:
    """Bodies, faces and edges of the staged solid (OpenCASCADE's own maps)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.engines import fea_driver as FD

    staged = FD.staged_solid(ws) or ws / "geometry.step"
    from meshpipeline.cad.scout import read_cad
    shape = getattr(read_cad, "__wrapped__", read_cad)(staged)
    out = {}
    for key, kind in (("bodies (solids)", TopAbs_SOLID), ("faces", TopAbs_FACE), ("edges", TopAbs_EDGE)):
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, m)
        out[key] = int(m.Extent())
    return out


def record_counts(record: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    out["faces measured"] = len(record.get("faces") or {})
    out["hot spots"] = len(record.get("hot_spots") or [])
    asm = record.get("assembly_plan") or {}
    if asm:
        out["assembly pairs"] = int(asm.get("pairs") or 0)
        out["assembly interfaces"] = int(asm.get("interfaces") or 0)
        out["assembly parts staged"] = len(asm.get("parts") or [])
    return out


def _digest(value: Any) -> str:
    """A short digest of a result (sorted JSON): equal digests, equal results."""
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def table(timers: Timers, counts: dict[str, Any], *, total_s: float, state: str, where: list[str]) -> str:
    lines = ["", "FEA sizing, stage by stage (inclusive seconds; nested stages indented)", "-" * 78,
             f"{'stage':<60}{'seconds':>10}{'calls':>8}"]
    for label, row in timers.rows.items():
        name = ("  " * int(row["depth"]) + label)[:59]
        lines.append(f"{name:<60}{row['s']:>10.1f}{row['n']:>8d}")
    lines += ["-" * 78, f"{'total':<60}{total_s:>10.1f}", "", "counts"]
    for k, v in counts.items():
        lines.append(f"  {k:<58}{v:>12}")
    lines += ["", f"ended: {state}"]
    if where:
        lines.append("where it was when the limit hit: " + " > ".join(where))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--route", default="fea", choices=["fea"], help="the route sized (only FEA has this sizing)")
    ap.add_argument("--limit-s", type=float, default=None,
                    help="stop after this many seconds and say where it was (default: the check's own limit)")
    ap.add_argument("--to-metres", type=float, default=1e-3, help="the file's unit in metres (default 0.001, mm)")
    ap.add_argument("--profile", type=Path, default=None, help="write the 25 heaviest functions (cProfile) here")
    ap.add_argument("--json", type=Path, default=None, help="also write the table's numbers here as JSON")
    ap.add_argument("--log", action="store_true", help="let the product's own log lines through (may name parts)")
    ap.add_argument("--standard-gib", type=float, default=None,
                    help="pretend the standard mesh job has this much memory (default: the product's own), so a "
                         "small public part takes the path of a part too big for it: the quicker mesh is planned")
    ap.add_argument("--keep", type=Path, default=None,
                    help="write the size choice, both plans and the assembly's sidecar here as JSON, to compare two "
                         "builds exactly (it holds the file's face names and places: public files only)")
    args = ap.parse_args(argv)

    # the product's log lines can carry names from the file: silent unless asked
    logging.basicConfig(level=logging.INFO if args.log else logging.CRITICAL)
    if not args.log:
        logging.disable(logging.ERROR)

    from meshpipeline.engines import fea_plan_options as PO

    if args.standard_gib:
        from meshpipeline.contracts import mesh_machine as MM
        MM.CLOUD_RUN_GIB = float(args.standard_gib)
    limit = float(args.limit_s) if args.limit_s else float(PO.BUDGET_S)
    src = args.file.resolve()
    if not src.exists():
        print("no such file", file=sys.stderr)
        return 1
    timers = Timers()
    instrument(timers)
    counts: dict[str, Any] = {"file size (MB)": round(src.stat().st_size / 1e6, 2), "limit (s)": int(limit)}
    where: list[str] = []
    state = "sized"
    done = threading.Event()
    t_start = time.perf_counter()

    def say_progress() -> None:
        while not done.wait(60.0):
            now = time.perf_counter() - t_start
            print(f"[{now:7.0f} s] in: {' > '.join(timers.stack) or '-'}", file=sys.stderr, flush=True)

    def on_alarm(_sig, _frame) -> None:
        # where it was, read now: the stages' own timers close as the stop unwinds through them
        where[:] = list(timers.stack)
        raise LimitHit()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, limit)
    threading.Thread(target=say_progress, daemon=True).start()
    prof = None
    if args.profile:
        import cProfile
        prof = cProfile.Profile()
    record_box: dict = {}
    code = 0
    with tempfile.TemporaryDirectory(prefix="time_fea_sizing_") as tmp:
        ws = Path(tmp)
        from meshpipeline.engines import fea_driver as FD
        measure = FD.measure_staged

        def kept_measure(*a, **k):
            out = measure(*a, **k)
            record_box["record"] = out[0]
            return out

        FD.measure_staged = kept_measure
        both = PO.options_and_plans

        def kept_plans(*a, **k):
            out = both(*a, **k)
            record_box["plans"] = out[1]
            return out

        PO.options_and_plans = kept_plans
        try:
            if prof is not None:
                prof.enable()
            PO.stage_file(src, ws, to_metres=float(args.to_metres))
            got = PO.plan_staged(ws, source_path=str(src))
            record_box["options"] = got
            counts["choice offered"] = "yes, two options" if got.get("choice") else "no, one option"
            counts["recommended"] = str(got.get("recommended") or "")
            for key in (PO.FULL, PO.QUICKER):
                if isinstance(got.get(key), dict):
                    counts[f"{key} mesh: elements"] = int(got[key].get("elements") or 0)
            # digests, to tell two builds gave the same answer without printing it (no names, no places)
            counts["digest: the size choice"] = _digest(got)
            for key, plan in (record_box.get("plans") or {}).items():
                counts[f"digest: the {key} plan"] = _digest(plan) if plan is not None else "none"
            from meshpipeline.cad import fea_assembly as A
            if (ws / A.SIDECAR).exists():
                record_box["sidecar"] = json.loads((ws / A.SIDECAR).read_text())
                counts["digest: the interfaces"] = _digest(record_box["sidecar"].get("interfaces"))
        except LimitHit:
            state = f"stopped by the limit at {limit:.0f} s"
            code = 3
        except Exception as exc:  # noqa: BLE001 - said by its kind only (its text may hold a path or a name)
            state = f"failed ({type(exc).__name__})"
            where[:] = list(timers.stack)
            code = 1
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if prof is not None:
                prof.disable()
            done.set()
        total = time.perf_counter() - t_start
        try:
            counts.update(shape_counts(ws))
        except BaseException:  # noqa: BLE001 - staged file missing (it stopped while staging)
            pass
    if record_box.get("record"):
        counts.update(record_counts(record_box["record"]))
    counts.update({k: (int(v) if float(v).is_integer() else v) for k, v in timers.counts.items()})
    print(table(timers, counts, total_s=total, state=state, where=where))
    if args.json:
        args.json.write_text(json.dumps({"stages": timers.rows, "counts": counts, "total_s": round(total, 2),
                                         "state": state, "where": where}, indent=1, default=str))
    if args.keep:
        args.keep.write_text(json.dumps({k: record_box.get(k) for k in ("options", "plans", "sidecar")},
                                        indent=1, sort_keys=True, default=str))
    if prof is not None:
        import io
        import pstats
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(25)
        args.profile.write_text(buf.getvalue())
        print(f"\nprofile (25 heaviest by cumulative time) written to {args.profile}")
    sys.stdout.flush()
    # a histogram's worker processes may still be running when the limit hit: they go with us
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
