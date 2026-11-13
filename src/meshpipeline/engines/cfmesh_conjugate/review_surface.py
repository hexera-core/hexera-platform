# Responsibility: Build the two things a conjugate review opens - one msh carrying both regions' boundaries, and the coolant region's volume.
# Boundaries: conversion only. It reads what the run left on disk and writes a review aid; it never fails a mesh.
# Collaborates with: cfmesh_conjugate/finalize.py (which calls both) and sandbox/mesh_reader.py (which reads the msh).
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    FLUID_TO_SOLID,
    SOLID_REGION,
    SOLID_TO_FLUID,
)
from meshpipeline.engines.cfmesh_conjugate.foam_exec import prefer_finished
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

logger = logging.getLogger(__name__)

#: What finalize publishes as mesh_paths.surface. The shared manifest writer hardcodes this name,
#: so a run that does not write it raises EVIDENCE_MISSING at the review before any renderer
#: starts - which a live two-solid multi-region run hit after passing every executor gate.
REVIEW_MSH = "mesh.msh"
#: Where foamToVTK puts a region's boundary patches and its cells.
_BOUNDARY_GLOB = "boundary/*.vtp"
_INTERNAL = "internal.vtu"

#: THE TRIANGLE BUDGET FOR THE REVIEW SURFACE, AND IT IS THIS FILE'S OWN, NOT THE VIEWER'S.
#:
#: IT WAS THE VIEWER'S, and sharing it was wrong for a reason that has nothing to do with the
#: number. `render.polymesh_budget.DRAWN_MAX_TRIANGLES` is a TRANSPORT figure - its own
#: declaration (settings/viewer_limits.py) measures it on "Intel UHD via ANGLE/D3D11", says what
#: binds is about 92 B of VRAM per delivered face, and INVITES a measured build to raise it
#: ("several million is not out of reach"). This file is not transport. It is a file
#: `sandbox/mesh_reader.read_mesh` has to open in the REVIEW process, where what binds is that
#: reader - which is fragile enough here that every element has to be a triangle (see
#: build_region_msh). At the measured 63 B a triangle, a deployment that raised the viewer's
#: figure to 10 M because its browsers are fast would have turned this msh into about 600 MB and
#: never known it had. The REDUCER is still the viewer's (`fit_to_budget`, `reduce_patch`) and
#: that reuse is the good half; the LIMIT is a different constraint and now has its own home.
#:
#: WHAT THE NUMBER IS MEASURED AGAINST, 2026-11-12, two things at once.
#:
#: 1. THE FEATURE HAS TO SURVIVE. A conjugate review opens this surface to see whether the
#:    channels are there, and the reducer clusters points onto an h-by-h grid keyed by which of
#:    six axis sectors a vertex faces (polymesh_budget._facing). Two walls of ONE channel face
#:    opposite ways and so never merge with each other; what merges is walls of the SAME parity,
#:    which are one PITCH apart. Measured on a comb of twenty 100.631 um channels on a 200 um
#:    pitch at 25.1578 um cells (320,000 quads, 640,000 triangles, 40 distinct wall planes),
#:    pushed through `reduce_patch` at a budget chosen to land on each h:
#:
#:        h    512 um -> 10 of 40 planes      h    200 um -> 38 of 40
#:        h    337 um -> 18 of 40             h    162 um -> 40 of 40
#:        h    254 um -> 22 of 40             h    128 um -> 40 of 40
#:        h    222 um -> 32 of 40             h    100 um -> 40 of 40
#:
#:    So the feature survives whole below about 0.8 x its pitch and is destroyed above it. The
#:    Corintis plate's fluid interface is 31,184,373 faces at 25.1578 um = 1.973705e-02 m2, and
#:    h = sqrt(2 x area / allow), so the allowance it needs is:
#:
#:        h 337 um    347,578 triangles
#:        h 254 um    611,849 triangles     <- about what the viewer's 800,000 gave it: 613,451,
#:                                             h 253.67 um, 22 of the 40 wall planes
#:        h 162 um  1,504,119 triangles     <- the coarsest h the comb survives whole
#:        h 100 um  3,947,410 triangles
#:
#:    (613,451 is 800,000 x 31,184,373 / 40,667,460 - the denominator is the faces the fit
#:    actually sees, which is the interface plus the solid's OTHER patches, because `_patch_files`
#:    drops the solid side of the interface. A line here used to annotate the 347,578 row instead,
#:    off the undropped 71.9 M total; that is not the share the reducer was ever given.)
#:
#: 2. THE FILE HAS TO OPEN. Measured end to end through `build_region_msh` and then back through
#:    `read_mesh`, re-run 2026-11-12. THE FIXTURE, named because the byte counts move a little
#:    with it and a figure nobody can rebuild is not a measurement: four patches of 1,000,000
#:    all-quad faces - "walls" and "ports" on each of the two regions, points shared within a
#:    patch - which is 4,000,000 delivered faces, written as binary .vtp beside a two-region
#:    polyMesh. Every row is a process of its OWN, so `ru_maxrss` is that phase's and not the
#:    run's:
#:
#:        budget            msh bytes      build    builder peak   read_mesh   REVIEW peak
#:        800,000          50,257,247 B   11.14 s     0.667 GiB      3.20 s     3.358 GiB
#:        2,000,000       126,449,891 B   12.22 s     0.667 GiB      4.66 s     3.707 GiB
#:
#: TWO MILLION is where those two meet: a 121 MiB file that opens in under 5 s, for an h the comb
#: above survives whole. 800,000 buys a picture with 22 of the plate's 40 wall planes in it.
#:
#: WHAT THE 2.5x COSTS, written down because it is spent in a process this module cannot see. The
#: BUILDER did not move at all - 0.667 GiB at both budgets, because the budget bounds what it
#: WRITES and the peak is set by what it READS (the 4 M faces it holds as the compact pair) - but
#: the REVIEW process, which has to hold the file `read_mesh` parses, went 3.358 to 3.707 GiB:
#: +357 MiB of peak, and 3.7 GiB in that process is a figure worth knowing before anybody raises
#: this again. The read itself went 3.20 s to 4.66 s.
#:
#: (An earlier note here said the builder went 0.666 -> 0.721 GiB and the review 3.381 -> 3.766.
#: Those were the same two operating points on a differently shaped fixture; the builder's figure
#: in particular is a property of the BOUNDARY and not of the budget, so it is quoted here as what
#: it is - unchanged - rather than as a cost of the raise.)
#:
#: NOT OVERRIDABLE, deliberately. What bounds it is `read_mesh` in the review process and the
#: pitch of the feature being reviewed - a property of this code and of the part, not of a
#: deployment's browsers - so there is nothing here for a deployment to measure and move.
REVIEW_MAX_TRIANGLES = 2_000_000

