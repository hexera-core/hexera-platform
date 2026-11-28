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

LOCAL SIZING (owner decision 2026-11-26, `thin_sizing`): every profile also never meshes below a floor and resolves
the part's thin material walls with the product's wall law (`local_options`, and the walls' fields from the payload's
`sizing`). Curvature sizing stays off, as in the product (thin_sizing.CURVATURE_POINTS says why). Each profile keeps
its algorithms and its global size.
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Iterator, Sequence

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
    from meshpipeline.cad.repair_v4.verdict import thin_sizing

    base: dict = {"profile": name, "min_sicn": MIN_SICN, "algorithms": list(algorithms(name)),
                  "local_sizing": thin_sizing.config()}
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

        return _METRES_PER_MM, {**fea_mesh_config.options(diag_model), **local_options(diag_model)}
    if name == "default":
        return 1.0, local_options(diag_model)
    if name == "alt":
        return 1.0, {**ALT_OPTIONS, "Mesh.MeshSizeMax": diag_model / ALT_SIZE_DIVISOR, **local_options(diag_model)}
    raise ValueError(f"unknown mesh profile {name!r}")


def local_options(diag_model: float) -> dict[str, float]:
    """Owner decision 2026-11-26 (thin_sizing): curvature sizing on every gmsh member, `CURVATURE_POINTS` per 2*pi
    (0 = off, see there). Its algorithm and global size stay. The floor and the curve integration precision are set
    only where walls are sized (`wall_options`)."""
    from meshpipeline.cad.repair_v4.verdict import thin_sizing

    return {"Mesh.MeshSizeFromCurvature": float(thin_sizing.CURVATURE_POINTS)}


def wall_options(diag_model: float) -> dict[str, float]:
    """Set only when a wall's size fields are applied: the floor (`FLOOR_OF_DIAG` x diagonal, model units) and the
    production curve integration precision (`fea_mesh_config.LC_INTEGRATION_PRECISION`: at gmsh's own 1e-9 a wall's
    size fields are evaluated minutes long on the curves). Set on EVERY file, the precision alone turned clean-control
    00522449 gmsh_alt from pass to low_quality with no wall sized: a file with no wall meshes exactly as before."""
    from meshpipeline.cad.repair_v4.verdict import thin_sizing
    from meshpipeline.engines.fea_mesh_config import LC_INTEGRATION_PRECISION

    return {"Mesh.MeshSizeMin": thin_sizing.FLOOR_OF_DIAG * diag_model,
            "Mesh.LcIntegrationPrecision": LC_INTEGRATION_PRECISION}


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
    return _tets_and_qualities(gmsh)[1]


def _tets_and_qualities(gmsh) -> tuple[list[int], list[float]]:
    import numpy as np

    types, tags, _nodes = gmsh.model.mesh.getElements(3)
    tets = [np.asarray(t, dtype=np.uint64) for kind, t in zip(types, tags, strict=True) if int(kind) in _TET_TYPES]
    if not tets:
        return [], []
    ids = [int(i) for i in np.concatenate(tets)]
    return ids, [float(q) for q in gmsh.model.mesh.getElementQualities(ids, "minSICN")]


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


#: What gmsh's 3D back-ends print, to the process's stdout and not to `gmsh.logger`, when they fail: TetGen's
#: "Found two duplicated facets. 1st: [203, 271, 282] #19" and HXT's "el1 = np = 3 204 324 326". Mesh NODE ids,
#: which the live 2D mesh turns into coordinates and the surfaces they lie on. Lane C5: the exception text gmsh
#: raises ("PLC Error: A segment and a facet intersect at point", "HXT 3D mesh failed") carries neither.
_FACET_NODES = re.compile(r"\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]\s*#\s*\d+")
_TRIANGLE_NODES = re.compile(r"\bel[12]\s*=\s*np\s*=\s*3\s+(\d+)\s+(\d+)\s+(\d+)")
MAX_OUTPUT_TRIANGLES = 6
#: a low-quality mesh is located at this many of its worst tets
WORST_TETS = 3
_STDOUT_KEEP = 200_000


