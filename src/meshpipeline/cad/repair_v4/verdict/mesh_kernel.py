# Responsibility: The gmsh half of layer 2 - mesh one STEP file under a named profile, in a kernel
#                 worker, and say whether the result is a usable tet mesh and where it went wrong if not.
# Owns: the `mesh_gmsh` kernel call, the three profiles, `judge` (the pass rule) and the SICN read.
# Boundaries: runs inside a kernel worker only; gmsh is imported inside the function, never at module
#             level, so the parent process never holds it. It judges one mesher on one file; the panel
#             that runs several is `panel.py`, and turning its answer into a MemberResult is `gmsh_members`.
"""One gmsh meshing run, bounded by the process boundary the WorkerPool puts around it.

Pass rule: a tet mesh exists, no element is inverted (SICN below zero), and the worst element's
SICN is at least `MIN_SICN`. SICN is gmsh's own `minSICN` from `getElementQualities`.

A gmsh option that does not take is an error, not a quiet default: every option is read back after it
is set. A raised gmsh error is the file's answer (`fail`); a segfault or hang never returns here and
the pool reports it as `crashed` or `timeout`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from meshpipeline.cad.repair_v4.kernel_calls import register

MIN_SICN = 0.01
ALT_SIZE_DIVISOR = 20.0  # GmshAlt's element size: this many elements across the part's diagonal
_METRES_PER_MM = 1e-3
_TET_TYPES = (4, 11)  # gmsh element types: 4-node and 10-node tetrahedron


def judge(qualities: Sequence[float]) -> tuple[str, str | None]:
    """`("pass", None)` or `("fail", kind)` for the SICN of every tet. NaN counts as inverted."""
    if len(qualities) == 0:
        return "fail", "no_volume"
    if any(not q >= 0.0 for q in qualities):
        return "fail", "inverted_elements"
    if min(qualities) < MIN_SICN:
        return "fail", "low_quality"
    return "pass", None


ALT_OPTIONS = {"Mesh.Algorithm": 6, "Mesh.Algorithm3D": 1}


def profile_config(name: str) -> dict:
    """Everything that decides a profile's mesh, as JSON-able data: the verdict cache key. The prod
    options depend on the part's diagonal, so they are sampled at two diagonals, which pins the size rule."""
    base: dict = {"profile": name, "min_sicn": MIN_SICN}
    if name == "prod":
        from meshpipeline.engines import fea_mesh_config

        return {
            **base,
            "occ_scale": _METRES_PER_MM,
            "options_at_diag_1": fea_mesh_config.options(1.0),
            "options_at_diag_2": fea_mesh_config.options(2.0),
        }
    if name == "default":
        return {**base, "occ_scale": 1.0, "options": {}}
    if name == "alt":
        return {**base, "occ_scale": 1.0, "options": dict(ALT_OPTIONS), "size_divisor": ALT_SIZE_DIVISOR}
    raise ValueError(f"unknown mesh profile {name!r}")


def _profile(name: str, diag_model: float) -> tuple[float, dict[str, float]]:
    """`(OCC scale, gmsh options)` for a profile, once the model's diagonal (in model units) is known.

    prod: the part in metres, as the FEA adapter reads it, with `fea_mesh_config.options`.
    default: gmsh as it comes, in the file's millimetres, nothing set.
    alt: 2D Frontal-Delaunay and 3D Delaunay set explicitly, at `ALT_SIZE_DIVISOR` elements across.
    """
    if name == "prod":
        from meshpipeline.engines import fea_mesh_config

        return _METRES_PER_MM, dict(fea_mesh_config.options(diag_model))
    if name == "default":
        return 1.0, {}
    if name == "alt":
        return 1.0, {**ALT_OPTIONS, "Mesh.MeshSizeMax": diag_model / ALT_SIZE_DIVISOR}
    raise ValueError(f"unknown mesh profile {name!r}")


def _diagonal(boxes: dict[int, tuple]) -> float:
    """Diagonal of the union of the surfaces' boxes, in model units; 0 when there are none."""
    if not boxes:
        return 0.0
    lows = [min(b[i] for b in boxes.values()) for i in range(3)]
    highs = [max(b[i + 3] for b in boxes.values()) for i in range(3)]
    return math.dist(lows, highs)


