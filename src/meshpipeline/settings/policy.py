# Responsibility: Declare what the product is permitted to do in this deployment.
# Boundaries: policy rather than provider wiring: data collection, trace mode, retention and production enforcement.
from __future__ import annotations

import meshpipeline.settings.env as env
import meshpipeline.settings.package_switches as package_switches
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
#: How long a retryable reconciliation failure waits before it may be claimed again. The
#: sweep runs far more often than a transient object-store fault clears, so without a delay
#: the five attempts RECONCILE_MAX_RETRIES allows are spent in five consecutive sweeps.
RECONCILE_RETRY_DELAY_SECONDS: int = int(optional_env("RECONCILE_RETRY_DELAY_SECONDS", "300"))
FAILED_JOB_RETENTION_HOURS: int = int(optional_env("FAILED_JOB_RETENTION_HOURS", "24"))
UPLOAD_RETENTION_DAYS: int      = int(optional_env("UPLOAD_RETENTION_DAYS", "30"))
STALLED_JOB_TIMEOUT_HOURS: int  = int(optional_env("STALLED_JOB_TIMEOUT_HOURS", "4"))

# THE SURVEYOR. An upload is measured, looked at, put to the customer as questions, planned by the
# geometry agent and handed to the builder. The five switches that used to gate that chain are gone:
# they existed to prove the feature changed nothing while it was off, beside a running product, and
# that job is done. `surveyor-v1-precleanup` is the tree where they still worked, and the commit that
# deleted them carries the last digests they produced. What remains below is configuration: how long
# a step may take, which model reads or plans, and two stages inside the measurement package that are
# its switches rather than ours.
#
# A MISSING MEASUREMENT IS STILL AN ABSENCE, not an error, and that has not changed with the gates:
# every reader treats a row that is not there as "not attempted" and proceeds, because an upload whose
# measurement failed is a conversation that must still work.
#: Under this many mebibytes the measurement runs inside the upload request, so the conversation
#: opens holding the table; at or above it the request returns immediately and a worker measures.
#: The split is a wait, not a capability: 44 corpus files measure at a median of 1.11 s and a p90 of
#: 3.91 s, and the one 12.32 s case is 55,946 faces. A customer will not wait for the tail.
GEOMETRY_MEASUREMENT_SYNC_MAX_MB: float = float(optional_env("GEOMETRY_MEASUREMENT_SYNC_MAX_MB", "4"))
#: How long one measurement may run before it is abandoned, matching the measurement package's own
#: default deadline. A measurement that outlives it is recorded as a failure and the conversation
#: proceeds exactly as it does with no measurement at all.
GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS: int = int(
    optional_env("GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS", "900"))

# LOOKING AT THE FILE. After a file is measured and its row is written, a background task renders the
# part and asks a vision model to describe it in words, and those words are stored in the same row. It
# never runs in the upload request and it never delays an upload: the look is queued behind the
# measurement, has its own deadline, and a failure leaves the row exactly as the measurement left it.
# There is nothing for a look to attach to without a measurement row, and the renders are labelled with
# the openings the measurement found, so the look follows the measurement and never precedes it.
#
# What it costs: one provider call per uploaded file. Measured on six corpus parts with gpt-5.6-luna on
# OpenAI, 2026-09-15: 18.2 to 29.1 seconds of wall clock including the render, 7,308 to 7,350 prompt
# tokens and 1,334 to 2,565 completion tokens. NO DOLLAR FIGURE, deliberately: that model id has no
# confirmed price in adapters/inference_telemetry/pricing.py, and that module's own rule is that an
# unconfirmed price is left out rather than guessed. A second job against the same upload pays nothing
# at all, and not because of a render cache: the look is stored in the measurement row and keyed the
# same way, so the task reads one row and stops, having fetched no bytes and called no provider.
#
# What it never does: produce a number. Every digit is removed from the description before it is stored,
# and the fields the measurement found untrustworthy are kept out of every prompt by the measurement
# package's own trust tiers rather than by anything here.
#: One look's wall clock, render included. Past it the look is abandoned and the row keeps the
#: measurement it already had. A look that failed is recorded as failed: the builder must never read a
#: failed look and a clear passage as the same thing.
GEOMETRY_VISION_TIMEOUT_SECONDS: int = int(optional_env("GEOMETRY_VISION_TIMEOUT_SECONDS", "180"))

