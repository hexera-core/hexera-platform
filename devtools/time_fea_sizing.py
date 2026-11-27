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
product). --profile also writes the 25 heaviest functions by cumulative time (cProfile) to that text file; it
sees only this process, which mostly waits on the counting processes' pipes. --profile-children writes the size
field's own work, timed and counted INSIDE every counting process and in this one, merged into one table: the signed
distance to the whole surface, the distance to each kind of zone group, the tile bookkeeping, the octree itself, the
pipe traffic per count, the field built per pass, and the counters that explain them (triangles, groups per kind,
triangles per group, points and point x group pairs measured). Step names, seconds and counts only.
    PYTHONPATH=src python devtools/time_fea_sizing.py part.step --profile-children /tmp/sizing_children.txt
While it runs it says every 60 s, on stderr, which stage it is in. Exit 0: sized; 3: the limit hit; 1: it failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
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


# --- --profile-children: the size field's own work, inside every counting process and in this one (TIME-12) ---

#: the zone kinds whose groups are their own (every other kind shares face groups, named by the kinds in them)
_OWN_GROUP_KINDS = ("part", "wall_spot", "corner")


class FieldWork:
    """Seconds, calls and counters of the size field's own steps in one process (step names and numbers only)."""

    def __init__(self) -> None:
        self.s: dict[str, float] = {}
        self.n: dict[str, int] = {}
        self.c: dict[str, float] = {}
        #: the last field built: group kind -> triangles (vtk) or sample points (edges) per group
        self.groups: dict[str, list[int]] = {}
        self.balls: dict[str, int] = {}
        self.counts: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.inner = 0.0            # seconds in _sizes_each so far (the tiles' own time is what is left)

    def time(self, key: str, dt: float, calls: int = 1) -> None:
        with self.lock:
            self.s[key] = self.s.get(key, 0.0) + dt
            self.n[key] = self.n.get(key, 0) + calls

    def add(self, key: str, v: float) -> None:
        with self.lock:
            self.c[key] = self.c.get(key, 0) + v

    def snap(self) -> dict[str, float]:
        with self.lock:
            return {**{"s:" + k: v for k, v in self.s.items()}, **{"c:" + k: v for k, v in self.c.items()}}

    def as_dict(self) -> dict[str, Any]:
        return {"s": self.s, "n": self.n, "c": self.c, "groups": self.groups, "balls": self.balls,
                "counts": self.counts}


class _CountW:
    def __init__(self, f: Any) -> None:
        self.f, self.n = f, 0

    def write(self, b: Any) -> Any:
        self.n += memoryview(b).nbytes
        return self.f.write(b)


class _CountR:
    def __init__(self, f: Any) -> None:
        self.f, self.n = f, 0

    def read(self, k: int = -1) -> bytes:
        b = self.f.read(k)
        self.n += len(b)
        return b

    def readline(self, k: int = -1) -> bytes:
        b = self.f.readline(k)
        self.n += len(b)
        return b

    def readinto(self, buf: Any) -> int:
        k = self.f.readinto(buf)
        self.n += int(k or 0)
        return k


class _Timed:
    """A zone group's distance (vtk triangles, balls or edge samples), timed and counted under its kind."""

    def __init__(self, fn: Any, key: str, work: FieldWork) -> None:
        self.fn, self.key, self.work = fn, key, work

    def __call__(self, p: Any, *a: Any) -> Any:
        t0 = time.perf_counter()
        out = self.fn(p, *a)
        self.work.time(self.key, time.perf_counter() - t0)
        self.work.add("point x group measured: " + self.key[len("distance: "):], len(p))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.fn, name)


def _group_kinds(SF: Any, field: Any, zones: list[dict], groups: list) -> list[str]:
    """Each built group's kind: a part base, a thin spot, a sharp corner, or the kinds of the zones on its faces."""
    import numpy as np

    names = {k: n for n, k in field.index.items()}
    by_face: dict[tuple[int, str], set[str]] = {}
    for z in zones:
        kind = str(z.get("kind") or "unnamed")
        size = float(z.get("size_m") or 0.0)
        if kind in _OWN_GROUP_KINDS or not size > 0 or not math.isfinite(size):
            continue
        b = int(math.floor(math.log(size) / math.log(SF.GROUP_RATIO)))
        for f in z.get("faces") or []:
            by_face.setdefault((b, str(f)), set()).add(kind)
    key_of = {id(v[0]): k for k, v in field._dists.items()}
    out = []
    for g in groups:
        if g.inside:
            out.append("part base")
        elif g.balls is not None:
            out.append("thin spot (wall_spot)")
        elif g.edges is not None:
            out.append("sharp corner (edge)")
        else:
            kinds: set[str] = set()
            key = key_of.get(id(g.faces))
            if key is not None:
                b = int(math.floor(math.log(g.size) / math.log(SF.GROUP_RATIO)))
                for lab in np.unique(field.label[np.frombuffer(key, dtype=np.int64)]).tolist():
                    kinds |= by_face.get((b, str(names.get(int(lab)))), set())
            out.append("faces: " + ("+".join(sorted(kinds)) or "unnamed"))
    return out


