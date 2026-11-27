"""A layer pass is judged by what it grew, never by its exit code.

WHY THIS FILE EXISTS. With OpenFOAM's shipped `minVol 1e-13` - an ABSOLUTE volume - every face of
a geometrically perfect microchannel mesh is flagged on length scale alone, snappyHexMesh extrudes
nothing, prints "Finished meshing without any errors", checkMesh says "Mesh OK", and the process
exits 0. Measured on a 20x20x10 blockMesh at 22.0 um. A stage that trusts its return code
therefore reports a layered mesh and delivers the mesh it was given.

AND THAT IS NOT THE ONLY SILENT FAILURE. The delivered mesh's own pass extruded 99.7653% of the
patch at iteration 0 while holding 82,548 illegal faces, because the stack it was given did not
fit its background cell. Coverage alone cannot see that; the achieved thickness and the
illegal-face count can.

Every figure asserted here is read off the run that produced the delivered layered coolant on
2026-11-08 (`~/qa/bl8/snappy.log`, 102,536,327 cells, 65.4 min end to end), not from a summary.
"""
from pathlib import Path

import pytest

import meshpipeline.engines.determinism as det
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import layers as L
from meshpipeline.settings.env import ConfigurationError

#: THE MEASURED RUN'S CELL AT THE INTERFACE, which is what the stack is sized against - NOT the
#: background, which was 402.525 um, sixteen times bigger.
BL8_CELL_M = 25.1578e-06
BL8_BACKGROUND_M = 402.525e-06
BL8_LEVELS = 4
BL8_FIRST_M = 4.15e-06
BL8_EXPANSION = 1.2
BL8_MIN_THICKNESS_M = 3.0e-06
#: The DELIVERED mesh's dict, which did not fit its own cell and cost ten repair iterations.
SHIPPED_BACKGROUND_M = 20.1263e-06
SHIPPED_FIRST_M = 4.63e-06
SHIPPED_EXPANSION = 1.5
SHIPPED_MIN_THICKNESS_M = 4.0e-07

INTERFACE = "fluid_to_solid"

GOOD_LOG = """
Extruding 16153727 out of 16619777 faces (97.1958%)
Added 49859331 out of 49859331 cells (100%)
Extruding 15880692 out of 16619777 faces (95.553%)
Added 47232886 out of 49859331 cells (94.7323%)
patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
fluid_to_solid 16619777 3        2.84     1.43e-05  94.6
Layers added in = 2521.87 s.
Finished meshing with 2888 illegal faces
"""

#: THE SILENT FAILURE, from the measured reproducer. Note the reassuring last line.
ZERO_LAYER_LOG = """
    faces with face pyramid volume < 1e-13                 : 12800
Extruding 0 out of 1600 faces (0%)
Added 0 out of 4800 cells (0%)
patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
walls          1600     3        0        0         0
Finished meshing without any errors
"""

NO_LAYER_PHASE_LOG = """
Create mesh for time = constant
Read mesh in = 664.9 s
Finished meshing without any errors
"""

#: THE RUN THIS MODULE EXISTS TO REFUSE: the delivered dict's own iteration 0. Plenty of coverage,
#: the full stack, and 82,548 illegal faces because the stack did not fit the cell.
THINNED_LOG = """
Extruding 16580757 out of 16619777 faces (99.7653%)
Added 49859331 out of 49859331 cells (100%)
patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
fluid_to_solid 16619777 3        3.00     2.20e-05  41.2
Layers added in = 300.0 s.
Finished meshing with 82548 illegal faces
"""


def _geom(bg=BL8_CELL_M, n=None):
    g = L.geometry_for(bg)
    if n is not None:
        g["n_layers"] = n
    return g


# ---------------------------------------------------------------- the geometry, and the fit rule

def test_the_measured_stack_is_reproduced_exactly_from_the_interface_cell():
    got = L.geometry_for(BL8_CELL_M)
    assert got["n_layers"] == 3
    assert got["first_layer_m"] == pytest.approx(BL8_FIRST_M, rel=1e-9)
    assert got["expansion"] == pytest.approx(BL8_EXPANSION, rel=1e-9)
    # NOT the measured run's 3.0 um any more. That floor was a fifth of the stack, and a
    # fifth is what let the mover thin one five times over - see MIN_THICKNESS_OF_STACK.
    stack = L.stack_thickness_m(first_layer_m=BL8_FIRST_M, expansion=BL8_EXPANSION,
                                n_layers=3)
    assert got["min_thickness_m"] == pytest.approx(L.MIN_THICKNESS_OF_STACK * stack,
                                                   rel=1e-9)
    assert got["min_thickness_m"] > BL8_MIN_THICKNESS_M


def test_the_measured_stack_takes_sixty_per_cent_of_one_cell_and_fits():
    stack = L.stack_thickness_m(first_layer_m=BL8_FIRST_M, expansion=BL8_EXPANSION, n_layers=3)
    assert stack == pytest.approx(15.106e-06, rel=1e-3)
    assert stack / BL8_CELL_M == pytest.approx(0.6004, abs=1e-3)
    assert L.fit_complaint(interface_cell_m=BL8_CELL_M, **_geom()) == ""


