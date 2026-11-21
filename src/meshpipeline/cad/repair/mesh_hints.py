# Responsibility: Per-face mesh-size HINTS for the spots a delivered STEP still will not mesh at
#                 default settings: produce them (a sidecar JSON next to the STEP), apply them in
#                 gmsh, and report "meshes at default" and "meshes with the hints" separately.
# Owns: the sidecar schema (`crs-mesh-hints/1`), the face table kernel, the refinement loop, the
#       hint-to-surface matching and the two-number probe.
# Boundaries: hints are an ADDITION to the returned STEP, never instead of it: nothing here edits
#             or replaces the STEP. All OCC work runs in `bounded_child` (`_face_table` is on its
#             allow-list); all gmsh work runs in `mesh_probe`'s fresh interpreter. Neither OCP nor
#             gmsh is imported at module level.
# Collaborates with: cad/repair/mesh_probe.py (`probe_file(hints=...)` calls `apply_in_gmsh`,
#                    `surface_props` and `volume_quality` in its child), cad/repair/localize.py
#                    (`_bounded`, the one door to a bounded kernel).
"""Mesh-size hints keyed to the faces of the DELIVERED STEP.

THE FACE ID. A hint names a face by its 1-based index in `TopExp.MapShapes(shape, FACE)` of the
shape `STEPControl_Reader` reads back from the delivered file - the same map `localize` indexes.
The writer's ADVANCED_FACE entity ids are not used: they are a numbering of one write (a re-export
renumbers them) and gmsh never sees them. The index is a property of the file's bytes, so it holds
whether the delivered STEP is a re-exported repair or the customer's untouched upload.

gmsh's own surface tag is NOT trusted to equal that index. Measured on the wave-9 inputs: on
0054_00544974 gmsh imports 172 surfaces where OCC reads 176 faces, so every tag after the gap is
shifted. So each hint also carries a fingerprint - area and centre of mass, which gmsh and OCC
compute with the same kernel and agree on - and the consumer applies a hint to the surface whose
fingerprint matches: the surface with the hint's own number when it matches, else the ONE surface
that does. A hint matching no surface (or several) is reported unmapped, never guessed. (The bounding
box is in the fingerprint for a reader; it is not matched on, because gmsh's box of a curved face
can differ from OCC's by millimetres.)

THE SIZES. Measured on the 9 wave-9 files that fail at default: the volume stage fails where two
faces' surface triangles cross because an element is wider than the gap between them. Making the
elements on those faces SMALLER is what meshes them, not coarsening over tiny faces. A face is
seeded at `SEED_FRACTION` of its shortest edge and halved each time gmsh names it again, never below
`FLOOR_FRACTION` of the part diagonal - without that floor, the sub-micron edges of a tiny-face
part drive the size to zero and the 2D stage fails instead.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshpipeline.cad.repair.mesh_probe import ProbeResult

logger = logging.getLogger(__name__)

SCHEMA = "crs-mesh-hints/1"
#: `part.step` -> `part.step.mesh-hints.json`, in the same directory.
SIDECAR_SUFFIX = ".mesh-hints.json"
#: How a hint's `face` is defined, written into every sidecar so a reader need not guess.
FACE_ID = "occ-face-index-after-step-reread"

#: The probe's default element size is the diagonal over this (`mesh_probe._SIZE_DIVISOR`).
DEFAULT_DIVISOR = 40.0
#: A newly named face starts at this fraction of its shortest edge.
SEED_FRACTION = 0.5
#: A face gmsh names again is refined by this factor.
REFINE_FACTOR = 0.5
#: No size below this fraction of the diagonal, and edges shorter than it are not "the shortest".
#: Measured: 1e-3 meshes 0060_00600919 (586 tiny faces); 1e-2 never does; with no floor a 2D
#: attempt took 380 s and failed.
FLOOR_FRACTION = 1e-3
#: No hinted face is sized finer than area / size^2 = this (about twice as many triangles).
#: MEASURED on the 111 wave-9 failures: every verified sidecar stays under 22,105. Above it, an
#: attempt is mostly time: 0054_00540003 (a 24.8 m part) put a 58,000-cell field on one BSpline
#: face, its next attempt ran 111 s and the one after hit the attempt wall.
MAX_FACE_CELLS = 25_000
#: The refinement loop's bounds: attempts, the whole producer's wall, and one attempt's wall.
#: MEASURED on the 111 (6 files at a time on one laptop): the slowest verified success took 7
#: attempts; time-to-verified was <=60 s for 32/41, <=180 s for 39/41, <=221 s for 41/41. 180 s
#: keeps 95% of them and is what every passthrough or repaired delivery may spend on hints.
MAX_ATTEMPTS = 8
HINTS_WALL_S = 180.0
ATTEMPT_S = 120.0
FACE_TABLE_S = 120.0
#: An attempt is not started with less than this left: it could not finish.
_MIN_ATTEMPT_S = 5.0

#: Fingerprint tolerances: relative on area, a fraction of the diagonal on the centre of mass.
AREA_REL_TOL = 1e-5
CENTROID_FRACTION = 1e-5
#: A defect's located box matches a delivered face's box within this fraction of the diagonal.
BOX_FRACTION = 1e-5

#: Sidecar `status`.
STATUS_VERIFIED = "verified"          # the last attempt meshed (3D volume) with these hints
STATUS_NOT_NEEDED = "not_needed"      # it meshed at default with no hint at all
STATUS_UNVERIFIED = "unverified"      # hints written, but no attempt meshed within the bounds
STATUS_NO_FACE_TABLE = "no_face_table"  # the delivered STEP could not be read in its budget

#: `why` of a hint.
WHY_SHORT_EDGE = "short_edge"
WHY_SUB_MESH_GAP = "sub_mesh_gap"
WHY_LOCATED_FACE = "located_face"     # gmsh named it, and it has no edge above the floor


def sidecar_path(step_path: Path | str) -> Path:
    step = Path(step_path)
    return step.with_name(step.name + SIDECAR_SUFFIX)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- matching (pure) ---------------------------------------------------------------------------

def same_face(a: Sequence[float], b: Sequence[float], *, diagonal_mm: float) -> bool:
    """Do two (area_mm2, cx, cy, cz) fingerprints name the same face? Pure."""
    if len(a) != 4 or len(b) != 4:
        return False
    floor = (diagonal_mm * FLOOR_FRACTION) ** 2 * 1e-3
    if abs(a[0] - b[0]) > max(AREA_REL_TOL * max(abs(a[0]), abs(b[0])), floor):
        return False
    tol = CENTROID_FRACTION * diagonal_mm
    return all(abs(a[k] - b[k]) <= tol for k in (1, 2, 3))


def match(number: int, fingerprint: Sequence[float],
          props: Mapping[int, Sequence[float]], *, diagonal_mm: float) -> int | None:
    """The key of `props` whose fingerprint matches: `number` itself when it does, else the ONE
    key that does, else None. Pure."""
    own = props.get(number)
    if own is not None and same_face(own, fingerprint, diagonal_mm=diagonal_mm):
        return number
    hits = [k for k, v in props.items() if same_face(v, fingerprint, diagonal_mm=diagonal_mm)]
    return hits[0] if len(hits) == 1 else None


def _key(fingerprint: dict) -> tuple[float, float, float, float]:
    c = fingerprint["centroid"]
    return float(fingerprint["area_mm2"]), float(c[0]), float(c[1]), float(c[2])


# --- the gmsh side: runs inside mesh_probe's child ---------------------------------------------

def _gmsh_props(gmsh, tag: int) -> tuple[float, float, float, float]:
    area = float(gmsh.model.occ.getMass(2, tag))
    cx, cy, cz = gmsh.model.occ.getCenterOfMass(2, tag)
    return area, float(cx), float(cy), float(cz)


def surface_props(gmsh, tags: Iterable[int]) -> dict[str, list[float]]:
    """`{tag: [area, cx, cy, cz]}` for the given surfaces. Never raises."""
    out: dict[str, list[float]] = {}
    for tag in sorted({int(t) for t in tags}):
        try:
            out[str(tag)] = list(_gmsh_props(gmsh, tag))
        except Exception as exc:  # noqa: BLE001 - an unmeasurable surface is simply not named
            logger.debug("mesh_hints: surface %d unmeasurable: %s", tag, exc)
    return out


def apply_in_gmsh(gmsh, hints: Sequence[dict], *, diagonal_mm: float) -> tuple[int, list[int]]:
    """Apply sidecar face hints as gmsh size fields: a Constant field per matched surface, all
    under one Min background field. Returns (applied, unmapped delivered-face indices)."""
    tags = [int(t) for _d, t in gmsh.model.getEntities(2)]
    tagset = set(tags)
    props: dict[int, Sequence[float]] = {}
    every: list[bool] = [False]

    def lookup(face: int, key: tuple[float, float, float, float]) -> int | None:
        if face in tagset and face not in props:
            props[face] = _gmsh_props(gmsh, face)
        if face in props and same_face(props[face], key, diagonal_mm=diagonal_mm):
            return face
        if not every[0]:
            for t in tags:
                if t not in props:
                    props[t] = _gmsh_props(gmsh, t)
            every[0] = True
        return match(face, key, props, diagonal_mm=diagonal_mm)

    fields: list[int] = []
    unmapped: list[int] = []
    for hint in hints:
        face = int(hint["face"])
        try:
            tag = lookup(face, _key(hint["fingerprint"]))
        except (KeyError, TypeError, ValueError, IndexError):
            tag = None
        if tag is None:
            unmapped.append(face)
            continue
        field = gmsh.model.mesh.field.add("Constant")
        gmsh.model.mesh.field.setNumbers(field, "SurfacesList", [tag])
        gmsh.model.mesh.field.setNumber(field, "VIn", float(hint["size_mm"]))
        gmsh.model.mesh.field.setNumber(field, "IncludeBoundary", 1)
        fields.append(field)
    if fields:
        smallest = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(smallest, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(smallest)
    return len(fields), unmapped


def volume_quality(gmsh) -> dict[str, float]:
    """The volume mesh's element count, min/mean gamma, min SICN and shortest edge. Never raises."""
    try:
        _types, element_tags, _nodes = gmsh.model.mesh.getElements(3)
        flat = [int(t) for block in element_tags for t in block]
        if not flat:
            return {}
        gamma = gmsh.model.mesh.getElementQualities(flat, "gamma")
        sicn = gmsh.model.mesh.getElementQualities(flat, "minSICN")
        edge = gmsh.model.mesh.getElementQualities(flat, "minEdge")
        return {"elements": float(len(flat)), "min_gamma": float(min(gamma)),
                "mean_gamma": float(sum(gamma) / len(gamma)), "min_sicn": float(min(sicn)),
                "min_edge_mm": float(min(edge))}
    except Exception as exc:  # noqa: BLE001 - quality is a report, never a verdict
        logger.info("mesh_hints: quality: %s: %s", type(exc).__name__, exc)
        return {}


