# Responsibility: Run blockMesh, surfaceFeatureExtract and snappyHexMesh and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import time as _time
from pathlib import Path

from meshpipeline.engines.snappy.foam_exec import (
    _DEFAULT_BASHRC,
    _foam_env,
    scan_case_dicts,
)
from meshpipeline.engines.snappy_hexmesh import parse_layer_coverage
from meshpipeline.sandbox.safe_exec import (
    NativeOutcome,
    describe_native_result,
    run_guarded,
)

logger = logging.getLogger(__name__)

#: The FoamFile header a case dictionary opens with.
_HDR = ("FoamFile{{ version 2.0; format ascii; class {cls}; object {obj}; }}\n")

#: Wall seconds per native step (every stage, snappyHexMesh's own three phases, checkMesh, the
#: passage measure, foamToVTK, total) and the rank count, written beside the mesh and carried in
#: the result as `timing` - the record cfMesh and Netgen already keep under the same name.
TIMING_FILE = "mesh_timing.json"

#: snappyHexMesh's own clock for each phase, as its log prints it ("Mesh refined in = 12.3 s.").
_SNAPPY_PHASES = (("castellate_s", re.compile(r"Mesh refined in\s*=\s*([0-9.eE+-]+)\s*s")),
                  ("snap_s", re.compile(r"Mesh snapped in\s*=\s*([0-9.eE+-]+)\s*s")),
                  ("layers_s", re.compile(r"Layers added in\s*=\s*([0-9.eE+-]+)\s*s")))


def stage_timing(stages: list, snappy_log: str, *, ranks: int) -> dict:
    """{step: wall seconds} of one native run: each stage as it ran (a name run twice adds up),
    snappyHexMesh's castellate / snap / layer phases from its own log, and the rank count."""
    timing: dict = {"ranks": int(ranks)}
    for s in stages:
        name = getattr(s, "name", "") or "stage"
        words = str(getattr(s, "command", "")).split()
        if name == "mpirun" and "-np" in words and words.index("-np") + 2 < len(words):
            name = words[words.index("-np") + 2]       # `mpirun ... -np N <tool>`: the tool
        key = f"{name}_s"
        timing[key] = round(float(timing.get(key, 0.0)) + float(getattr(s, "dt_s", 0.0) or 0.0), 2)
    for key, pat in _SNAPPY_PHASES:
        hits = pat.findall(snappy_log or "")
        if hits:
            try:
                timing[key] = round(float(hits[-1]), 1)
            except ValueError:
                pass
    return timing


def _measure(ws: Path, bashrc: str, out: dict, timing: dict) -> None:
    """checkMesh (+ the passage measure on internal flow) on the finished mesh, into
    `out["quality"]` and mesh_quality.json beside it; seconds into `timing`. Never raises."""
    # QUALITY IS MEASURED HERE, beside the mesh, and travels home in the result. The worker
    # carries no OpenFOAM, so a checkMesh there came back empty on every Cloud Run mesh (`cells=None`
    # in every log line, the reviewer refusing for missing `max_non_ortho` evidence after a mesh
    # had been built and paid for). The mesh is here, the binary is here.
    try:
        from meshpipeline.engines.snappy.foam_exec import check_mesh
        t1 = _time.monotonic()
        q = check_mesh(ws, bashrc=bashrc)
        timing["checkMesh_s"] = round(_time.monotonic() - t1, 1)
        if not q:
            return
        # cells across the local passage at every wall point (internal flow):
        # flow_gates.resolution_floor holds 12 at the narrowest wall
        try:
            from meshpipeline.engines.passage import passage_of_polymesh
            from meshpipeline.engines.workspace_facts import read_flow_topology
            # the fact the driver ships in the payload (drivers.REMOTE_WORKSPACE_FACTS); read
            # through the one reader, so a workspace without it is "not internal" rather than a
            # traceback after every mesh
            if read_flow_topology(ws) == "internal":
                t2 = _time.monotonic()
                q.update(passage_of_polymesh(ws))
                timing["passage_measure_s"] = round(_time.monotonic() - t2, 1)
        except Exception:  # noqa: BLE001 - evidence, not a verdict
            logger.warning("passage measure after meshing failed; omitted", exc_info=True)
        out["quality"] = q
        # Beside the mesh, not just in the return value: finalize, solvability and the judge
        # read it on the far side, and a file travels home with the mesh it describes.
        (ws / "mesh_quality.json").write_text(json.dumps(q, default=str))
    except Exception:  # noqa: BLE001 - a measurement must never lose a finished mesh
        logger.warning("checkMesh after meshing failed; quality omitted", exc_info=True)


