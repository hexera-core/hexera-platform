# Responsibility: Turn a delivered VOLUME mesh - cells as corner-node lists, plus the named boundary
# polygons the viewer draws - into the face-based mesh the heatmap measures, so a gmsh or VMTK
# delivery is coloured by the same numbers as an OpenFOAM one.
# Boundaries: connectivity and orientation only. It measures nothing (face_quality does), reads no
# file (the engine that owns the format does) and names no engine.
from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

from meshpipeline.render.face_quality import (
    ASPECT_RATIO_LIMIT,
    INTERNAL_SKEW_LIMIT,
    MAX_FACES_FOR_FIELDS,
    UnreadableMesh,
    _Mesh,
    _Patch,
    quality_fields_of,
)

logger = logging.getLogger(__name__)

#: THE BAR THE HEATMAP PAINTS RED for an engine with no non-orthogonality gate of its own. It is
#: the OpenFOAM family's solver-tolerance bar (engines/openfoam_criteria MAX_NON_ORTHO), the
#: number a finite-volume solver cares about; restated here so render/ imports no engine.
NON_ORTHO_LIMIT = 65.0

# The local faces of each cell kind, by corner count, in the corner order VTK and gmsh share
# (tetrahedron, pyramid with its base first, wedge with its two triangles first, hexahedron).
# The winding in these tables is NOT trusted: every face is turned to point out of its own cell
# numerically below, so a mesher's negative or unusual node order cannot flip a normal.
_FACES_BY_CORNERS: dict[int, tuple[tuple[int, ...], ...]] = {
    4: ((0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)),
    5: ((0, 3, 2, 1), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)),
    6: ((0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)),
    8: ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)),
}
_PAD = -1                                   # the fourth column of a triangle


