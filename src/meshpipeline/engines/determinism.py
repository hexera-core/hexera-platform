# Responsibility: Make one input give one mesh: pin what a native mesher may vary by (threads, ranks, hash order) and stamp every run with what it ran under.
# Owns: the determinism settings, the native environment they pin, the record of every time a wall clock changed a decision, and the reproducibility stamp written beside the mesh.
# Boundaries: mesh side and worker side alike; it records and pins, it never judges a mesh. Engines read their pinned values from here instead of from the host.
# Collaborates with: engines/dispatch.run_engine_local (opens a run, pins, stamps), engines/snappy/native (ranks), engines/gmsh/driver (seed), the time-limited call sites (note_time_limit).
"""REPEATABILITY: SAME CLEAN FILE IN, SAME RESULT OUT.

A mesher's result may depend on more than its input and its plan:

  * threads - cfMesh (OpenMP) and gmsh insert and smooth points in the order threads finish;
    a cfMesh volute gave max non-orthogonality 82.4 / 84.3 / 85.6 on three identical runs;
  * MPI ranks - snappyHexMesh in parallel cuts the domain by the rank count, and the rank count
    came from the machine (its CPU quota and memory), so a 20 GB lab slot ran 6 ranks and a 32 GB
    one 8;
  * hash order - Python's string hash is salted per process, so anything iterating a set of
    names can write patches or regions in another order on the next run;
  * the wall clock - a pass skipped "because too little time is left" depends on how loaded the
    machine was during the passes before it.

This module fixes the first three by default (MESH_DETERMINISTIC) and records the fourth: every
time a time limit changed what a run did, `note_time_limit` says so, and the stamp carries it, so a
result that differs between two runs can be told apart from one that hit a clock.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import platform
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from meshpipeline.settings.env import ConfigurationError, bool_env, optional_env

logger = logging.getLogger(__name__)

#: Pin threads, ranks and hash order (default). Off = the host decides, as before.
MESH_DETERMINISTIC: bool = bool_env("MESH_DETERMINISTIC", "true")

#: Threads every native tool runs with (OpenMP, BLAS, VTK). The Cloud Run mesh job's size (8 vCPU),
#: so a pinned run is no slower than an unpinned one there; never the host's core count.
MESH_THREADS: int = int(optional_env("MESH_THREADS", "8") or "8")
if MESH_THREADS < 1:
    raise ConfigurationError(f"MESH_THREADS must be at least 1, got {MESH_THREADS}")

#: cfMesh's OpenMP threads. Blank = MESH_THREADS. "1" = strictly serial: the only setting under
#: which cfMesh is bit-for-bit repeatable (its smoothers update points in place, in the order
#: threads reach them), at several times the run time - a measured choice, not a default.
MESH_CFMESH_THREADS: str = optional_env("MESH_CFMESH_THREADS", "")

#: snappyHexMesh's MPI ranks. Blank = the job's CPU quota (at most 12), or MESH_THREADS when the
#: container has no quota - never the host's core count. Memory may still lower it (3 GiB a rank),
#: and the stamp then says so.
MESH_SNAPPY_RANKS: str = optional_env("MESH_SNAPPY_RANKS", "")

#: The decomposition snappy's ranks are cut by (decomposeParDict `method`).
SNAPPY_DECOMPOSITION = "scotch"
#: gmsh: one thread and a fixed seed (engines/gmsh/driver pins them; recorded here).
GMSH_THREADS = 1
GMSH_RANDOM_SEED = 1
#: The hash seed native Python children run with (vmtk's pype, the snap-grid CLI).
PYTHON_HASH_SEED = "0"

#: The stamp beside the mesh: it travels home in the returned workspace.
STAMP_FILE = "reproducibility.json"
STAMP_VERSION = 1
#: Files of the case a mesher reads as its PLAN (dictionaries, specs) rather than its geometry.
_PLAN_SUFFIXES = (".json", ".geo", ".txt", ".dict")
_PLAN_DIRS = ("system",)
#: Never hashed: what a run writes (a later pass in the same workspace must not see an earlier
#: pass's mesh, logs, timing or stamp as a changed input).
_SKIP_PARTS = ("processor", "_rescue", "VTK", "postProcessing", "polyMesh")
_SKIP_FILES = frozenset({STAMP_FILE, "run_reproducibility.json", "mesh_quality.json", "mesh_timing.json",
                         "return_scope.json", "mesh_manifest.json", "quality.json"})
_HASH_CHUNK = 4 * 1024 * 1024


def _int_or_none(raw: str, name: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < 1:
        raise ConfigurationError(f"{name} must be at least 1, got {value}")
    return value


_CFMESH_THREADS = _int_or_none(MESH_CFMESH_THREADS, "MESH_CFMESH_THREADS")
_SNAPPY_RANKS = _int_or_none(MESH_SNAPPY_RANKS, "MESH_SNAPPY_RANKS")


# ------------------------------------------------------------------ what a run is pinned to ---
def threads_for(engine: str) -> int:
    """The OpenMP/BLAS/VTK threads `engine`'s native tools run with."""
    if engine == "cfmesh" and _CFMESH_THREADS:
        return _CFMESH_THREADS
    return MESH_THREADS