# WHICH READER LOOKS. The measurement package's `auto` provider takes ANTHROPIC_API_KEY first, then
# OPENAI_API_KEY, then DEEPINFRA_API_KEY, and this platform's own template carries only the last. So
# an image that said nothing else would read every part with DeepInfra's
# Qwen, which is none of the readers the look was measured with. The reader is named here instead.
# gpt-5.6-luna because the cheap reader was measured to be as good as the dear one once the ruler was
# fixed (13, 13, 12, 13 of 13 against gpt-6-astra's 13 on four draws) at about a twenty-fifth of the
# price. A provider with no key for it is a look that does not happen, never a fall-through to
# another provider.
#
# THE READER HAD NO KEY. This default said `openai` while the template carried only DEEPSEEK_API_KEY and
# DEEPINFRA_API_KEY, so vision/client.py's `_openai` returned None, `reader()` returned None, and every row
# stored the NO_READER sentence. The comment above stated the contradiction and nothing acted on it: a key
# that is WRITTEN DOWN is not a key that is READ.
#
# The default stays `openai`, and the template now carries OPENAI_API_KEY. gpt-5.6-luna on OpenAI is the
# reader the look was actually measured with, at $5.30 a thousand cold jobs. Switching the default to
# DeepInfra because a key for it already exists would have been worse twice over: the model named below is
# an OpenAI model id that DeepInfra does not serve, so every look would 404, and DeepInfra's own reader is
# Qwen3-VL, which no draw of this look has ever been scored on. A provider switch is a MODEL switch too;
# the template says so where an operator will read it.
GEOMETRY_VISION_PROVIDER: str = optional_env("GEOMETRY_VISION_PROVIDER", "openai").strip().lower()
GEOMETRY_VISION_MODEL: str = optional_env("GEOMETRY_VISION_MODEL", "gpt-5.6-luna").strip()

#: The providers the measurement package's vision client accepts (geometry_agent.vision.client.PROVIDERS),
#: minus `auto`. `auto` is deliberately NOT accepted here: it falls through providers by whichever key
#: happens to be set, and a part read by a model nobody chose is not a look anybody measured. `off` is the
#: one way to say this deployment takes no look, and it is a STATEMENT - which is why it is the only value
#: that gets past the refusal below with no key.
GEOMETRY_VISION_PROVIDERS = ("openai", "anthropic", "deepinfra", "deepseek", "off")
if GEOMETRY_VISION_PROVIDER not in GEOMETRY_VISION_PROVIDERS:
    raise ConfigurationError(
        f"GEOMETRY_VISION_PROVIDER={GEOMETRY_VISION_PROVIDER!r} is not a reader this deployment can use - "
        f"one of {GEOMETRY_VISION_PROVIDERS}. The package would raise on it at the first look and the row "
        f"would record a failure per upload; refusing here says it once. Use `off` to take no look.")

#: Whether each provider's key is present, one literal name per read. Spelled out rather than looped over a
#: table of names because the configuration certification refuses a name built from data
#: (devtools/quality/config_inventory.py) and it is right to: a name assembled at runtime is one no
#: catalogue can check. The names are the measurement package's own
#: (geometry_agent.vision.client._openai / _anthropic / _deepinfra / _deepseek).
_VISION_KEY_PRESENT: dict[str, bool] = {
    "openai": bool(optional_env("OPENAI_API_KEY", "").strip()),
    "anthropic": bool(optional_env("ANTHROPIC_API_KEY", "").strip()),
    "deepinfra": bool(optional_env("DEEPINFRA_API_KEY", "").strip()),
    "deepseek": bool(optional_env("DEEPSEEK_API_KEY", "").strip()),
    "off": True}
