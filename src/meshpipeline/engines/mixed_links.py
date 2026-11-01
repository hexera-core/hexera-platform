# Responsibility: The join of a MIXED part (shell where thin, solid where thick): each shell edge that ends on a
# solid region's cut face is tied to the solid so that bending moments cross (SHLINK-4, placeholder) - the shell runs
# one wall into the solid, and each of its two node rows there (on the cut face and on a tie face one wall deeper) is
# tied, translations only, to the weighted mean of its section's solid nodes: the moment crosses as the couple
# between the rows, each spread over the wall's whole section.
# Boundaries: pure numpy over node coordinates; it meshes nothing and writes no file. The ties come out as
# shell_links.Tie rows, written per solver by shell_links (CalculiX / Abaqus *EQUATION, Nastran MPC, Ansys CE).
# Collaborates with: engines/shell_links.py (Tie, the per-solver text, SHLINK-1 whose rule this carries to
# unstructured tets), engines/mixed_mesh.py (finds the rows and the sections, writes the decks),
# tests/unit/engines/test_mixed_textbook.py (the CalculiX proof cases).
"""SHLINK-4: A SHELL EDGE ON A SOLID REGION'S CUT FACE.

A mixed part's solid region is cut across each thin wall it meets, a measured distance into the wall (MIXED-2), so
the wall's stress is plate-like at the cut, and the cut face is the wall's own cross-section. A shell node has
rotations, a solid node none: shared nodes there would be a hinge (SHLINK-1).

Why not the shell node's rotation tied to the face's rigid motion (Abaqus' *SHELL TO SOLID COUPLING, Nastran's RBE3
from the shell node): CalculiX expands a shell into solid elements and its shell nodes' rotations never reach an
*EQUATION - tied that way, a half-shell, half-solid cantilever was still a hinge (its tip 10^8 times too far), with
or without a moment put on the nodes to make CalculiX knot them. Only a form CalculiX solves is written, the same in
every solver, so every deck carries the join that was proved.

The form (SHLINK-1's, carried to a tetrahedral mesh): the shell runs OVERLAP_WALLS of its wall on into the solid,
past the cut face, to a TIE FACE the solid is meshed with (an inner face across the wall, parallel to the cut face).
The shell has a node row on each. Each row's node k is tied, translations only, to the mean of the section's solid
nodes j weighted by w_j = (the node's share of the section's area) x (the row's hat function of node k at the node's
place along the row): the shares of neighbouring shell nodes add up to the whole section, so a force on the row is
spread over the wall's whole section, none of it twice. A moment crosses as the couple between the two rows, a wall
apart; a membrane force and an in-plane moment as the rows' own spread along the edge.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from meshpipeline.engines.shell_links import Tie

#: SHLINK-4: how far the shell runs on past the cut face into the solid, in walls (SHLINK-1's OVERLAP_WALLS)
OVERLAP_WALLS = 1.0
#: a face node's weight under this share of its row's largest is dropped (it changes no digit written)
TERM_FLOOR = 1e-9
#: a direction the points spread less along than this share of their widest is flat (left out of the fit)
FLAT_SHARE = 1e-4


@dataclass
class Row:
    """One shell node row on one section (the cut face or the tie face): its nodes in order (deck ids) and their
    places (mm); `closed`: a loop (a hole's rim, a tube's end), its last node next to its first."""
    nodes: list[int]
    xyz: np.ndarray
    closed: bool


def face_node_areas(xyz: dict[int, np.ndarray], triangles: np.ndarray) -> dict[int, float]:
    """Each face node's share of the face's area. `triangles` (m, 3 or 6) node ids, corners first then the edge
    midpoints 1-2, 2-3, 3-1. A 6-node triangle is split into four (its corners' triangles and the middle one), a third
    of each to its three nodes; a 3-node triangle gives a third to each corner."""
    out: dict[int, float] = {}
    for row in np.asarray(triangles, dtype=np.int64):
        tri = [int(v) for v in row]
        if len(tri) == 6:
            a, b, c, ab, bc, ca = tri
            subs = [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
        else:
            subs = [(tri[0], tri[1], tri[2])]
        for s in subs:
            p = [np.asarray(xyz[i], float) for i in s]
            area = 0.5 * float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])))
            for i in s:
                out[i] = out.get(i, 0.0) + area / 3.0
    return out


def hat_weights(row: Row, x: np.ndarray) -> np.ndarray:
    """(points, row nodes): each row node's hat function at each point's place along the row - the point taken to its
    nearest segment; a point past an open row's end belongs to its end node whole. Every row adds up to 1."""
    p = np.asarray(row.xyz, float)
    n = len(p)
    out = np.zeros((len(x), n))
    if n == 1:
        out[:, 0] = 1.0
        return out
    a = p if row.closed else p[:-1]
    b = np.roll(p, -1, axis=0) if row.closed else p[1:]
    seg = b - a
    ll = np.maximum((seg * seg).sum(axis=1), 1e-300)
    s = np.clip(((x[:, None, :] - a[None, :, :]) * seg[None, :, :]).sum(axis=2) / ll[None, :], 0.0, 1.0)
    foot = a[None, :, :] + s[:, :, None] * seg[None, :, :]
    k = np.argmin(np.linalg.norm(x[:, None, :] - foot, axis=2), axis=1)
    sk = s[np.arange(len(x)), k]
    rows = np.arange(len(x))
    np.add.at(out, (rows, k), 1.0 - sk)
    np.add.at(out, (rows, (k + 1) % n), sk)
    return out


def linear_weights(ref: np.ndarray, pts: np.ndarray, w: np.ndarray, *, dims: int = 2) -> np.ndarray:
    """The coefficients c_j with sum c_j f(x_j) = f(ref) for every field f linear over the section: the weighted
    least-squares plane through the points' values, read at `ref`. A plain weighted mean is that only where the
    weights' centre is the node itself: at a row's end node (its hat one-sided) the mean sat a third of an element
    inward, the in-plane moment of a cantilever crossed short and the join was 21% stiff."""
    r = np.asarray(pts, float) - np.asarray(ref, float)[None, :]
    wn = np.asarray(w, float) / float(np.sum(w))
    centre = (wn[:, None] * r).sum(axis=0)
    _u, sv, vt = np.linalg.svd(np.sqrt(wn)[:, None] * (r - centre[None, :]), full_matrices=False)
    keep = [k for k in range(min(dims, len(sv))) if sv[k] > FLAT_SHARE * max(float(sv[0]), 1e-300)]
    a = np.column_stack([np.ones(len(r))] + [r @ vt[k] for k in keep])
    big = a.T @ (wn[:, None] * a)
    return np.linalg.solve(big, (a * wn[:, None]).T)[0]


def section_ties(row: Row, face_xyz: dict[int, np.ndarray], face_area: dict[int, float]) -> list[Tie]:
    """SHLINK-4: each node of `row` tied, translations only, to the weighted mean of the section's nodes (node id ->
    xyz mm; their area shares). A row node no section node reaches is left out (the caller counts them)."""
    ids = np.array(sorted(face_xyz), dtype=np.int64)
    x = np.array([face_xyz[i] for i in ids.tolist()], dtype=float).reshape(-1, 3)
    area = np.array([face_area.get(i, 0.0) for i in ids.tolist()])
    hats = hat_weights(row, x)
    out: list[Tie] = []
    for c, node in enumerate(row.nodes):
        w = area * hats[:, c]
        top = float(w.max()) if len(w) else 0.0
        if top <= 0.0:
            continue
        use = np.flatnonzero(w > TERM_FLOOR * top)
        # all three directions: a curved cut face (a boss's cylinder) is not flat across a row's share
        wu = linear_weights(np.asarray(row.xyz[c], float), x[use], w[use], dims=3)
        for dof in (1, 2, 3):
            out.append(Tie(int(node), dof, [(int(ids[j]), dof, float(v)) for j, v in zip(use.tolist(), wu.tolist(),
                                                                                       strict=True)]))
    return out


def band_ties(row: Row, inward: np.ndarray, normal: np.ndarray, wall: np.ndarray, solid_xyz: dict[int, np.ndarray],
              *, depth: float, half_band: float) -> list[Tie]:
    """SHLINK-4, the inner row: each node of `row` (a wall's depth into the solid past the cut face) tied, translations
    only, to the solid nodes round it - those within `half_band` of its depth along `inward` (the row's direction into
    the solid, per node), inside its wall (`normal`, `wall` per node) and on its share of the row - by the weighted
    least-squares linear field through them read at the node (weights: the row's hat function). Linear in all three
    directions: the band has depth, so a node off the band's middle still reads the field where it is."""
    ids = np.array(sorted(solid_xyz), dtype=np.int64)
    x = np.array([solid_xyz[i] for i in ids.tolist()], dtype=float).reshape(-1, 3)
    p = np.asarray(row.xyz, float)
    lo, hi = p.min(axis=0) - (float(np.max(wall)) + depth + half_band), p.max(axis=0) + (float(np.max(wall)) + depth + half_band)
    near = np.flatnonzero(np.all((x >= lo) & (x <= hi), axis=1))
    ids, x = ids[near], x[near]
    hats = hat_weights(row, x)
    out: list[Tie] = []
    for c, node in enumerate(row.nodes):
        r = x - p[c][None, :]
        along_in = r @ inward[c]
        across = r @ normal[c]
        w = hats[:, c] * ((np.abs(along_in) <= half_band) & (np.abs(across) <= 0.5 * wall[c] * (1 + 1e-6)))
        use = np.flatnonzero(w > TERM_FLOOR * max(float(w.max()) if len(w) else 0.0, 1e-300))
        if len(use) < 4:
            continue
        wu = linear_weights(p[c], x[use], w[use], dims=3)
        for dof in (1, 2, 3):
            out.append(Tie(int(node), dof, [(int(ids[j]), dof, float(v)) for j, v in zip(use.tolist(), wu.tolist(),
                                                                                       strict=True)]))
    return out


__all__ = ["OVERLAP_WALLS", "Row", "band_ties", "face_node_areas", "hat_weights", "linear_weights", "section_ties"]