def native_environment(engine: str) -> dict[str, str]:
    """The environment a native run of `engine` is pinned to ({} when determinism is off)."""
    if not MESH_DETERMINISTIC:
        return {}
    n = str(threads_for(engine))
    return {
        "OMP_NUM_THREADS": n,
        # no runtime-chosen team sizes: the count asked is the count used
        "OMP_DYNAMIC": "FALSE",
        "OPENBLAS_NUM_THREADS": n,
        "MKL_NUM_THREADS": n,
        "VTK_SMP_MAX_THREADS": n,
        "PYTHONHASHSEED": PYTHON_HASH_SEED,
    }


def pin_process_environment(engine: str) -> dict[str, str]:
    """Pin this process's environment for a native run of `engine` (every native child inherits
    it: OpenFOAM through the scrubbed env, vmtk's pype directly). Returns what was pinned. The
    pinned values are authoritative while determinism is on: an operator's own OMP_NUM_THREADS
    does not survive into a mesh run (MESH_THREADS / MESH_CFMESH_THREADS are the settings)."""
    pinned = native_environment(engine)
    for name, value in pinned.items():
        os.environ[name] = value
    return pinned


def snappy_ranks(cpu_quota: int | None, memory_bytes: int, *, hard_max: int = 12,
                 gib_per_rank: float = 3.0, host_cpus: int | None = None) -> tuple[int, str]:
    """snappy's MPI rank count and, in words, what set it.

    Pinned (MESH_SNAPPY_RANKS) wins; else the job's CPU quota (what the job was configured
    with, the same on every run of that job), capped at `hard_max`; else MESH_THREADS. With
    determinism off, a host without a quota falls back to its core count, as before. Memory can
    only LOWER the count (every rank holds its own copy of the surface), and then says so."""
    if _SNAPPY_RANKS:
        ranks, basis = _SNAPPY_RANKS, f"pinned by MESH_SNAPPY_RANKS={_SNAPPY_RANKS}"
    elif cpu_quota:
        ranks = min(int(cpu_quota), hard_max)
        basis = f"the job's CPU quota ({cpu_quota}), at most {hard_max}"
    elif MESH_DETERMINISTIC or not host_cpus:
        ranks, basis = min(MESH_THREADS, hard_max), f"MESH_THREADS ({MESH_THREADS}): no CPU quota"
    else:
        ranks, basis = min(int(host_cpus), hard_max), f"the host's {host_cpus} cores (determinism off)"
    if memory_bytes:
        by_mem = max(1, int(memory_bytes / (gib_per_rank * (1 << 30))))
        if by_mem < ranks:
            basis += (f"; lowered from {ranks} to {by_mem} by memory "
                      f"({memory_bytes / (1 << 30):.0f} GiB at {gib_per_rank:g} GiB a rank)")
            ranks = by_mem
    return max(1, ranks), basis


# ------------------------------------------------------------- what a run actually did --------
class _RunRecord:
    def __init__(self) -> None:
        self.time_limited: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = {}
        self.lock = threading.Lock()