#: The variable the configured reader's key would be in, for a message that names it. Read from a table, not
#: used to read the environment, so it is documentation rather than a lookup.
_VISION_KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                     "deepinfra": "DEEPINFRA_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "off": ""}


def vision_reader_has_no_key() -> str:
    """Why this deployment cannot look, or "" when it can. The one place the question is answered.

    Said as a sentence rather than a boolean because every caller wants to repeat it: the refusal below,
    the deploy preflight, and a startup banner. It reports on the CONFIGURED reader only - a provider with
    no key is a look that does not happen, never a fall-through to another provider, so the other three
    keys are irrelevant to the verdict however many of them are set.
    """
    if _VISION_KEY_PRESENT.get(GEOMETRY_VISION_PROVIDER, False):
        return ""
    return (f"GEOMETRY_VISION_PROVIDER={GEOMETRY_VISION_PROVIDER} but "
            f"{_VISION_KEY_NAMES[GEOMETRY_VISION_PROVIDER]} is unset, so no upload will be looked at: "
            f"every row stores 'the configured reader has no key in this environment'. The look is the "
            f"half of the Surveyor that finds a passage the measurement calls plain.")


# A MISSING KEY IS LOUD, NOT A ROW THAT SAYS NOTHING HAPPENED. Same shape as the auth secrets above and the
# durable checkpointer below: outside the dev set, a configuration that silently degrades the product is a
# refusal to start rather than a per-job note in a log nobody reads. Absence of a key is NOT a decision to
# take no look - a default is not a confirmation - so the deployment has to say which it meant:
# GEOMETRY_VISION_PROVIDER=off is a statement, and an unset key is an accident.
#
# In the dev set it is not fatal: a developer with no OpenAI account must still be able to run the stack,
# and `vision_reader_has_no_key()` is what the startup banner says it with.
if requires_hardened_runtime(ENV):
    _no_reader = vision_reader_has_no_key()
    if _no_reader:
        raise ConfigurationError(
            f"ENV={ENV!r} is a non-dev (hosted) environment and {_no_reader} "
            f"Set the key, or set GEOMETRY_VISION_PROVIDER=off to say this deployment takes no look.")

# THE CHAIN, in Rehaan's order: the customer says what the part is for (intake), the stored measurement
# is composed against what they said (measure), the look is taken with that purpose (look), intake puts
# the Surveyor's own questions to them and posts the answers back with who gave them (intake), the
# geometry agent plans the part from the survey and those answers (geometry), a question only the plan
# can raise is put once (intake), and the builder receives both write-ups with the survey inside the
# typed block after the request cut.
#
# THE GEOMETRY AGENT'S STEP costs one run of the agent's loop per submission whose answers changed,
# inside the submission turn. The loop's own budget is GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS; over the
# clean pair of 322-case learning runs the median case took 85 s and the p90 125 s (agent/loop.py).
# Every handoff is checked by the package's own contract and every step is written to the job ledger on
# the survey row. If anything in it fails, the job runs without the plan and the reason is logged and
# kept on the row as `status: failed`, which is the fact the builder's side reads and says again.
#: Which model plans. Named, never discovered, for the look's reason: the package's `auto` falls
#: through providers, and a plan made by a model nobody chose is not a plan anybody measured.
#: `reference` is the package's deterministic stand-in policy, for tests and proofs; its plans are
#: grounded by the same checker and its ledger rows say `heuristic`, never `live`.
GEOMETRY_AGENT_STEP_PROVIDER: str = optional_env("GEOMETRY_AGENT_STEP_PROVIDER", "deepseek").strip().lower()
#: The geometry agent's wall clock for one plan. Past it the step records a failure on the row and the
#: job runs without the plan. ZERO MEANS NO CLOCK: the loop then runs to its own end, which on a
#: model that will not settle is the submission turn waiting on it.
#:
#: The wait is `asyncio.wait_for` over `asyncio.to_thread`, so what the timeout ends is the WAITING,
#: not the work: Python cannot stop a thread. The loop's own budget is what actually stops it, which
#: is why it is given this number too.
GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS: int = int(optional_env("GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS", "300"))
#: Where the job ledger's rows are ALSO appended as JSONL, for the package's own ledger tools. Empty,
#: the default, keeps them on the survey row only, which is where the durable copy always lives.
GEOMETRY_AGENT_LEDGER_PATH: str = optional_env("GEOMETRY_AGENT_LEDGER_PATH", "").strip()

