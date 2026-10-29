"""A cold plate with TWO scales of passage, which is the thing the corpus did not have.

WHY THIS PART EXISTS. Of the ten parts in the corpus read on 2026-10-28, every single one came
back single-scaled: `cad/scales.scales_of_field` found one mode in the width histogram and
returned None. The first real customer part is the only two-scale geometry anything here has ever
seen, and it is the one we cannot iterate against - it is 47 GB of mesh and 1.75 hours a run.

So every guard built for two-scale work - the scale split itself, the fine-surface writer, the
two-scale budget, the prismatic-axis measurement, the anisotropic stretch - was tested against
geometry that cannot trigger it, plus one customer file. That is the gap this closes.

WHAT MAKES IT TWO-SCALE, in the customer's own proportions:

    manifold ring and feeders   2.100 mm wide
    microchannel slots          0.098 mm wide
    ratio                       21.4 : 1, which is 4.4 octaves - the splitter needs 2

and the slots are on a 0.545 mm pitch, which is the pitch measured off the customer plate
(0.098 mm of coolant per 0.545 mm, giving the 0.180 void fraction their field actually has).

THE FINE SHARE IS TUNED, not accidental. `scales_of_field` refuses a split where the thin side is
more than MAX_FINE_SHARE of the wetted area, because a part that is mostly thin has one scale and
a misleading histogram. The customer plate sits at 35.9%; the slot length here is chosen to land
near that rather than wherever the drawing happened to put it.

TWO VARIANTS, and the difference is the one that matters for solving. The customer's inlet and
outlet are fitting BORES that stop inside the metal, so their coolant is sealed and the mesher
plans no ports at all - which is why their delivered mesh has exactly one patch. `sealed` has
that, deliberately. `ported` runs the same bores out through the lid so there is something that
can actually be flowed, and the two differ in nothing else.

UNITS ARE MILLIMETRES, as everywhere else in this package.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TwoScalePlateSpec:
    """A plate, a manifold ring, two feeders and a field of slots between them."""

    # the block
    plate_x_mm: float = 24.0
    plate_y_mm: float = 16.0
    plate_z_mm: float = 3.0
    #: metal above and below the coolant, so the channels are embedded rather than open-topped
    lid_mm: float = 0.55

    # the BROAD scale: a ring round the edge and two feeders across it
    manifold_mm: float = 2.100
    inset_mm: float = 1.0
    feeder_gap_mm: float = 4.2          # the clear run the slots cross, i.e. their length

    # the FINE scale, in the customer's own proportions
    slot_mm: float = 0.098
    pitch_mm: float = 0.545
    field_margin_mm: float = 3.0        # how far the slot field stops short of the ring

    # the ports
    bore_mm: float = 1.2
    #: how far a bore stops SHORT of the lid's outer face (sealed), or past it (ported)
    bore_recess_mm: float = 0.40
    bore_overhang_mm: float = 0.05
    ported: bool = False

    @property
    def coolant_z0(self) -> float:
        return self.lid_mm

    @property
    def coolant_z1(self) -> float:
        return self.plate_z_mm - self.lid_mm

    @property
    def channel_height_mm(self) -> float:
        return self.coolant_z1 - self.coolant_z0

    @property
    def narrowest_mm(self) -> float:
        return self.slot_mm

    def describe(self) -> str:
        n = len(self.slot_x_positions())
        return (f"  plate            {self.plate_x_mm} x {self.plate_y_mm} x {self.plate_z_mm} mm\n"
                f"  coolant height   {self.channel_height_mm:.3f} mm "
                f"(z {self.coolant_z0:.2f} .. {self.coolant_z1:.2f})\n"
                f"  manifold         {self.manifold_mm:.3f} mm wide\n"
                f"  slots            {n} at {self.slot_mm:.3f} mm on a {self.pitch_mm:.3f} mm "
                f"pitch, {self.feeder_gap_mm:.2f} mm long\n"
                f"  scale ratio      {self.manifold_mm / self.slot_mm:.1f} : 1\n"
                f"  void fraction    {self.slot_mm / self.pitch_mm:.3f} in the field\n"
                f"  ports            {'through the lid' if self.ported else 'SEALED - bores stop '
                                      f'{self.bore_recess_mm} mm inside the metal'}")

    # -- where things are ------------------------------------------------------------------

    def ring_bars(self) -> list[tuple[float, float, float, float]]:
        """The four manifold bars as (x0, y0, dx, dy)."""
        i, m = self.inset_mm, self.manifold_mm
        x0, x1 = i, self.plate_x_mm - i
        y0, y1 = i, self.plate_y_mm - i
        return [(x0, y0, x1 - x0, m),                 # bottom
                (x0, y1 - m, x1 - x0, m),             # top
                (x0, y0, m, y1 - y0),                 # left
                (x1 - m, y0, m, y1 - y0)]             # right

    def feeder_bars(self) -> list[tuple[float, float, float, float]]:
        """The two bars the slots run between, placed symmetrically about the plate's middle."""
        i, m = self.inset_mm, self.manifold_mm
        x0, x1 = i + m, self.plate_x_mm - i - m
        mid = self.plate_y_mm / 2.0
        half = self.feeder_gap_mm / 2.0
        return [(x0, mid - half - m, x1 - x0, m),
                (x0, mid + half, x1 - x0, m)]

    def slot_x_positions(self) -> list[float]:
        i, m = self.inset_mm, self.manifold_mm
        lo = i + m + self.field_margin_mm
        hi = self.plate_x_mm - i - m - self.field_margin_mm
        out, x = [], lo
        while x + self.slot_mm <= hi:
            out.append(round(x, 6))
            x += self.pitch_mm
        return out

    def slot_y_span(self) -> tuple[float, float]:
        """The slots overlap into both feeders by a tenth of a millimetre, so the fuse is a real
        join rather than two solids touching on a plane - OpenCASCADE is entitled to leave a
        zero-thickness face behind at an exact tangency and the passage count then reads wrong."""
        mid = self.plate_y_mm / 2.0
        half = self.feeder_gap_mm / 2.0
        return mid - half - 0.1, mid + half + 0.1

    def bores(self) -> list[tuple[float, float]]:
        """Where the inlet and outlet drop into the ring, as (x, y) centres - one in the bottom
        bar and one in the top, diagonally opposite so the flow has to cross the field."""
        i, m = self.inset_mm, self.manifold_mm
        return [(self.plate_x_mm * 0.25, i + m / 2.0),
                (self.plate_x_mm * 0.75, self.plate_y_mm - i - m / 2.0)]


