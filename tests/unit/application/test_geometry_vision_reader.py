# Responsibility: Verify the look reads with the named reader or not at all, is taken for the customer's purpose, and waits for step 1 when the survey is on.
# Boundaries: the platform's choice of reader and of when to look; what a reader says is the measurement package's.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import geometry_measurement, geometry_vision

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"


def test_the_reader_defaults_to_the_cheap_one_the_look_was_measured_with():
    assert polcfg.GEOMETRY_VISION_PROVIDER == "openai"
    assert polcfg.GEOMETRY_VISION_MODEL == "gpt-5.6-luna"


@pytest.fixture
def client_module():
    return require("geometry_agent.vision.client", needs="the reader the look is taken with")


def test_the_named_provider_and_model_are_the_ones_used(monkeypatch, client_module):
    monkeypatch.setattr(client_module.text_client, "load_dotenv_files", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("GEOMETRY_AGENT_VISION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_VISION_MODEL", raising=False)
    reader = geometry_vision.reader()
    assert (reader.provider, reader.model) == ("openai", "gpt-5.6-luna")


def test_a_provider_with_no_key_is_no_look_and_never_another_provider(monkeypatch, client_module):
    """This platform's own template carries DEEPINFRA_API_KEY and nothing else a look could use. Left to
    the package's `auto`, every part would be read by DeepInfra's Qwen."""
    monkeypatch.setattr(client_module.text_client, "load_dotenv_files", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "k")
    assert client_module.vision_client_from_env("auto").provider == "deepinfra"
    assert geometry_vision.reader() is None


def test_with_no_reader_the_row_says_why_and_nothing_was_rendered(monkeypatch, tmp_path):
    require("geometry_agent.agent.hexera", needs="the look block stored on the row")
    monkeypatch.setattr(geometry_vision, "reader", lambda: None)
    called = []
    monkeypatch.setattr(geometry_vision, "_package", lambda: (
        lambda *a, **k: called.append(1), object, __import__("geometry_agent.agent.hexera", fromlist=["x"])))
    block = geometry_vision.look_at_local_file(tmp_path / "x.step", {"status": "ok"})
    assert called == []
    assert block["status"] == "not_attempted"
    assert "never falls through to another provider" in block["reason"]


def test_the_look_is_taken_for_the_purpose_and_representation_the_customer_decided(monkeypatch, tmp_path):
    seen: dict = {}

    def look_at_geometry(path, facts, **kw):
        seen.update(kw)
        return {"status": "ok"}

    monkeypatch.setattr(geometry_vision, "reader", lambda: object())
    monkeypatch.setattr(geometry_vision, "_package", lambda: (look_at_geometry, object, object()))
    geometry_vision.look_at_local_file(tmp_path / "x.step", {"status": "ok", "measured_purpose": "internal_cfd"},
                                       purpose="external_cfd", representation="external")
    assert (seen["purpose"], seen["representation"]) == ("external_cfd", "external")
    assert seen["client"] is not None
    geometry_vision.look_at_local_file(tmp_path / "x.step", {"status": "ok", "measured_purpose": "internal_cfd"})
    assert (seen["purpose"], seen["representation"]) == ("internal_cfd", None)


def test_the_upload_never_queues_the_look(monkeypatch):
    """Step 3 comes after step 1: a look taken for the purpose ASSUMED at upload reads an external body
    as internal flow, so the measurement records what will become of the look and queues nothing."""
    queued = []
    monkeypatch.setattr("meshpipeline.contracts.geometry_measurement.enqueue_look",
                        lambda *a, **k: queued.append(a) or True)
    assert geometry_measurement._look_outcome("ok") == "deferred_to_survey"
    assert queued == []


def test_the_survey_queues_the_look_at_step_three_unless_it_was_already_taken(monkeypatch):
    require("geometry_agent.contract.deliver", needs="the survey that queues the look")
    from meshpipeline.application import geometry_survey as gs

    queued = []
    monkeypatch.setattr("meshpipeline.contracts.geometry_measurement.enqueue_look",
                        lambda *a, **k: queued.append(a) or True)
    doc = json.loads((FIXTURES / "venturi_orifice_001.json").read_text(encoding="utf-8"))
    assert gs._queue_the_look("s", "o", doc) == "queued"
    assert gs._queue_the_look("s", "o", {**doc, "look": {"status": "ok"}}) == "cached"
    assert len(queued) == 1
