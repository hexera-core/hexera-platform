# Responsibility: Verify each policy version is pinned, distinct from the schema version, and moves the fingerprint.
from __future__ import annotations

from pathlib import Path

import meshpipeline.agents.intake.admission_token as at
from meshpipeline.application.artifact_policy import ARTIFACT_POLICY_VERSION
from meshpipeline.pipeline.enums import FIDELITY_POLICY_VERSION

REPO = Path(__file__).parents[3]


def _canon(**over):
    kw = {"engine": "gmsh", "purpose": "internal_cfd", "input_kind": "fluid-domain",
          "dimensionality": "3D", "patches": [], "engine_params": {},
          "requested_mesh_fidelity": "draft", "request_txt": "x", "source_ref": None}
    kw.update(over)
    return at.approved_intent_canonical(**kw)


def test_both_policy_versions_are_present_and_non_empty():
    c = _canon()
    assert c["fidelity_policy_version"] == FIDELITY_POLICY_VERSION and FIDELITY_POLICY_VERSION
    assert c["artifact_policy_version"] == ARTIFACT_POLICY_VERSION and ARTIFACT_POLICY_VERSION


def test_fidelity_policy_version_drift_changes_the_fingerprint():
    base = at.fingerprint(_canon())
    drifted = dict(_canon())
    drifted["fidelity_policy_version"] = "3tier-v2"
    assert at.fingerprint(drifted) != base


def test_artifact_policy_version_drift_changes_the_fingerprint():
    base = at.fingerprint(_canon())
    drifted = dict(_canon())
    drifted["artifact_policy_version"] = _canon()["artifact_policy_version"] + "-drifted"
    assert at.fingerprint(drifted) != base


def test_policy_versions_are_distinct_from_the_schema_version():
    c = _canon()
    assert c["fidelity_policy_version"] != c["schema_version"]
    assert c["artifact_policy_version"] != c["schema_version"]
    assert c["fidelity_policy_version"] != c["artifact_policy_version"]


def test_current_policy_version_ids_are_pinned():
    assert FIDELITY_POLICY_VERSION == "3tier-v1"
    # v2: viewer_data joined mesh_bundle as a REQUIRED output class, so what a job must
    # deliver changed - exactly the change this constant exists to record.
    assert ARTIFACT_POLICY_VERSION == "artifacts-v2"