def _area_vectors(points: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """Twice the area vector of each padded face row (a triangle or a quad); the sign is all
    that is used, so the magnitude convention does not matter."""
    p0, p1, p2 = points[rows[:, 0]], points[rows[:, 1]], points[rows[:, 2]]
    n = np.cross(p1 - p0, p2 - p0)
    quad = rows[:, 3] >= 0
    if quad.any():
        p3 = points[rows[quad, 3]]
        n[quad] = np.cross(p2[quad] - p0[quad], p3 - p1[quad])
    return n


def _face_centres(points: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """The mean of each padded face row's corners (a proxy for the centre; the sign test below
    is all it serves)."""
    present = rows >= 0
    safe = np.where(present, rows, rows[:, [0]])             # a pad reads the first corner
    summed = (points[safe] * present[:, :, None]).sum(axis=1)
    return summed / present.sum(axis=1)[:, None]


def _candidate_faces(points: np.ndarray, blocks: Sequence[np.ndarray]):
    """Every face of every cell, wound to point out of its cell: padded (N,4) vertex rows, the
    cell each row belongs to, and the matching sorted key rows the pairing below runs on."""
    rows_out, cells_out = [], []
    offset = 0
    for block in blocks:
        block = np.asarray(block, dtype=np.int64)
        if block.ndim != 2 or block.shape[1] not in _FACES_BY_CORNERS:
            raise UnreadableMesh(f"a cell block with {block.shape[1:]} corners is not a cell kind "
                                 "the heatmap knows (4, 5, 6 or 8)")
        n = len(block)
        if n == 0:
            continue
        if int(block.min()) < 0 or int(block.max()) >= len(points):
            raise UnreadableMesh("a cell names a point that does not exist")
        centroid = points[block].mean(axis=1)                             # (n,3)
        for local in _FACES_BY_CORNERS[block.shape[1]]:
            rows = np.full((n, 4), _PAD, dtype=np.int64)
            rows[:, :len(local)] = block[:, list(local)]
            outward = np.einsum("ij,ij->i", _area_vectors(points, rows),
                                _face_centres(points, rows) - centroid) >= 0
            flipped = rows.copy()
            flipped[:, :len(local)] = rows[:, :len(local)][:, ::-1]
            rows = np.where(outward[:, None], rows, flipped)
            rows_out.append(rows)
            cells_out.append(offset + np.arange(n, dtype=np.int64))
        offset += n
    if not rows_out:
        raise UnreadableMesh("the volume has no cells")
    rows = np.vstack(rows_out)
    return rows, np.concatenate(cells_out), np.sort(rows, axis=1)


def _padded(polygons: Sequence[Sequence[int]], patch: str) -> np.ndarray:
    out = np.full((len(polygons), 4), _PAD, dtype=np.int64)
    for i, poly in enumerate(polygons):
        c = len(poly)
        if c not in (3, 4):
            raise UnreadableMesh(f"patch {patch!r}: a polygon with {c} corners is not a face the "
                                 "heatmap can colour (3 or 4)")
        out[i, :c] = poly
    return out


def mesh_from_volume(points, cells: Sequence[np.ndarray],
                     boundary: Sequence[tuple[str, Sequence[Sequence[int]]]]) -> _Mesh:
    """The face-based mesh of a volume mesh, with its boundary patches in the viewer's order.

    `points` is (P,3). `cells` is a list of (n,k) corner-node arrays, one block per cell kind,
    k in {4,5,6,8}; a second-order element passes its corner nodes only. `boundary` names each
    patch with the polygons the viewer draws for it, as corner-node sequences of 3 or 4, in the
    ORDER it draws them: a polygon with c corners is drawn as c-2 fan triangles and takes that
    many entries in the patch's field, so a quad reads twice. Every polygon must be a face of
    exactly one cell; a polygon that is not - a surface that does not sit on the volume - is
    refused, because a field aligned by guesswork would colour faces with other cells' numbers.

    Faces come out internal first (owner the lower cell id, wound from owner to neighbour), then
    every boundary face once, whether or not a patch draws it - a cell must be closed for its
    centre and volume to mean anything - and each patch names its faces explicitly."""
    points = np.ascontiguousarray(np.asarray(points, dtype=np.float64))
    if points.ndim != 2 or points.shape[1] != 3:
        raise UnreadableMesh("points must be (P,3)")
    rows, cell_of, keys = _candidate_faces(points, cells)
    if len(rows) > 2 * MAX_FACES_FOR_FIELDS:
        raise UnreadableMesh(f"{len(rows)} cell faces exceed the {MAX_FACES_FOR_FIELDS} cap")

    _uniq, inv, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inv = np.asarray(inv).reshape(-1)
    if int(counts.max()) > 2:
        raise UnreadableMesh("a face is shared by more than two cells (non-manifold volume)")
    order = np.argsort(inv, kind="stable")
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])

    # INTERNAL faces: the two rows of each pair; the owner is the lower cell id and the face
    # keeps the owner's outward winding, which is the OpenFOAM convention the metrics assume.
    pair = np.nonzero(counts == 2)[0]
    a, b = order[starts[pair]], order[starts[pair] + 1]
    swap = cell_of[b] < cell_of[a]
    a, b = np.where(swap, b, a), np.where(swap, a, b)
    internal_rows, owner_int, neighbour = rows[a], cell_of[a], cell_of[b]

    # BOUNDARY faces: every single row, once, in a fixed order; patches point into this list.
    single = order[starts[np.nonzero(counts == 1)[0]]]
    boundary_rows, owner_bnd, boundary_keys = rows[single], cell_of[single], keys[single]
    n_int, n_bnd = len(internal_rows), len(boundary_rows)

    patches: list[_Patch] = []
    if boundary:
        query_blocks = [_padded(polys, name) for name, polys in boundary]
        query = np.vstack(query_blocks) if query_blocks else np.zeros((0, 4), np.int64)
        if len(query):
            both = np.vstack([boundary_keys, np.sort(query, axis=1)])
            _u, inv2 = np.unique(both, axis=0, return_inverse=True)
            inv2 = np.asarray(inv2).reshape(-1)
            first = np.full(len(_u), -1, dtype=np.int64)
            first[inv2[:n_bnd]] = np.arange(n_bnd)
            match = first[inv2[n_bnd:]]
            if (match < 0).any():
                at = 0
                for (name, _polys), block in zip(boundary, query_blocks):
                    miss = int(np.count_nonzero(match[at:at + len(block)] < 0))
                    if miss:
                        raise UnreadableMesh(f"patch {name!r}: {miss} of {len(block)} drawn "
                                             "polygon(s) are not a face of any cell")
                    at += len(block)
            at = 0
            for (name, _polys), block in zip(boundary, query_blocks):
                m = match[at:at + len(block)]
                at += len(block)
                fan = np.where(block[:, 3] >= 0, 2, 1)             # triangles a polygon draws as
                ids = np.repeat(n_int + m, fan)
                patches.append(_Patch(name, -1, int(len(ids)), face_ids=ids))

    all_rows = np.vstack([internal_rows, boundary_rows])
    sizes = np.where(all_rows[:, 3] >= 0, 4, 3)
    face_off = np.zeros(len(all_rows) + 1, dtype=np.int64)
    np.cumsum(sizes, out=face_off[1:])
    face_flat = all_rows[all_rows >= 0]                 # padding sits at the row's end
    owner = np.concatenate([owner_int, owner_bnd]).astype(np.int32)
    return _Mesh(points, face_flat.astype(np.int32), face_off, owner,
                 neighbour.astype(np.int32), patches)


def attach_volume_quality(resp: dict, points, cells: Sequence[np.ndarray],
                          boundary: Sequence[tuple[str, Sequence[Sequence[int]]]], *,
                          non_ortho_limit: float = NON_ORTHO_LIMIT,
                          skew_limit: float = INTERNAL_SKEW_LIMIT,
                          aspect_limit: float = ASPECT_RATIO_LIMIT) -> None:
    """Add `quality_fields` to a viewer response built from a volume mesh, or leave it untouched.

    The same contract as face_quality.attach_quality_fields: the colouring is an addition to a
    mesh the user already has, so nothing that goes wrong here may cost them the mesh. A mesh
    this cannot read is logged and skipped; anything unexpected is logged with its traceback."""
    try:
        names = [p.get("name") for p in (resp.get("patches") or []) if isinstance(p, dict)]
        mesh = mesh_from_volume(points, cells, boundary)
        fields = quality_fields_of(mesh, non_ortho_limit=non_ortho_limit, skew_limit=skew_limit,
                                   aspect_limit=aspect_limit, patch_names=names)
        if fields:
            resp["quality_fields"] = fields
    except UnreadableMesh as exc:
        logger.info("viewer quality fields skipped: %s", exc)
    except Exception:  # noqa: BLE001 - a heatmap is never worth a delivery
        logger.exception("viewer quality fields unavailable - the mesh ships without a heatmap")