_CURRENT: contextvars.ContextVar[_RunRecord | None] = contextvars.ContextVar(
    "meshpipeline_determinism_run", default=None)
#: Notes made outside an open run (worker-side planning): drained by `drain_process_notes`.
_PROCESS = _RunRecord()
_PROCESS_MAX = 200


def _record() -> _RunRecord:
    return _CURRENT.get() or _PROCESS


@contextlib.contextmanager
def recording() -> Iterator[_RunRecord]:
    """One run's record: every note_time_limit / note_setting inside the block lands in it."""
    rec = _RunRecord()
    token = _CURRENT.set(rec)
    try:
        yield rec
    finally:
        _CURRENT.reset(token)


def note_time_limit(where: str, decision: str, **facts: Any) -> None:
    """A wall-clock limit changed what this run did (a pass skipped, a measurement abandoned, a
    step cut short). Two runs that differ and both carry such a note are told apart from a
    mesher that varies on its own. Facts are numbers or short words (budget_s, left_s, ...)."""
    rec = _record()
    entry = {"where": str(where), "decision": str(decision),
             **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in facts.items()}}
    with rec.lock:
        if rec is _PROCESS and len(rec.time_limited) >= _PROCESS_MAX:
            rec.time_limited.pop(0)
        rec.time_limited.append(entry)
    logger.info("time limit changed a decision: %s", entry)


def note_setting(name: str, value: Any, basis: str = "") -> None:
    """A setting this run was actually run with (ranks, threads, a seed) and what set it."""
    rec = _record()
    with rec.lock:
        rec.settings[str(name)] = {"value": value, **({"basis": basis} if basis else {})}


def drain_process_notes() -> list[dict[str, Any]]:
    """The time-limit notes made outside any open run since the last drain (worker side)."""
    with _PROCESS.lock:
        out, _PROCESS.time_limited = _PROCESS.time_limited, []
    return out


# ------------------------------------------------------------------------ the stamp -----------
def _sha256_file(path: Path, h: Any) -> None:
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)


def workspace_hashes(workspace) -> dict[str, Any]:
    """Content hashes of the case as handed to the mesher: `payload` over every file (path and
    bytes, in sorted order), `plan` over the files that are its plan (system/ and the specs)."""
    ws = Path(workspace)
    payload, plan = hashlib.sha256(), hashlib.sha256()
    files = 0
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws).as_posix()
        if (not p.is_file() or any(part.startswith(_SKIP_PARTS) for part in Path(rel).parts)
                or p.name in _SKIP_FILES or p.suffix == ".log" or p.name.startswith("log.")):
            continue
        files += 1
        for h in (payload,) + ((plan,) if _is_plan(rel) else ()):
            h.update(rel.encode() + b"\0")
            _sha256_file(p, h)
            h.update(b"\0")
    return {"payload_sha256": payload.hexdigest(), "plan_sha256": plan.hexdigest(), "files": files}


def _is_plan(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] in _PLAN_DIRS:
        return True
    return len(parts) == 1 and Path(rel).suffix in _PLAN_SUFFIXES