def instrument_field(work: FieldWork, SF: Any) -> None:
    """Time and count the size field's own steps in THIS process (the class's methods, so every field goes through
    them). Each group's distance is wrapped under its kind; nothing about the file is kept but numbers."""
    F = SF.SizeField
    init, groups_of, signed, sizes = F.__init__, F._groups, F.signed, F.sizes
    tiled, each = F._sizes_tiled, F._sizes_each

    def t_init(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return init(self, *a, **k)
        finally:
            work.time("build the size field (a pass)", time.perf_counter() - t0)
            with work.lock:
                work.c["surface triangles (last field)"] = int(len(getattr(self, "tris", ())))

    def t_groups(self, zones):
        out = groups_of(self, zones)
        kinds = _group_kinds(SF, self, zones, out)
        per: dict[str, list[int]] = {}
        balls: dict[str, int] = {}
        for g, kind in zip(out, kinds, strict=True):
            if g.faces is not None:
                per.setdefault(kind, []).append(int(g.faces.pd.GetNumberOfCells()))
                g.faces = _Timed(g.faces, "distance: " + kind, work)
            if g.edges is not None:
                per.setdefault(kind, []).append(int(len(g.edges.pts)))
                g.edges = _Timed(g.edges, "distance: " + kind, work)
            if g.balls is not None:
                balls[kind] = balls.get(kind, 0) + int(len(g.balls.r))
                g.balls = _Timed(g.balls, "distance: " + kind + " (its balls)", work)
            g.__dict__["_prof_kind"] = kind
        work.groups, work.balls = per, balls
        return out

    def t_signed(self, p):
        t0 = time.perf_counter()
        out = signed(self, p)
        work.time("signed distance to the whole surface", time.perf_counter() - t0)
        work.add("points: signed distance", len(out))
        return out

    def t_sizes(self, p, r=0.0):
        t0 = time.perf_counter()
        out = sizes(self, p, r)
        work.time("zone sizes (all of sizes())", time.perf_counter() - t0)
        work.add("points: zone sizes", len(out[0]))
        work.add("point x group, every group (what no pruning would measure)", len(out[0]) * len(self.groups))
        return out

    def t_tiled(self, p, r):
        t0, before = time.perf_counter(), work.inner
        out = tiled(self, p, r)
        work.time("tile bookkeeping (sort, tiles, each group's tile floor)",
                  time.perf_counter() - t0 - (work.inner - before))
        return out

    def t_each(self, p, r, h=None, need=None, groups=None):
        t0 = time.perf_counter()
        out = each(self, p, r, h, need, groups)
        dt = time.perf_counter() - t0
        work.inner += dt
        if groups is not None and len(groups) == 1:
            kind = groups[0].__dict__.get("_prof_kind", "unnamed")
            work.time("group looked at (box test + distance): " + kind, dt)
            work.add("point x group looked at: " + kind, len(p))
        else:
            work.time("every group at once (a small set of points)", dt)
        return out

    F.__init__, F._groups, F.signed, F.sizes = t_init, t_groups, t_signed, t_sizes
    F._sizes_tiled, F._sizes_each = t_tiled, t_each


def instrument_parent(work: FieldWork, SF: Any, out_dir: Path) -> None:
    """In the parent: the same field timers, the counting processes started as profiled children, the pipe traffic
    counted, and one row per count."""
    import pickle
    import subprocess

    instrument_field(work, SF)
    F, P = SF.SizeField, SF._Pool
    on, hist = F._on, F._histogram
    work.c["counting processes started"] = 0

    def p_init(self, spec, n):
        from concurrent.futures import ThreadPoolExecutor  # noqa: F401 - as the product's own __init__
        self.n = int(n)
        self._threads = None
        self._sent = None
        cmd =[sys.executable, str(Path(__file__).resolve()), "--child-of", str(out_dir)]
        self.procs = [subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE) for _ in range(self.n)]
        work.add("counting processes started", self.n)
        self.init(spec)

    def p_send_field(self, spec):
        t0 = time.perf_counter()
        message = self.init_message(spec) if hasattr(self, "init_message") else ("init", spec)
        data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
        for proc in self.procs:
            proc.stdin.write(data)
            proc.stdin.flush()
        work.time("pipe: the field sent to every child (per pass)", time.perf_counter() - t0)
        work.add("bytes sent: the field (all children)", len(data) * len(self.procs))

    def p_ask(self, proc, job):
        t0 = time.perf_counter()
        w = _CountW(proc.stdin)
        pickle.dump(job, w, protocol=pickle.HIGHEST_PROTOCOL)
        proc.stdin.flush()
        t1 = time.perf_counter()
        r = _CountR(proc.stdout)
        ok, value = pickle.load(r)  # noqa: S301 - our own child, over its own pipe
        t2 = time.perf_counter()
        work.time("pipe: pickle + send a slice (parent threads)", t1 - t0)
        work.time("pipe: wait for + read the answer (parent threads)", t2 - t1)
        work.add("bytes sent: slices", w.n)
        work.add("bytes back: answers", r.n)
        if not ok:
            raise RuntimeError(f"a size-field worker failed: {value}")
        return value

    def t_on(self, pool, what, p, r):
        t0 = time.perf_counter()
        out = on(self, pool, what, p, r)
        work.time(f"_on {what} (wall: split, children, join)", time.perf_counter() - t0)
        work.add(f"points asked: {what}", len(p))
        return out

    def t_hist(self, *a, **k):
        before = work.snap()
        t0 = time.perf_counter()
        out = hist(self, *a, **k)
        dt = time.perf_counter() - t0
        after = work.snap()

        def d(key: str) -> float:
            return float(after.get(key, 0.0) - before.get(key, 0.0))

        on_s = d("s:_on signed (wall: split, children, join)") + d("s:_on sizes (wall: split, children, join)")
        work.counts.append({
            "s": dt, "octree own s": dt - on_s, "signed wait s": d("s:_on signed (wall: split, children, join)"),
            "sizes wait s": d("s:_on sizes (wall: split, children, join)"), "cubes": int(out.cubes),
            "complete": bool(out.complete), "points signed": int(d("c:points asked: signed")),
            "points sized": int(d("c:points asked: sizes")), "bytes sent": int(d("c:bytes sent: slices")),
            "bytes back": int(d("c:bytes back: answers"))})
        work.time("the counts (_histogram, wall)", dt)
        return out

    P.__init__, P.init, P._ask = p_init, p_send_field, p_ask
    F._on, F._histogram = t_on, t_hist


