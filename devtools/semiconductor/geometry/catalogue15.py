"""Fifteen more chip-cooling geometries, sized before any of them is drawn.

WHY SIZE FIRST. Two parts already in this package are faithful to their papers and cannot be meshed
at the platform's own resolution rule on any machine we can rent: the Corintis baseline needs 1.43
billion cells and embedded_silicon_D9 needs 9.3 billion. A test article nobody can mesh teaches
nothing, so every spec here reports what it costs and `report()` refuses the set if any part cannot
be built on hardware that exists.

DIMENSIONS COME FROM PUBLISHED WORK where published work gives them, and every spec says which.
Where a family is described in the literature without a dimension table, the spec is parametric and
says so rather than inventing a citation.

The set spans six families on purpose, because a mesher fails differently on each:

  STRAIGHT        the baseline. Parallel prisms that never touch.
  DOUBLE LAYER    two stacked arrays, counterflow. Two fluid networks in one block.
  OBLIQUE FIN     channels interrupted by angled fins. Every interruption is a fresh leading edge.
  WAVY            a channel whose centreline curves. Staircasing shows up here first.
  PIN FIN         flow around posts rather than along slots. Far more wetted surface per volume.
  MANIFOLD        two levels joined only at openings. Severable connectivity.

UNITS ARE MILLIMETRES throughout, as everywhere else in this package.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: The platform's own rule (engines/passage.PASSAGE_CELLS_ACROSS) and its thermal counterpart
#: (engines/snappy_multiregion/thermal_fidelity.PLAN_CELLS_ACROSS).
FLOW_CELLS_ACROSS = 13
THERMAL_CELLS_ACROSS = 3
#: Measured on this machine with cfMesh, 2026-10-21.
BYTES_PER_CELL = 909
#: What we can actually mesh on.
LAPTOP_GB, BENCH_GB, BIG_GB = 15.0, 125.0, 976.0


@dataclass(frozen=True)
class Source:
    paper: str
    note: str
    measured: bool          # True when the dimensions are the paper's own, False when parametric


@dataclass(frozen=True)
class Part:
    name: str
    family: str
    #: the coolant passage the fluid moves through, and the thinnest solid heat must cross
    passage_mm: float
    layer_mm: float
    fluid_mm3: float
    block_mm3: float
    source: Source
    #: what makes this one hard for a mesher, in one line
    stresses: str

    @property
    def binding_cell_mm(self) -> float:
        """The finer of the two rules' demands. Flow almost always binds on a cooled chip."""
        return min(self.passage_mm / FLOW_CELLS_ACROSS, self.layer_mm / THERMAL_CELLS_ACROSS)

    @property
    def binding_regime(self) -> str:
        return ("flow" if self.passage_mm / FLOW_CELLS_ACROSS
                <= self.layer_mm / THERMAL_CELLS_ACROSS else "thermal")

    @property
    def fluid_cells(self) -> float:
        return self.fluid_mm3 / self.binding_cell_mm ** 3

    @property
    def conjugate_cells(self) -> float:
        """Fluid at the binding cell, solid at a background eight times coarser.

        The conjugate method is a coarse background with octree levels spent only at the coolant,
        so the solid is NOT filled at channel resolution. Three levels is the eight-fold step the
        built cases use, and the murray build came out within a few per cent of this estimate.
        """
        solid_mm3 = max(self.block_mm3 - self.fluid_mm3, 0.0)
        return self.fluid_cells + solid_mm3 / (self.binding_cell_mm * 8) ** 3

    @property
    def gb(self) -> float:
        return self.conjugate_cells * BYTES_PER_CELL / 1024 ** 3

    @property
    def where(self) -> str:
        g = self.gb
        if g <= LAPTOP_GB:
            return "laptop"
        if g <= BENCH_GB:
            return "mesh-bench"
        if g <= BIG_GB:
            return "big box"
        return "NOWHERE"


