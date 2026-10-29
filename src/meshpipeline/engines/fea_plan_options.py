# Responsibility: PLAN-fea-22, THE SIZE CHOICE - for an FEA part whose full-rules mesh needs more than the standard
# mesh job, the two meshes the user picks between (the full-rules one, on the machine it needs, and a quicker one
# sized to the standard machine with the low-stress zones coarsened first), each with its size, machine, time and
# what falls short, and which one the evidence recommends; and the two plans themselves, kept so the run reuses them.
# Boundaries: plans only (engines/fea_driver.choose_plan, the run's own planning); it meshes nothing and stores
# nothing itself. The check runs it in a child it can stop (`bounded`), on the file staged as the run stages it, and
# stores the plans it made (`PLANS_FILE`) for the run.
# Collaborates with: engines/fea_driver.py (choose_plan, the memory and time models; drive applies the pick, or the
# recommendation when nothing was picked), contracts/mesh_machine.py (which machine), application/geometry_check.py
# (runs it for the stage, stores the plans), application/fea_check.py (serves it).
"""THE SIZE CHOICE (PLAN-fea-22, 27-28 Oct).

The rules size a mesh for accuracy; the machine follows (RUN-1). When the full-rules mesh fits the standard mesh
job there is nothing to choose. When it needs a bigger machine, the user sees both meshes and picks:

- FULL: every zone at its rule size, on the machine its memory needs, with its time.
- QUICKER: sized to the standard machine, coarsened kind by kind in PLAN-fea-8's order (shape-only rounds and
  relief, then capped sharp corners, then walls and the parts' own sizes, the stress raisers last), with every
  zone it leaves short of its rule named.

THE RECOMMENDATION is read off what the quicker mesh gives up. When it coarsens only zones whose stress is not a
result anyone reads - rounds and relief kept for their shape, and sharp corners whose stress never settles however
fine (PLAN-fea-5) - the quicker mesh is recommended: the walls keep their elements through them and every fillet,
hole and notch its rule size, so the stresses an engineer reads are the same (the textbook cases in
tests/unit/engines/test_fea_textbook.py measure it). When it would coarsen a wall, a part's own size or a stress
raiser, the full mesh is recommended. Either way the user picks - BEFORE the run: once the run starts it never waits
for anyone (Areen, 28 Oct). With nothing picked (the stage had not sized it yet, or no answer came), the run takes
the recommended mesh itself and says so.

THE TIME is said as what such a run usually takes and its upper end (Areen, 28 Oct): "usually about X minutes, up to
Y" - the usual one at the fill rate measured end to end on the lab (FEA_USUAL_FILL_RATES), the upper one at the
part's slow rate (PLAN-fea-13) - and that a retry can take longer (a pass the ladder repeats).

PLANNED ONCE: the plans the stage makes are kept (PLANS_FILE, keyed by the staged solid and the code that plans it),
and the run reuses them instead of planning again when its own staged solid and code are the same.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

FULL, QUICKER = "full", "quicker"
#: the zone kinds whose coarsening changes no stress an engineer reads (see the module text)
LOW_STRESS = frozenset({"round", "relief", "corner"})
VERSION = 2
#: how long the check may spend sizing the two meshes (a child process, stopped at it). It is the sizing's own time,
#: after the stage is shown - never what the naming has left: the stage says "Sizing the mesh..." meanwhile, and a
#: run started before it is done takes the recommended mesh itself (lab, 28 Oct: on 4 vCPUs the 32 corpus parts
#: offered a choice took 72-3,136 s to size; 27 of 32 within 1,800 s)
BUDGET_S = 1800.0
#: the plans kept for the run (application/geometry_check stores it beside the check)
PLANS_FILE = "fea_plans.json"
_KIND_WORDS = {"round": "rounds kept for their shape", "relief": "raised or sunk lettering and texture",
               "corner": "capped sharp corners (their stress never settles however fine)", "wall": "walls",
               "wall_spot": "thin spots", "part": "the parts' own sizes", "hole": "holes", "fillet": "fillets",
               "notch": "notches"}


def standard_elements() -> int:
    """The element limit that keeps a plan on the standard mesh job: the most elements whose mesher AND solver
    files fit the job's usable memory (contracts/mesh_machine.choose, PLAN-fea-16's memory model - Cloud Run keeps
    the files in memory too), over PLAN-fea-8's aim, so a plan that fits its limit runs there."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan as P

    gib = 1024.0 ** 3
    room = MM.usable_gib(None) * gib - FD.MEMORY_MARGIN * FD.MEMORY_BASE_MB * 1024.0 * 1024.0
    per = FD.MEMORY_MARGIN * FD.BYTES_PER_ELEMENT + FD.WORKSPACE_BYTES_PER_ELEMENT
    return max(0, int(room / per / P.LIMIT_AIM))


