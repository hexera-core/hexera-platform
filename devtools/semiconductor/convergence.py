# Responsibility: Say what a mesh's answer is worth, from the same answer on coarser meshes.
# Boundaries: arithmetic over finished runs. It meshes nothing, solves nothing and reads no mesh.
"""GRID CONVERGENCE: the uncertainty on a number, from three meshes of the same part.

WHY THIS EXISTS. The four-way energy audit in cht.py answers "is this mesh self-consistent and
solvable" and it does not answer "is the answer right". Measured, 2026-10-22: unit_cell_100um at 2
cells across is 27.5% short of its CAD, and it solved, converged, conserved mass exactly and closed
all four heat readings to 0.00%. The coolant rise it reported was 32.66 K against the 5-across
mesh's 20.18 K. Nothing inside one run can tell you which of those is nearer the truth.

Three runs can. If the discretisation error falls as some power of the cell size, then the same
quantity on three systematically refined meshes determines that power, the value the sequence is
heading for, and a band around the finest mesh's answer. That is Richardson extrapolation and the
Grid Convergence Index, as set out in Roache's work and standardised in ASME V&V 20; the five-step
procedure here is Celik et al. (2008), which is what journals ask for.

    h           a representative cell size, (volume / cells)^(1/3)
    r21         h2/h1, the refinement ratio from the fine mesh to the middle one
    p           the OBSERVED order of convergence, solved for rather than assumed
    phi_ext     the extrapolated value at zero cell size
    GCI         1.25 x the relative change between the two finest, scaled by (r^p - 1)

WHAT IT IS NOT. It is not a proof that the answer is right: a mesh sequence can converge neatly to
the wrong number if the geometry, the physics or the boundary conditions are wrong, and all three
meshes share those. It bounds DISCRETISATION error and nothing else. It also assumes the runs are
converged in the solver sense, which is why `analyse` refuses a run that is not.

The 1.25 is the factor Roache recommends when the observed order comes out near the formal order of
the scheme and three grids were used. It is a safety factor on an error estimate, not a measurement,
and it is the one piece of this that is a convention rather than arithmetic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Roache's safety factor for a three-grid study. A convention, and the only one here.
SAFETY_FACTOR = 1.25

#: Below this refinement ratio the two meshes are too alike for the difference between them to be
#: signal. Celik et al. recommend 1.3 as a minimum; anything under it and the subtraction is
#: dominated by whatever else differs between the two runs.
MIN_REFINEMENT_RATIO = 1.3

#: A fixed-point solve, because p appears on both sides once the ratios are not exactly equal.
_MAX_ITERATIONS = 200
_TOLERANCE = 1.0e-10


class ConvergenceError(ValueError):
    """Not enough, or not the right kind of, evidence to say anything about convergence."""


@dataclass(frozen=True)
class Grid:
    """One mesh's answer for one quantity."""

    label: str
    cells: int
    #: The representative cell size, in metres: (meshed volume / cells)^(1/3). THE MESH'S OWN, not
    #: the size that was asked for. What a sizing request turns into is the thing being studied.
    h: float
    value: float
    converged: bool = True

    def __post_init__(self) -> None:
        if self.h <= 0 or not math.isfinite(self.h):
            raise ConvergenceError(f"{self.label}: cell size must be positive, got {self.h}")
        if not math.isfinite(self.value):
            raise ConvergenceError(f"{self.label}: value is not finite ({self.value})")