# ---------------------------------------------------------------- sources
VAFAI_ZHU = Source(
    "Vafai and Zhu, Int. J. Heat and Mass Transfer 42:2287-2297 (1999)",
    "The original double-layer design: 20 x 10 x 1.05 mm, 100 channels 50 um wide, coolant flowing "
    "in OPPOSITE directions in the two layers to even out the axial temperature gradient.", True)
OBLIQUE = Source(
    "Oblique-fin silicon test vehicles, single-phase water",
    "100 and 200 um nominal channel widths; best performance at 100 um with a 27 degree oblique "
    "angle, up to 47% heat transfer gain at Re 680.", True)
WAVY = Source(
    "Wavy microchannel with oblique grooves (numerical)",
    "Wave amplitude 250 um, wavelength 2500 um, fin widths 250 and 125 um; best case Nu about 80 "
    "at Re 850 for about 50 kPa.", True)
PINFIN_PITCH = Source(
    "Pitch and aspect-ratio study, diamond micro pin fins",
    "Transverse pitch-to-diameter 1.7 to 3.0, height-to-diameter 0.7 to 3.2. In-line circular "
    "arrays at 100 um pin diameter are the common comparison case.", True)
HYBRID = Source(
    "Oblique microchannel merged with circular micro pin fins",
    "Channel widths 500 and 300 um, fin widths 465.2 and 245.2 um, fin pitch 1995.2 and 1164.5 um, "
    "Re 300 to 800.", True)
PARAMETRIC = Source(
    "parametric, no single paper",
    "Built to a described family rather than a dimension table. Never to be presented as any "
    "paper's part.", False)


def _straight_fluid(n, w, h, length):
    return n * w * h * length


def _block(width, length, height):
    return width * length * height


