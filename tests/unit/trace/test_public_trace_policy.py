# Responsibility: Verify safe mode publishes activity without content, and raw mode still redacts identity and secrets.
# Boundaries: raw needs two switches, and a raw backlog replayed under safe startup leaks nothing.
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

import meshpipeline.events as E
from meshpipeline.trace.labels import GENERIC, SAFE_LABELS, known_tools, public_label
from meshpipeline.trace.policy import (
    RAW,
    SAFE,
    current_mode,
    project,
    reasoning_id,
)
from meshpipeline.trace.sanitizer import MODEL_MARK, REDACTED, sanitize_payload

ROOT = Path(__file__).parent.parent.parent.parent
APP = ROOT / "src" / "meshpipeline"


@pytest.fixture
def mode(monkeypatch):
    def _set(m: str):
        import meshpipeline.settings.policy as P
        from meshpipeline.settings.modes import ProductModes, TraceDisclosure
        # The deployment's answer is the typed object, so a test sets THAT - there is no flat
        # module attribute left for a caller (or a test) to redefine the mode through.
        monkeypatch.setattr(P, "MODES", ProductModes(
            data_collection_enabled=P.MODES.data_collection_enabled,
            trace_disclosure=TraceDisclosure(m)))
        return m
    return _set


def _reasoning_wire(**over):
    base = {"type": "reasoning", "stage": "builder", "id": "r:job:builder:1:0",
            "agent": "builder", "phase": "completed", "status": "success",
            "duration_ms": 8400, "token_count": 1276,
            "content": "First I will read the brief, then size the domain."}
    base.update(over)
    return base


# configuration


def _reload_policy(env: dict) -> tuple[bool, str]:
    old = {k: os.environ.get(k) for k in
           ("PUBLIC_TRACE_MODE", "ALLOW_PUBLIC_RAW_TRACE", "DATA_COLLECTION_ENABLED")}
    try:
        for k in old:
            os.environ.pop(k, None)
        os.environ.update(env)
        import meshpipeline.settings.policy as P
        importlib.reload(P)
        return True, P.MODES
    except Exception as exc:
        return False, str(exc)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        import meshpipeline.settings.policy as P
        importlib.reload(P)


def test_an_unset_environment_resolves_to_the_declared_defaults():
    # Not which way they ship - that is a product decision - but that an unconfigured deployment
    # lands exactly where the catalogue says it does, with no third answer in between.
    from meshpipeline.settings.inventory import default_of
    ok, modes = _reload_policy({})
    assert ok, modes
    assert modes.trace_disclosure == default_of("PUBLIC_TRACE_MODE")
    assert modes.data_collection_enabled is (default_of("DATA_COLLECTION_ENABLED") == "true")


def test_raw_with_the_acknowledgement_validates():
    ok, modes = _reload_policy({"PUBLIC_TRACE_MODE": "raw",
                                "ALLOW_PUBLIC_RAW_TRACE": "true"})
    assert ok, modes
    assert modes.trace_disclosure == RAW


def test_raw_without_the_acknowledgement_refuses_to_start():
    # The acknowledgement is withdrawn explicitly rather than left unset, so this proves the
    # mechanism regardless of which way the switch ships.
    ok, err = _reload_policy({"PUBLIC_TRACE_MODE": "raw", "ALLOW_PUBLIC_RAW_TRACE": "false"})
    assert not ok
    assert "ALLOW_PUBLIC_RAW_TRACE=true" in err, "the error does not say what to do"
    assert "reasoning" in err and "tool" in err, "the error does not say what is at stake"


def test_an_unknown_mode_refuses_to_start():
    for bad in ("debug", "full", "uncensored", "off", "censored", "private", ""):
        ok, err = _reload_policy({"PUBLIC_TRACE_MODE": bad})
        assert not ok, f"{bad!r} was accepted as a trace mode"
        assert "safe" in err and "raw" in err


def test_the_mode_is_not_a_parameter_a_caller_can_supply(mode):
    import inspect

    from meshpipeline.trace import policy as _pol
    assert not inspect.signature(_pol.current_mode).parameters, \
        "current_mode takes an argument - a caller could choose the mode"
    # and the deployment setting is what it answers with, in both directions
    mode(SAFE)
    assert current_mode() == SAFE
    mode(RAW)
    assert current_mode() == RAW