def triangles_in(text: str) -> list[tuple[int, int, int]]:
    """The node triples gmsh's 3D back-ends printed, each once, in order, at most MAX_OUTPUT_TRIANGLES."""
    found: list[tuple[int, int, int]] = []
    for line in text.splitlines():
        for match in (*_FACET_NODES.finditer(line), *_TRIANGLE_NODES.finditer(line)):
            tri = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if tri not in found:
                found.append(tri)
                if len(found) >= MAX_OUTPUT_TRIANGLES:
                    return found
    return found


def worst(ids: Sequence[int], qualities: Sequence[float], n: int) -> list[int]:
    """The `n` element ids of lowest quality (NaN counts as lowest), lowest first."""
    order = sorted(range(len(ids)), key=lambda i: qualities[i] if qualities[i] == qualities[i] else -math.inf)
    return [int(ids[i]) for i in order[:n]]


#: gmsh's `getBoundingBox` pads every box by 1e-7 MODEL units on each side (measured, lane D17: a planar face at
#: z = 0 reads z in [-1e-7, 1e-7] whether gmsh imported in millimetres or in metres). The kernel's own face box
#: (`BRepBndLib.Add`, which `ws_kernel.fingerprint` reads) carries the face's tolerance, 1e-7 MILLIMETRES for a
#: default face, so a millimetre import matches it as it stands. gmsh_prod imports in METRES (`Geometry.OCCScaling`
#: 1e-3, as the FEA adapter reads the part), so there the same 1e-7 is 1e-4 mm: against the fingerprint quantum of
#: 1.28e-4 mm at a 104 mm diagonal (`identity.quantum_for`) that is about 0.8 of a cell, and the rounded box lands
#: in the next cell. Measured on dev db90265b9b: gmsh_prod's surfaces matched 1 of 15 workspace faces as they were,
#: 15 of 15 with the padding taken off. So gmsh_prod - the member that fails most - had its failures placed on no
#: face, and every aimed op (D16's located defeature) missed it.
GMSH_BOX_PAD = 1e-7
#: the padding the kernel's box carries for a default-tolerance face, in mm
KERNEL_BOX_PAD_MM = 1e-7


def box_mm(box: Sequence[float], scale: float) -> list[float]:
    """A gmsh surface box (model units, padded) as the kernel would box the same face, in mm: gmsh's padding is taken
    off in MODEL units before the scale divide and the kernel's put back in mm. At scale 1 (the millimetre members)
    the two cancel, and the box is returned AS IT IS (not round-tripped through the arithmetic, which could move a
    coordinate by one ulp across a half-cell boundary), so only gmsh_prod's boxes change. A box thinner than the
    padding (a planar face) keeps its centre rather than inverting."""
    if scale == 1.0:
        return [float(v) for v in box]
    out = []
    for low, high in zip(box[:3], box[3:], strict=True):
        lo, hi = (low + GMSH_BOX_PAD) / scale - KERNEL_BOX_PAD_MM, (high - GMSH_BOX_PAD) / scale + KERNEL_BOX_PAD_MM
        if lo > hi:
            lo = hi = (low + high) / 2 / scale
        out.append((lo, hi))
    return [lo for lo, _ in out] + [hi for _, hi in out]


#: D28, measured on the 42 files gmsh_prod fails 'other' on: the 3D back-ends NAME where the surface mesh breaks, but
#: only in their printed output and log, never in the exception ('HXT 3D mesh failed'). TetGen prints 'PLC Error: A
#: segment and a facet intersect at point (x,y,z)' (18 occurrences on 22 files of the crossing-triangle kind) and gmsh
#: logs 'N elements remain invalid in surface S' (14 files). Neither reached `located`, so the failure could not be
#: aimed at (lane A11: edge_through_face 0 of 4 locatable).
_TEXT_POINT = re.compile(r"intersect at point \(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)")
_TEXT_SURFACE = re.compile(r"remain invalid in surface (\d+)")
MAX_TEXT_LOCATIONS = 6


