# Responsibility: The gmsh side of layer 2 - mesh one STEP file under a named profile and say whether the
#                 result is a usable tet mesh and where it went wrong if not. Runs as its own process.
# Owns: the three profiles (and the algorithm pair each effectively uses), `judge` (the pass rule), the
#       SICN read, `run_in_process` and the `python -m` entry point `main`.
# Boundaries: gmsh is imported inside `run_in_process` only. This is the CHILD of the `mesh_gmsh` kernel
#             function (`mesh_kernel`), which starts it with `subprocess.run`: a gmsh segfault ends the child,
#             never the pool worker, so it is seen as the file's crash. It judges one mesher on one file; the
#             panel that runs several is `panel.py`, and the MemberResult is made in `gmsh_members`.
"""One gmsh meshing run, in a process of its own.

Pass rule: a tet mesh exists, no element is inverted (SICN below zero), and the worst element's
SICN is at least `MIN_SICN`. SICN is gmsh's own `minSICN` from `getElementQualities`.

A gmsh option that does not take is an error, not a quiet default: every option is read back after it
is set. A gmsh error raised while importing or meshing is the file's answer (`fail`). Anything else that
raises here (a MemoryError, a numpy error, our own option read-back) is NOT the file's answer: it
propagates, the child exits non-zero with a traceback, and the parent reports infrastructure.

The three profiles are three different meshers, not one mesher at three sizes. The 2D/3D algorithm pair
each effectively runs (gmsh's own defaults filled in, verified against gmsh 4.15.2: `Mesh.Algorithm` 6
Frontal-Delaunay, `Mesh.Algorithm3D` 1 Delaunay) is `algorithms(name)`; `test_panel` fails if two coincide,
and the answer carries `algorithms` as gmsh read them back after meshing. The members CAN disagree on a real
file (native `test_the_members_can_disagree_on_a_real_file`); the fixtures do not show which of the
differences (OCC scale, element size, 2D or 3D algorithm) drives a disagreement, and no claim is made.

  prod     (2D 6 Frontal-Delaunay, 3D 10 HXT)      the DEFAULT production plan only: no zones, hints,
                                                    Delaunay fallback pass or thread counts (see
                                                    `engines/fea_mesh_config`), the part in metres
  default  (2D 6 Frontal-Delaunay, 3D 1 Delaunay)  gmsh as it comes, nothing set, in millimetres
  alt      (2D 1 MeshAdapt,        3D 4 Frontal)   neither of the others' algorithms, element size a
                                                    twentieth of the diagonal (`ALT_SIZE_DIVISOR`)
"""

from __future__ import annotations

import math
from collections.abc import Sequence

MIN_SICN = 0.01
#: Set in every child before it meshes. These ARE gmsh 4.15.2's defaults (so no verdict changes), pinned so a
#: changed default or a later tuning cannot make the panel run-to-run nondeterministic: HXT (gmsh_prod's 3D fill) is
#: multi-threaded and order-dependent once threads are on. Lane D measured every panel member bit-identical over
#: 5 fresh runs of 11 near-threshold files under these values.
REPRODUCIBLE: dict[str, float] = {"General.NumThreads": 1, "Mesh.MaxNumThreads1D": 1, "Mesh.MaxNumThreads2D": 1,
                                  "Mesh.MaxNumThreads3D": 1, "Mesh.RandomSeed": 1}
ALT_SIZE_DIVISOR = 20.0  # GmshAlt's element size: this many elements across the part's diagonal
_METRES_PER_MM = 1e-3
_TET_TYPES = (4, 11)  # gmsh element types: 4-node and 10-node tetrahedron


def judge(qualities: Sequence[float]) -> tuple[str, str | None]:
    """`("pass", None)` or `("fail", kind)` for the SICN of every tet. NaN counts as inverted.

    `not q >= 0.0` is partly redundant with the floor (a negative SICN is also below `MIN_SICN`); it
    changes only the kind label (`inverted_elements`, not `low_quality`) and catches NaN, which
    compares false to everything."""
    if len(qualities) == 0:
        return "fail", "no_volume"
    if any(not q >= 0.0 for q in qualities):
        return "fail", "inverted_elements"
    if min(qualities) < MIN_SICN:
        return "fail", "low_quality"
    return "pass", None


#: gmsh's own defaults, read from gmsh 4.15.2 (`gmsh.option.getNumber`): what a profile that does not set
#: `Mesh.Algorithm` / `Mesh.Algorithm3D` runs with. The native tier re-reads them from the installed gmsh.
GMSH_DEFAULT_ALGORITHM_2D = 6  # Frontal-Delaunay
GMSH_DEFAULT_ALGORITHM_3D = 1  # Delaunay
#: GmshAlt: MeshAdapt (2D 1) and the Frontal 3D algorithm (4), neither the default nor production's HXT.
ALT_OPTIONS = {"Mesh.Algorithm": 1, "Mesh.Algorithm3D": 4}


#: The diagonals (metres) prod's options are sampled at for the cache key: decades from a watch part to a
#: building, so a clamp or other non-linear size rule changes the key somewhere in the range.
_PROD_SAMPLE_DIAGONALS = (0.001, 0.01, 0.1, 0.5, 1.0, 2.0, 10.0, 100.0)


