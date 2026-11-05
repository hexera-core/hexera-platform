# Responsibility: Verify every engine's delivered mesh renders and is named, with no engine vocabulary in the fallback.
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent
APP = ROOT / "src" / "meshpipeline"
STATIC = ROOT / "ui"

from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402


# every engine, all the way to the screen
def test_every_engine_can_render_its_own_delivered_mesh():
    for name in engine_names():
        assert get_spec(name).viewer_surface is not None, (
            f"{name} declares no viewer_surface - its users would only ever see the "
            f"input skin, never the mesh they paid for")


def test_every_engine_names_the_thing_the_user_downloads():
    for name in engine_names():
        d = get_spec(name).deliverable
        assert d.label, f"{name} deliverable has no label"
        assert not d.label.endswith((".tar.gz", ".inp", ".msh")), (
            f"{name} deliverable label {d.label!r} is a filename, not a name")
        assert "_" not in d.label, f"{name} deliverable label {d.label!r} is not English"


def _code(path: Path) -> str:
    src = path.read_text()
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    return re.sub(r"^\s*#.*$|\s#.*$", "", src, flags=re.M)


def test_the_surface_fallback_holds_no_engine_vocabulary():
    src = _code(APP / "application" / "viewer_payload.py")
    body = src[src.index("def _build_surface_payload"):src.index("def _quality_block")]
    for engine_file in ("geom.stl", "triSurface", "polyMesh", "mesh.inp", "constant/"):
        assert engine_file not in body, (
            f"the shared surface fallback names {engine_file!r} - an engine's file, in "
            f"a layer that must work for every engine")


def test_is_mesh_is_decided_by_who_produced_it_not_by_the_format():
    assert '"is_mesh"' not in _code(APP / "render" / "viewer_pack.py"), (
        "viewer_pack sets is_mesh - it cannot know: the same helper serves a delivered "
        "gmsh surface and a fallback input skin")
    builder = _code(APP / "application" / "viewer_payload.py")
    assert 'resp["is_mesh"] = True' in builder and 'resp["is_mesh"] = False' in builder


# the reviewer's findings are for the USER, not the rubric
def test_every_review_axis_says_what_its_failure_MEANS_to_a_user():
    from meshpipeline.engines.purposes import PURPOSES
    axes = []
    for name in engine_names():
        axes += list(get_spec(name).review_rubric)
    for p in PURPOSES.values():
        axes += list(getattr(p, "review_axes", ()) or ())
    assert axes, "no review axes found"
    for ax in axes:
        assert ax.concern, f"review axis {ax.name!r} declares no user-facing concern"
        assert "_" not in ax.concern, (
            f"axis {ax.name!r} concern is not English: {ax.concern!r}")
        assert ax.name not in ax.concern, f"axis {ax.name!r} leaks its key into its concern"


def test_only_FAILED_axes_are_reported_and_they_are_translated():
    from meshpipeline.api.v1.simulation import _failed_concerns
    review = {"axis_findings": [{"axis_key": "cavity_fill", "passed": True},
                                {"axis_key": "surface_staircasing_adequacy", "passed": False}]}
    out = _failed_concerns(review, "cfmesh")
    assert len(out) == 1, out
    assert "surface_staircasing_adequacy" not in out[0]
    assert "coarse" in out[0].lower()
    # a clean PASS reports nothing
    assert _failed_concerns(
        {"axis_findings": [{"axis_key": "cavity_fill", "passed": True}]}, "cfmesh") == []
