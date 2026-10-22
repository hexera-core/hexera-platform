"""Harder chip-cooling geometries, sized so they can actually be built.

WHY SIZE FIRST AND DRAW SECOND. The parts already in this package are faithful to their papers and
two of them cannot be meshed at the platform's own resolution rule on any machine we can rent: the
Corintis baseline needs 1.43 billion cells at 13 across its channel, and embedded_silicon_D9 needs
9.3 billion. A harder part drawn without checking is a part nobody can mesh, which teaches nothing.

So every geometry here carries a `cells_at_rule` estimate and the set spans difficulty on purpose:

  UNIT CELL          one channel with symmetry planes. The realistic way a 100-channel chip is
                     actually simulated, and the one that fits comfortably.
  HOTSPOT ARRAY      channels that narrow where the die is hot. This is what Corintis sells -
                     non-uniform cooling placed against a power map - and it stresses a mesher
                     differently from a uniform array because the cell size has to track position.
  MANIFOLD           two levels: a distribution manifold crossing over microchannels beneath it.
                     The geometry `published.NOT_BUILT` could not be taken from its paper, built
                     here parametrically instead and never presented as that paper's part.

UNITS ARE MILLIMETRES, as everywhere else in this package.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: The platform's own flow rule: cells across the narrowest passage (engines/passage.py).
PASSAGE_CELLS_ACROSS = 13
#: Measured on this machine with cfMesh.
BYTES_PER_CELL = 909


def _cells_at_rule(fluid_mm3: float, narrowest_mm: float,
                   cells_across: int = PASSAGE_CELLS_ACROSS) -> float:
    """Fluid cells at the rule's resolution. The solid adds more; this is the floor."""
    cell = narrowest_mm / cells_across
    return fluid_mm3 / (cell ** 3)


@dataclass(frozen=True)
class Sized:
    """What a geometry costs before anyone meshes it."""

    name: str
    narrowest_mm: float
    fluid_mm3: float

    @property
    def cells_at_rule(self) -> float:
        return _cells_at_rule(self.fluid_mm3, self.narrowest_mm)

    @property
    def gb_at_rule(self) -> float:
        return self.cells_at_rule * BYTES_PER_CELL / 1024 ** 3

    def verdict(self, cap_gb: float = 125.0) -> str:
        g = self.gb_at_rule
        return (f"{self.cells_at_rule / 1e6:>8.1f}M fluid cells, {g:>7.1f} GB  "
                f"{'fits' if g <= cap_gb else 'TOO BIG'}")


# #
# 1. the unit cell - the easy one, and the one that is actually used
# #
@dataclass(frozen=True)
class UnitCellSpec:
    """One channel of a parallel array, cut on its symmetry planes.

    THE POINT. A hundred identical channels do not need meshing a hundred times. The array is
    periodic, so one channel with symmetry planes either side carries the same answer for a
    hundredth of the cells. This is how a chip of this kind is really simulated, and it is very
    likely what a 700 million cell figure actually describes.
    """

    channel_w_mm: float = 0.100
    channel_h_mm: float = 0.800
    fin_w_mm: float = 0.100
    length_mm: float = 32.5
    base_mm: float = 0.300
    roof_mm: float = 0.200

    @property
    def pitch_mm(self) -> float:
        return self.channel_w_mm + self.fin_w_mm

    @property
    def narrowest_mm(self) -> float:
        return min(self.channel_w_mm, self.fin_w_mm)

    @property
    def fluid_mm3(self) -> float:
        return self.channel_w_mm * self.channel_h_mm * self.length_mm

    def sized(self) -> Sized:
        return Sized("unit_cell_100um", self.narrowest_mm, self.fluid_mm3)


