# Responsibility: Verify failure classification is keyed dispatch needing no model, each gate declaring a section.
from __future__ import annotations

import asyncio

from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402
from meshpipeline.pipeline.classifier import (  # noqa: E402
    failed_axes,
    node_classifier,
    section_for_gate,
)
from meshpipeline.pipeline.enums import SEAM_SECTIONS, FailureSection  # noqa: E402


def _run(state):
    return asyncio.run(node_classifier(state))


def test_classification_never_needs_a_model_to_be_reachable():
    import meshpipeline.adapters.model_inference.router as router
    import meshpipeline.contracts.model_inference as mi

    called: list[str] = []
    patched: list[tuple[object, str, object]] = []

    def _trip(*a, __n="", **k):
        called.append(__n)
        raise AssertionError(f"the classifier reached a model: {__n}")

    for mod in (mi, router):
        for name in [n for n in dir(mod) if n.startswith("call_")]:
            patched.append((mod, name, getattr(mod, name)))
            setattr(mod, name, lambda *a, __n=f"{mod.__name__}.{name}", **k: _trip(__n=__n))
    try:
        out = _run({
            "job_id": "t", "engine": "gmsh", "executor_success": False,
            "executor_failed_gate": "sicn_floor",
            "executor_output": "[QUALITY] min SICN 0.03 is below the 0.1 floor",
        })
    finally:
        for mod, name, orig in patched:
            setattr(mod, name, orig)

    assert called == [], f"the classifier called a model: {called}"
    r = out["classifier_result"]
    assert r["section"] == FailureSection.MESH and r["failed_gate"] == "sicn_floor"
    assert out["builder_mode"] == "retry", "a failed run went unretried"


# the guard that matters: no engine's gate can go unclassified

def test_every_declared_gate_of_every_engine_declares_a_section():
    valid = set(FailureSection)
    for name in engine_names():
        for gate in get_spec(name).gates:
            assert gate.section in valid, f"{name}.{gate.key} has bogus section {gate.section!r}"
            # and it must resolve back through the lookup the classifier actually uses
            assert section_for_gate(name, gate.key) == gate.section


def test_seam_rejections_are_classified_too():
    for key, section in SEAM_SECTIONS.items():
        assert section_for_gate("cfmesh", key) == section
    assert section_for_gate("cfmesh", "domain_extent") == FailureSection.DOMAIN
    assert section_for_gate("cfmesh", "solvability") == FailureSection.MESH


def test_unknown_or_missing_gate_key_falls_back_to_mesh_never_crashes():
    assert section_for_gate("cfmesh", "") == FailureSection.MESH
    assert section_for_gate("cfmesh", "no_such_gate") == FailureSection.MESH
    assert section_for_gate("no_such_engine", "manifest_valid") in set(FailureSection)


# executor path

def test_executor_failure_uses_the_gates_declared_section_and_verbatim_feedback():
    out = _run({
        "job_id": "t", "engine": "gmsh", "executor_success": False,
        "executor_failed_gate": "sicn_floor",
        "executor_output": "[QUALITY] min SICN 0.03 is below the 0.1 floor - reduce size.value",
    })
    r = out["classifier_result"]
    assert r["section"] == FailureSection.MESH        # gmsh declares sicn_floor -> MESH
    assert r["failed_gate"] == "sicn_floor"
    assert r["error_source"] == "executor_fail"
    # the engine's own coaching survives intact - it is NOT re-summarised
    assert "min SICN 0.03" in r["summary"] and "reduce size.value" in r["summary"]
    # a gate cannot know the whole approach is wrong; only the reviewer can
    assert out["builder_mode"] == "retry"


def test_input_geometry_preflight_rejection_classifies_as_geometry():
    reason = ("[GEOMETRY_UNSUITABLE] the input surface self-intersects - its triangles pass "
              "through each other, so it does not bound a solid volume and no tetrahedral fill "
              "is possible.")
    out = _run({
        "job_id": "t", "engine": "vmtk", "executor_success": False,
        "executor_failed_gate": "geometry", "executor_output": reason,
    })
    r = out["classifier_result"]
    assert r["section"] == FailureSection.GEOMETRY
    assert r["failed_gate"] == "geometry"
    assert r["summary"] == reason          # the reason survives intact for the user
    assert r["error_source"] == "executor_fail"


def test_gmsh_quality_and_multiregion_gates_no_longer_fall_through():
    assert section_for_gate("gmsh", "sicn_floor") == FailureSection.MESH
    assert section_for_gate("snappy_multiregion", "regions_split") == FailureSection.MESH
    assert section_for_gate("snappy_multiregion", "interfaces") == FailureSection.GEOMETRY
    assert section_for_gate("snappy_multiregion", "region_contract") == FailureSection.GROUPS
    assert section_for_gate("vmtk", "quality_floor") == FailureSection.MESH