def _box(x0, y0, z0, dx, dy, dz):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _fuse(shapes):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    out = shapes[0]
    for s in shapes[1:]:
        out = BRepAlgoAPI_Fuse(out, s).Shape()
    return out


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    return BRepAlgoAPI_Cut(a, b).Shape()


def build(spec: TwoScalePlateSpec | None = None):
    """(solid, fluid, spec). The fluid is fused FIRST and the block cut by it, which is the only
    order that leaves the two sharing one surface exactly."""
    spec = spec or TwoScalePlateSpec()
    z0, dz = spec.coolant_z0, spec.channel_height_mm

    parts = [_box(x, y, z0, dx, dy, dz) for x, y, dx, dy in spec.ring_bars()]
    parts += [_box(x, y, z0, dx, dy, dz) for x, y, dx, dy in spec.feeder_bars()]

    sy0, sy1 = spec.slot_y_span()
    parts += [_box(x, sy0, z0, spec.slot_mm, sy1 - sy0, dz) for x in spec.slot_x_positions()]

    # the bores, up through the lid: short of the outer face when sealed, past it when ported
    top = (spec.plate_z_mm + spec.bore_overhang_mm if spec.ported
           else spec.plate_z_mm - spec.bore_recess_mm)
    b = spec.bore_mm
    parts += [_box(cx - b / 2.0, cy - b / 2.0, z0, b, b, top - z0) for cx, cy in spec.bores()]

    fluid = _fuse(parts)
    block = _box(0.0, 0.0, 0.0, spec.plate_x_mm, spec.plate_y_mm, spec.plate_z_mm)
    solid = _cut(block, fluid)
    return solid, fluid, spec


#: The two the exporter writes.
VARIANTS = (
    ("two_scale_plate_sealed", TwoScalePlateSpec(ported=False)),
    ("two_scale_plate_ported", TwoScalePlateSpec(ported=True)),
)
