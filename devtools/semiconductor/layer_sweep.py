#!/usr/bin/env python3
"""A/B the LAYER PASS's mesh-quality dictionary on a small real mesh, in the real image.

WHAT IS BEING TESTED, and why it is a dictionary and not a layer setting.

The delivered 155 M plate came back with every STANDARD checkMesh test passing - aspect ratio
87.8, non-orthogonality 64.3, skewness 3.35, face flatness, cell volumes, face areas, all OK - and
failed five of the EXTENDED (`-allGeometry`) ones:

    face tets with negative volume decomposition      27 faces
    cell determinant                                  minimum 0, 3,146 cells under 0.001
    concave cells (face planes)                       345,516 cells
    face interpolation weight                         minimum 0.0107, 4,390 faces under 0.05
    face volume ratio                                 minimum 0.0065, 56 faces under 0.01

Every one of those numbers is BELOW A FLOOR THE LAYER DICT ITSELF SETS: `minDeterminant 0.001`,
`minFaceWeight 0.02`, `minVolRatio 0.01`. The mover is not supposed to be able to produce them,
and it reported only 133 illegal faces - so either its floors were not being applied, or they were
not being applied the whole time.

THE SUSPECT. `addLayersControls` runs `nLayerIter 10` with `nRelaxedIter 3`: after the third
iteration the mover swaps `meshQualityControls` for its `relaxed` sub-dictionary. Ours is

    relaxed { maxNonOrtho 75; }

one line, as every OpenFOAM tutorial writes it. If the relaxed sub-dict is read as a dictionary in
its own right - each key looked up with a default when absent - then for iterations 3 through 9
there is NO minVol, NO minDeterminant, NO minFaceWeight, NO minVolRatio and NO minTetQuality, and
thinning a layer to a hairline is unopposed. The delivered run's own coverage trace says the last
seven iterations did almost nothing but that:

    99.9614  99.708  99.6876  99.6827 | 99.6794  99.6763  99.6734  99.6711  99.6693  99.6675
                                      ^ relaxed from here

0.015% of coverage recovered across seven iterations, for most of 1,487 s. It was not removing
layers. Thinning is the mover's other repair.

I could not read OpenFOAM's source to settle it - develop.openfoam.com wants a login and the
doxygen page 404s - so this settles it by measurement instead, which is the better evidence: if
the strict floors are still in force during relaxed iterations then spelling them out in `relaxed`
changes NOTHING, and V1 comes back identical to V0. Any difference at all is the proof.

HOW IT RUNS. The product's own chain, once per variant, through `conjugate.build`. The only thing
that differs between variants is the TEXT of `system/layers/snappyHexMeshDict`, patched by
wrapping the renderer - so nothing here can diverge from what the product would do with the same
dict. Each variant gets its own workspace and its own logs, kept for reading afterwards.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/work/devtools/semiconductor")

import parts as P  # noqa: E402,F401
import semi  # noqa: E402

from meshpipeline.engines.cfmesh_conjugate import conjugate as C  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate import layers as LY  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--part", default="two_scale_plate_ported")
ap.add_argument("--no-ports", action="store_true", dest="no_ports",
                help="both of this plate's ports are on the lid, so the engine refuses "
                     "to declare them; the delivered run was made this way")
ap.add_argument("--across", type=int, default=4)
ap.add_argument("--levels", type=int, default=3)
ap.add_argument("--ranks", type=int, default=4)
ap.add_argument("--layers", type=int, default=3)
ap.add_argument("--only", default="", help="comma-separated variant names to run")
ap.add_argument("--reuse", action="store_true",
                help="parse a variant whose workspace already holds a checkMesh log "
                     "instead of building it again")
a = ap.parse_args()


# --------------------------------------------------------------------------------------------
# THE VARIANTS. Each is a function from the shipped dict text to the text to actually use.
# --------------------------------------------------------------------------------------------

#: The strict block the shipped dict writes, repeated with ONLY the three angular/shape limits
#: loosened. The floors that keep a cell from collapsing - volume, determinant, face weight,
#: volume ratio, twist, tet quality - are carried over UNCHANGED, which is the whole point: a
#: relaxed iteration should be allowed to accept a worse-SHAPED cell, never a thinner one.
FULL_RELAXED = "\n".join([
    "    relaxed",
    "    {",
    "        maxNonOrtho          75;",
    "        maxBoundarySkewness  25;",
    "        maxInternalSkewness   6;",
    "        maxConcave           85;",
    "        minVol               __MINVOL__;",
    "        minTetQuality        __MINTET__;",
    "        minArea              -1;",
    "        minTwist             0.02;",
    "        minDeterminant       0.001;",
    "        minFaceWeight        0.02;",
    "        minVolRatio          0.01;",
    "        minTriangleTwist     -1;",
    "    }",
])

SHIPPED_RELAXED = "    relaxed { maxNonOrtho 75; }"


def _relaxed(min_vol: float) -> str:
    """FULL_RELAXED with its two values in. NOT `str.format`: the template is an OpenFOAM block,
    so it carries literal braces that `format` reads as field names and refuses."""
    return (FULL_RELAXED
            .replace("__MINVOL__", f"{float(min_vol):g}")
            .replace("__MINTET__", f"{LY.MIN_TET_QUALITY:g}"))


def _sub1(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"ABORT: {what}: matched {text.count(old)} of {old[:50]!r}")
    return text.replace(old, new, 1)


def _scaled_min_vol(interface_cell_m: float) -> float:
    """A face-pyramid floor that scales with the mesh instead of being a constant.

    `minVol` is an ABSOLUTE volume and OpenFOAM ships it at 1e-13, which on a 25 um mesh condemns
    every face of a perfect mesh - that is why this code carries 1e-20 instead. But 1e-20 is not a
    smaller floor, it is NO floor: the thinnest legitimate cell in this stack has a face pyramid
    of about 4.4e-16 m3, so 1e-20 is four orders of magnitude below anything real and stops
    nothing. The delivered mesh's smallest cell was 3.74e-17 m3 - a layer 0.059 um thick in a
    25 um cell - and nothing in the dictionary objected.

    A thousandth of the interface cell's own volume puts the floor about 30x under the thinnest
    legitimate pyramid and about 170x over a hairline, and it reproduces the shipped behaviour
    where the shipped behaviour was right: on the 22.0035 um blockMesh of the ablation it is
    1.07e-17 against a real pyramid of 1.78e-15, so layers still grow.
    """
    return 1.0e-3 * float(interface_cell_m) ** 3


def v0(text: str, cell: float) -> str:
    return text


def v1(text: str, cell: float) -> str:
    return _sub1(text, SHIPPED_RELAXED, _relaxed(LY.MIN_VOL), "v1 relaxed")


def v2(text: str, cell: float) -> str:
    mv = _scaled_min_vol(cell)
    text = _sub1(text, SHIPPED_RELAXED, _relaxed(mv), "v2 relaxed")
    return _sub1(text, f"    minVol               {LY.MIN_VOL:g};",
                 f"    minVol               {mv:g};", "v2 strict minVol")


def v3(text: str, cell: float) -> str:
    text = v2(text, cell)
    return _sub1(text, "    nBufferCellsNoExtrude       0;",
                 "    nBufferCellsNoExtrude       1;", "v3 buffer")


def v4(text: str, cell: float) -> str:
    """Never relax: the strict dictionary stands for all ten iterations."""
    return _sub1(text, "    nRelaxedIter                3;",
                 "    nRelaxedIter               10;", "v4 nRelaxedIter")


def v5(text: str, cell: float) -> str:
    """Stop where the delivered run stopped improving, before the relaxed phase begins."""
    return _sub1(text, "    nLayerIter                 10;",
                 "    nLayerIter                  3;", "v5 nLayerIter")


def v6(text: str, cell: float) -> str:
    """The dictionary fix and the short run together."""
    return v5(v2(text, cell), cell)


def v7(text: str, cell: float) -> str:
    """Raise the floor the mover removes a layer at, instead of keeping a thinned one."""
    import re as _re
    m = _re.search(r"    minThickness            ([-\d.e+]+);", text)
    if not m:
        raise SystemExit("ABORT v7: no minThickness line")
    stack = LY.stack_thickness_m(first_layer_m=LY.FIRST_LAYER_FRACTION,
                                 expansion=LY.EXPANSION_RATIO,
                                 n_layers=int(a.layers)) * float(cell)
    return _sub1(text, m.group(0), f"    minThickness            {0.5 * stack:.6g};", "v7 minThickness")


def v8(text: str, cell: float) -> str:
    """The dictionary fix and the thickness floor together."""
    return v7(v2(text, cell), cell)


def v9(text: str, cell: float) -> str:
    """The two levers that each worked, together: stop gradually AND remove rather than thin."""
    # The committed product already writes nBufferCellsNoExtrude 1, so the only thing left to
    # add on top of it is the thickness floor.
    return v7(text, cell)


def _one(line_now: str, line_new: str, what: str):
    """One knob of `addLayersControls`, changed from what the product writes today."""
    def go(text, cell):
        return _sub1(text, line_now, line_new, what)
    return go


# THE EXTRUSION DIRECTION. On a staircase every neighbouring face is at 90 degrees, so with no
# smoothing each prism is extruded along its own face normal and two prisms at a step point
# straight into one another - which is a concave cell by the face-plane test. Smoothing aims the
# extrusion diagonally across the step instead, which the module's own comment says is wrong, and
# the measurement behind that comment did not count concave cells. Both are here.
c_smoothsurf2 = _one("    nSmoothSurfaceNormals       0;",
                     "    nSmoothSurfaceNormals       2;", "nSmoothSurfaceNormals 2")
c_smoothsurf5 = _one("    nSmoothSurfaceNormals       0;",
                     "    nSmoothSurfaceNormals       5;", "nSmoothSurfaceNormals 5")
c_smoothnorm2 = _one("    nSmoothNormals              0;",
                     "    nSmoothNormals              2;", "nSmoothNormals 2")

# HOW THICK A LAYER MAY BE FOR THE ROOM IT IS IN. A folded prism at a step is a layer too thick
# for its local geometry, so these two are the direct expression of the fault.
c_medial40 = _one("    maxThicknessToMedialRatio 0.6;",
                  "    maxThicknessToMedialRatio 0.4;", "maxThicknessToMedialRatio 0.4")
c_medial30 = _one("    maxThicknessToMedialRatio 0.6;",
                  "    maxThicknessToMedialRatio 0.3;", "maxThicknessToMedialRatio 0.3")
c_facethick03 = _one("    maxFaceThicknessRatio     0.5;",
                     "    maxFaceThicknessRatio     0.3;", "maxFaceThicknessRatio 0.3")

# WHERE TO STOP EXTRUDING. `featureAngle 180` means never treat an edge as a feature, so the mover
# is made to carry layers around every 90-degree step. Lowering it lets it stop there instead,
# which should cost coverage - that is the trade this measures.
c_feature120 = _one("    featureAngle              180;",
                    "    featureAngle              120;", "featureAngle 120")
c_feature90 = _one("    featureAngle              180;",
                   "    featureAngle               90;", "featureAngle 90")

# AND HOW ABRUPTLY. `nGrow` widens the unextruded zone around a face that was not extruded.
c_grow1 = _one("    nGrow 0;", "    nGrow 1;", "nGrow 1")
# `slipFeatureAngle` lets a layer point slide along a feature rather than being pinned to it.
c_slip60 = _one("    slipFeatureAngle           30;",
                "    slipFeatureAngle           60;", "slipFeatureAngle 60")
# More inner relaxation per outer iteration: the mover's own repair, given more room to work.
c_relax10 = _one("    nRelaxIter                  5;",
                 "    nRelaxIter                 10;", "nRelaxIter 10")


def _smooth(n: int):
    """`nSmoothSurfaceNormals`, the one knob round one found."""
    return _one("    nSmoothSurfaceNormals       0;",
                f"    nSmoothSurfaceNormals       {int(n)};", f"nSmoothSurfaceNormals {n}")


def _smooth_and_floor(n: int, frac: float):
    """Smoothing with a thickness floor under it.

    Smoothing cures the folded prisms and manufactures thin ones, which is what the volume-ratio
    faces are; the floor is what removes a layer rather than keeping it as a hairline. They act on
    different ends of the same cell, so this asks whether both can hold at once.
    """
    def go(text, cell):
        return _floor(frac)(_smooth(n)(text, cell), cell)
    return go


def _smooth_and(n: int, line_now: str, line_new: str, what: str):
    def go(text, cell):
        return _one(line_now, line_new, what)(_smooth(n)(text, cell), cell)
    return go


def _fa120(text, cell):
    """Let the mover stop at the sharpest steps instead of carrying a layer round them."""
    return _one("    featureAngle              180;",
                "    featureAngle              120;", "featureAngle 120")(text, cell)


def _combo(n: int | None = None, fa: bool = False, frac: float | None = None):
    """Smoothing, the feature angle and the thickness floor, in any combination."""
    def go(text, cell):
        if n is not None:
            text = _smooth(n)(text, cell)
        if fa:
            text = _fa120(text, cell)
        if frac is not None:
            text = _floor(frac)(text, cell)
        return text
    return go


#: The dictionary the product renders TODAY, which is what these substitute against.
_NOW_FA = "    featureAngle              120;"
_NOW_SS = "    nSmoothSurfaceNormals       5;"


def _now(line_now: str, line_new: str, what: str):
    def go(text, cell):
        return _sub1(text, line_now, line_new, what)
    return go


def _pre(text, cell):
    """Everything run 4 had: no feature angle, no smoothing, seven tenths of the stack."""
    text = _sub1(text, _NOW_FA, "    featureAngle              180;", "pre featureAngle")
    text = _sub1(text, _NOW_SS, "    nSmoothSurfaceNormals       0;", "pre smoothing")
    return _floor(0.70)(text, cell)


def _floor(frac):
    """The product carries 0.5 of the stack; this asks what a different fraction would do."""
    def go(text, cell):
        import re as _re
        m = _re.search(r"    minThickness            ([-0-9.e+]+);", text)
        g = LY.geometry_for(cell, n_layers=int(a.layers))
        stack = LY.stack_thickness_m(first_layer_m=g["first_layer_m"],
                                     expansion=g["expansion"], n_layers=int(a.layers))
        return _sub1(text, m.group(0),
                     f"    minThickness            {frac * stack:.6g};", f"floor {frac}")
    return go


VARIANTS = {
    "v0_shipped": (v0, "as shipped: relaxed { maxNonOrtho 75; }, minVol 1e-20"),
    "v1_fullrelaxed": (v1, "relaxed repeats every floor; only angles/skew loosened"),
    "v2_minvol": (v2, "v1 + minVol scaled to the interface cell (1e-3 of its volume)"),
    "v3_buffer": (v3, "v2 + nBufferCellsNoExtrude 1"),
    "v4_norelax": (v4, "nRelaxedIter 10: the strict dict stands for every iteration"),
    "v5_shortrun": (v5, "nLayerIter 3: stop before the relaxed phase begins"),
    "v6_both": (v6, "v2 + nLayerIter 3"),
    "v7_minthick": (v7, "minThickness = half the stack: thin layers removed, not kept"),
    "v8_dict_and_thick": (v8, "v2 + v7"),
    "v9_buffer_and_thick": (v9, "v3 + v7: the two that each worked"),
    "f30": (_floor(0.30), "floor 0.30 of the stack"),
    "f40": (_floor(0.40), "floor 0.40 of the stack"),
    "f60": (_floor(0.60), "floor 0.60 of the stack"),
    "f70": (_floor(0.70), "floor 0.70 of the stack"),
    "f80": (_floor(0.80), "floor 0.80 of the stack"),
    # aimed at the concave cells, which run 4 showed are the LAYER PASS and not the staircase
    "c_smoothsurf2": (c_smoothsurf2, "nSmoothSurfaceNormals 2 (the module says never; measure it)"),
    "c_smoothsurf5": (c_smoothsurf5, "nSmoothSurfaceNormals 5"),
    "c_smoothnorm2": (c_smoothnorm2, "nSmoothNormals 2"),
    "c_medial40": (c_medial40, "maxThicknessToMedialRatio 0.4"),
    "c_medial30": (c_medial30, "maxThicknessToMedialRatio 0.3"),
    "c_facethick03": (c_facethick03, "maxFaceThicknessRatio 0.3"),
    "c_feature120": (c_feature120, "featureAngle 120: stop at the sharpest steps"),
    "c_feature90": (c_feature90, "featureAngle 90: stop at every step"),
    "c_grow1": (c_grow1, "nGrow 1: widen the unextruded zone"),
    "c_slip60": (c_slip60, "slipFeatureAngle 60: let layer points slide along a feature"),
    "c_relax10": (c_relax10, "nRelaxIter 10: more inner relaxation per outer iteration"),
    # round two: where the smoothing curve stops paying, and what pays off its volume-ratio faces
    "s3": (_smooth(3), "nSmoothSurfaceNormals 3"),
    "s8": (_smooth(8), "nSmoothSurfaceNormals 8"),
    "s10": (_smooth(10), "nSmoothSurfaceNormals 10"),
    "s20": (_smooth(20), "nSmoothSurfaceNormals 20"),
    "s5_f80": (_smooth_and_floor(5, 0.80), "nSmoothSurfaceNormals 5 + floor 0.80 of the stack"),
    "s5_f90": (_smooth_and_floor(5, 0.90), "nSmoothSurfaceNormals 5 + floor 0.90 of the stack"),
    "s10_f80": (_smooth_and_floor(10, 0.80), "nSmoothSurfaceNormals 10 + floor 0.80"),
    "s5_norelax": (_smooth_and(5, "    nRelaxedIter                3;",
                               "    nRelaxedIter               10;", "nRelaxedIter 10"),
                   "nSmoothSurfaceNormals 5, and never swap in the relaxed dictionary"),
    "s5_grow1": (_smooth_and(5, "    nGrow 0;", "    nGrow 1;", "nGrow 1"),
                 "nSmoothSurfaceNormals 5 + nGrow 1"),
    # the two levers together, and with the floor that cured volume-ratio faces once before
    "fa120_s5": (_combo(n=5, fa=True), "featureAngle 120 + nSmoothSurfaceNormals 5"),
    "fa120_s10": (_combo(n=10, fa=True), "featureAngle 120 + nSmoothSurfaceNormals 10"),
    "fa120_s5_f80": (_combo(n=5, fa=True, frac=0.80),
                     "featureAngle 120 + nSmoothSurfaceNormals 5 + floor 0.80"),
    "fa120_f80": (_combo(fa=True, frac=0.80), "featureAngle 120 + floor 0.80"),
    "fa150": (_one("    featureAngle              180;",
                   "    featureAngle              150;", "featureAngle 150"),
              "featureAngle 150: half way, for the coverage"),
    # FOUR-ACROSS: the baseline is the new dictionary, so run 4's is a variant of its own, and
    # each knob is isolated against the pair to see how the gains scale with the cell.
    "pre": (_pre, "run 4's dictionary: featureAngle 180, smoothing 0, floor 0.70"),
    "now_s0": (_now(_NOW_SS, "    nSmoothSurfaceNormals       0;", "smoothing 0"),
               "the shipped dict with the smoothing taken back out"),
    "now_fa180": (_now(_NOW_FA, "    featureAngle              180;", "featureAngle 180"),
                  "the shipped dict with the feature angle taken back out"),
    "now_s8": (_now(_NOW_SS, "    nSmoothSurfaceNormals       8;", "smoothing 8"),
               "more smoothing, which a finer cell may want"),
    "now_f70": (_floor(0.70), "the shipped dict at the 2-across concave minimum's floor"),
}


# --------------------------------------------------------------------------------------------
# WHAT checkMesh SAID, read off its own log. Everything optional: a missing number is reported
# as a missing number, never as a zero.
# --------------------------------------------------------------------------------------------

#: A number that ENDS AT ITS EXPONENT. `[-\d.e+]+` also matches the full stop that closes
#: "Min volume = 1.02393e-15." and hands float() a string it refuses.
NUM = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"

PATTERNS = {
    "aspect_max": r"Max aspect ratio = " + NUM,
    "nonortho_max": r"Mesh non-orthogonality Max: " + NUM,
    "skew_max": r"Max skewness = " + NUM,
    "vol_min": r"Min volume = " + NUM,
    "det_min": r"Cell determinant \(wellposedness\) : minimum: " + NUM,
    "weight_min": r"Face interpolation weight : minimum: " + NUM,
    "volratio_min": r"Face volume ratio : minimum: " + NUM,
}
COUNTS = {
    "tet_faces": r"([\d]+) faces with low quality or negative volume decomposition tets",
    "det_cells": r"Cells with small determinant \([^)]*\) found, number of cells: ([\d]+)",
    "concave_cells": r"Concave cells \([^)]*\) found, number of cells: ([\d]+)",
    "weight_faces": r"Faces with small interpolation weight \([^)]*\) found, number of faces: ([\d]+)",
    "volratio_faces": r"Faces with small volume ratio \([^)]*\) found, number of faces: ([\d]+)",
    "regions": r"Number of regions: ([\d]+)",
}


def _layers_from_log(ws: Path) -> dict:
    """The layer report, read back off the stage's own log in a reused workspace."""
    log = ws / "snappyHexMesh.log"
    if not log.is_file():
        return {}
    try:
        return LY.read_layer_log(log.read_text(errors="replace"))
    except Exception:
        return {}