def test_contract_rejection_is_labelled_groups_across_engines():
    for engine in ("cfmesh", "snappy", "gmsh", "vmtk"):
        assert section_for_gate(engine, "patch_contract") == FailureSection.GROUPS


# reviewer path

def _axis(name: str, passed: bool) -> dict:
    return {"axis_key": name, "owner": "engine:snappy", "passed": passed,
            "finding": "…", "evidence_ids": ["m-001"], "attempt": 0}


def test_failed_axes_reads_the_canonical_list_verdict():
    # canonical: a LIST of typed entries; failing iff passed is False.
    assert failed_axes([_axis("surface_capture", True), _axis("prism_layer_coverage", False)]) == \
        ["prism_layer_coverage"]
    assert failed_axes([]) == []
    assert failed_axes(None) == []                 # never crashes on a malformed tool call
    # a malformed entry (missing `passed`) is NOT read as a failure - `is False`, not falsiness
    assert failed_axes([{"axis_key": "x"}]) == []
    assert failed_axes([{"axis_key": "y", "passed": None}]) == []


def test_reviewer_rejection_routes_on_the_declared_rebuild_flag_not_on_prose():
    prose = "It would be completely wrong to say the topology is bad; only the layers failed."
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True,
        "reviewer_feedback": prose,
        "reviewer_axis_findings": [_axis("surface_capture", True),
                                   _axis("prism_layer_coverage", False)],
        "reviewer_rebuild_required": False,
    })
    assert out["builder_mode"] == "retry"               # NOT rebuild
    assert out["classifier_result"]["failed_axes"] == ["prism_layer_coverage"]
    assert out["classifier_result"]["summary"] == prose  # handed over verbatim


def test_reviewer_fail_with_multiple_failed_axes_reaches_classifier_with_those_exact_axes():
    feedback = "prism layers thin; far-field too tight"
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True,
        "reviewer_verdict": "FAIL",
        "reviewer_feedback": feedback,
        "reviewer_axis_findings": [
            _axis("surface_capture", True),
            _axis("prism_layer_coverage", False),
            _axis("farfield_clearance", False),
        ],
        "reviewer_rebuild_required": False,
    })
    assert out["classifier_result"]["failed_axes"] == ["farfield_clearance", "prism_layer_coverage"]
    assert out["classifier_result"]["error_source"] == "reviewer_fail"
    assert out["classifier_result"]["summary"] == feedback   # verbatim to the Builder advisory block
    assert out["builder_mode"] == "retry"


def test_reviewer_declaring_rebuild_routes_to_rebuild():
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True,
        "reviewer_feedback": "A 3D far-field box was built for a 2D section.",
        "reviewer_axis_findings": [_axis("domain_enclosure", False)],
        "reviewer_rebuild_required": True,
    })
    assert out["builder_mode"] == "rebuild"
    assert out["classifier_result"]["section"] == FailureSection.TOPOLOGY


def test_reviewer_section_derives_from_the_failing_axis_validation_axis():
    # farfield_clearance is a CONFORMANCE axis of the external_cfd purpose -> GROUPS
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True, "reviewer_feedback": "too tight",
        "reviewer_axis_findings": [_axis("farfield_clearance", False)],
        "reviewer_rebuild_required": False,
    })
    assert out["classifier_result"]["section"] == FailureSection.GROUPS
    # domain_enclosure is an INTEGRITY axis -> GEOMETRY
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True, "reviewer_feedback": "clipped",
        "reviewer_axis_findings": [_axis("domain_enclosure", False)],
        "reviewer_rebuild_required": False,
    })
    assert out["classifier_result"]["section"] == FailureSection.GEOMETRY


def test_reviewer_with_no_named_axes_still_classifies_and_retries():
    out = _run({
        "job_id": "t", "engine": "snappy", "purpose": "external_cfd",
        "executor_success": True, "reviewer_feedback": "bad",
        "reviewer_axis_findings": [], "reviewer_rebuild_required": False,
    })
    assert out["classifier_result"]["section"] == FailureSection.MESH
    assert out["builder_mode"] == "retry"


# the corpus label derived from the section

def test_every_failure_section_has_a_corpus_label():
    from meshpipeline.application.maintenance.export import _SECTION_FAILURE_LABELS
    missing = set(FailureSection) - set(_SECTION_FAILURE_LABELS)
    assert not missing, f"FailureSection members with no corpus label: {sorted(missing)}"


def test_rebuild_is_labelled_distinctly_from_a_mesh_defect():
    from meshpipeline.application.maintenance.export import _SECTION_FAILURE_LABELS
    assert _SECTION_FAILURE_LABELS[FailureSection.TOPOLOGY] == "wrong_topology"
    assert _SECTION_FAILURE_LABELS[FailureSection.TOPOLOGY] != \
        _SECTION_FAILURE_LABELS[FailureSection.MESH]
