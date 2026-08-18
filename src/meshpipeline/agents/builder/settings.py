# Responsibility: Declare the builder's own tunables.
# Boundaries: declaration only.
from __future__ import annotations

from meshpipeline.contracts.model_routing import Capability
from meshpipeline.settings.env import ConfigurationError, optional_env
from meshpipeline.settings.routes import route_from_catalogue

BUILDER_MODEL: str = optional_env("BUILDER_MODEL", "zai-org/GLM-5.2")

# SAMPLING POLICY: a CONSERVATIVE default for a structured tool-use agent. The Builder drives a
# deterministic tool workflow (configure → run_mesh → submit) against typed engine palettes and
# deterministic gates; high-entropy sampling bought nothing but retry churn and non-reproducible
# authoring. 0.3 is a provisional reliability default - NOT a claim of optimality, to be measured in
# a later real-model evaluation. Override with BUILDER_TEMPERATURE; the value is range-validated.
BUILDER_TEMPERATURE: float = float(optional_env("BUILDER_TEMPERATURE", "0.3"))
if not (0.0 <= BUILDER_TEMPERATURE <= 2.0):
    raise ConfigurationError(f"BUILDER_TEMPERATURE must be in 0.0..2.0, got {BUILDER_TEMPERATURE}")
BUILDER_TOP_P: float       = float(optional_env("BUILDER_TOP_P",       "0.95"))
if not (0.0 < BUILDER_TOP_P <= 1.0):
    raise ConfigurationError(f"BUILDER_TOP_P must be in (0.0, 1.0], got {BUILDER_TOP_P}")
BUILDER_MAX_TOKENS: int    = int(optional_env("BUILDER_MAX_TOKENS", "16384"))
# min_p - a probability FLOOR scaled to the model's confidence. Applied to the temp-driven agents.
BUILDER_MIN_P: float       = float(optional_env("BUILDER_MIN_P",       "0.05"))

BUILDER_MAX_ROUNDS: int       = int(optional_env("BUILDER_MAX_ROUNDS",       "60"))
BUILDER_RETRY_MAX_ROUNDS: int = int(optional_env("BUILDER_RETRY_MAX_ROUNDS", "45"))
BUILDER_NULL_CHOICES_SLEEP: int = int(optional_env("BUILDER_NULL_CHOICES_SLEEP", "15"))
# PER-ATTEMPT loop budget.
BUILDER_LOOP_TIMEOUT: int     = int(optional_env("BUILDER_LOOP_TIMEOUT",     "3600"))
# AGGREGATE budget: the wall-clock ceiling across EVERY Builder attempt + rebuild for ONE
# logical pipeline run. Set once on the first attempt (into pipeline state) and never reset by a
# retry, so the sum of attempts cannot exceed it; every model / run_mesh / run_python call inside an
# attempt is capped at the REMAINING budget. Finite, documented default (< the naive 5×per-attempt).
BUILDER_TOTAL_TIMEOUT_SECONDS: int = int(optional_env("BUILDER_TOTAL_TIMEOUT_SECONDS", "10800"))
if BUILDER_TOTAL_TIMEOUT_SECONDS <= 0:
    raise ConfigurationError(
        f"BUILDER_TOTAL_TIMEOUT_SECONDS must be a positive number of seconds, got {BUILDER_TOTAL_TIMEOUT_SECONDS}")
# Submit discipline: after this many exit-0 mesh runs without submit_mesh, the builder loop
# auto-submits (the reviewer judges quality downstream).
BUILDER_AUTO_SUBMIT_AFTER: int = int(optional_env("BUILDER_AUTO_SUBMIT_AFTER", "2"))

MAX_BUILDER_RETRIES: int = int(optional_env("MAX_BUILDER_RETRIES", "3"))
# The TRUE ceiling on build attempts shown to the user: 1 initial + MAX_BUILDER_RETRIES normal
# + 1 reviewer-feedback bonus (route_after_reviewer). Display/logging only.
BUILDER_MAX_TOTAL_ATTEMPTS: int = MAX_BUILDER_RETRIES + 2

# the builder's model ROUTE (role-owned; provider is data, not an import)
# The builder is the mesh-critical path and the dominant consumer of inference: up to
# BUILDER_MAX_ROUNDS calls per attempt, BUILDER_MAX_TOTAL_ATTEMPTS attempts, each up to
# BUILDER_MAX_TOKENS out. Its timeout is per ATTEMPT, well under BUILDER_LOOP_TIMEOUT so the
# loop's own clock, not the provider's, decides when an attempt is over.
# No standby by default - the simple OSS profile runs primary-only, and a hosted standby is an
# explicit, separately-validated decision (Gate 2B/2C), never a default.
BUILDER_ROUTE = route_from_catalogue(
    "builder",
    # The established operator-facing circuit name. It has meant "the builder's model is sick"
    # across model changes and must keep meaning that; it is NOT derived from GLM-5.2.
    circuit_group="deepinfra_builder",
    capabilities={Capability.TOOLS, Capability.STREAMING},
    rate_limit_backoff_base_s=60.0,
    rate_limit_backoff_max_s=300.0,
)
