# Responsibility: Declare every supported setting once: its default, whether it is required or secret, and its purpose.
# Owns: the grouped inventory, the generated .env, and the register of removed names.
# Boundaries: THE source for configuration.
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

#: WHERE a setting is meant to be seen. Every omission from the generated .env.example is
#: explained by this field: the generator has no exception list, so a setting cannot be dropped
#: from the template without saying so here.
#:   template  a developer/operator turns this knob; it is written to .env.example
#:   internal  an advanced control the product supports but does not put in front of an operator;
#:             real, documented in the catalogue, deliberately absent from the ordinary template
#:   external  set by the platform or a library rather than by a person editing .env (a Cloud Run
#:             injection, an SDK's own variable, a value Compose computes)
EXPOSURES = ("template", "internal", "external")

#: WHO reads it. `app` means this process reads it through the settings loader; the others name a
#: consumer outside src/, which is why such an entry can be declared and still never appear in an
#: AST scan of the application.
CONSUMERS = ("app", "compose", "make", "sdk")

#: What the value is parsed as, so a reader and the catalogue cannot disagree about the shape.
KINDS = ("str", "int", "float", "bool", "path", "list", "url")


@dataclass(frozen=True)
class EnvVar:
    name: str
    default: str = ""       # value written to a generated .env; blank means optional/disabled or a required value the user must supply
    required: bool = False  # a real value must be supplied before a real mesh job can run
    secret: bool = False    # a real credential; the inventory never carries one, so a generated .env never prints one
    help: str = ""          # one line; becomes the comment above the var in a generated .env
    kind: str = "str"       # one of KINDS: the type the reader coerces to
    exposure: str = "template"   # one of EXPOSURES: see above; decides .env.example membership
    consumer: str = "app"        # one of CONSUMERS: who actually reads the variable
    #: A default that is not a literal but a documented function of ANOTHER declared setting, e.g.
    #: "<DATA_ROOT>/jobs". The catalogue records the RELATIONSHIP; settings/runtime.py resolves it.
    #: This is not a dynamic environment NAME: the name is fixed and declared; only the value is
    #: computed, which is what keeps a machine-specific absolute path out of the template.
    derived: str = ""
    #: The fallback the CODE uses when the variable is unset, when that must differ from the value
    #: the template seeds. A secret is the honest case: `.env.example` seeds POSTGRES_PASSWORD so a
    #: fresh local stack works, while the code must fall back to empty so a deployment that forgot
    #: to set one fails closed instead of quietly running on a published dev password.
    runtime_default: str | None = None

    def code_default(self) -> str:
        return self.default if self.runtime_default is None else self.runtime_default

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind {self.kind!r} is not one of {KINDS}")
        if self.exposure not in EXPOSURES:
            raise ValueError(f"{self.name}: exposure {self.exposure!r} is not one of {EXPOSURES}")
        if self.consumer not in CONSUMERS:
            raise ValueError(f"{self.name}: consumer {self.consumer!r} is not one of {CONSUMERS}")
        if self.derived and self.default:
            raise ValueError(
                f"{self.name}: a derived default and a literal default are two answers to the "
                "same question: declare one")


@dataclass(frozen=True)
class Group:
    title: str
    vars: list[EnvVar]
    note: str = ""          # short block comment above the group


#: The one reason the five retired data-layout names share.
_RETIRED_DATA_LAYOUT = (
    "retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and "
    "corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR.")

# Settings DELETED without an alias, and why. A removed name is rejected at startup rather than
# ignored: silently accepting a stale variable is how an operator's configured value ends up
# meaning something it never meant. THE authority: nothing else may keep a second list.
REMOVED: dict[str, str] = {
    "REVIEWER_MAX_TOOL_CALLS":
        "renamed to REVIEWER_MAX_ROUNDS: it limits provider ROUNDS, not tool calls. The old "
        "name is not reused: a value set for a round budget must not silently become a "
        "tool-call budget.",
    "TRACE_CAPTURE_ENABLED":
        "deleted. It gated a trace.jsonl file nothing read, and because it wrapped the durable "
        "write it silenced capture entirely: so a deployment that had collection on got no "
        "records. DATA_COLLECTION_ENABLED is the one switch.",
    "EXPORT_CONVERSATION_DATA":
        "deleted. It gated the whole corpus sample rather than the conversation, and only ever "
        "applied behind DATA_COLLECTION_ENABLED.",
    "DEBUG_ENDPOINTS_ENABLED":
        "deleted with the /api/v1/debug routes it guarded. Nothing consumed them.",
    "OBJECT_STORE_BACKEND":
        "deleted. 'minio' was the only accepted value. The mesh-job exchange on GCS is addressed "
        "by the mesh adapter, not by this setting.",
    "MINIO_USE_SSL":
        "deleted. The object store is a service on the local stack, reached over plain HTTP.",
    "EVENTS_LOG_TTL_HOURS":
        "deleted with the sweep it configured, which expired a file nothing wrote any more.",
    "MESH_BACKEND":
        "deleted. The application always delegates meshing to the Cloud Run mesh job; it never "
        "meshes on the machine serving the UI. The native test tiers bind the local runner "
        "themselves and need no deployment setting.",
    # The data-layout names. These were refused by a SECOND hand-maintained tuple inside
    # settings/runtime.py, so "the removed names" had two answers and only one of them was
    # documented. Merged here so there is one authority; runtime now iterates this mapping.
    "PLANNER_TIMEOUT_SECONDS":
        "removed. The planner's per-attempt ceiling is PLANNER_TIMEOUT, one of the twelve declared "
        "settings of the planner model route. The old name briefly survived as the route's default "
        "and now controls nothing, so it is refused rather than read and discarded.",
    "OUTPUTS_BASE": _RETIRED_DATA_LAYOUT,
    "UPLOADS_BASE": _RETIRED_DATA_LAYOUT,
    "UPLOAD_STAGING_ROOT": _RETIRED_DATA_LAYOUT,
    "RUNTIME_DATA_ROOT": _RETIRED_DATA_LAYOUT,
    "RUNTIME_SAMPLES_DIR": _RETIRED_DATA_LAYOUT,
}