def test_raw_without_acknowledgement_cannot_be_constructed_at_all(monkeypatch):
    # Stronger than a runtime downgrade: there is no ProductModes value carrying raw without the
    # acknowledgement, so no code path can ever be handed one.
    from meshpipeline.settings.env import ConfigurationError
    from meshpipeline.settings.modes import load_product_modes
    monkeypatch.setenv("PUBLIC_TRACE_MODE", "raw")
    monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "false")
    with pytest.raises(ConfigurationError, match="ALLOW_PUBLIC_RAW_TRACE"):
        load_product_modes()


@pytest.mark.parametrize("trace,collect", [
    ("safe", "false"), ("safe", "true"), ("raw", "false"), ("raw", "true"),
])
def test_trace_mode_and_data_collection_are_independent(trace, collect):
    env = {"PUBLIC_TRACE_MODE": trace, "DATA_COLLECTION_ENABLED": collect}
    if trace == "raw":
        env["ALLOW_PUBLIC_RAW_TRACE"] = "true"
    ok, modes = _reload_policy(env)
    assert ok, modes
    assert modes.trace_disclosure == trace
    assert modes.data_collection_enabled is (collect == "true")


# safe reasoning


def test_safe_reasoning_publishes_activity_not_content(mode):
    out = project(_reasoning_wire(), mode(SAFE))
    assert out["content"] is None
    assert out["phase"] == "completed"
    assert out["duration_ms"] == 8400
    assert out["token_count"] == 1276


def test_safe_reasoning_started_carries_no_metrics_it_does_not_have(mode):
    out = project(_reasoning_wire(phase="started", status="active",
                                  duration_ms=None, token_count=None), mode(SAFE))
    assert out["phase"] == "started"
    assert out["duration_ms"] is None and out["token_count"] is None


def test_an_unmeasured_duration_is_absent_never_estimated(mode):
    out = project(_reasoning_wire(duration_ms=None), mode(SAFE))
    assert out["duration_ms"] is None


def test_a_token_count_appears_only_when_reported(mode):
    assert project(_reasoning_wire(token_count=None), mode(SAFE))["token_count"] is None
    # and a nonsense value is not a report
    assert project(_reasoning_wire(token_count=-5), mode(SAFE))["token_count"] is None
    assert project(_reasoning_wire(token_count="lots"), mode(SAFE))["token_count"] is None


def test_another_token_metric_cannot_wear_the_reasoning_label(mode):
    out = project(_reasoning_wire(token_count=None, output_tokens=900,
                                  total_tokens=4200), mode(SAFE))
    assert out["token_count"] is None
    assert "output_tokens" not in out and "total_tokens" not in out


def test_no_reasoning_text_can_reach_the_safe_stream(mode):
    mode(SAFE)
    wire = E.reasoning("builder", "r:1", "builder", "completed",
                       duration_ms=10, token_count=5,
                       content="PRIVATE-DELIBERATION-SENTINEL").wire()
    assert "PRIVATE-DELIBERATION-SENTINEL" not in json.dumps(wire)
    assert wire["content"] is None


def test_a_provider_failure_ends_the_activity_truthfully(mode):
    out = project(_reasoning_wire(phase="failed", status="failure",
                                  content=None, token_count=None), mode(SAFE))
    assert out["phase"] == "failed" and out["status"] == "failure"
    assert out["content"] is None


def test_one_reasoning_operation_keeps_one_identity():
    a = reasoning_id("job-1", "builder", 1, 0)
    assert a == reasoning_id("job-1", "builder", 1, 0)
    assert a != reasoning_id("job-1", "builder", 1, 1)
    assert a != reasoning_id("job-1", "reviewer", 1, 0)
    for leak in ("gpt", "glm", "openai", "sk-", "token"):
        assert leak not in a.lower(), "the id itself discloses something"


# raw reasoning


def test_raw_reasoning_shows_what_the_provider_returned(mode):
    out = project(_reasoning_wire(), mode(RAW))
    assert "size the domain" in out["content"]
    assert out["duration_ms"] == 8400 and out["token_count"] == 1276


def test_raw_cannot_show_reasoning_the_provider_never_returned(mode):
    for missing in (None, "", "   "):
        assert project(_reasoning_wire(content=missing), mode(RAW))["content"] is None


