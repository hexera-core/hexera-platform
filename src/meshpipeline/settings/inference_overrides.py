# Responsibility: Parse the two structured inference-override settings into validated tables.
# Boundaries: parsing and refusal only; it reads one declared entry each and decides no policy.

# These replace two environment NAMESPACES whose variable names were built from provider and model
# data. A name assembled from data cannot be declared, cannot be generated into a template, and
# cannot be spell-checked - so an operator's typo looked exactly like a setting the product does
# not support. One declared name each, with a schema that refuses everything it does not support.
from __future__ import annotations

import math

from meshpipeline.settings import inventory
from meshpipeline.settings.env import ConfigurationError, declared_value


def _supported_providers() -> frozenset[str]:
    # THE provider registry, not a copy of it: a provider this product cannot credential is not a
    # provider an override may introduce.
    from meshpipeline.settings.providers import LLM_PROVIDER_KEY_ENV
    return frozenset(LLM_PROVIDER_KEY_ENV)


def _entries(raw: str, setting: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in (c.strip() for c in raw.split(";")):
        if not chunk:
            continue
        if "=" not in chunk:
            raise ConfigurationError(
                f"{setting}: {chunk!r} is not '<key>=<value>'. Entries are separated by ';'.")
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key in seen:
            raise ConfigurationError(
                f"{setting}: {key!r} appears twice; one key may have only one value.")
        seen.add(key)
        out.append((key, value))
    return out


def price_overrides() -> dict[str, tuple[float, float, float]]:
    setting = "MODEL_PRICE_OVERRIDES"
    raw = declared_value(inventory.get("MODEL_PRICE_OVERRIDES")).strip()
    table: dict[str, tuple[float, float, float]] = {}
    for key, value in _entries(raw, setting):
        provider, sep, model = key.partition(":")
        if not sep or not model:
            raise ConfigurationError(
                f"{setting}: {key!r} is not 'provider:model'.")
        if provider not in _supported_providers():
            raise ConfigurationError(
                f"{setting}: {provider!r} is not a provider this product supports "
                f"({sorted(_supported_providers())}). An override cannot introduce one.")
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 3:
            raise ConfigurationError(
                f"{setting}: {key!r} needs exactly three prices per 1M tokens "
                f"(in,out,cached), got {value!r}.")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ConfigurationError(
                f"{setting}: {key!r} has a non-numeric price in {value!r}.") from None
        for n in nums:
            if not math.isfinite(n):
                raise ConfigurationError(f"{setting}: {key!r} has a non-finite price.")
            if n < 0:
                raise ConfigurationError(f"{setting}: {key!r} has a negative price.")
        table[key] = (nums[0], nums[1], nums[2])
    return table


def domain_budgets() -> dict[str, int]:
    setting = "MODEL_DOMAIN_BUDGETS"
    raw = declared_value(inventory.get("MODEL_DOMAIN_BUDGETS")).strip()
    table: dict[str, int] = {}
    for key, value in _entries(raw, setting):
        parts = key.split(":")
        if len(parts) != 3 or not all(parts):
            raise ConfigurationError(
                f"{setting}: {key!r} is not 'provider:account:model'.")
        if parts[0] not in _supported_providers():
            raise ConfigurationError(
                f"{setting}: {parts[0]!r} is not a provider this product supports "
                f"({sorted(_supported_providers())}).")
        try:
            n = int(value)
        except ValueError:
            raise ConfigurationError(
                f"{setting}: {key!r} budget {value!r} is not a whole number.") from None
        if n <= 0:
            raise ConfigurationError(
                f"{setting}: {key!r} budget must be positive, got {n}.")
        table[key] = n
    return table
