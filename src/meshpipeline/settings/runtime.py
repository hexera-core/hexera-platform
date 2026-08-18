# Responsibility: Declare the process-local limits, paths and budgets.
# Boundaries: per-process values, distinct from provider configuration and from policy.
from __future__ import annotations

from pathlib import Path

from meshpipeline.settings import inventory
from meshpipeline.settings.env import optional_env


def _declared(name: str) -> str:
    # THE default, fetched from the catalogue rather than restated here. A path entry may declare
    # its default as a RELATIONSHIP (<DATA_ROOT>/jobs); resolve_default expands it against the
    # value this process actually resolved, so overriding DATA_ROOT moves its children with it.
    return inventory.resolve_default(name, lambda other: str(_RESOLVED[other]))


#: Values already resolved in this module, for derived defaults to build on. Populated in order.
_RESOLVED: dict[str, object] = {}


def _resolve_path(name: str, raw: str) -> Path:
    # Relative in the catalogue, absolute in the process. `./data` is deterministic and
    # machine-independent - which is what belongs in a template - while the working directory is
    # what makes it correct in both places it runs: a checkout on a host, and WORKDIR /srv in the
    # image. Anchoring to the PACKAGE instead would point an installed distribution at
    # site-packages, which is how the API once died trying to mkdir under dist-packages.
    #
    # The NAME is passed to optional_env as a literal at each call site below, never through this
    # helper: a reader that takes its variable name as an argument is indistinguishable from a
    # constructed name, and the scanner is right to refuse to tell them apart.
    value = Path(raw).expanduser().resolve()
    _RESOLVED[name] = value
    return value

# filesystem paths
# WRITABLE runtime data is anchored to the process's working directory, NOT to the package.
# PROJECT_ROOT is where the package IS - correct for read-only package data (prompts ship in
# the wheel and live beside the code), and wrong for anything written: once the distribution
# is installed, a package-anchored default points into site-packages, and the API died at
# import trying to mkdir /usr/local/lib/python3.11/dist-packages/data/jobs. Running from a
# source tree hid that for as long as the package sat in a writable checkout - it is also
# what kept re-creating src/meshpipeline/workspaces/.
# The images WORKDIR /srv, so these resolve to /srv/{data,workspaces} - the paths the image
# creates and chowns, and the ones compose sets explicitly anyway. `./data` is also what the
# retired-variable message below has always told operators the default is.
# The retired data-layout names (OUTPUTS_BASE, UPLOADS_BASE, UPLOAD_STAGING_ROOT,
# RUNTIME_DATA_ROOT, RUNTIME_SAMPLES_DIR) used to be refused by a tuple kept right here. That made
# "which names are retired?" a question with two answers, only one of which was documented. They
# now live in inventory.REMOVED with every other removed name, and runtime/startup.py refuses the
# whole set from that one authority before the process does anything.
WORKSPACE_BASE = _resolve_path(
    "WORKSPACE_BASE", optional_env("WORKSPACE_BASE", _declared("WORKSPACE_BASE")))
DATA_ROOT = _resolve_path("DATA_ROOT", optional_env("DATA_ROOT", _declared("DATA_ROOT")))
JOBS_DIR = _resolve_path("JOBS_DIR", optional_env("JOBS_DIR", _declared("JOBS_DIR")))
CORPUS_DIR: str = str(_resolve_path(
    "CORPUS_DIR", optional_env("CORPUS_DIR", _declared("CORPUS_DIR"))))
# ./ui, not /srv/ui. The container's WORKDIR is /srv and the image copies the client to /srv/ui,
# so the relative form resolves to exactly the same directory there - while on a host checkout it
# finds the repository's ui/ instead of a path that does not exist. The old absolute default was
# the container layout stated as a universal one, and it silently disabled the entire browser UI
# for anyone running the API outside a container.
STATIC_DIR: str = str(_resolve_path("STATIC_DIR", optional_env("STATIC_DIR", _declared("STATIC_DIR"))))

# mesh toolchain locations / limits
OPENFOAM_BASHRC: str = optional_env("OPENFOAM_BASHRC", "/usr/lib/openfoam/openfoam2412/etc/bashrc")
# vmtk is invoked as an isolated subprocess (its conda vtk cannot coexist with the app's vtk).
VMTK_BIN: str = optional_env("VMTK_BIN", "vmtk")
OPENFOAM_COMMAND_TIMEOUT: int = int(optional_env("OPENFOAM_COMMAND_TIMEOUT", "1500"))