def test_the_stack_is_sized_against_the_cell_it_grows_in_not_the_background():
    """A FACTOR OF SIXTEEN, and the fit rule could not see it.

    The layers grow on the conjugate interface, whose cells are the background divided by the
    octree levels under it: on the run plan 402.5570 / 2**4 = 25.1598 um. Sized against the
    background, the first layer came out at 66.4053 um and the stack at 241.7153 um into 25.1598 um
    cells - the delivered mesh's own defect, ten times worse. And `fit_complaint` compared the
    stack to the same wrong cell, so both errors cancelled and the share read exactly 60.0% either
    way.
    """
    assert L.interface_cell_m(BL8_BACKGROUND_M, BL8_LEVELS) == pytest.approx(BL8_CELL_M, rel=1e-4)
    at_interface = L.geometry_for(L.interface_cell_m(BL8_BACKGROUND_M, BL8_LEVELS))
    assert at_interface["first_layer_m"] == pytest.approx(BL8_FIRST_M, rel=1e-3)

    at_background = L.geometry_for(BL8_BACKGROUND_M)
    assert at_background["first_layer_m"] == pytest.approx(BL8_FIRST_M * 16, rel=1e-3), (
        "this is the wrong number, and it is 2**levels out")
    # ...and the fit rule reads BOTH as 60.0% of their own cell, which is why the name matters
    for cell, geom in ((BL8_CELL_M, at_interface), (BL8_BACKGROUND_M, at_background)):
        assert L.fit_complaint(interface_cell_m=cell, **geom) == ""


def test_the_dict_that_shipped_is_refused_because_its_stack_did_not_fit_its_cell():
    said = L.fit_complaint(first_layer_m=SHIPPED_FIRST_M, expansion=SHIPPED_EXPANSION, n_layers=3,
                           min_thickness_m=SHIPPED_MIN_THICKNESS_M,
                           interface_cell_m=SHIPPED_BACKGROUND_M)
    assert said and "109" in said and "does not fit" in said


def test_a_floor_under_what_min_face_weight_allows_is_refused():
    said = L.fit_complaint(first_layer_m=2.0e-06, expansion=1.2, n_layers=3,
                           min_thickness_m=4.0e-07, interface_cell_m=SHIPPED_BACKGROUND_M)
    assert "minFaceWeight" in said


def test_the_renderers_own_fit_guard_can_actually_fire():
    """IT COULD NOT. The first draft recovered the background cell from
    `first_layer_m / FIRST_LAYER_FRACTION`, which makes the share self-referential: it came out at
    0.600450 for a first layer of 1e-9 and of 1e-3 alike, so the only guard inside the public
    renderer was decorative. The cell is the caller's to supply."""
    with pytest.raises(L.LayerError) as got:
        L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                             n_layers=3, first_layer_m=1e-3, expansion=1.2,
                             min_thickness_m=3.0e-06)
    assert "does not fit" in str(got.value)


def test_the_most_layers_that_can_ever_fit_is_derived_and_is_three():
    assert L.max_layers() == 3
    assert L.stack_thickness_m(first_layer_m=L.FIRST_LAYER_FRACTION,
                               expansion=L.EXPANSION_RATIO, n_layers=4) > L.MAX_STACK_FRACTION


# ---------------------------------------------------------------- what the host has to price

def test_the_layer_pass_is_priced_from_what_it_measured_on_the_real_mesh():
    """The coolant went 55,303,441 cells to 102,536,327 and the whole two-region mesh 223,191,421
    to 270,424,307. Every size decision - the plan, the octree ceiling, the machine, its disk, its
    lifetime, whether the archive comes home - was priced before this stage existed, and the
    manifest gate re-checks the DELIVERED count against the same limit AFTER the chain."""
    assert L.cells_multiplier(0) == 1.0
    # THE WHOLE MESH, not the coolant. The coolant grows 1.854x and the whole two-region mesh
    # 1.212x, because the coolant is a quarter of this part and the metal around it does not move.
    # The cell limit, the estimate, the machine and the archive are all about the whole mesh, and
    # using the coolant's figure over-priced the run by 53% - enough to refuse, at the limit
    # already configured, the very plan the hand-built mesh was built from.
    assert L.cells_multiplier(3) == pytest.approx(270_424_307 / 223_191_421, rel=0.01)
    assert L.coolant_multiplier(3) == pytest.approx(102_536_327 / 55_303_441, rel=0.01)
    assert L.cells_multiplier(3) < L.coolant_multiplier(3)
    assert L.cells_multiplier(3) > L.cells_multiplier(1) > 1.0


def test_the_layered_plate_still_fits_the_budget_that_is_already_configured():
    """With the coolant's factor it did not, and the refusal was correct arithmetic on the wrong
    number: 400,000,000 / 1.854 is 215,633,423 against an estimate of 257,011,785."""
    assert 400_000_000 / L.cells_multiplier(3) > 257_011_785
    assert 400_000_000 / L.coolant_multiplier(3) < 257_011_785


def test_the_seconds_estimate_reproduces_the_measured_pass():
    assert L.seconds_estimate(3, coolant_cells=55_303_441) == pytest.approx(2521.87, rel=0.02)
    assert L.seconds_estimate(0, coolant_cells=55_303_441) == 0.0


# ---------------------------------------------------------------- the dictionaries

def test_the_quality_block_is_written_out_and_never_included():
    """`#include "meshQualityDict"` is what the measured dict used and what the product cannot use:
    foam_case_guard refuses any active preprocessor directive, and that refusal fires again INSIDE
    foam_exec.check_mesh - so a finished mesh of hundreds of millions of cells would be thrown
    away on a dictionary, after every stage had succeeded."""
    from meshpipeline.sandbox import foam_case_guard as G

    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert "#include" not in text
    # The floor is derived from the cell now, not a literal - see
    # `test_a_relaxed_iteration_may_accept_a_worse_shape_but_not_a_thinner_cell`.
    assert "minVol" in text and f"{L.min_vol_for(BL8_CELL_M):g}" in text
    assert not G._HASH_DIRECTIVE.search(text), "the case guard would refuse this dict"
    for name, body in list(L.render_support_dicts().items()) + [
            ("meshQualityDict", L.render_mesh_quality_dict(BL8_CELL_M)),
            ("decomposeParDict", L.render_decompose_dict(8))]:
        assert not G._HASH_DIRECTIVE.search(body), name


