"""The connectivity rule on the coolant's cellZone, the sizes behind the piece count, and a part
whose intake declared no ports (cfmesh_conjugate: conjugate, foam_exec, mouths, driver).

ALL FOUR COME OFF ONE RUN. Job 6ddb58aa, ColdPlateAssembly_CLEAN.step, 283,410,955 cells
(fluid 91,329,913 / solid 192,081,042) in 3.94 h on n2-highmem-64, peak 133.8 GiB, rc=0 - a mesh
that was built and then refused on two facts:

  the coolant came out in 6 pieces where the part has 2 - 91,226,027 / 6,666 / 1 / 1 / 1 / 97,217
  cells, so 99.89% of it is one piece and three of the extras are a single cell. The four extra
  pieces are 247.998 mm3 of bulk METAL that `surfaceToCell useSurfaceOrientation` tagged as
  coolant, winding number 0.0000 against both shells of the surface.

  the inlet was to be `coolant_in` and the fluid region has no patch of that name - on a job whose
  own notes say not to name any ports on the file. The engine had named the first mouth it measured
  coolant_in; it is the blind 11.8 mm one, topoSet found 0 of the coolant's 2,640 outer faces in
  its box, and createPatch removed the zero-sized patch.

Neither is a resolution problem, and the gate's sentence said one of them was.
[[project-corintis-island-diagnosis]] [[project-corintis-port-roles]]
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import mouths as MO
from meshpipeline.engines.cfmesh_conjugate import plan as PL

#: The three mouths job 6ddb58aa's own conjugate_plan.json carries, in the order measured. The
#: first is the blind 11.8 mm lid port the engine called the inlet.
MOUTHS = ({"centroid_m": (0.065923414495, 0.033446178113, 0.07540532726),
           "normal": (0.0, 1.0, 0.0), "diameter_m": 0.0118, "passage": 0},
          {"centroid_m": (0.099881949608, 0.054681178113, 0.043305688999),
           "normal": (0.0, 1.0, 0.0), "diameter_m": 0.008, "passage": 0},
          {"centroid_m": (0.089339605589, 0.054681178113, 0.077388253039),
           "normal": (0.0, 1.0, 0.0), "diameter_m": 0.008, "passage": 1})


def _plan(rows) -> PL.ConjugatePlan:
    return PL.ConjugatePlan(
        background_cell_m=4.025248e-4, levels=1, expect_fluid_volume_m3=5.3661786893e-05,
        expect_fluid_pieces=2, volume_tol=0.05, narrowest_m=2.112337225e-3,
        port_patches=tuple(rows))


# # # the connectivity rule # # #

def test_with_no_seeds_the_dictionary_is_the_one_that_already_ran():
    """The rule is additive or it is nothing: a run that cannot name points behaves exactly as it
    did before, rather than falling back to some third thing nobody has measured."""
    plain = C.render_toposetdict(fluid_file="constant/triSurface/fluid.stl")
    assert plain == C.render_toposetdict(fluid_file="constant/triSurface/fluid.stl", seeds=[])
    assert plain == C.render_toposetdict(fluid_file="constant/triSurface/fluid.stl", seeds=None)
    assert "regionToCell" not in plain


def test_the_seeds_are_a_subset_action_between_the_tagging_and_the_zone():
    """ORDER IS THE WHOLE SAFETY ARGUMENT. `subset` can only take cells away from what the
    orientation test tagged, never add one; and because it runs before `solidCells` is formed as
    everything-minus-fluidCells, the cells it drops become metal in the same pass. That is what
    keeps both sides of the interface the same surface - measured in OpenFOAM v2412,
    fluid_to_solid and solid_to_fluid came out 3,000 each with the rule on and 3,096 each with it
    off. A `subsetMesh` after the split takes faces off one side only."""
    text = C.render_toposetdict(fluid_file="f.stl",
                                seeds=[(0.0351939, 0.030226, 0.0286963),
                                       (0.0786564, 0.0547888, 0.0757106)])
    at = {k: text.index(k) for k in ("surfaceToCell", "regionToCell", "setToCellZone",
                                     "boxToCell", "cellToCell")}
    assert at["surfaceToCell"] < at["regionToCell"] < at["setToCellZone"] < at["boxToCell"]
    assert at["boxToCell"] < at["cellToCell"]
    block = text[at["regionToCell"] - 200:at["setToCellZone"]]
    assert "action    subset;" in block
    assert f"set       {C.FLUID_CELLS};" in block
    assert "insidePoints ((0.0351939 0.030226 0.0286963) (0.0786564 0.0547888 0.0757106));" in text
    # nErode peels layers off the kept region, which would unpick the staircased channel walls
    # this engine spends its refinement on.
    assert "nErode    0;" in block


def test_the_drop_is_read_out_of_toposets_own_log_rather_than_assumed():
    log = """
    Applying source surfaceToCell
    cellSet fluidCells now size 91336582
    Applying source regionToCell
    Adding all cells of connected region containing points 2((1 2 3)(4 5 6)) ...
    cellSet fluidCells now size 91329913
    cellZoneSet fluid now size 91329913