@dataclass
class Convergence:
    """What three meshes say about one quantity, and how much of it to believe."""

    quantity: str
    #: Fine to coarse, always, because every formula below is written that way round.
    grids: tuple[Grid, ...]
    behaviour: str = "unknown"
    order: float | None = None
    extrapolated: float | None = None
    #: Fractional, not per cent. The band on the FINEST mesh's answer.
    gci: float | None = None
    #: |phi1 - phi2| / |phi1|, the raw change between the two finest. Reported beside the GCI
    #: because it is what gets quoted by someone who has not done this, and it is NOT an error
    #: estimate: it is the difference between two meshes, where the GCI is an estimate of how far
    #: the finer one still is from the answer.
    #:
    #: WHICH IS BIGGER DEPENDS ON THE OBSERVED ORDER, since GCI / change = 1.25 / (r**p - 1). At
    #: r = 2 and p = 2 that is 0.42, so the raw change OVERSTATES the error threefold. At p = 0.5
    #: it is 3.0, so the raw change understates it threefold. On a cut-cell mesh of a chip-cooling
    #: part, where the staircase error moves erratically with resolution, p comes out low, and the
    #: raw change is then the optimistic number - which is the one that would have been quoted.
    relative_change: float | None = None
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def fine(self) -> Grid:
        return self.grids[0]

    @property
    def band(self) -> tuple[float, float] | None:
        """The finest answer plus and minus its GCI, as an absolute interval."""
        if self.gci is None:
            return None
        half = abs(self.fine.value) * self.gci
        return (self.fine.value - half, self.fine.value + half)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        if self.gci is None or self.band is None:
            return f"{self.quantity}: no band ({self.behaviour})"
        lo, hi = self.band
        return (f"{self.quantity}: {self.fine.value:.6g} with a {self.gci:.2%} band "
                f"[{lo:.6g}, {hi:.6g}], observed order {self.order:.2f}, "
                f"extrapolated {self.extrapolated:.6g}")


def ratios(grids: tuple[Grid, ...]) -> tuple[float, float]:
    """(r21, r32) for three grids ordered fine to coarse."""
    g1, g2, g3 = grids
    return g2.h / g1.h, g3.h / g2.h


def observed_order(eps32: float, eps21: float, r21: float, r32: float) -> float:
    """Solve Celik's equation for p, the order the sequence actually converges at.

    p is on both sides through q(p) whenever the two refinement ratios differ, so it is solved by
    fixed-point iteration from the constant-ratio answer. With r21 == r32 the q term is zero and the
    first guess is already exact.

    NOT ASSUMED TO BE THE SCHEME'S FORMAL ORDER. Upwind convection is formally first order and the
    laplacian is second, so there is no single formal order to assume here, and an observed order
    far from either is itself the finding: it usually means the meshes are not in the asymptotic
    range and the band below is optimistic.
    """
    if eps21 == 0:
        raise ConvergenceError(
            "the two finest meshes gave the same answer to the last digit, so there is no "
            "difference to extract an order from")
    s = math.copysign(1.0, eps32 / eps21)
    ratio = abs(eps32 / eps21)
    p = abs(math.log(ratio) / math.log(r21))
    for _ in range(_MAX_ITERATIONS):
        q = math.log((r21 ** p - s) / (r32 ** p - s)) if abs(r21 - r32) > 1e-12 else 0.0
        nxt = abs((math.log(ratio) + q) / math.log(r21))
        if not math.isfinite(nxt):
            raise ConvergenceError("the order solve diverged")
        if abs(nxt - p) < _TOLERANCE:
            return nxt
        p = nxt
    return p


def best_triple(grids: tuple[Grid, ...]) -> tuple[Grid, ...]:
    """Three grids out of however many there are, chosen for spacing and not just for fineness.

    THE THREE FINEST IS THE WRONG RULE. A part meshed at 2, 4, 5 and 8 cells across has its three
    finest at 8, 5 and 4, whose refinement ratios are 1.66 and 1.21 - and 1.21 is under the floor,
    so the study gets refused even though 8, 4 and 2 sit at 1.89 and 2.00 and would have worked.
    Anyone adding a run to a sweep would be making the study worse.

    So: of every triple whose ratios both clear the floor, take the one with the FINEST mesh in it,
    because that is the answer the band goes on, and break ties on the wider spacing. Falls back to
    the three finest when nothing clears the floor, so `analyse` can report that properly rather
    than being handed an empty choice.
    """
    ordered = tuple(sorted(grids, key=lambda g: g.h))
    if len(ordered) <= 3:
        return ordered
    best = None
    for i in range(len(ordered) - 2):
        for j in range(i + 1, len(ordered) - 1):
            for k in range(j + 1, len(ordered)):
                triple = (ordered[i], ordered[j], ordered[k])
                r21, r32 = ratios(triple)
                if min(r21, r32) < MIN_REFINEMENT_RATIO:
                    continue
                # finest h1 first, then the wider spacing
                key = (triple[0].h, -min(r21, r32))
                if best is None or key < best[0]:
                    best = (key, triple)
    return best[1] if best else ordered[:3]


