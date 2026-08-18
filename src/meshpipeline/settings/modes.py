# Responsibility: Build the one typed product-mode object, and refuse a combination the product does not support.
# Owns: TraceDisclosure, ProductModes, and the acknowledgement rule that guards raw disclosure.
# Boundaries: product modes only - observability, database, worker and per-job settings are other authorities.
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meshpipeline.settings.env import ConfigurationError, bool_env, optional_env


class TraceDisclosure(StrEnum):
    # How much of HOW the system works the live page may carry. Not a retention question.
    SAFE = "safe"     # activity without content: no reasoning text, tool names, arguments or images
    RAW = "raw"       # additionally the provider's reasoning, real tool names, arguments and images


@dataclass(frozen=True)
class ProductModes:
    # Retention: whether a run leaves redacted training data behind.
    data_collection_enabled: bool
    # Publication: what the live page may show. Deliberately independent of the above - a
    # deployment may show a raw trace and keep nothing, or a safe trace and capture everything.
    trace_disclosure: TraceDisclosure


def _disclosure(raw: str) -> TraceDisclosure:
    try:
        return TraceDisclosure(raw.strip().lower())
    except ValueError:
        raise ConfigurationError(
            f"PUBLIC_TRACE_MODE={raw!r} is not a public trace mode. The only values are "
            f"{' and '.join(repr(m.value) for m in TraceDisclosure)}: 'safe' publishes activity "
            "without content, 'raw' additionally publishes provider reasoning, real tool names, "
            "arguments, results and inspection images. There is no 'debug', 'full', 'off' or "
            "'uncensored' mode.") from None


def load_product_modes() -> ProductModes:
    disclosure = _disclosure(optional_env("PUBLIC_TRACE_MODE", TraceDisclosure.RAW.value))
    # Raw takes TWO switches: naming the mode is not enough, so narrowing to 'safe' by changing one
    # variable cannot be undone by a typo in the other. The acknowledgement is consumed here and is
    # not carried on the object - once construction succeeds there is nothing left to re-decide.
    if disclosure is TraceDisclosure.RAW and not bool_env("ALLOW_PUBLIC_RAW_TRACE", "true"):
        raise ConfigurationError(
            "PUBLIC_TRACE_MODE=raw requires ALLOW_PUBLIC_RAW_TRACE=true. Raw mode puts the "
            "agents' reasoning, their real tool names, arguments and results, and the reviewer's "
            "inspection images on the live page for anyone who can open it. Set "
            "ALLOW_PUBLIC_RAW_TRACE=true to confirm, or set PUBLIC_TRACE_MODE=safe to publish "
            "activity without content. (Neither switch affects DATA_COLLECTION_ENABLED.)")
    return ProductModes(
        data_collection_enabled=bool_env("DATA_COLLECTION_ENABLED", "true"),
        trace_disclosure=disclosure,
    )
