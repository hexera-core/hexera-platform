# Responsibility: Verify no secret, payload or model reasoning survives projection into a run record.
# Boundaries: the emitted keys are exactly the allow-list, and a failing extension is dropped rather than fatal.
from __future__ import annotations

import json

import pytest

from meshpipeline.agents.builder.diagnostics import BuilderRunExtension
from meshpipeline.agents.intake.diagnostics import IntakeRunExtension
from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.loop.accounting import AgentRunAccountant, ToolInvocation
from meshpipeline.agents.reviewer.diagnostics import ReviewRunExtension
from meshpipeline.contracts.agent_loop import AgentRole, LoopExit, LoopLimits
from meshpipeline.contracts.model_inference import ToolCallRequest

SECRETS = [
    "sk-live-DEADBEEF",                                   # credential
    "Bearer eyJhbGciOi",                                  # auth header
    "You are a mesh-quality reviewer",                    # system prompt
    "let me think step by step about the wing",           # model reasoning
    "https://minio.local/bucket/mesh.tgz?X-Amz-Signature=abc",   # signed URL
    "solid wing\nfacet normal 0 0 1",                     # geometry
    '{"patch_colors": {"aircraft": "neon red"}}',         # raw tool arguments
]


def _record(extension):
    a = AgentRunAccountant(role=AgentRole.reviewer, job_id="job-1", limits=LoopLimits(),
                           pipeline_attempt=1, agent_attempt=1)
    inv = ToolInvocation.of(ToolCallRequest(id="c1", name="zoom", arguments=SECRETS[6]),
                            round_index=1, call_index=1, parsed={"secret": SECRETS[0]})
    inv.result = {"image_b64": "AAAA", "prompt": SECRETS[2], "reasoning": SECRETS[3]}
    a.record_tool_call(inv, category="navigation")
    return a.report(exit=LoopExit.rounds_exhausted, extension=extension,
                    failure_marker="reviewer_evidence_missing")


@pytest.mark.parametrize("secret", SECRETS)
def test_no_secret_survives_the_projection(secret):
    blob = json.dumps(diagnostics.sanitized(_record(ReviewRunExtension())))
    assert secret not in blob


def test_the_projection_is_json_serializable_end_to_end():
    json.dumps(diagnostics.sanitized(_record(ReviewRunExtension(
        required_axes=("surface_capture",), missing_axes=("wake_resolution",)))))


def test_an_extension_cannot_widen_the_envelope_with_a_non_scalar():
    class _Rogue:
        def sanitized(self):
            return {"ok": 3, "prompt_blob": {"system": SECRETS[2]},
                    "images": [b"\x89PNG"], "reasoning": SECRETS[3], "axes": ("a", "b")}
    out = diagnostics.sanitize_extension(_Rogue())
    assert out == {"ok": 3, "reasoning": SECRETS[3], "axes": ("a", "b")}
    assert "prompt_blob" not in out and "images" not in out


def test_an_extension_that_raises_is_dropped_not_fatal():
    class _Broken:
        def sanitized(self):
            raise RuntimeError("boom")
    assert diagnostics.sanitize_extension(_Broken()) == {}


def test_the_emitted_keys_are_exactly_the_allow_list():
    out = diagnostics.sanitized(_record(ReviewRunExtension()))
    assert set(out) == {"role", "job_id", "pipeline_attempt", "agent_attempt", "exit",
                        "failure_marker", "tally", "calls_by_category", "limits",
                        "rounds", "tool_calls", "extension"}
    for call in out["tool_calls"]:
        assert set(call) == set(diagnostics.TOOL_CALL_FIELDS)


def test_a_new_record_field_is_invisible_until_deliberately_added():
    rec = _record(ReviewRunExtension())
    object.__setattr__(rec, "smuggled_prompt", SECRETS[2])
    assert "smuggled_prompt" not in json.dumps(diagnostics.sanitized(rec))


# the three agent extensions
@pytest.mark.parametrize("ext", [
    IntakeRunExtension(missing_fields=("purpose",), tools_invoked=("submit_requirements",),
                       authorization_state="unauthorized", canonical_revision="rev-3"),
    BuilderRunExtension(engine="snappy", mode="retry", run_mesh_calls=2, submitted=True,
                        forced_tools=("submit_mesh",)),
    ReviewRunExtension(required_axes=("surface_capture", "wake_resolution"),
                       missing_axes=("wake_resolution",), eligibility_rejections=9,
                       rejection_reasons=("axis 'wake_resolution' has no finding",)),
])
def test_every_agent_extension_flattens_to_sanitized_scalars(ext):
    flat = diagnostics.sanitize_extension(ext)
    assert flat, "an extension must contribute something"
    for key, value in flat.items():
        assert isinstance(value, (str, int, float, bool, tuple)), (key, type(value))
        if isinstance(value, tuple):
            assert all(isinstance(v, str) for v in value)


def test_the_reviewer_extension_never_defaults_a_verdict():
    assert ReviewRunExtension().accepted_verdict is None
    assert "accepted_verdict" not in ReviewRunExtension().sanitized()


def test_an_accepted_verdict_is_emitted_as_its_value():
    from meshpipeline.contracts.review_outcome import ReviewVerdict
    flat = ReviewRunExtension(accepted_verdict=ReviewVerdict.failed).sanitized()
    assert flat["accepted_verdict"] == "failed"


def test_the_reviewer_extension_carries_identifiers_not_prose():
    ext = ReviewRunExtension(
        required_axes=("surface_capture",), missing_axes=("surface_capture",),
        invalid_evidence_refs=("t-999",), rejection_reasons=("axis 'x' has no finding",))
    flat = diagnostics.sanitize_extension(ext)
    assert flat["missing_axes"] == ("surface_capture",)
    assert flat["invalid_evidence_refs"] == ("t-999",)
    assert "finding" not in set(flat) and "reasoning" not in set(flat)


def test_the_neutral_contract_cannot_represent_model_reasoning():
    import dataclasses

    from meshpipeline.contracts import agent_loop
    for cls in (agent_loop.ToolCallRecord, agent_loop.RoundRecord, agent_loop.LoopTally,
                agent_loop.AgentRunRecord):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not any("reasoning" in n or "prompt" in n or "message" in n for n in names), cls


def test_reasoning_text_reaches_no_run_record(monkeypatch):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo
    a = AgentRunAccountant(role=AgentRole.builder, job_id="j", limits=LoopLimits())
    a.record_round(ModelRoundResult(assistant_text="visible",
                                    reasoning_text="PRIVATE CHAIN OF THOUGHT",
                                    finish_reason="stop",
                                    provider=ProviderAttemptInfo(1, "p", "m")))
    blob = json.dumps(diagnostics.sanitized(
        a.report(exit=LoopExit.terminal_action, extension=BuilderRunExtension())))
    assert "PRIVATE CHAIN OF THOUGHT" not in blob and "visible" not in blob


def test_emission_never_fails_the_agent(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("training log down")
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _boom)
    assert diagnostics.emit(_record(ReviewRunExtension()))["role"] == "reviewer"