def test_the_dict_carries_the_one_setting_that_decides_whether_anything_extrudes():
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert f"{L.min_vol_for(BL8_CELL_M):g};" in text
    assert L.min_vol_for(BL8_CELL_M) < 1e-13, "OpenFOAM shipped value extrudes nothing here"
    assert "-1e30" not in text and "minTetQuality         1e-30;" in text


def test_the_dict_keeps_the_blocks_snappy_refuses_to_run_without():
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert "castellatedMesh false;" in text and "castellatedMeshControls" in text
    assert "addLayers       true;" in text and "snap            false;" in text
    assert f"maxGlobalCells      {L.MAX_GLOBAL_CELLS};" in text
    assert L.MAX_GLOBAL_CELLS < 2 ** 31, "the label size caps the dict entry, not only the mesh"


def test_the_inert_point_is_a_fixed_origin_and_says_so():
    """It was computed by reading the whole coolant surface into memory - a multi-gigabyte list and
    minutes of wall clock immediately before the mesher's own memory peak - for a value the dict
    itself documents as never read, because castellatedMesh is false."""
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert "locationInMesh (0 0 0);" in text
    assert "not read" in text


def test_only_the_interface_is_named_and_the_ports_are_left_to_slide():
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert f"layers {{ {INTERFACE} {{ nSurfaceLayers 3; }} }}" in text
    assert "port_a" not in text and "port_b" not in text


def test_the_surface_normals_are_smoothed_and_the_other_smoother_is_still_off():
    """The two normal smoothers are not one knob, and only one of them does anything.

    This used to assert both were off, on the strength of a variant that turned
    `nSmoothSurfaceNormals` AND `nSmoothNormals` off together and came back better on six figures.
    All six still stand and none of them is the concave count, which is the largest thing the
    layer pass makes. Swept one at a time on the two-across plate with the pinch repair in:

        nSmoothNormals        0 -> 2    concave 37,677 -> 37,651     a tenth of a per cent
        nSmoothSurfaceNormals 0 -> 5    concave 37,677 -> 20,046     weight faces 209 -> 66

    So the knob the old rule was written about was never the one doing the damage. On THAT plate 5
    is the optimum of 0/2/3/5/8/10 and the curve turns back up past it.

    THAT PLATE IS TWO CELLS ACROSS, so it was re-swept 2026-11-26 over channel
    WIDTH, on a 45-degree diagonal lattice at this engine's own 201.3 um pitch and 25.1578 um cell
    with the 3x y-stretch, one knob off the shipped dict:

        cells across    nSmoothSurfaceNormals 0          nSmoothSurfaceNormals 5
                        cover layers thick concave       cover layers thick concave
             2           100   2.81   97.6   1234        99.96  2.81   97.6    772
             3           100   1.83   81.2    480        75.18  0.784  45.3    358
             4           100   3.00  100.0      0        75.79  0.808  46.4    332
             6           100   3.00  100.0      0        76.52  0.822  47.1    284
            10           100   3.00  100.0      0        76.43  0.825  47.1    168

    Read DOWN the three columns that are the deliverable - coverage, layers kept of three,
    thickness achieved - and 0 is never worse at ANY width, while from four cells across it is
    perfect and 5 discards a quarter of the interface and over half the stack at every width. At
    two across the two are identical on all three and differ only in the concave count, which is
    what the old sweep optimised. Job 22ac7054's part is 3.91 cells across its thin passages and
    10.50 across its broad ones, so hardly any of its interface is in the regime 5 was fitted in.

    AND THEN IT WAS MEASURED ON THE PART AND THE RIG LOST. Jobs 22ac7054 and b8f09ae9 are the
    same cold plate on the same plan with this constant the only difference - identical solid
    region, 192,087,711 cells in both. At 5: 90.6422% coverage, 2.00 layers, 79.8% thickness,
    1,340,185 concave. At 0: 90.7174%, 2.00, 79.9%, 2,341,980 concave, and 138 seconds slower. The
    three deliverable columns did not move and the concave count nearly doubled, so it is 5.

    THE RIG COULD NOT HAVE TRANSFERRED, which is the lesson and not the excuse. b8f09ae9 recorded
    what the extruder was ever offered: 91.1145% offered, 99.5641% of that kept. 8.89 of the 9.28
    missing points were declined up front by `featureAngle 120` and 0.40 were lost in repair. A
    smoother only acts on a face that was offered, so on this part these three knobs cannot reach
    more than 0.40 of a point however they are set. The rig had almost nothing declined, which is
    why it showed a 25-point coverage swing where the part has four tenths of one available.

    nSmoothThickness stays at 10: it smooths how THICK the stack is, not which way it points, so
    the staircase argument never reached it and no measurement has moved it. (At
    nSmoothSurfaceNormals 0 it was inert - nSmoothThickness 0 measured byte-identical there - but
    the surface normals are smoothed again, so it is live.)
    """
    assert L.SMOOTH_SURFACE_NORMALS == 5, (
        "0 was measured on the part and lost: same plan, same solid to the cell, 1,340,185 "
        "concave cells at 5 against 2,341,980 at 0, for 0.075 of a coverage point")
    assert L.SMOOTH_NORMALS == 0, (
        "nSmoothNormals was measured to do nothing; turning it on buys nothing and is untested")
    assert L.SMOOTH_THICKNESS == 10