#: THE route matrix. Five roles, twelve settings each: sixty explicit names, declared here and
#: nowhere else. settings/routes.py used to build these names with f"{prefix}_{suffix}", which
#: meant the supported configuration surface existed only as a formula: nothing could list it,
#: the template could not carry it, and a typo in an operator's .env was indistinguishable from a
#: setting the product does not have. The repetition below is the point.
#:
#: (role, prefix, provider, model, timeout, attempts, backoff_base, backoff_max, budget, queue)
ROUTE_MATRIX: list[tuple[str, str, str, str, str, str, str, str, str, str]] = [
    ("intake",     "INTAKE",            "deepseek",  "deepseek-v4-pro",
     "120.0",  "5", "5.0",  "60.0",  "16", "15.0"),
    ("builder",    "BUILDER",           "deepinfra", "zai-org/GLM-5.2",
     "1800.0", "3", "15.0", "120.0", "8",  "30.0"),
    ("visual_reviewer", "VISUAL_REVIEWER", "deepinfra", "Qwen/Qwen3-VL-235B-A22B-Thinking",
     "1800.0", "3", "5.0",  "60.0",  "8",  "30.0"),
    ("summarizer", "SEARCH_SUMMARIZER", "deepseek",  "deepseek-v4-flash",
     "60.0",   "2", "2.0",  "15.0",  "8",  "10.0"),
    ("planner",    "PLANNER",           "deepinfra", "zai-org/GLM-5.2",
     "1800.0", "3", "15.0", "120.0", "4",  "30.0"),
]

#: suffix -> (kind, help). The per-route default comes from ROUTE_MATRIX; everything else about
#: the setting is the same whichever role it belongs to.
ROUTE_SUFFIXES: list[tuple[str, str, str]] = [
    ("PROVIDER",           "str",   "which supported provider serves this role"),
    ("MODEL",              "str",   "the model identifier sent to that provider"),
    ("ACCOUNT",            "str",   "quota account; roles sharing one provider+account+model share a concurrency domain"),
    ("STANDBY_PROVIDER",   "str",   "optional second provider; set together with the standby model or startup refuses"),
    ("STANDBY_MODEL",      "str",   "optional second model; set together with the standby provider or startup refuses"),
    ("STANDBY_ACCOUNT",    "str",   "quota account for the standby; read only when a standby is configured"),
    ("TIMEOUT",            "float", "per-attempt ceiling in seconds"),
    ("MAX_ATTEMPTS",       "int",   "attempts before the role fails"),
    ("BACKOFF_BASE",       "float", "first retry delay in seconds"),
    ("BACKOFF_MAX",        "float", "retry delay ceiling in seconds"),
    ("CONCURRENCY_BUDGET", "int",   "in-flight calls allowed for this role's quota domain"),
    ("QUEUE_DEADLINE",     "float", "seconds a call may wait for admission before the standby is considered"),
]


def _route_default(row: tuple, suffix: str) -> str:
    _, _, provider, model, timeout, attempts, bo_base, bo_max, budget, queue = row
    return {"PROVIDER": provider, "MODEL": model, "ACCOUNT": "default",
            "STANDBY_PROVIDER": "", "STANDBY_MODEL": "", "STANDBY_ACCOUNT": "default",
            "TIMEOUT": timeout, "MAX_ATTEMPTS": attempts, "BACKOFF_BASE": bo_base,
            "BACKOFF_MAX": bo_max, "CONCURRENCY_BUDGET": budget, "QUEUE_DEADLINE": queue}[suffix]


def route_setting_name(prefix: str, suffix: str) -> str:
    # Used ONLY to build the catalogue below and to look an entry up by role. It never reaches an
    # environment read: settings/routes.py asks for a declared EnvVar, and the loader reads that
    # entry's own name.
    return f"{prefix}_{suffix}"


def _route_groups() -> list[Group]:
    out = []
    for row in ROUTE_MATRIX:
        role, prefix = row[0], row[1]
        out.append(Group(
            f"Model route: {role}",
            note=f"The {role} role's provider, model and reliability budget. Every name below is "
                 "supported configuration; leave them unset to run the shipped route.",
            vars=[EnvVar(route_setting_name(prefix, suffix), _route_default(row, suffix),
                         kind=kind, help=hlp)
                  for suffix, kind, hlp in ROUTE_SUFFIXES]))
    return out


