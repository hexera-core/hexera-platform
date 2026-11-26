# The layer feature angle is the most expensive decision in the run, and it could only be changed
# by a deploy.
#
# MEASURED on job cba7ac69's 279,281,786-cell plate: the layer pass is 49.5 minutes of a
# 127-minute VM - 39% of the whole run, against 8.4 minutes for cartesianMesh itself - and it
# delivered prisms on 90.8603% of the interface because the extruder declined 1,515,568 faces
# where the lattice staircase turns by more than featureAngle. Of the faces it WAS given the mover
# kept 99.7599%. Whether those refused faces could carry prisms at acceptable cell quality decides
# whether that 49.5 minutes is buying what it should, and the question can only be answered on
# that geometry: the constant's own table was measured on a 506,322-face coupon where the refusal
# was 0.35%, which is 1/26th of the share.
#
# So it is settable, and 120 unless it is set.
import importlib
import os

import pytest

from meshpipeline.engines.cfmesh_conjugate import layers as L

ENV = "HEXERA_LAYER_FEATURE_ANGLE"


@pytest.fixture(autouse=True)
def _clean_env():
    had = os.environ.pop(ENV, None)
    importlib.reload(L)
    yield
    if had is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = had
    importlib.reload(L)


def _with(value):
    if value is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = value
    importlib.reload(L)
    return L.FEATURE_ANGLE


def test_unset_is_the_shipped_value_and_the_shipped_value_is_120():
    assert _with(None) == 120
    assert L.FEATURE_ANGLE_DEFAULT == 120


@pytest.mark.parametrize("value, angle", [("180", 180), ("150", 150), ("90", 90), ("0", 0),
                                          (" 180 ", 180), ("120.0", 120)])
def test_a_usable_angle_is_taken(value, angle):
    assert _with(value) == angle


@pytest.mark.parametrize("value", ["181", "-1", "abc", "", "   ", "1e999"])
def test_anything_else_leaves_the_shipped_value_alone(value):
    # IGNORED, not clamped: a run that silently meshed at an angle nobody chose would be worse
    # than one that meshed at the angle the module documents. 0..180 is what OpenFOAM reads as an
    # angle between two face normals.
    assert _with(value) == 120


def test_the_dictionary_the_mesher_reads_carries_whatever_was_chosen():
    # The knob is worthless if it does not reach the file snappyHexMesh actually reads.
    kw = {'interface_patch': 'fluid_to_solid', 'interface_cell_m': 2.51578e-05,
          'n_layers': 3, 'first_layer_m': 4.15e-06, 'expansion': 1.2,
          'min_thickness_m': 1e-06}
    _with('180')
    assert 'featureAngle              180' in L.render_layers_dict(**kw)
    _with('120')
    assert 'featureAngle              120' in L.render_layers_dict(**kw)


def test_the_angle_that_actually_ran_is_recorded_in_the_dictionary():
    # Whatever a future run meshes at, the dict it meshed from says so - that file travels home
    # in the workspace, so the answer is never a matter of remembering what was set.
    _with('150')
    text = L.render_layers_dict(interface_patch='fluid_to_solid', interface_cell_m=2.51578e-05,
                                n_layers=3, first_layer_m=4.15e-06, expansion=1.2,
                                min_thickness_m=1e-06)
    assert str(L.FEATURE_ANGLE) in text