def located_from_text(text: str, boxes: dict[int, tuple], scale: float, quantum: float) -> list[dict]:
    """Locations gmsh's own words name: intersection POINTS (model units -> mm) and SURFACES whose 2D mesh is
    invalid (their box, de-padded, and fingerprint, as `_located` names a surface). Each once, in order, capped."""
    from meshpipeline.cad.repair.identity import fingerprint_of

    found: list[dict] = []
    for m in _TEXT_POINT.finditer(text or ""):
        xyz = [float(m.group(i)) / scale for i in (1, 2, 3)]
        item = {"entity": None, "xyz": xyz, "mesher_tag": "printed point"}
        if item not in found:
            found.append(item)
        if len(found) >= MAX_TEXT_LOCATIONS:
            break
    for m in _TEXT_SURFACE.finditer(text or ""):
        tag = int(m.group(1))
        box = boxes.get(tag)
        if box is None:
            continue
        mm = box_mm(box, scale)
        entity = fingerprint_of("face", {"bbox_min": mm[:3], "bbox_max": mm[3:]}, quantum=quantum)
        item = {"entity": entity or None, "xyz": None, "mesher_tag": f"surface {tag}", "box": mm}
        if entity and all(x.get("mesher_tag") != item["mesher_tag"] for x in found):
            found.append(item)
        if len(found) >= 2 * MAX_TEXT_LOCATIONS:
            break
    return found


def located_from_nodes(gmsh, groups: Sequence[Sequence[int]], boxes: dict[int, tuple], scale: float,
                       quantum: float, *, boundary_only: bool = False) -> list[dict]:
    """Locations from groups of mesh nodes (a failing triangle, a bad tet): the surfaces the nodes lie on, named by
    their box fingerprint as `_located` names them, and one point per group (the centroid, in mm). With
    `boundary_only` the point is the centroid of the nodes on a surface, curve or vertex (a tet's interior centroid is
    on no face). A node gmsh does not know is skipped; a surface with no box gives no fingerprint."""
    from meshpipeline.cad.repair.identity import fingerprint_of

    surfaces: list[int] = []
    points: list[dict] = []
    for group in groups:
        coords: list[list[float]] = []
        for node in group:
            try:
                coord, _param, dim, tag = gmsh.model.mesh.getNode(int(node))
            except Exception:  # noqa: BLE001, S112 - gmsh raises a plain Exception for an unknown node: skip it
                continue
            if int(dim) == 2 and int(tag) not in surfaces:
                surfaces.append(int(tag))
            if not boundary_only or int(dim) < 3:
                coords.append([float(v) for v in coord])
        if coords:
            centre = [sum(c[a] for c in coords) / len(coords) / scale for a in range(3)]
            points.append({"entity": None, "xyz": centre, "mesher_tag": "nodes " + ",".join(str(n) for n in group)})
    named: list[dict] = []
    for tag in surfaces[:MAX_OUTPUT_TRIANGLES]:
        box = boxes.get(tag)
        if box is None:
            continue
        mm = box_mm(box, scale)
        entity = fingerprint_of("face", {"bbox_min": mm[:3], "bbox_max": mm[3:]}, quantum=quantum)
        if entity:
            named.append({"entity": entity, "xyz": None, "mesher_tag": f"surface {tag}", "box": mm})
    return named + points[:MAX_OUTPUT_TRIANGLES]


@contextlib.contextmanager
def _captured_stdout() -> Iterator[dict]:
    """The C-level stdout AND stderr of the block (TetGen and HXT print with printf, TetGen's "duplicated facets" to
    stderr), as `holder["text"]`. File-descriptor level, because nothing gmsh's back-ends write passes through Python's
    `sys.stdout`. What was captured is also written back to the real descriptors afterwards, so nothing the caller
    used to see (a crash's stderr tail) is lost."""
    import ctypes
    import os
    import sys
    import tempfile

    holder: dict = {"text": ""}
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        libc = None
    sys.stdout.flush()
    sys.stderr.flush()
    saved = {fd: os.dup(fd) for fd in (1, 2)}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        os.dup2(out.fileno(), 1)
        os.dup2(err.fileno(), 2)
        try:
            yield holder
        finally:
            if libc is not None:
                libc.fflush(None)
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, keep in saved.items():
                os.dup2(keep, fd)
                os.close(keep)
            texts = []
            for fd, sink in ((1, out), (2, err)):
                sink.seek(0)
                data = sink.read()
                os.write(fd, data[-_STDOUT_KEEP:])
                texts.append(data[-_STDOUT_KEEP:].decode("utf-8", errors="replace"))
            holder["text"] = "\n".join(texts)