@dataclass(frozen=True)
class RemovedFamily:
    #: An anchored pattern for configuration that USED to be accepted under a name built from data.
    #: It exists ONLY to refuse history. It never authorises a live setting: nothing reads a name
    #: matched here, and a test proves no live catalogue entry can match one.
    label: str          # the shape an operator would recognise, e.g. MODEL_PRICE_<PROVIDER>_<MODEL>
    pattern: str        # anchored regex over the whole variable name
    replacement: str    # the live setting that now carries this configuration
    guidance: str

    def matches(self, name: str) -> bool:
        return re.fullmatch(self.pattern, name) is not None


REMOVED_FAMILIES: list[RemovedFamily] = [
    RemovedFamily(
        label="MODEL_PRICE_<PROVIDER>_<MODEL>",
        # NOT MODEL_PRICE_OVERRIDES: the live setting shares the prefix, so the exclusion is part
        # of the pattern rather than a special case applied afterwards.
        pattern=r"MODEL_PRICE_(?!OVERRIDES$).+",
        replacement="MODEL_PRICE_OVERRIDES",
        guidance="one price per record: 'provider:model=in,out,cached', records separated by ';'."),
    RemovedFamily(
        label="MODEL_BUDGET_<DOMAIN>",
        pattern=r"MODEL_BUDGET_.+",
        replacement="MODEL_DOMAIN_BUDGETS",
        guidance="one budget per record: 'provider:account:model=N', records separated by ';'."),
]


def retired_reason(name: str) -> str | None:
    # THE single answer to "is this name retired, and what replaced it?" - exact names first,
    # then the anchored legacy families. Returns None for anything still supported.
    if name in REMOVED:
        return REMOVED[name]
    for fam in REMOVED_FAMILIES:
        if fam.matches(name):
            return (f"was one of the {fam.label} variables, whose NAME carried provider and model "
                    f"data. That form is no longer read. Move it to {fam.replacement}: "
                    f"{fam.guidance}")
    return None


def retired_present(environ) -> list[tuple[str, str]]:
    # Catalogue-derived inspection of the environment for RETIRED configuration only. It resolves
    # no value and authorises nothing: the caller receives names and guidance, never data.
    found: list[tuple[str, str]] = []
    for name in environ:
        reason = retired_reason(name)
        if reason is not None:
            found.append((name, reason))
    return sorted(found)


