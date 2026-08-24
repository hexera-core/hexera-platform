# Responsibility: Pin the delivery surfaces: a caveated success renders its deviations BEFORE
# any success language, the caveats round-trip FinalResult v5, and v4 history still renders
# (caveat-free by construction).
from __future__ import annotations

from meshpipeline.application.final_result import (
    FINAL_RESULT_SCHEMA_VERSION,
    FinalResult,
    TerminalStatus,
    build_final_result,
    render_message,
)

CAVEAT = {"direction": "downstream", "requested": 8.0, "measured": 6.67,
          "units": "reference_lengths", "ruler_m": 0.06, "ruler_source": "user_stated"}


def _built(**over):
    base = dict(job_id="j", owner_id="o", status=TerminalStatus.succeeded,
                engine="snappy", purpose="external_cfd", dimensionality="3D",
                approved_snapshot_id="s", executor_success=True,
                reviewer_verdict="PASS", failed_gate="", api_failure="",
                attempts=3, attempts_max=3, required_ready=True,
                delivered_types=["mesh"], optional_warnings=[])
    base.update(over)
    return build_final_result(**base)


class TestCaveatedDeliveryMessage:
    def test_the_deviations_come_before_any_success_language(self):
        msg = render_message(_built(requirement_caveats=[CAVEAT]))
        assert msg.index("stated deviations") < msg.index("completed successfully")
        assert "downstream margin: requested 8, delivered 6.67" in msg

    def test_a_clean_success_is_byte_identical_to_before(self):
        msg = render_message(_built())
        assert "deviation" not in msg
        assert msg.startswith("Mesh generation completed successfully.")


class TestSchemaRoundTrip:
    def test_v5_roundtrips_the_caveats(self):
        d = _built(requirement_caveats=[CAVEAT]).to_dict()
        assert d["schema_version"] == FINAL_RESULT_SCHEMA_VERSION == 5
        back = FinalResult.from_dict(d)
        assert back.requirement_caveats == [CAVEAT]

    def test_v4_history_still_renders_caveat_free(self):
        d = _built().to_dict()
        d["schema_version"] = 4
        d.pop("requirement_caveats", None)
        back = FinalResult.from_dict(d)
        assert back.requirement_caveats == []

    def test_v3_and_unknown_versions_still_refuse(self):
        import pytest
        d = _built().to_dict()
        d["schema_version"] = 3
        with pytest.raises(ValueError, match="refusing to render"):
            FinalResult.from_dict(d)


class TestDispatchRecomputeAgreesWithApproval:
    def test_a_v5_intent_with_extents_round_trips_the_fingerprint(self):
        # the live heat-sink replay was refused at dispatch because the contract recompute
        # omitted the v5 fields - the three calculators must agree forever
        from meshpipeline.agents.intake.admission_token import approved_intent_fingerprint
        from meshpipeline.application.dispatch_contract import recompute_intent_fingerprint

        geometry = {"sha256": "0" * 64, "bytes": 10, "schema_version": 1,
                    "revision_id": None}
        kw = dict(engine="snappy", purpose="external_cfd", input_kind="body-surface",
                  dimensionality="3D", patches=[{"name": "body", "type": "wall"}],
                  engine_params={}, request_txt="r", geometry=geometry,
                  requested_extents={"downstream": 8.0}, reference_length_m=0.06,
                  requirements_strict=False)
        approved = approved_intent_fingerprint(**kw)
        payload = {"mesh_engine": "snappy", "purpose": "external_cfd",
                   "input_kind": "body-surface", "dimensionality": "3D",
                   "intake_patches": [{"name": "body", "type": "wall"}],
                   "engine_params": {}, "request_txt": "r",
                   "requested_extents": {"downstream": 8.0},
                   "reference_length_m": 0.06, "requirements_strict": False,
                   "geometry_source": None}
        import meshpipeline.application.dispatch_contract as dc
        # bypass source-ref plumbing: both sides must hash the same geometry identity
        orig = dc.source_ref_of
        dc.source_ref_of = lambda _p: None
        try:
            from unittest.mock import patch as _patch
            with _patch("meshpipeline.agents.intake.admission_token.geometry_identity",
                        return_value=geometry):
                recomputed = dc.recompute_intent_fingerprint(payload)
        finally:
            dc.source_ref_of = orig
        assert recomputed == approved
