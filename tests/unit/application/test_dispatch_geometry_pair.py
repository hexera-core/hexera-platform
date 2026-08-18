# Responsibility: Verify a geometry source and its interpretation travel together or not at all, at build and on reload.
from __future__ import annotations

import pytest

from meshpipeline.application.dispatch_contract import (
    DispatchContractError,
    build,
    to_run_kwargs,
    validate,
)

SOURCE = {
    "source_id": "11111111-1111-4111-8111-111111111111",
    "owner_id": "owner-1",
    "object_key": "sources/11111111-1111-4111-8111-111111111111",
    "sha256": "a" * 64,
    "original_filename": "part.step",
    "suffix_hint": ".step",
    "size_bytes": 1024,
}
INTERPRETATION = {
    "interpretation_id": "22222222-2222-4222-8222-222222222222",
    "geometry_source_id": "11111111-1111-4111-8111-111111111111",
    "unit": "mm",
    "scale_to_metres": 1e-3,
    "basis": "user_confirmed",
    "evidence": "declared",
}


def _payload(**over):
    base = {"job_id": "job-1", "owner_id": "owner-1", "mesh_engine": "cfmesh", "domain": "d"}
    base.update(over)
    return base


def test_a_run_may_carry_no_geometry_at_all():
    # A programmatic submit meshes a parametric domain and approves no bytes. That is not the
    # same as losing an interpretation, and must stay dispatchable.
    payload = build(**_payload())
    assert "geometry_source" not in to_run_kwargs(payload)


def test_source_and_interpretation_together_are_accepted():
    payload = build(**_payload(geometry_source=SOURCE, geometry_interpretation=INTERPRETATION))
    kwargs = to_run_kwargs(payload)
    assert kwargs["geometry_source"] == SOURCE
    assert kwargs["geometry_interpretation"] == INTERPRETATION


def test_source_without_interpretation_is_refused_at_build():
    with pytest.raises(DispatchContractError, match="no geometry_interpretation"):
        build(**_payload(geometry_source=SOURCE))


def test_interpretation_without_source_is_refused_at_build():
    with pytest.raises(DispatchContractError, match="no geometry_source"):
        build(**_payload(geometry_interpretation=INTERPRETATION))


def test_a_stored_payload_that_lost_its_interpretation_is_refused_on_RELOAD():
    payload = build(**_payload(geometry_source=SOURCE, geometry_interpretation=INTERPRETATION))
    del payload["geometry_interpretation"]
    with pytest.raises(DispatchContractError, match="no geometry_interpretation"):
        to_run_kwargs(payload, where="reconstruction")


def test_an_empty_interpretation_is_not_an_interpretation():
    # None and {} are the two shapes a dropped field actually takes in a JSON round-trip.
    for empty in (None, {}):
        with pytest.raises(DispatchContractError, match="no geometry_interpretation"):
            build(**_payload(geometry_source=SOURCE, geometry_interpretation=empty))


def test_the_rejection_names_the_producer_boundary():
    payload = {"schema_version": 2, **_payload(geometry_source=SOURCE)}
    with pytest.raises(DispatchContractError, match="cloud-run reconstruction"):
        validate(payload, where="cloud-run reconstruction")


def test_the_pair_check_does_not_invent_a_default_unit():
    with pytest.raises(DispatchContractError):
        build(**_payload(geometry_source=SOURCE))
    payload = build(**_payload(geometry_source=SOURCE, geometry_interpretation=INTERPRETATION))
    # nothing was added on the way through
    assert payload["geometry_interpretation"] == INTERPRETATION
    assert payload["geometry_interpretation"]["scale_to_metres"] == 1e-3


def test_a_malformed_interpretation_is_refused_not_carried():
    bad = {**INTERPRETATION, "unit": "furlong"}
    with pytest.raises(DispatchContractError, match="unsupported geometry unit"):
        build(**_payload(geometry_source=SOURCE, geometry_interpretation=bad))


def test_an_unsanctioned_scale_factor_is_refused():
    bad = {**INTERPRETATION, "scale_to_metres": 0.5}
    with pytest.raises(DispatchContractError, match="does not match"):
        build(**_payload(geometry_source=SOURCE, geometry_interpretation=bad))


def test_an_interpretation_missing_a_field_is_refused():
    bad = {k: v for k, v in INTERPRETATION.items() if k != "interpretation_id"}
    with pytest.raises(DispatchContractError):
        build(**_payload(geometry_source=SOURCE, geometry_interpretation=bad))


def test_an_interpretation_for_DIFFERENT_bytes_is_refused_at_dispatch():
    other = {**INTERPRETATION, "geometry_source_id": "99999999-9999-4999-8999-999999999999"}
    with pytest.raises(DispatchContractError, match="another file's scale"):
        build(**_payload(geometry_source=SOURCE, geometry_interpretation=other))
