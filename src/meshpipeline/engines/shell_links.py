# Responsibility: The SHELL-TO-SOLID JOIN that carries bending moments (design section 2.5, SHLINK-1..3,
# placeholders): the linear constraints that tie a shell mesh to a solid mesh on a mixed part, written per solver.
# Boundaries: pure numpy over node coordinates; it meshes nothing. CalculiX / Abaqus *EQUATION, Nastran MPC and
# Ansys CE text here, the same ties in each (only CalculiX's is solved in our tests).
# Collaborates with: engines/shell_mesh.py (the shell side), engines/shell_writers.py (writes the decks),
# tests/unit/engines/test_shell_textbook.py (the CalculiX proof cases).
"""THE SHELL-TO-SOLID JOIN.

A shell node has rotations, a solid node none. Shell and solid sharing nodes only along the line where they meet
is a HINGE: CalculiX's half-shell, half-solid cantilever deflected 10^9 times too far, a T-joint the same. A
moment can only cross as a couple of forces, so (measured in CalculiX, tests/unit/engines/test_shell_textbook.py):

SHLINK-1, THE SHELL RUNS INTO THE SOLID (an EDGE link: a shell's edge on a solid region). The shell's mid-surface
is carried on into the solid by OVERLAP_WALLS of its wall (at least one element row), and each of its nodes in
there is tied - translations only - to the MEAN of the solid's nodes across the shell's wall at that place: those
whose offset along the shell normal is within half the wall, and whose distance from the node along the shell is
within half the shell's element size. The moment crosses as the couple between the overlapped node rows, spread
over the wall's whole footprint (tied to one node line instead, a 3 mm web's moment pulled the plate locally and
the T was 84% too soft).
  cantilever 200 x 20 x 2, half solid: tip 0.6% off the pure shell and 1.7% off PL^3/3EI;
  T-joint, web shell into a solid plate: 1.6% off the all-shell T and 4.6% off the all-solid one (the all-shell T
  is itself 6.3% softer than the all-solid one: its junction has no metal in the corner).
SHLINK-2, A SOLID STANDING ON A SHELL'S FACE (a FACE link: a thick boss or web on a shelled plate): each solid node
on the contact face rides on the shell point under it, moved rigidly by the offset d from the mid-surface:
u_i = sum_k N_k (u_k + theta_k x d), N_k the shell element's own shape functions at the foot.
  T-joint, plate shell under a solid web: within 4% of the all-solid T.
SHLINK-3, THE SOLID SIDE: at least two quadratic elements through the wall at a link (design 2.5), so its face can
carry the linear stress a moment puts on it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: SHLINK-1: how far the shell runs into the solid, in walls (at least one element row is used)
OVERLAP_WALLS = 1.0
#: SHLINK-1: a shell node's band is the solid nodes on its wall's thickness line - their foot on the mid-surface
#: within TIE_REACH of the shell's element size of it. Wider bands stiffen the join (a quarter: the cantilever's
#: tip 0.6% off the pure shell; a half: 15% stiff), so the solid is meshed to put nodes on the overlapped strip.
TIE_REACH = 0.25


@dataclass
class Tie:
    """One linear constraint: dependent (node, dof) = sum coef * (node, dof). Nodes are the deck's own numbers."""
    node: int
    dof: int
    terms: list[tuple[int, int, float]]


def band_ties(shell_nodes: dict[int, np.ndarray], normal: dict[int, np.ndarray], wall: dict[int, float],
              solid_nodes: dict[int, np.ndarray], *, cap: float) -> list[Tie]:
    """SHLINK-1: each overlapped shell node (id -> xyz) tied, translations only, to the mean of the solid nodes on
    its wall's thickness line: within half the wall of it along its normal, their foot on the mid-surface within
    `cap` of it (TIE_REACH x the shell's element size). A shell node given no solid node is left out (the caller
    says so)."""
    sids = sorted(shell_nodes)
    ids = np.array(sorted(solid_nodes), dtype=np.int64)
    xyz = np.array([solid_nodes[i] for i in ids.tolist()], dtype=float).reshape(-1, 3)
    share: dict[int, list[int]] = {}
    for s in sids:
        p = np.asarray(shell_nodes[s], float)
        n = np.asarray(normal[s], float)
        n = n / float(np.linalg.norm(n))
        q = xyz - p
        off = q @ n
        foot = np.linalg.norm(q - np.outer(off, n), axis=1)
        tol = 1e-6 * max(wall[s], 1.0)
        share[s] = ids[(np.abs(off) <= 0.5 * wall[s] + tol) & (foot <= cap + tol)].tolist()
    out: list[Tie] = []
    for s in sids:
        if not share[s]:
            continue
        w = 1.0 / len(share[s])
        for dof in (1, 2, 3):
            out.append(Tie(s, dof, [(i, dof, w) for i in share[s]]))
    return out


