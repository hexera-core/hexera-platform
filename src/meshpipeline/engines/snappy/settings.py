# Responsibility: Declare snappy's own tunables.
# Boundaries: declaration only.
from __future__ import annotations

from meshpipeline.contracts.model_routing import Capability
from meshpipeline.settings.env import ConfigurationError, bool_env, optional_env
from meshpipeline.settings.routes import route_from_catalogue

MAX_SNAPPY_ATTEMPTS: int = int(optional_env("MAX_SNAPPY_ATTEMPTS", "3"))

# THIN-FEATURE LAYER POLICY - geometry-adaptive local prism layers (engines/snappy/layer_policy.py).
# The classifier measures local thickness/sharpness on the tessellated surface; these knobs decide
# when that measurement changes the authored layer configuration.
SNAPPY_THIN_LAYER_POLICY: bool = bool_env("SNAPPY_THIN_LAYER_POLICY", "true")

# act only when the thin+razor classes together cover at least this fraction of the wetted area -
# below it the measurement is noise-scale and the authored dict stays exactly the historical one
SNAPPY_THIN_AREA_FLOOR: float = float(optional_env("SNAPPY_THIN_AREA_FLOOR", "0.002"))
if not (0.0 <= SNAPPY_THIN_AREA_FLOOR <= 1.0):
    raise ConfigurationError(
        f"SNAPPY_THIN_AREA_FLOOR must be a fraction in 0.0..1.0, got {SNAPPY_THIN_AREA_FLOOR}")

# razor = locally thinner than this many wall cells (opposing prism stacks cannot even castellate)
SNAPPY_RAZOR_CELL_FACTOR: float = float(optional_env("SNAPPY_RAZOR_CELL_FACTOR", "1.0"))
if SNAPPY_RAZOR_CELL_FACTOR <= 0.0:
    raise ConfigurationError(
        f"SNAPPY_RAZOR_CELL_FACTOR must be positive, got {SNAPPY_RAZOR_CELL_FACTOR}")

# thin = locally thinner than this many TWO-SIDED prism stacks (the stacks growing from the two
# opposing surfaces would collide before reaching their requested thickness)
SNAPPY_THIN_STACK_FACTOR: float = float(optional_env("SNAPPY_THIN_STACK_FACTOR", "2.0"))
if SNAPPY_THIN_STACK_FACTOR <= 0.0:
    raise ConfigurationError(
        f"SNAPPY_THIN_STACK_FACTOR must be positive, got {SNAPPY_THIN_STACK_FACTOR}")

# the planner's model identity (its OWN, not the builder's)
PLANNER_MODEL: str = optional_env("PLANNER_MODEL", "zai-org/GLM-5.2")

# the planner's sampling (independent env vars; literal defaults, NOT bcfg.BUILDER_*)
# Defaults equal the builder's conservative structured-tool-use values today, but changing a
# BUILDER_* env can never move these - they are read from PLANNER_* only, and validated.
PLANNER_TEMPERATURE: float = float(optional_env("PLANNER_TEMPERATURE", "0.3"))
if not (0.0 <= PLANNER_TEMPERATURE <= 2.0):
    raise ConfigurationError(f"PLANNER_TEMPERATURE must be in 0.0..2.0, got {PLANNER_TEMPERATURE}")
PLANNER_TOP_P: float = float(optional_env("PLANNER_TOP_P", "0.95"))
if not (0.0 < PLANNER_TOP_P <= 1.0):
    raise ConfigurationError(f"PLANNER_TOP_P must be in (0.0, 1.0], got {PLANNER_TOP_P}")
PLANNER_MIN_P: float = float(optional_env("PLANNER_MIN_P", "0.05"))
if not (0.0 <= PLANNER_MIN_P <= 1.0):
    raise ConfigurationError(f"PLANNER_MIN_P must be in 0.0..1.0, got {PLANNER_MIN_P}")
PLANNER_MAX_TOKENS: int = int(optional_env("PLANNER_MAX_TOKENS", "16384"))
if PLANNER_MAX_TOKENS <= 0:
    raise ConfigurationError(f"PLANNER_MAX_TOKENS must be positive, got {PLANNER_MAX_TOKENS}")

# the planner's aggregate budget (an EXPLICIT contract, not an implicit reliance on the
# builder's deadline). The planner is one deliberate reasoning call; a finite documented ceiling
# bounds it even if the caller ever invoked it outside a builder attempt. The snappy driver still
# caps the effective per-call budget at min(this, builder-remaining, pipeline-remaining).
PLANNER_TOTAL_TIMEOUT_SECONDS: int = int(optional_env("PLANNER_TOTAL_TIMEOUT_SECONDS", "1800"))
if PLANNER_TOTAL_TIMEOUT_SECONDS <= 0:
    raise ConfigurationError(
        f"PLANNER_TOTAL_TIMEOUT_SECONDS must be a positive number of seconds, got "
        f"{PLANNER_TOTAL_TIMEOUT_SECONDS}")

# the planner's model ROUTE (engine-owned; its OWN circuit)
# 1-2 calls per workflow. It has its OWN circuit_group so a builder-model outage does not open the
# planner's circuit and vice versa - they are attributed and failed independently. Re-pointing,
# budgeting or a standby is now a planner-only decision.
PLANNER_ROUTE = route_from_catalogue(
    "planner",
    circuit_group="deepinfra_planner",
    capabilities={Capability.TOOLS, Capability.STREAMING},
    rate_limit_backoff_base_s=60.0,
    rate_limit_backoff_max_s=300.0,
)