def test_the_smoothing_was_swept_over_width_and_then_measured_on_the_part():
    """THE OPPOSITE FAILURE TO GUARD, which is re-fitting this constant on one geometry again.

    The value shipped at 5 from a sweep on a two-across plate that minimised the concave count
    there. A width sweep then said 0 is never worse at 2/3/4/6/10 cells across and perfect from 4,
    so it was changed to 0 and run on the part - where it bought 0.075 of a coverage point, moved
    layers kept and thickness not at all, and nearly doubled the concave count. It is 5 again.

    SO THE BAR FOR MOVING IT IS NOW A RUN OF THE PART, not a rig: a rig with no port cuts, no
    mouths and no pinch-bridged metal has almost no faces declined by `featureAngle`, and on the
    real part 8.89 of the 9.28 missing coverage points are declined before any smoother is
    consulted. Whoever moves this next has to show the two conjugate_report.json files.
    """
    assert L.SMOOTH_SURFACE_NORMALS == 5
    assert L.SMOOTH_NORMALS == 0
    assert L.SMOOTH_THICKNESS == 10, (
        "nSmoothThickness scales the stack, it does not aim it; nothing has measured it")
    # and the dict the mesher reads carries all three, because a constant nothing renders is a note
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M, **_geom())
    for line in ("nSmoothSurfaceNormals       5;",
                 "nSmoothNormals              0;",
                 "nSmoothThickness           10;"):
        assert line in text, line


def test_the_mover_may_stop_at_a_step_instead_of_carrying_a_layer_round_it():
    """`featureAngle` 180 means never treat an edge as a feature, so the mover is required to
    extrude around every 90-degree step of the staircase, including the steps where it cannot.

    Measured on the two-across plate: 180 against 120 is 1,796 faces of 506,322 not extruded -
    0.35% of the interface - and those faces were producing the mesh's worst cells. Max aspect
    ratio 37.49 -> 19.30, max non-orthogonality 59.30 -> 47.43, interpolation-weight faces
    209 -> 34. 90 excludes 5,480 more for nothing but a concave count that doubles.
    """
    assert L.FEATURE_ANGLE == 120
    # not 180, which is the "never stop" the sweep was run against...
    assert L.FEATURE_ANGLE < 180
    # ...and not below the 90 degrees a staircase step actually makes, where the count doubles
    assert L.FEATURE_ANGLE > 90


def test_the_rendered_dict_carries_the_measured_values_not_just_the_constants():
    """The constants are only worth anything if they reach the dict snappyHexMesh reads."""
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    assert "nSmoothSurfaceNormals       5;" in text
    assert "nSmoothNormals              0;" in text
    assert "nSmoothThickness           10;" in text
    assert "featureAngle              120;" in text


def test_the_feature_angle_in_the_dict_follows_the_constant():
    """It was a literal in the renderer, so the sweep's answer could be written down in the module
    and never reach the mesher."""
    import re
    text = L.render_layers_dict(interface_patch=INTERFACE, interface_cell_m=BL8_CELL_M,
                                **_geom())
    m = re.search(r"featureAngle\s+(\d+);", text)
    assert m and int(m.group(1)) == L.FEATURE_ANGLE


def test_the_decomposition_is_scotch_with_no_coefficients():
    text = L.render_decompose_dict(8)
    assert "numberOfSubdomains 8;" in text and "method scotch;" in text
    assert "coeffs" not in text and "hierarchical" not in text


def test_the_layer_case_is_written_in_ascii_because_the_readers_refuse_binary():
    assert "writeFormat     ascii;" in L.render_support_dicts()["controlDict"]


def test_the_stage_is_named_after_the_tool_so_a_customer_sees_a_line(tmp_path):
    """`engines/mesher_stage.read_stage` derives the line shown while a stage runs from its LOG
    STEM: `snappyHexMesh` sends it to the phase table that already has "adding wall layers" in it,
    and any other name falls through to nothing. A stage called `addLayers` writes addLayers.log,
    matches nothing, and the longest stage of the chain - 42 minutes of 65 - runs with no line at
    all."""
    from meshpipeline.engines.mesher_stage import read_stage

    (tmp_path / f"{L.LAYER_STAGE}.log").write_text(
        "Shrinking and layer addition phase\nExtruding 9 out of 10 faces (95.5%)\n")
    got = read_stage(tmp_path)
    assert got is not None, f"a {L.LAYER_STAGE}.log must name a stage"
    assert got["key"] == "layers"
    assert got["label"] == "adding wall layers"

    (tmp_path / f"{L.LAYER_STAGE}.log").unlink()
    (tmp_path / "addLayers.log").write_text(
        "Shrinking and layer addition phase\nExtruding 9 out of 10 faces (95.5%)\n")
    assert read_stage(tmp_path) is None, (
        "this is what the stage name used to be, and it is why it had no line")


# ---------------------------------------------------------------- the shell, promote and demote

def test_the_script_promotes_the_region_runs_and_puts_it_back():
    s = L.case_script(ranks=8, region="fluid", dict_dir="system/layers", mpi="MPIRUN_HERE")
    assert "set -e" in s
    assert 'mv "$p/constant/fluid/polyMesh" "_layers/$p/constant/polyMesh"' in s
    assert 'mv "_layers/$p/constant/polyMesh" "$p/constant/fluid/polyMesh"' in s
    assert s.index("constant/fluid/polyMesh") < s.index("MPIRUN_HERE") < s.rindex(
        "constant/fluid/polyMesh")


