"""The Murray tree with a feed and a drain, so coolant can actually move through it.

WHY A SECOND PART AND NOT AN EDIT. `murray.py` builds the tree sealed inside the metal with a 120 um
wall all round. That is a good test article for a MESHER - branching topology, three-way junctions,
widths from 300 down to 119 um in one part - and it is not one for a solve. A sealed coolant region
has no boundary anywhere that fixes pressure, so the pressure matrix is singular; the first conjugate
solve on it reached the preconditioner and died there, while the energy equation solved happily and
heat conducted into a blob of liquid that never went anywhere. Both parts are worth having, so this
is a second one rather than a change to the first.

HOW IT IS PLUMBED, AND WHY THAT WAY. A branching tree has one trunk and sixteen leaf tips, so a
single inlet and a single outlet cannot both be slots in the same plane:

  THE FEED is the trunk, carried straight out through the block's ymin face. The trunk already starts
  at the origin pointing +y and the wall there is 120 um, so this is a 120 um stub and nothing else
  about the tree changes.

  THE DRAIN is sixteen holes, one above each leaf tip, straight up through the block's zmax face.
  That is how a cold plate is really plumbed - fed from the side, drained through the lid - and it is
  the only arrangement that reaches every tip without a collector slot cutting across the fan.

  A flat collector was the obvious idea and it does not work: the tips sit anywhere from y = 7.46 to
  y = 11.01, so a slot low enough to catch the lowest one swallows most of the tree, and a slot high
  enough not to catch only four. Extending each tip along its own heading does not work either -
  the inner tips are nearly 3 mm from the face they point at, and a stub that long runs straight
  through its neighbours.

The holes are 119 um across through a 120 um wall, which is on purpose: at a coarse resolution they
staircase away and the outlet patch comes back with no faces at all. That is a real failure and the
port gate catches it, which is worth having one part that demonstrates.

UNITS ARE MILLIMETRES, as everywhere else in this package.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from murray import MurraySpec, _centrelines

#: How far a port sticks out past the block face it leaves by.
#:
#: It has to be MORE than nothing and it does not matter how much. The port is fused to the coolant
#: before the block is cut, so an overhang is trimmed off the solid automatically; what it buys is
#: certainty that the coolant surface crosses the face cleanly rather than landing exactly on it,
#: where a float32 STL coordinate can fall either side and the port is sometimes there and
#: sometimes not.
PORT_OVERHANG_MM = 0.050


@dataclass(frozen=True)
class PortedSpec:
    """The tree, plus where its feed and drain go."""

    tree: MurraySpec = MurraySpec()
    #: Block face the trunk is carried out through.
    feed_face: str = "ymin"
    #: Block face the leaf drains go up through.
    drain_face: str = "zmax"
    overhang_mm: float = PORT_OVERHANG_MM

    @property
    def narrowest_mm(self) -> float:
        return self.tree.narrowest_mm

    def describe(self) -> str:
        n_tips = 2 ** self.tree.generations
        return (f"{self.tree.describe()}\n"
                f"  feed: the trunk out through {self.feed_face}, "
                f"{self.tree.trunk_width_mm * 1e3:.0f} um wide\n"
                f"  drain: {n_tips} holes of {self.narrowest_mm * 1e3:.0f} um up through "
                f"{self.drain_face}, one per leaf tip")


def leaf_tips(spec: MurraySpec) -> list[tuple[float, float]]:
    """Where every leaf of the tree ends, in the xy plane."""
    return [end for _start, end, g in _centrelines(spec) if g == spec.generations]


def port_volume_mm3(spec: PortedSpec, wall_mm: float) -> float:
    """What the ports add to the coolant, as an upper bound.

    Upper, because the stub and the holes each overlap the tree they are fused to by the radius of
    whatever they join. A measured volume above this means the fuse produced something larger than
    its own parts.
    """
    t = spec.tree
    feed = t.trunk_width_mm * t.depth_mm * (wall_mm + t.trunk_width_mm / 2 + spec.overhang_mm)
    r = t.width(t.generations) / 2.0
    holes = len(leaf_tips(t)) * math.pi * r * r * (wall_mm + spec.overhang_mm)
    return feed + holes


def build(spec: PortedSpec | None = None):
    """Return (solid_part, fluid_network, spec).

    THE ORDER MATTERS. The block is sized from the bare tree's bounding box, then the ports are
    added, then the block is cut. Sizing it from the ported fluid instead grows the block to contain
    the overhang, which puts the wall back over the ports and seals the part again - the exact bug
    this part exists to avoid, and one that looks like a successful build.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    import murray

    spec = spec or PortedSpec()
    t = spec.tree
    # The bare tree, exactly as murray.py builds it, so the two parts share one tree and cannot
    # drift apart.
    _solid_unported, tree_fluid, _ = murray.build(t)

    bb = Bnd_Box()
    BRepBndLib.Add_s(tree_fluid, bb)
    lo, hi = bb.CornerMin(), bb.CornerMax()
    m = t.wall_mm
    bx0, by0, bz0 = lo.X() - m, lo.Y() - m, lo.Z() - m
    bx1, by1, bz1 = hi.X() + m, hi.Y() + m, hi.Z() + m

    if spec.feed_face != "ymin" or spec.drain_face != "zmax":
        raise ValueError(
            f"this part is plumbed feed=ymin drain=zmax and nothing else is implemented; "
            f"asked for feed={spec.feed_face} drain={spec.drain_face}")

    ports = []
    # The feed: the trunk carried out through ymin. The trunk starts at the origin and already has
    # a half-width disc below it, so the stub runs from past the block face up to the origin.
    half = t.trunk_width_mm / 2.0
    y_start = by0 - spec.overhang_mm
    ports.append(BRepPrimAPI_MakeBox(
        gp_Pnt(-half, y_start, 0.0),
        t.trunk_width_mm, -y_start, t.depth_mm).Shape())

    # The drains: one hole per leaf tip, from inside the channel layer up through zmax.
    r = t.width(t.generations) / 2.0
    for (tx, ty) in leaf_tips(t):
        ports.append(BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(tx, ty, t.depth_mm / 2.0), gp_Dir(0, 0, 1)),
            r, (bz1 + spec.overhang_mm) - t.depth_mm / 2.0).Shape())

    fluid = tree_fluid
    for p in ports:
        fluid = BRepAlgoAPI_Fuse(fluid, p).Shape()

    block = BRepPrimAPI_MakeBox(
        gp_Pnt(bx0, by0, bz0), bx1 - bx0, by1 - by0, bz1 - bz0).Shape()
    solid = BRepAlgoAPI_Cut(block, fluid).Shape()
    return solid, fluid, spec


def analytic_fluid_volume(spec: PortedSpec) -> float:
    """An upper bound on the ported coolant volume: the tree's bound plus the ports'."""
    import murray
    return (murray.analytic_fluid_volume(spec.tree)
            + port_volume_mm3(spec, spec.tree.wall_mm))


__all__ = ["PortedSpec", "PORT_OVERHANG_MM", "leaf_tips", "port_volume_mm3", "build",
           "analytic_fluid_volume", "replace"]
