# Responsibility: Build the model routes from the declared route catalogue.
# Boundaries: construction from declared values; the routing policy itself is a contract.

# No environment NAME appears here. Every one of a role's twelve settings is an explicit entry in
# settings/inventory.py, and the loader reads that entry's own name - so the supported surface can
# be listed, generated into .env.example and type-checked, which an f-string could never be.
from __future__ import annotations

from meshpipeline.contracts.model_routing import (
    Capability,
    ModelRoute,
    RetryPolicy,
    RouteTarget,
)
from meshpipeline.settings import inventory
from meshpipeline.settings.env import ConfigurationError, declared_value


class UnknownRoute(ConfigurationError):
    pass


def _prefix_for(role: str) -> str:
    for row in inventory.ROUTE_MATRIX:
        if row[0] == role:
            return row[1]
    raise UnknownRoute(
        f"{role!r} is not a declared model route. The routes this product supports are "
        f"{sorted(r[0] for r in inventory.ROUTE_MATRIX)}; a new one is added to ROUTE_MATRIX in "
        "settings/inventory.py, which is what puts its settings in the template and the roster.")


def _value(role: str, suffix: str) -> str:
    # Looks the DECLARED entry up and hands it to the loader. A suffix that is not in the declared
    # set raises rather than silently resolving to a name nobody supports.
    known = {s for s, _, _ in inventory.ROUTE_SUFFIXES}
    if suffix not in known:
        raise UnknownRoute(f"{suffix!r} is not a declared route setting; declared: {sorted(known)}")
    return declared_value(inventory.get(inventory.route_setting_name(_prefix_for(role), suffix)))


def route_from_catalogue(
    role: str,
    *,
    circuit_group: str,
    standby_circuit_group: str = "",
    capabilities: frozenset[Capability] | set[Capability] = frozenset(),
    # PRESERVED behaviour, not a new knob: a 429 means the provider already told us to slow
    # down, so the roles that had a longer rate-limit backoff keep it. None = use the ordinary
    # backoff, which is what intake/summarizer have always done.
    rate_limit_backoff_base_s: float | None = None,
    rate_limit_backoff_max_s: float | None = None,
    total_deadline_s: float | None = None,
) -> ModelRoute:
    # Only NON-configurable declarations are parameters now. Provider, model, account, standby,
    # timeout, attempts, backoff and budgets all come from the catalogue, so a caller can no
    # longer supply a configuration default that competes with it.
    prefix = _prefix_for(role)

    def _v(suffix: str) -> str:
        return _value(role, suffix)

    def _f(suffix: str) -> float:
        raw = _v(suffix)
        try:
            return float(raw)
        except ValueError:
            raise ConfigurationError(
                f"{inventory.route_setting_name(prefix, suffix)}={raw!r} is not a number of "
                "seconds.") from None

    def _i(suffix: str) -> int:
        raw = _v(suffix)
        try:
            return int(raw)
        except ValueError:
            raise ConfigurationError(
                f"{inventory.route_setting_name(prefix, suffix)}={raw!r} is not a whole "
                "number.") from None

    # The circuit group is DECLARED by the role, never read from env alongside the model: it is
    # a stable operator-facing name, and letting a model edit rename it is precisely the failure
    # this separation exists to prevent.
    primary = RouteTarget(
        provider=_v("PROVIDER"),
        model=_v("MODEL"),
        account=_v("ACCOUNT"),
        circuit_group=circuit_group,
    )

    sb_provider = _v("STANDBY_PROVIDER").strip()
    sb_model = _v("STANDBY_MODEL").strip()
    if bool(sb_provider) != bool(sb_model):
        raise ConfigurationError(
            f"{prefix}_STANDBY_PROVIDER and {prefix}_STANDBY_MODEL must be set together "
            f"(got provider={sb_provider!r} model={sb_model!r}). A half-configured standby "
            "would look like protection and provide none.")
    if sb_provider and not standby_circuit_group:
        raise ConfigurationError(
            f"{prefix}: a standby is configured but the role declares no standby circuit group. "
            "A standby sharing the primary's circuit would be opened by the primary's failures "
            "- the outage it exists to survive.")
    standby = (
        RouteTarget(provider=sb_provider, model=sb_model,
                    account=_v("STANDBY_ACCOUNT"),
                    circuit_group=standby_circuit_group)
        if sb_provider else None
    )

    if standby is not None and standby.domain.key == primary.domain.key:
        raise ConfigurationError(
            f"{prefix}: the standby route resolves to the same quota domain as the primary "
            f"({primary.domain.key}). It would share the provider's pool and fail with it, so "
            "it is not a standby.")

    return ModelRoute(
        role=role,
        primary=primary,
        standby=standby,
        required_capabilities=frozenset(capabilities),
        timeout_s=_f("TIMEOUT"),
        retry=RetryPolicy(
            max_attempts=_i("MAX_ATTEMPTS"),
            backoff_base_s=_f("BACKOFF_BASE"),
            backoff_max_s=_f("BACKOFF_MAX"),
            rate_limit_backoff_base_s=rate_limit_backoff_base_s,
            rate_limit_backoff_max_s=rate_limit_backoff_max_s,
        ),
        concurrency_budget=_i("CONCURRENCY_BUDGET"),
        queue_deadline_s=_f("QUEUE_DEADLINE"),
        total_deadline_s=total_deadline_s,
    )