def test_the_script_never_deletes_a_layer_case_that_holds_a_mesh():
    """THE PROMOTION IS A RENAME, so after a failed pass the only copy of the coolant is inside the
    layer case. A first draft began with `rm -rf` on it, so a rerun DELETED THE MESH and then
    reported a missing file."""
    s = L.case_script(ranks=8, region="fluid", dict_dir="system/layers", mpi="x")
    assert "already holds a mesh from an earlier pass" in s
    assert s.index("already holds a mesh") < s.index("rm -rf"), (
        "the deletion must be guarded by the test, not the other way round")


def test_the_script_refuses_to_nest_the_layered_mesh_inside_a_stale_one():
    """`mv src dst` with dst an existing directory moves src INSIDE it, so a stale
    constant/fluid/polyMesh turned the layered mesh into polyMesh/polyMesh - and the guard that
    followed, which tested for polyMesh/owner, passed on the stale one. The reconstruct then
    merged the UNLAYERED mesh and the delivery reported layers it did not have."""
    s = L.case_script(ranks=8, region="fluid", dict_dir="system/layers", mpi="x")
    assert 'test ! -e "$p/constant/fluid/polyMesh"' in s
    assert "is in the way of the layered mesh" in s


def test_the_script_tests_for_processor_directories_before_globbing_them():
    """An unmatched glob stays literal, so `mkdir -p "_layers/processor*/constant"` succeeded and
    left a directory called `processor*`; the count guard that was supposed to catch it came after
    the mv that had already failed."""
    s = L.case_script(ranks=8, region="fluid", dict_dir="system/layers", mpi="x")
    assert "ls -d processor0" in s
    assert s.index("ls -d processor0") < s.index("for p in processor*")
    assert "no processor directories to layer" in s


def test_the_script_checks_every_rank_came_back():
    s = L.case_script(ranks=8, region="fluid", dict_dir="system/layers", mpi="x")
    assert "not every rank was put back" in s


# ---------------------------------------------------------------- the seam, through build()

def _stage_dicts(tmp_path, monkeypatch, **kw):
    seen = {}
    monkeypatch.setattr(C, "scan_case_dicts",
                        lambda ws: seen.setdefault("scanned", True) and "stop here")
    with pytest.raises(C.ConjugateError):
        C.build(tmp_path, background_cell_m=BL8_BACKGROUND_M, **kw)
    return Path(tmp_path), seen


def _fluid_stl(tmp_path):
    tri = tmp_path / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    (tri / "fluid.stl").write_text(
        C.block_surface_stl((0.0, 0.0, 0.0), (0.001, 0.002, 0.003)))


def test_the_layer_dicts_are_on_disk_before_the_guard_ever_scans(tmp_path, monkeypatch):
    _fluid_stl(tmp_path)
    ws, seen = _stage_dicts(tmp_path, monkeypatch, ranks=8, layers=3)
    assert seen.get("scanned"), "the scan must have run, or this proves nothing about order"
    d = ws / "system" / "layers"
    for name in ("snappyHexMeshDict", "meshQualityDict", "decomposeParDict",
                 "controlDict", "fvSchemes", "fvSolution"):
        assert (d / name).is_file(), f"{name} was not written before the scan"
    assert "#include" not in (d / "snappyHexMeshDict").read_text()
    assert "numberOfSubdomains 8;" in (d / "decomposeParDict").read_text()


def test_a_part_asking_for_no_layers_gets_no_layer_dicts(tmp_path, monkeypatch):
    _fluid_stl(tmp_path)
    ws, _ = _stage_dicts(tmp_path, monkeypatch, ranks=8)
    assert not (ws / "system" / "layers").exists()


def test_the_real_case_guard_accepts_what_build_wrote(tmp_path, monkeypatch):
    from meshpipeline.sandbox.foam_case_guard import scan_case_dicts

    _fluid_stl(tmp_path)
    ws, _ = _stage_dicts(tmp_path, monkeypatch, ranks=8, layers=3)
    assert scan_case_dicts(ws) in ("", None), scan_case_dicts(ws)


def test_a_stack_that_cannot_fit_refuses_before_any_stage_runs(tmp_path, monkeypatch):
    """IT TAKES FOUR LAYERS TO REACH IT, and that is worth saying. `geometry_for` scales the three
    thicknesses WITH the background cell, so at three layers the stack is 60.0% of a cell on every
    part at every resolution and the fit rule cannot fire however coarse or fine the mesh is - the
    delivered dict's defect is impossible to re-create through this path. What the check in `build`
    still catches is a LAYER COUNT that does not fit, which is the one part of the geometry the
    caller chooses: four layers is 88.5% of a cell. `determinism.conjugate_layers` refuses four at
    startup as well, so this is the second of two guards and not the only one."""
    _fluid_stl(tmp_path)
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "")
    for bg in (1e-05, BL8_BACKGROUND_M, 1e-03):
        assert L.fit_complaint(interface_cell_m=bg, **_geom(bg)) == "", (
            "three layers must fit any cell, because the stack is a fraction of it")
    with pytest.raises(C.ConjugateError) as got:
        C.build(tmp_path, background_cell_m=BL8_BACKGROUND_M, ranks=8, layers=4)
    assert "does not fit" in str(got.value)
    assert "88" in str(got.value)


def test_a_serial_plan_grows_no_layers_and_is_not_refused(tmp_path, monkeypatch):
    """`ranks_for` returns 1 for everything at or below the per-process octree ceiling, which is
    every part in the corpus and every mesh so far delivered. Refusing here would turn one
    deployment-wide setting into a corpus-wide outage, each failure filed as a resolution problem
    and told to the customer as a fault in their file."""
    _fluid_stl(tmp_path)
    ws, _ = _stage_dicts(tmp_path, monkeypatch, ranks=1, layers=3)
    assert not (ws / "system" / "layers").exists(), (
        "a serial plan must grow no layers, and must not be refused for it")
    assert L.serial_complaint(1) and L.serial_complaint(8) == ""


