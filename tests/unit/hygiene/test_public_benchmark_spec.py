# Responsibility: Keep the external benchmark spec publishable: both engines, provenance, metrics and release evidence must stay named.
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs" / "research" / "public-benchmarks.md"


def test_public_benchmark_spec_names_both_engine_tracks():
    text = SPEC.read_text()
    assert "mesh-engine benchmark" in text
    assert "cad-repair benchmark" in text


def test_public_benchmark_spec_requires_publishable_provenance_and_metrics():
    text = SPEC.read_text()
    for phrase in (
        "corpus provenance",
        "redistribution status",
        "primary metrics",
        "baselines",
        "release artifact",
        "OpenCascade",
        "hexera-cad-defect-generation",
    ):
        assert phrase in text