# --- the OCC side: a bounded_child kernel ------------------------------------------------------

def _read_shape(path: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopoDS import TopoDS_Shape

    if Path(path).suffix.lower() in (".brep", ".brp"):
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, path, BRep_Builder()):
            raise OSError(f"could not read {path}")
        return shape
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise OSError(f"could not read {path}")
    reader.TransferRoots()
    return reader.OneShape()


def _face_table(path: str) -> dict:
    """KERNEL (`bounded_child.KERNELS["mesh_hints.face_table"]`): every face of the shape read
    from `path`, by its 1-based index, with its fingerprint and shortest edge above the floor."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _bounds_of, _indexed

    shape = _read_shape(path)
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diagonal = math.dist((x0, y0, z0), (x1, y1, z1))
    floor = diagonal * FLOOR_FRACTION
    faces = _indexed(shape, TopAbs_FACE)
    all_edges = _indexed(shape, TopAbs_EDGE)
    lengths: dict[int, float] = {}
    rows = []
    for index in range(1, faces.Extent() + 1):
        face = TopoDS.Face_s(faces.FindKey(index))
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        centre = props.CentreOfMass()
        bounds = _bounds_of(face)
        try:
            kind = str(BRepAdaptor_Surface(face).GetType().name).replace("GeomAbs_", "").lower()
        except Exception:  # noqa: BLE001
            kind = "unknown"
        shortest: float | None = None
        edges = TopExp_Explorer(face, TopAbs_EDGE)
        while edges.More():
            edge = TopoDS.Edge_s(edges.Current())
            edges.Next()
            if BRep_Tool.Degenerated_s(edge):
                continue
            key = int(all_edges.FindIndex(edge))
            length = lengths.get(key)
            if length is None:
                try:
                    length = float(GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge)))
                except Exception:  # noqa: BLE001 - an unmeasurable edge is not the shortest
                    logger.debug("mesh_hints: edge %d of face %d unmeasurable", key, index)
                    continue
                lengths[key] = length
            if length > floor and (shortest is None or length < shortest):
                shortest = length
        rows.append({
            "face": index, "surface": kind, "area_mm2": float(props.Mass()),
            "centroid": [float(centre.X()), float(centre.Y()), float(centre.Z())],
            "bbox": None if bounds is None else [*bounds[0], *bounds[1]],
            "shortest_edge_mm": shortest})
    return {"diagonal_mm": diagonal, "faces": rows}


def face_table(step_path: Path | str, *, budget_s: float = FACE_TABLE_S) -> dict | None:
    """`_face_table` in a bounded fresh interpreter, or None (unreadable, out of budget)."""
    from meshpipeline.cad.repair.localize import _bounded

    answer = _bounded(_face_table, str(step_path), budget_s=budget_s)
    return answer if isinstance(answer, dict) else None


# --- the producer ------------------------------------------------------------------------------

@dataclass(slots=True)
class _Hint:
    size_mm: float
    why: str
    source: dict
    refinements: int = 0


def _attr(defect: Any, name: str, default: Any = None) -> Any:
    if isinstance(defect, dict):
        return defect.get(name, default)
    return getattr(defect, name, default)


def _face_floor(row: dict, *, default_mm: float, floor_mm: float) -> float:
    """The finest size a face may be given: the part's floor, or coarser where the face is so big
    that the floor would cost more than MAX_FACE_CELLS; never above the default size."""
    area = float(row.get("area_mm2") or 0.0)
    return min(max(floor_mm, math.sqrt(max(area, 0.0) / MAX_FACE_CELLS)), default_mm)


def _seed_size(row: dict, *, default_mm: float, floor_mm: float,
               gap_mm: float | None = None) -> tuple[float, str]:
    floor_mm = _face_floor(row, default_mm=default_mm, floor_mm=floor_mm)
    shortest = row.get("shortest_edge_mm")
    base = float(shortest) if shortest else None
    if gap_mm is not None and gap_mm > floor_mm and (base is None or gap_mm < SEED_FRACTION * base):
        return min(max(gap_mm, floor_mm), default_mm), WHY_SUB_MESH_GAP
    if base is None:
        return min(max(SEED_FRACTION * default_mm, floor_mm), default_mm), WHY_LOCATED_FACE
    return min(max(SEED_FRACTION * base, floor_mm), default_mm), WHY_SHORT_EDGE


def _face_for_box(location: dict, table: list[dict], *, tol: float) -> int | None:
    lo, hi = location.get("bbox_min"), location.get("bbox_max")
    if not lo or not hi:
        return None
    want = [*lo, *hi]
    hits = [row["face"] for row in table if row.get("bbox")
            and all(abs(a - b) <= tol for a, b in zip(want, row["bbox"], strict=True))]
    return int(hits[0]) if len(hits) == 1 else None


def seed_from_defects(defects: Iterable[Any], table: list[dict], *, diagonal_mm: float,
                      ) -> tuple[dict[int, _Hint], int]:
    """Hints for the located MESH defects (code `mesh_*`, on a face) the CAD edits left, keyed by
    delivered face. Defects of other kinds are not sized: the measurement says the fix is finer
    elements where the mesh clashes, not around every small feature. Returns (hints, unplaced)."""
    default_mm = diagonal_mm / DEFAULT_DIVISOR
    floor_mm = diagonal_mm * FLOOR_FRACTION
    rows = {int(r["face"]): r for r in table}
    out: dict[int, _Hint] = {}
    unplaced = 0
    for defect in defects:
        code = _attr(defect, "code")
        code_value = str(getattr(code, "value", code) or "")
        if not code_value.startswith("mesh_") or _attr(defect, "entity_type") != "face":
            continue
        face = _face_for_box(_attr(defect, "location") or {}, table,
                             tol=diagonal_mm * BOX_FRACTION)
        if face is None:
            unplaced += 1
            continue
        measurements = _attr(defect, "measurements") or {}
        gap = measurements.get("clash_gap_mm")
        size, why = _seed_size(rows[face], default_mm=default_mm, floor_mm=floor_mm,
                               gap_mm=float(gap) if isinstance(gap, (int, float)) else None)
        source = {"code": code_value, "fingerprint": _attr(defect, "fingerprint") or None,
                  **{k: measurements[k] for k in ("clash_kind", "clash_gap_mm", "located_by")
                     if measurements.get(k) is not None}}
        if face not in out or size < out[face].size_mm:
            out[face] = _Hint(size, why, source)
    return out, unplaced


def _named_faces(result: ProbeResult, table: list[dict], *, diagonal_mm: float,
                 ) -> tuple[list[int], list[tuple[int, int]]]:
    """The delivered faces a failed attempt names - gmsh's trusted surfaces and the clashing
    pairs - and those pairs as faces. Each surface is matched to a face by fingerprint, never by
    tag number alone; one that matches no face is dropped."""
    from meshpipeline.cad.repair.mesh_probe import locating_surfaces

    failure = result.failure
    if failure is None:
        return [], []
    tags = [*locating_surfaces(failure), *(t for c in result.clashes for t in c.surfaces)]
    if not tags and failure.point is None:
        tags = list(failure.surfaces)   # e.g. an edge through a face: the log's last surface
    props = {int(r["face"]): _key(r) for r in table}
    face_of: dict[int, int] = {}
    for tag in dict.fromkeys(tags):
        fp = result.named_props.get(int(tag))
        face = None if fp is None else match(int(tag), fp, props, diagonal_mm=diagonal_mm)
        if face is not None:
            face_of[int(tag)] = face
    faces = list(dict.fromkeys(face_of.values()))
    pairs = [(face_of[a], face_of[b]) for a, b in (c.surfaces for c in result.clashes)
             if a in face_of and b in face_of]
    return faces, pairs


def _entries(hints: dict[int, _Hint], rows: dict[int, dict]) -> list[dict]:
    out = []
    for face in sorted(hints):
        row, hint = rows[face], hints[face]
        out.append({
            "face": face,
            "fingerprint": {"surface": row["surface"], "area_mm2": row["area_mm2"],
                            "centroid": row["centroid"], "bbox": row["bbox"]},
            "size_mm": hint.size_mm, "why": hint.why, "source": hint.source,
            "refinements": hint.refinements})
    return out


def hints_for(delivered_step_path: Path | str, unresolved_defects: Iterable[Any] = (), *,
              budget_s: float = HINTS_WALL_S, max_attempts: int = MAX_ATTEMPTS) -> Path | None:
    """Write the mesh-hints sidecar next to a delivered STEP and return its path, or None when
    there is no hint to give (it meshes at default, or it could not be read) - then no sidecar is
    left at that path. NEVER RAISES for a bad part; never runs past `budget_s` plus a child's reap.

    Seeds hints from the located mesh defects, then refines them against the real mesher:
    probe with the hints, and while it fails, size every face gmsh names (new ones at
    `SEED_FRACTION` of their shortest edge, known ones by `REFINE_FACTOR`), down to the floor.
    Deterministic for the same file and defects (gmsh runs single-threaded); bounded by
    `max_attempts` and `budget_s`. Every attempt is recorded in the sidecar, and the hints
    written are exactly the ones the last attempt probed: its verdict is the sidecar's `status`.
    """
    from meshpipeline.cad.repair.mesh_probe import NO_VOLUME, PROBE_ERROR, TIMEOUT, probe_file

    step = Path(delivered_step_path)
    started = time.monotonic()
    deadline = started + float(budget_s)
    out_path = sidecar_path(step)
    doc: dict[str, Any] = {
        "schema": SCHEMA, "units": "mm", "face_id": FACE_ID,
        "step": {"name": step.name, "sha256": _sha256(step)},
        "global_max_mm": None, "status": STATUS_NO_FACE_TABLE, "faces": [], "attempts": 0,
        "attempt_log": [],
        "producer": {"max_attempts": int(max_attempts), "wall_s": float(budget_s),
                     "seed_fraction": SEED_FRACTION, "refine_factor": REFINE_FACTOR,
                     "floor_fraction": FLOOR_FRACTION}}
    table_doc = face_table(step, budget_s=min(FACE_TABLE_S, max(deadline - time.monotonic(), 0)))
    if table_doc is None or not table_doc.get("faces"):
        out_path.unlink(missing_ok=True)
        return None
    table: list[dict] = table_doc["faces"]
    rows = {int(r["face"]): r for r in table}
    diagonal = float(table_doc["diagonal_mm"])
    default_mm = diagonal / DEFAULT_DIVISOR
    floor_mm = diagonal * FLOOR_FRACTION
    hints, unplaced = seed_from_defects(unresolved_defects, table, diagonal_mm=diagonal)
    doc.update(face_count=len(table), diagonal_mm=diagonal, default_size_mm=default_mm,
               floor_mm=floor_mm,
               unplaced_defects=unplaced, status=STATUS_UNVERIFIED)
    stop = "max_attempts"
    partners: dict[int, set[int]] = {}
    for attempt in range(int(max_attempts)):
        left = deadline - time.monotonic()
        if left < _MIN_ATTEMPT_S:
            stop = "wall"
            break
        began = time.monotonic()
        result = probe_file(step, diagonal_mm=diagonal, hints=_entries(hints, rows),
                            budget_s=min(ATTEMPT_S, left))
        failure = result.failure
        named, pairs = (([], []) if result.meshed
                        else _named_faces(result, table, diagonal_mm=diagonal))
        for a, b in pairs:
            partners.setdefault(a, set()).add(b)
            partners.setdefault(b, set()).add(a)
        doc["attempt_log"].append({
            "attempt": attempt, "hints": len(hints), "applied": result.hints_applied,
            "meshed": result.meshed, "volume_elements": result.volume_elements,
            "kind": None if failure is None else failure.kind,
            "stage": None if failure is None else failure.stage,
            "faces_named": named, "seconds": round(time.monotonic() - began, 2)})
        if result.meshed:
            doc["status"] = STATUS_VERIFIED if hints else STATUS_NOT_NEEDED
            stop = "meshed"
            break
        if failure is None or failure.kind in (TIMEOUT, PROBE_ERROR, NO_VOLUME):
            stop = "no_verdict" if failure is None or failure.kind != NO_VOLUME else "no_volume"
            break
        if attempt == int(max_attempts) - 1:
            break   # no refinement after the last attempt: what is written is what was probed
        # A face named again is refined WITH every face it has clashed with on any attempt: the
        # crossing is between the two, and gmsh's error often names only one side.
        targets = list(dict.fromkeys(
            [*named, *(p for f in named if f in hints for p in sorted(partners.get(f, ())))]))
        changed = False
        for face in targets:
            hint = hints.get(face)
            if hint is None:
                size, why = _seed_size(rows[face], default_mm=default_mm, floor_mm=floor_mm)
                hints[face] = _Hint(size, why, {"attempt": attempt, "mesh_kind": failure.kind})
                changed = True
            elif hint.size_mm > (finest := _face_floor(rows[face], default_mm=default_mm,
                                                        floor_mm=floor_mm)):
                hint.size_mm = max(hint.size_mm * REFINE_FACTOR, finest)
                hint.refinements += 1
                changed = True
        if not changed:
            stop = "nothing_to_refine"
            break
    doc["stopped"] = stop
    doc["attempts"] = len(doc["attempt_log"])
    doc["faces"] = _entries(hints, rows)
    if not hints:
        out_path.unlink(missing_ok=True)
        return None
    _write(out_path, doc)
    return out_path


def _write(path: Path, doc: dict) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(doc, indent=1, sort_keys=False))
    partial.replace(path)


def read_hints(path: Path | str) -> dict:
    """A sidecar, checked for its schema. Raises ValueError on another schema or major version."""
    doc = json.loads(Path(path).read_text())
    schema = str(doc.get("schema", ""))
    if schema.split("/")[0] != SCHEMA.split("/")[0] or schema.split("/")[-1] != "1":
        raise ValueError(f"not a {SCHEMA} sidecar: {schema!r}")
    return doc


# --- the two numbers ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HintedVerdict:
    """"STEP meshes at default settings" and "STEP meshes with the hints", kept apart."""
    meshes_default: bool
    #: None when there is no usable sidecar (absent, another schema, or for different bytes).
    meshes_with_hints: bool | None
    default: ProbeResult
    hinted: ProbeResult | None
    note: str = ""


def probe_with_hints(step_path: Path | str, hints_path: Path | str | None = None, *,
                     diagonal_mm: float | None = None, budget_s: float = 240.0,
                     quality: bool = False) -> HintedVerdict:
    """Mesh the STEP at default settings, then with its sidecar's hints, and report both. The
    success test is the probe's own: a 3D volume mesh with at least one element. NEVER RAISES.

    `hints_path` defaults to the STEP's sidecar. The diagonal comes from the argument, else the
    sidecar, else a bounded face-table read. Half the budget goes to each probe."""
    from meshpipeline.cad.repair.mesh_probe import PROBE_ERROR, MeshFailure, ProbeResult, probe_file

    step = Path(step_path)
    deadline = time.monotonic() + float(budget_s)
    doc: dict | None = None
    note = ""
    path = Path(hints_path) if hints_path is not None else sidecar_path(step)
    try:
        doc = read_hints(path) if path.exists() else None
        if doc is None:
            note = "no sidecar"
        elif doc.get("step", {}).get("sha256") != _sha256(step):
            doc, note = None, "sidecar is for different STEP bytes"
    except (OSError, ValueError) as exc:
        doc, note = None, f"sidecar unreadable: {exc}"
    diagonal = diagonal_mm or (doc or {}).get("diagonal_mm")
    if not diagonal:
        table = face_table(step, budget_s=min(FACE_TABLE_S, float(budget_s) / 3))
        diagonal = None if table is None else table.get("diagonal_mm")
    if not diagonal:
        dead = ProbeResult(False, 0, MeshFailure(PROBE_ERROR, (), None,
                                                 "the STEP could not be read for its size"))
        return HintedVerdict(False, None, dead, None, note or "unreadable STEP")
    default = probe_file(step, diagonal_mm=float(diagonal), quality=quality,
                         budget_s=max(deadline - time.monotonic(), 0) / 2)
    if doc is None:
        return HintedVerdict(default.meshed, None, default, None, note)
    faces = doc.get("faces") or []
    if not faces:
        return HintedVerdict(default.meshed, default.meshed, default, None, "no hints: default")
    hinted = probe_file(step, diagonal_mm=float(diagonal), hints=faces, quality=quality,
                        budget_s=max(deadline - time.monotonic(), 0))
    return HintedVerdict(default.meshed, hinted.meshed, default, hinted, note)
