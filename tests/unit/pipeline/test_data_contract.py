# Responsibility: Verify every dispatch field, state field, event type and manifest key is registered with an intent.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import inspect
import re
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
REPO_DIR = Path(__file__).parent.parent.parent.parent

# heavy-dep stubs (same pattern as test_dispute_flow.py)

from tests._scan import scanned

from meshpipeline.pipeline import data_contract as dc  # noqa: E402


def test_every_dispatch_kwarg_is_registered():
    import meshpipeline.application.pipeline_run as wt
    sig = inspect.signature(wt.run_pipeline)
    kwargs = set(sig.parameters) - {"job_id"} | {"job_id"}
    unregistered = kwargs - dc.dispatch_kwargs()
    assert not unregistered, f"unregistered dispatch kwargs: {unregistered}"


def test_every_jobrequest_slot_is_registered():
    import meshpipeline.application.pipeline_run as wt
    slots = set(wt.JobRequest.__slots__)
    unregistered = slots - dc.dispatch_kwargs()
    assert not unregistered, f"unregistered JobRequest slots: {unregistered}"


def test_every_session_column_is_registered():
    import meshpipeline.persistence.models as m
    cols = {c.name for c in m.ChatSession.__table__.columns}
    # infra columns are not inter-agent data. organization_id (0004) is the tenant, not a datum
    # any agent produces or reads.
    infra = {"id", "owner_id", "organization_id", "messages", "created_at", "updated_at",
             "intake_submitted", "job_id"}
    unregistered = cols - infra - dc.session_columns()
    assert not unregistered, f"unregistered session columns: {unregistered}"


def test_every_emitted_event_type_is_registered():
    emitted: set[str] = set()
    for f in scanned(APP.rglob("*.py"), "the shipped application package"):
        for m in re.finditer(r'\.log\(\s*"([a-z_]+)"', f.read_text()):
            emitted.add(m.group(1))
    unregistered = emitted - set(dc.EVENT_TYPES)
    assert not unregistered, f"corpus events without registered purpose: {unregistered}"


def test_every_corpus_entry_names_its_purpose():
    for name, why in dc.EVENT_TYPES.items():
        assert why.startswith(("training", "qa")), (
            f"event {name!r} must justify capture as training and/or qa")
    for v in dc.CONTRACT:
        if v.corpus:
            assert v.corpus.startswith(("training", "qa")), (
                f"{v.concept!r} corpus justification must start training/qa")


def test_aliases_require_documented_reason():
    for v in dc.CONTRACT:
        names = {n for n in (v.intake_field, v.session_column,
                             v.dispatch_kwarg, v.state_field) if n}
        if len(names) > 1:
            assert v.alias_reason, (
                f"{v.concept!r} uses different names across layers "
                f"({sorted(names)}) without a documented alias_reason")


def test_engine_choice_uses_one_name_everywhere():
    v = next(x for x in dc.CONTRACT if x.concept == "engine choice (user)")
    assert v.intake_field == v.session_column == v.dispatch_kwarg == "mesh_engine"


def test_every_state_field_has_registered_intent():
    src = (APP / "contracts" / "pipeline_state.py").read_text()
    body = src.split("class PipelineState(TypedDict):", 1)[1]
    body = body.split("\n\n\n", 1)[0]
    fields = set(re.findall(r"^    (\w+):", body, re.M))
    assert fields == set(dc.STATE_FIELDS), (
        f"unregistered state fields: {fields - set(dc.STATE_FIELDS)}; "
        f"registered but gone: {set(dc.STATE_FIELDS) - fields}")


def test_every_config_knob_has_a_reader():
    settings_files = [*(APP / "settings").glob("*.py"),
                      *APP.glob("agents/*/settings.py"), *APP.glob("engines/*/settings.py")]
    cfg_src = "\n".join(p.read_text() for p in settings_files)
    knobs = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:=]", cfg_src, re.M))
    _settings_paths = {p.resolve() for p in settings_files}
    app_src = "\n".join(
        f.read_text() for f in APP.rglob("*.py") if f.resolve() not in _settings_paths)
    tests_src = "\n".join(
        f.read_text() for f in (REPO_DIR / "tests").glob("*.py"))
    dead = set()
    for k in sorted(knobs):
        if re.search(rf"\.{k}\b", app_src):     # any owner-qualified read: <alias>.KNOB
            continue
        if re.search(rf"\.{k}\b", tests_src):
            continue
        if len(re.findall(rf"\b{k}\b", cfg_src)) > 1:   # composed internally across settings
            continue
        if k == "REMOVED":      # a registry of DELETED names, not a knob; read by
            continue            # runtime.startup to reject stale environments
        dead.add(k)
    assert not dead, f"config knobs with zero readers: {dead}"


def test_intake_prompt_blocks_are_registered_and_composed():
    import meshpipeline.agents.intake.agent as intake
    names = [b[0] for b in intake.INTAKE_PROMPT_BLOCKS]
    assert names == ["quality_criteria", "engine_first"]
    for name, purpose, build in intake.INTAKE_PROMPT_BLOCKS:
        assert purpose and callable(build)
    # composition uses exactly the registry (no stray system += in run_intake)
    src = (APP / "agents" / "intake" / "agent.py").read_text()
    run_intake_body = src.split("async def node_intake", 1)[1]
    assert "compose_intake_system()" in run_intake_body
    assert "system + " not in run_intake_body   # ad-hoc appends are dead


