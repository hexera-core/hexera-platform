# Responsibility: Run blockMesh, surfaceFeatureExtract and snappyHexMesh and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import logging
import os
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


def _run_snappy_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                      timeout: int = 1800) -> dict:
    from meshpipeline.engines.snappy import parallel_stages as ps

    ws = Path(workspace)
    nproc = min(os.cpu_count() or 1, 12)
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
        return _snappy_result(ws, verdict["rc"], verdict["timed_out"], snap_log,
                              stages=stages, stage_note=verdict["note"],
                              benign=verdict["benign"])

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