# TWO STAGES INSIDE THE MEASUREMENT PACKAGE, each its own setting and each off by default. THESE ARE
# NOT GATES OVER THIS PLATFORM'S OWN FEATURE, which is why they outlived the five that were: they are
# the package's switches, they each cost something real, and this platform's job is to be the one place
# an operator sets them and the one place they are documented. Without an entry here they were reachable
# only by setting an undocumented environment variable by hand, which is not a switch anybody can find.
#
# WHERE A PASSAGE STOPS WITH NO MOUTH. The stops detector is a measurement and always was, but until
# this stage it ran inside the look's renderer, so a closed pipe end reached the builder only on a job
# that had looked. On, it runs at measure time and the builder is told about closed ends whether or not
# the look ran. It costs one more pass over the mesh at upload and it adds `facts.passage_ends`; off,
# that field is absent and the stored document is byte for byte what it was.
#
# IT IS READ WHERE THE FILE IS OPENED, which is why `arm_the_package` exists: the package reads it from
# the environment inside `facts.measure`, and the measurement runs in a child process that inherits this
# one's environment. Setting it once and never unsetting it is safe; setting and unsetting it around a
# call would be two measurements in one worker racing each other over one variable.
GEOMETRY_MEASURED_STOPS_ENABLED: bool = (
    optional_env("GEOMETRY_MEASURED_STOPS_ENABLED", "false").lower() == "true")

# WHICH SIDE OF THE SURFACE IS THE FLUID. A ring with one hole through a thick body reads the same as
# the end of an annular passage and as the mouth of a bore through solid metal, and the file cannot say
# which. Off, the mesher's own reading stands unremarked and a solid plate can reach the builder with
# confident junction places on it. On, the survey says so, puts the question to the customer, and places
# nothing on the flow path until somebody answers; an answer composes the survey again with the side
# they named. It costs the customer a question they may not need: most briefs settle the side themselves.
GEOMETRY_FLUID_SIDE_ENABLED: bool = (
    optional_env("GEOMETRY_FLUID_SIDE_ENABLED", "false").lower() == "true")


def arm_the_package() -> dict[str, str]:
    """Put the two platform settings into the environment the measurement package reads them from.

    CALLED FROM EXACTLY TWO PLACES, named here so the next reader can check rather than trust this
    sentence: `application/geometry_measurement.measure_local_file` and
    `application/geometry_survey.composition`. This docstring said it was called at those two points
    while nothing called it at all, and the two settings above were therefore switches an operator could
    set with no effect whatever. `tests/unit/application/test_geometry_package_switches.py` asserts the
    call at both, and asserts it happens BEFORE the package is reached.

    Idempotent: it writes the same value every time and never unsets one, so two jobs in one worker
    cannot disagree about it and a child process inherits whatever the operator set.

    AN OPERATOR'S OWN SETTING WINS. Where the package's variable is already in the environment it is
    left alone, because a developer who exported it meant it and a platform that overwrote it would make
    the package's own tests and evals unrunnable in the same shell.

    THE WRITING IS IN `settings/package_switches.py` and the DECIDING is here, which is the split the whole
    settings tier keeps: no production module may touch `os.environ`, and giving this one module an exemption
    for two lines would blind that rule to every future direct read in the platform's largest settings file.
    """
    return package_switches.arm(measured_stops=GEOMETRY_MEASURED_STOPS_ENABLED,
                                fluid_side=GEOMETRY_FLUID_SIDE_ENABLED)

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
