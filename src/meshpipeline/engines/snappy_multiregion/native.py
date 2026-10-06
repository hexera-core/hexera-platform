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
    # THE OUTSIDE GOES. Cells in no declared region - the air around a pipe and its wall, which
    # nobody asked to mesh - came back as an undeclared region `domain0` and failed every
    # multi-region case on main (both lab CHT assemblies, every format). Only the cells of the
    # declared regions' zones are kept; the faces they expose join the `exterior` wall patch.
    ("topoSet -dict system/topoSetDict.zoned", "topoSet"),
    ("subsetMesh zoned -overwrite -patch exterior", "subsetMesh"),
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
        if logname == "topoSet":
            empty = empty_regions(ws)
            if empty:
                # NO CELL IN A REGION: splitMeshRegions would crash on the empty mesh (signal 11,
                # "regionProperties is missing" - diffuser_plenum_004_cht, lab 2026-10-06). Said
                # here with the cause snappy leaves behind: it could not tell the region's inside
                # from its outside because the region's surface is not closed.
                logs.append(f"[regions] no cells in {', '.join(empty)}")
                why = empty_regions_reason(ws, empty)
                # ...and kept beside the case, so the split's absence is reported by its cause
                # (regions.read_region_properties), not as "regionProperties is missing"
                # (without the engine tag: the failure that reads it adds its own)
                (ws / STOP_REASON_FILE).write_text(why.removeprefix("[SNAPPY_MULTIREGION] "))
                out = describe_native_result(
                    returncode=1, args=["bash", "-lc", command], stage="regions",
                    output="\n".join(logs) + "\n" + why)
                out["empty_regions"] = empty
                return out

    # THE USER'S BOUNDARY NAMES. Each region's `exterior` faces become the declared ports where
    # they lie at a declared port, and the declared wall everywhere else.
    for _region, cmds in name_exterior(ws, regions):
        for command, logname in cmds:
            outcome = run_stage(ws, command, logname, env=env, bashrc=bashrc, timeout=timeout,
                                logs=logs)
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
    # parse_layer_coverage reports the areal headline as "overall_pct" (the old read of a
    # "coverage" key matched nothing, so the build result's layer figure was always None).
    result.update({"layer_coverage": layer.get("overall_pct"),
                   "per_patch_layers": layer.get("per_patch")})
    return result


#: why the run stopped before the region split, in words (read by regions.read_region_properties)
STOP_REASON_FILE = "multiregion_stop_reason.txt"


def empty_regions(ws: Path) -> list[str]:
    """The declared regions whose cellZone came out of snappyHexMesh with no cells, read off the
    topoSet log ("Using zone <name> with 0 cells")."""
    import re
    try:
        text = (Path(ws) / "log.topoSet").read_text(errors="replace")
    except OSError:
        return []
    return sorted({m.group(1) for m in re.finditer(r"Using zone (\S+) with 0 cells", text)})


def empty_regions_reason(ws: Path, empty: list[str]) -> str:
    """Why a region got no cells, in words, with the open edges surfaceFeatureExtract counted on
    the region surfaces when it reported any."""
    import re
    opens: list[int] = []
    try:
        text = (Path(ws) / "log.surfaceFeatureExtract").read_text(errors="replace")
        opens = [int(n) for n in re.findall(r"open edges\s*:\s*(\d+)", text)]
    except OSError:
        pass
    where = (f" (the region surfaces have {sum(opens)} open edge(s))" if any(opens) else "")
    return (f"[SNAPPY_MULTIREGION] no cell was placed in region(s) {', '.join(empty)}: "
            "snappyHexMesh could not tell their inside from their outside, because a region's "
            f"surface is not closed{where}. The solids of the assembly must be closed and share "
            "matching faces; re-export it with each solid closed (or stitched), then mesh again.")


_PORT_BOX = 0.6   # a port's faces are taken within this many port diameters of its centre


def _boundary_patches(boundary_file: Path) -> list[str]:
    import re
    try:
        text = boundary_file.read_text(errors="replace")
    except OSError:
        return []
    return re.findall(r"^\s*([A-Za-z_][\w.-]*)\s*\n\s*\{\s*\n\s*type", text, re.M)


def _declared(ws: Path) -> list[dict]:
    try:
        d = json.loads((ws / "port_declaration.json").read_text())
        return [p for p in d if isinstance(p, dict) and p.get("name")]
    except (OSError, ValueError):
        return []


def _port_diameter_m(p: dict) -> float | None:
    for k in ("diameter_mm", "outer_diameter_mm"):
        if p.get(k):
            return float(p[k]) / 1000.0
    if p.get("width_mm") and p.get("height_mm"):
        return max(float(p["width_mm"]), float(p["height_mm"])) / 1000.0
    if p.get("area_mm2"):
        return 2.0 * (float(p["area_mm2"]) / 3.141592653589793) ** 0.5 / 1000.0
    return None


def name_exterior(ws: Path, regions: list) -> list[tuple[str, list[tuple[str, str]]]]:
    """Per region with exterior faces: write its topoSetDict / createPatchDict and return the
    commands that turn `exterior` into the declared ports (fluid regions: the exterior faces
    inside a box of _PORT_BOX diameters around each declared port's location) and the declared
    wall (everything else)."""
    declared = _declared(ws)
    wall = next((p["name"] for p in declared if p.get("type") == "wall"), "wall")
    ports = [p for p in declared if p.get("type") in ("inlet", "outlet") and p.get("near_mm")]
    kind = {str(r.get("name")): str(r.get("type")) for r in regions or [] if isinstance(r, dict)}
    hdr = "FoamFile {{ version 2.0; format ascii; class dictionary; object {obj}; }}\n"
    out = []
    for region in sorted(kind):
        bnd = ws / "constant" / region / "polyMesh" / "boundary"
        if "exterior" not in _boundary_patches(bnd):
            continue
        sysdir = ws / "system" / region
        sysdir.mkdir(parents=True, exist_ok=True)
        cmds: list[tuple[str, str]] = []
        made: list[str] = []
        if kind[region] == "fluid" and ports:
            acts = []
            for p in ports:
                d = _port_diameter_m(p) or 0.0
                c = [float(v) / 1000.0 for v in p["near_mm"]]
                r = max(_PORT_BOX * d, 1e-9)
                lo = " ".join(f"{c[k] - r:.9g}" for k in range(3))
                hi = " ".join(f"{c[k] + r:.9g}" for k in range(3))
                n = f"port_{p['name']}"
                acts += [f"    {{ name {n}; type faceSet; action new; source patchToFace; "
                         "patch exterior; }",
                         f"    {{ name {n}; type faceSet; action subset; source boxToFace; "
                         f"box ({lo}) ({hi}); }}"]
                made.append(str(p["name"]))
            (sysdir / "topoSetDict").write_text(hdr.format(obj="topoSetDict")
                                                + "actions\n(\n" + "\n".join(acts) + "\n);\n")
            cmds.append((f"topoSet -region {region}", f"topoSet.{region}"))
        # TWO PASSES: a whole-patch move in the same dict as the set moves overrides them (the
        # ports came back empty and every exterior face became wall) - so the ports first, then
        # what is left of the exterior becomes the wall.
        if made:
            entries = [f"    {{ name {n}; patchInfo {{ type patch; }} constructFrom set; "
                       f"set port_{n}; }}" for n in made]
            (sysdir / "createPatchDict").write_text(hdr.format(obj="createPatchDict")
                                                    + "pointSync false;\npatches\n(\n"
                                                    + "\n".join(entries) + "\n);\n")
            cmds.append((f"createPatch -region {region} -overwrite", f"createPatch.{region}"))
        (sysdir / "createPatchDict.wall").write_text(
            hdr.format(obj="createPatchDict") + "pointSync false;\npatches\n(\n"
            + f"    {{ name {wall}; patchInfo {{ type wall; }} constructFrom patches; "
            "patches (exterior); }\n);\n")
        # The wall pass reads its dict under the default name: `-dict` with `-region` resolves
        # differently across OpenFOAM lines (v2412 looked for system/<r>/<r>/...). And only when
        # something is left of the exterior: v2412's createPatch drops a patch the ports emptied
        # (a fluid enclosed by its pipe has no outside but its ports), OpenFOAM 11 keeps it.
        cmds.append((f"if grep -qE '^[[:space:]]*exterior[[:space:]]*$' "
                     f"constant/{region}/polyMesh/boundary; then "
                     f"cp system/{region}/createPatchDict.wall system/{region}/createPatchDict && "
                     f"createPatch -region {region} -overwrite; fi", f"createPatch.wall.{region}"))
        out.append((region, cmds))
    return out


__all__ = ["NATIVE_STAGES", "StageOutcome", "name_exterior", "run_native_build", "run_stage"]