"""
    assert C.seed_drop(log) == (91336582, 91329913)


def test_a_log_with_no_connectivity_rule_in_it_reports_nothing():
    """Job 6ddb58aa's own topoSet.log, in shape: the action was not there, so there is no drop to
    report and None is not zero."""
    assert C.seed_drop("Applying source surfaceToCell\n"
                       "    cellSet fluidCells now size 91336582\n"
                       "    cellZoneSet fluid now size 91336582\n") is None
    assert C.seed_drop("") is None


def test_a_seed_count_that_disagrees_with_the_passage_count_names_no_points(tmp_path):
    """THE ASYMMETRY IS THE WHOLE DESIGN. A seed that is missing costs the islands this exists to
    remove, which is where the engine already was. A seed that is WRONG costs the whole mesh:
    measured in OpenFOAM v2412, a regionToCell point in a cell the orientation test did not tag
    emptied the coolant set and still exited 0, and a point outside the mesh was a FOAM FATAL
    ERROR at the topoSet stage with the mesh already built. So a disagreement writes nothing."""
    from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary

    stl = tmp_path / "fluid.stl"
    write_stl_binary(stl, _box_triangles((0.0, 0.0, 0.0), (0.02, 0.02, 0.02))
                     + _box_triangles((0.03, 0.0, 0.0), (0.05, 0.02, 0.02)))
    good, said = C.fluid_seeds(stl, background_cell_m=1e-3, expect_fluid_pieces=2)
    assert len(good) == 2
    assert "kept as the cells connected to 2 seed point(s)" in said
    none, why = C.fluid_seeds(stl, background_cell_m=1e-3, expect_fluid_pieces=3)
    assert none == []
    assert "2 found against 3 passage(s) expected" in why
    # ...and a surface that cannot be read at all is a run that behaves as it did before, not a
    # refusal: the islands are a defect worth removing, never a reason to throw away the mesh.
    gone, cannot = C.fluid_seeds(tmp_path / "absent.stl", background_cell_m=1e-3,
                                 expect_fluid_pieces=2)
    assert gone == []
    assert "could not be read" in cannot


# # # the piece count # # #

def test_checkmesh_already_prints_the_piece_sizes_and_they_are_kept():
    """Four separate records threw these away while the count went on alone. They cost nothing:
    checkMesh writes them beside the count with no extra flag."""
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import parse_check_mesh

    out = """    cells:            91329913
   *Number of regions: 6
    The mesh has multiple regions which are not connected by any face.
  <<Writing region information to "constant/cellToRegion"
  <<Writing region 0 with 91226027 cells to cellSet region0
  <<Writing region 1 with 6666 cells to cellSet region1
  <<Writing region 2 with 1 cells to cellSet region2
  <<Writing region 3 with 1 cells to cellSet region3
  <<Writing region 4 with 1 cells to cellSet region4
  <<Writing region 5 with 97217 cells to cellSet region5
"""
    q = parse_check_mesh(out)
    assert q["regions"] == 6
    assert q["piece_cells"] == [91226027, 6666, 1, 1, 1, 97217]
    # And a mesh in one piece says nothing about pieces, rather than saying [1].
    assert "piece_cells" not in parse_check_mesh("    cells:            10\nMesh OK.\n")


def test_the_piece_refusal_hands_over_the_sizes_and_asserts_no_cause():
    """IT USED TO ASSERT ONE AND BE WRONG. "Islands mean the cells are too coarse to hold the
    passage together" is a claim the gate never measures, and on this mesh it was false: the
    extra pieces are metal, not a severed passage, which three of them being a single cell says
    on sight. The sentence sent a ten-agent investigation at the resolution."""
    rep = C.ConjugateReport(
        fluid=C.RegionReport(name="fluid", cells=91329913, pieces=6, mesh_ok=False,
                             piece_cells=(91226027, 6666, 1, 1, 1, 97217)),
        solid=C.RegionReport(name="solid", cells=192081042, pieces=1, mesh_ok=False))
    rep.interface_faces_fluid = rep.interface_faces_solid = 16590533
    said = " ".join(C.verify(rep, expect_fluid_pieces=2).problems)
    assert "fluid is in 6 disconnected piece(s), expected 2." in said
    assert "The pieces are 91,226,027 / 6,666 / 1 / 1 / 1 / 97,217 cells." in said
    assert "Islands mean the cells are too coarse" not in said
    assert "cannot separate them" in said


def test_a_mesh_in_one_piece_still_refuses_without_a_size_list():
    """checkMesh prints no piece list for a mesh in one piece, and an absent list must not put an
    empty "The pieces are  cells." into the sentence."""
    rep = C.ConjugateReport(fluid=C.RegionReport(name="fluid", cells=10, pieces=1, mesh_ok=True),
                            solid=C.RegionReport(name="solid", cells=10, pieces=1, mesh_ok=True))
    rep.interface_faces_fluid = rep.interface_faces_solid = 4
    said = " ".join(C.verify(rep, expect_fluid_pieces=2).problems)
    assert "expected 2." in said
    assert "The pieces are" not in said


# # # the ports nobody asked for # # #

def test_a_part_that_declared_no_ports_gets_no_inlet_and_no_outlet():
    rows, said = MO.port_rows(MO.shell_stubs(list(MOUTHS)), named=None, roles=False)
    assert [r["name"] for r in rows] == ["mouth_1", "mouth_2", "mouth_3"]
    assert {r["type"] for r in rows} == {"opening"}
    assert "No inlet and no outlet is named" in said
    assert "The direction is a guess" not in said


def test_an_unassigned_opening_is_delivered_as_a_patch_and_not_as_a_wall():
    """A `wall` where the part has an 8 mm hole is the wrong physics for a mesh that has to be
    solvable later. "opening" is deliberately absent from ROLE_TO_OF_TYPE, whose fallback is
    `patch` - the shape this customer's two mouths have already been shipped under."""
    rows, _ = MO.port_rows(MO.shell_stubs(list(MOUTHS)), named=None, roles=False)
    topo, patch = C.render_port_patch_dicts(rows)
    assert "type patch;" in patch
    assert "type wall;" not in patch
    assert C.ROLE_TO_OF_TYPE.get("opening") is None