def _set(gmsh, key: str, value: float) -> None:
    gmsh.option.setNumber(key, value)
    got = gmsh.option.getNumber(key)
    if not math.isclose(got, float(value), rel_tol=1e-9, abs_tol=1e-12):
        raise RuntimeError(f"gmsh option {key} did not take: set {value}, reads {got}")


def _tet_qualities(gmsh) -> list[float]:
    import numpy as np

    types, tags, _nodes = gmsh.model.mesh.getElements(3)
    tets = [np.asarray(t, dtype=np.uint64) for kind, t in zip(types, tags, strict=True) if int(kind) in _TET_TYPES]
    if not tets:
        return []
    ids = np.concatenate(tets)
    return [float(q) for q in gmsh.model.mesh.getElementQualities([int(i) for i in ids], "minSICN")]


def _located(failure, boxes: dict[int, tuple], scale: float, quantum: float) -> list[dict]:
    from meshpipeline.cad.repair.identity import fingerprint_of
    from meshpipeline.cad.repair.mesh_probe import locating_surfaces

    found: list[dict] = []
    for tag in locating_surfaces(failure):
        box = boxes.get(int(tag))
        if box is None:
            continue
        mm = [v / scale for v in box]
        entity = fingerprint_of("face", {"bbox_min": mm[:3], "bbox_max": mm[3:]}, quantum=quantum)
        found.append({"entity": entity or None, "xyz": None, "mesher_tag": f"surface {int(tag)}"})
    if failure.point is not None:
        found.append({"entity": None, "xyz": [v / scale for v in failure.point], "mesher_tag": "point"})
    return found


def _answer(
    status: str,
    kind: str | None,
    message: str = "",
    located: list[dict] | None = None,
    min_sicn: float | None = None,
    elements: int = 0,
) -> dict:
    return {
        "status": status,
        "error_kind": kind,
        "message": message,
        "located": located or [],
        "min_sicn": min_sicn,
        "elements": elements,
    }


@register("mesh_gmsh")
def mesh_gmsh(payload: dict) -> dict:
    """Mesh `payload["step"]` under `payload["profile"]` ("prod", "default", "alt") and report."""
    import gmsh

    from meshpipeline.cad.repair.identity import quantum_for
    from meshpipeline.cad.repair.mesh_probe import STAGE_2D, STAGE_3D, STAGE_IMPORT, classify

    step, profile = str(payload["step"]), str(payload["profile"])
    scale, _unsized = _profile(profile, 1.0)  # validates the profile name before gmsh starts
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.logger.start()
        stage = STAGE_IMPORT
        boxes: dict[int, tuple] = {}
        try:
            _set(gmsh, "Geometry.OCCScaling", scale)
            try:
                gmsh.model.occ.importShapes(step)
            finally:
                gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
            gmsh.model.occ.synchronize()
            for _dim, tag in gmsh.model.getEntities(2):
                boxes[int(tag)] = tuple(float(v) for v in gmsh.model.getBoundingBox(2, tag))
            _scale, options = _profile(profile, _diagonal(boxes))
            for key, value in options.items():
                _set(gmsh, key, value)
            stage = STAGE_2D
            gmsh.model.mesh.generate(2)
            stage = STAGE_3D
            gmsh.model.mesh.generate(3)
        except Exception as exc:  # noqa: BLE001 - a gmsh error is the file's answer, not a broken worker
            if isinstance(exc, RuntimeError) and "did not take" in str(exc):
                raise  # our own option read-back: infrastructure, never a finding about the file
            failure = classify(str(exc), gmsh.logger.get(), stage=stage)
            failed_kind = "import_failed" if stage == STAGE_IMPORT else failure.kind
            located = _located(failure, boxes, scale, quantum_for(_diagonal(boxes) / scale))
            return _answer("fail", failed_kind, failure.message, located)
        qualities = _tet_qualities(gmsh)
        status, kind = judge(qualities)
        finite = [q for q in qualities if math.isfinite(q)]
        message = {
            None: "",
            "no_volume": "gmsh produced no tetrahedra",
            "inverted_elements": f"{sum(1 for q in qualities if not q >= 0.0)} inverted element(s)",
            "low_quality": f"minimum SICN {min(qualities):.4g} is below {MIN_SICN}",
        }[kind]
        return _answer(status, kind, message, None, min(finite) if finite else None, len(qualities))
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass
