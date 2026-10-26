"""What are the pin-fin plate's 526 cap candidates, and why does nothing close?

Its coolant is one chamber with pins standing in it. 526 candidates and no passage, with the
reason "the space inside the parts reaches the outside other than through a flat opening". The
question this answers: are those candidates the PIN ENDS (in which case they are not openings at
all and the fix is to tell them from real ones), or something else?
"""
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")

from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # noqa: E402
from OCP.gp import gp_Trsf  # noqa: E402

from meshpipeline.cad import fluid_volume as fv  # noqa: E402
from meshpipeline.cad.scout import read_cad  # noqa: E402

CAD = Path("/mnt/c/Users/rehaa/OneDrive/Documents/Hexera/semiconductors/semi/cad")


def scaled(path: Path):
    t = gp_Trsf()
    t.SetScaleFactor(0.001)
    return BRepBuilderAPI_Transform(read_cad(path), t, True).Shape()


for name in ("unit_cell_100um", "wavy_250um_16ch", "hotspot_array", "manifold_two_level",
             "corintis_straight_baseline_100um", "murray_ported_4gen",
             "embedded_silicon_D9_10um", "lee_garimella_2005_test1",
             "tuckerman_pease_1981_81F9", "pin_fin_300um_staggered", "murray_branching_4gen"):
    src = CAD / f"{name}_solid.step"
    if not src.is_file():
        continue
    shape = scaled(src)
    lo, hi = fv._real_box(shape)
    diag = math.dist(lo, hi)
    solids = fv._solids(shape)
    caps = fv.candidate_caps(solids, [], diag)
    def on_box(c) -> bool:
        return any(abs(c.centroid[k] - lo[k]) < 1e-9 or abs(c.centroid[k] - hi[k]) < 1e-9
                   for k in range(3))

    out = sum(1 for c in caps if on_box(c))
    x = fv.extract(shape)
    verdict = ("found " + str(len(x.passages)) + "p") if x.found else "NOT FOUND"
    print(name.ljust(34) + ("caps %4d   on box %4d   inside %4d   -> %s"
                            % (len(caps), out, len(caps) - out, verdict)))