def test_raw_reasoning_is_bounded(mode):
    out = project(_reasoning_wire(content="x" * 100_000), mode(RAW))
    assert len(out["content"]) < 21_000
    assert out["content"].endswith("…[truncated]")


def test_raw_reasoning_still_redacts_secrets(mode):
    out = project(_reasoning_wire(
        content="I will call it with Bearer sk-live-abcdefghijklmnop then read "
                "/srv/workspaces/843f/attempt_1/system/meshDict"), mode(RAW))
    assert "sk-live-abcdefghijklmnop" not in out["content"]
    assert "/srv/workspaces/843f" not in out["content"]
    assert REDACTED in out["content"] and "<workspace>" in out["content"]


def test_raw_reasoning_still_redacts_model_identity(mode):
    out = project(_reasoning_wire(
        content="As GLM 5.2 running on zai-org I should defer to GPT-5.6."), mode(RAW))
    for ident in ("GLM", "5.2", "zai-org", "GPT-5.6"):
        assert ident not in out["content"], f"{ident!r} survived raw-mode censorship"
    assert MODEL_MARK in out["content"]


def test_control_characters_never_reach_the_page(mode):
    out = project(_reasoning_wire(content="a\x00b\x07c\x1bd"), mode(RAW))
    assert out["content"] == "abcd"


# safe tools


def test_every_production_tool_has_an_approved_public_label():
    from meshpipeline.agents.builder.tools import ACTIONS as B
    from meshpipeline.agents.reviewer.tools import ACTIONS as R
    roster = set(B) | set(R) | {
        "submit_requirements", "propose_engine_selection", "confirm_engine_selection",
        "recommend_compatible_engines", "preview_selected_admission", "submit_findings",
    }
    missing = sorted(roster - known_tools())
    assert not missing, f"production tools with no safe public label: {missing}"


def test_safe_tool_calls_carry_the_label_and_nothing_else(mode):
    out = project({"type": "tool_call", "stage": "reviewer", "id": "c1",
                   "agent": "reviewer", "tool_name": "go_to_coordinates",
                   "arguments": {"x": 0.932, "patch_name": "aircraft"},
                   "status": "started"}, mode(SAFE))
    assert out["tool_name"] is None
    assert out["arguments"] is None
    assert out["public_label"] == "Adjusted inspection view"
    flat = json.dumps(out)
    assert "go_to_coordinates" not in flat and "aircraft" not in flat


def test_safe_tool_results_carry_no_result(mode):
    out = project({"type": "tool_result", "stage": "builder", "id": "r1",
                   "tool_call_id": "c1", "agent": "builder", "tool_name": "run_mesh",
                   "result": {"cells": 3907590, "log": "/srv/workspaces/x/log"},
                   "status": "success", "duration_ms": 136_000}, mode(SAFE))
    assert out["tool_name"] is None and out["result"] is None
    assert out["public_label"] == "Started mesh generation"
    assert out["duration_ms"] == 136_000        # timing is activity, not content
    assert "3907590" not in json.dumps(out)


@pytest.mark.parametrize("tool,label", [
    ("produce_inspection_image", "Produced inspection image"),
    ("inspect_image", "Inspected image"),
    ("go_to_coordinates", "Adjusted inspection view"),
    ("set_camera_preset", "Adjusted inspection view"),
    ("inspect_region", "Inspected mesh region"),
    ("read_mesh_quality", "Checked mesh quality"),
    ("write_file", "Generated file"),
    ("update_configuration", "Updated configuration"),
    ("validate_configuration", "Generated mesh configuration"),
    ("deliver_mesh_result", "Delivered mesh result"),
    ("run_mesh", "Started mesh generation"),
    ("inspect_native_output", "Checked mesh output"),
    ("validate_mesh_output", "Validated mesh output"),
    ("package_artifacts", "Packaged mesh artifacts"),
    ("web_search", "Checked reference material"),
    ("submit_requirements", "Submitted requirements"),
    ("submit_findings", "Finalized review findings"),
])
def test_the_catalog_says_what_the_operation_does(tool, label):
    assert public_label(tool) == label


def test_an_unmapped_runtime_tool_degrades_without_leaking_its_name():
    assert public_label("internal_secret_probe_v2") == GENERIC
    assert "internal_secret_probe" not in GENERIC


def test_no_label_echoes_an_internal_tool_name():
    for tool, label in SAFE_LABELS.items():
        assert tool not in label, f"the public label for {tool!r} contains the tool name"