def analyse(quantity: str, grids: list[Grid]) -> Convergence:
    """The five-step procedure, on three grids ordered fine to coarse.

    Refuses rather than guesses. A study on two grids, on grids too alike to subtract, or on runs
    that never converged is not a study, and reporting a band from one would be worse than
    reporting nothing: a band is read as a guarantee.
    """
    ordered = tuple(sorted(grids, key=lambda g: g.h))
    out = Convergence(quantity=quantity, grids=ordered)

    if not ordered:
        out.behaviour = "not measured"
        out.problems.append(
            "no run reported this quantity, so there is nothing to study. A solve-side quantity "
            "needs runs that were solved, not only meshed.")
        return out
    if len(ordered) < 3:
        out.behaviour = "too few grids"
        out.problems.append(
            f"a grid convergence study needs three meshes of the same part and this has "
            f"{len(ordered)}. Two give a difference and no order, so there is no band to put on "
            "either of them.")
        return out
    if len(ordered) > 3:
        chosen = best_triple(ordered)
        dropped = [g.label for g in ordered if g not in chosen]
        out.notes.append(
            f"{len(ordered)} grids available; {', '.join(g.label for g in chosen)} used, and "
            f"{', '.join(dropped)} left out. The procedure takes three, and they are chosen for "
            "spacing as well as fineness: see best_triple.")
        ordered = chosen
        out.grids = ordered

    unconverged = [g.label for g in ordered if not g.converged]
    if unconverged:
        out.behaviour = "unconverged runs"
        out.problems.append(
            f"these runs never met their residual targets: {', '.join(unconverged)}. The "
            "difference between two unfinished solves is not a discretisation error, so nothing "
            "here would mean anything.")
        return out

    r21, r32 = ratios(ordered)
    for name, r in (("fine to middle", r21), ("middle to coarse", r32)):
        if r < MIN_REFINEMENT_RATIO:
            out.problems.append(
                f"the {name} refinement ratio is only {r:.3f}, under the {MIN_REFINEMENT_RATIO} "
                "the procedure needs. Those two meshes are too alike for the difference between "
                "them to be the discretisation error rather than noise.")
    if out.problems:
        out.behaviour = "grids too alike"
        return out

    g1, g2, g3 = ordered
    eps21, eps32 = g2.value - g1.value, g3.value - g2.value
    out.relative_change = abs(eps21 / g1.value) if g1.value else None

    if eps21 == 0:
        out.behaviour = "identical"
        out.notes.append(
            "the two finest meshes agree to the last digit, which is either a converged answer or "
            "a quantity that does not depend on the mesh at all")
        out.order, out.extrapolated, out.gci = None, g1.value, 0.0
        return out

    if eps32 / eps21 < 0:
        # Celik's step 5 note: with the sign flipping there is no monotone sequence to extrapolate
        # along, and the honest band is the oscillation itself.
        out.behaviour = "oscillatory"
        out.notes.append(
            "the answer went one way and then back: the sequence is oscillatory, not monotone, so "
            "there is no order to extract and no value to extrapolate to. The band quoted is the "
            "oscillation's own amplitude, which is a floor on the uncertainty rather than an "
            "estimate of it.")
        out.gci = abs(eps21 / g1.value) if g1.value else None
        return out

    try:
        out.order = observed_order(eps32, eps21, r21, r32)
    except ConvergenceError as exc:
        out.behaviour = "no order"
        out.problems.append(str(exc))
        return out

    out.behaviour = "monotone"
    r21p = r21 ** out.order
    out.extrapolated = (r21p * g1.value - g2.value) / (r21p - 1.0)
    out.gci = SAFETY_FACTOR * abs(eps21 / g1.value) / (r21p - 1.0) if g1.value else None

    if out.order > 3.0:
        out.notes.append(
            f"the observed order is {out.order:.2f}, well above anything these schemes can deliver "
            "(upwind convection is formally first order). That usually means the meshes are not in "
            "the asymptotic range yet and the band is optimistic, not that the mesh is unusually "
            "good.")
    elif out.order < 0.5:
        out.notes.append(
            f"the observed order is only {out.order:.2f}, so refining is buying very little. On a "
            "cut-cell mesh that is what a geometry whose features keep landing differently against "
            "the octree looks like.")
    return out


__all__ = ["Grid", "Convergence", "ConvergenceError", "SAFETY_FACTOR", "MIN_REFINEMENT_RATIO",
           "ratios", "observed_order", "best_triple", "analyse"]
