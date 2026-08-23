# Responsibility: Declare the reviewer's own tunables, including its aggregate per-invocation budget.
# Boundaries: declaration only; the budget is enforced by the canonical loop and capped by the run's remaining time.
from __future__ import annotations

from meshpipeline.contracts.model_routing import Capability
from meshpipeline.settings.env import ConfigurationError, optional_env
from meshpipeline.settings.routes import route_from_catalogue

REVIEWER_MODEL: str = optional_env("REVIEWER_MODEL", "Qwen/Qwen3-VL-235B-A22B-Thinking")

REVIEWER_TEMPERATURE: float      = float(optional_env("REVIEWER_TEMPERATURE",      "0.7"))
REVIEWER_TOP_P: float            = float(optional_env("REVIEWER_TOP_P",            "0.8"))
REVIEWER_TOP_K: int              = int(optional_env("REVIEWER_TOP_K",              "20"))
REVIEWER_PRESENCE_PENALTY: float = float(optional_env("REVIEWER_PRESENCE_PENALTY", "1.5"))
REVIEWER_MAX_TOKENS: int         = int(optional_env("REVIEWER_MAX_TOKENS", "16384"))
# The number of PROVIDER ROUNDS one review may take. The old name, REVIEWER_MAX_TOOL_CALLS,
# described a counter it never governed - tool calls were counted and compared to nothing.
# It is DELETED, not aliased: reusing the name with a new meaning would silently change an
# operator's existing value from a round budget into a tool-call budget. runtime.startup
# rejects the removed name so a stale .env fails loudly instead of being ignored.
REVIEWER_MAX_ROUNDS: int         = int(optional_env("REVIEWER_MAX_ROUNDS",         "60"))

# AGGREGATE budget: the wall-clock ceiling for ONE logical review invocation - a single
# node_reviewer call reviewing ONE native-build attempt. It is SEPARATE from the provider per-call
# timeout, the provider retry count and REVIEWER_MAX_ROUNDS, and it bounds the WHOLE interactive
# session: a monotonic deadline is set once at the start of the loop and never reset by a provider
# retry, a viewer-tool round, or a malformed-output re-prompt; every provider call and viewer op is
# capped at the REMAINING budget, no new round starts after exhaustion, and exhaustion is a truthful
# non-verdict (never a retained PASS). This is per-review-invocation, so each rebuild attempt's review
# gets its own fresh budget - the number of reviews is already bounded by the pipeline retry_count.
# A finite documented default (30 min): generous for a real interactive review yet far below the
# pathological REVIEWER_MAX_ROUNDS × provider-timeout × retries this replaces.
REVIEWER_TOTAL_TIMEOUT_SECONDS: int = int(optional_env("REVIEWER_TOTAL_TIMEOUT_SECONDS", "1800"))
if REVIEWER_TOTAL_TIMEOUT_SECONDS <= 0:
    raise ConfigurationError(
        f"REVIEWER_TOTAL_TIMEOUT_SECONDS must be a positive number of seconds, got "
        f"{REVIEWER_TOTAL_TIMEOUT_SECONDS}")

# the two reviewer ROUTES
# The visual reviewer needs MULTIMODAL: it sends rendered views of the mesh, and its verdict is
# a quality gate. The audit found no equivalent serverless multimodal model at any other
# provider, so it is primary-only and a standby must never be configured casually - a different
# model here changes what "PASS" means.
VISUAL_REVIEWER_ROUTE = route_from_catalogue(
    "visual_reviewer",
    circuit_group="deepinfra_reviewer",
    capabilities={Capability.TOOLS, Capability.STREAMING, Capability.MULTIMODAL},
    rate_limit_backoff_base_s=60.0,
    rate_limit_backoff_max_s=300.0,
)