#: HOW MUCH OF THAT BUDGET THE INTERFACE GETS BEFORE ANYTHING ELSE DOES.
#:
#: `fit_to_budget` shares a budget out in proportion to how much of the surface each patch is,
#: which is right for a viewer drawing a whole mesh and wrong here. The patch a conjugate reviewer
#: opens this file to look at is the one between the coolant and the metal: the channels live on
#: it, and every other patch is a face of the BLOCK - a plane, whose shape is fully described by a
#: handful of triangles however many faces it was meshed with. Spending the budget by face count
#: therefore buys detail where there is none to see and takes it from the only place there is.
#:
#: WHICH CHANGE IS THE FIX, AND IT IS NOT THIS ONE. Measured 2026-11-12 on the plate's own numbers
#: - interface 31,184,373 faces of 25.1578 um = 1.973705e-02 m2, through `reduce_patch`'s own
#: h = sqrt(2 x area / allowance) (render/polymesh_budget) - and checked against the comb above,
#: which keeps 40 of 40 wall planes at and below 162 um:
#:
#:     what the interface is allowed              triangles        h      planes   margin vs 162
#:     the old 800,000, shared by face count        613,451   253.67 um    22/40      -56.6%
#:     2,000,000, shared by face count            1,533,627   160.43 um    40/40       +1.0%
#:     2,000,000 at this share                    1,800,000   148.09 um    40/40       +8.6%
#:     the FULL 2,000,000                         2,000,000   140.49 um    40/40      +13.3%
#:
#: So THE BUDGET RAISE IS THE FIX: 800,000 to 2,000,000 is what moves the interface from 22 of 40
#: wall planes to 40 of 40, and it does that on the proportional share alone, with this constant
#: absent. What THIS constant buys is 160.43 um -> 148.09 um, which is 12.3 um of extra margin
#: against the 162 um the comb survives whole - 1.0% to 8.6% - and that is worth having, because
#: a part whose pitch is a little tighter than this one's or whose interface is a little larger
#: eats 1% without noticing. It is a margin, not the fix, and the line that used to be here said
#: "h = 141 um" at this share, which is the figure for the FULL budget: 141 um is 2,000,000
#: triangles and 148 um is 0.9 of them.
#:
#: The other patches are left 200,000 triangles between them, which is two and a half times the
#: ENTIRE delivered surface of the coupon this engine was first validated on (80,000 triangles).
#:
#: A case with no interface patch in its export takes neither branch - it is fitted in one call at
#: the whole budget, exactly as before - so nothing changes for a workspace that has no split.
INTERFACE_SHARE_OF_BUDGET = 0.9

#: Above this many boundary faces the surface is REFUSED rather than attempted, loudly.
#:
#: WHY A CEILING AT ALL. Everything below the budget is proportional and affordable; this is here
#: so a pathological boundary cannot spend a finished mesh's remaining time and then be killed.
#: It is the same number and the same reason as `native.INDEX_SKIP_ABOVE_FACES`, which guards the
#: boundary index against the same shape of input, so there is one figure for "more boundary than
#: a mesh job will walk" rather than two.
#:
#: AND IT IS ASKED BEFORE THE READ, which it was not. Until 2026-11-12 this was evaluated only
#: after every .vtp of both regions had been read off disk and held as the compact pair, so at
#: 119,999,999 faces the refusal landed having already spent about 3.8 GiB of that pair plus the
#: per-patch pyvista peak and the whole VTK tree's worth of reads - it could not prevent the cost
#: the paragraph above says it exists to prevent. It is now asked twice: first off the delivered
#: polyMesh's own boundary table, which costs a few hundred bytes of ASCII a region
#: (`_declared_faces`), and then again off what was actually read, because the table is the cheap
#: check and not the truth.
#:
#: WHAT IT IS MEASURED AGAINST, 2026-11-12, on synthetic two-region all-quad boundaries
#: (`_bench_msh.py`, since deleted): building the whole surface costs 0.161, 0.130 and 0.121 GiB
#: of peak RSS per million boundary faces at 4 M, 8 M and 20 M faces - so about 0.12 GiB a million
#: once the fixed costs are lost, and 120 M faces is about 14.5 GiB. That is well inside the
#: 57.6 GiB the smallest machine this engine runs on allows one process
#: (contracts/mesh_machine.usable_gib of n2-highmem-8). The Corintis plate's two regions are
#: 31,184,373 and 40,667,460 faces, 71.9 M together before the solid side of the interface is
#: dropped, so the part this was written for sits at 60% of the ceiling and about 8.6 GiB.
#:
#: A REFUSAL IS THE POINT. mesh_paths.surface is required=True (_shared.py), so no surface means
#: the review says EVIDENCE_MISSING on a mesh that is on disk and paid for. That is a bad outcome
#: and it is far better than the alternative, which is the same outcome reached by an OOM kill six
#: hours in with nothing written down at all.
REFUSE_ABOVE_FACES = 120_000_000