def _kinds_words(kinds) -> str:
    words = [_KIND_WORDS.get(k, k) for k in kinds]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def minutes_words(usual: int | None, upper: int | None) -> str:
    """A run's time as the stage and the run say it: what it usually takes and its upper end (Areen, 28 Oct)."""
    def one(m: int) -> str:
        return f"{max(1, m)} minutes" if m < 90 else f"{m / 60.0:.1f} hours"
    if not upper:
        return ""
    if usual and usual < upper:
        return f"usually about {one(int(usual))}, up to {one(int(upper))}"
    return f"up to about {one(int(upper))}"


def row(record: dict, plan: dict, key: str) -> dict:
    """One option as the stage shows it: its elements, machine, time (usually, and the upper end at the part's slow
    fill rate) and what it leaves short of the rules."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_driver as FD

    est = int((plan.get("estimate") or {}).get("elements") or 0)
    process, files = FD.memory_need(float(est))
    machine = MM.choose(process_gib=process, workspace_gib=files, why="memory model")
    rate, cls = FD.fill_rate(record)
    secs = FD.planned_seconds(plan, rate) or 0.0
    usual = FD.planned_seconds(plan, FD.FEA_USUAL_FILL_RATES[cls]) or 0.0
    shorts = [z for z in plan.get("zones") or [] if z.get("short")]
    kinds = sorted({str(z["kind"]) for z in shorts})
    return {"key": key, "elements": est, "at_least": bool((plan.get("estimate") or {}).get("at_least")),
            "where": machine["where"], "machine": machine.get("machine"),
            "need_gib": round(float(machine.get("need_gib") or process), 1),
            "memory_gib": float(machine.get("memory_gib") or 0.0),
            "standard": machine["where"] != "gce", "minutes": int(math.ceil(secs / 60.0)),
            "usual_minutes": int(math.ceil(usual / 60.0)),
            "short_kinds": kinds, "short_zones": len(shorts), "short": list(plan.get("resolution_short") or [])[:6],
            "at_minimums": bool((plan.get("coarsened") or {}).get("at_minimums"))}


def decide(record: dict, full_plan: dict, quicker_plan) -> tuple[dict, dict | None]:
    """The size choice from the plans (see the module text): ({"choice", "full", "quicker", "recommended", "why"},
    the quicker plan or None). `quicker_plan` is the plan or a callable making it - called only when the full-rules
    mesh needs a bigger machine than the standard job."""
    full = row(record, full_plan, FULL)
    out: dict = {"version": VERSION, "rule": "PLAN-fea-22", "full": full, "quicker": None, "choice": False,
                 "recommended": FULL}
    if full["standard"]:
        out["why"] = "The full-rules mesh fits the standard machine: there is nothing to choose."
        return out, None
    qplan = quicker_plan() if callable(quicker_plan) else quicker_plan
    quick = row(record, qplan, QUICKER)
    if not quick["standard"] or quick["at_minimums"]:
        out["why"] = ("Even with every zone at its validity minimum the mesh needs more than the standard machine, "
                      "so there is no quicker mesh: the full-rules one is made.")
        return out, qplan
    out.update(quicker=quick, choice=True)
    gave_up = [k for k in quick["short_kinds"] if k not in LOW_STRESS]
    quick_time = minutes_words(quick["usual_minutes"], quick["minutes"])
    full_time = minutes_words(full["usual_minutes"], full["minutes"])
    if not quick["short_kinds"]:
        out.update(recommended=QUICKER, why="The quicker mesh keeps every zone at its rule size.")
    elif not gave_up:
        out.update(recommended=QUICKER,
                   why=(f"The quicker mesh coarsens only {_kinds_words(quick['short_kinds'])}. Every wall keeps its "
                        "elements through it and every fillet, hole and notch its rule size, so the stresses you "
                        f"read are the same - {quick_time} on the standard machine instead of "
                        f"{full_time} on a {full['memory_gib']:g} GiB one."))
    else:
        out.update(recommended=FULL,
                   why=(f"The quicker mesh would leave {_kinds_words(gave_up)} short of their rules "
                        f"({quick['short_zones']} zones). The full-rules mesh keeps them, on a {full['memory_gib']:g} "
                        f"GiB machine, {full_time}."))
    return out, qplan


def options(record: dict, surface, diag_m: float, *, ai: dict | None = None) -> dict:
    """The size choice for a measured part (see the module text): {"choice": bool, "full": row, "quicker": row |
    None, "recommended": FULL | QUICKER, "why": words}."""
    return options_and_plans(record, surface, diag_m, ai=ai)[0]


def options_and_plans(record: dict, surface, diag_m: float, *, ai: dict | None = None) -> tuple[dict, dict]:
    """The size choice and the plans it was read from ({"full": plan, "quicker": plan | None})."""
    from meshpipeline.engines import fea_driver as FD

    full_plan = FD.choose_plan(record, surface, diag_m, ai=ai)
    out, qplan = decide(record, full_plan,
                        lambda: FD.choose_plan(record, surface, diag_m, ai=ai, cap=standard_elements()))
    return out, {FULL: full_plan, QUICKER: qplan}


# #
# PLANNED ONCE: the plans kept for the run
# #

_PLANNING_SOURCES = ("engines/fea_driver.py", "engines/fea_plan.py", "engines/fea_size_field.py",
                     "engines/fea_ai.py", "engines/fea_ladder.py", "engines/fea_plan_options.py",
                     "cad/fea_measure.py", "cad/fea_assembly.py", "cad/fea_features.py", "cad/fea_joins.py")


def planning_code() -> str:
    """A digest of the code that plans an FEA mesh: plans kept by other code are never reused."""
    import meshpipeline
    root = Path(meshpipeline.__file__).resolve().parent
    h = hashlib.sha256(f"v{VERSION}".encode())
    for rel in _PLANNING_SOURCES:
        p = root / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"-")
    return h.hexdigest()[:16]


def plans_key(ws: Path, *, connection: str, gap_joins: bool) -> str:
    """What the kept plans were made for: the staged solid as the run reads it (geometry.brep, else geometry.step),
    the assembly's connection and small-gap answer, and the planning code."""
    ws = Path(ws)
    staged = ws / "geometry.brep" if (ws / "geometry.brep").exists() else ws / "geometry.step"
    h = hashlib.sha256(staged.read_bytes())
    h.update(f"|{connection}|{bool(gap_joins)}|{planning_code()}".encode())
    return h.hexdigest()


