"""Build the three advanced geometries as real B-rep solids, and verify each before writing it.

Every part is returned as (solid, fluid): the piece of silicon with the coolant cut out of it, and
the coolant itself. The same pair the conjugate path expects.

UNITS ARE MILLIMETRES, OpenCASCADE's own convention and the frame in which a 60 um feature is 0.06
rather than 6e-5. The same shape built in metres fails: trimesh reports is_volume False for a
cylinder of radius 2e-4 because the end caps collapse.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from advanced import HotspotArraySpec, ManifoldSpec, UnitCellSpec  # noqa: E402


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


# #
# 1. unit cell
# #
def build_unit_cell(spec: UnitCellSpec | None = None):
    """One channel with half a fin either side: the periodic unit of a parallel array.

    The half-fins are what make it periodic. Cutting on the fin's centreline means the unit tiles
    exactly, so a hundred of these side by side are the hundred-channel chip - which is why this
    is the honest way to simulate one.
    """
    s = spec or UnitCellSpec()
    w = s.pitch_mm                       # one channel plus one fin: the tiling width
    h = s.base_mm + s.channel_h_mm + s.roof_mm
    block = _box(0, 0, 0, w, s.length_mm, h)
    # The channel sits centred in the width, on top of the base.
    x0 = (w - s.channel_w_mm) / 2.0
    fluid = _box(x0, 0, s.base_mm, s.channel_w_mm, s.length_mm, s.channel_h_mm)
    return _cut(block, fluid), fluid, s


# #
# 2. hotspot array
# #
def build_hotspot_array(spec: HotspotArraySpec | None = None):
    """Channels that narrow over the hot region.

    Each channel is three boxes in a row - wide, narrow, wide - fused into one passage. The narrow
    middle is centred on the hotspot. Fusing rather than drawing one tapered solid keeps the step
    sharp, which is deliberate: a sharp change in width is harder for a mesher than a taper and is
    what a photolithographic channel actually looks like.
    """
    s = spec or HotspotArraySpec()
    width, length = s.footprint_mm
    h = s.base_mm + s.channel_h_mm + s.roof_mm
    block = _box(0, 0, 0, width, length, h)

    hot_len = length * s.hotspot_span_frac
    hot_y0 = length * s.hotspot_centre_frac - hot_len / 2.0

    parts = []
    for i in range(s.n_channels):
        xc = s.fin_w_mm + i * (s.channel_w_mm + s.fin_w_mm) + s.channel_w_mm / 2.0
        for y0, ylen, wch in ((0.0, hot_y0, s.channel_w_mm),
                              (hot_y0, hot_len, s.hotspot_w_mm),
                              (hot_y0 + hot_len, length - hot_y0 - hot_len, s.channel_w_mm)):
            if ylen <= 0:
                continue
            parts.append(_box(xc - wch / 2.0, y0, s.base_mm, wch, ylen, s.channel_h_mm))
    fluid = _fuse(parts)
    return _cut(block, fluid), fluid, s


# #
# 3. two-level manifold
# #
def build_manifold(spec: ManifoldSpec | None = None):
    """A distribution manifold crossing over microchannels, joined only at the slots.

    THE HARD PART, and the reason this geometry is here. The fluid is one network across two
    levels and the ONLY thing joining them is the slot openings. A cell too coarse at a slot
    severs the network, and the two levels then mesh as separate regions - the connectivity
    failure conjugate.verify gates on, produced on demand.
    """
    s = spec or ManifoldSpec()
    width = s.n_channels * (s.channel_w_mm + s.fin_w_mm) + s.fin_w_mm
    z_chan = s.base_mm
    z_man = z_chan + s.channel_h_mm
    h = z_man + s.manifold_h_mm + s.wall_mm
    block = _box(0, 0, 0, width, s.length_mm, h)

    parts = []
    # lower level: the microchannels, running the full length
    for i in range(s.n_channels):
        x0 = s.fin_w_mm + i * (s.channel_w_mm + s.fin_w_mm)
        parts.append(_box(x0, 0, z_chan, s.channel_w_mm, s.length_mm, s.channel_h_mm))
    # upper level: slots running ACROSS the channels, each spanning the full width
    n = s.n_manifold_slots
    for k in range(n):
        yc = (k + 0.5) * s.length_mm / n
        parts.append(_box(0, yc - s.slot_w_mm / 2.0, z_man, width, s.slot_w_mm, s.manifold_h_mm))
    fluid = _fuse(parts)
    return _cut(block, fluid), fluid, s


BUILDERS = (("unit_cell_100um", build_unit_cell),
            ("hotspot_array", build_hotspot_array),
            ("manifold_two_level", build_manifold))