def child_main(out_dir: Path) -> int:
    """A profiled counting process: the size field's own _serve, timed, and its numbers written when it stops."""
    import pickle

    from meshpipeline.engines import fea_size_field as SF

    work = FieldWork()
    instrument_field(work, SF)
    load, dump = pickle.load, pickle.dump

    def t_load(f, *a, **k):
        t0 = time.perf_counter()
        r = _CountR(f)
        try:
            return load(r, *a, **k)
        finally:
            work.time("pipe: wait for + read a job (idle included)", time.perf_counter() - t0)
            work.add("bytes read (jobs + fields)", r.n)

    def t_dump(obj, f, *a, **k):
        t0 = time.perf_counter()
        w = _CountW(f)
        try:
            return dump(obj, w, *a, **k)
        finally:
            work.time("pipe: pickle + write an answer", time.perf_counter() - t0)
            work.add("bytes written (answers)", w.n)

    pickle.load, pickle.dump = t_load, t_dump  # type: ignore[assignment]
    t0 = time.perf_counter()
    try:
        SF._serve()
    finally:
        work.time("alive (wall)", time.perf_counter() - t0)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"child-{os.getpid()}.json").write_text(json.dumps(work.as_dict()))
    return 0


def _stats(v: list[int]) -> str:
    if not v:
        return "-"
    s = sorted(v)
    return f"{s[0]} / {s[len(s) // 2]} / {s[-1]}"


