# Responsibility: Observe mesh workload sizing and outcomes without deciding where a run executes.
# Boundaries: Reads only the declared submission payload; never persists, routes, retries, or mutates results.
from __future__ import annotations

import json
import logging
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from meshpipeline.application.mesh_execution_profiles import MeshWorkload, select_profile
from meshpipeline.contracts.mesh_execution import submission_payload_files

logger = logging.getLogger(__name__)

_JSON_LIMIT_BYTES = 2 * 1024 * 1024
_GEOMETRY_SUFFIXES = frozenset({
    ".ecxml", ".fms", ".iges", ".igs", ".msh", ".obj", ".ply", ".step", ".stl",
    ".stp", ".vtk", ".vtp", ".vtu",
})
_CELL_KEYS = frozenset({"max_cells"})
_LAYER_KEYS = frozenset({"boundary_layers", "n_layers"})
_REGION_KEYS = frozenset({"region_count"})


def _binary_stl_triangles(path: Path) -> int | None:
    try:
        size = path.stat().st_size
        if size < 84:
            return None
        with path.open("rb") as stream:
            stream.seek(80)
            count = struct.unpack("<I", stream.read(4))[0]
    except (OSError, struct.error):
        return None
    return count if 84 + 50 * count == size else None


def _numeric_fields(value: Any) -> Iterator[tuple[str, int]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                yield str(key), item
            yield from _numeric_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numeric_fields(item)


def _json_fields(path: Path) -> Iterator[tuple[str, int]]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_JSON_LIMIT_BYTES + 1)
        if len(raw) > _JSON_LIMIT_BYTES:
            return
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return
    yield from _numeric_fields(value)


def workload_from_workspace(workspace: Any, engine: str) -> MeshWorkload:
    files = submission_payload_files(workspace)
    if files is None:
        return MeshWorkload(engine=engine)

    paths = [Path(path) for path in files]
    geometry = [path for path in paths if path.suffix.lower() in _GEOMETRY_SUFFIXES]
    sizes: list[int] = []
    triangles: list[int] = []
    for path in geometry:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
        if path.suffix.lower() == ".stl":
            count = _binary_stl_triangles(path)
            if count is not None:
                triangles.append(count)

    cells: list[int] = []
    layers: list[int] = []
    regions: list[int] = []
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        for key, value in _json_fields(path):
            if key in _CELL_KEYS:
                cells.append(value)
            elif key in _LAYER_KEYS:
                layers.append(value)
            elif key in _REGION_KEYS:
                regions.append(value)

    return MeshWorkload(
        engine=engine,
        source_bytes=sum(sizes) if sizes else None,
        surface_triangles=sum(triangles) if triangles else None,
        requested_cells=max(cells) if cells else None,
        region_count=max(regions) if regions else None,
        boundary_layers=max(layers) if layers else None,
    )


def _emit(workload: MeshWorkload, actual: dict[str, Any]) -> None:
    decision = select_profile(workload)
    record = {
        "policy_version": decision.policy_version,
        "workload": asdict(workload),
        "recommended_profile": decision.profile.value,
        "reasons": list(decision.reasons),
        "actual": actual,
    }
    logger.info("mesh execution profile shadow: %s",
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                extra={"engine": workload.engine})


def _elapsed(started: float) -> float:
    return round(max(0.0, time.monotonic() - started), 3)


def _safe_warning(message: str, *args: Any) -> None:
    try:
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001 - logging cannot affect provider execution
        pass


def _actual_cells(result: dict[str, Any]) -> int | None:
    candidates = []
    for key in ("quality", "remote_quality"):
        quality = result.get(key)
        candidates.append(quality.get("cells") if isinstance(quality, dict) else None)
    candidates.append(result.get("cells"))
    return next((value for value in candidates
                 if isinstance(value, int) and not isinstance(value, bool) and value >= 0), None)


def run_with_shadow_profile(
    workspace: Any,
    engine: str,
    run: Callable[[], dict],
) -> dict:
    try:
        workload = workload_from_workspace(workspace, engine)
    except Exception as exc:  # noqa: BLE001 - observation must never block provider execution
        _safe_warning("mesh execution profile shadow observation failed for %s (%s)",
                      engine, type(exc).__name__)
        return run()

    started = time.monotonic()
    try:
        result = run()
    except BaseException as exc:
        try:
            _emit(workload, {"elapsed_seconds": _elapsed(started),
                             "exception": type(exc).__name__})
        except Exception as observation_error:  # noqa: BLE001 - preserve the provider exception
            _safe_warning("mesh execution profile shadow logging failed for %s (%s)",
                          engine, type(observation_error).__name__)
        raise

    actual = {
        "elapsed_seconds": _elapsed(started),
        "rc": result.get("rc"),
        "timed_out": bool(result.get("timed_out")),
        "cells": _actual_cells(result),
    }
    try:
        _emit(workload, actual)
    except Exception as exc:  # noqa: BLE001 - observation must not alter a completed result
        _safe_warning("mesh execution profile shadow logging failed for %s (%s)",
                      engine, type(exc).__name__)
    return result