def _export_volume_timed(ws: Path, bashrc: str) -> float:
    """foamToVTK's volume export beside the mesh (the reviewer slices it; the worker has no
    OpenFOAM to write it); its wall seconds. Never raises."""
    t0 = _time.monotonic()
    try:
        from meshpipeline.engines.snappy.foam_exec import export_volume_vtk
        if export_volume_vtk(ws, bashrc=bashrc):
            logger.info("volume VTK exported beside the mesh for review")
    except Exception:  # noqa: BLE001 - an export must never lose a finished mesh
        logger.warning("foamToVTK after meshing failed; the reviewer will have no volume to "
                       "slice", exc_info=True)
    return round(_time.monotonic() - t0, 1)


def run_snappy(workspace, *, context=None, bashrc: str = _DEFAULT_BASHRC,
               timeout: int = 1800) -> dict:
    reason = scan_case_dicts(workspace)
    if reason:
        return {"rc": -2, "timed_out": False, "log_tail": f"REJECTED: {reason}",
                "layer_coverage": 0.0, "per_patch_layers": {}}
    from meshpipeline.contracts.mesh_execution import run_mesh
    result = run_mesh(workspace, engine="snappy", timeout=timeout)
    # A cloud FAILURE dict carries only {rc, timed_out, log_tail}; guarantee the layer keys
    # the snappy path reads so a dispatch failure degrades cleanly (no KeyError downstream).
    result.setdefault("layer_coverage", 0.0)
    result.setdefault("per_patch_layers", {})
    return result


def _snappy_result(ws: Path, rc: int, timed_out: bool, snappy_log: str,
                   *, stages: list | None = None, stage_note: str = "",
                   benign: dict | None = None) -> dict:
    tail = "\n".join((snappy_log or "").splitlines()[-30:]) if snappy_log else ""
    cov = parse_layer_coverage(snappy_log)
    # The shared reading first, then this bundle's own layer facts on top. A signal is named
    # here exactly as it is for every other engine, because the seam does not know the engine.
    result = describe_native_result(
        returncode=rc, args=["bash", "-lc", "snappyHexMesh"],
        stage=stage_note or "snappyHexMesh", output=tail,
        outcome=NativeOutcome.timed_out if timed_out else None)
    result.update({"layer_coverage": cov["overall_pct"], "per_patch_layers": cov["per_patch"],
                   "stages": [s.to_dict() if hasattr(s, "to_dict") else s for s in (stages or [])],
                   "stage_note": stage_note, "benign": benign})
    return result


def _foam_version(bashrc: str) -> str:
    try:
        out = subprocess.run(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && "
                              "printf %s \"$WM_PROJECT_VERSION\""],
                             capture_output=True, text=True, timeout=30, env=_foam_env())
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001 - version context is diagnostic, never fatal
        return ""


def _cgroup_value(path: str) -> str:
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return ""