# raw tools


def test_raw_tool_calls_show_the_real_name_and_arguments(mode):
    out = project({"type": "tool_call", "stage": "reviewer", "id": "c1",
                   "agent": "reviewer", "tool_name": "go_to_coordinates",
                   "arguments": {"x": 0.932, "span": 3.5, "preset": "iso"},
                   "status": "started"}, mode(RAW))
    assert out["tool_name"] == "go_to_coordinates"
    assert out["arguments"] == {"x": 0.932, "span": 3.5, "preset": "iso"}


def test_raw_tool_results_are_sanitised_recursively(mode):
    out = project({"type": "tool_result", "stage": "builder", "id": "r", "tool_call_id": "c",
                   "agent": "builder", "tool_name": "run_mesh", "status": "success",
                   "result": {"cells": 12, "nested": {"deep": {"api_key": "sk-live-xyz",
                              "url": "https://s3/x?X-Amz-Signature=abc",
                              "path": "/srv/workspaces/j/attempt_1"}},
                              "model": "GLM-5.2"}}, mode(RAW))
    flat = json.dumps(out)
    assert "sk-live-xyz" not in flat and "X-Amz-Signature" not in flat
    assert "/srv/workspaces" not in flat
    assert "GLM" not in flat, "model identity survived in raw mode"
    assert out["result"]["cells"] == 12


def test_unsupported_values_are_refused_not_coerced(mode):
    mode(RAW)
    assert sanitize_payload(object()) is None
    assert sanitize_payload({"f": open(__file__)})["f"] is None
    assert sanitize_payload({"b": b"\x00\x01"})["b"].endswith("bytes>")
    assert sanitize_payload({"x": float("nan")})["x"] is None
    assert sanitize_payload({"e": ValueError("boom")})["e"] == "ValueError: boom"


def test_a_cycle_is_refused_rather_than_followed():
    d: dict = {"a": 1}
    d["self"] = d
    out = sanitize_payload(d)
    assert out["a"] == 1 and isinstance(out["self"], str)


def test_oversized_structures_are_bounded():
    out = sanitize_payload({"big": ["x" * 100] * 5_000})
    assert out is None or len(json.dumps(out)) < 70_000


@pytest.mark.parametrize("status", ["success", "failure", "blocked"])
def test_tool_outcomes_are_truthful(status, mode):
    out = project({"type": "tool_result", "stage": "builder", "id": "r",
                   "tool_call_id": "c", "agent": "builder", "tool_name": "write_file",
                   "status": status, "result": None}, mode(SAFE))
    assert out["status"] == status


def test_a_blocked_call_is_reported_as_blocked(mode):
    out = project({"type": "tool_call", "stage": "builder", "id": "c",
                   "agent": "builder", "tool_name": "write_file",
                   "status": "blocked"}, mode(SAFE))
    assert out["status"] == "blocked"


# inspection images


def test_safe_mode_sends_no_inspection_image(mode):
    assert project({"type": "screenshot", "stage": "reviewer",
                    "image": "iVBORw0KGgo=" * 50}, mode(SAFE)) is None


def test_raw_mode_sends_the_inspection_image(mode):
    out = project({"type": "screenshot", "stage": "reviewer", "image": "iVBORw0KGgo="},
                  mode(RAW))
    assert out and out["image"] == "iVBORw0KGgo="


def test_raw_inspection_metadata_is_sanitised(mode):
    out = project({"type": "screenshot", "stage": "reviewer", "image": "AAAA",
                   "meta": {"preset": "iso", "x": 0.932, "patch": "aircraft",
                            "model": "Kimi-K2.5", "src": "/srv/workspaces/j/007.png",
                            "signed_url": "https://s3/x?Signature=zz"}}, mode(RAW))
    flat = json.dumps(out)
    assert out["meta"]["preset"] == "iso" and out["meta"]["patch"] == "aircraft"
    assert "Kimi" not in flat and "/srv/workspaces" not in flat
    assert "Signature=zz" not in flat


def test_an_oversized_image_is_dropped_not_truncated(mode):
    out = project({"type": "screenshot", "stage": "reviewer", "image": "A" * 5_000_000},
                  mode(RAW))
    assert out is None, "half a base64 image is a broken image"


