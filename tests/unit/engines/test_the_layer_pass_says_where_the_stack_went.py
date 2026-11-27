# Responsibility: the layer report tells a declined face, a withdrawn face and a shallow column apart.
# Boundaries: `layers.read_layer_log` only - no mesh, no dict, no floor is changed here.
# Collaborates with: the snappyHexMesh log format, reproduced from two real runs of one part.
"""One number said 2.00 and no floor could see why.

The delivered cold plate retains 2.00 of 3 requested layers. `KEPT_OF_OFFERED_FLOOR_PCT` reads
99.7599% on that run and passes, because its denominator is iteration 0's POST-removal count -
and iteration 0 is where the stack is lost. Measured on the two real logs of the same part:

                                 plate, featureAngle 120   bl8, featureAngle 180
    layers at iteration 0                    2.0817                  3.0000
    layers in the mesh                       2.0000                  2.8400
    isolated points stopped               2,371,211                       0
    kept_of_offered_pct                     99.7599%                98.3098%
    illegal faces                                   8                   2,888

The bar scores the WORSE stack higher. That is not a wrong number - 99.7599% is a true statement
about iterations 1 to 9 - it is a bar that cannot see the thing that moved. These tests pin the
figures that can, and pin that the bar's own value did not change, because quietly redefining a
number a floor is drawn on is how a bar stops meaning anything.

The fixtures reproduce the real lines, including the one that settles the argument: bl8's
iteration 0 prints `Added 49859331 out of 49859331 cells (100%)` beside
`Extruding 16153727 out of 16619777`, and 49,859,331 is 3 x 16,619,777 exactly while
3 x 16,153,727 is 48,461,181 - so the Added line is not describing the face count printed above
it, and `offered_pct` is not "what was ever offered".
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import layers as LY

PATCH = "fluid_to_solid"


def log(*, faces: int, iters: list[tuple[int, int, int]], target: int = 3,
        isolated: int = 0, mesh_layers: str = "2", thickness_pct: str = "79.8",
        illegal: int = 8) -> str:
    """A layer log with `iters` of (extruding, removed, added). Shaped like the real thing."""
    asked = target * faces
    out = [
        "Layer thickness specified as first layer thickness.",
        "",
        "patch          faces    layers avg thickness[m]",
        "                        near-wall overall",
        "-----          -----    --------- -------",
        f"{PATCH} {faces} {target}      4.15e-06  1.51e-05",
        "",
    ]
    for n, (ext, rem, add) in enumerate(iters):
        out += [
            f"Layer addition iteration {n}",
            "--------------------------",
            "displacementMedialAxis : Removing isolated regions ...",
            "    - if partially extruded faces make angle < 60",
            f"displacementMedialAxis : Number of isolated points extrusion stopped : {isolated}",
            f"Extruding {ext} out of {faces} faces ({100.0 * ext / faces:.4f}%)."
            f" Removed extrusion at {rem} faces.",
            f"Added {add} out of {asked} cells ({100.0 * add / asked:.4f}%).",
            "",
        ]
    out += [
        "patch          faces        layers        overall thickness",
        "                        target   mesh     [m]       [%]",
        "-----          -----    -----    ----     ---       ---",
        f"{PATCH} {faces} {target}        {mesh_layers}        1.21e-05  {thickness_pct}    ",
        "",
        f"Finished meshing with {illegal} illegal faces",
        "Layers added in = 1846.8 s",
    ]
    return "\n".join(out)


#: The delivered plate, from /home/rehaan/plate_facts/snappyHexMesh.log
PLATE = log(faces=16582198, isolated=2371211, mesh_layers="2", thickness_pct="79.8", illegal=8,
            iters=[(15066630, 281058, 34518913),
                   (15031050, 11332, 33188196),
                   (15030462, 74, 33117875)])

#: bl8, the run the 0.75 floor is calibrated on, from /home/rehaan/qa/bl8/snappy.log
BL8 = log(faces=16619777, isolated=0, mesh_layers="2.84", thickness_pct="94.6", illegal=2888,
          iters=[(16153727, 340361, 49859331),
                 (15998406, 46013, 47734446),
                 (15880692, 30066, 47232886)])


# ------------------------------------------------------------------ the three depths

def test_the_report_carries_three_depths_not_one():
    got = LY.read_layer_log(PLATE)
    assert got["layers_at_iteration_0"] == pytest.approx(2.0817, abs=1e-4)
    assert got["layers_per_extruded_face"] == pytest.approx(2.2034, abs=1e-4)
    assert (got["patches"][PATCH]["mesh_layers"]) == pytest.approx(2.0)


def test_the_up_front_loss_is_where_this_part_loses_its_stack():
    """0.92 of a layer before any repair ran; ten repair iterations cost 0.08."""
    got = LY.read_layer_log(PLATE)
    asked = got["patches"][PATCH]["target_layers"]
    up_front = asked - got["layers_at_iteration_0"]
    repair = got["layers_at_iteration_0"] - got["patches"][PATCH]["mesh_layers"]
    assert up_front == pytest.approx(0.9183, abs=1e-3)
    assert repair == pytest.approx(0.0817, abs=1e-3)
    assert up_front > 10 * repair, "the loss is up front, which is what no floor looks at"


def test_the_same_part_at_a_wider_feature_angle_loses_nothing_up_front():
    got = LY.read_layer_log(BL8)
    assert got["layers_at_iteration_0"] == pytest.approx(3.0, abs=1e-6)
    assert got["cells_added_first_iter_pct"] == pytest.approx(100.0, abs=1e-4)
    assert got["isolated_points_stopped"] == 0


def test_the_mechanism_travels_in_its_own_words():
    assert LY.read_layer_log(PLATE)["isolated_points_stopped"] == 2371211


# ------------------------------------------------------------------ the bar's blind spot

def test_the_bar_scores_the_worse_stack_higher():
    """THE WHOLE POINT. 99.7599% on a 2.00-layer mesh, 98.3098% on a 2.84-layer one, because the
    denominator starts after the loss. Pinned so that nobody reads the bar as a depth figure."""
    plate, bl8 = LY.read_layer_log(PLATE), LY.read_layer_log(BL8)
    assert plate["kept_of_offered_pct"] > bl8["kept_of_offered_pct"]
    assert plate["patches"][PATCH]["mesh_layers"] < bl8["patches"][PATCH]["mesh_layers"]
    # and both clear the floor, so the floor cannot tell them apart at all
    assert plate["kept_of_offered_pct"] > LY.KEPT_OF_OFFERED_FLOOR_PCT
    assert bl8["kept_of_offered_pct"] > LY.KEPT_OF_OFFERED_FLOOR_PCT


def test_the_bar_s_own_value_did_not_change():
    """Calibration points are drawn on these two numbers; they are reported, not redefined."""
    plate = LY.read_layer_log(PLATE)
    assert plate["offered_pct"] == pytest.approx(90.8603, abs=1e-4)
    assert plate["kept_of_offered_pct"] == pytest.approx(99.7599, abs=1e-3)


# ------------------------------------------------------------------ declined vs withdrawn

def test_a_face_never_offered_is_told_from_one_taken_back():
    got = LY.read_layer_log(PLATE)
    assert got["faces_on_patch"] == 16582198
    assert got["faces_removed_first_iter"] == 281058
    assert got["faces_never_offered"] == 16582198 - 15066630 - 281058
    assert got["never_offered_pct"] == pytest.approx(7.4448, abs=1e-3)
    assert got["removed_first_iter_pct"] == pytest.approx(1.6949, abs=1e-3)


def test_the_two_causes_add_up_to_the_missing_coverage():
    """7.4448 + 1.6949 + the repair's 0.2181 is the 9.36 points of missing coverage, and the old
    comment put 9.14 of it on the part's shape."""
    got = LY.read_layer_log(PLATE)
    rest = got["offered_pct"] - got["coverage_by_iteration"][-1]
    total = got["never_offered_pct"] + got["removed_first_iter_pct"] + rest
    assert total == pytest.approx(100.0 - got["coverage_by_iteration"][-1], abs=1e-2)


