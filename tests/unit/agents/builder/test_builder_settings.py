# Responsibility: Verify the builder's sampling and budget settings refuse an out-of-range value at import.
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.settings.env import ConfigurationError

SETTINGS_SRC = Path(bcfg.__file__).read_text()


def test_temperature_default_is_conservative_not_one():
    assert bcfg.BUILDER_TEMPERATURE == pytest.approx(0.3)
    assert bcfg.BUILDER_TEMPERATURE < 1.0


def test_temperature_default_carries_a_documented_rationale():
    assert "provisional" in SETTINGS_SRC.lower()
    assert "optimality" in SETTINGS_SRC.lower()
    assert "BUILDER_TEMPERATURE" in SETTINGS_SRC


def _reload_with(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(bcfg)


@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    importlib.reload(bcfg)


def test_out_of_range_temperature_raises_at_import(monkeypatch):
    with pytest.raises(ConfigurationError):
        _reload_with(monkeypatch, BUILDER_TEMPERATURE="2.5")


def test_out_of_range_top_p_raises_at_import(monkeypatch):
    with pytest.raises(ConfigurationError):
        _reload_with(monkeypatch, BUILDER_TOP_P="0")   # (0.0, 1.0] - 0 is invalid


def test_nonpositive_total_budget_raises_at_import(monkeypatch):
    with pytest.raises(ConfigurationError):
        _reload_with(monkeypatch, BUILDER_TOTAL_TIMEOUT_SECONDS="0")


def test_valid_overrides_are_accepted(monkeypatch):
    mod = _reload_with(monkeypatch, BUILDER_TEMPERATURE="0.5",
                       BUILDER_TOTAL_TIMEOUT_SECONDS="7200")
    assert mod.BUILDER_TEMPERATURE == pytest.approx(0.5)
    assert mod.BUILDER_TOTAL_TIMEOUT_SECONDS == 7200
