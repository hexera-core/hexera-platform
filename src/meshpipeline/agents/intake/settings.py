# Responsibility: Declare intake's own tunables.
# Boundaries: declaration only.
from __future__ import annotations

from meshpipeline.contracts.model_routing import Capability
from meshpipeline.settings.env import ConfigurationError, optional_env
from meshpipeline.settings.routes import route_from_catalogue

# The number of PROVIDER ROUNDS one intake conversation turn may take. It governs MODEL calls,
# not tool calls: intake may issue several tool calls inside one round, and none of them is
# bounded by this. Previously a private `_MAX_INNER_LLM_CALLS` inside the node - enforced but
# unnamed, so an operator could neither read nor tune it, and the passive accounting reported a
# limit no configuration mentioned. The value is unchanged at 20.
# Deliberately the ONLY intake loop setting: no tool-call ceiling, no category ceiling, no
# deadline, no warning or no-progress threshold. Those belong to the canonical loop, and intake
# is not migrated onto it - inventing them here would record limits nothing enforces.
INTAKE_MAX_ROUNDS: int = int(optional_env("INTAKE_MAX_ROUNDS", "20"))
if INTAKE_MAX_ROUNDS <= 0:
    raise ConfigurationError(
        f"INTAKE_MAX_ROUNDS must be a positive number of provider rounds, got "
        f"{INTAKE_MAX_ROUNDS}")

INTAKE_TEMPERATURE: float = float(optional_env("INTAKE_TEMPERATURE", "1.0"))
INTAKE_MAX_TOKENS: int    = int(optional_env("INTAKE_MAX_TOKENS",    "2048"))
INTAKE_MIN_P: float       = float(optional_env("INTAKE_MIN_P",       "0.05"))
# run the LLM intake greeting synchronously inside the upload request. False → a static
# greeting returns immediately and intake runs on the first chat message.
INTAKE_GREETING_ON_UPLOAD: bool = optional_env("INTAKE_GREETING_ON_UPLOAD", "true").lower() == "true"

# the intake ROUTE
# The MODEL IDENTITY of these roles used to live in settings/providers.py as DEEPSEEK_MODEL - a
# provider-named setting the APPLICATION layer read directly to record capture provenance. That
# made "which model does intake use?" a question about a vendor rather than about the role, and
# it is why removing a provider reached into application code. The model belongs to the role now.
# INTAKE is safety-critical: it preserves exact user identifiers, submits a validated tool
# schema, and must never invent a value or change the user's chosen engine. It is deliberately
# primary-only - automatic substitution here risks correctness, not merely latency. Its calls
# are interactive, so the timeout is short.
INTAKE_ROUTE = route_from_catalogue(
    "intake",
    circuit_group="deepseek",
    capabilities={Capability.TOOLS, Capability.REASONING_CONTROL},
)
