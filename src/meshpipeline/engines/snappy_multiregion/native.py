# Responsibility: Run snappyHexMesh followed by splitMeshRegions and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from meshpipeline.engines.snappy_multiregion.foam_exec import (
    _DEFAULT_BASHRC,
    _foam_env,
    scan_case_dicts,
)
from meshpipeline.sandbox.safe_exec import (
    NativeOutcome,
    describe_native_result,
    run_guarded,
)

logger = logging.getLogger(__name__)

#: (command, log name). ORDER IS CONTRACT - see the module docstring.
NATIVE_STAGES: tuple[tuple[str, str], ...] = (
    ("blockMesh", "blockMesh"),
    ("surfaceFeatureExtract", "surfaceFeatureExtract"),
    ("snappyHexMesh -overwrite", "snappyHexMesh"),
    ("splitMeshRegions -cellZones -overwrite", "splitMeshRegions"),
)


@dataclass(frozen=True)
class StageOutcome:

    stage: str
    returncode: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_stage(workspace: Path, command: str, logname: str, *, env: dict, bashrc: str,
              timeout: int, logs: list[str]) -> StageOutcome:
    full = f"source {bashrc} >/dev/null 2>&1 && {command}"
    try:
        p = run_guarded(["bash", "-lc", full], cwd=str(workspace), env=env,
                        capture_output=True, text=True, timeout=timeout)
        (workspace / f"log.{logname}").write_text((p.stdout or "") + "\n" + (p.stderr or ""))
        logs.append(f"[{logname}] rc={p.returncode}")
        return StageOutcome(stage=logname, returncode=p.returncode)
    except subprocess.TimeoutExpired:
        # The guard owns the process tree; a timeout is reported as such and never as rc=0.
        logs.append(f"[{logname}] TIMEOUT")
        return StageOutcome(stage=logname, returncode=-1, timed_out=True)


def run_native_build(workspace, *, preflight, render_region_properties, parse_layer_coverage,
                     bashrc: str = _DEFAULT_BASHRC, timeout: int = 2400) -> dict:
    ws = Path(workspace)
    # BEFORE ANY NATIVE COMMAND. A workspace whose region map never validated has no blockMeshDict,
    # no merged region surfaces and no .regions.json - running blockMesh on it spends the OpenFOAM
    # startup cost only to fail with rc=1 and a dictionary error, which tells an operator nothing
    # about the actual problem. `configure_mesh` already rejects an invalid map; this refuses to
    # launch when that rejection was ignored.
    unusable = preflight(ws)
    if unusable:
        return {"rc": -2, "timed_out": False, "code": unusable["code"],
                "log_tail": f"REJECTED before blockMesh: {unusable['error']}",
                **{k: v for k, v in unusable.items() if k not in ("code", "error")}}
    reason = scan_case_dicts(ws)
    if reason:
        return {"rc": 1, "timed_out": False, "log_tail": f"case dicts rejected: {reason}"}

    regions = (json.loads((ws / ".regions.json").read_text())
               if (ws / ".regions.json").exists() else [])
    env = _foam_env()
    logs: list[str] = []

    for command, logname in NATIVE_STAGES:
        outcome = run_stage(ws, command, logname, env=env, bashrc=bashrc, timeout=timeout,
                            logs=logs)
        if outcome.timed_out:
            return describe_native_result(returncode=-1, args=["bash", "-lc", command],
                                          stage=outcome.stage, output="\n".join(logs),
                                          outcome=NativeOutcome.timed_out)
        if not outcome.ok:
            return describe_native_result(returncode=outcome.returncode,
                                          args=["bash", "-lc", command],
                                          stage=outcome.stage, output="\n".join(logs))

    # The split succeeded: declare the regions the solver will read. Written only here, after
    # every stage passed - a regionProperties beside an unfinished split would describe a case
    # that does not exist.
    (ws / "constant" / "regionProperties").write_text(render_region_properties(regions))
    snappy_log = ((ws / "log.snappyHexMesh").read_text(errors="replace")
                  if (ws / "log.snappyHexMesh").exists() else "")
    layer = parse_layer_coverage(snappy_log)
    result = describe_native_result(returncode=0, args=["bash", "-lc", "splitMeshRegions"],
                                    stage="splitMeshRegions", output="\n".join(logs))
    result.update({"layer_coverage": layer.get("coverage"),
                   "per_patch_layers": layer.get("per_patch")})
    return result


__all__ = ["NATIVE_STAGES", "StageOutcome", "run_native_build", "run_stage"]