def kept_plan(kept: dict | None, key: str, pick: str) -> dict | None:
    """The kept plan for `pick` when it was made for this key, else None (the run plans it itself)."""
    if not isinstance(kept, dict) or kept.get("key") != key or pick not in (FULL, QUICKER):
        return None
    plan = kept.get(pick)
    return plan if isinstance(plan, dict) else None


def plan_staged(ws: Path, *, connection: str = "bonded", gap_joins: bool = True, source_path: str = "",
                plans_out: Path | None = None) -> dict:
    """The size choice for the part staged in `ws` (geometry.step / geometry.brep in metres), planned as the run
    plans it: closed shells read as solids, the staged solid measured, an assembly prepared as the stage's default
    answers have it. With `plans_out`, the plans are kept there for the run (PLANS_FILE)."""
    from meshpipeline.engines import fea_driver as FD

    key = plans_key(ws, connection=connection, gap_joins=gap_joins) if plans_out else ""
    FD.solids_from_shells(ws)
    record, surface, diag_m = FD.measure_staged(ws)
    if int(record.get("bodies") or 1) > 1:
        record = FD.prepare_assembly(ws, record, surface, connection=connection, source_path=source_path,
                                     gap_joins=gap_joins)
    out, plans = options_and_plans(record, surface, diag_m)
    if plans_out:
        Path(plans_out).write_text(json.dumps({"key": key, **plans}, default=str))
    return out