INVENTORY: list[Group] = [
    Group("LLM: DeepSeek (intake / search summarizer)", note="A real key is the one thing you must set for a live job.", vars=[
        EnvVar("DEEPSEEK_API_KEY", "", required=True, secret=True, help="real key from platform.deepseek.com"),
        EnvVar("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        EnvVar("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    ]),
    Group("LLM: DeepInfra (builder + reviewer, OpenAI-compatible)", vars=[
        EnvVar("DEEPINFRA_API_KEY", "", required=True, secret=True, help="real key from deepinfra.com"),
        EnvVar("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1/openai"),
        EnvVar("REVIEWER_MODEL", "Qwen/Qwen3-VL-235B-A22B-Thinking"),
        EnvVar("BUILDER_MAX_TOKENS", "16384", help="DeepInfra caps output at 16384 tokens per response for these models"),
        EnvVar("REVIEWER_MAX_TOKENS", "16384"),
        EnvVar("REVIEWER_TEMPERATURE", "0.7", help="reviewer sampling; the value the shipped stack has always run with"),
        EnvVar("REVIEWER_TOP_P", "0.8"),
        EnvVar("REVIEWER_PRESENCE_PENALTY", "1.5"),
    ]),
    Group("Resilience (circuit breaker)", note="A dependency that fails repeatedly trips its breaker and the job fails as a system issue; there are no fallback models that silently degrade mesh quality.", vars=[
        EnvVar("CIRCUIT_FAILURE_THRESHOLD", "5"),
        EnvVar("CIRCUIT_RECOVERY_SECONDS", "30"),
        EnvVar("CIRCUIT_HALF_OPEN_MAX", "1"),
        EnvVar("CELERY_MAX_REDELIVERIES", "3", help="a job that keeps crashing the worker is dead-lettered after this many deliveries"),
    ]),
    Group("Remote mesh execution", note="The local stack is the control plane; every mesh runs off-box on the Cloud Run mesh job. dev-up and dev-doctor require the values below and refuse to start without them.", vars=[
        EnvVar("GCP_PROJECT_ID", "", required=True, help="the project hosting the Cloud Run mesh job"),
        EnvVar("GCP_REGION", "us-central1"),
        EnvVar("CLOUDRUN_JOB", "", required=True, help="the Cloud Run mesh job to dispatch to"),
        EnvVar("GCP_MESH_BUCKET", "", required=True, help="the GCS bucket workspaces are exchanged through"),
        EnvVar("GOOGLE_ADC_FILE", "./secrets/gcp/application_default_credentials.json", required=True, kind="path", consumer="compose", help="THE canonical location, resolved from the repository root. Obtain your own file with 'gcloud auth application-default login' and move it there; Hexera never creates it. A path, never a credential: never paste the JSON here, and never use someone else's file"),
        EnvVar("OPENFOAM_BASHRC", "/usr/lib/openfoam/openfoam2412/etc/bashrc"),
        EnvVar("OPENFOAM_COMMAND_TIMEOUT", "1500"),
        EnvVar("BUILDER_LOOP_TIMEOUT", "3600", help="must be >= 2 * OPENFOAM_COMMAND_TIMEOUT (validated at startup)"),
    ]),
    Group("Agent round budgets", note="How many PROVIDER ROUNDS one agent invocation may take. Not tool-call budgets: an agent may issue several tool calls inside one round. The defaults are the shipped values; leave them unset unless you have measured a reason.", vars=[
        EnvVar("INTAKE_MAX_ROUNDS", "20", help="rounds per intake conversation turn; must be a positive integer"),
        EnvVar("BUILDER_MAX_ROUNDS", "60", help="rounds for a first build attempt"),
        EnvVar("BUILDER_RETRY_MAX_ROUNDS", "45", help="rounds for a rebuild attempt: shorter, because a retry starts from a reviewed failure"),
        EnvVar("REVIEWER_MAX_ROUNDS", "60", help="rounds for one review invocation"),
    ]),
    Group("PostgreSQL", note="Addresses on this machine: .env is edited on the host. The stack sets the container names its services answer on.", vars=[
        EnvVar("POSTGRES_HOST", "localhost"),
        EnvVar("POSTGRES_PORT", "5432"),
        EnvVar("POSTGRES_DB", "meshpipeline"),
        EnvVar("POSTGRES_USER", "meshpipeline"),
        EnvVar("POSTGRES_PASSWORD", "localdev", secret=True, runtime_default="", help="fine for local dev; set a strong value for production: the code falls back to empty so an unset password fails closed"),
        EnvVar("DATABASE_URL", "", secret=True, help="a managed-Postgres connection string (e.g. Neon). When set it is THE database and the POSTGRES_* parts above are unused"),
    ]),
    Group("Redis (broker + pub/sub)", vars=[
        EnvVar("REDIS_PASSWORD", "", secret=True, consumer="compose", help="set in production and use a credentialed REDIS_URL"),
        EnvVar("REDIS_URL", "redis://localhost:6379/0"),
    ]),
    Group("MinIO / S3", vars=[
        EnvVar("MINIO_ENDPOINT", "localhost:9000"),
        EnvVar("MINIO_PUBLIC_ENDPOINT", "localhost:9000", runtime_default="", help="the address a BROWSER reaches the store on. Signed download URLs are signed FOR their host, so this must be the address the user's browser uses, not the one the container dials. Blank = same as MINIO_ENDPOINT"),
        EnvVar("MINIO_ACCESS_KEY", "minioadmin"),
        EnvVar("MINIO_REGION", "us-east-1", help="signed into every URL as part of the SigV4 credential scope, and passed explicitly so the client never makes a GetBucketLocation call to discover it. MinIO's default; change only for a store that reports a different region"),
        EnvVar("MINIO_SECURE", "false", kind="bool", help="reach the object store over TLS. False for the local compose stack, which serves plain HTTP on the same host. A hosted S3-compatible endpoint (Google Cloud Storage through its S3-interoperability API, for one) serves TLS only and refuses a plain-HTTP request"),
        EnvVar("MINIO_SECRET_KEY", "minioadmin", secret=True, help="MinIO's own local default"),
        EnvVar("MINIO_BUCKET", "mesh-artifacts", help="bucket names must be lowercase; an uppercase value is rejected and the stack never becomes ready"),
        EnvVar("MINIO_SIGNED_URL_TTL", "900"),
    ]),
    Group("Web search sub-agent (SearXNG retrieval + DeepSeek distillation)", vars=[
        EnvVar("WEB_SEARCH_ENABLED", "true"),
        EnvVar("WEB_SEARCH_PROVIDER", "searxng"),
        EnvVar("WEB_SEARCH_BASE_URL", "http://localhost:8080"),
    ]),
    Group("Auth / environment", note="ENV=production enforces a non-empty MESH_API_KEY, non-wildcard CORS_ORIGINS, and a set POSTGRES_PASSWORD at startup. USER_TOKEN_SECRET enables signed X-User-Id so identity is not self-asserted.", vars=[
        EnvVar("ENV", "dev"),
        EnvVar("MESH_API_KEY", "", secret=True),
        EnvVar("USER_TOKEN_SECRET", "", secret=True),
        EnvVar("CORS_ORIGINS", "*"),
    ]),
    Group("Product modes", vars=[
        EnvVar("DATA_COLLECTION_ENABLED", "true",
               help="Save redacted run data for training and corpus export."),
        EnvVar("PUBLIC_TRACE_MODE", "raw",
               help="Control technical details shown through the UI: safe or raw."),
        EnvVar("ALLOW_PUBLIC_RAW_TRACE", "true",
               help="Required when PUBLIC_TRACE_MODE=raw to prevent accidental disclosure."),
    ]),
    Group("Optional: Langfuse tracing (leave blank to disable)", vars=[
        EnvVar("LANGFUSE_PUBLIC_KEY", ""),
        EnvVar("LANGFUSE_SECRET_KEY", "", secret=True),
        EnvVar("LANGFUSE_HOST", ""),
    ]),
    Group("Quotas", vars=[
        EnvVar("MAX_JOBS_PER_OWNER", "5"),
        EnvVar("MAX_CONCURRENT_JOBS", "20"),
        EnvVar("RECONCILE_RETRY_DELAY_SECONDS", "300", kind="int",
               help="how long a retryable artifact-reconciliation failure waits before the "
                    "sweep may claim it again"),
        EnvVar("CELERY_WORKER_CONCURRENCY", "2", kind="int", consumer="compose", help="read by docker-compose.yml when it starts the worker, not by the application"),
    ]),
    Group("Postgres connection pool", note="Per process. The API container and the worker each open their own pool against the local database.", vars=[
        EnvVar("DB_POOL_SIZE", "5"),
        EnvVar("DB_MAX_OVERFLOW", "5"),
        EnvVar("DB_POOL_TIMEOUT", "30"),
        EnvVar("DB_POOL_RECYCLE", "1800"),
    ]),
    Group("Logging", vars=[
        EnvVar("LOG_FORMAT", "", help="json emits one JSON object per line; blank is human-readable"),
        EnvVar("LOG_LEVEL", "INFO"),
    ]),
    Group("Distributed tracing (OpenTelemetry): off by default", note="OTEL_TRACES_ENABLED=true plus an endpoint ties an API request, worker job, and each LLM call into one trace. OTEL_TRACES_EXPORTER=console prints spans to stdout for a quick smoke.", vars=[
        EnvVar("OTEL_TRACES_ENABLED", "false", kind="bool", help="set true to export traces"),
        EnvVar("OTEL_TRACES_EXPORTER", "otlp"),
        EnvVar("OTEL_EXPORTER_OTLP_ENDPOINT", "", kind="url", consumer="sdk"),
        EnvVar("OTEL_SERVICE_NAME", "mesh-api"),
    ]),

    Group("Filesystem layout", note="Relative to the process working directory, never to the installed package: an installed distribution must not write into site-packages. The images WORKDIR /srv, so these resolve to /srv/... there and to your checkout on a host.", vars=[
        EnvVar("DATA_ROOT", "./data", kind="path", help="root of the writable data layout"),
        EnvVar("JOBS_DIR", derived="<DATA_ROOT>/jobs", kind="path", help="per-job upload staging"),
        EnvVar("CORPUS_DIR", derived="<DATA_ROOT>/corpus", kind="path", help="written only while collection is on; not swept: remove samples you no longer want"),
        EnvVar("WORKSPACE_BASE", "./workspaces", kind="path", help="engine workspaces for running jobs"),
        EnvVar("STATIC_DIR", "./ui", kind="path", help="the browser client served at /ui; ./ui is your checkout on a host and /srv/ui in the image, because both resolve it from the working directory"),
    ]),

    Group("Request limits", vars=[
        EnvVar("RATE_LIMIT_PER_MINUTE", "240", kind="int", help="per identity (X-User-Id, else client IP); 0 disables. Job quotas are separate"),
        EnvVar("WS_MAX_SESSION_SECONDS", "3000", kind="int", help="close a live socket on our terms before the platform's 60-minute cap; the browser reconnects with its cursor"),
        EnvVar("EVENT_LOG_TTL_SECONDS", "86400", kind="int", help="how long a job's replayable event log outlives its sockets"),
    ]),

    Group("Retention and job lifetime", vars=[
        EnvVar("FAILED_JOB_RETENTION_HOURS", "24", kind="int"),
        EnvVar("UPLOAD_RETENTION_DAYS", "30", kind="int"),
        EnvVar("STALLED_JOB_TIMEOUT_HOURS", "4", kind="int", help="a job with no progress for this long is reaped and failed truthfully"),
        EnvVar("PIPELINE_TOTAL_TIMEOUT_SECONDS", "21600", kind="int", help="one absolute wall-clock ceiling for an ENTIRE job across every attempt, retry and restart"),
    ]),

    Group("Worker lease and fencing", note="A claim is valid for the lease without a heartbeat; the owner heartbeats well inside it, and a takeover is only allowed once the lease has EXPIRED.", vars=[
        EnvVar("WORKER_LEASE_SECONDS", "900", kind="int"),
        EnvVar("WORKER_HEARTBEAT_SECONDS", "60", kind="int", help="must be well under WORKER_LEASE_SECONDS (validated at import)"),
        EnvVar("REQUIRE_DURABLE_CHECKPOINTER", "", kind="bool", help="forced on outside development; leave blank"),
        EnvVar("PIPELINE_BACKEND", "celery", help="which execution backend the run is attributed to"),
    ]),

    Group("Safety switches", note="On by default. Each one is a gate that refuses bad geometry or unsafe generated work; turn one off only with a measured reason.", vars=[
        EnvVar("SOLVABILITY_GATE_ENABLED", "true", kind="bool"),
        EnvVar("DOMAIN_EXTENT_GATE_ENABLED", "true", kind="bool"),
        EnvVar("MESH_SCRIPT_SCAN_ENABLED", "true", kind="bool"),
        EnvVar("RUN_PYTHON_REQUIRE_SANDBOX", "true", kind="bool", help="refuse to run generated Python outside the seccomp/Landlock jail"),
        EnvVar("CELL_HARD_LIMIT", "8000000", kind="int", help="compute-feasibility cap on mesh size: the only cell-count gate"),
        EnvVar("LAYER_CAVEAT_FLOOR_PCT_EXTERNAL", "40", kind="int",
               help="areal prism-layer coverage floor for caveated delivery of external-aero "
                    "snappy meshes; below it a layers-only review FAIL stays a failure"),
        EnvVar("LAYER_CAVEAT_PATCH_MIN_THICKNESS_PCT", "10", kind="int",
               help="per-wall-patch thickness floor for caveated delivery: one effectively "
                    "bare wall patch keeps the failure a failure"),
    ]),

    Group("Workspace archive limits", note="Bounds on the workspace archive a remote mesh returns: a decompression bomb, a traversal entry or a runaway member is refused before anything is written.", vars=[
        EnvVar("WORKSPACE_ARCHIVE_MAX_BYTES", "536870912", kind="int", help="compressed archive ceiling"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_ENTRIES", "20000", kind="int"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_TOTAL_BYTES", "4294967296", kind="int", help="cumulative expanded size"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_FILE_BYTES", "1073741824", kind="int", help="any single regular file"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_DEPTH", "40", kind="int"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_PATH_LEN", "1024", kind="int"),
        EnvVar("WORKSPACE_ARCHIVE_MAX_RATIO", "200.0", kind="float", help="uncompressed/compressed ceiling"),
    ]),

    Group("Viewer and dispute", vars=[
        EnvVar("VIEWER_GRID_PX", "2.5", kind="float"),
        EnvVar("VIEWER_FINE_FILL", "8", kind="float"),
        EnvVar("VIEWER_FRAME_FRAC", "0.85", kind="float"),
        EnvVar("VIEWER_FLAG_SPAN_FACTOR", "2.5", kind="float"),
        EnvVar("DISPUTE_MAX_FLAGS", "20", kind="int"),
    ]),

    Group("Provider timeouts and retries", vars=[
        EnvVar("DEEPINFRA_CALL_TIMEOUT", "1800", kind="int"),
        EnvVar("DEEPINFRA_CONNECT_TIMEOUT", "15", kind="int"),
        EnvVar("DEEPINFRA_READ_TIMEOUT", "60", kind="int"),
        EnvVar("DEEPINFRA_WRITE_TIMEOUT", "30", kind="int"),
        EnvVar("MAX_BUILDER_RETRIES", "3", kind="int", help="rebuild attempts after a reviewer rejection"),
        EnvVar("MAX_SNAPPY_ATTEMPTS", "3", kind="int"),
    ]),

    Group("Web search sizing", vars=[
        EnvVar("WEB_SEARCH_MAX_RESULTS", "5", kind="int"),
        EnvVar("WEB_SEARCH_TIMEOUT", "20", kind="int"),
        EnvVar("TAVILY_API_KEY", "", secret=True, help="required only when WEB_SEARCH_PROVIDER=tavily"),
    ]),

    Group("Mesh toolchain", vars=[
        EnvVar("VMTK_BIN", "vmtk", help="vmtk runs as an isolated subprocess: its conda VTK cannot coexist with the app's"),
    ]),

    Group("Error reporting", vars=[
        EnvVar("SENTRY_DSN", "", kind="url", secret=True, help="leave blank to disable"),
    ]),

    # INTERNAL: real, supported, deliberately not in front of an ordinary operator.
    Group("Advanced: model sampling", note="INTERNAL. The shipped values are the ones the pipeline is tuned around; changing them changes mesh quality, not just cost.", vars=[
        EnvVar("BUILDER_TEMPERATURE", "0.3", kind="float", exposure="internal"),
        EnvVar("BUILDER_TOP_P", "0.95", kind="float", exposure="internal"),
        EnvVar("BUILDER_MIN_P", "0.05", kind="float", exposure="internal"),
        EnvVar("INTAKE_TEMPERATURE", "1.0", kind="float", exposure="internal"),
        EnvVar("INTAKE_MIN_P", "0.05", kind="float", exposure="internal"),
        EnvVar("INTAKE_MAX_TOKENS", "2048", kind="int", exposure="internal"),
        EnvVar("PLANNER_TEMPERATURE", "0.3", kind="float", exposure="internal"),
        EnvVar("PLANNER_TOP_P", "0.95", kind="float", exposure="internal"),
        EnvVar("PLANNER_MIN_P", "0.05", kind="float", exposure="internal"),
        EnvVar("PLANNER_MAX_TOKENS", "16384", kind="int", exposure="internal"),
        EnvVar("REVIEWER_TOP_K", "20", kind="int", exposure="internal"),
        EnvVar("SEARCH_SUMMARIZER_TEMPERATURE", "0.2", kind="float", exposure="internal"),
        EnvVar("SEARCH_SUMMARIZER_MAX_TOKENS", "700", kind="int", exposure="internal"),
    ]),

    Group("Advanced: per-role budgets", note="INTERNAL. Child budgets are each capped at the pipeline's remaining time; raising one cannot exceed PIPELINE_TOTAL_TIMEOUT_SECONDS.", vars=[
        EnvVar("BUILDER_TOTAL_TIMEOUT_SECONDS", "10800", kind="int", exposure="internal"),
        EnvVar("BUILDER_AUTO_SUBMIT_AFTER", "2", kind="int", exposure="internal"),
        EnvVar("BUILDER_NULL_CHOICES_SLEEP", "15", kind="int", exposure="internal"),
        EnvVar("REVIEWER_TOTAL_TIMEOUT_SECONDS", "1800", kind="int", exposure="internal"),
        EnvVar("PLANNER_TOTAL_TIMEOUT_SECONDS", "1800", kind="int", exposure="internal"),
        EnvVar("MAX_TOOL_OUTPUT_CHARS", "16000", kind="int", exposure="internal", help="context-blowup guard on any single builder tool result"),
        EnvVar("MODEL_DEFAULT_CONCURRENCY_BUDGET", "8", kind="int", exposure="internal"),
        EnvVar("INTAKE_GREETING_ON_UPLOAD", "true", kind="bool", exposure="internal"),
    ]),

    # EXTERNAL: nobody edits these in .env; a platform or a library supplies them.
    Group("Platform-supplied", note="EXTERNAL. Declared so the catalogue accounts for every name the code reads, but set by Cloud Run, Compose, the dispatcher or a library: not by editing .env.", vars=[
        EnvVar("CLOUD_RUN_EXECUTION", "", exposure="external", consumer="app", help="injected by Cloud Run; the execution this process belongs to"),
        EnvVar("PIPELINE_EXECUTION_ID", derived="<CLOUD_RUN_EXECUTION>", exposure="external", consumer="app", help="the backend execution identity, falling back to the Cloud Run injection"),
        EnvVar("GOOGLE_APPLICATION_CREDENTIALS", "", kind="path", exposure="external", consumer="app", help="Compose mounts GOOGLE_ADC_FILE here and points google-auth at it"),
        EnvVar("PROMETHEUS_MULTIPROC_DIR", "", kind="path", exposure="external", consumer="app", help="prometheus_client's own variable; set by the worker entrypoint"),
        EnvVar("ALEMBIC_CONFIG", "", kind="path", exposure="external", consumer="app", help="alembic's own variable; the migration wrapper honours it when set"),
    ]),
    Group("Inference overrides", note="Two structured settings, each one declared name. They replace the MODEL_PRICE_<PROVIDER>_<MODEL> and MODEL_BUDGET_<DOMAIN> namespaces, where the variable NAME was built from provider and model data: so the supported surface was unlistable and a typo was indistinguishable from an unsupported setting.", vars=[
        EnvVar("MODEL_PRICE_OVERRIDES", "", help=(
            "per-1M-token prices for models whose published price the shipped table does not carry. "
            "Schema: 'provider:model=in,out,cached' separated by ';'. The provider must be one this "
            "product supports; prices must be finite and non-negative. Example: "
            "'deepinfra:Qwen/Qwen3-VL-235B-A22B-Thinking=0.20,0.88,0.11'")),
        EnvVar("MODEL_DOMAIN_BUDGETS", "", help=(
            "concurrency budget per quota domain, overriding the routes' own. Schema: "
            "'provider:account:model=N' separated by ';'. The provider must be one this product "
            "supports and N a positive whole number.")),
    ]),
    *_route_groups(),
]

_HEADER = """\
# Responsibility: List every supported setting with its default, as the template .env is created from.
# Boundaries: generated from the settings inventory: a name absent from it is not a setting.

# Hexera Platform example configuration
#
# THIS FILE IS THE TEMPLATE, NOT YOUR CONFIGURATION. `make setup` copies it to .env when you have
# no .env yet, and leaves an existing one untouched. Edit .env; never edit this file by hand.
#
# Then set the two keys marked REQUIRED. .env is gitignored and is the only file the application
# reads.
#
# A shell variable beats .env, and .env beats the default shown here.
#
# WHAT THIS FILE CONTAINS: every setting a developer or operator is expected to turn. It is NOT
# the whole catalogue. src/meshpipeline/settings/inventory.py also declares advanced controls
# deliberately kept out of this template, and variables supplied by the platform or a library
# rather than by editing .env: each one carrying the reason it is not here. That module is the
# complete list; this file is the part meant for you. Refresh after a settings change:
#
#     .venv/bin/python -m meshpipeline.settings.inventory > .env.example
"""


def all_vars() -> list[EnvVar]:
    return [v for g in INVENTORY for v in g.vars]


def template_vars() -> list[EnvVar]:
    # Exactly what .env.example carries. Membership is decided by the entry's own exposure, so a
    # setting cannot leave the template without that being written down beside it.
    return [v for v in all_vars() if v.exposure == "template"]


def resolve_default(name: str, lookup) -> str:
    # THE one place a derived default becomes a value. `lookup` answers with the RESOLVED value of
    # another declared setting, so <DATA_ROOT>/jobs follows a DATA_ROOT the operator overrode
    # instead of silently re-deriving from the shipped default.
    var = get(name)
    if not var.derived:
        return var.default
    out = var.derived
    for other in all_vars():
        token = f"<{other.name}>"
        if token in out:
            out = out.replace(token, lookup(other.name))
    if "<" in out and ">" in out:
        raise KeyError(f"{name}: derived default {var.derived!r} names a setting that is not declared")
    return out


def get(name: str) -> EnvVar:
    for v in all_vars():
        if v.name == name:
            return v
    raise KeyError(name)


def default_of(name: str) -> str:
    return get(name).default


def required_names() -> list[str]:
    return [v.name for v in all_vars() if v.required]


def render_env() -> str:
    import textwrap

    out: list[str] = [_HEADER]
    for g in INVENTORY:
        shown = [v for v in g.vars if v.exposure == "template"]
        if not shown:
            continue                      # a wholly internal/external group is not an empty heading
        out.append("")
        out.append(f"# {g.title}")
        if g.note:
            out.extend(textwrap.wrap(g.note, 94, initial_indent="# ", subsequent_indent="# "))
        for v in shown:
            note = (f"REQUIRED - {v.help}" if v.required and v.help else
                    "REQUIRED" if v.required else v.help)
            if v.derived:
                # The RELATIONSHIP is the documentation; leaving the value blank keeps a
                # machine-specific absolute path out of the template while still naming the key.
                note = (note + " - " if note else "") + f"defaults to {v.derived}"
            if note:
                out.extend(textwrap.wrap(note, 94, initial_indent="# ", subsequent_indent="#   "))
            out.append(f"{v.name}={v.default}")
    return "\n".join(out) + "\n"


#: Markers around the generated block in docs/reference/configuration.md. The prose outside them is
#: hand-written and stays that way; the roster between them is derived, so a setting cannot be
#: added to the product and quietly stay undocumented.
REFERENCE_BEGIN = "<!-- BEGIN GENERATED SETTINGS ROSTER -->"
REFERENCE_END = "<!-- END GENERATED SETTINGS ROSTER -->"
DEVENV_BEGIN = "<!-- BEGIN GENERATED DEVELOPMENT ENVIRONMENTS -->"
DEVENV_END = "<!-- END GENERATED DEVELOPMENT ENVIRONMENTS -->"
REMOVED_BEGIN = "<!-- BEGIN GENERATED REMOVED SETTINGS -->"
REMOVED_END = "<!-- END GENERATED REMOVED SETTINGS -->"


def render_reference() -> str:
    # Names, where they are exposed, and who reads them. Deliberately NOT defaults or prose: those
    # belong to the hand-written sections, and duplicating them here would recreate the drift this
    # roster exists to end.
    rows = ["| Setting | Exposure | Read by | Secret |", "|---|---|---|---|"]
    for g in INVENTORY:
        for v in sorted(g.vars, key=lambda x: x.name):
            rows.append(f"| `{v.name}` | {v.exposure} | {v.consumer} | {'yes' if v.secret else ''} |")
    body = "\n".join([
        REFERENCE_BEGIN,
        "",
        "<!-- Regenerate: python -m meshpipeline.settings.inventory --reference -->",
        "",
        f"Every supported setting ({len(all_vars())} entries). `template` settings are the ones "
        "`.env.example` carries; `internal` are advanced controls deliberately kept out of it; "
        "`external` are supplied by the platform or a library rather than by editing `.env`.",
        "",
        *rows,
        "",
        REFERENCE_END,
    ])
    return body


def replace_block(text: str, begin: str, end: str, block: str) -> str:
    # Replaces EXACTLY the marked region. A missing or duplicated marker raises rather than
    # guessing, so a generator can never rewrite prose it does not own.
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError(f"expected exactly one {begin} and one {end}")
    i, j = text.index(begin), text.index(end) + len(end)
    if j <= i:
        raise ValueError(f"{end} appears before {begin}")
    return text[:i] + block + text[j:]


def render_dev_environments() -> str:
    # Rendered from the policy authority itself, so the document cannot list a development name
    # the classifier does not exempt (or miss one it does).
    from meshpipeline.settings.policy import _AUTH_OPTIONAL_ENVS
    names = " ".join(f"`{n}`" for n in sorted(_AUTH_OPTIONAL_ENVS))
    return "\n".join([DEVENV_BEGIN, "", names, "", DEVENV_END])


def render_removed_reference() -> str:
    # The retired surface, rendered from the same authority startup refuses with. No count is
    # written into the prose: a number in a sentence is the thing that goes stale.
    rows = ["| Retired | Refused at startup | Replacement | Why |", "|---|---|---|---|"]
    for name in sorted(REMOVED):
        rows.append(f"| `{name}` | yes | see guidance | {REMOVED[name]} |")
    for fam in sorted(REMOVED_FAMILIES, key=lambda f: f.label):
        rows.append(f"| `{fam.label}` (any such name) | yes | `{fam.replacement}` | "
                    f"the variable NAME carried provider and model data; {fam.guidance} |")
    return "\n".join([
        REMOVED_BEGIN,
        "",
        "<!-- Regenerate: python -m meshpipeline.settings.inventory --removed -->",
        "",
        "Setting one of these makes the process refuse to start, naming the variable and its "
        "replacement. The configured value is never echoed, because it may be a credential.",
        "",
        *rows,
        "",
        REMOVED_END,
    ])


def _main(argv: list[str]) -> int:
    args = argv[1:]
    if "--required" in args:
        print("\n".join(required_names()))
        return 0
    if "--reference" in args:
        print(render_reference())
        return 0
    if "--removed" in args:
        print(render_removed_reference())
        return 0
    if "--dev-environments" in args:
        print(render_dev_environments())
        return 0
    sys.stdout.write(render_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
