# Responsibility: Prove devtools/simplify/fast_solve.py against CalculiX on the same decks: wall time (split), peak memory and iterations of each, and the relative differences of max |u|, reaction, compliance, strain energy and nodal von Mises.
# Boundaries: a developer tool run on a bench VM with `ccx` on PATH; it reads decks and writes only under each deck's own directory (ccx/ and fast_*.json/npz). Nothing in the product imports it.
# Collaborates with: devtools/simplify/fast_solve.py (the solver under test), devtools/simplify/harness.py (the deck format and the .frd/.dat quantities it compares).
"""FAST_SOLVE VS CALCULIX, MEASURED.

    python devtools/simplify/bench_solvers.py DECKDIR... [--methods pardiso,pmg] [--threads 16] [--no-ccx]
    python devtools/simplify/bench_solvers.py --table DECKDIR...       # markdown rows from bench.json

Each DECKDIR holds a job.inp (harness or local.py format) and optionally mesh.npz (tags, xyz, conn,
nfix, nload, delta, bend: the in-memory mesh, so the deck-free path is timed too). CalculiX runs once
per deck in DECKDIR/ccx with one extra card, *NODE PRINT NSET=NALL / U, because the .frd's E12.5
displacements carry 6 digits and cannot show a 1e-6 agreement; the .dat's carry 7. Every solve runs in
a fresh process, so wall time includes interpreter start-up and imports; memory is the peak summed
PSS of the process tree (MPI ranks included).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def _time_v(cmd, cwd, env=None, timeout=4 * 3600) -> tuple[subprocess.CompletedProcess, float, float]:
    """Run cmd; return (process, wall s, peak memory GB). Memory is the peak over time of the summed
    PSS of the whole process tree (the MPI ranks of fast_solve's PETSc path included, shared memory
    counted once), sampled every 0.2 s."""
    import threading

    import psutil
    t0 = time.perf_counter()
    proc = psutil.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = [0]
    done = threading.Event()

    def sample():
        while not done.is_set():
            tot = 0
            try:
                for q in [proc] + proc.children(recursive=True):
                    try:
                        tot += q.memory_full_info().pss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except psutil.NoSuchProcess:
                pass
            peak[0] = max(peak[0], tot)
            done.wait(0.2)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    out, err = proc.communicate(timeout=timeout)
    done.set()
    th.join()
    wall = time.perf_counter() - t0
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err), wall, peak[0] / 1e9


# ------------------------------------------------------------------------------------------------
# CalculiX

def run_ccx(deck: Path, threads: int) -> dict:
    wd = deck.parent / "ccx"
    wd.mkdir(exist_ok=True)
    meta = wd / "ccx.json"
    if meta.exists():
        return json.loads(meta.read_text())
    text = deck.read_text()
    text = text.replace("*END STEP", "*NODE PRINT, NSET=NALL\nU\n*END STEP", 1)
    (wd / "job.inp").write_text(text)
    env = dict(os.environ, OMP_NUM_THREADS=str(threads), CCX_NPROC_EQUATION_SOLVER=str(threads))
    p, wall, rss = _time_v(["ccx", "-i", "job"], wd, env)
    if p.returncode != 0:
        raise RuntimeError(f"ccx failed on {deck}: {p.stdout[-2000:]}")
    out = {"wall": wall, "mem_gb": rss, "threads": threads}
    meta.write_text(json.dumps(out))
    return out


def read_dat(dat: Path) -> dict:
    """Blocks of a CalculiX .dat: per-node tables by (quantity, set), and the totals lines."""
    tables: dict[tuple[str, str], dict[int, list[float]]] = {}
    totals: dict[tuple[str, str], list[float]] = {}
    cur = None
    lines = dat.read_text().splitlines()
    for i, ln in enumerate(lines):
        mt = re.match(r"\s*(total force|total internal energy).*for set (\S+)", ln)
        mh = re.match(r"\s*(displacements|forces) \(.*\) for set (\S+)", ln)
        if mt:
            nxt = next(x for x in lines[i + 1:i + 4] if x.strip())
            totals[(mt.group(1), mt.group(2))] = [float(v) for v in nxt.split()]
            cur = None
        elif mh:
            cur = tables.setdefault((mh.group(1), mh.group(2)), {})
        elif cur is not None:
            p = ln.split()
            if len(p) == 4:
                try:
                    cur[int(p[0])] = [float(v) for v in p[1:]]
                except ValueError:
                    cur = None
            elif p:
                cur = None
    return {"tables": tables, "totals": totals}


def read_frd_stress(frd: Path) -> dict[int, np.ndarray]:
    out, block = {}, False
    with frd.open() as fh:
        for ln in fh:
            if ln.startswith(" -4"):
                block = ln[5:13].strip() == "STRESS"
            elif ln.startswith(" -3"):
                block = False
            elif block and ln.startswith(" -1"):
                out[int(ln[3:13])] = np.array([float(ln[13 + 12 * k:25 + 12 * k]) for k in range(6)])
    return out


def vm(s: np.ndarray) -> np.ndarray:
    sxx, syy, szz, sxy, syz, szx = s.T
    return np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2) + 3 * (sxy ** 2 + syz ** 2 + szx ** 2))


# ------------------------------------------------------------------------------------------------
# fast_solve, each run in its own process

def _worker(argv) -> int:
    mode, src, method, out, threads = argv[0], Path(argv[1]), argv[2], Path(argv[3]), int(argv[4]) or None
    t0 = time.perf_counter()
    sys.path.insert(0, str(HERE))
    import fast_solve as F
    t_import = time.perf_counter() - t0
    if mode == "deck":
        r, d = F.solve_deck(src, method=method, threads=threads)
        tags = d.tags
        sets = {k.upper(): v for k, v in d.sets.items()}
        totals = {name: r.total_force(sets[name.upper()]).tolist() for name in d.print_sets}
    else:
        g = np.load(src)
        lut = F._rows_of(g["tags"])
        d = int(g["bend"])
        br, bc, bv = F._expand([(g["nfix"], 1, 3, 0.0), (g["nload"], d + 1, d + 1, float(g["delta"]))], {}, lut)
        r = F.solve(g["xyz"], lut[g["conn"]], br, bc, bv, method=method, threads=threads)
        r.rows = lut
        tags = g["tags"]
        totals = {"NLOAD": r.total_force(g["nload"]).tolist()}
    np.savez(out.with_suffix(".npz"), tags=tags, u=r.u, s=r.s, f=r.f)
    out.write_text(json.dumps({"times": {"import": t_import} | r.times, "iterations": r.iterations,
                               "residual": r.residual, "energy": r.energy, "totals": totals, "method": r.method}))
    return 0


def run_fast(src: Path, mode: str, method: str, threads: int) -> dict:
    out = src.parent / f"fast_{mode}_{method}.json"
    p, wall, rss = _time_v([sys.executable, "-I", __file__, "--worker", mode, str(src), method, str(out),
                            str(threads)], src.parent)
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout).strip().splitlines()[-1:]}
    res = json.loads(out.read_text())
    res |= {"wall": wall, "mem_gb": rss}
    return res


# ------------------------------------------------------------------------------------------------
# compare

def rel(a, b) -> float:
    return abs(a - b) / abs(b) if b else float("nan")


def compare(deck: Path, fast_json: Path) -> dict:
    wd = deck.parent / "ccx"
    dat = read_dat(wd / "job.dat")
    fast = json.loads(fast_json.read_text())
    g = np.load(fast_json.with_suffix(".npz"))
    row = {int(t): i for i, t in enumerate(g["tags"])}
    uc = dat["tables"][("displacements", "NALL")]
    tags = np.array(sorted(uc))
    u_ccx = np.array([uc[t] for t in tags])
    u_fast = g["u"][[row[t] for t in tags]]
    umax_c, umax_f = np.linalg.norm(u_ccx, axis=1).max(), np.linalg.norm(u_fast, axis=1).max()
    out = {"max_u": rel(umax_f, umax_c),
           "u_field": float(np.abs(u_fast - u_ccx).max() / np.abs(u_ccx).max())}
    e_c = next(v[-1] for k, v in dat["totals"].items() if k[0] == "total internal energy")
    out["energy"] = rel(fast["energy"], e_c)
    forces = {k[1]: v for k, v in dat["totals"].items() if k[0] == "total force"}
    if forces:
        name, fc = next(iter(forces.items()))
        ff = fast["totals"].get(name) or fast["totals"].get(name.upper())
        k = int(np.argmax(np.abs(fc)))
        out["reaction"] = rel(ff[k], fc[k])
        out["compliance"] = rel(1 / ff[k], 1 / fc[k])
    per_node = {k[1]: v for k, v in dat["tables"].items() if k[0] == "forces"}
    for name, tab in per_node.items():   # local pieces: per-node RF on the cut nodes
        t = np.array(sorted(tab))
        fc = np.array([tab[x] for x in t])
        ff = g["f"][[row[x] for x in t]]
        out[f"rf_nodes_{name}"] = float(np.abs(ff - fc).max() / np.abs(fc).max())
    sc = read_frd_stress(wd / "job.frd")
    t = np.array(sorted(sc))
    vm_c = vm(np.array([sc[x] for x in t]))
    vm_f = vm(g["s"][[row[x] for x in t]])
    out["max_vm"] = rel(vm_f.max(), vm_c.max())
    out["vm_field"] = float(np.abs(vm_f - vm_c).max() / vm_c.max())
    # the .frd carries each stress component as E12.5 (6 digits): rounded the same way, how much of
    # the difference is left?
    vm_r = vm(_frd_round(g["s"][[row[x] for x in t]]))
    out["max_vm_frd_rounded"] = rel(vm_r.max(), vm_c.max())
    out["vm_field_frd_rounded"] = float(np.abs(vm_r - vm_c).max() / vm_c.max())
    return out


def _frd_round(a: np.ndarray) -> np.ndarray:
    """Round to 6 significant digits, as a Fortran E12.5 field does."""
    e = np.floor(np.log10(np.where(a == 0, 1.0, np.abs(a))))
    return np.round(a / 10.0 ** e, 5) * 10.0 ** e


def table(dirs) -> str:
    """Markdown rows from each DECKDIR/bench.json."""
    out = ["| deck | tets | solver | wall s | assemble / setup / solve s | peak GB | its | max u | reaction | "
           "energy | max vM (frd) |", "|" + "---|" * 11]
    for d in map(Path, dirs):
        rep = json.loads((d / "bench.json").read_text())
        deck = d / "job.inp"
        tets = sum(1 for _ in _element_lines(deck))
        c = rep.get("ccx")
        if c:
            out.append(f"| {d.name} | {tets} | ccx SPOOLES {c['threads']}t | {c['wall']:.1f} | | {c['mem_gb']:.1f} | | | | | |")
        for k, r in rep.items():
            if k == "ccx" or "error" in r:
                continue
            t = r["times"]
            asm = t.get("pattern", 0) + t.get("assemble", 0)
            ag = r.get("agreement", {})

            def f(key, ag=ag):
                return f"{ag[key]:.1e}" if key in ag else ""
            out.append(f"| {d.name} | {tets} | {k} | {r['wall']:.1f} | {asm:.1f} / {t['setup']:.1f} / {t['solve']:.1f} "
                       f"| {r['mem_gb']:.1f} | {r['iterations']} | {f('max_u')} | {f('reaction') or f('rf_nodes_NCUT')} | "
                       f"{f('energy')} | {f('max_vm')} |")
    return "\n".join(out)


def _element_lines(deck: Path):
    on = False
    with deck.open() as fh:
        for ln in fh:
            if ln.startswith("*"):
                on = ln.upper().startswith("*ELEMENT")
            elif on and ln.strip():
                yield ln


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--worker"]:
        return _worker(argv[1:])
    if argv[:1] == ["--compare"]:     # recompute the agreement of every saved run against ccx/
        for d in (Path(x).resolve() for x in argv[1:]):
            rep = json.loads((d / "bench.json").read_text())
            for k, r in rep.items():
                if k != "ccx" and "error" not in r:
                    method, mode = k.rsplit("_", 1)
                    r["agreement"] = compare(d / "job.inp", d / f"fast_{mode}_{method}.json")
                    print(d.name, k, json.dumps(r["agreement"]), flush=True)
            (d / "bench.json").write_text(json.dumps(rep, indent=1))
        return 0
    if argv[:1] == ["--table"]:
        print(table(argv[1:]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--methods", default="pardiso,pmg")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 1, help="CalculiX threads")
    ap.add_argument("--fast-threads", type=int, default=0,
                    help="fast_solve threads / MPI ranks (0: its defaults, all logical cores / physical cores)")
    ap.add_argument("--no-ccx", action="store_true")
    a = ap.parse_args(argv)
    report = {}
    for d in (Path(x).resolve() for x in a.dirs):
        deck = d / "job.inp"
        rep: dict = {}
        if not a.no_ccx:
            rep["ccx"] = run_ccx(deck, a.threads)
            print(d, "ccx", rep["ccx"], flush=True)
        for method in a.methods.split(","):
            for mode, src in (("deck", deck), ("mem", d / "mesh.npz")):
                if not src.exists():
                    continue
                r = run_fast(src, mode, method, a.fast_threads)
                if "error" not in r and (d / "ccx" / "ccx.json").exists():     # a ccx run that finished
                    r["agreement"] = compare(deck, src.parent / f"fast_{mode}_{method}.json")
                rep[f"{method}_{mode}"] = r
                print(d, method, mode, json.dumps(r), flush=True)
        report[str(d)] = rep
        (d / "bench.json").write_text(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
