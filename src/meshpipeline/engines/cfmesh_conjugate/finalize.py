# Responsibility: Turn the conjugate run's report into the manifest the gates and the review read.
# Boundaries: it reports what the run measured. It meshes nothing, and it judges nothing the gates judge.
# Collaborates with: cfmesh_conjugate/native.py (which wrote the report) and cfmesh_conjugate/gates.py.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION

logger = logging.getLogger(__name__)

#: What the split names the coupling's own two patches. They are the engine's, never the user's,
#: so they are no part of the boundary contract the user approved at intake.
INTERFACE_MARK = "_to_"

#: The two regions a conjugate case has, and what regionProperties calls each.
REGION_TYPE = {FLUID_REGION: "fluid", SOLID_REGION: "solid"}

#: An OpenFOAM patch type read back off the delivered mesh, as a role in the user's words. Only
#: the types that carry a role: a plain `patch` could be an inlet or an outlet and the mesh cannot
#: tell which, so that one is not here.
_OF_TYPE_TO_ROLE = {"wall": "wall", "symmetryplane": "symmetry", "empty": "empty"}

#: Roles the delivered mesh genuinely witnesses. For anything else - inlet, outlet - the declared
#: role is echoed, because both are written as a plain `patch` and reporting them as "patch" would
#: lose the only record of which end the coolant goes in.
_MESH_EVIDENCED_ROLES = frozenset({"wall", "symmetry", "empty"})


def _region_boundary(ws: Path, region: str) -> list[dict]:
    """The patches of ONE region, read off that region's own boundary file.

    NEVER constant/polyMesh. The root mesh survives splitMeshRegions untouched, carrying the
    pre-split patches with their pre-split face counts, and those counts are the two regions'
    added together: measured on the unit cell, ymin has 532 faces at the root, which is the
    fluid's 128 plus the solid's 404. A check that read the root would see every patch populated
    on a case where the coolant never reached one of them.
    """
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    polymesh = ws / "constant" / region / "polyMesh"
    if not (polymesh / "boundary").is_file():
        return []
    try:
        return read_boundary_patches(polymesh)
    except (OSError, ValueError):
        logger.warning("conjugate finalize: %s has no readable boundary file", region,
                       exc_info=True)
        return []