def _located(failure, boxes: dict[int, tuple], scale: float, quantum: float) -> list[dict]:
    from meshpipeline.cad.repair.identity import fingerprint_of
    from meshpipeline.cad.repair.mesh_probe import locating_surfaces

    found: list[dict] = []
    for tag in locating_surfaces(failure):
        box = boxes.get(int(tag))
        if box is None:
            continue
        mm = box_mm(box, scale)
        entity = fingerprint_of("face", {"bbox_min": mm[:3], "bbox_max": mm[3:]}, quantum=quantum)
        found.append({"entity": entity or None, "xyz": None, "mesher_tag": f"surface {int(tag)}", "box": mm})
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
        applied: dict | None = None
        try:
            _set(gmsh, "Geometry.OCCScaling", scale)
            try:
                gmsh.model.occ.importShapes(step)
            finally:
                gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
            gmsh.model.occ.synchronize()
            for _dim, tag in gmsh.model.getEntities(2):
                boxes[int(tag)] = tuple(float(v) for v in gmsh.model.getBoundingBox(2, tag))
            diag_model = _diagonal(boxes)
            _scale, options = _profile(profile, diag_model)
            for key, value in options.items():
                _set(gmsh, key, value)
            if payload.get("sizing"):  # thin walls: the product's wall law, below the member's own global size
                from meshpipeline.cad.repair_v4.verdict import thin_sizing

                applied = thin_sizing.gmsh_fields(gmsh, payload["sizing"], scale,
                                                  float(options.get("Mesh.MeshSizeMax", diag_model)), boxes)
                if applied["fields"]:
                    for key, value in wall_options(diag_model).items():
                        _set(gmsh, key, value)
            stage = STAGE_2D
            with _captured_stdout() as printed:
                gmsh.model.mesh.generate(2)
                stage = STAGE_3D
                gmsh.model.mesh.generate(3)
        except Exception as exc:  # noqa: BLE001 - narrowed on the next line
            if not is_gmsh_error(exc):
                raise  # a MemoryError, a numpy error, our own option read-back: never a finding about the file
            failure = classify(str(exc), gmsh.logger.get(), stage=stage)
            failed_kind = "import_failed" if stage == STAGE_IMPORT else failure.kind
            quantum = quantum_for(_diagonal(boxes) / scale)
            where = _located(failure, boxes, scale, quantum)
            if stage != STAGE_IMPORT:
                try:  # best effort, as for a low-quality mesh below
                    where += [x for x in located_from_nodes(gmsh, triangles_in(printed["text"]), boxes, scale,
                                                            quantum) if x not in where]
                except Exception:  # noqa: BLE001
                    pass
                try:  # D28: the points and surfaces gmsh NAMES in its printed output and its log
                    text = "\n".join([printed.get("text", ""), *gmsh.logger.get()])
                    where += [x for x in located_from_text(text, boxes, scale, quantum) if x not in where]
                except Exception:  # noqa: BLE001
                    pass
            return {**_answer("fail", failed_kind, failure.message, where, algorithms=_read_algorithms(gmsh)),
                    "sizing": applied}
        ids, qualities = _tets_and_qualities(gmsh)
        status, kind = judge(qualities)
        finite = [q for q in qualities if math.isfinite(q)]
        message = _message(kind, qualities)
        located: list[dict] | None = None
        if status == "fail" and ids:  # a mesh exists, so its worst elements say where it went wrong
            try:  # WHERE is best effort: a failure to locate never changes or breaks the verdict
                groups = [list(gmsh.model.mesh.getElement(tag)[1]) for tag in worst(ids, qualities, WORST_TETS)]
                located = located_from_nodes(gmsh, groups, boxes, scale, quantum_for(_diagonal(boxes) / scale),
                                             boundary_only=True)
            except Exception:  # noqa: BLE001
                located = None
        return {**_answer(status, kind, message, located, min(finite) if finite else None, len(qualities),
                          _read_algorithms(gmsh)), "sizing": applied}
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