# the TOP-LEVEL logical pipeline budget - one finite wall-clock ceiling for an ENTIRE job across
# every attempt, rebuild, provider retry, worker generation and restart. It is an ABSOLUTE deadline
# derived from the job's durable `created_at` (created_at + this), so it is set once, survives restart/
# redelivery/lease-takeover, and NEVER resets on a retry/rebuild/new-generation. Child budgets
# (Builder Reviewer, native, Planner) are each capped at the pipeline's REMAINING time; exhaustion
# fails the job truthfully (timed_out) and starts no new expensive work. Default 6h - generous for the
# largest real mesh, far under the Celery/Cloud Run backend timeout that coarsely bounds it.
PIPELINE_TOTAL_TIMEOUT_SECONDS: int = int(optional_env("PIPELINE_TOTAL_TIMEOUT_SECONDS", "21600"))
if PIPELINE_TOTAL_TIMEOUT_SECONDS <= 0:
    from meshpipeline.settings.env import ConfigurationError as _CfgErr
    raise _CfgErr(
        f"PIPELINE_TOTAL_TIMEOUT_SECONDS must be a positive number of seconds, got "
        f"{PIPELINE_TOTAL_TIMEOUT_SECONDS}")

# worker lease/fencing (durable exclusive execution ownership). WORKER_LEASE_SECONDS is how long
# a claim is valid without a heartbeat; the owner heartbeats every WORKER_HEARTBEAT_SECONDS (well
# under the lease). A takeover is only allowed once the lease has EXPIRED. Defaults
# are conservative: a long lease (a mesh stage can run many minutes) so a healthy-but-slow worker is
# never fenced, with frequent heartbeats so a genuinely-dead worker is taken over reasonably soon.
WORKER_LEASE_SECONDS: int = int(optional_env("WORKER_LEASE_SECONDS", "900"))          # 15 min
WORKER_HEARTBEAT_SECONDS: int = int(optional_env("WORKER_HEARTBEAT_SECONDS", "60"))   # 1 min
for _n, _v in (("WORKER_LEASE_SECONDS", WORKER_LEASE_SECONDS),
               ("WORKER_HEARTBEAT_SECONDS", WORKER_HEARTBEAT_SECONDS)):
    if _v <= 0:
        from meshpipeline.settings.env import ConfigurationError as _CE
        raise _CE(f"{_n} must be a positive number of seconds, got {_v}")
if WORKER_HEARTBEAT_SECONDS >= WORKER_LEASE_SECONDS:
    from meshpipeline.settings.env import ConfigurationError as _CE
    raise _CE("WORKER_HEARTBEAT_SECONDS must be well under WORKER_LEASE_SECONDS "
              f"(got heartbeat={WORKER_HEARTBEAT_SECONDS}, lease={WORKER_LEASE_SECONDS})")

# transport / process limits
# Crashed jobs are redelivered (acks_late) and re-run (idempotent); one that DETERMINISTICALLY
# crashes the worker is dead-lettered after this many deliveries (the retry-storm guard).
CELERY_MAX_REDELIVERIES: int = int(optional_env("CELERY_MAX_REDELIVERIES", "3"))
# Request-level rate limit (per identity, per minute) - 0 disables. Job quotas are separate.
RATE_LIMIT_PER_MINUTE: int = int(optional_env("RATE_LIMIT_PER_MINUTE", "240"))
# The per-job event log outlives any single socket; a reconnecting browser replays from its
# last-seen seq. Must exceed the longest run plus tab-shut time.
EVENT_LOG_TTL_SECONDS: int = int(optional_env("EVENT_LOG_TTL_SECONDS", "86400"))
# Cloud Run severs any request (WebSockets too) at 60 min; we close first, on our terms, so
# the client reconnects with its cursor. Keep below the platform cap with room to spare.
WS_MAX_SESSION_SECONDS: int = int(optional_env("WS_MAX_SESSION_SECONDS", "3000"))
# Hard cap on any single builder tool result fed back into the LLM context (context-blowup guard).
MAX_TOOL_OUTPUT_CHARS: int = int(optional_env("MAX_TOOL_OUTPUT_CHARS", "16000"))

# resilience (circuit breaker) - no fallback models; an open circuit FAILS the job
CIRCUIT_FAILURE_THRESHOLD: int   = int(optional_env("CIRCUIT_FAILURE_THRESHOLD", "5"))
CIRCUIT_RECOVERY_SECONDS: float  = float(optional_env("CIRCUIT_RECOVERY_SECONDS", "30"))
CIRCUIT_HALF_OPEN_MAX: int       = int(optional_env("CIRCUIT_HALF_OPEN_MAX", "1"))
