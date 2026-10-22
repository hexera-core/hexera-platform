"""Microchannel cooler geometries rebuilt from published dimensions, every number traceable to a paper.

WHAT THESE ARE FOR. Hexera needs real test articles at microchannel scale, and there is no public
microchannel cold-plate STEP or STL anywhere. So these are rebuilt from the dimensions the papers state,
with the quote for every number recorded in `specs_raw.json` beside this file.

THE ONE THING TO BE HONEST ABOUT. Corintis's headline geometry, the topology-optimised network, CANNOT
be rebuilt from their paper. It is delivered as a black and white pixel image from a density field, and
the paper gives no channel length, no channel count, no fin thickness and no inlet or outlet dimensions:
only a statistical description of the channel widths (minimum 90.37, mode 134.74, mean 186.12, maximum
370.93 micrometres). What CAN be rebuilt exactly is the baseline the same paper defines and measures its
optimised design against: seven straight-channel cold plates with equal channel and fin widths. That
baseline is Corintis's own published comparison case, so building it is honest; calling it "a Corintis
design" would not be.

For a branching network at the right scale, use `murray.py`, which is a parametric test article built on
Murray's law rather than a replica of anyone's part.

UNITS ARE MILLIMETRES, which is OpenCASCADE's convention and the frame in which a 10 micrometre feature
is 0.01 rather than 1e-5. The same shapes built in metres fail: trimesh reports is_volume False for
cylinders of radius 2e-4 because the end caps collapse.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    paper: str
    url: str
    note: str = ""


@dataclass(frozen=True)
class StraightArray:
    """N straight rectangular channels in a slab. The shared shape of four of the six geometries."""
    name: str
    channel_w_mm: float
    channel_d_mm: float
    channel_l_mm: float
    n_channels: int
    pitch_mm: float
    base_mm: float               # solid below the channel floors
    roof_mm: float               # solid above the channel roofs; 0 means open-topped
    source: Source
    side_wall_mm: float = 0.0    # solid outboard of the first and last channel
    results: str = ""

    @property
    def fluid_volume_mm3(self) -> float:
        """Exact. N rectangular prisms, no overlaps, so this is not a bound but the true value."""
        return self.n_channels * self.channel_w_mm * self.channel_d_mm * self.channel_l_mm

    @property
    def extent_mm(self) -> tuple[float, float, float]:
        x = (self.n_channels - 1) * self.pitch_mm + self.channel_w_mm + 2 * self.side_wall_mm
        return (x, self.channel_l_mm, self.base_mm + self.channel_d_mm + self.roof_mm)

    @property
    def narrowest_mm(self) -> float:
        return min(self.channel_w_mm, self.pitch_mm - self.channel_w_mm)

    def describe(self) -> str:
        x, y, z = self.extent_mm
        return (f"{self.name}\n"
                f"  {self.n_channels} channels, {self.channel_w_mm*1e3:.0f} x "
                f"{self.channel_d_mm*1e3:.0f} um, {self.channel_l_mm:.2f} mm long, "
                f"pitch {self.pitch_mm*1e3:.0f} um\n"
                f"  fin {(self.pitch_mm-self.channel_w_mm)*1e3:.0f} um, base {self.base_mm*1e3:.0f} um, "
                f"roof {self.roof_mm*1e3:.0f} um\n"
                f"  envelope {x:.2f} x {y:.2f} x {z:.3f} mm, narrowest feature "
                f"{self.narrowest_mm*1e3:.1f} um\n"
                f"  source: {self.source.paper}")


def build_straight(spec: StraightArray):
    """Return (solid, fluid). Fluid is a compound of N prisms; solid is the slab minus them."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from OCP.gp import gp_Pnt

    x_ext, y_ext, z_ext = spec.extent_mm
    slab = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), x_ext, y_ext, z_ext).Shape()

    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    solid = slab
    for i in range(spec.n_channels):
        x0 = spec.side_wall_mm + i * spec.pitch_mm
        ch = BRepPrimAPI_MakeBox(gp_Pnt(x0, 0.0, spec.base_mm),
                                 spec.channel_w_mm, spec.channel_l_mm, spec.channel_d_mm).Shape()
        builder.Add(comp, ch)
        solid = BRepAlgoAPI_Cut(solid, ch).Shape()
    return solid, comp


