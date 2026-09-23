# Responsibility: The platform's two package-stage settings actually reach the measurement package, and reach it
#                 before the file is opened; off, they leave the package at its own shipped default.
# Boundaries: the policy seam and the two functions that arm it. What the package then DOES with the switch is
#             the package's own tests and the corpus runs; this file only proves the wire is connected.
#
# WHY THIS FILE EXISTS. `arm_the_package` was written, documented as "called at the two points the package
# consults them", and called from nowhere. With it dead, GEOMETRY_FLUID_SIDE_ENABLED and
# GEOMETRY_MEASURED_STOPS_ENABLED were switches an operator could set and nothing would happen: the package
# reads its OWN environment variables and never sees a platform attribute. Gaps B and C were unreachable
# through the platform's own configuration, which is the only way anybody turns them on.
#
# THEY SURVIVED THE FLAG CLEANUP, and this file is where the reason is checked rather than asserted: they are
# not "is the feature on" for anything this platform does. Each is a stage inside another component, each
# costs something real - a pass over the mesh at upload, a question put to the customer - and each is still
# off unless somebody asks for it.
from __future__ import annotations

import os

import pytest

import meshpipeline.settings.package_switches as package_switches
import meshpipeline.settings.policy as polcfg

THEIRS = ("GEOMETRY_AGENT_MEASURED_STOPS", "GEOMETRY_AGENT_FLUID_SIDE")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """The package's own variables absent, which is what a fresh worker has."""
    for name in THEIRS:
        monkeypatch.delenv(name, raising=False)


def test_the_two_platform_settings_map_to_the_packages_own_variable_names():
    """Spelled once. A typo here is a switch that silently does nothing, which is what this file is about."""
    assert package_switches.PACKAGE_SWITCHES == {
        "GEOMETRY_AGENT_MEASURED_STOPS": "GEOMETRY_MEASURED_STOPS_ENABLED",
        "GEOMETRY_AGENT_FLUID_SIDE": "GEOMETRY_FLUID_SIDE_ENABLED"}


def test_both_are_still_settings_and_both_are_still_off_by_default():
    """The five gates over the Surveyor are gone; these two are not gates over it and did not go with them."""
    assert polcfg.GEOMETRY_MEASURED_STOPS_ENABLED is False
    assert polcfg.GEOMETRY_FLUID_SIDE_ENABLED is False


def test_off_arms_the_package_to_off_and_never_to_nothing(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASURED_STOPS_ENABLED", False)
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", False)
    assert polcfg.arm_the_package() == dict.fromkeys(THEIRS, "off")
    assert [os.environ[k] for k in THEIRS] == ["off", "off"]


def test_on_arms_the_package_on(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASURED_STOPS_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", True)
    assert polcfg.arm_the_package() == dict.fromkeys(THEIRS, "on")
    assert [os.environ[k] for k in THEIRS] == ["on", "on"]


def test_an_operators_own_export_wins_over_the_platform_setting(monkeypatch):
    """A developer who exported the package's variable meant it; the platform must not overwrite it, or the
    package's own tests and evals stop being runnable in the same shell."""
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", False)
    monkeypatch.setenv("GEOMETRY_AGENT_FLUID_SIDE", "on")
    assert polcfg.arm_the_package()["GEOMETRY_AGENT_FLUID_SIDE"] == "on"
    assert os.environ["GEOMETRY_AGENT_FLUID_SIDE"] == "on"


def test_arming_twice_says_the_same_thing(monkeypatch):
    """Idempotent, because it is called once per measurement and once per composition in one worker."""
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASURED_STOPS_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", False)
    assert polcfg.arm_the_package() == polcfg.arm_the_package()


def test_the_words_written_are_words_the_package_accepts():
    """`on` and `off`, not `true`/`false`: the package RAISES on anything it does not know, so a platform that
    wrote `True` would turn a measurement into an exception rather than a switch."""
    measure = pytest.importorskip("geometry_agent.facts.measure")
    catalog = pytest.importorskip("geometry_agent.agent.catalog")
    assert measure.measured_stops_on({"GEOMETRY_AGENT_MEASURED_STOPS": "on"}) is True
    assert measure.measured_stops_on({"GEOMETRY_AGENT_MEASURED_STOPS": "off"}) is False
    assert catalog.fluid_side_on({"GEOMETRY_AGENT_FLUID_SIDE": "on"}) is True
    assert catalog.fluid_side_on({"GEOMETRY_AGENT_FLUID_SIDE": "off"}) is False


def test_the_measurement_arms_the_package_before_it_opens_the_file(monkeypatch):
    """BEFORE, because `measure_isolated` measures in a child process that inherits this environment. A flag
    armed after the spawn arrives too late and the stage silently does not run."""
    from meshpipeline.application import geometry_measurement as gm
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASURED_STOPS_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", True)
    order: list[str] = []
    monkeypatch.setattr(polcfg, "arm_the_package", lambda: order.append("armed") or {})

    def _refuse():
        order.append("package read")
        raise gm._Unavailable("not for this test")

    monkeypatch.setattr(gm, "_package", _refuse)
    gm.measure_local_file(gm.Path("no-such-file.step"))
    assert order[0] == "armed", f"the package was reached before the switches were armed: {order}"


def test_composing_a_survey_arms_the_package_before_the_side_is_read(monkeypatch):
    """The side is read inside `catalog` off the environment, so the arming has to happen first here too."""
    from meshpipeline.application import geometry_survey as gs
    order: list[str] = []
    monkeypatch.setattr(polcfg, "arm_the_package", lambda: order.append("armed") or {})
    with pytest.raises(gs.SurveyError):
        gs.composition({"status": "no"}, purpose="internal_cfd")
    assert order == ["armed"], "a composition that refuses still has to arm before it reads anything"
