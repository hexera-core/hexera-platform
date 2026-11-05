# Responsibility: Verify the scene context is typed, immutable, path-free, and reproduces its text byte for byte.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.agents.reviewer.scene_text import (
    go_to_coordinates_text,
    navigation_context,
    patch_colour_legend,
)
from meshpipeline.contracts.review_evidence import (
    PatchLegendEntry,
    ReviewRenderSession,
    SceneBounds,
    SceneContext,
)


def _scene(**kw):
    base = {
        "has_geometry": True, "mesh_units": "m", "pan_step_mm": 10.0, "zoom_step": 1.5,
        "bounds": SceneBounds(0.0, 100.0, -5.0, 5.0, 0.0, 20.0),
        # DELIBERATELY not alphabetical: with an already-sorted legend, a mutation that
        # sorts it changes nothing and the order assertion proves nothing.
        "patch_legend": (PatchLegendEntry("wall", "wall", "#00ff00"),
                         PatchLegendEntry("inlet", "inlet", "#ff0000")),
    }
    base.update(kw)
    return SceneContext(**base)


# 1-3: the contract
def test_the_session_contract_exposes_typed_scene_context():
    assert hasattr(ReviewRenderSession, "scene_context")
    from meshpipeline.sandbox.render_adapter import BackendRenderSession
    from meshpipeline.sandbox.review_session import ReviewSession

    for cls in (BackendRenderSession, ReviewSession):
        assert hasattr(cls, "scene_context"), f"{cls.__name__} exposes no scene context"


def test_scene_context_carries_no_object_path_or_open_dict():
    import dataclasses

    # `patch_views` is renderer-DERIVED framing, carried as a typed tuple for the same
    # reason the legend is: the scene context must stay closed - no open dictionaries.
    allowed = {"has_geometry": bool, "mesh_units": str, "pan_step_mm": float,
               "zoom_step": float, "bounds": object, "patch_legend": tuple,
               "patch_views": tuple}
    fields = {f.name for f in dataclasses.fields(SceneContext)}
    assert fields == set(allowed)

    scene = _scene()
    for value in vars(scene).values():
        assert not callable(value), "scene context carries an executable value"
        assert not isinstance(value, dict), "scene context carries an open dictionary"
        assert not isinstance(value, Path), "scene context carries a filesystem path"
    for text in (str(scene), repr(scene)):
        assert "/" not in text.replace("#", ""), f"a path-like value leaked: {text}"


def test_scene_context_is_immutable():
    scene = _scene()
    with pytest.raises(Exception):
        scene.pan_step_mm = 99.0


# 4-7: geometry presence is explicit, never inferred
def test_geometry_presence_is_an_explicit_fact():
    assert _scene(has_geometry=True).has_geometry is True
    assert _scene(has_geometry=False, bounds=None, patch_legend=()).has_geometry is False


def test_geometry_with_zero_targets_is_still_geometry():
    scene = _scene(patch_legend=())
    assert scene.has_geometry is True
    assert scene.patch_legend == ()


def test_missing_geometry_is_distinguishable_from_zero_targets():
    no_geom = _scene(has_geometry=False, bounds=None, patch_legend=())
    zero_targets = _scene(patch_legend=())
    assert no_geom.has_geometry is not zero_targets.has_geometry
    assert no_geom.bounds is None and zero_targets.bounds is not None


def test_geometry_present_without_bounds_is_refused():
    with pytest.raises(ValueError, match="inconsistent scene state"):
        SceneContext(has_geometry=True, mesh_units="m", pan_step_mm=1.0, zoom_step=1.5)