def _stage_seconds(ws: Path) -> float:
    """How long the layer stage took, if the workspace kept a report saying so."""
    for cand in ("conjugate_report.json", "report.json"):
        p = ws / cand
        if p.is_file():
            try:
                return float((json.loads(p.read_text()).get("seconds") or {})
                             .get("snappyHexMesh") or 0.0)
            except Exception:
                return 0.0
    return 0.0


def read_check(path: Path) -> dict:
    if not path.is_file():
        return {"present": False}
    t = path.read_text(errors="replace")
    out = {"present": True}
    for k, pat in PATTERNS.items():
        m = re.search(pat, t)
        out[k] = float(m.group(1)) if m else None
    for k, pat in COUNTS.items():
        m = re.search(pat, t)
        out[k] = int(m.group(1)) if m else 0
    m = re.search(r"Failed (\d+) mesh checks", t)
    out["failed"] = int(m.group(1)) if m else (0 if "Mesh OK" in t else None)
    out["mesh_ok"] = "Mesh OK" in t
    return out


# --------------------------------------------------------------------------------------------

part = semi.pick(semi.CAD, a.part)
lo, hi, background, open_, ports, cells, nbytes = semi.plan(
    part, a.across, a.levels, None, None, None, bool(a.no_ports))
cell = LY.interface_cell_m(float(background), int(a.levels))

