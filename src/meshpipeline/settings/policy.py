# Responsibility: Declare what the product is permitted to do in this deployment.
# Boundaries: policy rather than provider wiring: data collection, trace mode, retention and production enforcement.
from __future__ import annotations

import meshpipeline.settings.env as env
from meshpipeline.settings.env import (
    ConfigurationError,
    load_prompt,
    optional_env,
)

# Compute-feasibility cap on mesh size - the SINGLE source of truth (the only cell-count gate,
# enforced by each engine's declared gate; the builder prompt aims under it). Cross-product
# policy: it is the Cloud Run feasibility limit (8 vCPU / 32 GiB / ~50 min), not a per-engine knob.
CELL_HARD_LIMIT: int = int(optional_env("CELL_HARD_LIMIT", "8000000"))

# deployment identity + request auth
ENV: str = optional_env("ENV", "dev").lower()
MESH_API_KEY: str = optional_env("MESH_API_KEY", "")
# When set, X-User-Id must carry a valid HMAC X-User-Sig; empty = dev self-asserted.
USER_TOKEN_SECRET: str = optional_env("USER_TOKEN_SECRET", "")
# FAIL CLOSED: with both secrets unset, any caller could act as any user (IDOR). Acceptable
# ONLY for genuinely-local single-tenant dev - otherwise the server refuses to start.
_AUTH_OPTIONAL_ENVS = frozenset({"dev", "development", "local", "test", "testing", "ci"})


# THE environment classification, and the only one. Everything that must behave differently in a
# hosted deployment asks this question instead of comparing ENV to a string: `ENV == "production"`
# reads like a hardening check but silently exempts every other hosted name an operator might
# choose - staging, prod, hosted - which is precisely how those deployments came to start with
# wildcard CORS and no database credential while production refused.
#
# It is deliberately phrased as "does this environment REQUIRE hardening", not "is this
# production": the answer is true for everything except the names below, so an unrecognised value
# fails closed rather than being quietly treated as development.
#
# Normalization matches what this module already applied to ENV above - `.lower()`, and nothing
# else. Whitespace is NOT stripped, so ENV=" dev " is not the dev environment and is hardened;
# that is the existing behaviour and the safe direction, so it is preserved exactly.
def requires_hardened_runtime(environment: str) -> bool:
    return environment.lower() not in _AUTH_OPTIONAL_ENVS


if requires_hardened_runtime(ENV):
    _missing = [n for n, v in (("MESH_API_KEY", MESH_API_KEY),
                               ("USER_TOKEN_SECRET", USER_TOKEN_SECRET)) if not v]
    if _missing:
        raise ConfigurationError(
            f"ENV={ENV!r} is not a recognised dev environment, but "
            f"{' and '.join(_missing)} {'is' if len(_missing) == 1 else 'are'} unset. "
            "The API would accept self-asserted identities. Set both secrets for a multi-tenant "
            "deployment, or use ENV=dev for genuinely local single-tenant use.")
CORS_ORIGINS: list = optional_env("CORS_ORIGINS", "*").split(",")

# job quotas + retention
MAX_JOBS_PER_OWNER: int         = int(optional_env("MAX_JOBS_PER_OWNER", "5"))
MAX_CONCURRENT_JOBS: int        = int(optional_env("MAX_CONCURRENT_JOBS", "20"))
# WHAT A NEW ACCOUNT IS GIVEN, in whole credits, and the only place the number lives. 0 disables
# the grant without a code change. What a credit is WORTH is deliberately not decided here or
# anywhere else yet - see the design's decision 8.
SIGNUP_GRANT_CREDITS: int = int(optional_env("SIGNUP_GRANT_CREDITS", "100"))
# WHETHER an unrecognised Identity Platform account may provision itself one. This is the REAL
# gate and it lives on the API, not the console: anyone can create an Identity Platform account
# directly against the project's public web API key, so a console that merely hides the sign-up
# form is not a gate at all.
CONSOLE_SIGNUP_ENABLED: bool = optional_env("CONSOLE_SIGNUP_ENABLED", "true").lower() == "true"
#: How long a retryable reconciliation failure waits before it may be claimed again. The
#: sweep runs far more often than a transient object-store fault clears, so without a delay
#: the five attempts RECONCILE_MAX_RETRIES allows are spent in five consecutive sweeps.
RECONCILE_RETRY_DELAY_SECONDS: int = int(optional_env("RECONCILE_RETRY_DELAY_SECONDS", "300"))
FAILED_JOB_RETENTION_HOURS: int = int(optional_env("FAILED_JOB_RETENTION_HOURS", "24"))
UPLOAD_RETENTION_DAYS: int      = int(optional_env("UPLOAD_RETENTION_DAYS", "30"))
STALLED_JOB_TIMEOUT_HOURS: int  = int(optional_env("STALLED_JOB_TIMEOUT_HOURS", "4"))