# ---------------------------------------------------------------- the fifteen
PARTS: tuple[Part, ...] = (
    # --- double layer: two networks, counterflow -------------------------------------------
    Part("vafai_zhu_double_layer", "double-layer", 0.050, 0.050,
         2 * _straight_fluid(100, 0.050, 0.200, 20.0), _block(10.0, 20.0, 1.05),
         VAFAI_ZHU, "two independent fluid networks in one block, flowing opposite ways"),
    Part("double_layer_truncated", "double-layer", 0.050, 0.050,
         _straight_fluid(100, 0.050, 0.200, 20.0) + _straight_fluid(100, 0.050, 0.200, 12.0),
         _block(10.0, 20.0, 1.05), PARAMETRIC,
         "upper layer stops short of the lower: an asymmetric pair, and a free end mid-block"),

    # --- oblique fin: interrupted channels --------------------------------------------------
    Part("oblique_fin_100um_27deg", "oblique", 0.100, 0.100,
         _straight_fluid(40, 0.100, 0.400, 10.0) * 1.18, _block(8.0, 10.0, 0.80),
         OBLIQUE, "every fin interruption is a fresh leading edge and a re-entrant corner"),
    Part("oblique_fin_200um_27deg", "oblique", 0.200, 0.200,
         _straight_fluid(20, 0.200, 0.400, 10.0) * 1.18, _block(8.0, 10.0, 0.80),
         OBLIQUE, "the same topology twice as coarse: the control for the 100 um case"),
    Part("oblique_fin_dense", "oblique", 0.100, 0.060,
         _straight_fluid(50, 0.100, 0.400, 10.0) * 1.25, _block(8.0, 10.0, 0.80),
         PARAMETRIC, "fins thinner than the channel, so the SOLID is the thin feature"),

    # --- wavy: curved centreline ------------------------------------------------------------
    Part("wavy_fin250", "wavy", 0.250, 0.250,
         _straight_fluid(16, 0.250, 0.500, 20.0) * 1.08, _block(8.0, 20.0, 0.95),
         WAVY, "a curved wall is where cut-cell staircasing shows up first"),
    Part("wavy_fin125", "wavy", 0.250, 0.125,
         _straight_fluid(21, 0.250, 0.500, 20.0) * 1.08, _block(8.0, 20.0, 0.95),
         WAVY, "same waves, half the fin: the thin solid between two curved walls"),
    Part("wavy_tight_amplitude", "wavy", 0.150, 0.150,
         _straight_fluid(26, 0.150, 0.400, 15.0) * 1.15, _block(8.0, 15.0, 0.85),
         PARAMETRIC, "amplitude comparable to the channel width, so the wave nearly closes it"),

    # --- pin fin: flow around posts ---------------------------------------------------------
    Part("pinfin_circular_100um_inline", "pin-fin", 0.100, 0.100,
         0.60 * _block(6.0, 8.0, 0.30), _block(6.0, 8.0, 0.60),
         PINFIN_PITCH, "far more wetted surface per volume than any slot array"),
    Part("pinfin_diamond_ST2", "pin-fin", 0.100, 0.100,
         0.58 * _block(6.0, 8.0, 0.30), _block(6.0, 8.0, 0.60),
         PINFIN_PITCH, "sharp leading and trailing corners on every post"),
    Part("pinfin_staggered_dense", "pin-fin", 0.070, 0.070,
         0.55 * _block(5.0, 6.0, 0.25), _block(5.0, 6.0, 0.50),
         PINFIN_PITCH, "pitch-to-diameter near 1.7, the tightest the literature reports"),
    Part("pinfin_tall_aspect3", "pin-fin", 0.100, 0.100,
         0.60 * _block(5.0, 6.0, 0.30), _block(5.0, 6.0, 0.65),
         PINFIN_PITCH, "height-to-diameter 3.2, the tall end: slender posts that can be lost"),

    # --- hybrid -----------------------------------------------------------------------------
    Part("hybrid_oblique_pinfin", "hybrid", 0.300, 0.245,
         _straight_fluid(10, 0.300, 0.500, 12.0) * 1.30, _block(8.0, 12.0, 0.95),
         HYBRID, "two cooling mechanisms in one part, each with its own length scale"),

    # --- manifold and plenum ----------------------------------------------------------------
    Part("manifold_3slot_long", "manifold", 0.100, 0.100,
         _straight_fluid(16, 0.100, 0.300, 6.0) + 3 * 0.400 * 0.400 * 3.3,
         _block(3.3, 6.0, 1.05), PARAMETRIC,
         "fewer slots, longer channel runs: connectivity through three openings only"),
    Part("plenum_fed_array", "straight", 0.100, 0.100,
         _straight_fluid(30, 0.100, 0.400, 8.0) + 2 * 0.80 * 0.50 * 6.0,
         _block(6.0, 10.0, 0.95), PARAMETRIC,
         "real inlet and outlet plenums, so the fluid has genuine ports rather than cut faces"),
)


def report() -> int:
    print(f"{'part':<30}{'family':<14}{'binds':>7}{'cell':>9}{'fluid':>10}"
          f"{'conjugate':>12}{'RAM':>9}   where")
    nowhere = []
    for p in sorted(PARTS, key=lambda x: x.conjugate_cells):
        if p.where == "NOWHERE":
            nowhere.append(p.name)
        print(f"  {p.name:<28}{p.family:<14}{p.binding_regime:>7}{p.binding_cell_mm * 1e3:>7.2f}um"
              f"{p.fluid_cells / 1e6:>9.1f}M{p.conjugate_cells / 1e6:>11.1f}M{p.gb:>8.1f}G"
              f"   {p.where}")
    print()
    cited = sum(1 for p in PARTS if p.source.measured)
    print(f"  {len(PARTS)} parts, {cited} from published dimensions, "
          f"{len(PARTS) - cited} parametric and labelled as such")
    for where in ("laptop", "mesh-bench", "big box", "NOWHERE"):
        n = sum(1 for p in PARTS if p.where == where)
        if n:
            print(f"  {n:>2} build on {where}")
    if nowhere:
        print(f"\n  REFUSED, cannot be meshed on anything we have: {', '.join(nowhere)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
