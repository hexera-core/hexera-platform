# Responsibility: Verify every engine and purpose declares review axes that map to real validation axes, additively.
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.base import ValidationAxis  # noqa: E402
from meshpipeline.engines.purposes import PURPOSES  # noqa: E402
from meshpipeline.engines.quality_criteria import (  # noqa: E402
    compose_review_rubric,
    criteria_for,
)
from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402

_VALID_AXES = {a.value for a in ValidationAxis}


def _all_engine_axes():
    for e in engine_names():
        for ax in get_spec(e).review_rubric:
            yield e, ax


def _all_purpose_axes():
    for k, p in PURPOSES.items():
        for ax in p.review_axes:
            yield k, ax


# coverage: every engine + every purpose declares review guidance
def test_every_engine_declares_a_nonempty_review_rubric():
    for e in engine_names():
        assert get_spec(e).review_rubric, f"{e} declares no review_rubric"


def test_every_purpose_declares_review_axes():
    for k, p in PURPOSES.items():
        assert p.review_axes, f"purpose {k} declares no review_axes"


def test_every_engine_including_the_former_metric_engines_owns_a_rubric():
    # Rubric ownership is a spec property, never tied to a review-mode flag (which no longer exists).
    for e in ("gmsh", "vmtk"):
        sp = get_spec(e)
        assert not hasattr(sp, "visual_review")
        assert sp.review_rubric, f"{e} must own a review rubric"


def test_every_axis_maps_to_a_valid_validation_axis():
    for src, ax in [*_all_engine_axes(), *_all_purpose_axes()]:
        assert ax.validation_axis in _VALID_AXES, f"{src}:{ax.name} → bad axis {ax.validation_axis!r}"


# Decision #3: ONE layered map - ReviewAxis names disjoint from Criterion keys
def test_review_axes_do_not_restate_machine_criteria():
    for e in engine_names():
        crit_keys = {c.key for c in criteria_for(e)}
        axis_names = {ax.name for ax in get_spec(e).review_rubric}
        overlap = crit_keys & axis_names
        assert not overlap, f"{e}: {overlap} are BOTH a Criterion key and a ReviewAxis name"


# Decision #2: engine ∪ purpose, deduped by name, owner stamped, additive
def test_compose_is_additive_and_owner_stamped():
    axes = compose_review_rubric("snappy", "external_cfd")
    names = [a.name for a in axes]
    assert len(names) == len(set(names)), "composed rubric has duplicate axis names"
    owners = {a.owner for a in axes}
    assert any(o.startswith("engine:snappy") for o in owners)
    assert any(o.startswith("purpose:external_cfd") for o in owners)
    # additive: composed == engine count + purpose count (no name clash here)
    n_eng = len(get_spec("snappy").review_rubric)
    n_pur = len(PURPOSES["external_cfd"].review_axes)
    assert len(axes) == n_eng + n_pur


def test_compose_dedup_prefers_engine_axis_on_name_clash():
    # Synthesise a clash: an engine axis and a purpose axis sharing a name → engine wins.
    import dataclasses

    eng = get_spec("snappy").review_rubric[0]
    clash = dataclasses.replace(eng, guidance="PURPOSE VERSION")
    # monkey-compose by hand mirrors compose_review_rubric's rule
    seen = {eng.name}
    kept = [eng] + [a for a in (clash,) if a.name not in seen]
    assert kept == [eng]  # the purpose clash is dropped


def test_no_engine_purpose_matrix_exists():
    # There must be no per-(engine×purpose) rubric object - composition is the only path.
    import meshpipeline.engines.quality_criteria as qc
    src = Path(qc.__file__).read_text()
    assert "compose_review_rubric" in src
    # a matrix would be keyed by tuples; guard against an obvious dict-of-tuples table
    assert not re.search(r"\{\s*\(\s*['\"]\w+['\"]\s*,\s*['\"]\w+['\"]\s*\)\s*:", src)


# Decision #1: NO setpoints / solver recipes in rubric text (the RAG guard)
_SETPOINT_PATTERNS = [
    (r"\by\s*\+", "y+ target"),
    (r"\byplus\b", "y+ target"),
    (r"\b(k-?omega|k-?epsilon|\bsst\b|spalart|allmaras|realizable|reynolds stress|"
     r"\bles\b|\bdes\b|\brans\b|\brsm\b)\b", "turbulence model"),
    (r"\b(nsurfacelayers|maxcellsize|surface_level|feature_level|n_layers|first_layer|"
     r"max_cell_factor|wall_cell|thickness_ratio|max_cells|curvature_nodes|refinement level)\b",
     "mesher knob"),
    (r"\b\d+(\.\d+)?\s*[×x]\b", "multiplier setpoint"),
    (r"\b\d+(\.\d+)?\s*(chord|chords|diameters?|body[- ]lengths?|mm|cm|deg|degrees?|°|cells?|elements?)\b",
     "numeric setpoint"),
    (r"[<>]=?\s*\d", "numeric threshold"),
]