# 8-11: bounds and units
def test_bounds_are_carried_exactly():
    b = _scene().bounds
    assert (b.xmin, b.xmax, b.ymin, b.ymax, b.zmin, b.zmax) == (0.0, 100.0, -5.0, 5.0, 0.0, 20.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_bounds_are_refused(bad):
    with pytest.raises(ValueError, match="finite"):
        SceneBounds(0.0, bad, 0.0, 1.0, 0.0, 1.0)


def test_mesh_units_are_exact():
    assert _scene(mesh_units="m").mesh_units == "m"
    with pytest.raises(ValueError):
        _scene(mesh_units="")


# 12-15: live navigation state
def test_current_navigation_defaults_are_exposed():
    scene = _scene(pan_step_mm=42.0, zoom_step=3.0)
    assert scene.pan_step_mm == 42.0 and scene.zoom_step == 3.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_navigation_defaults_are_refused(bad):
    with pytest.raises(ValueError):
        _scene(pan_step_mm=bad)
    with pytest.raises(ValueError):
        _scene(zoom_step=bad)


# 16-19: the legend
def test_legend_order_is_the_declared_order():
    assert [e.patch_id for e in _scene().patch_legend] == ["wall", "inlet"], (
        "the legend was reordered - the manifest's order is the prompt's order")


def test_duplicate_patch_ids_are_refused():
    with pytest.raises(ValueError, match="duplicate patch legend id"):
        _scene(patch_legend=(PatchLegendEntry("wall", "wall", "#ff0000"),
                             PatchLegendEntry("wall", "wall", "#00ff00")))


@pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "#gggggg", "#ff00", "rgb(1,2,3)"])
def test_invalid_colours_are_refused(bad):
    with pytest.raises(ValueError):
        _scene(patch_legend=(PatchLegendEntry("wall", "wall", bad),))


@pytest.mark.parametrize("good", ["#ff0000", "#FFF", "cyan", "neon_pink", "hot-pink"])
def test_real_colour_shapes_are_accepted(good):
    assert _scene(patch_legend=(PatchLegendEntry("wall", "wall", good),)) is not None


# 20-22: BYTE-FOR-BYTE reproduction of today's text
class _LegacyLike:

    def __init__(self, scene):
        self._bbox = ({"xmin": scene.bounds.xmin, "xmax": scene.bounds.xmax,
                       "ymin": scene.bounds.ymin, "ymax": scene.bounds.ymax,
                       "zmin": scene.bounds.zmin, "zmax": scene.bounds.zmax,
                       "units": scene.mesh_units} if scene.bounds else {})
        self._pan_step = scene.pan_step_mm
        self._zoom_step = scene.zoom_step
        self._colors = {e.patch_id: e.color for e in scene.patch_legend}

    def get_navigation_context(self):
        return {
            "bbox_mm":     self._bbox,
            "pan_step_mm": self._pan_step,
            "zoom_step":   self._zoom_step,
            "note": (
                "move_camera distance defaults to pan_step_mm if omitted. "
                "zoom() uses zoom_step if factor omitted. "
                "Call set_navigation_defaults() to change either."
            ),
        }

    def get_patch_colour_legend(self):
        parts = [f"{name}={colour}" for name, colour in self._colors.items()]
        return ", ".join(parts) if parts else "see mesh colours"


def test_navigation_context_is_reproduced_byte_for_byte():
    scene = _scene()
    assert navigation_context(scene) == _LegacyLike(scene).get_navigation_context()


def test_navigation_context_empty_bounds_behaviour_matches():
    scene = _scene(has_geometry=False, bounds=None, patch_legend=())
    assert navigation_context(scene)["bbox_mm"] == {}
    assert navigation_context(scene) == _LegacyLike(scene).get_navigation_context()


def test_patch_colour_legend_is_reproduced_byte_for_byte():
    scene = _scene()
    assert patch_colour_legend(scene) == _LegacyLike(scene).get_patch_colour_legend()
    assert patch_colour_legend(scene) == "wall=#00ff00, inlet=#ff0000"


def test_the_empty_legend_says_see_mesh_colours():
    scene = _scene(patch_legend=())
    assert patch_colour_legend(scene) == "see mesh colours"
    assert patch_colour_legend(scene) == _LegacyLike(scene).get_patch_colour_legend()


def test_go_to_coordinates_text_is_reproduced_byte_for_byte():
    scene = _scene(pan_step_mm=12.345, mesh_units="m")
    got = go_to_coordinates_text(scene, x=1.0, y=2.0, z=3.0, span=10.0,
                                 preset="front", patch_name="wall")
    assert got == ("Camera preset=front, centred on (1, 2, 3) m, span=10 m. "
                   "(isolated: wall) Pan step recalibrated to 12.35 m.")

    plain = go_to_coordinates_text(scene, x=1.0, y=2.0, z=3.0, span=10.0)
    assert plain == ("Camera centred on (1, 2, 3) m, span=10 m. "
                     "Pan step recalibrated to 12.35 m.")


