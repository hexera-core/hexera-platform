# Responsibility: Cast the local-radius field's inward chords (engines/radius_field) on every core the run may use, with the same rays and so the same readings as one core.
# Boundaries: geometry only - it reads the rays and returns the hits' readings; the chord rules are radius_field's (_first_chord), and it judges nothing.
"""THE CHORDS ON EVERY CORE.

radius_field.local_radius casts one ray per wall point into a vtkOBBTree, 1-8 ms a ray, and VTK
holds the GIL through every IntersectWithLine (four threads took 71 s where one took 66 s). The
passage measure beside a finished snappy mesh casts up to 60,000 of them: 105 s of a 115 s measure
on a 0.9 M-cell elbow, on one core of an 8-CPU mesh job whose mesher had just finished with all 8.

So above PARALLEL_FROM points the rays are split across child PROCESSES - a fresh interpreter each
(`python -m meshpipeline.engines.radius_chords`), never a fork of this one: the mesh job runs
threads beside the measure (the volume export, the progress beacon), and a fork copies whatever
lock those threads hold. Each child builds its own tree from the same points and triangles - the
same tree - and casts every K-th point, so every point is read by exactly the ray one core would
cast and the field is the same number for number. A child that fails leaves its points to be cast
here, one core, as before: the measure is never lost to its own speed-up.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: Below this many points the rays are cast here: a child's start (an interpreter, numpy and VTK
#: imported, the tree built) costs about two seconds, and 4,000 rays take about seven on one core.
PARALLEL_FROM = 4000


def workers_for(n_points: int) -> int:
    """How many processes cast `n_points` rays: the cores this process may run on, no more than
    the native tools' pinned thread count (MESH_THREADS, the mesh job's size - a Cloud Run job
    sees the host's cores and is throttled to its quota), and 1 below PARALLEL_FROM."""
    if n_points < PARALLEL_FROM:
        return 1
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    try:
        from meshpipeline.engines.determinism import MESH_THREADS
        cores = min(cores, int(MESH_THREADS))
    except Exception:  # noqa: BLE001 - a sizing hint; the affinity alone still bounds it
        pass
    return max(1, min(cores, n_points // (PARALLEL_FROM // 4)))


def cast(points, faces, normals, face_normals, ring, fold, eps: float, reach: float,
         indices) -> tuple[np.ndarray, np.ndarray]:
    """(first hit, reading) as half chords at `indices`, NaN where none: the ray from each point
    along its inward normal (-normals[i]) into a vtkOBBTree of the triangles, read by
    radius_field._first_chord."""
    import pyvista as pv
    import vtk

    from meshpipeline.engines.radius_field import _first_chord
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    mesh = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(mesh)
    obb.BuildLocator()
    hits = vtk.vtkPoints()
    ids = vtk.vtkIdList()
    idx = np.asarray(indices, dtype=np.int64)
    first = np.full(len(idx), np.nan)
    r = np.full(len(idx), np.nan)
    for k, i in enumerate(idx.tolist()):
        hits.Reset()
        ids.Reset()
        if obb.IntersectWithLine(pts[i] - normals[i] * eps, pts[i] - normals[i] * reach, hits, ids) \
                and hits.GetNumberOfPoints():
            first[k], r[k] = _first_chord(pts[i], -normals[i], hits, ids, f, face_normals,
                                          ring[i], fold[i])
    return first, r


def _ring_arrays(ring) -> tuple[np.ndarray, np.ndarray]:
    sizes = np.fromiter((len(s) for s in ring), dtype=np.int64, count=len(ring))
    offsets = np.concatenate([[0], np.cumsum(sizes)])
    flat = np.fromiter((v for s in ring for v in sorted(s)), dtype=np.int64, count=int(offsets[-1]))
    return offsets, flat


def cast_all(points, faces, normals, face_normals, ring, fold, eps: float,
             reach: float) -> tuple[np.ndarray, np.ndarray]:
    """`cast` at every point, on workers_for(points) processes (see the module text)."""
    n = len(points)
    k = workers_for(n)
    if k <= 1:
        return cast(points, faces, normals, face_normals, ring, fold, eps, reach, np.arange(n))
    first = np.full(n, np.nan)
    r = np.full(n, np.nan)
    done = np.zeros(n, dtype=bool)
    with tempfile.TemporaryDirectory(prefix="chords_") as tmp:
        offsets, flat = _ring_arrays(ring)
        job = Path(tmp) / "job.npz"
        np.savez(job, points=np.asarray(points, dtype=float), faces=np.asarray(faces, dtype=np.int64),
                 normals=np.asarray(normals, dtype=float),
                 face_normals=np.asarray(face_normals, dtype=float), ring_offsets=offsets,
                 ring_flat=flat, fold=np.asarray(fold, dtype=float), eps=float(eps),
                 reach=float(reach))
        env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1", "VTK_SMP_MAX_THREADS": "1"}
        procs = []
        try:
            for part in range(k):
                out = Path(tmp) / f"part{part}.npz"
                procs.append((part, out, subprocess.Popen(
                    [sys.executable, "-m", "meshpipeline.engines.radius_chords", str(job),
                     str(part), str(k), str(out)],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)))
            for part, out, proc in procs:
                _, err = proc.communicate()
                if proc.returncode == 0 and out.is_file():
                    got = np.load(out)
                    idx = np.arange(part, n, k)
                    first[idx], r[idx] = got["first"], got["r"]
                    done[idx] = True
                else:
                    logger.warning("chord worker %d of %d failed (rc %s): %s - its points are "
                                   "cast here", part, k, proc.returncode,
                                   (err or b"").decode(errors="replace")[-400:])
        finally:
            for _, _, proc in procs:            # a guard that fired while we waited: no orphans
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    if not done.all():
        left = np.flatnonzero(~done)
        first[left], r[left] = cast(points, faces, normals, face_normals, ring, fold, eps, reach,
                                    left)
    return first, r


def _main(argv: list[str]) -> int:
    job, part, of, out = argv[0], int(argv[1]), int(argv[2]), argv[3]
    d = np.load(job)
    offsets, flat = d["ring_offsets"], d["ring_flat"]
    n = len(d["points"])
    idx = np.arange(part, n, of)
    ring: dict[int, frozenset] = {int(i): frozenset(flat[offsets[i]:offsets[i + 1]].tolist())
                                  for i in idx.tolist()}
    first, r = cast(d["points"], d["faces"], d["normals"], d["face_normals"], ring, d["fold"],
                    float(d["eps"]), float(d["reach"]), idx)
    np.savez(out, first=first, r=r)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
