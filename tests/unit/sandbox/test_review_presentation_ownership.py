# Responsibility: Verify the renderer owns colour, camera and legend, and a manifest can supply none of them.
from __future__ import annotations

# THE REVIEW RENDERER OWNS PRESENTATION. THE MANIFEST CARRIES ENGINEERING FACTS.
# A real DPW4 CRM run passed native meshing and every technical gate, then lost the mesh
# because a presentation value in the manifest reached the reviewer's scene contract. The
# repair was not to validate that value more politely - it was to stop carrying it. These
# tests hold the resulting boundary: nothing upstream of a review can choose the colours,
# the framing or the visibility that review is conducted under, and there is exactly one
# current contract with no compatibility path back to the old one.
import subprocess
import sys

import pytest

from meshpipeline.contracts.review_evidence import PatchLegendEntry, PatchView, SceneContext
from meshpipeline.render import review_palette as rp
from meshpipeline.sandbox.render_inputs import RETIRED_PRESENTATION_KEYS, RenderMetadata

ROLES = {"aircraft": "wall", "farfield": "farfield", "wing": "wall", "tail": "wall"}


# 1-2. generated manifests carry no presentation

def test_a_generated_manifest_contains_no_presentation_metadata(tmp_path):
    from meshpipeline.engines.manifest import write_manifest
    manifest = write_manifest(
        tmp_path, patch_types={"aircraft": "wall", "farfield": "farfield"},
        patch_entities={"aircraft": [1], "farfield": [2]},
        bbox=(0.0, 1.0, 0.0, 1.0, 0.0, 1.0), quality={"cells": 10},
        body_bbox=((0, 0, 0), (1, 1, 1)), mesh_units="m")
    for retired in ("patch_colors", "patch_views", "palette", "camera", "background",
                    "lighting", "opacity"):
        assert retired not in manifest, f"the manifest still prescribes {retired!r}"
    # ...while the engineering facts remain
    assert manifest["patch_types"] == {"aircraft": "wall", "farfield": "farfield"}
    assert "patches" in manifest and "geometry" in manifest


# 3-4. the builder cannot author the manifest or presentation

def test_the_builder_cannot_write_the_manifest():
    from meshpipeline.agents.builder.tools import _PROTECTED_PATHS
    assert "mesh_manifest.json" in _PROTECTED_PATHS


def test_no_engine_prompt_requests_presentation_styling():
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.engines.registry import engine_names, get_spec

    for name in engine_names():
        prompt = get_spec(name)._load_prompt()
        low = prompt.lower()
        for phrase in ("neon", "choose a colour", "choose a color", "pick a colour",
                       "pick a color", "camera preset", "set the background"):
            assert phrase not in low, f"{name} prompt asks for presentation: {phrase!r}"
        # The renderer boundary is a Builder-wide rule, so it lives in the packaged role prompt
        # rather than being restated by all five engines. Assert it on the message the builder is
        # actually given - the shared contract composed with this engine's text.
        assembled = (polcfg.prompts.builder_system + "\n\n" + prompt).lower()
        assert "review renderer" in assembled, (
            f"{name}: the assembled builder prompt does not state the renderer boundary")


# 5-9. deterministic renderer-owned colour

def test_colours_are_deterministic_and_order_independent():
    a = rp.assign_hex(["aircraft", "wing", "tail", "farfield"], ROLES)
    b = rp.assign_hex(["farfield", "tail", "wing", "aircraft"], ROLES)
    assert a == b, "reordering the patch list repainted the mesh"


def test_colours_are_stable_across_processes():
    code = ("from meshpipeline.render.review_palette import assign_hex;"
            "print(assign_hex(['aircraft','farfield'],"
            "{'aircraft':'wall','farfield':'farfield'}))")
    runs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
                           check=True).stdout.strip()
            for seed in ("0", "1", "12345")}
    assert len(runs) == 1, f"assignment varies with PYTHONHASHSEED: {runs}"


