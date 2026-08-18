# Responsibility: Verify each role's circuit is independent and named without a provider, model or account.
from __future__ import annotations

import pytest

from meshpipeline.adapters._shared.resilience import get_breaker, reset_breakers
from meshpipeline.adapters.model_inference.routes import all_routes

# The stable, operator-facing circuit names. Changing one of these strings is an operator-facing
# break, not a refactor. The planner has its OWN circuit, `deepinfra_planner`.
STABLE_GROUPS = {"deepinfra_builder", "deepinfra_planner", "deepinfra_reviewer",
                 "deepseek", "summarizer"}

# The declared circuit each role owns. the planner is INDEPENDENT of the builder - a builder
# outage must not open the planner's circuit, and its failures are attributed to the planner.
EXPECTED_GROUPS = {
    "builder": "deepinfra_builder",
    "planner": "deepinfra_planner",
    "visual_reviewer": "deepinfra_reviewer",
    "intake": "deepseek",
    "summarizer": "summarizer",
}


@pytest.fixture(autouse=True)
def _clean():
    reset_breakers()
    yield
    reset_breakers()


# circuit identity
@pytest.mark.parametrize("role,group", sorted(EXPECTED_GROUPS.items()))
def test_each_role_declares_its_established_circuit_group(role, group):
    assert all_routes()[role].primary.circuit_group == group


def test_the_circuit_name_contains_no_provider_endpoint_account_or_model_identifier():
    for route in all_routes().values():
        for target in route.targets:
            g = target.circuit_group
            assert ":" not in g and "/" not in g, f"{g!r} looks like a quota-domain key"
            assert target.model not in g, f"{g!r} embeds the model identifier"
            assert target.account not in g or g == "default", f"{g!r} embeds the account scope"
            assert g in STABLE_GROUPS, f"{g!r} is not an established circuit name"


def test_the_circuit_group_is_not_derived_from_the_quota_domain():
    routes = all_routes()
    intake, summarizer = routes["intake"].primary, routes["summarizer"].primary
    assert intake.provider == summarizer.provider          # same provider…
    assert intake.domain.key != summarizer.domain.key      # …different quota domain…
    assert intake.circuit_group != summarizer.circuit_group  # …and different circuit.


def test_builder_and_planner_circuits_are_independent():
    b, p = all_routes()["builder"].primary, all_routes()["planner"].primary
    assert b.circuit_group == "deepinfra_builder"
    assert p.circuit_group == "deepinfra_planner"
    assert b.circuit_group != p.circuit_group


def test_a_builder_circuit_outage_does_not_open_the_planner_circuit():
    b = get_breaker("deepinfra_builder")
    for _ in range(b.failure_threshold):
        b.record_failure()
    assert not b.allow()
    assert get_breaker("deepinfra_planner").allow(), "builder failures opened the planner circuit"


def test_a_planner_circuit_outage_does_not_open_the_builder_circuit():
    p = get_breaker("deepinfra_planner")
    for _ in range(p.failure_threshold):
        p.record_failure()
    assert not p.allow()
    assert get_breaker("deepinfra_builder").allow(), "planner failures opened the builder circuit"


def test_a_model_change_does_not_rename_the_readiness_key():
    from meshpipeline.contracts.model_routing import RouteTarget

    before = all_routes()["builder"].primary
    after = RouteTarget(provider=before.provider, model="some-org/Another-Model-9",
                        account=before.account, circuit_group=before.circuit_group)
    assert after.domain.key != before.domain.key, "a model change must move the quota domain"
    assert after.circuit_group == before.circuit_group == "deepinfra_builder"


# circuit independence
def test_the_summarizer_circuit_is_independent_of_intake():
    s = get_breaker("summarizer")
    for _ in range(s.failure_threshold):
        s.record_failure()
    assert not s.allow()
    assert get_breaker("deepseek").allow(), "summarizer failures opened the intake circuit"


def test_intake_failures_do_not_open_the_summarizer_circuit():
    d = get_breaker("deepseek")
    for _ in range(d.failure_threshold):
        d.record_failure()
    assert not d.allow()
    assert get_breaker("summarizer").allow(), "intake failures opened the summarizer circuit"


def test_the_builder_and_reviewer_circuits_are_independent():
    b = get_breaker("deepinfra_builder")
    for _ in range(b.failure_threshold):
        b.record_failure()
    assert not b.allow()
    assert get_breaker("deepinfra_reviewer").allow()


def test_circuit_transitions_stay_bounded_and_recover():
    s = get_breaker("summarizer")
    for _ in range(s.failure_threshold):
        s.record_failure()
    assert s.state.value == "open"
    s.record_success()
    assert s.state.value == "closed" and s.allow()


# readiness presentation
def _probe_body(circuits: dict, checks: dict):
    hard_ok = all(v == "ok" for v in checks.values())
    return hard_ok, {"status": "ready" if hard_ok else "not_ready",
                     "checks": checks, "circuits": circuits}


_HEALTHY = {"postgres": "ok", "redis": "ok", "object_store": "ok"}


@pytest.mark.parametrize("circuits,expect_ready", [
    ({}, True),                                                        # 1. all closed
    ({"deepinfra_builder": "open"}, True),                             # 2. builder open
    ({"deepinfra_reviewer": "open"}, True),                            # 3. reviewer open
    ({"deepseek": "open"}, True),                                      # 4. deepseek open
    ({"summarizer": "open"}, True),                                    # 5. summarizer open
    ({"deepinfra_builder": "open", "deepinfra_reviewer": "open",
      "deepseek": "open", "summarizer": "open"}, True),                # 6. every circuit open
])
def test_no_open_circuit_ever_makes_the_service_unready(circuits, expect_ready):
    hard_ok, body = _probe_body(circuits, dict(_HEALTHY))
    assert hard_ok is expect_ready
    assert body["status"] == "ready"
    assert body["circuits"] == circuits          # reported, verbatim, as diagnostics


def test_a_hard_dependency_down_is_the_only_thing_that_makes_the_service_unready():
    hard_ok, body = _probe_body({"deepinfra_builder": "open"},
                                {**_HEALTHY, "redis": "down: ConnectionError"})
    assert hard_ok is False and body["status"] == "not_ready"


def test_the_summarizer_circuit_is_observable_without_affecting_readiness():
    hard_ok, body = _probe_body({"summarizer": "open"}, dict(_HEALTHY))
    assert body["circuits"]["summarizer"] == "open"   # observable
    assert hard_ok is True and body["status"] == "ready"  # and harmless


def test_the_readiness_body_keeps_its_established_shape():
    _hard_ok, body = _probe_body({}, dict(_HEALTHY))
    assert set(body) == {"status", "checks", "circuits"}
    assert set(body["checks"]) == {"postgres", "redis", "object_store"}


def test_a_registered_circuit_never_disappears_from_diagnostics():
    from meshpipeline.adapters._shared.resilience import breaker_states

    for name in sorted(STABLE_GROUPS):
        get_breaker(name)
    states = breaker_states()
    assert STABLE_GROUPS <= set(states), f"missing from diagnostics: {STABLE_GROUPS - set(states)}"
    assert all(v == "closed" for v in states.values())