@dataclass(frozen=True)
class PinFinArray:
    """A staggered circular pin-fin array enclosed between two silicon plates."""
    name: str
    pin_dia_mm: float
    pin_h_mm: float
    pitch_mm: float
    array_w_mm: float
    array_l_mm: float
    n_rows: int
    base_mm: float
    cover_mm: float
    source: Source
    results: str = ""

    @property
    def pins_per_row(self) -> int:
        """11 pin-equivalents per row, which the paper's own flow areas confirm.

        A wall row has 12 centres at x = 0, 600, ... 6600 um, but the two at the walls are bisected by
        them, so it contributes 10 whole pins plus two halves = 11. An offset row has 11 whole pins at
        x = 300, 900, ... 6300. 24 rows x 11 = 264 pin-equivalents.
        """
        return int(round(self.array_w_mm / self.pitch_mm))

    @property
    def n_pin_equivalents(self) -> int:
        return self.n_rows * self.pins_per_row

    @property
    def fluid_volume_mm3(self) -> float:
        """Exact: the enclosed box minus the pin volume, using pin EQUIVALENTS so the half pins at the
        side walls are counted once between them."""
        box = self.array_w_mm * self.array_l_mm * self.pin_h_mm
        pins = self.n_pin_equivalents * math.pi * (self.pin_dia_mm / 2.0) ** 2 * self.pin_h_mm
        return box - pins

    @property
    def narrowest_mm(self) -> float:
        """The gap between neighbouring pins, which is the feature a mesher has to resolve."""
        return self.pitch_mm - self.pin_dia_mm

    def describe(self) -> str:
        return (f"{self.name}\n"
                f"  {self.n_pin_equivalents} pin-equivalents, {self.pin_dia_mm*1e3:.0f} um diameter, "
                f"{self.pin_h_mm*1e3:.0f} um tall, pitch {self.pitch_mm*1e3:.0f} um\n"
                f"  array {self.array_w_mm:.2f} x {self.array_l_mm:.2f} mm, {self.n_rows} rows "
                f"staggered\n"
                f"  narrowest gap between pins {self.narrowest_mm*1e3:.0f} um\n"
                f"  source: {self.source.paper}")