#: WHAT ONE BOUNDARY FACE COSTS THIS BUILDER, so the surface can be priced against what is left of
#: the run's budget before it is started rather than discovered to be unaffordable afterwards.
#: Exactly the shape, and for exactly the reason, of `native.INDEX_SECONDS_PER_FACE`: this is one
#: pass with no deadline of its own, so the only honest way to bound it is not to start one that
#: cannot finish. It was the last phase inside the engine with no budget at all - `_review_aids`
#: passed `budget_left_s` to the export and `native.py` passed it to the index, and this was
#: handed nothing - while at the plate it reads 71.9 M faces of .vtp through pyvista and writes
#: 121 MiB of msh. REFUSE_ABOVE_FACES does not bind there: 71.9 M is 60% of it.
#:
#: MEASURED RATHER THAN EXTRAPOLATED, re-run 2026-11-12 at the budget this module actually spends
#: (REVIEW_MAX_TRIANGLES), one process per row, on the fixture named under REVIEW_MAX_TRIANGLES
#: above. The figure is wall clock over the WHOLE call - the .vtp reads, the declared-face check,
#: the fit, the fan and the gmsh write:
#:
#:     delivered faces     whole call     a face    peak RSS
#:      1,000,000             3.13 s     3.13 us    0.483 GiB
#:      4,000,000            10.21 s     2.55 us    0.666 GiB
#:      7,997,584            15.47 s     1.94 us    1.090 GiB
#:     16,000,000            28.68 s     1.79 us    1.976 GiB
#:
#: IT IS AFFINE, NOT PROPORTIONAL, which is why the per-face column falls as the boundary grows
#: and why the charge is not simply the worst row. The last two rows give the marginal cost
#: directly - (28.68 - 15.47) / (16,000,000 - 7,997,584) = 1.65 us a face - and a fixed 2.3 s of
#: gmsh initialisation and model writing that does not scale.
#:
#: THE WALL CLOCKS ARE A SHARED MACHINE'S, which is why the SHAPE is what is charged against and
#: not any one row: these were taken on a box that was also running two other test suites, so the
#: absolute seconds are an upper bound rather than a best case. That is the conservative direction
#: for a cap - a rate measured too high refuses a surface that would have fitted, and only ever at
#: the end of a run that had already spent its budget - and the marginal 1.65 us is stable across
#: the two largest rows, which is the part the arithmetic rests on.
#:
#: CHARGED AT 2.0 us A FACE, which is above the marginal rate at every size measured and covers
#: the fixed 2.3 s for any boundary over about 6.5 M faces. Below that it under-prices, by at most
#: the 2 s the fixed cost is - a shortfall that cannot matter, because the gate it feeds is
#: "is there enough budget left to start", and 2 s of a 21,600 s budget is not what runs out. At
#: the plate's 71,851,833 faces it prices the surface at 144 s against a measured 121 s, which is
#: the direction to be wrong in: the same convention `INDEX_SECONDS_PER_FACE` states ("the SLOWER
#: of the two is charged ... a skipped index is a worse outcome than a tight one").
#:
#: (An earlier note here recorded 1.63 / 1.81 / 1.62 us for the first three sizes and called the
#: cost linear. Those were measured at the OLD 800,000 budget, where the fan and the gmsh write
#: are a quarter of the triangles; at the budget this module now spends, the rows above are what
#: it costs. The affine shape is also why no single per-face figure was ever going to reproduce.)
SURFACE_SECONDS_PER_FACE = 2.0e-6


def _budget() -> int:
    """The triangle budget this surface is fitted into: this module's own (REVIEW_MAX_TRIANGLES).

    A function rather than the constant used directly, because the tests that assert the file is
    bounded by the budget and not by the mesh read it from here.
    """
    return int(REVIEW_MAX_TRIANGLES)


def _time_index(case_dir_name: str) -> int:
    """foamToVTK names a time directory "<case>_<n>", not "<n>".

    Measured: a run in runs/review wrote VTK/fluid/review_0/, so parsing the whole name as a
    number found no time directory at all and both builders returned empty on a mesh that was
    there. The index is sorted numerically so _10 lands after _9 rather than before it. Same
    reading as engines/surface_deliverable._time_index, which this had got wrong.
    """
    _, _, tail = case_dir_name.rpartition("_")
    try:
        return int(tail)
    except ValueError:
        return -1


