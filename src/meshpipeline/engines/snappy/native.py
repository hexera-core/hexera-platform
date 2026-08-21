# Responsibility: Run blockMesh, surfaceFeatureExtract and snappyHexMesh and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import os
import pathlib
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
    cpus = _container_cpus(os.cpu_count() or 1)
    ranks = min(cpus, hard_max)
    mem = _container_mem_bytes()
    if mem:
        by_mem = int(mem / (gib_per_rank * (1 << 30)))
        ranks = min(ranks, max(1, by_mem))
    return max(1, ranks)


def _run_snappy_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                      timeout: int = 1800) -> dict:
    from meshpipeline.engines.snappy import parallel_stages as ps

    ws = Path(workspace)
    nproc = _rank_count()
    attempt_started = _time.time()
    stages: list[ps.StageResult] = []

    def _sh(cmd: str, logname: str, *, name: str = "", authoritative: bool = True,
            expected: tuple[str, ...] = ()) -> ps.StageResult:
        full = f"source {bashrc} >/dev/null 2>&1 && {cmd}"
        t0 = _time.time()
        rc, timed_out = 0, False
        try:
            with (ws / logname).open("w") as fh:
                proc = run_guarded(["bash", "-lc", full], cwd=str(ws), env=_foam_env(),
                                   stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
            rc = proc.returncode
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
        # QUALITY IS MEASURED HERE, beside the mesh, and travels home in the result.
        #
        # The caller runs check_mesh() itself, which shells out to `checkMesh` - an OpenFOAM
        # binary. That works when the mesh was built in the same container. On the Cloud Run
        # path it cannot: the local worker image carries no OpenFOAM at all, so every metric
        # came back empty. It is why every log line reads `cells=None`, and why the reviewer
        # refused each run for missing `max_non_ortho` evidence AFTER a mesh had been built,
        # paid for, and passed both the manifest and solvability gates.
        #
        # Measuring on this side is not a workaround for that - it is where the measurement
        # belongs. The mesh is here, the binary is here, and the numbers describe bytes that
        # have not yet crossed a network.
        if (ws / "constant" / "polyMesh" / "owner").exists():
            try:
                from meshpipeline.engines.snappy.foam_exec import check_mesh
                q = check_mesh(ws, bashrc=bashrc)
                if q:
                    out["quality"] = q
                    # Beside the mesh, not just in the return value. The measurement has three
                    # more readers on the far side - finalize (which writes it into the manifest
                    # the reviewer reads), solvability, and the judge - and threading a return
                    # value to each is four chances to miss one. A file in the workspace travels
                    # home with the mesh it describes and every reader finds it the same way.
                    (ws / "mesh_quality.json").write_text(json.dumps(q, default=str))
            except Exception:  # noqa: BLE001 - a measurement must never lose a finished mesh
                logger.warning("checkMesh after meshing failed; quality omitted", exc_info=True)
            # THE VOLUME EXPORT, for the same reason and by the same route. The reviewer slices
            # the mesh to see inside it, and every slice comes from this .vtu. It is written by
            # foamToVTK - another OpenFOAM binary that exists here and nowhere on the far side -
            # so calling it there produced nothing, silently: bash exits nonzero, run_guarded
            # raises nothing, the glob finds no file and returns None without a word. The manifest
            # then declared five inspection regions the renderer had no volume to cut, and the
            # reviewer refused a mesh that had passed every other gate.
            try:
                from meshpipeline.engines.snappy.foam_exec import export_volume_vtk
                if export_volume_vtk(ws, bashrc=bashrc):
                    logger.info("volume VTK exported beside the mesh for review")
            except Exception:  # noqa: BLE001 - an export must never lose a finished mesh
                logger.warning("foamToVTK after meshing failed; the reviewer will have no "
                               "volume to slice", exc_info=True)
        return out

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
        if not timed_out:                                   # keep the workspace / round-trip tar lean
            for pd in ws.glob("processor*"):                # (preserve processor dirs on timeout as evidence)
                shutil.rmtree(pd, ignore_errors=True)
    else:
        _sh("snappyHexMesh -overwrite", "snappyHexMesh.log",
            expected=("constant/polyMesh/owner",))

    return _finish()


__all__ = ["_foam_version", "_run_snappy_local", "_snappy_result", "run_snappy"]