# durable graph checkpointing is MANDATORY outside genuinely-local dev/test. A silent
# fallback from AsyncPostgresSaver to MemorySaver would make a mid-run restart re-run from scratch
# (duplicate native/model work), lose in-flight state, or drop durable worker fencing - with NO
# operator signal. Detected from the same robust env set as auth (not a fragile ENV=="production"
# string): any non-dev environment REQUIRES a durable checkpointer, and the pipeline refuses to run
# without one (failing the job truthfully before expensive work).
# CRUCIALLY, a hosted/multi-tenant deployment CANNOT re-enable in-process checkpoints by an ordinary
# env value. Outside the dev set this flag is FORCED true, and an explicit REQUIRE_DURABLE_CHECKPOINTER
# =false is treated as a fatal misconfiguration (refuse to start) rather than silently honoured - so
# there is no environment-variable path to MemorySaver in production. Only genuinely-local dev/test
# (ENV in the dev set) may opt into MemorySaver, and only by leaving this at its dev default (false).
_require_ck_raw = optional_env("REQUIRE_DURABLE_CHECKPOINTER", "").strip().lower()
if requires_hardened_runtime(ENV):
    if _require_ck_raw in ("false", "0", "no", "off"):
        raise ConfigurationError(
            f"ENV={ENV!r} is a non-dev (hosted/multi-tenant) environment, but "
            "REQUIRE_DURABLE_CHECKPOINTER is set to a false-y value. Durable graph checkpointing "
            "cannot be disabled outside genuinely-local dev/test - in-process MemorySaver would make "
            "a mid-run restart re-run from scratch and would drop durable worker fencing. Remove the "
            "override (it is forced on here) or use ENV=dev for local single-tenant use.")
    REQUIRE_DURABLE_CHECKPOINTER: bool = True
else:
    REQUIRE_DURABLE_CHECKPOINTER = _require_ck_raw in ("true", "1", "yes", "on")

# LAYER-COVERAGE CAVEAT DELIVERY (reviewer-fairness follow-on, adversarially reviewed
# 2026-08-26): a solver-ready mesh whose ONLY review miss is prism-layer coverage may deliver
# with a stated caveat instead of a terminal failure. The floor is AREAL coverage percent
# (the snappy 'Added N out of M cells' headline) keyed by flow topology - an ABSENT key means
# the caveat path is CLOSED for that topology (only external ships in v1; internal flow is
# more layer-critical and needs its own reviewed floor). The per-patch minimum is achieved
# THICKNESS percent - one effectively-bare wall patch keeps the failure a failure. These
# constants must never leak into prompt-rendered text (the criteria row threshold stays 0.0).
LAYER_CAVEAT_FLOOR_PCT: dict = {
    "external": float(optional_env("LAYER_CAVEAT_FLOOR_PCT_EXTERNAL", "40")),
}
LAYER_CAVEAT_PATCH_MIN_THICKNESS_PCT: float = float(
    optional_env("LAYER_CAVEAT_PATCH_MIN_THICKNESS_PCT", "10"))

# PRODUCT MODES - built once, by the one authority that also refuses an unsupported combination.
# Retention (collection) and publication (disclosure) are separate questions and are not allowed
# to imply one another; settings/modes.py is where both are declared and validated together.
from meshpipeline.settings.modes import (  # noqa: E402
    ProductModes,
    load_product_modes,
)

MODES: ProductModes = load_product_modes()

# OBSERVABILITY - how this process reports on itself. Its defaults live in the catalogue and are
# read here once, so no consumer keeps a second copy.
from meshpipeline.settings.observability import (  # noqa: E402
    ObservabilitySettings,
    load_observability,
)

OBSERVABILITY: ObservabilitySettings = load_observability()

# feature flags / safety switches
SOLVABILITY_GATE_ENABLED: bool = optional_env("SOLVABILITY_GATE_ENABLED", "true").lower() == "true"
DOMAIN_EXTENT_GATE_ENABLED: bool = optional_env("DOMAIN_EXTENT_GATE_ENABLED", "true").lower() == "true"
MESH_SCRIPT_SCAN_ENABLED: bool = optional_env("MESH_SCRIPT_SCAN_ENABLED", "true").lower() == "true"
RUN_PYTHON_REQUIRE_SANDBOX: bool = optional_env("RUN_PYTHON_REQUIRE_SANDBOX", "true").lower() == "true"

# viewer / dispute policy (served to the frontend via GET /api/v1/client-config)
VIEWER_GRID_PX: float          = float(optional_env("VIEWER_GRID_PX", "2.5"))
VIEWER_FINE_FILL: float        = float(optional_env("VIEWER_FINE_FILL", "8"))
VIEWER_FRAME_FRAC: float       = float(optional_env("VIEWER_FRAME_FRAC", "0.85"))
VIEWER_FLAG_SPAN_FACTOR: float = float(optional_env("VIEWER_FLAG_SPAN_FACTOR", "2.5"))
DISPUTE_MAX_FLAGS: int         = int(optional_env("DISPUTE_MAX_FLAGS", "20"))

# shared prompt registry (prompts/<agent>/<role>.txt)
# All THREE agent roles load their general contract from here. The Builder used to have none: its
# shared behaviour was duplicated across five engine-local Python constants, so the role could not
# be read or reviewed anywhere. The engine bundles still own their differentiated instructions -
# this file holds only what is true of the Builder whatever engine it drives.
REQUIRED_PROMPTS: list[tuple[str, str]] = [
    ("reviewer_system",        "reviewer/system.txt"),
    ("intake_system",          "intake/system.txt"),
    ("builder_system",         "builder/system.txt"),
]


class Prompts:
    reviewer_system: str
    intake_system: str
    builder_system: str


def _load_all_prompts() -> Prompts:
    # module-qualified so a test that patches settings.env.PROMPTS_DIR (or PROMPTS_DIR here)
    # before re-loading is honoured rather than reading a value bound at import.
    prompts_dir = env.PROMPTS_DIR
    if not prompts_dir.exists():
        raise ConfigurationError(
            f"Prompts directory not found: {prompts_dir}\nCreate this directory and add all "
            f"required prompt files: {[fname for _, fname in REQUIRED_PROMPTS]}")
    p = Prompts()
    for attr, fname in REQUIRED_PROMPTS:
        setattr(p, attr, load_prompt(prompts_dir / fname))
    return p


prompts: Prompts = _load_all_prompts()