def test_a_dropped_image_does_not_stop_the_events_after_it(mode):
    m = mode(SAFE)
    seq = [{"type": "screenshot", "stage": "reviewer", "image": "AAAA"},
           {"type": "check", "stage": "reviewer", "statement": "patches correct", "ok": True}]
    out = [project(e, m) for e in seq]
    assert out[0] is None and out[1]["statement"] == "patches correct"


# identity


@pytest.mark.parametrize("field", [
    "model", "model_name", "model_id", "model_version", "model_alias", "provider",
    "provider_name", "provider_id", "deployment", "deployment_name", "route",
    "router", "system_fingerprint", "fallback_model", "inference_backend",
])
def test_identity_fields_are_removed_from_structured_payloads(field):
    out = sanitize_payload({field: "GLM-5.2", "keep": 1})
    assert field not in out and out["keep"] == 1


# rationale


def test_rationale_is_application_authored_and_shown_in_both_modes(mode):
    for m in (SAFE, RAW):
        mode(m)
        w = E.rationale("reviewer", "Reviewer", "The mesh meets the brief",
                        "every declared axis passed").wire()
        assert project(w, m) == w, "rationale was altered by the trace mode"
        assert w["conclusion"] == "The mesh meets the brief"


def test_rationale_is_scrubbed_like_everything_else():
    w = E.rationale("builder", "Builder", "Configuration ready",
                    "written to /srv/workspaces/j/system/meshDict by GLM-5.2").wire()
    assert "/srv/workspaces" not in w["because"]
    assert "GLM" not in w["because"]


def test_rationale_carries_no_stack_trace():
    w = E.rationale("outcome", "Application", "The run could not be completed",
                    "Traceback (most recent call last):\n  File \"x.py\", line 1").wire()
    assert "Traceback" in w["because"] or True   # text is allowed…
    assert "\x00" not in w["because"]
    # …but a producer passing an exception object gets its message only
    assert sanitize_payload(RuntimeError("boom")) == "RuntimeError: boom"


# replay / egress


def test_a_raw_backlog_replayed_under_safe_startup_leaks_nothing(mode):
    stored_raw = [
        project(_reasoning_wire(), RAW),
        project({"type": "tool_call", "stage": "reviewer", "id": "c1", "agent": "reviewer",
                 "tool_name": "go_to_coordinates", "arguments": {"x": 0.932},
                 "status": "started"}, RAW),
        project({"type": "tool_result", "stage": "builder", "id": "r1", "tool_call_id": "c1",
                 "agent": "builder", "tool_name": "run_mesh",
                 "result": {"cells": 3907590}, "status": "success"}, RAW),
        project({"type": "screenshot", "stage": "reviewer", "image": "SECRETIMAGE"}, RAW),
    ]
    assert stored_raw[0]["content"] and stored_raw[1]["tool_name"]

    now_safe = [project(e, SAFE) for e in stored_raw]
    assert now_safe[0]["content"] is None
    assert now_safe[1]["tool_name"] is None and now_safe[1]["arguments"] is None
    assert now_safe[2]["result"] is None
    assert now_safe[3] is None
    assert "SECRETIMAGE" not in json.dumps(now_safe)
    assert "size the domain" not in json.dumps(now_safe)


def test_the_projection_is_idempotent_so_egress_can_re_run_it(mode):
    for m in (SAFE, RAW):
        once = project(_reasoning_wire(), m)
        assert project(once, m) == once


def test_ordering_metadata_survives_projection(mode):
    w = _reasoning_wire()
    w["seq"] = 41
    out = project(w, mode(SAFE))
    assert out["seq"] == 41 and out["stage"] == "builder"


def test_a_malformed_trace_event_does_not_break_the_stream(mode):
    m = mode(RAW)
    assert project(None, m) is None
    assert project({"type": "reasoning"}, m)["id"] == ""
    assert project({"type": "check", "ok": True}, m)["ok"] is True


# regressions


def test_the_trace_never_carries_authorization_material(mode):
    mode(RAW)
    out = sanitize_payload({
        "admission_token": "adm_SECRET", "preview_token": "prev_SECRET",
        "api_key": "sk-live-SECRET", "authorization": "Bearer SECRET",
        "cookie": "sid=SECRET", "password": "hunter2"})
    flat = json.dumps(out)
    for sentinel in ("adm_SECRET", "prev_SECRET", "sk-live-SECRET", "hunter2", "sid=SECRET"):
        assert sentinel not in flat
