# Responsibility: Declare the shared agent-tool tunables.
# Boundaries: declaration only.
from __future__ import annotations

from meshpipeline.settings.env import optional_env
from meshpipeline.settings.routes import route_from_catalogue

SUMMARIZER_TEMPERATURE: float = float(optional_env("SEARCH_SUMMARIZER_TEMPERATURE", "0.2"))
SUMMARIZER_MAX_TOKENS: int    = int(optional_env("SEARCH_SUMMARIZER_MAX_TOKENS", "700"))

# the summarizer ROUTE
# It distils raw web-search snippets into a short answer: bounded output, low temperature, no
# tools, no structured output, no reasoning, retry-safe. Technically the most substitutable role
# in the product - and, at ~1-3 calls per workflow, the one where substitution buys the least.
# It is primary-only for that reason, not because it would be hard.
# It is the ONE role whose failure is not fatal: a search that cannot be distilled degrades the
# builder's context, it does not fail the mesh. Hence the short timeout and small budget - this
# role must never be the reason a workflow queues.
SUMMARIZER_ROUTE = route_from_catalogue(
    "summarizer",
    # Its OWN circuit: a search that cannot be distilled must never open intake's or the
    # summarizer's circuit, and vice versa. It is not a hard dependency.
    circuit_group="summarizer",
    capabilities=frozenset(),
    # 60s is the TOTAL route budget (admission + both attempts + backoff), not per attempt: a
    # failing distillation must never keep a builder round waiting ~120s. The per-attempt
    # timeout is the same number only because the total is what actually binds.
    total_deadline_s=60.0,
)