def children_table(parent: FieldWork, out_dir: Path) -> str:
    """The parent's and every child's numbers, merged into one table."""
    kids = [json.loads(f.read_text()) for f in sorted(out_dir.glob("child-*.json"))]
    cs: dict[str, float] = {}
    cn: dict[str, int] = {}
    cc: dict[str, float] = {}
    for k in kids:
        for key, v in k["s"].items():
            cs[key] = cs.get(key, 0.0) + v
        for key, v in k["n"].items():
            cn[key] = cn.get(key, 0) + v
        for key, v in k["c"].items():
            if key != "surface triangles (last field)":
                cc[key] = cc.get(key, 0) + v
    counts = parent.counts
    nc = max(1, len(counts))
    lines = ["", "FEA size field, inside the counting processes (--profile-children)", "=" * 92,
             f"counting processes started {int(parent.c.get('counting processes started', 0))}, reported {len(kids)}"
             f" (one that did not stop cleanly writes nothing); counts made {len(counts)}"
             f" ({sum(1 for c in counts if not c['complete'])} stopped early); fields built in this process "
             f"{parent.n.get('build the size field (a pass)', 0)}",
             f"surface triangles (the whole-surface vtk distance is built on these): "
             f"{int(parent.c.get('surface triangles (last field)', 0))}", ""]
    busy_keys = ["signed distance to the whole surface", "zone sizes (all of sizes())", "build the size field (a pass)",
                 "pipe: pickle + write an answer"]
    busy = sum(cs.get(k, 0.0) for k in busy_keys)
    sizes_s = cs.get("zone sizes (all of sizes())", 0.0) + parent.s.get("zone sizes (all of sizes())", 0.0)

    def row(label: str, key: str, *, share: bool = True, indent: int = 0) -> str:
        p_s, k_s = parent.s.get(key, 0.0), cs.get(key, 0.0)
        calls = parent.n.get(key, 0) + cn.get(key, 0)
        sh = f"{100.0 * k_s / busy:>7.1f}%" if share and busy > 0 else f"{'':>8}"
        name = ("  " * indent + label)[:51]
        return f"{name:<52}{p_s:>9.1f}{k_s:>11.1f}{sh}{calls:>11d}"

    lines += [f"{'step':<52}{'parent s':>9}{'children s':>11}{'share':>8}{'calls':>11}", "-" * 92,
              row("signed distance to the whole surface", "signed distance to the whole surface"),
              row("zone sizes (all of sizes())", "zone sizes (all of sizes())"),
              row("tile bookkeeping", "tile bookkeeping (sort, tiles, each group's tile floor)", indent=1),
              row("every group at once (small point sets)", "every group at once (a small set of points)", indent=1)]
    kinds = sorted({k.split(": ", 1)[1] for k in list(cs) + list(parent.s)
                    if k.startswith("group looked at (box test + distance): ")})
    dist_all = 0.0
    for kind in kinds:
        lines.append(row(f"group kind {kind}: looked at", "group looked at (box test + distance): " + kind, indent=1))
        lines.append(row("its distance", "distance: " + kind, indent=2))
        dist_all += cs.get("distance: " + kind, 0.0) + parent.s.get("distance: " + kind, 0.0)
        if ("distance: " + kind + " (its balls)") in cs or ("distance: " + kind + " (its balls)") in parent.s:
            lines.append(row("its balls", "distance: " + kind + " (its balls)", indent=2))
            dist_all += cs.get("distance: " + kind + " (its balls)", 0.0) + \
                parent.s.get("distance: " + kind + " (its balls)", 0.0)
    lines += [f"{'  numpy in sizes() (all of it minus every distance)':<52}{'':>9}"
              f"{sizes_s - dist_all:>11.1f}   (parent + children)",
              row("build the size field (a pass)", "build the size field (a pass)"),
              row("pipe: pickle + write an answer", "pipe: pickle + write an answer"),
              row("pipe: wait for + read a job (idle incl.)", "pipe: wait for + read a job (idle included)",
                  share=False),
              row("alive (wall)", "alive (wall)", share=False), "",
              "the parent (wall seconds; pipe rows are thread-seconds over its reply threads)", "-" * 92]
    for label, key in (("the counts (_histogram)", "the counts (_histogram, wall)"),
                       ("_on signed (split, children, join)", "_on signed (wall: split, children, join)"),
                       ("_on sizes (split, children, join)", "_on sizes (wall: split, children, join)"),
                       ("pipe: pickle + send a slice", "pipe: pickle + send a slice (parent threads)"),
                       ("pipe: wait for + read the answer", "pipe: wait for + read the answer (parent threads)"),
                       ("pipe: the field sent to every child", "pipe: the field sent to every child (per pass)")):
        lines.append(f"{label:<52}{parent.s.get(key, 0.0):>9.1f}{'':>19}{parent.n.get(key, 0):>11d}")
    own = sum(c["octree own s"] for c in counts)
    lines.append(f"{'the octree itself (counts minus their _on waits)':<52}{own:>9.1f}")
    lines += ["", "counters (all counts; per count = / counts made)", "-" * 92]

    def cnt(label: str, v: float) -> str:
        return f"  {label:<62}{int(v):>14d}{int(v / nc):>14d}"

    lines.append(f"  {'':<62}{'all':>14}{'per count':>14}")
    lines.append(cnt("points evaluated: signed distance", parent.c.get("points asked: signed", 0)))
    lines.append(cnt("points evaluated: zone sizes", parent.c.get("points asked: sizes", 0)))
    every = cc.get("point x group, every group (what no pruning would measure)", 0) + \
        parent.c.get("point x group, every group (what no pruning would measure)", 0)
    lines.append(cnt("point x group, every group (no pruning)", every))
    for kind in kinds:
        looked = cc.get("point x group looked at: " + kind, 0) + parent.c.get("point x group looked at: " + kind, 0)
        meas = cc.get("point x group measured: " + kind, 0) + parent.c.get("point x group measured: " + kind, 0)
        lines.append(cnt(f"{kind}: point x group looked at (box test)", looked))
        lines.append(cnt(f"{kind}: point x group measured (distance)", meas))
    lines.append(cnt("bytes sent: slices", parent.c.get("bytes sent: slices", 0)))
    lines.append(cnt("bytes back: answers", parent.c.get("bytes back: answers", 0)))
    lines.append(cnt("bytes sent: the field (all children, every pass)",
                     parent.c.get("bytes sent: the field (all children)", 0)))
    lines += ["", "zone groups of the last field built (triangles per group; edges: sample points)", "-" * 92,
              f"  {'kind':<44}{'groups':>8}{'min / median / max':>28}{'balls':>10}"]
    for kind, v in sorted(parent.groups.items()):
        lines.append(f"  {kind[:43]:<44}{len(v):>8d}{_stats(v):>28}{parent.balls.get(kind, 0):>10d}")
    lines += ["", "each count (the parent)", "-" * 92,
              f"  {'#':>3}{'s':>8}{'octree':>8}{'signed':>8}{'sizes':>8}{'cubes':>11}{'done':>5}{'pts signed':>12}"
              f"{'pts sized':>12}{'MB out':>8}{'MB back':>8}"]
    for i, c in enumerate(counts):
        lines.append(f"  {i + 1:>3}{c['s']:>8.1f}{c['octree own s']:>8.1f}{c['signed wait s']:>8.1f}"
                     f"{c['sizes wait s']:>8.1f}{c['cubes']:>11d}{'yes' if c['complete'] else 'no':>5}"
                     f"{c['points signed']:>12d}{c['points sized']:>12d}{c['bytes sent'] / 1e6:>8.1f}"
                     f"{c['bytes back'] / 1e6:>8.1f}")
    lines.append("")
    lines.append("children s are process-seconds summed over every counting process; share = of their busy seconds "
                 "(signed + sizes + field + answers)")
    return "\n".join(lines)


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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--child-of"]:
        return child_main(Path(argv[1]))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--route", default="fea", choices=["fea"], help="the route sized (only FEA has this sizing)")
    ap.add_argument("--limit-s", type=float, default=None,
                    help="stop after this many seconds and say where it was (default: the check's own limit)")
    ap.add_argument("--to-metres", type=float, default=1e-3, help="the file's unit in metres (default 0.001, mm)")
    ap.add_argument("--profile", type=Path, default=None, help="write the 25 heaviest functions (cProfile) here")
    ap.add_argument("--profile-children", type=Path, default=None,
                    help="write the size field's own work, timed inside every counting process and merged, here")
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
    field_work = FieldWork() if args.profile_children else None
    kids_dir = Path(tempfile.mkdtemp(prefix="time_fea_children_")) if field_work is not None else None
    if field_work is not None and kids_dir is not None:
        from meshpipeline.engines import fea_size_field as SF
        instrument_parent(field_work, SF, kids_dir)
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
    if field_work is not None and kids_dir is not None:
        import shutil
        args.profile_children.write_text(children_table(field_work, kids_dir) + "\n")
        shutil.rmtree(kids_dir, ignore_errors=True)
        print(f"\nthe counting processes' own work written to {args.profile_children}")
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