def test_the_stage_lands_between_the_split_and_the_reconstruct():
    """And nowhere else. Before the split there is no interface to extrude from; after the
    reconstruct the pass is serial, which the measured runs put at 10,038 s against 2,522."""
    names = [n for n, _s in C._layer_chain(8, 3)]
    assert names == [L.LAYER_STAGE]
    assert C._layer_chain(8, 0) == ()
    assert C._layer_chain(1, 0) == ()


# ---------------------------------------------------------------- the gate

def test_the_measured_run_passes_the_gate_and_reads_back_its_own_figures():
    got = L.read_layer_log(GOOD_LOG)
    assert got["coverage_pct"] == pytest.approx(95.553)
    assert got["cells_added"] == 47_232_886
    assert got["illegal_faces"] == 2888
    assert got["seconds"] == pytest.approx(2521.87)
    assert got["patches"][INTERFACE]["mesh_layers"] == pytest.approx(2.84)
    assert got["patches"][INTERFACE]["thickness_pct"] == pytest.approx(94.6)
    assert L.complain(got, interface_patch=INTERFACE, n_layers=3,
                      cells=102_536_327) == ""


def test_the_silent_zero_layer_failure_is_caught_although_it_exits_zero():
    got = L.read_layer_log(ZERO_LAYER_LOG)
    assert got["coverage_pct"] == 0.0
    said = L.complain(got, interface_patch="walls", n_layers=3)
    assert said and ("0%" in said or "no layers" in said)


def test_a_pass_that_never_reached_the_layer_phase_is_caught():
    got = L.read_layer_log(NO_LAYER_PHASE_LOG)
    assert got["iterations"] == 0
    said = L.complain(got, interface_patch=INTERFACE, n_layers=3)
    assert "no extrusion at all" in said
    assert "1e-13" in said, "the reason names the setting that causes it"


def test_the_run_that_extruded_plenty_and_thinned_to_hairlines_is_REPORTED_not_refused():
    """THE ONE THE FIRST GATE LET THROUGH, and it is a reading rather than a verdict.

    The delivered dict's own iteration 0: 99.7653% coverage, the full three layers, and 82,548
    illegal faces, because the stack did not fit its cell. Coverage alone cannot see that, so the
    thickness and the illegal-face count are read too.

    BUT NOT AS A REFUSAL. Those thresholds come from ONE hand-built run of ONE part, and by the
    time they can be read the pass has already moved the only copy of the coolant and rewritten
    it - so refusing here throws away a finished mesh that nothing else had anything against. The
    figures come home through `measured` and the judgement is made on the mesh.
    """
    got = L.read_layer_log(THINNED_LOG)
    assert got["coverage_pct"] == pytest.approx(99.7653)
    assert got["patches"][INTERFACE]["mesh_layers"] == pytest.approx(3.0)
    assert L.complain(got, interface_patch=INTERFACE, n_layers=3, cells=49_859_331) == ""
    short = L.shortfalls(got, interface_patch=INTERFACE, n_layers=3, cells=49_859_331)
    assert any("41.2% of the thickness" in s for s in short), short
    assert any("per million" in s for s in short), short