# #
# 2. the hotspot array - the medium one, and the one Corintis actually sells
# #
@dataclass(frozen=True)
class HotspotArraySpec:
    """Channels that narrow where the die is hot.

    A uniform array cools uniformly, which is the wrong answer for a die whose power is not
    uniform. Narrowing the channel over a hot region raises the local velocity and the local heat
    transfer, at the cost of pressure drop. That trade is the product.

    FOR A MESHER this is harder than a uniform array in a specific way: the narrowest passage is no
    longer the same everywhere, so a single cell size is either wasteful over the cool region or
    too coarse over the hot one. It is the case `engines/gap_sizing` exists for - refine where the
    gap is small, not everywhere.
    """

    n_channels: int = 24
    channel_w_mm: float = 0.120          # nominal, away from the hotspot
    hotspot_w_mm: float = 0.060          # narrowed over the hot region
    channel_h_mm: float = 0.500
    fin_w_mm: float = 0.100
    length_mm: float = 8.0
    hotspot_centre_frac: float = 0.5
    hotspot_span_frac: float = 0.25
    base_mm: float = 0.250
    roof_mm: float = 0.150

    @property
    def narrowest_mm(self) -> float:
        return min(self.hotspot_w_mm, self.fin_w_mm)

    @property
    def footprint_mm(self) -> tuple[float, float]:
        w = self.n_channels * (self.channel_w_mm + self.fin_w_mm) + self.fin_w_mm
        return (w, self.length_mm)

    @property
    def fluid_mm3(self) -> float:
        hot = self.length_mm * self.hotspot_span_frac
        cool = self.length_mm - hot
        per = (cool * self.channel_w_mm + hot * self.hotspot_w_mm) * self.channel_h_mm
        return per * self.n_channels

    def sized(self) -> Sized:
        return Sized("hotspot_array", self.narrowest_mm, self.fluid_mm3)


# #
# 3. the manifold - the hard one
# #
@dataclass(frozen=True)
class ManifoldSpec:
    """Two levels: a distribution manifold crossing over microchannels beneath it.

    Coolant enters the manifold, drops into the microchannels through openings, runs a short
    distance, and leaves through the next opening. The short run is the whole idea: pressure drop
    goes with the length of the channel, so a manifold that feeds every few hundred microns keeps
    the channels short and the pumping power low.

    FOR A MESHER this is the hardest shape in the package. The fluid is one connected network
    across two levels joined only at the openings, so a cell too coarse at an opening severs the
    network and the two levels mesh as separate regions. That is exactly the connectivity failure
    `conjugate.verify` gates on, and this part is here to produce it on demand.

    NOT a reconstruction of `published.NOT_BUILT['manifold_mmc']`, whose paper is missing four of
    the dimensions needed to close the intersection. Parametric, and never to be presented as that.
    """

    n_channels: int = 16
    channel_w_mm: float = 0.100
    channel_h_mm: float = 0.300
    fin_w_mm: float = 0.100
    n_manifold_slots: int = 5            # inlet, outlet, inlet, outlet, inlet
    slot_w_mm: float = 0.400
    manifold_h_mm: float = 0.400
    length_mm: float = 4.0
    base_mm: float = 0.200
    wall_mm: float = 0.150

    @property
    def narrowest_mm(self) -> float:
        return min(self.channel_w_mm, self.fin_w_mm)

    @property
    def channel_run_mm(self) -> float:
        """How far coolant travels in a microchannel between one slot and the next."""
        return self.length_mm / max(self.n_manifold_slots - 1, 1)

    @property
    def fluid_mm3(self) -> float:
        width = self.n_channels * (self.channel_w_mm + self.fin_w_mm) + self.fin_w_mm
        chan = self.n_channels * self.channel_w_mm * self.channel_h_mm * self.length_mm
        slots = self.n_manifold_slots * self.slot_w_mm * self.manifold_h_mm * width
        return chan + slots

    def sized(self) -> Sized:
        return Sized("manifold_two_level", self.narrowest_mm, self.fluid_mm3)


CATALOGUE = (UnitCellSpec(), HotspotArraySpec(), ManifoldSpec())


def report() -> int:
    """What each geometry costs at the platform's own rule, before anything is drawn."""
    print(f"{'geometry':<22}{'narrowest':>11}{'fluid':>12}   at 13 cells across")
    worst = 0.0
    for spec in CATALOGUE:
        s = spec.sized()
        worst = max(worst, s.gb_at_rule)
        print(f"  {s.name:<20}{s.narrowest_mm * 1e3:>8.0f}um{s.fluid_mm3:>10.3f}mm3   {s.verdict()}")
    print()
    print("  for comparison, the parts already in this package:")
    for name, narrow_mm, fluid in (("corintis_baseline", 0.100, 260.0),
                                   ("embedded_D9", 0.010, 0.75),
                                   ("murray_tree", 0.1191, 6.312)):
        s = Sized(name, narrow_mm, fluid)
        print(f"  {s.name:<20}{narrow_mm * 1e3:>8.0f}um{fluid:>10.3f}mm3   {s.verdict()}")
    return 0 if worst <= 125.0 else 1


if __name__ == "__main__":
    raise SystemExit(report())
