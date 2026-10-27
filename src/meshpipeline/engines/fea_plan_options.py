# Responsibility: PLAN-fea-22, THE SIZE CHOICE - for an FEA part whose full-rules mesh needs more than the standard
# mesh job, the two meshes the user picks between (the full-rules one, on the machine it needs, and a quicker one
# sized to the standard machine with the low-stress zones coarsened first), each with its size, machine, time and
# what falls short, and which one the evidence recommends.
# Boundaries: plans only (engines/fea_driver.choose_plan, the run's own planning); it meshes nothing and stores
# nothing. The check runs it in a child it can stop (`bounded`), on the file staged as the run stages it.
# Collaborates with: engines/fea_driver.py (choose_plan, the memory and time models), contracts/mesh_machine.py
# (which machine), application/geometry_check.py (runs it for the stage), application/fea_check.py (serves it),
# engines/fea_driver.drive (applies the pick: state fea_plan_choice).
"""THE SIZE CHOICE (PLAN-fea-22, 27 Oct).

The rules size a mesh for accuracy; the machine follows (RUN-1). When the full-rules mesh fits the standard mesh
job there is nothing to choose. When it needs a bigger machine, the user sees both meshes and picks:

- FULL: every zone at its rule size, on the machine its memory needs, with its time.
- QUICKER: sized to the standard machine, coarsened kind by kind in PLAN-fea-8's order (shape-only rounds and
  relief, then capped sharp corners, then walls and the parts' own sizes, the stress raisers last), with every
  zone it leaves short of its rule named.

THE RECOMMENDATION is read off what the quicker mesh gives up. When it coarsens only zones whose stress is not a
result anyone reads - rounds and relief kept for their shape, and sharp corners whose stress never settles however
fine (PLAN-fea-5) - the quicker mesh is recommended: the walls keep their elements through them and every fillet,
hole and notch its rule size, so the stresses an engineer reads are the same (the textbook case
tests/unit/engines/test_fea_plan_options.py measures it). When it would coarsen a wall, a part's own size or a
stress raiser, the full mesh is recommended. Either way the user picks.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

FULL, QUICKER = "full", "quicker"
#: the zone kinds whose coarsening changes no stress an engineer reads (see the module text)
LOW_STRESS = frozenset({"round", "relief", "corner"})
VERSION = 1
#: how long the check may spend sizing the two meshes (a child process, stopped at it): past it, the stage says
#: the mesh is sized when it runs, and the run makes the full-rules mesh
BUDGET_S = 1200.0
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


def _time_words(minutes: int) -> str:
    if minutes < 90:
        return f"about {max(1, minutes)} minutes"
    return f"about {minutes / 60.0:.1f} hours"


def row(record: dict, plan: dict, key: str) -> dict:
    """One option as the stage shows it: its elements, machine, time (at the part's slow fill rate) and what it
    leaves short of the rules."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_driver as FD

    est = int((plan.get("estimate") or {}).get("elements") or 0)
    process, files = FD.memory_need(float(est))
    machine = MM.choose(process_gib=process, workspace_gib=files, why="memory model")
    rate, _cls = FD.fill_rate(record)
    secs = FD.planned_seconds(plan, rate) or 0.0
    shorts = [z for z in plan.get("zones") or [] if z.get("short")]
    kinds = sorted({str(z["kind"]) for z in shorts})
    return {"key": key, "elements": est, "at_least": bool((plan.get("estimate") or {}).get("at_least")),
            "where": machine["where"], "machine": machine.get("machine"),
            "need_gib": round(float(machine.get("need_gib") or process), 1),
            "memory_gib": float(machine.get("memory_gib") or 0.0),
            "standard": machine["where"] != "gce", "minutes": int(math.ceil(secs / 60.0)),
            "short_kinds": kinds, "short_zones": len(shorts), "short": list(plan.get("resolution_short") or [])[:6],
            "at_minimums": bool((plan.get("coarsened") or {}).get("at_minimums"))}


def options(record: dict, surface, diag_m: float, *, ai: dict | None = None) -> dict:
    """The size choice for a measured part (see the module text): {"choice": bool, "full": row, "quicker": row |
    None, "recommended": FULL | QUICKER, "why": words}."""
    from meshpipeline.engines import fea_driver as FD

    full = row(record, FD.choose_plan(record, surface, diag_m, ai=ai), FULL)
    out: dict = {"version": VERSION, "rule": "PLAN-fea-22", "full": full, "quicker": None, "choice": False,
                 "recommended": FULL}
    if full["standard"]:
        out["why"] = "The full-rules mesh fits the standard machine: there is nothing to choose."
        return out
    quick = row(record, FD.choose_plan(record, surface, diag_m, ai=ai, cap=standard_elements()), QUICKER)
    if not quick["standard"] or quick["at_minimums"]:
        out["why"] = ("Even with every zone at its validity minimum the mesh needs more than the standard machine, "
                      "so there is no quicker mesh: the full-rules one is made.")
        return out
    out.update(quicker=quick, choice=True)
    gave_up = [k for k in quick["short_kinds"] if k not in LOW_STRESS]
    if not quick["short_kinds"]:
        out.update(recommended=QUICKER, why="The quicker mesh keeps every zone at its rule size.")
    elif not gave_up:
        out.update(recommended=QUICKER,
                   why=(f"The quicker mesh coarsens only {_kinds_words(quick['short_kinds'])}. Every wall keeps its "
                        "elements through it and every fillet, hole and notch its rule size, so the stresses you "
                        f"read are the same, in {_time_words(quick['minutes'])} on the standard machine instead of "
                        f"{_time_words(full['minutes'])} on a {full['memory_gib']:g} GiB one."))
    else:
        out.update(recommended=FULL,
                   why=(f"The quicker mesh would leave {_kinds_words(gave_up)} short of their rules "
                        f"({quick['short_zones']} zones). The full-rules mesh keeps them, on a {full['memory_gib']:g} "
                        f"GiB machine in {_time_words(full['minutes'])}."))
    return out


def plan_staged(ws: Path, *, connection: str = "bonded", gap_joins: bool = True, source_path: str = "") -> dict:
    """The size choice for the part staged in `ws` (geometry.step / geometry.brep in metres), planned as the run
    plans it: closed shells read as solids, the staged solid measured, an assembly prepared as the stage's default
    answers have it."""
    from meshpipeline.engines import fea_driver as FD

    FD.solids_from_shells(ws)
    record, surface, diag_m = FD.measure_staged(ws)
    if int(record.get("bodies") or 1) > 1:
        record = FD.prepare_assembly(ws, record, surface, connection=connection, source_path=source_path,
                                     gap_joins=gap_joins)
    return options(record, surface, diag_m)


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


def bounded(src: Path, *, to_metres: float, budget_s: float = BUDGET_S) -> dict | None:
    """The size choice for an uploaded CAD file, sized in a child process stopped `budget_s` after it starts, so a
    part whose planning takes longer never holds the check. None when it was not sized (out of time, or it
    failed): the stage then says the mesh is sized when it runs. Never raises."""
    import logging
    import os
    import subprocess

    import meshpipeline
    from meshpipeline.sandbox.safe_exec import run_guarded, scrubbed_subprocess_env

    log = logging.getLogger(__name__)
    here = str(Path(meshpipeline.__file__).resolve().parents[1])
    env = scrubbed_subprocess_env({"PYTHONPATH": os.pathsep.join(dict.fromkeys([here] + [p for p in sys.path if p]))})
    cmd = [sys.executable, "-m", "meshpipeline.engines.fea_plan_options", str(src), "--to-metres", f"{to_metres!r}"]
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
    with tempfile.TemporaryDirectory(prefix="fea_plan_options_") as tmp:
        ws = Path(tmp)
        stage_file(src, ws, to_metres=to_metres)
        out = plan_staged(ws, source_path=str(src))
    print(json.dumps(out), flush=True)
    return 0


__all__ = ["BUDGET_S", "FULL", "LOW_STRESS", "QUICKER", "bounded", "options", "plan_staged", "row",
           "stage_file", "standard_elements"]


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