def stage_file(src: Path, ws: Path, *, to_metres: float) -> None:
    """Stage an uploaded CAD file as the run stages it (engines/gmsh/gmsh_runner): read at its metre scale, written
    as geometry.step and geometry.brep."""
    import gmsh

    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("plan_options")
        gmsh.option.setNumber("Geometry.OCCScaling", float(to_metres))
        try:
            gmsh.model.occ.importShapes(str(src))
        finally:
            gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
        gmsh.model.remove()
    finally:
        if mine:
            gmsh.finalize()


def bounded(src: Path, *, to_metres: float, budget_s: float = BUDGET_S, plans_out: Path | None = None) -> dict | None:
    """The size choice for an uploaded CAD file, sized in a child process stopped `budget_s` after it starts, so a
    part whose planning takes longer never holds the check. With `plans_out`, the child keeps the plans there.
    None when it was not sized (out of time, or it failed): the run then sizes it and takes the recommended mesh.
    Never raises."""
    import logging
    import os
    import subprocess

    import meshpipeline
    from meshpipeline.sandbox.safe_exec import run_guarded, scrubbed_subprocess_env

    log = logging.getLogger(__name__)
    here = str(Path(meshpipeline.__file__).resolve().parents[1])
    env = scrubbed_subprocess_env({"PYTHONPATH": os.pathsep.join(dict.fromkeys([here] + [p for p in sys.path if p]))})
    cmd = [sys.executable, "-m", "meshpipeline.engines.fea_plan_options", str(src), "--to-metres", f"{to_metres!r}"]
    if plans_out:
        cmd += ["--plans-out", str(plans_out)]
    try:
        p = run_guarded(cmd, env=env, capture_output=True, text=True, timeout=float(budget_s))
        text = p.stdout or ""
    except subprocess.TimeoutExpired:
        log.info("fea plan options: not sized within %.0f s - sized when the mesh runs", budget_s)
        return None
    except Exception as exc:  # noqa: BLE001 - the stage then says it is sized when the mesh runs
        log.warning("fea plan options: could not be sized (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                got = json.loads(line)
            except ValueError:
                continue
            return got if isinstance(got, dict) and "full" in got else None
    return None


def _main(argv: list[str]) -> int:
    import tempfile

    src = Path(argv[0])
    to_metres = float(argv[argv.index("--to-metres") + 1]) if "--to-metres" in argv else 1e-3
    plans_out = Path(argv[argv.index("--plans-out") + 1]) if "--plans-out" in argv else None
    with tempfile.TemporaryDirectory(prefix="fea_plan_options_") as tmp:
        ws = Path(tmp)
        stage_file(src, ws, to_metres=to_metres)
        out = plan_staged(ws, source_path=str(src), plans_out=plans_out)
    print(json.dumps(out), flush=True)
    return 0


__all__ = ["BUDGET_S", "FULL", "LOW_STRESS", "PLANS_FILE", "QUICKER", "bounded", "decide", "kept_plan",
           "minutes_words", "options", "options_and_plans", "plan_staged", "planning_code", "plans_key", "row",
           "stage_file", "standard_elements"]


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