print(f"part          {part.name}")
print(f"across        {a.across}  levels {a.levels}  ranks {a.ranks}  layers {a.layers}")
print(f"background    {background * 1e6:.4f} um -> interface cell {cell * 1e6:.4f} um")
print(f"estimate      {cells:,} cells before layers")
print(f"product minVol {LY.min_vol_for(cell):g}   buffer {LY.BUFFER_CELLS_NO_EXTRUDE}")
print()

want = [w.strip() for w in a.only.split(",") if w.strip()] or list(VARIANTS)
results = {}

_real_render = LY.render_layers_dict

for name in want:
    fn, blurb = VARIANTS[name]
    print("=" * 94)
    print(f"{name}   {blurb}")
    print("=" * 94, flush=True)

    def _wrapped(**kw):
        return fn(_real_render(**kw), float(kw["interface_cell_m"]))

    LY.render_layers_dict = _wrapped
    C.LY = LY  # conjugate imports it lazily by module, but be explicit

    ws = Path("/work/runs") / f"sweep-{a.part}-{a.across}a-{a.ranks}r-L{a.layers}-{name}"
    done = (ws / "checkMesh_fluid.log").is_file()
    if a.reuse and done:
        print("  (reusing the mesh already in this workspace)", flush=True)
        row = {"variant": name, "blurb": blurb, "reused": True}
        row["layers"] = _layers_from_log(ws)
        row["check_fluid"] = read_check(ws / "checkMesh_fluid.log")
        row["check_solid"] = read_check(ws / "checkMesh_solid.log")
        dp = ws / "system" / "layers" / "snappyHexMeshDict"
        row["dict"] = dp.read_text() if dp.is_file() else ""
        row["seconds"] = {"snappyHexMesh": _stage_seconds(ws)}
        results[name] = row
        cf = row["check_fluid"]
        print(f"  fluid failed={cf.get('failed')}  det_min={cf.get('det_min')}  "
              f"det_cells={cf.get('det_cells')}  concave={cf.get('concave_cells')}  "
              f"weight_min={cf.get('weight_min')} ({cf.get('weight_faces')})  "
              f"volratio_min={cf.get('volratio_min')} ({cf.get('volratio_faces')})  "
              f"tetfaces={cf.get('tet_faces')}  volmin={cf.get('vol_min')}", flush=True)
        lay = row.get("layers") or {}
        print(f"  coverage {lay.get('coverage_pct')}  illegal {lay.get('illegal_faces')}  "
              f"trace {lay.get('coverage_by_iteration')}\n", flush=True)
        continue
    if ws.exists():
        shutil.rmtree(ws)
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    semi.write_case_skeleton(ws)
    (ws / "constant" / "triSurface" / "block.stl").write_text(C.block_surface_stl(lo, hi))
    shutil.copyfile(part.fluid_stl, ws / "constant" / "triSurface" / "fluid.stl")

    row = {"variant": name, "blurb": blurb}
    t0 = time.monotonic()
    try:
        rep = C.build(ws, background_cell_m=background, levels=a.levels,
                      expect_fluid_volume_m3=part.fluid_volume_m3,
                      expect_fluid_pieces=part.passages,
                      volume_tol=semi.volume_tolerance(a.across, None)[0],
                      ports=ports, narrowest_m=part.narrowest_m,
                      ranks=int(a.ranks), layers=int(a.layers), merge=False)
        row["wall_s"] = round(time.monotonic() - t0, 1)
        row["seconds"] = {k: round(v, 1) for k, v in (rep.seconds or {}).items()}
        row["fluid_cells"] = rep.fluid.cells
        row["solid_cells"] = rep.solid.cells
        row["fluid_ok"] = rep.fluid.mesh_ok
        row["solid_ok"] = rep.solid.mesh_ok
        row["problems"] = rep.problems
        row["layers"] = rep.layers
    except Exception as exc:  # the chain refusing IS a result
        row["wall_s"] = round(time.monotonic() - t0, 1)
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()[-1500:]
        print(row["error"], flush=True)
    finally:
        LY.render_layers_dict = _real_render

    row["check_fluid"] = read_check(ws / "checkMesh_fluid.log")
    row["check_solid"] = read_check(ws / "checkMesh_solid.log")
    dict_path = ws / "system" / "layers" / "snappyHexMeshDict"
    row["dict"] = dict_path.read_text() if dict_path.is_file() else ""
    results[name] = row

    cf = row["check_fluid"]
    print(f"  wall {row.get('wall_s')} s   layer pass "
          f"{(row.get('seconds') or {}).get('snappyHexMesh')} s", flush=True)
    if cf.get("present"):
        print(f"  fluid failed={cf['failed']}  det_min={cf['det_min']}  "
              f"det_cells={cf['det_cells']}  concave={cf['concave_cells']}  "
              f"weight_min={cf['weight_min']} ({cf['weight_faces']})  "
              f"volratio_min={cf['volratio_min']} ({cf['volratio_faces']})  "
              f"tetfaces={cf['tet_faces']}  volmin={cf['vol_min']}", flush=True)
    lay = row.get("layers") or {}
    if lay:
        print(f"  coverage {lay.get('coverage_pct')}  illegal {lay.get('illegal_faces')}  "
              f"iters {lay.get('iterations')}  trace {lay.get('coverage_by_iteration')}",
              flush=True)
    print(flush=True)