def _container_cpus(fallback: int) -> int:
    """CPUs this container may actually use, not the host's core count.

    cpu.max (v2) and cfs_quota_us (v1) express a QUOTA, which no affinity mask reflects:
    Cloud Run hands the process a wide mask and then throttles it. Reading the quota is
    the only way to see the limit the job was actually configured with.
    """
    raw = _cgroup_value("/sys/fs/cgroup/cpu.max")           # v2: "<quota> <period>" or "max ..."
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                return max(1, int(float(parts[0]) / float(parts[1])))
            except (ValueError, ZeroDivisionError):
                pass
    quota = _cgroup_value("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")   # v1
    period = _cgroup_value("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        q, per = int(quota), int(period)
        if q > 0 and per > 0:
            return max(1, q // per)
    except ValueError:
        pass
    return fallback


def _container_mem_bytes() -> int:
    for path in ("/sys/fs/cgroup/memory.max",                      # v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):   # v1
        raw = _cgroup_value(path)
        if raw and raw != "max":
            try:
                v = int(raw)
                # v1 reports a sentinel near 2**63 when unlimited.
                if 0 < v < (1 << 62):
                    return v
            except ValueError:
                pass
    return 0


def _rank_count(*, hard_max: int = 12, gib_per_rank: float = 3.0) -> int:
    """How many MPI ranks this container can afford - bounded by MEMORY, not just CPU.

    Two bugs lived in the one-liner this replaces (`min(os.cpu_count() or 1, 12)`):

    os.cpu_count() reports the HOST's cores and ignores the cgroup quota entirely, so a
    4-vCPU Cloud Run job ran `-np 5` and an 8-vCPU job ran `-np 10` - every rank
    oversubscribed, with `--oversubscribe` hiding it rather than failing.

    And CPU was the wrong budget to begin with. Every snappy rank carries its own copy of
    the surface and its mesh partition, so total memory grows with rank count. Doubling
    the machine doubled the ranks and left memory-per-rank flat, which is why a 16 GiB job
    was OOM-killed at the same stage an 8 GiB one was. Ranks must be capped by memory or
    buying a bigger container buys nothing.
    """
    # REPEATABLE (engines/determinism.py): the rank count decides how snappy cuts the domain,
    # so it comes from the job's configuration (its CPU quota, or a pinned value), never from
    # the host it lands on; memory may only lower it, and the stamp says when it did.
    from meshpipeline.engines import determinism as det
    ranks, basis = det.snappy_ranks(_container_cpus(0) or None, _container_mem_bytes(),
                                    hard_max=hard_max, gib_per_rank=gib_per_rank,
                                    host_cpus=os.cpu_count())
    det.note_setting("snappy_ranks", ranks, basis)
    det.note_setting("snappy_decomposition", det.SNAPPY_DECOMPOSITION if ranks >= 2 else "serial")
    return ranks


def _run_snappy_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                      timeout: int = 1800) -> dict:
    from meshpipeline.contracts.native_time import STALL_WINDOW_S
    from meshpipeline.engines.snappy import parallel_stages as ps
    from meshpipeline.sandbox.safe_exec import MemoryExpired, StalledExpired, StoppedByWatch

    ws = Path(workspace)
    nproc = _rank_count()
    attempt_started = _time.time()
    stages: list[ps.StageResult] = []
    # NO GUESSED TIME LIMIT (contracts/native_time): `timeout` is the WALL of the machine this runs
    # on, for the whole run - every stage shares it - and each stage is WATCHED: stopped only when
    # its log has not grown for the stall window. A slow snappy that is still refining, snapping or
    # adding layers is never cut (it was, at 2,400 s, and then re-planned smaller).
    wall_at = _time.monotonic() + float(timeout)
    stalled: list[str] = []
    oom: list[float] = []      # stopped before the machine ran out of memory (GiB in use then)

    # THE SIZE BRAKE (engines/size_brake): an external body's snappyHexMesh is stopped as
    # soon as its own log says the mesh is headed far past its cell budget - after castellation, or
    # after snapping and before the layer stage - and the driver re-plans the pass smaller
    oversize: list[dict] = []

    def _brake_for(logname: str):
        if not logname.startswith("snappyHexMesh"):
            return None
        from meshpipeline.engines.size_brake import SizeBrake, case_budget
        from meshpipeline.engines.workspace_facts import read_flow_topology
        budget = case_budget(ws)
        if not budget or read_flow_topology(ws) == "internal":
            return None
        return SizeBrake(ws / logname, budget)

    def _sh(cmd: str, logname: str, *, name: str = "", authoritative: bool = True,
            expected: tuple[str, ...] = ()) -> ps.StageResult:
        full = f"source {bashrc} >/dev/null 2>&1 && {cmd}"
        t0 = _time.time()
        rc, timed_out = 0, False
        brake = _brake_for(logname)
        try:
            with (ws / logname).open("w") as fh:
                proc = run_guarded(["bash", "-lc", full], cwd=str(ws), env=_foam_env(),
                                   stdout=fh, stderr=subprocess.STDOUT,
                                   timeout=max(1.0, wall_at - _time.monotonic()),
                                   stall_s=STALL_WINDOW_S, watch=brake)
            rc = proc.returncode
        except StoppedByWatch:
            rc = -1
            if brake is not None and brake.tripped:
                oversize.append(dict(brake.tripped))
        except MemoryExpired as exc:
            rc, timed_out = -1, False
            oom.append(float(exc.used_gib))
        except StalledExpired:
            rc, timed_out = -1, True
            stalled.append(name or cmd.split()[0])
        except subprocess.TimeoutExpired:
            rc, timed_out = -1, True
        present = {p: (ws / p).exists() and (ws / p).stat().st_size > 0 for p in expected}
        s = ps.StageResult(name=name or cmd.split()[0], command=cmd, log=logname, rc=rc,
                           timed_out=timed_out, dt_s=round(_time.time() - t0, 2),
                           authoritative=authoritative, expected_outputs=expected,
                           outputs_present=present)
        stages.append(s)
        return s

    def _finish() -> dict:
        log = ws / "snappyHexMesh.log"
        snap_log = log.read_text(errors="replace") if log.exists() else ""
        verdict = ps.classify(ws, stages, foam_version=_foam_version(bashrc),
                              attempt_started=attempt_started)
        out = _snappy_result(ws, verdict["rc"], verdict["timed_out"], snap_log,
                             stages=stages, stage_note=verdict["note"],
                             benign=verdict["benign"])
        # STUCK, not slow: the stage that made no progress for the stall window (the driver's
        # re-plan-smaller lever answers only this; a run stopped at the machine's wall still
        # working moves to a bigger machine instead - contracts/mesh_machine.off_the_wall)
        if stalled:
            out["stalled"] = True
            out["stalled_stage"] = stalled[-1]
        if oom:
            # OUT OF MEMORY, stopped cleanly (sandbox/safe_exec.MemoryExpired): one machine up
            out.update(out_of_memory=True, timed_out=False, used_gib=oom[-1])
        if oversize:
            # STOPPED BY THE SIZE BRAKE: headed far past the budget (its count, stage and ratio)
            out["oversize"] = oversize[-1]
        # quality and the volume export are measured HERE, beside the mesh (see _measure)
        timing = stage_timing(stages, snap_log, ranks=nproc)
        if (ws / "constant" / "polyMesh" / "owner").exists():
            # THE VOLUME EXPORT RUNS BESIDE THE MEASUREMENT, not after it: foamToVTK and
            # checkMesh are both serial readers of the same finished mesh and write to
            # different places (VTK/ and constant/polyMesh/sets), so the slower one sets the
            # wall instead of the two adding up - 34 s + 34 s on a 3.2 M-cell car at 8 ranks.
            from concurrent.futures import ThreadPoolExecutor
            t_m = _time.monotonic()
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="snappy-vtk") as pool:
                vtk = pool.submit(_export_volume_timed, ws, bashrc)
                _measure(ws, bashrc, out, timing)
                timing["foamToVTK_s"] = vtk.result()
            timing["measure_wall_s"] = round(_time.monotonic() - t_m, 1)
        timing["total_s"] = round(_time.time() - attempt_started, 1)
        out["timing"] = timing
        try:
            (ws / TIMING_FILE).write_text(json.dumps(timing))
        except OSError:
            pass
        logger.info("snappy native steps (s): %s", timing)
        return out

    def _merge_class_patches():
        # The renderer authors system/createPatchDict only when the layer policy split the wall
        # into synthetic class regions (see render_snappy_case): fold them back into the declared
        # wall so the delivered boundary is exactly the one the user signed. Authoritative - a
        # merge that fails would leave the contract unmet, which is a failed run either way.
        if (ws / "system" / "createPatchDict").exists():
            _sh("createPatch -overwrite", "createPatch.log",
                expected=("constant/polyMesh/boundary",))

    # background grid + feature edges (fast, serial). A pre-parallel failure short-circuits.
    if not _sh("blockMesh", "blockMesh.log",
               expected=("constant/polyMesh/points",)).ok:
        return _finish()
    if not _sh("surfaceFeatureExtract", "surfaceFeatureExtract.log").ok:
        return _finish()

    if nproc >= 2:
        (ws / "system" / "decomposeParDict").write_text(
            _HDR.format(cls="dictionary", obj="decomposeParDict")
            + f"numberOfSubdomains {nproc}; method scotch;\n")
        dp = _sh("decomposePar -force", "decomposePar.log", expected=("processor0",))
        timed_out = dp.timed_out
        if dp.ok:
            sm = _sh(f"mpirun --allow-run-as-root --oversubscribe -np {nproc} "
                     "snappyHexMesh -overwrite -parallel", "snappyHexMesh.log")
            timed_out = timed_out or sm.timed_out
            if sm.ok:
                rp = _sh("reconstructParMesh -constant -mergeTol 1e-6",
                         "reconstructParMesh.log",
                         expected=("constant/polyMesh/owner", "constant/polyMesh/boundary"))
                timed_out = timed_out or rp.timed_out
                if rp.ok:
                    _merge_class_patches()
        if not timed_out:                                   # keep the workspace / round-trip tar lean
            for pd in ws.glob("processor*"):                # (preserve processor dirs on timeout as evidence)
                shutil.rmtree(pd, ignore_errors=True)
    else:
        # PROGRESSIVE SNAPSHOTS (engines/snappy/progressive): with the deployment's switch on, the
        # serial run is invoked WITHOUT `-overwrite`, so each phase's mesh is kept in its own
        # numbered time directory and the runner can publish a frame of the castellated one while
        # snappy is still working. With the switch off this is `snappyHexMesh -overwrite`, exactly
        # as it has always been, and no branch below it runs.
        from meshpipeline.engines.snappy import progressive as prog
        snapshotting = prog.snapshots_enabled(nproc=nproc)
        sm = _sh(prog.snappy_command(nproc=nproc), "snappyHexMesh.log",
                 expected=() if snapshotting else ("constant/polyMesh/owner",))
        if sm.ok and snapshotting:
            # AUTHORITATIVE: the phase meshes are in `1/`, `2/`, `3/` and `constant/polyMesh` still
            # holds blockMesh's background box, which every gate downstream would read as the
            # delivered mesh. Promotion moves the last phase's mesh into place and removes the
            # intermediates; when it cannot, it removes `constant/polyMesh` so the run fails on a
            # missing mesh rather than delivering the box.
            promoted = ps.StageResult(
                name="promote_phase_mesh", command="(progressive snapshots: promote phase mesh)",
                log="snappyHexMesh.log", rc=0 if prog.promote_final_mesh(ws) else -1,
                timed_out=False, dt_s=0.0, authoritative=True,
                expected_outputs=("constant/polyMesh/owner",),
                outputs_present={"constant/polyMesh/owner":
                                 (ws / "constant" / "polyMesh" / "owner").exists()})
            stages.append(promoted)
            if promoted.ok:
                _merge_class_patches()
        elif snapshotting:
            # A run that failed or was stopped still has its intermediates on disk, and they ride
            # home in the archive (engines/return_scope: a 1M-cell polyMesh is about 42 MB and the
            # extraction ceiling is 512 MiB). The logs are the evidence of a failed phase; a second
            # copy of the mesh is not.
            prog.drop_time_dirs(ws)
        elif sm.ok:
            _merge_class_patches()

    return _finish()


__all__ = ["TIMING_FILE", "_foam_version", "_run_snappy_local", "_snappy_result", "run_snappy", "stage_timing"]