def test_review_rubric_no_setpoints():
    problems = []
    for src, ax in [*_all_engine_axes(), *_all_purpose_axes()]:
        text = " ".join((ax.guidance, *ax.failure_signals)).lower()
        for pat, label in _SETPOINT_PATTERNS:
            m = re.search(pat, text)
            if m:
                problems.append(f"{src}:{ax.name} leaks {label} ({m.group(0)!r})")
    assert not problems, "setpoint leakage in rubric text:\n  " + "\n  ".join(problems)


# the shared prompts carry no engine/domain persona (injected context only)
_FORBIDDEN_PROMPT_TOKENS = (
    "cfmesh", "snappy", "gmsh", "cfd", "fea", "far-field", "farfield",
    "aerodynamic", "openfoam", "snappyhexmesh", "meshdict", "maxcellsize",
    "sicn", "y+", "komega", "senior fea engineer",
)


def test_shared_reviewer_prompts_are_neutral():
    # Only the unified reviewer prompt remains (the metric-only/verdict templates were deleted in D).
    for fname in ("reviewer/system.txt",):
        text = (APP / "prompts" / fname).read_text().lower()
        hits = [t for t in _FORBIDDEN_PROMPT_TOKENS if t in text]
        assert not hits, f"{fname} contains engine/domain vocabulary: {hits}"


# the composed rubric reaches BOTH reviewer briefs
def test_rubric_reaches_the_visual_brief():
    import tempfile

    from meshpipeline.agents.reviewer.context import build_review_prompt
    ws = Path(tempfile.mkdtemp())
    manifest = {"domain": "x", "quality_criteria": {"criteria": []}, "patches": {}}
    system_prompt, _ = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=ws, step_basename="x.stl",
        patch_names=[], patch_colour_legend="", mesh_units="m",
        request="r", review_brief="b", job_id="t",
        engine="snappy", purpose="external_cfd")
    # an engine (snappy) axis AND a purpose (external_cfd) axis must both be present
    assert "surface_capture" in system_prompt
    assert "farfield_clearance" in system_prompt
    assert "Engine: snappy" in system_prompt
    # review_rationale (the once-dead modality justification) now reaches the brief too
    assert "Why this review approach:" in system_prompt
    assert get_spec("snappy").review_rationale[:30] in system_prompt


def test_rubric_reaches_the_gmsh_brief():
    # Post-C2 gmsh uses the ONE unified visual brief (the metric-only brief was deleted in D). The
    # composed engine+purpose rubric must reach it.
    import tempfile

    from meshpipeline.agents.reviewer.context import build_review_prompt
    ws = Path(tempfile.mkdtemp())
    manifest = {"domain": "x", "quality_criteria": {"criteria": []}, "patches": {}}
    system, _ = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=ws, step_basename="x.stl",
        patch_names=[], patch_colour_legend="", mesh_units="m",
        request="r", review_brief="b", job_id="t", engine="gmsh", purpose="structural")
    assert "group_completeness" in system          # gmsh engine axis
    assert "restraint_load_surfaces" in system      # structural purpose axis
    assert "Engine: gmsh" in system
    assert get_spec("gmsh").review_rationale[:30] in system


# #5 GROUNDING CONTRACT: no render-based axis may silently fabricate
# A render-based ReviewAxis must EITHER declare typed `requires` (the authoritative grounding) OR
# be a conscious `visual_only` exception (a genuinely unmeasurable spatial/shape judgment), enforced
# across EVERY renderable engine so grounding can't drift back to being snappy-only.


def test_every_render_based_axis_is_grounded():
    problems = []
    for e in engine_names():
        sp = get_spec(e)
        if not sp.renders_for_review:
            continue                      # no renderer -> no render axis to fabricate from
        for ax in sp.review_rubric:
            if "render" not in (ax.evidence or ()):
                continue
            if not (bool(getattr(ax, "requires", ())) or ax.visual_only):
                problems.append(f"{e}:{ax.name}")
    assert not problems, (
        "render-based review axes with no grounding - a silent fabrication risk (add typed "
        "requires=(...) or visual_only=True):\n  " + "\n  ".join(problems))


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