def algorithms(name: str) -> tuple[int, int]:
    """`(2D, 3D)` gmsh algorithm numbers a profile effectively meshes with, gmsh's defaults filled in."""
    _scale, options = _profile(name, 1.0)
    return (
        int(options.get("Mesh.Algorithm", GMSH_DEFAULT_ALGORITHM_2D)),
        int(options.get("Mesh.Algorithm3D", GMSH_DEFAULT_ALGORITHM_3D)),
    )


def profile_config(name: str) -> dict:
    """Everything that decides a profile's mesh, as JSON-able data: the verdict cache key.

    The prod options depend on the part's diagonal, so they are sampled at `_PROD_SAMPLE_DIAGONALS`. A
    sample pins the rule only at those points: a change that moves `options()` elsewhere only is
    not seen. Of the production settings only what `fea_mesh_config` holds is followed (see its docstring)."""
    base: dict = {"profile": name, "min_sicn": MIN_SICN, "algorithms": list(algorithms(name))}
    if name == "prod":
        from meshpipeline.engines import fea_mesh_config

        return {
            **base,
            "occ_scale": _METRES_PER_MM,
            "options_by_diagonal": {str(d): fea_mesh_config.options(d) for d in _PROD_SAMPLE_DIAGONALS},
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
    alt: `ALT_OPTIONS` (MeshAdapt 2D, Frontal 3D) set explicitly, at `ALT_SIZE_DIVISOR` elements across.
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


def is_gmsh_error(exc: BaseException) -> bool:
    """True for an error gmsh itself raised. Every one of the 382 raises in gmsh 4.15.2's Python API is a
    plain `Exception`; a subclass (MemoryError, ValueError, a numpy error, our RuntimeError) is not gmsh's."""
    return type(exc) is Exception


def _set(gmsh, key: str, value: float) -> None:
    gmsh.option.setNumber(key, value)
    got = gmsh.option.getNumber(key)
    if not math.isclose(got, float(value), rel_tol=1e-9, abs_tol=1e-12):
        raise RuntimeError(f"gmsh option {key} did not take: set {value}, reads {got}")


def _read_algorithms(gmsh) -> list[int]:
    """`[Mesh.Algorithm, Mesh.Algorithm3D]` as gmsh holds them now: what it actually meshed with."""
    return [int(gmsh.option.getNumber("Mesh.Algorithm")), int(gmsh.option.getNumber("Mesh.Algorithm3D"))]


def _tet_qualities(gmsh) -> list[float]:
    import numpy as np

    types, tags, _nodes = gmsh.model.mesh.getElements(3)
    tets = [np.asarray(t, dtype=np.uint64) for kind, t in zip(types, tags, strict=True) if int(kind) in _TET_TYPES]
    if not tets:
        return []
    ids = np.concatenate(tets)
    return [float(q) for q in gmsh.model.mesh.getElementQualities([int(i) for i in ids], "minSICN")]


def _message(kind: str | None, qualities: Sequence[float]) -> str:
    """The words for `judge`'s kind. Built only for the kind that happened: a table of every kind's text
    evaluates `min(qualities)` on a mesh with no tetrahedra, raises, and the pool reports infrastructure
    for what is the file (the mesher produced no volume), which drops it from the scoreboard."""
    if kind is None:
        return ""
    if kind == "no_volume":
        return "gmsh produced no tetrahedra"
    if kind == "inverted_elements":
        return f"{sum(1 for q in qualities if not q >= 0.0)} inverted element(s)"
    return f"minimum SICN {min(qualities):.4g} is below {MIN_SICN}"


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
    algorithms: list[int] | None = None,
) -> dict:
    return {
        "algorithms": algorithms,
        "status": status,
        "error_kind": kind,
        "message": message,
        "located": located or [],
        "min_sicn": min_sicn,
        "elements": elements,
    }


def run_in_process(payload: dict) -> dict:
    """Mesh `payload["step"]` under `payload["profile"]` ("prod", "default", "alt") and report."""
    import gmsh

    from meshpipeline.cad.repair.identity import quantum_for
    from meshpipeline.cad.repair.mesh_probe import STAGE_2D, STAGE_3D, STAGE_IMPORT, classify

    step, profile = str(payload["step"]), str(payload["profile"])
    scale, _unsized = _profile(profile, 1.0)  # validates the profile name before gmsh starts
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        for key, value in REPRODUCIBLE.items():
            _set(gmsh, key, value)
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
        except Exception as exc:  # noqa: BLE001 - narrowed on the next line
            if not is_gmsh_error(exc):
                raise  # a MemoryError, a numpy error, our own option read-back: never a finding about the file
            failure = classify(str(exc), gmsh.logger.get(), stage=stage)
            failed_kind = "import_failed" if stage == STAGE_IMPORT else failure.kind
            located = _located(failure, boxes, scale, quantum_for(_diagonal(boxes) / scale))
            return _answer("fail", failed_kind, failure.message, located, algorithms=_read_algorithms(gmsh))
        qualities = _tet_qualities(gmsh)
        status, kind = judge(qualities)
        finite = [q for q in qualities if math.isfinite(q)]
        message = _message(kind, qualities)
        return _answer(
            status, kind, message, None, min(finite) if finite else None, len(qualities), _read_algorithms(gmsh)
        )
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass


def main(argv: Sequence[str]) -> int:
    """`python -m ...mesh_child <payload.json> <answer.json>`: mesh and write the answer as JSON."""
    import json

    payload_path, answer_path = argv
    with open(payload_path) as handle:
        payload = json.load(handle)
    answer = run_in_process(payload)
    with open(answer_path, "w") as handle:
        json.dump(answer, handle)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
