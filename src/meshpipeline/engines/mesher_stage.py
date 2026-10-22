# Responsibility: Say what a running mesher is doing now, in plain words, from the logs it is
# writing in its workspace - snappyHexMesh's phases, cfMesh's steps and the OpenFOAM tools around
# them - so a run's live line can say "adding wall layers" instead of only a clock.
# Boundaries: reads the workspace and nothing else. Pure apart from that read; never raises. It
# judges nothing about the mesh: a stage it cannot name is None, and the line keeps its clock.
from __future__ import annotations

import re
from pathlib import Path

#: How much of a log's end is read: a phase header is always near the end while it runs.
_TAIL_BYTES = 96 * 1024
#: Which log is which tool's, and what that tool is doing while it writes it.
_TOOLS: dict[str, tuple[str, str]] = {
    "blockMesh": ("background", "building the background mesh"),
    "surfaceFeatureExtract": ("features", "finding the sharp edges"),
    "surfaceFeatureEdges": ("features", "finding the sharp edges"),
    "decomposePar": ("splitting", "splitting the work across processors"),
    "reconstructParMesh": ("joining", "joining the pieces back together"),
    "mergeMeshes": ("joining", "joining the pieces back together"),
    "createPatch": ("patches", "naming the boundaries"),
    "improveMeshQuality": ("improving", "improving cell quality"),
    "checkMesh": ("checking", "checking the cells"),
}
#: snappyHexMesh's phases, by the headers it prints - the LAST one in the log is the one running.
_SNAPPY = [
    (re.compile(r"^Refinement phase", re.M), "castellating", "refining and cutting the cells (castellating)"),
    (re.compile(r"^(Morphing phase|Snapping phase|Morph iteration)", re.M), "snapping",
     "snapping the cells to the surface"),
    (re.compile(r"^(Shrinking and layer addition phase|Layer addition phase|Layer addition iteration|"
                r"Adding layers|Shrinking and layer addition)", re.M | re.I), "layers", "adding wall layers"),
    (re.compile(r"^Doing final balancing", re.M), "balancing", "finishing up"),
]
#: cfMesh's cartesianMesh, by the lines it prints for each step.
_CFMESH = [
    (re.compile(r"^(Creating octree|Refining boundary|Refining the octree)", re.M | re.I), "octree",
     "building the octree"),
    (re.compile(r"^(Creating polyMesh from octree|Generating mesh|Adding new cells)", re.M | re.I), "cells",
     "making the cells"),
    (re.compile(r"^(Mapping vertices|Mapping mesh|Smoothing mesh surface)", re.M | re.I), "surface",
     "fitting the cells to the surface"),
    (re.compile(r"^(Adding boundary layers|Generating boundary layers|Extruding)", re.M | re.I), "layers",
     "adding wall layers"),
    (re.compile(r"^(Optimising|Optimizing|Improving mesh quality|Untangling)", re.M | re.I), "improving",
     "improving cell quality"),
]


def _tail(path: Path) -> str:
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - _TAIL_BYTES))
        return fh.read().decode("utf-8", errors="replace")


def _last_of(text: str, patterns) -> tuple[str, str] | None:
    best, at = None, -1
    for rx, key, label in patterns:
        for m in rx.finditer(text):
            if m.start() > at:
                best, at = (key, label), m.start()
    return best


def read_stage(workspace) -> dict | None:
    """The stage the mesher in `workspace` is at now: {"key", "label", "tool"}, from the log
    written most recently; None when there is no log yet, or none this knows."""
    try:
        ws = Path(workspace)
        logs = [p for p in [*ws.glob("*.log"), *ws.glob("*/*.log")] if p.is_file()]
        if not logs:
            return None
        newest = max(logs, key=lambda p: p.stat().st_mtime)
        tool = newest.stem.split("_")[0]
        if tool == "snappyHexMesh":
            found = _last_of(_tail(newest), _SNAPPY)
            key, label = found or ("snappy", "starting snappyHexMesh")
        elif tool == "cartesianMesh":
            found = _last_of(_tail(newest), _CFMESH)
            key, label = found or ("cfmesh", "starting cartesianMesh")
        elif tool in _TOOLS:
            key, label = _TOOLS[tool]
        else:
            return None
        return {"key": key, "label": label, "tool": tool}
    except Exception:  # noqa: BLE001 - a stage we cannot read is a line with only its clock
        return None


__all__ = ["read_stage"]