# ------------------------------------------------------------------ the identity it rests on

@pytest.mark.parametrize("body,expected", [(PLATE, 1.99719), (BL8, 2.84197)])
def test_mesh_layers_is_cells_added_over_every_patch_face(body, expected):
    """Which is why `layers_in_mesh` is not a depth: it averages over faces that got nothing."""
    got = LY.read_layer_log(body)
    assert got["cells_added"] / got["faces_on_patch"] == pytest.approx(expected, abs=1e-5)


# ------------------------------------------------------------------ nothing to read

def test_a_log_with_no_layer_pass_publishes_none_of_it():
    got = LY.read_layer_log("")
    for k in ("layers_at_iteration_0", "layers_per_extruded_face", "faces_on_patch",
              "faces_never_offered", "faces_removed_first_iter", "isolated_points_stopped"):
        assert got[k] is None, k


def test_a_log_that_never_removed_an_extrusion_says_so_rather_than_zero_dividing():
    body = log(faces=1000, iters=[(1000, 0, 3000)], mesh_layers="3", thickness_pct="100.0",
               illegal=0)
    got = LY.read_layer_log(body)
    assert got["faces_removed_first_iter"] == 0
    assert got["faces_never_offered"] == 0
    assert got["layers_at_iteration_0"] == pytest.approx(3.0)