def test_adding_a_patch_does_not_repaint_the_others():
    before = rp.assign_hex(["aircraft", "farfield"], ROLES)
    after = rp.assign_hex(["aircraft", "farfield", "wing"], ROLES)
    assert after["aircraft"] == before["aircraft"]
    assert after["farfield"] == before["farfield"]


def test_every_categorical_colour_clears_the_contrast_floor():
    assert rp.REVIEW_CATEGORICAL_COLORS
    for rgb in rp.REVIEW_CATEGORICAL_COLORS:
        ratio = rp.contrast_ratio(rgb, rp.REVIEW_BACKGROUND)
        assert ratio >= rp.MIN_CONTRAST_RATIO, (
            f"{rp.to_hex(rgb)} is {ratio:.2f}:1 against the review background")


def test_reserved_colours_are_never_assigned_as_categories():
    reserved = set(rp.RESERVED_COLORS)
    got = set(rp.assign([f"patch_{i}" for i in range(40)], {}).values())
    assert not (got & reserved), f"a reserved colour was handed out: {got & reserved}"
    assert rp.REVIEW_CONTEXT_COLOR not in rp.REVIEW_CATEGORICAL_COLORS


def test_context_roles_are_deterministic():
    got = rp.assign(["aircraft", "farfield"], ROLES)
    assert got["farfield"] == rp.REVIEW_CONTEXT_COLOR
    assert got["aircraft"] != rp.REVIEW_CONTEXT_COLOR
    assert rp.assign(["outer"], {"outer": "domain"})["outer"] == rp.REVIEW_CONTEXT_COLOR


def test_the_palette_holds_no_per_engine_or_per_patch_dictionary():
    import re
    # identifiers, not prose: "wing" also occurs inside "reviewing"
    text = open(rp.__file__).read()
    for token in ("aircraft", "wing", "fuselage", "lumen", "snappy", "cfmesh", "gmsh", "vmtk"):
        assert not re.search(rf"\b{token}\b", text), (
            f"the palette names {token!r} - assignment must stay generic")


# 10-11. camera framing is renderer-derived

def test_camera_views_are_derived_from_loaded_geometry():
    bounds = {"aircraft": (0.0, 2.0, -1.0, 1.0, 0.0, 0.5),
              "farfield": (-10.0, 10.0, -10.0, 10.0, -10.0, 10.0)}
    views = rp.derive_patch_views(bounds, ROLES)
    assert "farfield" not in views, "context geometry was given a view of its own"
    v = views["aircraft"]
    assert v["x"] == 1.0 and v["y"] == 0.0 and v["z"] == 0.25
    assert v["span"] == 2.0
    assert v["preset"] == rp.REVIEW_DEFAULT_PRESET
    # deterministic, and independent of dict ordering
    assert rp.derive_patch_views(dict(reversed(list(bounds.items()))), ROLES) == views


def test_a_manifest_cannot_supply_camera_framing():
    # the retired key is refused outright; there is no path by which it reaches framing
    with pytest.raises(ValueError, match="retired review-presentation"):
        RenderMetadata.from_manifest(
            {"mesh_units": "m", "patch_views": {"aircraft": {"x": 99, "y": 99, "z": 99}}})


# 12-13. the manifest cannot supply any presentation

@pytest.mark.parametrize("key", sorted(RETIRED_PRESENTATION_KEYS))
def test_a_manifest_carrying_a_retired_key_is_rejected(key):
    with pytest.raises(ValueError, match="retired review-presentation"):
        RenderMetadata.from_manifest({"mesh_units": "m", key: {"aircraft": "#ff0000"}})


def test_render_metadata_exposes_engineering_facts_only():
    import dataclasses
    fields = {f.name for f in dataclasses.fields(RenderMetadata)}
    assert fields == {"mesh_units", "patch_roles"}, f"unexpected metadata fields: {fields}"