def delivered_boundary_types(ws: Path, intake_patches: list) -> dict[str, str]:
    """The boundaries the user gets, with the role the delivered mesh evidences for each.

    The interface patches are left out: the user never asked for them and never approved them -
    the split writes them because a coupled case needs them.
    """
    declared = {(p.get("name") or "").strip(): (p.get("type") or "").strip()
                for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    out: dict[str, str] = {}
    for region in (FLUID_REGION, SOLID_REGION):
        for patch in _region_boundary(ws, region):
            name = str(patch.get("name") or "")
            if not name or INTERFACE_MARK in name:
                continue
            role = _OF_TYPE_TO_ROLE.get(str(patch.get("type") or "").strip().lower())
            if not role:
                said = declared.get(name, "")
                role = said if said and said.lower() not in _MESH_EVIDENCED_ROLES else "patch"
            out.setdefault(name, role)
    return out


def empty_patches(ws: Path) -> list[str]:
    """Named patches that came out of the split with no faces, as "<region>:<patch>".

    A PATCH WITH NO FACES IS A NAME WITH NOTHING BEHIND IT. Every block face is cut into both
    regions' boundary lists whether or not either region reaches it, so a declared inlet whose
    coolant never touches that face still appears in the fluid's boundary - with nFaces 0. The
    solver then has a `coolant_in` it cannot apply a flow rate to.

    The solid legitimately has no faces on nothing here, and neither does the fluid: both regions
    touch every side of the block they were cut from in the cases this engine takes. Where one
    genuinely does not, the gate's own words say which region and which patch, so the reader can
    see whether the name was the problem or the geometry was.
    """
    out = []
    for region in (FLUID_REGION, SOLID_REGION):
        for patch in _region_boundary(ws, region):
            name = str(patch.get("name") or "")
            if name and INTERFACE_MARK not in name and int(patch.get("n_faces") or 0) <= 0:
                out.append(f"{region}:{name}")
    return out


def _carry_mesh_wide(q: dict, rows: list[dict]) -> None:
    """The whole mesh's own figures, summed or spanned from the regions that make it up.

    THE SUMS ARE SUMS AND THE SPANS ARE SPANS, which is not the same reduction. Faces, points and
    every cell type add; the cell SIZE does not - the mesh's smallest cell is the smaller of the
    two regions' smallest, and its largest the larger. Taking max() of both ends, which is what
    collapsing with a single reducer would do, reports a mesh whose finest cell is the metal's.

    Nothing is written for a reading no region supplied. A mesh whose size was never measured must
    not report 0.0 m, because every gate that compares a size against a bar passes that.
    """
    if not rows:
        return
    # CONCAVE CELLS AND THE TET-DECOMPOSITION FACES ADD like the rest: a concave cell in the metal
    # and one in the coolant are two concave cells. Both only exist as readings since checkMesh
    # started being asked the extended geometry questions (`-allGeometry`, foam_exec.check_mesh),
    # and on this part they are the two biggest things it found - 4,620,363 concave cells across
    # the pair, and 22 faces whose tet decomposition is the only Error in either report.
    for key in ("faces", "points", "skew_faces", "small_determinant_cells",
                "concave_cells", "low_quality_tet_faces"):
        total = sum(int(r.get(key) or 0) for r in rows)
        if total:
            q[key] = total
    types: dict[str, int] = {}
    for r in rows:
        for kind, n in (r.get("cell_types") or {}).items():
            types[kind] = types.get(kind, 0) + int(n)
    if types:
        q["cell_types"] = types
        q.update(types)        # the viewer's metric allowlist reads each kind as its own key
    lows = [r["min_cell_m"] for r in rows if r.get("min_cell_m")]
    highs = [r["max_cell_m"] for r in rows if r.get("max_cell_m")]
    if lows:
        q["min_cell_m"] = min(lows)
    if highs:
        q["max_cell_m"] = max(highs)
    if lows and highs and min(lows) > 0.0:
        q["cell_size_ratio"] = max(highs) / min(lows)
    ars = [r["max_aspect_ratio"] for r in rows if r.get("max_aspect_ratio") is not None]
    if ars:
        q["max_aspect_ratio"] = max(ars)
    avgs = [r["avg_non_ortho"] for r in rows if r.get("avg_non_ortho") is not None]
    if avgs:
        # the worse of the two averages, not an average of averages: a mean over two regions of
        # wildly different cell counts weights them equally, which is not a figure about anything
        q["avg_non_ortho"] = max(avgs)


def quality_of(ws: Path, report: dict) -> dict:
    """The manifest's quality dict, from the report the run left and the delivered regions.

    Everything here was MEASURED by the run: checkMesh per region, the meshed volume of each,
    the interface's face count from both sides, and the coolant's disagreement with the CAD. None
    of it is re-derived, and nothing that was not measured is reported as a number - an
    unmeasured non-orthogonality carried as 0.0 reads as a perfect mesh to every gate that
    compares it against a bar.
    """
    regions = report.get("regions") or {}
    rows: list[dict] = []
    fatal: list[str] = []
    non_ortho: list[float] = []
    skew: list[float] = []
    for name in (FLUID_REGION, SOLID_REGION):
        r = regions.get(name)
        if not isinstance(r, dict):
            continue
        fatal += [f"{name}:{f}" for f in (r.get("fatal") or [])]
        if r.get("max_non_ortho") is not None:
            non_ortho.append(float(r["max_non_ortho"]))
        if r.get("max_skewness") is not None:
            skew.append(float(r["max_skewness"]))
        rows.append({"name": name, "type": REGION_TYPE[name],
                     "cells": int(r.get("cells") or 0),
                     "volume_m3": float(r.get("volume_m3") or 0.0),
                     "pieces": int(r.get("pieces") or 0),
                     # THE SIZES, the third hop of three they have to survive: native._as_dict,
                     # conjugate_report.json, here, mesh_manifest.json. Job 6ddb58aa's manifest
                     # carried 23 readings of its coolant, down to a 329,153,282-byte
                     # concaveCells.vtp, and `pieces: 6` with no sizes - the one reading that
                     # identifies the defect was the only one the chain dropped.
                     "piece_cells": [int(x) for x in (r.get("piece_cells") or ())],
                     "mesh_ok": bool(r.get("mesh_ok")),
                     "fatal": list(r.get("fatal") or []),
                     # PER REGION, which is the only way these mean anything here. The coolant is
                     # the fine region and the metal the coarse one; collapsed with max() and
                     # min() below they describe a mesh that does not exist. A reader asking "is
                     # my mesh good" wants to know WHICH half is not.
                     "faces": int(r.get("faces") or 0),
                     "points": int(r.get("points") or 0),
                     "cell_types": dict(r.get("cell_types") or {}),
                     "min_cell_m": r.get("min_cell_m"),
                     "max_cell_m": r.get("max_cell_m"),
                     "max_non_ortho": r.get("max_non_ortho"),
                     "max_skewness": r.get("max_skewness"),
                     "max_aspect_ratio": r.get("max_aspect_ratio"),
                     "avg_non_ortho": r.get("avg_non_ortho"),
                     "skew_faces": int(r.get("skew_faces") or 0),
                     # WHERE the poor cells are, carried across so the reading below can find it.
                     # `q["poor_cell_sets"]` reads `poor_sets` off these rows, and this key was
                     # not on the list, so that reading has been [] on every run since it was
                     # written - production included - while the data sat one dict away.
                     # native.py:245 puts it on the region ("WHERE the poor cells are, beside how
                     # many of them there are") and `r` IS that region dict; these rows name their
                     # keys explicitly and this one was simply missing from the names.
                     #
                     # It is one of the six readings the first customer asked for on 2026-10-29,
                     # as a "highlight of poor-quality cells". The DRAWABLE half never came
                     # through here - viewer._poor_surfaces reads poor_cells/<region>/*.vtp off
                     # the disk itself - so the highlight drew while the manifest said there was
                     # nothing to highlight. That is the worse way round: a reader who checks the
                     # number is told their mesh is clean.
                     "poor_sets": [dict(x) for x in (r.get("poor_sets") or ())],
                     "small_determinant_cells": int(r.get("small_determinant_cells") or 0),
                     "concave_cells": int(r.get("concave_cells") or 0),
                     "low_quality_tet_faces": int(r.get("low_quality_tet_faces") or 0)})

    fluid = regions.get(FLUID_REGION) or {}
    plan = report.get("plan") or {}
    expected = float(plan.get("expect_fluid_volume_m3") or 0.0)
    meshed = float(fluid.get("volume_m3") or 0.0)
    faces_fluid = int(report.get("interface_faces_fluid") or 0)
    faces_solid = int(report.get("interface_faces_solid") or 0)
    missing = [n for n in (FLUID_REGION, SOLID_REGION) if n not in {r["name"] for r in rows}]
    undeclared = [n for n in regions if n not in REGION_TYPE]
    blank = empty_patches(ws)

    q: dict = {
        "cells": sum(r["cells"] for r in rows),
        "fatal": fatal,
        "regions": rows,
        "regions_missing": missing,
        "regions_undeclared": undeclared,
        # BOTH SIDES OF THE COUPLING, and they must be the same number. The split cuts along faces
        # that already existed, so the two sides are the same surface: measured at 93,600 faces
        # each on the unit cell. A mismatch is a mesh the solver cannot map across.
        "interface_faces_fluid": faces_fluid,
        "interface_faces_solid": faces_solid,
        "interface_ok": faces_fluid > 0 and faces_fluid == faces_solid,
        "coolant_volume_m3": meshed,
        "coolant_expected_m3": expected,
        "coolant_pieces": int(fluid.get("pieces") or 0),
        "coolant_piece_cells": [int(x) for x in (fluid.get("piece_cells") or ())],
        "coolant_pieces_expected": int(plan.get("expect_fluid_pieces") or 0),
        "empty_patches": blank,
        # THE MOUTHS THE PLAN ASKED FOR AND THE ONES THE MESH DOES NOT CARRY. `empty_patches`
        # cannot see a dropped mouth - createPatch REMOVES a patch whose face set came out empty
        # rather than leaving it at zero faces, so the name is simply gone. Lifted out of the
        # conjugate report and into the manifest so the viewer, the verdict and the review can
        # see it: otherwise the only way to learn that the delivered plan names an opening the
        # mesh lacks is to untar a multi-gigabyte bundle and read its second member.
        "port_patches_planned": list(report.get("port_patches_planned") or []),
        "port_patches_dropped": list(report.get("port_patches_dropped") or []),
        "ports": report.get("ports"),
        "patch_names": dict(report.get("patch_names") or {}),
        "native_problems": list(report.get("problems") or []),
        "notes": list(report.get("notes") or []),
        "background_cell_m": plan.get("background_cell_m"),
        "levels": plan.get("levels"),
        "cells_across_asked": plan.get("cells_across"),
        "narrowest_passage_m": plan.get("narrowest_m"),
    }
    if expected > 0.0:
        q["coolant_error_pct"] = abs(meshed - expected) / expected * 100.0
        q["coolant_tol_pct"] = float(plan.get("volume_tol") or 0.0) * 100.0
        # THE ERROR AS A FRACTION OF WHAT THIS RESOLUTION ALLOWS, and the reason it is computed
        # here rather than compared somewhere: the bar is not a constant. It is the measured
        # staircase error for the cells-across used, with a margin, and it rides in the plan
        # (cfmesh_conjugate/plan._tolerance). A declared Criterion can only hold a static
        # threshold, so stating the bar twice - 5% in the criterion and 19.89% in the gate - gave
        # one quantity two readers that disagreed: this fixture came out 9.95% wrong at 5 cells
        # across, which the gate allows and the criterion refused. Expressed as a ratio, both read
        # the SAME number and the static bar is 1.0.
        if q["coolant_tol_pct"] > 0.0:
            q["coolant_error_over_tol"] = q["coolant_error_pct"] / q["coolant_tol_pct"]
    # THE FIT IS CHECKED AGAINST THE RUN THAT WAS JUST PAID FOR. budget.PEAK_BYTES_PER_CELL is
    # fitted by hand on three builds and it is what chooses the machine: driver.drive calls
    # MM.choose(process_gib=B.peak_gib(estimate)). The mesher measures its own peak on every run -
    # conjugate.children_peak_rss_bytes, written into the report as peak_rss_bytes - and nothing
    # has ever compared the two, so the fit can drift arbitrarily far and the first sign of it is
    # a kernel kill hours in. budget.py already records one 27% miss in exactly that direction.
    #
    # AGAINST THE DELIVERED CELL COUNT, not the pre-flight estimate. That estimate carries its own
    # factor of three and folding it in would measure the cell model instead of the memory model.
    #
    # REPORTED, NOT GATED. There is no defensible bar for this ratio yet and the first thing needed
    # is the numbers. The criterion declared beside it is non-gating for a second reason as well:
    # peak_rss_bytes is legitimately 0 when the mark cannot be attributed to this build, and a
    # gating key missing from the quality block fails closed.
    peak = int(report.get("peak_rss_bytes") or 0)
    if peak > 0 and q.get("cells"):
        from meshpipeline.engines.cfmesh_conjugate import budget as B

        q["peak_gib"] = peak / 1024.0 ** 3
        q["peak_bytes_per_cell"] = peak / q["cells"]
        # ABSENT rather than 0.0 when it cannot be measured: a zero ratio reads as a perfect fit
        # to every reader that compares it against a bar.
        q["peak_over_predicted"] = q["peak_gib"] / B.peak_gib(q["cells"])
    # WHETHER THE COOLANT HAS A WAY IN AND A WAY OUT, which is the first thing a solve needs and
    # the last thing anything here measured. `ConjugateReport.flowable` has existed since the
    # engine was written and no gate reads it, so a sealed coolant cleared all eight mesh gates
    # and was refused further down for a patch-name contract mismatch - true, and the wrong
    # diagnosis. The customer plate is exactly this case: its fittings are bores that stop inside
    # the metal, so the delivered fluid region carries one patch and no port at all.
    _ports = report.get("ports") or {}
    _fluid_patches = (regions.get(FLUID_REGION) or {}).get("patches") or {}
    # THE PORT IS NAMED TWICE AND THE DELIVERED PATCH WEARS THE SECOND NAME. `ports` carries the
    # BLOCK FACE the coolant leaves by - `ymin` - because that is what the port plan is written
    # in; the patch in the delivered mesh is called `coolant_in`, because renameBoundary puts the
    # user's name on the face before the split. Looking the face name up in the delivered patches
    # finds nothing and reads as a sealed part, which is how the first version of this refused
    # the canonical deliverable fixture. `patch_names` is the mapping and `_port` below already
    # walks it in the other direction.
    _named = {str(k): str(v) for k, v in (report.get("patch_names") or {}).items()}

    def _faces_of(which: str) -> int:
        face = str(_ports.get(which) or "")
        if not face:
            return 0
        return int(_fluid_patches.get(_named.get(face, face), 0))

    q["coolant_ports_declared"] = bool(_ports.get("inlet") or _ports.get("outlet"))
    q["coolant_inlet_faces"] = _faces_of("inlet")
    q["coolant_outlet_faces"] = _faces_of("outlet")
    q["coolant_flowable"] = q["coolant_inlet_faces"] > 0 and q["coolant_outlet_faces"] > 0
    q["coolant_pieces_short"] = _pieces_short(q)
    q["resolution_short"] = _resolution_short(plan)
    # WHERE THE POOR CELLS ARE. The count is already here per region; these are the surfaces
    # checkMesh wrote them out as, which is the half a customer can open beside their mesh.
    q["poor_cell_sets"] = [
        {"region": str(r.get("name")), **dict(x)}
        for r in rows for x in (r.get("poor_sets") or ())]
    # NEAR-WALL RESOLUTION, reported on every run because it is asked for on every run. The layer
    # count is the MEASURED one when a pass measured one and the plan's ask otherwise, with the
    # ask always beside it. See `_near_wall` and NEAR_WALL_LAYERS.
    #
    # THE LAYER PASS IS JUDGED ON THIS PATH TOO, which is the defect this closes. Every delivered
    # conjugate mesh comes through `native._run_conjugate_local`, which calls `conjugate.build`,
    # which reads the layer log and lists its shortfalls - and then `native._as_dict` dropped the
    # whole report, so the judgement reached no artifact, no manifest and no reviewer. On job
    # 0ff6f9e5 three floors were missed (88.5826% coverage against 92.0, 71.9% thickness against
    # 75.0, 239 faces in error per million against 200) and the delivered manifest published
    # `near_wall_layers: 3` with nothing else about layers in it at all.
    _grown = _layers_published(report.get("layers") or {})
    q.update(_grown)
    q.update(_near_wall(plan, _grown))
    # WHETHER THE INTERIOR CAN BE CUT AT ALL. The engine declares a REGION inspection target, so
    # the review context tells the reviewer to call inspect_region - and the slices are published
    # only when a per-region volume was exported. Without one the reviewer is sent to a tool that
    # produces no evidence, and a review with nothing to submit stalls into a non-verdict rather
    # than failing (the defect engines/manifest's own comment records, job 63ff3d42: 8 rounds, 0
    # submissions). The export never fails a mesh, so this is recorded rather than refused - but
    # it is recorded, because a stalled review with no trace of why is the worst of the three.
    q["review_volume"] = (report.get("review") or {}).get("volume") or ""
    if non_ortho:
        q["max_non_ortho"] = max(non_ortho)
    if skew:
        q["max_skewness"] = max(skew)
    _carry_mesh_wide(q, rows)
    # AND WHETHER CHECKMESH SAID SO, which this did not ask. `report["ok"]` is whether the native
    # run had problems and `fatal` is _FATAL's four markers; neither is checkMesh's own verdict.
    # Each region's is carried in its row and was never read, so a region that failed two checks
    # was published as mesh_ok: true. Dormant until `check_mesh` started passing -allGeometry,
    # because before that both of the customer plate's regions printed "Mesh OK." anyway.
    q["mesh_ok"] = (bool(report.get("ok")) and not fatal and not missing and not undeclared
                    and not blank and q["interface_ok"]
                    and all(bool(r.get("mesh_ok")) for r in rows))
    return q


def _pieces_short(q: dict) -> list[str]:
    """The piece-count disagreement in words, or [] when the counts agree.

    A LIST AND NOT A BOOLEAN, because the declared criterion is `op="empty"` and the gate must be
    able to tell "they agree" from "nobody counted": an absent key reads as unmeasured and
    refuses, where False would read as a pass.
    """
    want, got = int(q["coolant_pieces_expected"]), int(q["coolant_pieces"])
    if want <= 0 or got == want:
        return []
    # WITH THE SIZES IN THE SENTENCE, because this string IS the criterion's `measured` value and
    # therefore the line a reader of the report card is given. The count alone sent a ten-agent
    # investigation at the cell size on a mesh whose four extra pieces were metal; three of them
    # were a single cell, which says on sight that the question is which cells were tagged.
    # `conjugate.piece_sizes_said` is the one spelling, shared with the engine's own problem list
    # and with the customer-facing gate, so a reader comparing them sees one mesh.
    from meshpipeline.engines.cfmesh_conjugate.conjugate import piece_sizes_said

    return [f"the coolant came out as {got} piece(s) where the part's geometry has {want}. "
            + piece_sizes_said(q.get("coolant_piece_cells"))]


#: HOW MANY PRISM LAYERS A CONJUGATE MESH HAS, and the number is not a shortfall to be fixed.
#:
#: Corintis asked for "number of boundary layers in the fluid" (2026-10-29) and the answer for a
#: microchannel part on this engine is none. The reason stated here until 2026-10-30 was that a
#: layer stack on `fluid_to_solid` would leave every face without a partner, because an extruded
#: prism is a cell inserted on ONE side of a patch and conformality is what the split-after-
#: meshing approach exists to guarantee.
#:
#: THAT WAS MEASURED AND IT IS FALSE. `generateBoundaryLayers` with `optimiseLayer 0` on the
#: coupon kept all 14,477 interface faces identical on BOTH sides to 4e-18 m, at 99.97% coverage.
#: Layers do not break conformality. The argument was about the wrong thing, and because it read
#: as "impossible" it also argued against the run that would be worth doing.
#:
#: THE FIT ARGUMENT WAS ALSO MEASURED, AND IT IS NOW OUT OF DATE. This said "a prism stack does
#: not go across a 98 um passage at any affordable cell size", on a reading that took the quality
#: to non-orthogonality 64.95 against a limit of 65 and bought nothing. What it was measuring was a
#: stack that did not fit its own background cell, which is a dict defect and not a property of the
#: part: at `firstLayerThickness 4.15e-06` and `expansionRatio 1.2` into a 25.1578 um cell the
#: three-layer stack is 60.0% of ONE cell, and the pass delivered 95.553% coverage over
#: 16,619,777 interface faces with 2,936 faces in error out of 102,536,327 cells
#: (docs/semiconductor/MESH_PITCH8.md, 2026-11-08). `cfmesh_conjugate/layers.fit_complaint` is that
#: rule, stated once, and it refuses the dict this comment was describing.
#:
#: TWO REPAIRS WERE NAMED HERE AS BLOCKERS AND THE DELIVERED LAYERED MESH REFUTES BOTH. The claim
#: was that cfMesh's writer drops the mappedWall coupling keys and that checkMesh then wants
#: `renumberMesh`. The boundary file snappyHexMesh wrote AFTER the layer pass carries
#: `type mappedWall`, `sampleMode nearestPatchFace`, `sampleRegion solid` and
#: `samplePatch solid_to_fluid`, with nFaces still 16,619,777 matching solid_to_fluid exactly; and
#: that mesh's own checkMesh prints "Upper triangular ordering OK." Neither repair is needed, and
#: pointing an implementer at them cost nothing only because nobody followed the advice.
#:
#: The mechanical part is real and unchanged: `boundaryLayers.patchBoundaryLayers` names PATCHES,
#: and at meshing time the coolant's wall is not a patch - it is interior to one manifold block,
#: and the patch is created by `splitMeshRegions` afterwards. So layers are a SECOND PASS over the
#: split mesh, not a key in the meshDict. That is what `cfmesh_conjugate/layers` does.
#:
#: THIS CONSTANT IS NOW ONLY THE DEFAULT FOR A MESH THAT ASKED FOR NONE, which is still every
#: delivered mesh. `_near_wall` reads the plan, because a manifest that published 0 on a layered
#: mesh would be a false figure in the customer's own report.
NEAR_WALL_LAYERS = 0


def _near_wall(plan: dict, grown: dict | None = None) -> dict:
    """The near-wall resolution: how fine the cell at the coolant's wall is, over how many graded
    levels it got there, and HOW MANY LAYERS THE PASS ACTUALLY GREW.

    THE LAYER COUNT IS THE MEASURED ONE, NOT THE ASK, and that is the whole point of `grown`.
    This function read `plan["layers"]` and published it as `near_wall_layers`, which is the
    number the mesher was TOLD to grow. On the real plate, job 0ff6f9e5, that published 3 into the
    customer's own report while the delivered mesh carried 2.25 - the figure is in that run's own
    snappyHexMesh.log, `fluid_to_solid 16582198 3 2.25 1.09e-05 71.9`, and nothing read it. A
    report that publishes only the ask is publishing an intention, which is `layers.measured`'s
    own words for it.

    `grown` is `layers.measured()` as `conjugate.build` took it, carried across the container
    boundary in `conjugate_report.json["layers"]`. The ask travels beside it as
    `near_wall_layers_asked` and is never dropped: a reader comparing 2.25 against 3 is reading
    the shortfall, and a reader given only 2.25 cannot see that anything was short.

    THE MEASURED COUNT IS A FRACTION AND THE ONE USER-FACING READER NOW CARRIES IT AS ONE. It did
    not: `render/mesh_facts.quality` listed `near_wall_layers` in `_INT_METRICS`, so the viewer
    payload got `int(2.25)` = 2 - a THIRD number, neither the 3 that was asked for nor the 2.25
    that grew, in the one panel a customer reads. Worse, none of the eleven layer figures beside it
    were in that reader at all, so the payload was `{near_wall_layers: 2, near_wall_levels: 4,
    near_wall_cell_m: 2.51578e-05}` and the ask had vanished from it: no reader could see that
    anything had fallen short. Since 2026-11-17 that key is in `_FLOAT_METRICS`, with
    `near_wall_layers_asked`, `near_wall_layers_measured`, the four figures this engine draws its
    own floors on and `layer_short` beside it.

    NOTHING DOWNSTREAM WANTS AN INTEGER, and that was grepped rather than assumed: the only
    consumers of this field outside this engine are the two `viewer.js` copies, whose `show()`
    prints an integer as a count and a non-integer to two decimals.

    BOTH OF THOSE COPIES ARE SERVED, and this comment said one of them was not. It read
    "`apps/console/public/static/js/viewer/viewer.js` is a second copy nothing serves", which is
    false, and the chain was traced end to end on 2026-11-17 rather than assumed either way:

        ui/js/viewer/viewer.js
            docker-compose.yml:185 mounts ./ui at /srv/ui:ro, settings/inventory STATIC_DIR
            defaults to ./ui, and the api serves it at /ui. tests/ui/conftest points STATIC_DIR
            here too, so this is the copy every browser test drives.
        apps/console/public/static/js/viewer/viewer.js
            Dockerfile:682 COPYs apps/console/public/ into the console image;
            apps/console/src/app/(dashboard)/runs/[jobId]/page.tsx renders Workbench;
            workbench.tsx:85 loads /static/js/main.js; main.js:34 imports ./viewer/viewer.js;
            apps/console/next.config.ts declares no rewrites. Next serves public/ at the root, so
            /static/js/viewer/viewer.js IS that file and the console run page runs it.

    The two are hand-maintained mirrors with no build step between them (nothing in the Dockerfile,
    package.json or any script copies one onto the other), and they had drifted far: 3,554 lines
    against 1,836. The console's copy still printed "none: a prism stack does not fit across a
    passage this narrow" and "graded refinement instead" - the claim this engine's own delivered
    mesh refutes, with 2.25 layers in it - to every reader of the console's run page. The layer
    block is now the same in both, and `tests/unit/render/test_the_layer_count_reaches_the_reader`
    holds them identical over it so the next divergence fails a test instead of shipping.

    `near_wall_layers_in_mesh` IS GONE, 2026-11-17. It existed for one reason, stated here: to
    carry the exact 2.25 "under a key nothing coerces", because `near_wall_layers` was being cast
    to 2 by `mesh_facts._as_int`. That cast was removed in the same change - the key is in
    `_FLOAT_METRICS` now - so the duplicate was guarding a coercion that no longer happens, and
    nothing in src/, ui/ or apps/ ever read it. `layers_in_mesh` stays, because `_near_wall` reads
    it to get this figure at all and `_said` prints it.
    """
    bg = float(plan.get("background_cell_m") or 0.0)
    levels = int(plan.get("levels") or 0)
    # THE FINE SURFACES ARE WHERE THE WALL ACTUALLY IS on a two-scale part: the thin passages get
    # more levels than the coolant as a whole, so the coolant's own level count understates the
    # resolution at the only wall that was hard to resolve. The deepest wins.
    for surf in plan.get("fine_surfaces") or ():
        levels = max(levels, int((surf or {}).get("levels") or 0))
    asked = int(plan.get("layers") or NEAR_WALL_LAYERS)
    out: dict = {"near_wall_layers_asked": asked, "near_wall_levels": levels}
    in_mesh = (grown or {}).get("layers_in_mesh")
    if in_mesh is None:
        # NO LAYER PASS MEASURED ONE. The ask stands in, exactly as it did - on every mesh this
        # engine has delivered with `layers: 0` that is the honest answer, and on a run whose log
        # could not be read it is the only answer there is. `near_wall_layers_measured` says
        # which of the two a reader is looking at, because 3 asked and 3 grown are the same
        # number and only one of them is evidence.
        out["near_wall_layers"] = asked
        out["near_wall_layers_measured"] = False
    else:
        out["near_wall_layers"] = float(in_mesh)
        out["near_wall_layers_measured"] = True
    if bg > 0.0 and levels >= 0:
        out["near_wall_cell_m"] = bg / 2 ** levels
    return out


#: THE MEASURED LAYER FIGURES THAT TRAVEL INTO THE MANIFEST, keyed as `layers.measured` names
#: them so the two sides cannot drift. Published only when the pass measured them: an absent
#: figure is a figure nobody took, and a 0.0 reads as a measurement of nothing grown.
_LAYER_FIGURES = ("layer_coverage_pct", "layer_thickness_pct", "layer_thickness_m",
                  "layer_faces_in_error", "layer_cells_added", "layer_iterations",
                  "layer_seconds", "layer_patch", "layers_asked", "layers_in_mesh")


#: THERE IS NO `_floors_had_a_reading` HELPER AND THIS NOTE IS WHY, 2026-11-18. It existed, it
#: was `len(layers.floors_unjudged(grown)) < len(layers.LAYER_FLOORS)`, and it had NO CALLER
#: anywhere in src/, ui/, apps/ or tests/ - while two comment blocks in `render/mesh_facts` named
#: it as the whole of how the two empty cases are told apart. A function with no caller that is
#: described elsewhere as load-bearing is worse than no function: the next reader goes looking for
#: the guard where the comment points and does not find the one that runs.
#:
#: IT WAS REMOVED RATHER THAN WIRED, and the reason is that the caller it would have had cannot
#: use it. `_layers_published` needs the unjudged roster ITSELF - it publishes the names - so it
#: already holds `layers.floors_unjudged(grown)`; calling a helper that recomputes the same
#: tuple only to compare its length would read that roster twice to answer a question the roster
#: in hand already answers. The guard is the two lines in `_layers_published` below, and it is
#: the only guard there has ever been in the shipped path.
#:
#: WHAT THE HELPER'S OWN DOCSTRING RECORDED IS KEPT, because it is the measurement that put the
#: guard there and losing it would cost the next round the same day. MEASURED, SYNTHETIC,
#: 2026-11-17: `layers.read_layer_log("")` - what `conjugate.build` hands over when the layer log
#: is missing or unparseable - returns every figure as `None` except `iterations`, which is
#: `len(extruding)` and so comes back `0`. `0` is neither `None` nor `""`, so it survives
#: `layers.measured`' filter, so the `if not out: return {}` that used to stand here never fired
#: on the one case it was for: the payload got `{"layer_iterations": 0, "layer_short": []}`, and
#: the declared `layer_short` criterion (`op="empty"`, `review_types.Criterion.evaluate` returns
#: `not v`) read that empty list as a PASS. A run whose judgement never happened reported as a
#: clean one.


def _layers_published(raw: dict) -> dict:
    """What the layer pass grew and where it fell short, for the manifest's quality block.

    `raw` is `conjugate_report.json["layers"]`, which is `layers.read_layer_log`'s own dict with
    the shortfalls `conjugate.build` already computed added to it. {} when NO LAYER PASS RAN, and
    as of 2026-11-17 that is the only thing {} means.

    THREE ANSWERS, NOT TWO. A pass that ran and left no floor anything to be judged on returned {}
    as well, so AN UNREADABLE LAYER LOG and NO LAYER PASS AT ALL published byte-identical
    payloads. MEASURED, SYNTHETIC, 2026-11-17: `_layers_published(layers.read_layer_log(""))` and
    `_layers_published({})` were both {}, and with `_near_wall` over each the two payloads were
    equal key for key - `near_wall_layers` 3, `near_wall_layers_measured` False,
    `near_wall_layers_asked` 3, `near_wall_levels` 3, `near_wall_cell_m` 2.5e-05 - so the panel
    drew the same rows for both. NEITHER READS AS A CLEAN PASS, so this was a completeness gap and
    never a false claim; what a reader could not do was tell "we grew layers and cannot tell you
    how they came out" from "we grew none", and those call for different actions - the first is a
    log to go back for, the second is a plan that asked for none. The two are distinct at the
    source (`conjugate.build` leaves `layer_report` {} unless `layers > 0`, and `read_layer_log`
    always returns its ten keys), so the distinction was thrown away HERE and nowhere else.

    WHAT THE UNREADABLE CASE PUBLISHES IS THE ABSENCE ITSELF: `layer_floors_unjudged` naming all
    four floors, `layer_floors_total` beside it, and not one figure or verdict more. Not
    `layer_short`, whose empty list is the one value a reader is entitled to read as a clean pass,
    and not `layer_iterations`, which is 0 on every unreadable log and says only that nothing was
    counted.

    THE SHORTFALL IS CARRIED, NOT RE-DERIVED. `layers.shortfalls` is evaluated once, inside
    `conjugate.build`, against the pass's own `cells_added` - a number no other artifact holds, so
    a second reader here could not get the same answer. That is exactly the "one quantity, two
    readers that disagreed" this engine has already paid for once (5% in a criterion against
    19.89% in a gate). The FIGURES go through `layers.measured`, which is the one spelling of
    their names, for the same reason.

    `layer_short` is a LIST so the declared criterion can be `op="empty"` and tell "the pass met
    every floor" from "nobody judged it": an absent key reads as unmeasured and never as a pass,
    where False would read as a pass on a run that grew nothing. WHICH ONLY WORKS IF THE ABSENT
    CASE IS ACTUALLY ABSENT, and until 2026-11-17 it was not: an unreadable log published that
    empty list beside a lone `layer_iterations: 0`.

    THE GUARD THAT KEEPS THE ABSENT CASE ABSENT IS THE `len(unjudged) >= len(LAYER_FLOORS)` TEST
    BELOW, and nowhere else. It is named here because two comment blocks in `render/mesh_facts`
    named a `_floors_had_a_reading` helper instead, which had no caller and is now gone (the note
    above `_layers_published` carries its measurement). The `if not out: return {}` that stood
    here before 2026-11-17 is gone too: it asked whether any FIGURE at all survived, which the
    iteration count always does, so it never fired on the one case it was there for.
    """
    from meshpipeline.engines.cfmesh_conjugate import layers as LY
    from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID

    # THE REPORT IS UNTRUSTED: it is JSON written on the far side of a container boundary, and a
    # non-dict here would take the whole manifest down over a figure about layers. The mesh is
    # the deliverable and is never lost over a reading beside it.
    if not isinstance(raw, dict) or not raw:
        return {}
    grown = LY.measured(raw, interface_patch=FLUID_TO_SOLID)
    # NO FIGURE AND NO VERDICT WHEN NOTHING WAS JUDGED - BUT NOT SILENCE, which is the answer a
    # run with no layer pass at all gives and a run whose log could not be read must not. Not the
    # iteration count, which is 0 on every unreadable log and says only that nothing was counted,
    # and above all not `layer_short`, whose empty list is the one value a reader is entitled to
    # read as a clean pass. The two tally keys are the whole of what such a pass can honestly
    # publish: a pass ran, and none of this engine's floors could be judged on what it left.
    unjudged = LY.floors_unjudged(grown)
    if len(unjudged) >= len(LY.LAYER_FLOORS):
        return {"layer_floors_unjudged": list(unjudged),
                "layer_floors_total": len(LY.LAYER_FLOORS)}
    out: dict = {k: grown[k] for k in _LAYER_FIGURES if grown.get(k) is not None}
    out["layer_short"] = [str(s) for s in (raw.get("shortfalls") or ())]
    # AND HOW MANY OF THE FOUR FLOORS THAT EMPTY LIST IS ACTUALLY ABOUT. An `[]` beside nothing
    # else says "judged, and it met every floor", and on a PARTLY readable log that is a claim
    # about three floors nobody evaluated. MEASURED, SYNTHETIC, 2026-11-17: a log holding only the
    # Extruding and Added lines published `layer_coverage_pct` 99.9, `layer_short` `[]` AND
    # `near_wall_layers_measured` False, so the one panel a customer reads said both "not
    # measured - this is what the mesher was asked to grow" and "no shortfall against this
    # engine's layer floors" about the same pass.
    #
    # BOTH KEYS, AND BOTH ALWAYS. The names say WHICH floors had nothing (`()` when all four did,
    # which is a reading and not an absence, so it goes through `mesh_facts._LINE_METRICS` beside
    # `layer_short`), and the total says how many there are - because a panel holding three names
    # and no denominator leaves "three of how many" to the reader, which is how "three of four"
    # came to be arithmetic a customer had to do for themselves. The total is published rather
    # than written into the viewer for the same reason every threshold here is: a constant with a
    # second reader on the far side of an HTTP boundary is a constant that can disagree.
    #
    # WHAT THIS TALLY IS, EXACTLY, NARROWED 2026-11-18. It is "which floors had no FIGURE to be
    # drawn on", INFERRED here from `grown`; it is not a record of which floors `shortfalls`
    # reached, and the two can part. `shortfalls` guards the layers-kept floor on
    # `asked and mesh_layers is not None`, where `asked` is `n_layers or row["target_layers"]`;
    # `floors_unjudged` sees only the figures, and `n_layers` travels in no artifact. MEASURED,
    # SYNTHETIC, 2026-11-18, on a log whose table row reads `fluid_to_solid 16582198 0 0 0 0`
    # with 0 cells added and 90,000 illegal faces:
    #
    #     shortfalls called with   floors it evaluated   this tally claims judged   error
    #     n_layers 0               2 (coverage, thick)   3                          +1
    #     n_layers 3               3 (+ layers kept)     3                          0
    #
    # THE +1 ROW IS NOT A SHAPE THE PRODUCER CAN EMIT, and that was measured too, not assumed:
    # `conjugate.build` builds a `layer_report` at all only under `if layers > 0` and passes
    # `n_layers=layers` into `shortfalls`, so `asked` is positive on every report that reaches
    # here, and a plan asking for no layers leaves `raw` {} - `_layers_published({})` is {} and
    # returns above. So the reachable row is the second one and the tally is exact on it.
    #
    # AND THERE ARE TWO PATCHES HERE, NOT ONE, AND THEY ARE NAMED AND MEASURED SEPARATELY. This
    # block said "AND THE OBVIOUS PATCH MAKES IT WORSE" and then argued about one of them without
    # saying which it meant - a conclusion with no mechanism beside it, in the file whose whole
    # subject is that a comment must name the mechanism that runs. Both are patches to the
    # layers-kept entry of `layers.LAYER_FLOORS`, both add `layers_asked` to it, and they differ
    # only in HOW it is required. RE-MEASURED, SYNTHETIC, 2026-11-18, on the log this block is
    # written on - table row `fluid_to_solid 16582198 0 0 0 0`, `Added 0 out of 0 cells (0%)`,
    # `Finished meshing with 90000 illegal faces` - through the production `read_layer_log`,
    # `measured` and `floors_unjudged`, with each roster rebuilt beside the live one:
    #
    #     patch to ("layers kept", ...)        unjudged                        tally says judged
    #     none (what ships)                    ('faces in error',)             3
    #     B: keys + ("layers_asked",)          ('faces in error',)             3
    #     A: B, and layers_asked also in       ('layers kept',                 2
    #        _FLOORS_NEED_A_POSITIVE_READING    'faces in error')
    #
    # PATCH B - ADD THE ASKED COUNT TO THE KEY TUPLE - MOVES NOTHING AT ALL, and it is the patch
    # the structure of that table invites: "faces in error" already carries TWO keys for exactly
    # this reason, a figure plus the number it is divided by, so the next reader's first move is
    # to give "layers kept" its second key the same way. It is inert because `measured()` keeps
    # `layers_asked` when `target_layers` is 0 - `0` is not `None` and not `""` - and `_judged_on`
    # accepts ANY non-None value for a key outside `_FLOORS_NEED_A_POSITIVE_READING`. So the
    # count is ZERO BUT PRESENT, the entry stays judged, and the unjudged roster is byte-identical
    # to the one that ships. A patch that looks like the fix and changes no output is worse than
    # none, because it closes the question.
    #
    # PATCH A - REQUIRE A POSITIVE ASKED COUNT - IS THE ONE THAT MAKES IT WORSE, and it is the one
    # the old sentence was about. It closes the first row of the table above (+1 -> 0) and OPENS
    # the second (0 -> -1): `shortfalls` takes its own `asked` from `n_layers or
    # row["target_layers"]`, so on the shape production emits (n_layers 3, target_layers 0 in the
    # log) it JUDGES the layers-kept floor and says the pass kept 0.00 of 3, while this tally
    # would name that same floor unjudged. MEASURED on the same log: `shortfalls` evaluates 2
    # floors at n_layers 0 and 3 at n_layers 3, against the 3 this tally claims either way. An
    # under-claim on the only shape production emits is worse than an over-claim on one it cannot.
    #
    # SO NEITHER WAS APPLIED, and the reason is now two reasons: one patch is inert and the other
    # trades a reachable error for an unreachable one. The asymmetry can only be closed where it
    # is created - by `shortfalls` reporting which floors IT evaluated instead of a second reader
    # inferring it from the figures - and that is `layers.py` and `conjugate.py`, not this file.
    out["layer_floors_unjudged"] = list(unjudged)
    out["layer_floors_total"] = len(LY.LAYER_FLOORS)
    # THE WHOLE CURVE, because on the real plate it is the reading that says what happened.
    # Job 0ff6f9e5 extruded 97.2267% at iteration 0 and the ten repair iterations took it DOWN
    # every single time - 90.2273, 89.6661, 89.2765, 89.1136, 88.9878, 88.8744, 88.7604, 88.6609,
    # 88.5826. The final figure alone says the pass missed a floor; the curve says the repair is
    # what missed it, which is a different defect and a different fix.
    curve = raw.get("coverage_by_iteration")
    if isinstance(curve, (list, tuple)) and curve:
        out["layer_coverage_by_iteration"] = [float(c) for c in curve]
    return out


def _resolution_short(plan: dict) -> list[str]:
    """Where the mesh is coarser across the passages than the product's flow rule asks.

    The rule is engines/passage.PASSAGE_CELLS_ACROSS, and this engine does not reach it on a real
    cold plate inside the cell budget (engines/cfmesh_conjugate/budget has the measurement). The
    shortfall is REPORTED here and gated nowhere: the bar that decides delivery is the coolant's
    measured volume error, because the staircase measurements show the cell count does not
    predict that error for any one part.
    """
    from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS

    asked = int(plan.get("cells_across") or 0)
    if asked <= 0 or asked >= PASSAGE_CELLS_ACROSS:
        return []
    narrowest = float(plan.get("narrowest_m") or 0.0)
    where = f" ({narrowest * 1e6:.4g} um wide)" if narrowest > 0 else ""
    # "HAS" WAS THE WRONG VERB. `cells_across` is what the plan ASKED the octree for, and the
    # octree delivers what fits: on a two-scale part the thin passages get the fine surface's
    # levels, not this number, and a cell that straddles a level boundary gets neither. Saying the
    # passage "has" it states a property of the delivered mesh from a number that was written
    # before the mesh existed. The measured reading is min_cell_m, which foam_exec takes from
    # checkMesh's own minimum cell volume - and the manifest already keys this one honestly, as
    # `cells_across_asked`.
    # WHOSE RULE IT IS, AND WHAT DECIDES INSTEAD. The bare count, under a ledger row whose
    # label says "the fidelity asks for", reads as an unmet requirement whatever the job is.
    # MEASURED, job 22ac7054's review of the real plate: it PASSED this axis on the coolant
    # error (0.472 of its tolerance) and still raised a concern that "the resolution should not
    # be represented as satisfying that flow-fidelity rule", because nothing it was shown said
    # whose rule 13 is. It is a FLOW count - `engines/passage` measures it across a pipe, for a
    # pressure drop and a coolant temperature rise - and this row is `gating=False` for a
    # measured reason: the staircase runs show the count does not predict the coolant volume
    # error on any one part (seven across came out fourteen times worse than six), so the bar
    # that decides delivery is that error. SAYING BOTH IS NOT AN EXCUSE FOR THE NUMBER: the
    # count is still reported unrounded, the error is still measured, and the error still gates.
    # What changes is that the sentence no longer states a requirement it does not mean.
    return [f"the plan asked for {asked} cells across the narrowest passage{where}, where the "
            f"flow rule asks for {PASSAGE_CELLS_ACROSS} - a flow-fidelity count, for a pressure "
            f"drop or a coolant temperature rise, and not the bar this delivery is decided on: "
            f"that is the coolant's own measured volume error, because the count does not "
            f"predict it on any one part"]


def finalize(workspace_dir, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    """The manifest for a delivered conjugate case, or a failure the executor can read.

    The seven positional arguments and the `success` key are the executor's, not this engine's.
    """
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.native import read_report
    from meshpipeline.engines.cfmesh_conjugate.slices import review_slices
    from meshpipeline.engines.manifest import write_manifest

    ws = Path(workspace_dir)
    report = read_report(ws)
    if not report:
        out = (f"[CFMESH_CONJUGATE] no readable {PL.REPORT_NAME} - the mesher did not finish")
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    if report.get("refused"):
        out = f"[CFMESH_CONJUGATE] the mesher refused this case: {report['refused']}"
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    if not report.get("regions"):
        out = ("[CFMESH_CONJUGATE] the report carries no region measurements - checkMesh never "
               "read this mesh, and it cannot be delivered unchecked")
        return {"success": False, "stdout": "", "stderr": "", "output": out}

    q = quality_of(ws, report)
    patch_types = delivered_boundary_types(ws, intake_patches)
    review = report.get("review") or {}
    # THE TWO NAMED BODIES the reviewer opens the surface by (cfmesh_conjugate/review_surface):
    # the coolant and the metal, each one physical group. Published beside the patch names so the
    # manifest's entity map covers everything the renderer can be asked for.
    entities = {str(k): list(v) for k, v in (review.get("groups") or {}).items()}

    lo, hi = _block(ws, report)
    write_manifest(
        ws,
        patch_types=patch_types,
        patch_entities={**{n: [] for n in patch_types}, **entities},
        bbox=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        quality=q,
        domain=domain or "conjugate heat transfer (a cooled solid)",
        body_bbox=(tuple(lo), tuple(hi)),
        mesh_bounds=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        # The COOLANT's volume, never the merged block: that merged export is the one thing a
        # conjugate review has to tell a real split apart from.
        volume_path=review.get("volume") or None,
        mesh_units=COMPLETED_MESH_UNIT.value,
        mesh_mode="cfmesh_conjugate",
        flow_topology=flow_topology,
        engine_params={**(engine_params or {}),
                       "_regions": [{"name": r["name"], "type": r["type"]} for r in q["regions"]]},
        # THE CUTS THE MESHER ACTUALLY DREW, when it drew any. They carry the window each frame
        # was rendered in, the um/px that came out of it and the picture itself, and the mesher is
        # the only side that can know those: it is the side with the interior. Recomputing them
        # here would publish a promise beside pictures that were made to a different scale.
        #
        # The fallback is the geometry alone, which is what this published before the cuts were
        # drawn on the VM at all, so an older report still publishes exactly what it used to.
        inspection_regions=(review.get("cuts")
                            or review_slices(lo, hi, inlet=_port(q, "inlet"),
                                             outlet=_port(q, "outlet"))),
    )
    out = _said(q)
    # SUCCESS IS "A MESH CAME OUT", NOT "THE MESH IS GOOD", and the difference cost this engine its
    # whole gate row.
    #
    # pipeline/executor.py wraps the declared gates in `if executor_success:`, and executor_success
    # is this key. So returning mesh_ok here answered the gates' question before they were asked:
    # on the first real customer cold plate - 99,172,391 cells, fluid 40,099,581 / solid 59,072,810,
    # a conformal 7,224,348-face interface, checkMesh reporting Mesh OK for both regions - the
    # coolant came out in 367 disconnected pieces, mesh_ok went False, and NOT ONE of the eight
    # gates ran. They did not fail; they never executed. The run was then reported as a mesher that
    # "stopped before it finished writing the mesh", which is the one thing that did not happen.
    #
    # EVERY CONJUNCT OF mesh_ok HAS ITS OWN GATE, which is why this loses no safety and gains
    # precision. report["ok"]'s piece count is _gate_coolant_pieces, which says "the coolant came
    # out as 367 separate piece(s) where the part's own geometry has 2"; regions_missing and
    # regions_undeclared are _gate_regions; interface_ok is _gate_interface; empty_patches is
    # _gate_named_patches; fatal is the quality floor. Each of those names the defect in the
    # engineer's language and carries its own FailureCause, where this boolean carried none.
    #
    # WHAT IS KEPT: `fatal` - a checkMesh fatal class - and whether a mesh was delivered at all.
    # That is exactly cfmesh/finalize.py's `success = not fatal` plus its guard for an incomplete
    # run, and it keeps a workspace with no mesh out of the gate row, where _no_mesh_cause can
    # still say which of the four things happened. mesh_ok itself stays in the quality block, where
    # its readers are.
    return {"success": a_mesh_came_out(q), "stdout": out, "stderr": "", "output": out}


def a_mesh_came_out(q: dict) -> bool:
    """Whether there is a mesh here for the gates to measure - NOT whether it is a good one.

    This is what the executor reads as `success`, and therefore what decides whether the declared
    gate row runs at all. Named rather than inlined so a test can call it: the first attempt at a
    test for this restated the expression in its own body, which passes whatever the code does.

    Two things only, both of which mean there is nothing further to measure:
      no region with cells   nothing was delivered, and the executor's no-mesh path can say which
                             of the four things happened instead of a gate crashing on an empty
                             workspace
      a fatal checkMesh class  the same line cfmesh/finalize.py draws with `success = not fatal`
    """
    return bool(q.get("regions")) and int(q.get("cells") or 0) > 0 and not q.get("fatal")


def _port(quality: dict, which: str) -> str:
    """The block face the coolant enters or leaves by, which is what names the cut across the flow.

    The BLOCK FACE and not the user's name for it: the slice needs the axis, and `coolant_in`
    carries none. `patch_names` maps one to the other.
    """
    ports = quality.get("ports") or {}
    named = str(ports.get(which) or "")
    if not named:
        return ""
    for face, name in (quality.get("patch_names") or {}).items():
        if name == named:
            return str(face)
    return named


def _block(ws: Path, report: dict):
    """The block the mesh fills, from the plan the host wrote.

    Falls back to the delivered points when a plan written before this field existed is read, and
    to the unit box only when neither is there - a bbox of zeros would publish a mesh with no
    extent, which every downstream reader of the manifest treats as a degenerate case.
    """
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    try:
        p = PL.read_plan(ws)
        if p.block_lo and p.block_hi:
            return list(p.block_lo), list(p.block_hi)
    except (OSError, ValueError):
        logger.info("conjugate finalize: no plan to read the block from")
    bounds = (report.get("review") or {}).get("bbox")
    if bounds and len(bounds) == 6:
        return [float(v) for v in bounds[:3]], [float(v) for v in bounds[3:]]
    logger.warning("conjugate finalize: neither the plan nor the review surface carries the "
                   "block - the manifest's extent is a unit box")
    return [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]


def _said(q: dict) -> str:
    """One line, in the numbers a reader of the build log would want."""
    bits = [f"cells={q['cells']:,}"]
    for r in q["regions"]:
        bits.append(f"{r['name']}={r['cells']:,}")
    if q.get("coolant_error_pct") is not None:
        bits.append(f"coolant={q['coolant_volume_m3'] * 1e9:.5g} mm3 "
                    f"({q['coolant_error_pct']:.2f}% from the CAD)")
    bits.append(f"interface={q['interface_faces_fluid']:,} faces both sides")
    # THE LAYERS AS GROWN, in the line a reader of the build log sees. Job 0ff6f9e5 grew 2.25 of
    # the 3 it asked for over 88.5826% of the interface and this line said nothing about layers at
    # all, so the only figure anyone had was the manifest's `near_wall_layers: 3`.
    if q.get("layers_in_mesh") is not None:
        bits.append(f"layers={q['layers_in_mesh']:.2f} of {q.get('layers_asked')} asked "
                    f"over {float(q.get('layer_coverage_pct') or 0.0):.4f}% of the interface "
                    f"at {float(q.get('layer_thickness_pct') or 0.0):.1f}% of the thickness")
    # AND WHERE IT FELL SHORT OF THIS ENGINE'S OWN FLOORS, in full rather than as a count. The
    # shortfall is NOT a refusal (layers.shortfalls says why: the pass has already moved the only
    # copy of the coolant mesh and rewritten it in place, so refusing throws away a finished mesh
    # nothing else had anything against) - so this line and the declared `layer_short` criterion
    # are the whole of how it is said, and a quiet pass is the one outcome that is not acceptable.
    for _line in q.get("layer_short") or ():
        bits.append(f"LAYER SHORTFALL: {_line}")
    # AND WHICH OF THE FOUR FLOORS THE LOG LEFT NOTHING TO JUDGE. Without this the line above is
    # read as the whole verdict, and on a partly readable log it is a verdict on one floor.
    if q.get("layer_floors_unjudged"):
        bits.append("LAYER FLOORS NOT JUDGED (" + str(len(q["layer_floors_unjudged"])) + " of "
                    + str(q.get("layer_floors_total")) + "): "
                    + ", ".join(q["layer_floors_unjudged"]))
    if q["fatal"]:
        bits.append(f"fatal={q['fatal'][:6]}")
    if q["empty_patches"]:
        bits.append(f"empty={q['empty_patches'][:6]}")
    if q.get("port_patches_dropped"):
        bits.append(f"planned but not in the mesh={q['port_patches_dropped'][:6]}")
    if q["native_problems"]:
        bits.append("problems: " + "; ".join(q["native_problems"][:4]))
    if not q.get("review_volume"):
        bits.append("NO per-region volume was exported: the reviewer cannot cut interior slices")
    return "[CFMESH_CONJUGATE] " + " ".join(bits)


__all__ = ["a_mesh_came_out", "delivered_boundary_types", "empty_patches", "finalize", "quality_of"]