def _newest_time(region_dir: Path) -> Path | None:
    """The latest FINISHED time directory foamToVTK wrote for a region.

    Only one time describes the mesh as delivered. Globbing across the tree would collect every
    step it wrote, and since patches are named by file stem the same patch would be added twice
    under one region - a surface with the boundary laid on top of itself.

    FINISHED, because "the newest" on its own read a truncated export as the mesh. A live run
    delivered two fluid times: constant (the mesh, 70,361-byte inlet.vtp, complete) and 0 (the
    blank solver fields, 65,539 bytes, stopping mid-base64 with no closing tag). This picked the
    second, pv.read raised "Error parsing input file", the loop in build_region_msh logged it and
    moved on, and the review opened with patches: ['solid'] - no coolant at all - on a mesh whose
    fluid was there and correct. foam_exec.NO_FIELD_TIME stops that export being attempted;
    prefer_finished is what stops a half-written one being believed, whatever wrote it - and it
    prefers rather than requires, so a missing marker can never cost a surface that is readable.
    """
    times = prefer_finished([p for p in region_dir.iterdir() if p.is_dir()])
    if not times:
        return None
    return max(times, key=lambda d: (_time_index(d.name), d.name))


def delivered_regions(ws: Path) -> list[str]:
    """The regions the MESH has, read off the delivered polyMesh rather than the VTK export.

    The export is what fails; the polyMesh is what was built. Asking the export which regions
    exist is asking the thing that went wrong to grade itself.
    """
    root = ws / "constant"
    return [d.name for d in sorted(root.glob("*"))
            if d.is_dir() and d.name != "polyMesh" and (d / "polyMesh" / "boundary").is_file()]


def _patch_files(ws: Path, region: str) -> list[Path]:
    """Every boundary patch of one region, in a stable order.

    SOLID_TO_FLUID is skipped on purpose. It is the same surface as the fluid side's interface -
    measured at 93,600 faces on both - so carrying both doubles the interface geometry in the file
    for a view the reviewer already has from the coolant's side.
    """
    region_dir = ws / "VTK" / region
    if not region_dir.is_dir():
        return []
    latest = _newest_time(region_dir)
    if latest is None:
        return []
    return sorted(p for p in latest.glob(_BOUNDARY_GLOB) if p.stem != SOLID_TO_FLUID)


def _read_patch(path, name: str) -> dict | None:
    """One boundary .vtp as the patch render/polymesh_budget takes: {name, face_count, points,
    polys}. None when it is unreadable or empty.

    THE SAME SHAPE `polymesh_surface.boundary_surface` PRODUCES, deliberately, so the budget and
    the reducer the viewer already uses on a delivered boundary can be used on this one without a
    second implementation of either. `GetNumberOfPolys` rather than `n_cells`: the budget shares
    itself out by how much of the surface each patch is, and on a polydata that is the polygons
    and not the verts or lines VTK will also count.

    THE POINTS ARE COPIED TO float32 AND THE POLYS TO uint32, which the old path did not do: it
    handed gmsh whatever dtype the .vtp carried. float32 is the house convention for these patch
    dicts (render/index_surface, render/exact_surface, polymesh_surface.boundary_surface all store
    them this way) and `polymesh_budget._decode` reads them back at exactly that dtype, so this is
    consistency rather than a change of mind. What it costs is about 1e-8 m of rounding on a
    170 mm part, which reaches one place a reader might care about: `review.bbox` is derived from
    these points and is `finalize._block`'s fallback for the manifest extent. The clustering
    already shrinks that bbox by up to one cluster size, which is five orders of magnitude larger,
    so the rounding is not the thing to watch there.
    """
    import numpy as np
    import pyvista as pv

    try:
        mesh = pv.read(path)
    except Exception:
        logger.exception("conjugate review: could not read %s", Path(path).name)
        return None
    if mesh.n_cells == 0 or mesh.n_points == 0:
        return None
    return {"name": name,
            "face_count": int(mesh.GetNumberOfPolys()),
            "points": np.asarray(mesh.points, dtype=np.float32).tobytes(),
            "polys": np.asarray(mesh.faces, dtype=np.uint32).tobytes()}


def _declared_faces(ws: Path) -> int:
    """How many boundary faces the DELIVERED mesh says it has, over the patches this file would
    carry - `SOLID_TO_FLUID` left out, exactly as `_patch_files` leaves it out.

    0 when no region's boundary table can be read, which is every unit fixture that fakes a VTK
    tree without a polyMesh and is why the gate below is a pre-check rather than the only check.
    """
    total = 0
    for region in delivered_regions(ws):
        polymesh = ws / "constant" / region / "polyMesh"
        try:
            rows = read_boundary_patches(polymesh)
        except OSError:
            logger.warning("conjugate review: %s has no readable boundary table", region)
            continue
        total += sum(int(r.get("n_faces") or 0) for r in rows
                     if str(r.get("name")) != SOLID_TO_FLUID)
    return total


def _refuse(faces: int, into: dict | None) -> None:
    """Say, loudly and in the payload, that the boundary is past the ceiling.

    A REFUSAL IS THE POINT, and the key it writes is `surface_refused` rather than `refused`: the
    report already has a top-level `refused` meaning "the mesher refused this case", which
    `finalize.py` branches on to return success=False.
    """
    logger.error("conjugate review: the delivered boundary is %s faces, over the %s this "
                 "builds in a mesh job, so NO review surface is written - the mesh stands "
                 "and the review will say its evidence is missing (REFUSE_ABOVE_FACES)",
                 f"{faces:,}", f"{REFUSE_ABOVE_FACES:,}")
    if into is not None:
        into["surface_refused"] = (f"{faces:,} boundary faces is over the "
                                   f"{REFUSE_ABOVE_FACES:,} this builds in a mesh job")