def _version(module: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(module)
    except Exception:  # noqa: BLE001 - a version is context, never a requirement
        return None


def tool_versions(engine: str, *, foam_version: str = "") -> dict[str, Any]:
    """The versions that can change a mesh: the package, the native tool, numpy, the CPU."""
    out: dict[str, Any] = {"meshpipeline": _meshpipeline_version(), "python": platform.python_version(),
                           "numpy": _version("numpy"), "machine": platform.machine(),
                           "cpu": _cpu_model()}
    if foam_version:
        out["openfoam"] = foam_version
    if engine == "gmsh":
        out["gmsh"] = _version("gmsh")
    if engine == "vmtk":
        out["vtk"] = _version("vtk")
    return {k: v for k, v in out.items() if v}


def _meshpipeline_version() -> str | None:
    try:
        import meshpipeline
        return str(getattr(meshpipeline, "__version__", "") or "") or None
    except Exception:  # noqa: BLE001
        return None


def job_cpus() -> int:
    """The CPUs THE JOB may use - its cgroup quota (v2 cpu.max, else v1 cfs quota), never the host's
    core count (Cloud Run hands the process a wide mask and os.cpu_count() reports the host); the
    affinity mask, then os.cpu_count(), only when no quota is set."""
    from pathlib import Path as _P

    def read(path: str) -> str:
        try:
            return _P(path).read_text().strip()
        except OSError:
            return ""

    parts = read("/sys/fs/cgroup/cpu.max").split()
    if len(parts) == 2 and parts[0] != "max":
        try:
            return max(1, int(float(parts[0]) / float(parts[1])))
        except (ValueError, ZeroDivisionError):
            pass
    try:
        q, per = int(read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")), int(read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
        if q > 0 and per > 0:
            return max(1, q // per)
    except ValueError:
        pass
    import os
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _cpu_model() -> str | None:
    """The CPU model: numpy and the native tools pick vector code paths by it, so two machines
    can round the same sums differently."""
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def stamp(engine: str, *, record: _RunRecord | None = None, hashes: dict | None = None,
          foam_version: str = "", started: float | None = None,
          ) -> dict[str, Any]:
    """The reproducibility stamp of one native run: what went in (hashes), what it ran with
    (engine, versions, image, threads, ranks, seeds), and every time a clock changed a decision."""
    rec = record or _record()
    env = native_environment(engine)
    out: dict[str, Any] = {
        "version": STAMP_VERSION,
        "engine": engine,
        "deterministic": MESH_DETERMINISTIC,
        "inputs": hashes or {},
        "image": {k: v for k, v in {
            "source_tree": optional_env("MESH_SOURCE_TREE", ""),
            "execution": optional_env("CLOUD_RUN_EXECUTION", ""),
        }.items() if v},
        "tools": tool_versions(engine, foam_version=foam_version),
        "threads": {"engine": threads_for(engine), "gmsh": GMSH_THREADS},
        "python_hash_seed": {"children": env.get("PYTHONHASHSEED"),
                             "this_process_salted": sys.flags.hash_randomization == 1},
        "pinned": env,
        "settings": dict(rec.settings),
        "time_limited": list(rec.time_limited),
    }
    if engine in ("snappy",):
        out["decomposition"] = SNAPPY_DECOMPOSITION
    if engine == "gmsh":
        out["seeds"] = {"Mesh.RandomSeed": GMSH_RANDOM_SEED, "General.NumThreads": GMSH_THREADS}
    if started is not None:
        out["seconds"] = round(time.monotonic() - started, 1)
    return out


def write_stamp(workspace, data: dict) -> None:
    try:
        (Path(workspace) / STAMP_FILE).write_text(json.dumps(data, indent=1, default=str))
    except OSError:
        logger.warning("could not write %s beside the mesh", STAMP_FILE, exc_info=True)


def read_stamp(workspace) -> dict | None:
    try:
        data = json.loads((Path(workspace) / STAMP_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def comparable(a: dict, b: dict) -> list[str]:
    """What differs between two stamps that should make their meshes differ (inputs, tools,
    threads, ranks) - [] when two runs were set up the same and any difference is the mesher's."""
    diffs = []
    for key in ("engine", "deterministic", "inputs", "tools", "threads", "pinned"):
        if (a.get(key) or {}) != (b.get(key) or {}):
            diffs.append(key)
    sa = {k: (v or {}).get("value") for k, v in (a.get("settings") or {}).items()}
    sb = {k: (v or {}).get("value") for k, v in (b.get("settings") or {}).items()}
    if sa != sb:
        diffs.append("settings")
    return diffs


__all__ = [
    "GMSH_RANDOM_SEED", "GMSH_THREADS", "MESH_CFMESH_THREADS", "MESH_DETERMINISTIC",
    "MESH_SNAPPY_RANKS", "MESH_THREADS", "SNAPPY_DECOMPOSITION", "STAMP_FILE", "comparable",
    "drain_process_notes", "native_environment", "note_setting", "note_time_limit",
    "pin_process_environment", "read_stamp", "recording", "snappy_ranks", "stamp", "threads_for",
    "tool_versions", "workspace_hashes", "write_stamp",
]