# mesh-manifest registry  #
# Dynamic maps: children keyed by patch/metric names, registered as "parent.*".
_MF_DYNAMIC = {"patches", "patch_types", "patch_face_counts",
               "validation.patch_validation", "quality",
               "engine_params"}
# Structures documented wholly in their own registry entry - not walked into.
_MF_OPAQUE = {"quality_criteria.criteria", "geometry.domain_box",
              "geometry.body_box", "validation.warnings"}


def _walk_manifest(node, prefix: str, out: set) -> None:
    if prefix:
        out.add(prefix)
    if prefix in _MF_OPAQUE:
        return
    if prefix in _MF_DYNAMIC:
        if isinstance(node, dict) and node:
            out.add(prefix + ".*")
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_manifest(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, dict):
                for k, vv in v.items():
                    _walk_manifest(vv, f"{prefix}.{k}", out)


def _emitted_manifest_paths(tmp_path) -> set[str]:
    from meshpipeline.engines.manifest import write_manifest
    ws = tmp_path / "ws"
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "boundary").write_text(
        "body { type wall; nFaces 100; startFace 0; }\n"
        "inlet { type patch; nFaces 10; startFace 100; }\n"
        "outlet { type patch; nFaces 10; startFace 110; }\n")
    manifest = write_manifest(
        ws,
        patch_types={"body": "wall", "inlet": "inlet", "outlet": "outlet"},
        patch_entities={"body": [1], "inlet": [2], "outlet": [3]},
        bbox=(0, 0, 0, 1, 1, 1),
        quality={"cells": 10, "fatal": [], "skew_fraction": 0.0,
                 "max_non_ortho": 40.0, "layer_coverage": 0.9, "regions": 5},
        domain="external_flow",
        body_bbox=((0.2, 0.2, 0.2), (0.8, 0.8, 0.8)),
        mesh_bounds=(0, 0, 0, 1, 1, 1),
        volume_path="VTK/vol.vtk",
        requested_box=((-5.0, -5.0, -5.0), (5.0, 5.0, 5.0)),
        mesh_units="m",
        mesh_mode="cfmesh",
        engine_params={"topology": "external"},
    )
    out: set[str] = set()
    _walk_manifest(manifest, "", out)
    return out


def test_manifest_keys_match_registry_exactly(tmp_path):
    emitted = _emitted_manifest_paths(tmp_path)
    registered = set(dc.MANIFEST_KEYS)
    assert emitted == registered, (
        f"emitted but unregistered: {sorted(emitted - registered)}; "
        f"registered but never emitted: {sorted(registered - emitted)}")


def test_every_manifest_read_is_registered_and_not_a_ghost(tmp_path):
    segments: set[str] = set()
    for path in dc.MANIFEST_KEYS:
        segments.update(s for s in path.split(".") if s != "*")
    top_emitted = {p for p in _emitted_manifest_paths(tmp_path) if "." not in p}
    read_re = re.compile(r'manifest\.get\(\s*"(\w+)"|manifest\[\s*"(\w+)"\]')
    chain_re = re.compile(r'\.get\(\s*"(\w+)"')
    for f in scanned(APP.rglob("*.py"), "the shipped application package"):
        if f == APP / "engines" / "manifest.py":   # the writer itself
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            tops = [a or b for a, b in read_re.findall(line)]
            if not tops:
                continue
            for top in tops:
                assert top in dc.MANIFEST_KEYS, (
                    f"{f.relative_to(APP)}:{i} reads unregistered manifest key {top!r}")
                assert top in top_emitted, (
                    f"{f.relative_to(APP)}:{i} reads manifest key {top!r} "
                    f"that the writer never emits (ghost)")
            if any(t in _MF_DYNAMIC for t in tops):
                continue                            # dynamic children: any name
            for k in chain_re.findall(line):
                assert k in segments, (
                    f"{f.relative_to(APP)}:{i} reads nested manifest key {k!r} "
                    f"not present in any registered path")


def _review_prompt(manifest: dict) -> str:
    from meshpipeline.agents.reviewer.context import build_review_prompt
    system, _ = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=Path("/tmp/nonexistent"),
        step_basename="crm.step", patch_names=["body"],
        patch_colour_legend="body=red", mesh_units="m",
        request="req", review_brief="brief", job_id="t", user_dispute=None)
    return system


def test_reviewer_checklist_renders_from_quality_criteria():
    rows = [{"key": "skew_fraction", "label": "Cell skewness", "gating": True,
             "ok_when": "<= 0.05", "measured": 0.01, "passed": True,
             "rationale": "distorted cells corrupt gradients",
             "evidence_url": "https://example.org/skew"}]
    system = _review_prompt({"domain": "external aero",
                             "quality_criteria": {"engine": "cfmesh",
                                                  "criteria": rows}})
    assert "Cell skewness" in system and "<= 0.05" in system
    assert "no quality checks defined" not in system