def test_too_many_faces_in_error_is_reported():
    got = L.read_layer_log(GOOD_LOG.replace("2888 illegal", "900000 illegal"))
    assert L.complain(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327) == ""
    short = L.shortfalls(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert any("faces in error" in s and "per million" in s for s in short), short


def test_far_fewer_layers_than_asked_is_reported():
    got = L.read_layer_log(GOOD_LOG.replace("3        2.84", "3        1.10"))
    assert L.complain(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327) == ""
    short = L.shortfalls(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert any("1.10 layers of the 3" in s for s in short), short


def test_a_pass_that_kept_no_layers_at_all_is_still_refused():
    """The line between the two: NOTHING GREW is a refusal, grew-less-well is a reading."""
    got = L.read_layer_log(GOOD_LOG.replace("3        2.84", "3        0.00"))
    said = L.complain(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert "0.0 layers in the mesh against 3" in said


def test_a_pass_that_kept_no_layers_at_all_misses_the_layers_floor_in_the_record_too():
    """THE DEFECT, 2026-11-17. `shortfalls` guarded this floor on `0.0 < mesh_layers`, so the
    largest miss it can have - a pass that grew NOTHING - fell through the one test that exists to
    catch it, and the record came back one sentence short of the truth.

    MEASURED, SYNTHETIC. The log below is GOOD_LOG's table with the layer count, the thickness and
    the coverage taken to zero and 90,000 illegal faces left behind. THREE of the four floors are
    missed; before the defect was fixed the list had TWO sentences and the silent one was the
    layer count itself.

    THE FOURTH CANNOT BE MISSED BY THIS LOG, and that is correct rather than a gap. The floor
    that replaced the coverage one is KEPT_OF_OFFERED_FLOOR_PCT - of the faces the extruder
    OFFERED, how many kept their layers - and taking every `Extruding` line to 0.0001% makes a
    pass that was offered almost nothing and kept all of it, which is 100%. A pass that grew
    nothing is what `complain` refuses outright (the test above), not something a floor on the
    mover's success rate should pretend to catch.

    `complain` does refuse such a run inside the mesher (the test above holds that, and it is why
    this was never a defect in a DELIVERED mesh) - but the report can reach `finalize` across a
    container boundary with `complain` never consulted, and a record that is three-quarters right
    is the thing this engine has already been caught publishing once.
    """
    zero = (GOOD_LOG.replace("3        2.84     1.43e-05  94.6", "3        0        0         0")
            .replace("(95.553%)", "(0.0001%)").replace("2888 illegal", "900000 illegal"))
    got = L.read_layer_log(zero)
    assert got["patches"][INTERFACE]["mesh_layers"] == 0.0, "the premise: a zero in the table"
    short = L.shortfalls(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert any("kept 0.00 layers of the 3" in s for s in short), (
        "a pass that grew no layers at all is not reported as missing this engine's layers "
        f"floor, which is the floor it missed by the whole of it: {short!r}")
    assert len(short) == len(L.LAYER_FLOORS) - 1, (
        f"four floors were missed and the record names {len(short)}: {short!r}")


def test_a_missing_layer_table_is_still_not_a_zero_layer_reading():
    """The other side of the same guard, and why the presence test is `is not None` rather than
    `> 0.0` being deleted. A log with no layer table has no reading of the count at all, so this
    floor must stay silent - `floors_unjudged` is what says so, by name."""
    no_table = GOOD_LOG[:GOOD_LOG.index("patch          faces")]
    got = L.read_layer_log(no_table)
    assert not got["patches"], "the premise: the parser found no layer table"
    short = L.shortfalls(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert not any("layers of the 3" in s for s in short), (
        f"a floor with no figure behind it was judged anyway: {short!r}")
    assert "layers kept" in L.floors_unjudged(L.measured(got, interface_patch=INTERFACE)), (
        "and the floor nobody could judge is not named as unjudged")


def test_the_layers_floor_is_met_by_exact_equality_and_that_is_the_rule_not_an_accident():
    """CHECKED, NOT ASSUMED. The real plate kept 2.25 of 3 and `LAYERS_FLOOR_FRACTION * 3` is
    2.25, so this floor is decided at the boundary - and `2.25 < 2.25` is False, which is why the
    delivered record says THREE floors missed and not four.

    STRICTLY LESS THAN IS RIGHT. The floor is "at least 75% of the ask": 75% exactly meets it, and
    a `<=` here would report a shortfall against a bar the pass reached. Nor is the equality a
    float tail that could fall either way on another host - 0.75 is 3/4 and 2.25 is 9/4, both
    exactly representable in binary, so the product is exact and this comparison is the rule.
    REAL PLATE (job 0ff6f9e5) figures; neither SURFACE nor VOLUME - a reading of its layer log.
    """
    assert L.LAYERS_FLOOR_FRACTION * 3 == 2.25, "the boundary this case sits on is not exact"
    at = GOOD_LOG.replace("3        2.84", "3        2.25")
    short = L.shortfalls(L.read_layer_log(at), interface_patch=INTERFACE, n_layers=3,
                         cells=102_536_327)
    assert not any("layers of the 3" in s for s in short), (
        f"75% of the ask exactly is reported as falling under a 75% floor: {short!r}")
    # and a hair under it IS a shortfall, so the boundary is where it is claimed to be
    under = GOOD_LOG.replace("3        2.84", "3        2.24")
    short = L.shortfalls(L.read_layer_log(under), interface_patch=INTERFACE, n_layers=3,
                         cells=102_536_327)
    assert any("kept 2.24 layers of the 3" in s for s in short), (
        f"and one hundredth of a layer under the floor is not reported: {short!r}")


def test_the_measured_run_has_nothing_to_report():
    assert L.shortfalls(L.read_layer_log(GOOD_LOG), interface_patch=INTERFACE, n_layers=3,
                        cells=102_536_327) == []


# ------------------------------------------- which of the four floors had a figure at all

def test_a_log_that_yielded_every_figure_leaves_no_floor_unjudged():
    """`()` is the reading that lets an empty shortfall list mean what `render/mesh_facts` says it
    means. Without it `[]` is two opposite answers wearing the same clothes."""
    got = L.measured(L.read_layer_log(GOOD_LOG), interface_patch=INTERFACE)
    assert L.floors_unjudged(got) == (), (
        f"a complete log is reported as having floors nobody judged: {L.floors_unjudged(got)!r}")


def test_a_log_holding_only_the_extrusion_lines_names_the_three_floors_it_could_not_judge():
    """THE PARTIAL READ, which is the case `finalize._layers_published`' all-four-unjudged return
    by design lets through. SYNTHETIC - the plate's first extrusion line and its cell count, with
    the layer table and the illegal-face line cut off, which is what a stage killed mid-write
    leaves behind."""
    truncated = ("Extruding 16122330 out of 16582198 faces (99.9%). Removed extrusion at 0 faces.\n"
                 "Added 49746594 out of 49746594 cells (100%).\n")
    got = L.measured(L.read_layer_log(truncated), interface_patch=INTERFACE)
    assert got["layer_coverage_pct"] == pytest.approx(99.9), "the premise: coverage WAS read"
    assert L.floors_unjudged(got) == ("thickness", "layers kept", "faces in error"), (
        "a log that left three of this engine's four layer floors with nothing to judge them on "
        f"does not say which three: {L.floors_unjudged(got)!r}")


def test_a_log_nothing_could_be_read_from_leaves_every_floor_unjudged():
    """All four, which is how `finalize._layers_published` tells "nobody judged it" from "it met
    every floor" and publishes nothing at all."""
    got = L.measured(L.read_layer_log(""), interface_patch=INTERFACE)
    assert len(L.floors_unjudged(got)) == len(L.LAYER_FLOORS)


def test_the_roster_has_one_entry_for_each_floor_shortfalls_evaluates():
    """A FIFTH FLOOR WITH NO ENTRY HERE WOULD BE INVISIBLE, and a stale entry would be a floor
    claimed on every run that nothing judges. The count is the contract between the two."""
    assert len(L.LAYER_FLOORS) == 4, L.LAYER_FLOORS
    assert [n for n, _ in L.LAYER_FLOORS] == ["faces kept of those offered", "thickness",
                                              "layers kept", "faces in error"]
    # every key named here is a key `measured` actually produces, under that spelling
    produced = set(L.measured(L.read_layer_log(GOOD_LOG), interface_patch=INTERFACE))
    for name, keys in L.LAYER_FLOORS:
        for k in keys:
            assert k in produced, f"the {name} floor is drawn on {k!r}, which `measured` never produces"


def test_layers_on_the_wrong_patch_are_not_layers_on_the_interface():
    said = L.complain(L.read_layer_log(GOOD_LOG), interface_patch="solid_to_fluid", n_layers=3)
    assert "no layer table row" in said and "fluid_to_solid" in said


def test_the_floor_is_on_what_the_pass_kept_not_on_what_the_part_allowed():
    """THE COVERAGE FLOOR WAS FITTED TO THE WRONG QUANTITY, and this test used to pin it there.

    It asserted `90.7146 < COVERAGE_FLOOR_PCT < 95.553` - a floor placed between the measured
    run and the defective one, because "a floor under that would say nothing about the very run
    this module exists to catch".

    WHAT THAT MISSED. Job b8f09ae9 recorded what the extruder was ever OFFERED: 91.1145% of the
    interface, of which it kept 99.5641%. Of the 9.28 points of coverage missing, 8.89 were
    declined up front - a face whose surface turns more than FEATURE_ANGLE degrees is never
    offered a prism - and only 0.40 were offered and then lost. The 8.89 is the part's own
    shape and no setting of the pass moves it, so both good runs of this plate sat under 92%
    (90.6422% and 90.7174%). The defective run was not defective for its coverage either: it
    was refused for not fitting its cell, which the thickness and faces-in-error floors catch.

    So the floor is on the share the pass CONTROLS, and it is still a real bar - a mover offered
    everything that keeps three quarters of it fails, which is what the width rig at
    SMOOTH_SURFACE_NORMALS 5 did.
    """
    assert not hasattr(L, "COVERAGE_FLOOR_PCT"), "the floor is back on the wrong quantity"
    for kept in (99.5641, 99.7599):      # both measured runs of this part, with margin
        assert kept > L.KEPT_OF_OFFERED_FLOOR_PCT
    bad = {"coverage_pct": 75.0, "offered_pct": 100.0, "kept_of_offered_pct": 75.0,
           "iterations": 10, "patches": {}}
    said = L.shortfalls(bad, interface_patch=INTERFACE, n_layers=3, cells=102_536_327)
    assert [s for s in said if "offered it" in s], said
    # and the run that WAS refused is still not refused by `complain`, which is only ever about
    # nothing growing at all - the thickness and faces-in-error floors are what caught it
    thin = GOOD_LOG.replace("(95.553%)", "(90.7146%)")
    got = L.read_layer_log(thin)
    assert L.complain(got, interface_patch=INTERFACE, n_layers=3, cells=102_536_327) == ""


# ---------------------------------------------------------------- what gets published

def test_the_figures_the_run_measured_are_carried_out_of_it():
    """All of them were parsed and discarded. The ASKED count is in the plan and is a different
    number: this run asked for three and got 2.84 over 94.6% of the thickness."""
    got = L.measured(L.read_layer_log(GOOD_LOG), interface_patch=INTERFACE)
    assert got["layer_coverage_pct"] == pytest.approx(95.553)
    assert got["layers_in_mesh"] == pytest.approx(2.84)
    assert got["layers_asked"] == 3
    assert got["layer_thickness_pct"] == pytest.approx(94.6)
    assert got["layer_cells_added"] == 47_232_886
    assert got["layer_faces_in_error"] == 2888
    assert got["layer_seconds"] == pytest.approx(2521.87)
    assert got["layer_patch"] == INTERFACE


def test_nothing_is_published_about_a_pass_that_did_not_happen():
    assert L.measured({}, interface_patch=INTERFACE) == {}


# ---------------------------------------------------------------- the knob

def test_the_knob_is_off_by_default(monkeypatch):
    """THE DEFAULT, not whatever this deployment happens to be set to. The suite loads the
    repository's own .env, so a bare call here reads whatever a run is currently configured for -
    which is how this test started failing the moment the pitch run was set up."""
    import inspect

    src = inspect.getsource(det)
    assert 'CONJUGATE_LAYERS: str = optional_env("CONJUGATE_LAYERS", "0")' in src
    monkeypatch.setattr(det, "CONJUGATE_LAYERS", "")
    assert det.conjugate_layers() == 0


def test_a_layer_count_that_can_never_build_is_refused_at_startup(monkeypatch):
    """Four layers is 88.5% of a cell at this expansion ratio, over the fit rule's 75%, so it
    cannot build on any part at any resolution. As a per-job refusal a typo became a run of
    failures, each telling a customer their sound file could not be prepared."""
    monkeypatch.setattr(det, "CONJUGATE_LAYERS", "4")
    with pytest.raises(ConfigurationError) as got:
        det.conjugate_layers()
    assert "can never be built" in str(got.value)
    assert f"The most that fits is {L.max_layers()}" in str(got.value)


@pytest.mark.parametrize("bad", ["banana", "-1", "1.5"])
def test_a_malformed_layer_count_is_refused(monkeypatch, bad):
    monkeypatch.setattr(det, "CONJUGATE_LAYERS", bad)
    with pytest.raises(ConfigurationError):
        det.conjugate_layers()


def test_three_layers_is_accepted(monkeypatch):
    monkeypatch.setattr(det, "CONJUGATE_LAYERS", "3")
    assert det.conjugate_layers() == 3
