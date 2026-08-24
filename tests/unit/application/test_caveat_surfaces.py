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