def test_go_to_coordinates_text_uses_the_live_pan_step_not_a_cached_one():
    before = go_to_coordinates_text(_scene(pan_step_mm=10.0), x=0, y=0, z=0, span=1.0)
    after = go_to_coordinates_text(_scene(pan_step_mm=99.0), x=0, y=0, z=0, span=1.0)
    assert "10 m" in before and "99 m" in after


# the legend filter itself, unstubbed
# It used to live inside `backend.py`, which the hermetic tier cannot import - so an omitted or
# reordered entry reached the prompt with nothing to catch it. It is native-free now.
def test_legend_pairs_keeps_only_patches_that_actually_loaded():
    from meshpipeline.sandbox.render_inputs import legend_pairs

    got = legend_pairs({"wall": "#f00", "farfield": "#0f0", "inlet": "#00f"},
                       {"wall": [1], "inlet": [2]})
    assert got == [("wall", "#f00"), ("inlet", "#00f")]


def test_legend_pairs_preserves_the_manifest_order():
    from meshpipeline.sandbox.render_inputs import legend_pairs

    loaded = {"a": [1], "m": [2], "z": [3]}
    assert legend_pairs({"z": "#1", "a": "#2", "m": "#3"}, loaded) == [
        ("z", "#1"), ("a", "#2"), ("m", "#3")]


def test_legend_pairs_omits_nothing_that_loaded():
    from meshpipeline.sandbox.render_inputs import legend_pairs

    colors = {f"p{i}": f"#{i}{i}{i}" for i in range(6)}
    assert len(legend_pairs(colors, dict.fromkeys(colors, [1]))) == 6


def test_legend_pairs_is_empty_when_nothing_loaded():
    from meshpipeline.sandbox.render_inputs import legend_pairs

    assert legend_pairs({"wall": "#f00"}, {}) == []


# labels are validated, not just ids
def test_a_patch_label_that_is_an_object_repr_is_refused():
    for bad in ("<meshpipeline.sandbox.backend.MeshRenderBackend object at 0x7f00>",
                "<built-in method>", ""):
        with pytest.raises(ValueError):
            _scene(patch_legend=(PatchLegendEntry("wall", bad, "#ff0000"),))


def test_a_real_label_is_accepted():
    assert _scene(patch_legend=(PatchLegendEntry("wall", "wall", "#ff0000"),)) is not None


# the SECOND private reach
def test_move_camera_text_is_reproduced_byte_for_byte():
    from meshpipeline.agents.reviewer.scene_text import move_camera_text

    scene = _scene(pan_step_mm=12.5, mesh_units="m")
    assert move_camera_text(scene, direction="up", distance_mm=5.0) == \
        "Camera moved up by 5 m."
    # omitted distance -> the LIVE pan step, formatted :g (not the coordinate text's :.4g)
    assert move_camera_text(scene, direction="left", distance_mm=None) == \
        "Camera moved left by 12.5 m."

    # A value that DISTINGUISHES the two formats: :g gives 6 significant figures (12.3457),
    # :.4g gives four (12.35). With a round fixture both agree and the assertion proves nothing -
    # the same weakness that let a legend-sort mutation survive earlier in this migration.
    precise = _scene(pan_step_mm=12.3456789, mesh_units="m")
    assert move_camera_text(precise, direction="up", distance_mm=None) == \
        "Camera moved up by 12.3457 m."
    assert move_camera_text(precise, direction="up", distance_mm=98.7654321) == \
        "Camera moved up by 98.7654 m."


def test_move_camera_default_distance_follows_recalibration():
    from meshpipeline.agents.reviewer.scene_text import move_camera_text

    before = move_camera_text(_scene(pan_step_mm=10.0), direction="up", distance_mm=None)
    after = move_camera_text(_scene(pan_step_mm=50.0), direction="up", distance_mm=None)
    assert before == "Camera moved up by 10 m." and after == "Camera moved up by 50 m."


def test_the_pan_step_tools_have_scene_based_formatters():
    import meshpipeline.agents.reviewer.scene_text as st

    assert hasattr(st, "go_to_coordinates_text") and hasattr(st, "move_camera_text")
