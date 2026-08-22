# Responsibility: Declare how this deployment reaches its external services.
# Boundaries: endpoints, credentials and pool sizing; no policy decision.
from __future__ import annotations

from meshpipeline.settings.env import (
    ConfigurationError,
    normalize_database_url,
    optional_env,
)

# LLM inference (DeepSeek / DeepInfra, OpenAI wire protocol)
# The API keys are OPTIONAL at import - like every other provider credential in this module
# (Tavily, MinIO, GCP). A provider's key is REQUIRED only when that provider is part of the
# ENABLED profile - i.e. a configured route actually references it. That derivation lives in the
# routing layer (adapters.model_inference.routes.missing_provider_credentials) because it reads the
# routes, and settings/ must not import upward; runtime.startup.validate enforces it at boot. This
# is what lets a single-provider profile skip the other provider's key, and local/test import none.
DEEPSEEK_API_KEY: str  = optional_env("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = optional_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL: str    = optional_env("DEEPSEEK_MODEL",    "deepseek-v4-pro")
DEEPINFRA_API_KEY: str  = optional_env("DEEPINFRA_API_KEY", "")
DEEPINFRA_BASE_URL: str = optional_env("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai")

DEEPINFRA_CALL_TIMEOUT: int      = int(optional_env("DEEPINFRA_CALL_TIMEOUT",      "1800"))
DEEPINFRA_CONNECT_TIMEOUT: float = float(optional_env("DEEPINFRA_CONNECT_TIMEOUT", "15"))
DEEPINFRA_READ_TIMEOUT: float    = float(optional_env("DEEPINFRA_READ_TIMEOUT",    "60"))
DEEPINFRA_WRITE_TIMEOUT: float   = float(optional_env("DEEPINFRA_WRITE_TIMEOUT",   "30"))

# search-result summarizer (a cheap model)
SEARCH_SUMMARIZER_MODEL: str         = optional_env("SEARCH_SUMMARIZER_MODEL", "deepseek-v4-flash")
SEARCH_SUMMARIZER_MAX_TOKENS: int    = int(optional_env("SEARCH_SUMMARIZER_MAX_TOKENS", "700"))
SEARCH_SUMMARIZER_TEMPERATURE: float = float(optional_env("SEARCH_SUMMARIZER_TEMPERATURE", "0.2"))

# web search provider
WEB_SEARCH_ENABLED: bool    = optional_env("WEB_SEARCH_ENABLED", "true").lower() == "true"
WEB_SEARCH_PROVIDER: str    = optional_env("WEB_SEARCH_PROVIDER", "searxng")  # searxng | tavily
WEB_SEARCH_BASE_URL: str    = optional_env("WEB_SEARCH_BASE_URL", "http://localhost:8080")
WEB_SEARCH_MAX_RESULTS: int = int(optional_env("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_TIMEOUT: int     = int(optional_env("WEB_SEARCH_TIMEOUT", "20"))
TAVILY_API_KEY: str         = optional_env("TAVILY_API_KEY", "")  # only if provider=tavily
if WEB_SEARCH_PROVIDER not in ("searxng", "tavily"):
    raise ConfigurationError(
        f"WEB_SEARCH_PROVIDER must be 'searxng' or 'tavily', got '{WEB_SEARCH_PROVIDER}'")

# object storage (MinIO local / GCS hosted)
import re as _re  # noqa: E402


def _valid_bucket_name(name: str, *, var: str) -> str:
    b = str(name or "").strip()
    if not b:
        raise ConfigurationError(f"{var} must be a non-empty bucket name (got {name!r})")
    problems: list[str] = []
    if not (3 <= len(b) <= 63):
        problems.append("must be 3-63 characters")
    if b != b.lower():
        problems.append("must be lowercase (S3/MinIO reject uppercase - a common legacy-codename trap)")
    if not _re.fullmatch(r"[a-z0-9.-]+", b):
        problems.append("may contain only lowercase letters, digits, '-' and '.'")
    if b and (not b[0].isalnum() or not b[-1].isalnum()):
        problems.append("must start and end with a letter or digit")
    if ".." in b:
        problems.append("must not contain consecutive dots")
    if _re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", b):
        problems.append("must not be formatted as an IP address")
    if problems:
        raise ConfigurationError(
            f"{var}={name!r} is not a valid object-store bucket name: "
            + "; ".join(problems) + ". Use e.g. 'mesh-artifacts'.")
    return b


MINIO_ENDPOINT: str        = optional_env("MINIO_ENDPOINT",       "localhost:9000")
MINIO_ACCESS_KEY: str      = optional_env("MINIO_ACCESS_KEY",     "minioadmin")
MINIO_SECRET_KEY: str      = optional_env("MINIO_SECRET_KEY",     "minioadmin")
# The address a BROWSER reaches the object store on. A different fact from MINIO_ENDPOINT, which is
# the address THIS PROCESS reaches it on: under compose they differ (minio:9000 inside the network,
# localhost:9000 from the host). The distinction matters only for signed URLs, because the host is
# part of the SigV4 signature: a URL signed for minio:9000 cannot be resolved by a browser, and
# rewriting its host afterwards invalidates the signature. So a URL handed OUT is signed with this.
# Blank means "the same address for both", which is the hosted case and the pre-existing behaviour.
MINIO_PUBLIC_ENDPOINT: str = (optional_env("MINIO_PUBLIC_ENDPOINT", "").strip()
                              or MINIO_ENDPOINT)
# Passed to the client explicitly because minio-py otherwise resolves it with a live
# GetBucketLocation request BEFORE it signs anything. That request is why signing cannot simply be
# pointed at the public address: the address a browser uses is not necessarily one this process can
# reach, and the lookup fails there. Given a region up front the client signs locally and dials
# nothing. us-east-1 is MinIO's own default and the value minio-py itself falls back to; an
# S3-compatible store in another region sets this, and a wrong value fails loudly on first use
# rather than silently on the download path.
MINIO_REGION: str          = optional_env("MINIO_REGION", "us-east-1")
# The local artifact bucket, created fresh by the stack (minio-init). It is validated below: S3/
# MinIO bucket names must be lowercase and DNS-compatible, so a coworker who omits MINIO_BUCKET still
# gets a working default and any invalid value (e.g. a former uppercase codename) fails LOUDLY at
# startup instead of a stack that silently never becomes ready. Hosted GCS uses its own
# deployment-owned bucket name, not this default.
MINIO_BUCKET: str          = _valid_bucket_name(optional_env("MINIO_BUCKET", "mesh-artifacts"),
                                                var="MINIO_BUCKET")
MINIO_SIGNED_URL_TTL: int  = int(optional_env("MINIO_SIGNED_URL_TTL", "900"))

# datastores (Postgres + Redis)
POSTGRES_HOST: str     = optional_env("POSTGRES_HOST",     "localhost")
POSTGRES_PORT: int     = int(optional_env("POSTGRES_PORT", "5432"))
POSTGRES_DB: str       = optional_env("POSTGRES_DB",       "meshpipeline")
POSTGRES_USER: str     = optional_env("POSTGRES_USER",     "meshpipeline")
POSTGRES_PASSWORD: str = optional_env("POSTGRES_PASSWORD", "")
POSTGRES_DSN: str = (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
# A single managed-Postgres URL (Neon) takes precedence when present; normalised to asyncpg
# with libpq ssl params stripped (TLS requested via connect_args, see persistence/session.py).
DATABASE_URL: str = optional_env("DATABASE_URL", "")
DB_SSL_REQUIRED: bool = False
if DATABASE_URL:
    POSTGRES_DSN, DB_SSL_REQUIRED = normalize_database_url(DATABASE_URL)

# connection budgeting (see persistence/connection_budget.py)
# Pool per PROCESS. Conservative production defaults, ROLE-specialised via env: the API service
# keeps a modest pool; the pipeline JOB (one job, one graph) overrides these to a tiny pool in its
# manifest so 20 concurrent one-shot jobs cannot each hold a large general-purpose pool.
DB_POOL_SIZE: int     = int(optional_env("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW: int  = int(optional_env("DB_MAX_OVERFLOW", "5"))
DB_POOL_TIMEOUT: int  = int(optional_env("DB_POOL_TIMEOUT", "30"))
DB_POOL_RECYCLE: int  = int(optional_env("DB_POOL_RECYCLE", "1800"))   # recycle before an idle cut

REDIS_URL: str = optional_env("REDIS_URL", "redis://localhost:6379/0")

# Google Cloud + pipeline-execution selection
GCP_PROJECT_ID: str        = optional_env("GCP_PROJECT_ID", "")
GCP_REGION: str            = optional_env("GCP_REGION", "us-central1")
GCP_MESH_BUCKET: str       = optional_env("GCP_MESH_BUCKET", "")
# The Cloud Run MESH job the local pipeline invokes. NO default: this is a deployment-owned
# resource name that differs per organisation, so hard-coding one would silently address someone
# else's infrastructure. require_cloudrun_config() demands it; empty here so a missing value
# fails loudly at dispatch.
CLOUDRUN_JOB: str          = optional_env("CLOUDRUN_JOB", "")
GOOGLE_APPLICATION_CREDENTIALS: str = optional_env("GOOGLE_APPLICATION_CREDENTIALS", "")
# How the multi-hour LangGraph pipeline is launched, always in this deployment:
# "celery" (the local worker) | "deferred" (persist the payload for an explicit later
# launch - what the native tiers drive).
PIPELINE_BACKEND: str = optional_env("PIPELINE_BACKEND", "celery")

# the BACKEND'S OWN execution identity for this process, used by the durable worker claim to
# tell a RESTART of the same logical execution (which resumes its generation) from a genuinely new
# dispatch (which becomes a new generation once the old lease expires). The neutral
# PIPELINE_EXECUTION_ID is the contract; the platform-specific variable is mapped here - in settings,
# where deployment identity belongs - so the run use case names no backend and imports no vendor SDK.
# Empty means "not supplied": the worker then falls back to a stable per-process id, which is still
# correct (a different process is a different execution), just coarser.
PIPELINE_EXECUTION_ID: str = optional_env(
    "PIPELINE_EXECUTION_ID", optional_env("CLOUD_RUN_EXECUTION", ""))

# observability providers (Sentry error tracking, Langfuse tracing)
SENTRY_DSN: str = optional_env("SENTRY_DSN", "")
LANGFUSE_PUBLIC_KEY: str = optional_env("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY: str = optional_env("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST: str       = optional_env("LANGFUSE_HOST", "")

# The name of the env var that credentials each LLM provider - the one fact the routing layer
# needs to turn "provider X is enabled" into "set X_API_KEY". It lives here, next to the values it
# names, so the credential surface is described in one place; the routing layer reads it.
LLM_PROVIDER_KEY_ENV: dict[str, str] = {
    "deepinfra": "DEEPINFRA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
