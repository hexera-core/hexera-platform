# Responsibility: Verify mesh workload evidence selects an explainable worker tier and one bounded escalation.
from __future__ import annotations

import pytest

from meshpipeline.application.mesh_execution_profiles import (
    MeshExecutionProfile,
    MeshFailure,
    MeshWorkload,
    escalation_profile,
    select_profile,
)


def test_small_known_workload_stays_on_standard():
    decision = select_profile(MeshWorkload(
        engine="gmsh", source_bytes=80_000_000, surface_triangles=900_000,
        requested_cells=4_000_000, region_count=4, boundary_layers=0))

    assert decision.profile is MeshExecutionProfile.STANDARD
    assert decision.policy_version == "mesh-execution-profile-v1"
    assert decision.reasons == ()


@pytest.mark.parametrize("signal,value", [
    ("source_bytes", 500_000_001),
    ("surface_triangles", 5_000_001),
    ("requested_cells", 8_000_001),
    ("region_count", 65),
])
def test_one_signal_over_the_standard_ceiling_selects_high_memory(signal, value):
    workload = MeshWorkload(engine="gmsh", **{signal: value})

    decision = select_profile(workload)

    assert decision.profile is MeshExecutionProfile.HIGH_MEMORY
    assert decision.reasons == (f"{signal} exceeds standard ceiling",)


@pytest.mark.parametrize("signal,value", [
    ("source_bytes", 4_000_000_001),
    ("surface_triangles", 50_000_001),
    ("requested_cells", 100_000_001),
    ("region_count", 513),
])
def test_one_signal_over_the_high_memory_ceiling_selects_ultramem(signal, value):
    workload = MeshWorkload(engine="gmsh", **{signal: value})

    decision = select_profile(workload)

    assert decision.profile is MeshExecutionProfile.ULTRAMEM
    assert decision.reasons == (f"{signal} exceeds high-memory ceiling",)


def test_values_on_every_standard_ceiling_remain_standard():
    decision = select_profile(MeshWorkload(
        engine="gmsh", source_bytes=500_000_000, surface_triangles=5_000_000,
        requested_cells=8_000_000, region_count=64, boundary_layers=0))

    assert decision.profile is MeshExecutionProfile.STANDARD


def test_values_on_every_high_memory_ceiling_remain_high_memory():
    decision = select_profile(MeshWorkload(
        engine="gmsh", source_bytes=4_000_000_000, surface_triangles=50_000_000,
        requested_cells=100_000_000, region_count=512, boundary_layers=20))

    assert decision.profile is MeshExecutionProfile.HIGH_MEMORY


def test_one_ultramem_signal_wins_over_small_signals():
    decision = select_profile(MeshWorkload(
        engine="gmsh", source_bytes=1, surface_triangles=1,
        requested_cells=100_000_001, region_count=1, boundary_layers=0))

    assert decision.profile is MeshExecutionProfile.ULTRAMEM
    assert decision.reasons == ("requested_cells exceeds high-memory ceiling",)


def test_snappy_with_boundary_layers_adds_capacity_pressure():
    decision = select_profile(MeshWorkload(
        engine="snappy", requested_cells=7_000_000, boundary_layers=8))

    assert decision.profile is MeshExecutionProfile.HIGH_MEMORY
    assert decision.reasons == ("snappy boundary layers exceed standard complexity",)


def test_vmtk_with_boundary_layers_adds_capacity_pressure():
    decision = select_profile(MeshWorkload(
        engine="vmtk", requested_cells=7_000_000, boundary_layers=8))

    assert decision.profile is MeshExecutionProfile.HIGH_MEMORY
    assert decision.reasons == ("vmtk boundary layers exceed standard complexity",)


@pytest.mark.parametrize("layers,expected", [
    (6, MeshExecutionProfile.STANDARD),
    (7, MeshExecutionProfile.HIGH_MEMORY),
    (20, MeshExecutionProfile.HIGH_MEMORY),
    (21, MeshExecutionProfile.ULTRAMEM),
])
def test_boundary_layer_thresholds_are_exact(layers, expected):
    decision = select_profile(MeshWorkload(
        engine="snappy", source_bytes=1, surface_triangles=1,
        requested_cells=1, region_count=1, boundary_layers=layers))

    assert decision.profile is expected


def test_unknown_measurements_default_to_standard_with_an_explanation():
    decision = select_profile(MeshWorkload(engine="gmsh"))

    assert decision.profile is MeshExecutionProfile.STANDARD
    assert decision.reasons == ("no sizing evidence available",)


def test_partial_evidence_that_selects_standard_names_the_missing_signals():
    decision = select_profile(MeshWorkload(engine="gmsh", requested_cells=4_000_000))

    assert decision.profile is MeshExecutionProfile.STANDARD
    assert decision.reasons == (
        "sizing evidence incomplete: source_bytes, surface_triangles, region_count, "
        "boundary_layers unavailable",
    )


@pytest.mark.parametrize("field", [
    "source_bytes", "surface_triangles", "requested_cells", "region_count", "boundary_layers",
])
def test_negative_measurements_are_rejected(field):
    with pytest.raises(ValueError, match=field):
        MeshWorkload(engine="gmsh", **{field: -1})


@pytest.mark.parametrize("failure", [
    MeshFailure.OUT_OF_MEMORY,
    MeshFailure.CAPACITY_TIMEOUT,
    MeshFailure.ARTIFACT_CAPACITY,
])
@pytest.mark.parametrize("current,expected", [
    (MeshExecutionProfile.STANDARD, MeshExecutionProfile.HIGH_MEMORY),
    (MeshExecutionProfile.HIGH_MEMORY, MeshExecutionProfile.ULTRAMEM),
])
def test_capacity_failure_escalates_exactly_one_profile(failure, current, expected):
    assert escalation_profile(current, failure, automatic_escalations=0) is expected


def test_ultramem_has_no_higher_profile():
    assert escalation_profile(
        MeshExecutionProfile.ULTRAMEM, MeshFailure.OUT_OF_MEMORY,
        automatic_escalations=0) is None


@pytest.mark.parametrize("failure", [
    MeshFailure.OUT_OF_MEMORY,
    MeshFailure.CAPACITY_TIMEOUT,
    MeshFailure.ARTIFACT_CAPACITY,
])
def test_a_run_that_already_escalated_does_not_escalate_again(failure):
    assert escalation_profile(
        MeshExecutionProfile.HIGH_MEMORY, failure,
        automatic_escalations=1) is None


@pytest.mark.parametrize("failure", [
    MeshFailure.GEOMETRY,
    MeshFailure.VALIDATION,
    MeshFailure.INFRASTRUCTURE,
])
def test_non_capacity_failure_does_not_escalate(failure):
    assert escalation_profile(
        MeshExecutionProfile.STANDARD, failure,
        automatic_escalations=0) is None