out = Path("/work/runs") / f"layer_sweep-{a.part}-{a.across}a-L{a.layers}.json"
out.write_text(json.dumps(results, indent=2, default=str))

print("=" * 128)
print("THE TABLE")
print("=" * 128)
hdr = (f"{'variant':<16}{'lay s':>7}{'cover%':>9}{'illegal':>9}{'failed':>8}"
       f"{'det_min':>10}{'det_n':>8}{'concave':>10}{'wt_min':>10}{'wt_n':>7}"
       f"{'vr_min':>10}{'vr_n':>6}{'tetf':>6}{'volmin':>11}")
print(hdr)
print("-" * len(hdr))
for name in want:
    r = results[name]
    c = r["check_fluid"]
    lay = r.get("layers") or {}
    if r.get("error"):
        print(f"{name:<16}  REFUSED: {r['error'][:100]}")
        continue
    def _n(x, w=10, nd=5):
        return ("-" if x is None else f"{x:.{nd}g}").rjust(w)
    _lay_s = float((r.get('seconds') or {}).get('snappyHexMesh') or 0)
    print(f"{name:<16}{_lay_s:>7.0f}"
          f"{lay.get('coverage_pct', 0):>9.3f}{lay.get('illegal_faces', 0):>9,}"
          f"{str(c.get('failed')):>8}{_n(c.get('det_min'))}{c.get('det_cells', 0):>8,}"
          f"{c.get('concave_cells', 0):>10,}{_n(c.get('weight_min'))}"
          f"{c.get('weight_faces', 0):>7,}{_n(c.get('volratio_min'))}"
          f"{c.get('volratio_faces', 0):>6,}{c.get('tet_faces', 0):>6,}"
          f"{_n(c.get('vol_min'), 11, 4)}")
print()
print(f"written {out}")