def test_the_gates_are_not_held_to_a_port_the_engine_invented():
    """`plan.ports_from` reads the roles back out of these rows and `finalize` sets
    `coolant_ports_declared` from that, so a guessed pair becomes a declared one and the flowable
    gate refuses the run for not reaching it. With no roles there is nothing to read back."""
    stubs = MO.shell_stubs(list(MOUTHS))
    guessed, _ = MO.port_rows(stubs, named=None, roles=True)
    assert PL.ports_from(_plan(guessed)).inlet == "coolant_in"
    unassigned, _ = MO.port_rows(stubs, named=None, roles=False)
    assert PL.ports_from(_plan(unassigned)) is None


def test_every_mouth_is_still_cut_including_the_blind_one():
    """NOT CUTTING IT LOSES THE ONLY MEASUREMENT. The 11.8 mm lid port opens into 0.500 mm of
    metal, so no coolant reaches it: measured, topoSet selected 0 of the coolant's 2,640 outer
    faces into its box, createPatch removed the zero-sized patch and `empty_patches` came out
    empty, so cutting it cannot refuse anything. A face COUNT of zero on that mouth is the
    evidence the throat is closed; an absent patch cannot tell that from a choice not to look."""
    rows, _ = MO.port_rows(MO.shell_stubs(list(MOUTHS)), named=None, roles=False)
    assert len(rows) == 3
    blind = next(r for r in rows if r["diameter_m"] == pytest.approx(0.0118))
    assert blind["name"] == "mouth_1"


def test_a_declaration_still_binds_the_names_the_user_approved():
    """The no-roles branch is for an ABSENT declaration only. A job that named its ports keeps
    them, bound by position, exactly as before."""
    stubs = MO.shell_stubs(list(MOUTHS))
    named, _why = MO.declared_for(stubs, [
        {"name": "inlet", "type": "inlet", "near_mm": [65.923, 33.446, 75.405]},
        {"name": "outlet", "type": "outlet", "near_mm": [99.882, 54.681, 43.306]}])
    rows, _said = MO.port_rows(stubs, named=named, roles=True)
    assert PL.ports_from(_plan(rows)) is not None
    assert {r["type"] for r in rows} & {"inlet", "outlet"}


def test_the_severed_circuit_preflight_does_not_refuse_a_part_with_no_ports():
    """`ports_on_one_passage` refuses before meshing when the named inlet and outlet are on pieces
    that do not touch. That is the right refusal for a declared pair and the wrong one for a
    customer who asked in writing for the mesh without ports."""
    from meshpipeline.cad.fluid_passages import ports_on_one_passage
    from meshpipeline.cad.stl_io import _box_triangles

    tris = _box_triangles((0, 0, 0), (0.01, 0.01, 0.01)) + \
        _box_triangles((0.02, 0, 0), (0.03, 0.01, 0.01))
    apart = [{"name": "coolant_in", "type": "inlet", "centre_m": (0.005, 0.005, 0.0),
              "diameter_m": 0.01},
             {"name": "coolant_out", "type": "outlet", "centre_m": (0.025, 0.005, 0.0),
              "diameter_m": 0.01}]
    assert ports_on_one_passage(tris, apart), "a declared pair on two pieces is refused"
    unassigned = [{**p, "type": "opening", "name": f"mouth_{k + 1}"}
                  for k, p in enumerate(apart)]
    assert ports_on_one_passage(tris, unassigned) == ""
