# Responsibility: Declare which model each capability may use, and what credentials that requires.
# Boundaries: a table plus the questions the API asks of it (which providers are configured, what is missing).
from __future__ import annotations

import logging

from meshpipeline.contracts.model_routing import ModelRoute
from meshpipeline.settings.env import optional_env

logger = logging.getLogger(__name__)


def _collect() -> dict[str, ModelRoute]:
    # Imported lazily inside the function: this module is imported by the router, and the role
    # settings pull in the agents' own config trees.
    import meshpipeline.agent_tools.shared.settings as scfg
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.agents.intake.settings as icfg
    import meshpipeline.agents.reviewer.settings as rcfg
    import meshpipeline.engines.snappy.settings as pcfg

    routes = (
        bcfg.BUILDER_ROUTE,
        pcfg.PLANNER_ROUTE,
        rcfg.VISUAL_REVIEWER_ROUTE,
        icfg.INTAKE_ROUTE,
        scfg.SUMMARIZER_ROUTE,
    )
    return {r.role: r for r in routes}


_ROUTES: dict[str, ModelRoute] | None = None


def all_routes() -> dict[str, ModelRoute]:
    global _ROUTES
    if _ROUTES is None:
        _ROUTES = _collect()
    return _ROUTES



def domain_budget(domain_key: str, routes: dict[str, ModelRoute] | None = None) -> int:
    # No caller-supplied DEFAULT and no constructed variable name. `routes` is not a fallback
    # value - it is which routes to search, so a caller holding a route the registry does not
    # (the routing layer, mid-call) can still have its own declared budget counted. The answer is,
    # in order: an explicit entry in the one declared MODEL_DOMAIN_BUDGETS setting, the largest
    # budget the routes themselves declare for this domain, then the declared fallback.
    from meshpipeline.settings.inference_overrides import domain_budgets
    override = domain_budgets().get(domain_key)
    if override is not None:
        return override
    declared = [r.concurrency_budget for r in (routes or all_routes()).values()
                if r.primary.domain.key == domain_key
                or (r.standby is not None and r.standby.domain.key == domain_key)]
    if declared:
        return max(declared)
    return int(optional_env("MODEL_DEFAULT_CONCURRENCY_BUDGET", "8"))


def domain_map() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role, route in sorted(all_routes().items()):
        out.setdefault(route.primary.domain.key, []).append(role)
        if route.standby is not None:
            out.setdefault(route.standby.domain.key, []).append(f"{role}(standby)")
    return out


def summary() -> str:
    lines = ["  Model routes (role -> provider/model, budget is per QUOTA DOMAIN):"]
    for role, route in sorted(all_routes().items()):
        sb = f" standby={route.standby.label}" if route.standby else ""
        lines.append(f"    {role:<16} {route.primary.label}{sb}")
    lines.append("  Quota domains (roles sharing a domain share one ceiling + circuit):")
    for key, roles in sorted(domain_map().items()):
        lines.append(f"    {key:<50} budget={domain_budget(key):<4} <- {', '.join(roles)}")
    return "\n".join(lines)


def enabled_llm_providers() -> set[str]:
    provs: set[str] = set()
    for route in all_routes().values():
        provs.add(route.primary.provider)
        if route.standby is not None:
            provs.add(route.standby.provider)
    return provs


def missing_provider_credentials() -> list[str]:
    import meshpipeline.settings.providers as p

    key_value = {"DEEPINFRA_API_KEY": p.DEEPINFRA_API_KEY, "DEEPSEEK_API_KEY": p.DEEPSEEK_API_KEY}
    missing: list[str] = []
    for prov in sorted(enabled_llm_providers()):
        env = p.LLM_PROVIDER_KEY_ENV.get(prov)
        if env and not key_value.get(env, ""):
            missing.append(f"{prov} inference (set {env})")
    if p.WEB_SEARCH_ENABLED and p.WEB_SEARCH_PROVIDER == "tavily" and not p.TAVILY_API_KEY:
        missing.append("tavily web search (set TAVILY_API_KEY)")
    return missing