def build_pinfin(spec: PinFinArray):
    """Return (solid, fluid). Fluid is the flow box minus the pins; solid is the silicon shell plus pins."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    z0 = spec.base_mm
    flow = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, z0),
                               spec.array_w_mm, spec.array_l_mm, spec.pin_h_mm).Shape()
    r = spec.pin_dia_mm / 2.0
    dy = spec.array_l_mm / spec.n_rows
    fluid = flow
    pins = []
    for row in range(spec.n_rows):
        y = (row + 0.5) * dy
        offset = 0.0 if row % 2 == 0 else spec.pitch_mm / 2.0
        n_pos = spec.pins_per_row + (1 if row % 2 == 0 else 0)
        for i in range(n_pos):
            x = offset + i * spec.pitch_mm
            if x < -1e-9 or x > spec.array_w_mm + 1e-9:
                continue
            cyl = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), r, spec.pin_h_mm).Shape()
            pins.append(cyl)
            fluid = BRepAlgoAPI_Cut(fluid, cyl).Shape()

    total_z = spec.base_mm + spec.pin_h_mm + spec.cover_mm
    shell = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0),
                                spec.array_w_mm, spec.array_l_mm, total_z).Shape()
    solid = BRepAlgoAPI_Cut(shell, fluid).Shape()
    return solid, fluid


# --------------------------------------------------------------------------------------------------
# the catalogue. Every number here appears in specs_raw.json with the sentence it came from.
# --------------------------------------------------------------------------------------------------

CORINTIS_BASELINE_100 = StraightArray(
    name="corintis_straight_baseline_100um",
    channel_w_mm=0.100, channel_d_mm=0.800, channel_l_mm=32.5,
    n_channels=100, pitch_mm=0.200, base_mm=0.300, roof_mm=0.0,
    source=Source(
        paper="Boutsikakis, Soutter, Salazar de Troya, Esposito, Mukasheva, Bouras, van Erp (Corintis), "
              "Glacierware: Hotspot-aware Microfluidic Cooling for High TDP Chips using Topology "
              "Optimization, arXiv:2408.15024",
        url="https://arxiv.org/pdf/2408.15024",
        note="One of the seven straight baselines the paper defines: 'seven canonical designs of "
             "straight microchannels with equal channel and fin widths of 200, 175, 150, 125, 100, 75 "
             "and 50 um'. Base 300 um and channel layer 800 um are quoted. The 20 x 32.5 mm footprint "
             "is the chip. 100 channels at 200 um pitch spans 20 mm exactly. NOT the optimised design, "
             "which cannot be rebuilt from the paper."),
    results="The optimised design beat the best of these baselines by 12.69 per cent. Junction 81.19 C, "
            "max fin 48.99 C, peak velocity 3.71 m/s at 1.09 LPM, 738.66 W with four 400 W/cm2 hotspots.")

TUCKERMAN_PEASE_81F9 = StraightArray(
    name="tuckerman_pease_1981_81F9",
    channel_w_mm=0.050, channel_d_mm=0.302, channel_l_mm=14.0,
    n_channels=200, pitch_mm=0.100, base_mm=0.156, roof_mm=0.0,
    source=Source(
        paper="Tuckerman and Pease, High-performance heat sinking for VLSI, IEEE Electron Device "
              "Letters EDL-2(5), 1981",
        url="https://ieeexplore.ieee.org/document/1482278",
        note="Design C, sample 81F9, the canonical microchannel heat sink and the paper that started "
             "the field. 200 channels at 100 um pitch spans the quoted 20 mm array width. Capped by a "
             "flat Pyrex cover, so roof is 0 here and the cover is a separate body. FULL TEXT WAS NOT "
             "READ by the extractor, so treat these as high-confidence secondary numbers."),
    results="Thermal resistance 0.09 C/W at 600 W/cm2.")

LEE_GARIMELLA_TEST1 = StraightArray(
    name="lee_garimella_2005_test1",
    channel_w_mm=0.194, channel_d_mm=0.884, channel_l_mm=25.4,
    n_channels=10, pitch_mm=2.54, base_mm=0.500, roof_mm=0.0,
    source=Source(
        paper="Lee, Garimella and Liu, Investigation of heat transfer in rectangular microchannels, "
              "Int. J. Heat Mass Transfer 48 (2005) 1688-1704",
        url="http://www2.egr.uh.edu/~dli9/2004_ijhmt-singlephase.pdf",
        note="Test piece 1 of five: ten channels cut into a copper block 25.4 mm square. Channel "
             "194 x 884 um, 25.4 mm long. The fin width is NOT stated, so the 2.54 mm pitch here is "
             "ten channels spread evenly across the quoted 25.4 mm block face, which is an assumption "
             "and is recorded as one. Their validated grid was 20 x 50 x 100 on a quarter domain with "
             "axial ratio 1.08, agreeing with experiment to 5 per cent."),
    results="Average deviation 5 per cent against their own experiments. Grid triplet gave average Nu "
            "8.78, 8.87, 8.97.")

EMBEDDED_SILICON_D9 = StraightArray(
    name="embedded_silicon_D9_10um",
    channel_w_mm=0.010, channel_d_mm=0.075, channel_l_mm=5.0,
    n_channels=200, pitch_mm=0.025, base_mm=0.100, roof_mm=0.200,
    source=Source(
        paper="Silicon embedded microchannel cold plate, design D9 (manifold microchannel study)",
        url="see specs_raw.json for the full citation",
        note="The most extreme case in the set: 10 um channels on a 25 um pitch, which makes the "
             "silicon fin between them 15 um. 200 channels at 25 um pitch spans the quoted 5 mm "
             "footprint exactly. Roof 200 um is quoted. This is the geometry that breaks meshers."),
    results="Hydraulic diameter 17.65 um.")

PIN_FIN = PinFinArray(
    name="pin_fin_300um_staggered",
    pin_dia_mm=0.300, pin_h_mm=0.300, pitch_mm=0.600,
    array_w_mm=6.600, array_l_mm=14.400, n_rows=24,
    base_mm=0.200, cover_mm=0.500,
    source=Source(
        paper="An Experimental Study of Microchannel and Micro-Pin-Fin Based On-Chip Cooling Systems "
              "with Silicon-to-Silicon Direct Bonding",
        url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7583888/",
        note="Circular pins fully enclosed between two silicon plates, four-sided heating. 24 rows at "
             "600 um pitch span 14.4 mm exactly; 264 pin-equivalents is confirmed by the paper's own "
             "minimum flow area. Pins modelled as right circular cylinders with sharp roots, because "
             "the paper states DRIE perpendicularity and gives no draft angle or root fillet."),
    results="Silicon substrate 200 um below, cover 500 um above.")

STRAIGHT_CATALOGUE: tuple[StraightArray, ...] = (
    CORINTIS_BASELINE_100,
    TUCKERMAN_PEASE_81F9,
    LEE_GARIMELLA_TEST1,
    EMBEDDED_SILICON_D9,
)

#: Not built. The two-level manifold microchannel needs four numbers the paper does not state, and
#: inventing them would make a geometry that looks authoritative and is not. Recorded so the gap is
#: visible rather than silently absent.
NOT_BUILT = {
    "manifold_mmc": "Two-level embedded microchannel plus 3D manifold. 34 of 37 dimensions are stated "
                    "and the two levels close arithmetically, but four numbers needed to make the "
                    "intersection watertight are missing. Build it when those are supplied.",
    "corintis_topology_optimised": "Delivered as a black and white pixel image from a density field. "
                                   "No channel length, count, fin thickness or port dimensions are "
                                   "published. Use murray.py for a branching article at this scale.",
}