def _is_interface(patch: dict) -> bool:
    """Whether this patch is the surface between the coolant and the metal - the one the review
    opens this file to look at. The name is "<region>/<patch>" (see _read_patch)."""
    stem = str(patch.get("name") or "").rpartition("/")[2]
    return stem in (FLUID_TO_SOLID, SOLID_TO_FLUID)


def _fit(patches: list[dict]) -> tuple[list[dict], dict | None]:
    """Every patch fitted into REVIEW_MAX_TRIANGLES, the interface first.

    THE REDUCER IS THE VIEWER'S, unchanged: this decides only how much of the budget each GROUP of
    patches is offered, and `fit_to_budget` then shares that out by face count with its own floor
    inside the group. See INTERFACE_SHARE_OF_BUDGET for what the split is measured against, and
    `_merge_fit_notes` for why the note is rebuilt rather than taken from one call.

    A boundary with no interface patch, or one with nothing but interface, is fitted in ONE call at
    the whole budget - which is both the old behaviour and the right one, since there is nothing to
    take the budget away from.
    """
    from meshpipeline.render.polymesh_budget import fit_to_budget

    budget = _budget()
    feature = [p for p in patches if _is_interface(p)]
    rest = [p for p in patches if not _is_interface(p)]
    if not feature or not rest:
        return fit_to_budget(patches, max_triangles=budget)
    share = max(1, int(budget * INTERFACE_SHARE_OF_BUDGET))
    fitted_feature, note_feature = fit_to_budget(feature, max_triangles=share)
    fitted_rest, note_rest = fit_to_budget(rest, max_triangles=max(1, budget - share))
    out = fitted_feature + fitted_rest
    return out, _merge_fit_notes((note_feature, note_rest), patches, out)


def _merge_fit_notes(notes, before: list[dict], after: list[dict]) -> dict | None:
    """The two fits' records as the one block the payload carries.

    THE TOTALS ARE RECOUNTED, not added. `fit_to_budget` returns None when it reduced nothing, and
    its `delivered_faces` / `drawn_triangles` cover only the patches IT was given - so adding the
    two notes would report the interface's faces as the whole boundary's whenever the block sides
    happened to fit. Counted off the patches going in and coming out, which is what `fit_to_budget`
    itself does. `resolution_m` is the COARSEST cluster used anywhere, the same convention.
    """
    live = [n for n in notes if n]
    if not live:
        return None
    return {"delivered_faces": sum(int(p.get("face_count") or 0) for p in before),
            "drawn_triangles": sum(int(p.get("face_count") or 0) for p in after),
            "resolution_m": max(float(n.get("resolution_m") or 0.0) for n in live),
            "patches": {k: v for n in live for k, v in (n.get("patches") or {}).items()}}