def face_ties(solid_face_nodes: dict[int, np.ndarray], feet: dict[int, list[tuple[int, float]]],
              offset: dict[int, np.ndarray]) -> list[Tie]:
    """SHLINK-2: each solid node i on a contact face rides on the shell under it: u_i = sum_k N_k (u_k + theta_k x
    d_i), feet[i] = [(shell node k, N_k)], d_i = offset[i] (from the mid-surface to the node)."""
    out: list[Tie] = []
    for i in sorted(solid_face_nodes):
        d = np.asarray(offset[i], float)
        # theta x d: x = th_y d_z - th_z d_y; y = th_z d_x - th_x d_z; z = th_x d_y - th_y d_x
        rot = {1: ((5, d[2]), (6, -d[1])), 2: ((6, d[0]), (4, -d[2])), 3: ((4, d[1]), (5, -d[0]))}
        for dof in (1, 2, 3):
            terms = []
            for k, nk in feet[i]:
                terms.append((k, dof, nk))
                for r, c in rot[dof]:
                    if abs(c) > 0:
                        terms.append((k, r, nk * c))
            out.append(Tie(i, dof, terms))
    return out


def equations(ties: list[Tie]) -> list[str]:
    """CalculiX / Abaqus *EQUATION blocks: dependent first (coefficient 1), the rest moved to the left."""
    lines: list[str] = []
    for t in ties:
        terms = [(t.node, t.dof, 1.0)] + [(n, d, -c) for n, d, c in t.terms if abs(c) > 1e-14]
        lines += ["*EQUATION", str(len(terms))]
        flat = [f"{n}, {d}, {c:.12g}" for n, d, c in terms]
        lines += [", ".join(flat[i:i + 4]) for i in range(0, len(flat), 4)]
    return lines


def _terms(t: Tie) -> list[tuple[int, int, float]]:
    """The tie as sum c_j u_j = 0: the dependent first (1), the rest moved to its side."""
    return [(t.node, t.dof, 1.0)] + [(n, d, -c) for n, d, c in t.terms if abs(c) > 1e-14]


def nastran_mpcs(ties: list[Tie], sid: int = 1) -> list[str]:
    """The Nastran form: one MPC card per tie (sum A_j u_j = 0, the first grid the dependent one), in large fixed
    fields (16 characters: a 1/3 keeps its digits). The small-field layout - SID, two (grid, component,
    coefficient) triplets a line, a continuation's first field blank - laid four fields a line, continued by '*'.
    The case control must select it: MPC = sid."""
    from meshpipeline.engines.fea_writers import _f16

    lines: list[str] = []
    for t in ties:
        trip = [[f"{n:>16}", f"{d:>16}", _f16(c)] for n, d, c in _terms(t)]
        data = [f"{sid:>16}", *trip[0], *(trip[1] if len(trip) > 1 else [" " * 16] * 3), " " * 16]
        for i in range(2, len(trip), 2):
            pair = trip[i] + (trip[i + 1] if i + 1 < len(trip) else [" " * 16] * 3)
            data += [" " * 16, *pair, " " * 16]
        for i in range(0, len(data), 4):
            lines.append(("MPC*    " if i == 0 else "*       ") + "".join(data[i:i + 4]).rstrip())
    return lines


_ANSYS_DOF = {1: "UX", 2: "UY", 3: "UZ", 4: "ROTX", 5: "ROTY", 6: "ROTZ"}


def ansys_ces(ties: list[Tie]) -> list[str]:
    """The Ansys form: one constraint equation per tie (0 = sum C_j u_j), CE with three terms a command and
    CE,HIGH carrying on the highest-numbered equation."""
    lines: list[str] = []
    for t in ties:
        terms = [f"{n},{_ANSYS_DOF[d]},{c:.12g}" for n, d, c in _terms(t)]
        lines.append("CE,NEXT,0," + ",".join(terms[:3]))
        for i in range(3, len(terms), 3):
            lines.append("CE,HIGH,0," + ",".join(terms[i:i + 3]))
    return lines


__all__ = ["OVERLAP_WALLS", "Tie", "ansys_ces", "band_ties", "equations", "face_ties", "nastran_mpcs"]