def test_the_renderer_owns_the_drawing_constants():
    for const in ("REVIEW_BACKGROUND", "REVIEW_EDGE_COLOR", "REVIEW_EDGE_VISIBLE",
                  "REVIEW_LINE_WIDTH", "REVIEW_OPACITY", "REVIEW_LIGHTING"):
        assert hasattr(rp, const), f"{const} is not renderer-owned"
    assert rp.REVIEW_EDGE_VISIBLE is True, "edges must stay visible in review evidence"
    assert rp.REVIEW_OPACITY == 1.0, "review geometry must not be drawn transparent"


def test_a_legend_entry_reports_a_renderer_assigned_colour():
    assigned = rp.assign_hex(["aircraft"], ROLES)
    scene = SceneContext(
        has_geometry=False, mesh_units="m", pan_step_mm=1.0, zoom_step=1.5,
        patch_legend=(PatchLegendEntry(patch_id="aircraft", label="aircraft",
                                       color=assigned["aircraft"]),))
    assert scene.patch_legend[0].color == assigned["aircraft"]
    assert scene.patch_legend[0].color.startswith("#")


# 14-15. gates and scene construction

def test_engine_gates_require_no_cosmetic_metadata():
    from meshpipeline.engines.cfmesh.flow_gates import FLOW_GATES as CF
    from meshpipeline.engines.snappy.flow_gates import FLOW_GATES as SN
    for gates in (CF, SN):
        for gate in gates:
            proves = (getattr(gate, "proves", "") or "").lower()
            for cosmetic in ("colour", "color", "palette", "camera"):
                assert cosmetic not in proves, f"a gate still requires {cosmetic!r}"


def test_scene_context_builds_from_a_semantic_only_manifest():
    meta = RenderMetadata.from_manifest(
        {"mesh_units": "m", "patch_types": ROLES, "patches": {"aircraft": [1]}})
    assert dict(meta.patch_roles) == ROLES
    scene = SceneContext(
        has_geometry=False, mesh_units=meta.mesh_units, pan_step_mm=1.0, zoom_step=1.5,
        patch_legend=(PatchLegendEntry(patch_id="aircraft", label="aircraft",
                                       color=rp.assign_hex(["aircraft"], ROLES)["aircraft"]),),
        patch_views=(PatchView(patch_id="aircraft", x=0.0, y=0.0, z=0.0, span=1.0,
                               preset=rp.REVIEW_DEFAULT_PRESET),))
    assert scene.patch_views[0].patch_id == "aircraft"


def test_the_scene_context_stays_closed():
    import dataclasses
    scene = SceneContext(has_geometry=False, mesh_units="m", pan_step_mm=1.0, zoom_step=1.5)
    for f in dataclasses.fields(SceneContext):
        assert not isinstance(getattr(scene, f.name), dict), (
            f"{f.name} is an open dictionary on the scene contract")


# 16-17. real failures still fail; science untouched

def test_a_malformed_authoritative_field_still_fails():
    # presentation is fail-soft by removal; AUTHORITATIVE data is not made permissive
    with pytest.raises(ValueError):
        SceneContext(has_geometry=True, mesh_units="", pan_step_mm=1.0, zoom_step=1.5)
    with pytest.raises(ValueError):
        SceneContext(has_geometry=True, mesh_units="m", pan_step_mm=0, zoom_step=1.5)


def test_geometry_presence_is_still_explicit():
    with pytest.raises(ValueError, match="no bounds"):
        SceneContext(has_geometry=True, mesh_units="m", pan_step_mm=1.0, zoom_step=1.5,
                     bounds=None).__post_init__()


def test_scalar_field_policy_is_untouched():
    # the reserved colours must not become a numeric ramp, and no colormap lives here
    text = open(rp.__file__).read()
    for token in ("colormap", "cmap", "scalar_range", "lookup_table"):
        assert token not in text, f"the review palette encroaches on scalar policy ({token})"


# 18-20. no frontend, no retired symbols

def test_no_frontend_dependency_is_introduced():
    text = open(rp.__file__).read()
    for token in ("ui/", "tokens.css", "document.", "window.", "<div"):
        assert token not in text