def build_region_msh(workspace, *, into: dict | None = None,
                     budget_left_s: float | None = None
                     ) -> tuple[dict[str, list[int]], tuple | None]:
    """Both regions' delivered boundaries as one msh, a physical group per REGION.

    ONE GROUP PER REGION, not per patch, which is the difference from the single-region engines.
    The reviewer's question here is whether the metal and the coolant are the two bodies the part
    actually has and whether they share their interface; the patch names are still on the
    manifest, and are what the patch targets walk.

    EVERY ELEMENT IS A TRIANGLE, which is not an aesthetic choice. sandbox/mesh_reader.py prefixes
    each face block with a corner-count column and then np.vstack()s the blocks, so a group
    holding both quadrangles and pentagons raises ValueError and the review never opens. Measured
    on a real two-region case: the mixed-type file is 28,781,401 bytes and read_mesh FAILS; all
    triangles is 35,654,271 bytes, loads in 0.8 s, and read_mesh reports the physical groups
    [('fluid', (1,)), ('solid', (2,))]. The extra 24% on disk buys a file that opens.

    AND 35,654,271 BYTES WAS THE ONLY SIZE ANYBODY HAD EVER MEASURED. This used to triangulate
    with a `while` loop that appended three Python ints per triangle for every boundary face, with
    no budget, no reduction and no size guard, and then hand gmsh `.tolist()` of the lot. The
    Corintis plate's two regions are 31,184,373 and 40,667,460 boundary faces, so that loop was
    over 200 million Python ints in a list - and `mesh_paths.surface` is required=True (_shared),
    so the OOM would have taken a mesh that cost hours with it.

    WHAT IT IS NOW, and both halves are reuse rather than invention:

      * the triangulation is `render.polymesh_budget._triangles`, the vectorised fan the viewer
        has used on delivered boundaries all along - it handles each polygon size present as one
        numpy block, which is right here because these are cell faces of an axis-aligned mesh and
        so planar and convex;
      * the size is `render.polymesh_budget.fit_to_budget`, which shares one budget across patches
        with a floor per patch and returns a patch UNCHANGED when it already fits - spent here at
        this module's own REVIEW_MAX_TRIANGLES rather than the viewer's figure, and with the
        INTERFACE patches given first claim on it (INTERFACE_SHARE_OF_BUDGET). Both of those
        constants carry the measurement that set them.

    "SO EVERY CASE THIS ENGINE HAS EVER DELIVERED WRITES THE SAME BYTES IT DID" is what the note
    above REVIEW_MAX_TRIANGLES used to claim for a patch that fits, and it is FALSE. The claim came
    from `reduce_patch` returning an unchanged patch under its budget, which is true; what does not
    follow is that the FILE is unchanged, because the triangulation changed underneath it too.
    Measured 2026-11-12, the old per-face `while` loop against `polymesh_budget._triangles` over
    ten shapes - all quads, a single triangle, a pentagon, a hexagon, two mixed-size orderings and
    the degenerate n=1 and n=2 cases: the triangle SETS and the WINDINGS match in all ten, so the
    geometry is sound and that is the half that matters, but the SEQUENCES differ wherever one
    size block holds more than one face. Plain all-quads already comes out non-identical, because
    the vectorised fan emits triangles grouped by fan index rather than one face at a time. A
    reader comparing an old msh with a new one byte for byte will find them different and the mesh
    will be the same mesh.

    MEASURED 2026-11-12, OLD AGAINST NEW, on synthetic two-region all-quad boundaries. The
    old-against-new rows were taken with `_bench_msh.py` and `_bench_tri.py`, which are not in the
    tree (the old shape they ran is gone with them), AT THE OLD 800,000 BUDGET - so read them for
    the comparison they are about and not for the absolute size of the file this now writes. The
    fan alone:

        1,000,000 faces -> 2,000,000 triangles    1.02 s -> 0.24 s    0.327 -> 0.272 GiB peak
        8,000,000 faces -> 16,000,000 triangles   8.20 s -> 1.61 s    2.124 -> 1.927 GiB peak

    and the whole of this function, end to end, writing the msh:

        boundary faces      OLD                                NEW
         4,000,000           14.36 s  1.649 GiB  390,176,539 B   6.37 s  0.644 GiB  38,464,735 B
         7,997,584           30.56 s  3.155 GiB  792,326,654 B  11.55 s  1.039 GiB  39,311,759 B
        19,998,784          (not run: ~76 s and ~7.9 GiB)       28.74 s  2.411 GiB  38,814,719 B

    The OLD column is linear in the face count, so at the Corintis plate's 71.9 M it comes to
    about 273 s, 28 GiB of peak RSS and a 7.1 GB mesh.msh - and that last number is the one that
    matters most, because `sandbox/mesh_reader.read_mesh` has to open the file in the review
    process afterwards.

    AND THE CLAIM THE NEW COLUMN IS REALLY MAKING - that the file's size is fixed by the BUDGET
    and not by the mesh - was re-measured on its own, 2026-11-12, at REVIEW_MAX_TRIANGLES, on the
    fixture named under that constant, one process per row:

        delivered faces     mesh.msh         whole call     peak RSS
         1,000,000          127,069,475 B       3.13 s      0.483 GiB
         4,000,000          126,449,891 B      10.21 s      0.666 GiB
         7,997,584          125,649,891 B      15.47 s      1.090 GiB
        16,000,000          125,309,891 B      28.68 s      1.976 GiB

    Sixteen times the boundary and the file is 1.4% SMALLER - that is the whole point of the
    budget, and it is the half of the old note worth trusting. (Every row there is AT OR OVER the
    budget: 1,000,000 quad faces is exactly 2,000,000 triangles. UNDER it nothing is reduced and
    the file is the size of the mesh - 600,000 faces writes 75,782,034 B - which is the deliberate
    "a boundary inside the budget keeps every face it had" half of `reduce_patch`.) What does grow with the boundary is
    the time and the builder's own peak, which is why this function is also priced and bounded
    (SURFACE_SECONDS_PER_FACE, REFUSE_ABOVE_FACES). At 800,000 the same 4 M-face boundary writes
    50,257,247 B, and the two files read back through `read_mesh` in 3.20 s and 4.66 s, reporting
    the physical groups ['fluid', 'solid'].

    `into` collects what the fit cost, so the run's report can say what the reviewer is looking at
    (native._review_aids puts it under `review.surface_reduced`). A payload that was not reduced
    records nothing, the same convention `fit_to_budget` itself uses.

    `budget_left_s` is what remains of the run's whole budget (native._run_conjugate_local), and a
    boundary that cannot be converted inside it is NOT STARTED - priced at
    SURFACE_SECONDS_PER_FACE off the same delivered boundary table the ceiling is asked of, in the
    same breath, because both are a few hundred bytes of ASCII a region and both have to be
    answered before the .vtp tree is read. None means "unbounded by the run", which is what every
    caller outside the pipeline wants.

    Returns ({region: [entity tags]}, the model's bounding box), or ({}, None) when there is no
    VTK boundary to convert - a non-fatal state, as everywhere else in this module.
    """
    ws = Path(workspace)
    # THE PAIR, AND ONLY THE PAIR - which is one of three ideas this engine holds about how many
    # regions a conjugate case has. `conjugate.build` refuses anything but exactly
    # {fluid, solid}, and `cfmesh_conjugate/viewer.py` was deliberately written for the coupled
    # plate-and-chip case that delivers THREE and says so in its own comment. The gap is noted at
    # conjugate.build's own assertion, which is where it bites first; closing it is one decision
    # about the coupled case taken in all three places at once, not a loop widened here.
    per_region = {r: _patch_files(ws, r) for r in (FLUID_REGION, SOLID_REGION)}
    if not any(per_region.values()):
        logger.info("conjugate review: no VTK boundary beside the mesh, nothing to convert")
        return {}, None

    # THE CEILING IS ASKED BEFORE THE TREE IS READ, which is the only order in which it can
    # prevent the cost it exists to prevent. It used to be asked AFTER - every .vtp of both
    # regions read off disk and held as the compact pair - so at 119,999,999 faces the refusal
    # landed having already spent about 3.8 GiB of compact pair, the pyvista peak on top of it and
    # the whole VTK tree's worth of reads. The loop this replaced streamed one patch at a time and
    # held none, so that was the one dimension in which the new shape was WORSE than the old.
    #
    # THE COUNT COMES OFF THE DELIVERED polyMesh, not off the export, which is the same source
    # `native._patch_rows` prices the boundary index from and the same rule `delivered_regions`
    # below follows: the export is the thing that fails, so the mesh's own boundary table is what
    # is believed about how big the mesh is. It is a few hundred bytes of ASCII a region.
    declared = _declared_faces(ws)
    if declared > REFUSE_ABOVE_FACES:
        _refuse(declared, into)
        return {}, None

    # ...AND SO IS THE PRICE, off the same table and in the same breath. A run whose budget cannot
    # pay for this pass is one whose machine is deleted partway through it, which leaves a
    # part-written msh and nothing to say why - the same failure the boundary index was given a
    # budget to prevent. Said as the resource it is, under the key a reader already has for "there
    # is no picture and here is why" (`surface_refused`; see `_refuse`).
    priced_s = declared * SURFACE_SECONDS_PER_FACE
    if budget_left_s is not None and priced_s > max(0.0, float(budget_left_s)):
        left_s = max(0.0, float(budget_left_s))
        logger.error("conjugate review: %s boundary faces price at about %.0fs and the run's "
                     "budget has %.0fs left, so NO review surface was built - the mesh stands and "
                     "the review will say its evidence is missing", f"{declared:,}", priced_s,
                     left_s)
        if into is not None:
            into["surface_refused"] = (
                f"{declared:,} boundary faces price at about {priced_s:.0f}s and the run's budget "
                f"has {left_s:.0f}s left, so no review surface was built "
                "(CONJUGATE_RUN_TIMEOUT_S is the budget, and the machine's life is derived "
                "from it)")
        return {}, None

    # EVERY PATCH IS READ BEFORE ANY IS WRITTEN, because the budget is shared out in proportion to
    # how much of the surface each patch is and that is not known until they have all been
    # counted. The alternative - count in one pass, reduce in a second - reads the whole VTK tree
    # twice, which on this plate is about 4 GB of .vtp read again to learn nothing new. Held as
    # the compact (float32 points, uint32 polys) pair rather than as pyvista objects: 71.9 M faces
    # is about 2.3 GiB that way.
    read: dict[str, list[dict]] = {}
    for region, files in per_region.items():
        got = [p for p in (_read_patch(f, f"{region}/{f.stem}") for f in files) if p]
        read[region] = got
    # ...AND ASKED AGAIN OF WHAT WAS ACTUALLY READ, because the declared table is the cheap check
    # and not the truth: a workspace whose polyMesh cannot be read declares nothing, and an export
    # can carry more faces than the boundary table says (the pre-split block, a time directory
    # from a hand-run utility). The expensive read has happened by here, so this gate buys only
    # the gmsh phase - which is why it is the second one and not the only one.
    faces = sum(int(p["face_count"]) for ps in read.values() for p in ps)
    if faces > REFUSE_ABOVE_FACES:
        _refuse(faces, into)
        return {}, None

    flat = [p for region in per_region for p in read.get(region, ())]
    fitted, note = _fit(flat)
    by_name = {str(p.get("name")): p for p in fitted}
    # THE FULL-SIZE PAYLOADS GO as soon as the fit is done, and that is not tidiness: they are the
    # 2.3 GiB the plate's 71.9 M faces are held as, and holding them through the gmsh phase as
    # well would double the peak for nothing. A patch that fit is returned UNCHANGED by
    # `reduce_patch`, so for those this is the same object and nothing is freed or copied.
    read = {r: [by_name.get(str(p["name"])) or p for p in ps] for r, ps in read.items()}
    del flat, fitted, by_name
    if note and into is not None:
        into["surface_reduced"] = note
        logger.warning("conjugate review: the surface carries %s triangles of %s delivered "
                       "faces, merged at %.4f mm - the reviewer is looking at a reduced "
                       "boundary and the payload says so", f"{note['drawn_triangles']:,}",
                       f"{note['delivered_faces']:,}", note["resolution_m"] * 1000.0)

    import gmsh

    gmsh.initialize(interruptible=False)          # worker-thread safe
    _pin_gmsh(gmsh)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("conjugate")
        groups: dict[str, list[int]] = {}
        tag = 0
        for region, files in per_region.items():
            tags: list[int] = []
            for patch in read.get(region, ()):
                tag += 1
                # Node tags are global in gmsh, so each patch gets its own block; points stay
                # SHARED within a patch, which is what keeps this a fraction of the size it would
                # be with three corners per face.
                base = tag * 100_000_000
                coords, tris = _as_arrays(patch, base)
                gmsh.model.addDiscreteEntity(2, tag)
                # NUMPY ALL THE WAY INTO GMSH, not `.tolist()`. gmsh's own Python API takes numpy
                # arrays when numpy is importable (`_ivectorsize` -> uintp, `_ivectordouble` ->
                # float64) and an array already of that dtype goes through `ascontiguousarray`
                # untouched, so these two calls copy nothing. The lists they replace were the
                # larger half of what this function used to cost: 240 million Python ints for the
                # plate's elements and another 100 million floats for its points.
                gmsh.model.mesh.addNodes(2, tag, _node_tags(base, len(coords) // 3), coords)
                if tris.size == 0:
                    continue
                gmsh.model.mesh.addElementsByType(tag, 2, _node_tags(0, 0), tris)
                tags.append(tag)
            if not tags:
                # LOUD, and naming the region. A dropped region used to leave no trace here at
                # all: the fluid's patches failed to read, this continued, and the only sign was
                # the reviewer refusing two region targets it could not find. Nothing in the log
                # an operator reads said the word "fluid". The mesh stands - this module never
                # fails one - but the review will be inconclusive and the reason belongs here.
                logger.error("conjugate review: region %r contributed no readable surface from "
                             "%d boundary file(s) - the review will not see it: %s",
                             region, len(files), ", ".join(f.name for f in files) or "(none)")
                continue
            group = gmsh.model.addPhysicalGroup(2, tags)
            gmsh.model.setPhysicalName(2, group, region)
            groups[region] = tags
        if not groups:
            return {}, None
        # HALF A SURFACE IS NOT THE SURFACE, and publishing it anyway is how a conjugate mesh
        # came back PASSED on a review that never saw its coolant. Measured on two live runs of
        # the same coupon: the fluid's .vtp files failed to parse, this published a mesh.msh
        # holding the solid alone, and the reviewer opened "patches: ['solid']". The second of
        # them returned verdict=passed with concerns=[], because every gate between here and the
        # verdict tests presence and not substance - the patch obligation asks for one patch of
        # any kind, so one inspection of one region's boundary discharges "every renderable
        # boundary patch".
        #
        # The regions are read off the DELIVERED polyMesh, not off the export: the export is the
        # thing that failed, and asking it which regions exist is asking it to grade itself. A
        # workspace with no region directories at all - every unit fixture that fakes a VTK tree
        # - names no required regions and is published as before.
        want = delivered_regions(ws)
        missing = [r for r in want if r not in groups]
        if missing:
            logger.error("conjugate review: the mesh has %d region(s) (%s) and the surface would "
                         "carry %d (%s), so it is NOT published - a review of half a conjugate "
                         "mesh reads as a review of all of it", len(want), ", ".join(want),
                         len(groups), ", ".join(sorted(groups)))
            return {}, None
        box = gmsh.model.getBoundingBox(-1, -1)
        gmsh.write(str(ws / REVIEW_MSH))
        return groups, tuple(box)
    except Exception:
        logger.exception("conjugate review: conversion failed (non-fatal)")
        return {}, None
    finally:
        gmsh.finalize()


def _node_tags(base: int, count: int):
    """`count` consecutive gmsh node tags from `base + 1`, in the dtype gmsh wants.

    uintp because that is what gmsh's `_ivectorsize` converts a "vector of sizes" to; handing it
    an array of exactly that dtype means `ascontiguousarray` returns the same buffer and nothing
    is copied on the way into the library. `range(...)` here was 100 million Python ints on the
    plate's points alone.
    """
    import numpy as np

    return np.arange(base + 1, base + 1 + int(count), dtype=np.uintp)


def _as_arrays(patch: dict, base: int):
    """One patch as (points, element node tags) in gmsh's own dtypes.

    THE FAN IS `render.polymesh_budget._triangles`, not a second implementation of it. A
    quadrangle is split and a face with more sides is fanned, which is correct for these because
    they are cell faces of an axis-aligned mesh and so planar and convex - and one physical group
    may not hold two corner counts (see build_region_msh).
    """
    import numpy as np

    from meshpipeline.render.polymesh_budget import _triangles

    pts = np.frombuffer(patch["points"], dtype=np.float32).reshape(-1, 3)
    tri = _triangles(np.frombuffer(patch["polys"], dtype=np.uint32))
    # float64 is what gmsh's `_ivectordouble` wants; this is the one copy that cannot be avoided,
    # because VTK writes these points as float32 and gmsh stores doubles.
    coords = np.ascontiguousarray(pts.reshape(-1), dtype=np.float64)
    return coords, np.ascontiguousarray((tri + (base + 1)).reshape(-1), dtype=np.uintp)


def review_volume(workspace) -> str | None:
    """The COOLANT region's cells, as a workspace-relative path, or None.

    The fluid region explicitly, never VTK/case_0/internal.vtu. That merged file is the whole
    block as one body, which is the very thing a conjugate review has to be able to tell a real
    split apart from - and with both exports on disk a glob returns the fluid's only by
    alphabetical luck.
    """
    ws = Path(workspace)
    region_dir = ws / "VTK" / FLUID_REGION
    if not region_dir.is_dir():
        return None
    latest = _newest_time(region_dir)
    if latest is None:
        return None
    hit = latest / _INTERNAL
    return hit.relative_to(ws).as_posix() if hit.is_file() else None


def _pin_gmsh(gmsh) -> None:
    """One thread and a fixed seed (engines/determinism.py): threaded gmsh inserts points in the
    order threads finish, so the same surface could come out as a different staging."""
    from meshpipeline.engines.determinism import GMSH_RANDOM_SEED, GMSH_THREADS
    gmsh.option.setNumber("General.NumThreads", GMSH_THREADS)
    gmsh.option.setNumber("Mesh.RandomSeed", GMSH_RANDOM_SEED)
