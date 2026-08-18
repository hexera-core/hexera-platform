# Responsibility: Refuse to start a process whose configuration cannot support it.
# Boundaries: validation at boot, so a misconfiguration fails immediately rather than at the first job.
from __future__ import annotations

from pathlib import Path

import meshpipeline.settings.env as env
import meshpipeline.settings.policy as _policy
import meshpipeline.settings.policy as polmod
import meshpipeline.settings.runtime as rtmod
from meshpipeline.adapters.model_inference.routes import missing_provider_credentials
from meshpipeline.agents.builder.settings import (
    BUILDER_LOOP_TIMEOUT,
    BUILDER_MAX_ROUNDS,
    BUILDER_MAX_TOTAL_ATTEMPTS,
    BUILDER_MODEL,
    BUILDER_RETRY_MAX_ROUNDS,
    MAX_BUILDER_RETRIES,
)
from meshpipeline.agents.reviewer.settings import REVIEWER_MAX_ROUNDS, REVIEWER_MODEL
from meshpipeline.settings.env import ConfigurationError
from meshpipeline.settings.policy import (
    CORS_ORIGINS,
    ENV,
    MESH_API_KEY,
    REQUIRED_PROMPTS,
    SOLVABILITY_GATE_ENABLED,
)
from meshpipeline.settings.providers import (
    DATABASE_URL,
    DEEPINFRA_CALL_TIMEOUT,
    DEEPINFRA_READ_TIMEOUT,
    DEEPSEEK_MODEL,
    POSTGRES_PASSWORD,
    REDIS_URL,
    SEARCH_SUMMARIZER_MODEL,
    WEB_SEARCH_BASE_URL,
    WEB_SEARCH_ENABLED,
    WEB_SEARCH_PROVIDER,
)
from meshpipeline.settings.runtime import (
    JOBS_DIR,
    OPENFOAM_BASHRC,
    OPENFOAM_COMMAND_TIMEOUT,
    WORKSPACE_BASE,
)


def validate() -> None:
    import os as _os
    import warnings as _warnings

    # A DELETED setting present in the environment is a configuration error, never a no-op. The
    # process refuses to start and names its replacement, so a stale .env cannot quietly keep a
    # value that no longer means what it used to.
    from meshpipeline.settings import inventory as _inventory
    # Exact retired names AND the anchored legacy families, both from the one authority. The scan
    # yields names and guidance only - never a configured value, which may be a credential.
    _stale = _inventory.retired_present(_os.environ)
    if _stale:
        raise ConfigurationError("; ".join(
            f"{name} was removed: {reason}" for name, reason in _stale))

    try:
        WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigurationError(f"Cannot create required directory: {e}") from e

    # HARDENED ENVIRONMENTS. The question is "does this deployment require hardening", asked of the
    # one authority in settings.policy - never `ENV == "production"`, which exempted staging, prod
    # and hosted from every check below while reading as though it covered them.
    #
    # MESH_API_KEY and USER_TOKEN_SECRET are deliberately NOT checked here: settings.policy refuses
    # the import for any hardened environment missing either, so this function cannot run without
    # them. Restating the check here made it look like startup's to enforce and left two branches
    # that could never execute.
    if polmod.requires_hardened_runtime(ENV):
        if CORS_ORIGINS == ["*"]:
            raise ConfigurationError(
                f"ENV={ENV!r} is a hosted environment and requires a non-wildcard CORS_ORIGINS. "
                "Set CORS_ORIGINS to an explicit comma-separated origin list "
                "(e.g. https://app.example.com) - refusing to start.")
        if not POSTGRES_PASSWORD and not DATABASE_URL:
            raise ConfigurationError(
                f"ENV={ENV!r} is a hosted environment and requires a database: set DATABASE_URL "
                "(a managed-Postgres connection string, e.g. Neon - carries its own credentials) "
                "OR POSTGRES_PASSWORD for the POSTGRES_* parts.")
        _missing = missing_provider_credentials()
        if _missing:
            raise ConfigurationError(
                f"ENV={ENV!r} is a hosted environment and requires credentials for every ENABLED "
                "provider (a provider a configured model route or the web search actually uses). "
                "Missing: " + "; ".join(_missing) + ". Set them, or reconfigure the routes so no "
                "role targets a provider you cannot credential - refusing to start.")

    if BUILDER_LOOP_TIMEOUT < 2 * OPENFOAM_COMMAND_TIMEOUT:
        raise ConfigurationError(
            f"BUILDER_LOOP_TIMEOUT ({BUILDER_LOOP_TIMEOUT}s) must be >= "
            f"2 * OPENFOAM_COMMAND_TIMEOUT (= {2 * OPENFOAM_COMMAND_TIMEOUT}s). "
            "Otherwise the asyncio.wait_for ceiling can race the subprocess "
            "kill cycle and leave orphan processes.")

    if not polmod.requires_hardened_runtime(ENV):
        if not POSTGRES_PASSWORD:
            _warnings.warn(
                "POSTGRES_PASSWORD is empty - set a strong password before deploying to production.",
                RuntimeWarning, stacklevel=2)
        if CORS_ORIGINS == ["*"]:
            _warnings.warn(
                "CORS_ORIGINS is set to '*' (wildcard). Any browser origin can make cross-origin "
                "requests to this API. Set ENV=production + restricted CORS_ORIGINS before deploying.",
                RuntimeWarning, stacklevel=2)
        if not MESH_API_KEY:
            _warnings.warn(
                "MESH_API_KEY is empty - authentication is DISABLED on every route. "
                "Set ENV=production to enforce auth at startup.",
                RuntimeWarning, stacklevel=2)
        _missing = missing_provider_credentials()
        if _missing:
            _warnings.warn(
                "Enabled provider(s) missing credentials: " + "; ".join(_missing) + ". "
                "Model/search calls to them will fail until the keys are set (or the routes point "
                "elsewhere). Local/test profiles that make no such calls can ignore this.",
                RuntimeWarning, stacklevel=2)
    if "redis://" in REDIS_URL and "@" not in REDIS_URL:
        _warnings.warn(
            "REDIS_URL has no password (no '@' found) - set requirepass in Redis config "
            "and include credentials in REDIS_URL before deploying to production.",
            RuntimeWarning, stacklevel=2)

    if not Path(OPENFOAM_BASHRC).exists():
        import warnings
        warnings.warn(
            f"OPENFOAM_BASHRC not found at '{OPENFOAM_BASHRC}'. OpenFOAM commands will fail "
            "unless running inside the OpenFOAM container.",
            RuntimeWarning, stacklevel=2)

    # A deployment that collects nothing provisions no corpus storage.
    if _policy.MODES.data_collection_enabled:
        _training_path = Path(rtmod.CORPUS_DIR)
        try:
            _training_path.mkdir(parents=True, exist_ok=True)
            _probe = _training_path / ".write_probe"
            _probe.write_text("ok")
            _probe.unlink()
            import logging as _log
            _log.getLogger(__name__).info("corpus dir: %s - writable", _training_path.resolve())
        except OSError as _exc:
            import warnings as _w
            _w.warn(
                f"CORPUS_DIR '{rtmod.CORPUS_DIR}' is not writable: {_exc}. "
                "Collected runs will not be exported.",
                RuntimeWarning, stacklevel=2)

    import logging as _plog
    _plogger = _plog.getLogger(__name__)
    for _attr, _fname in REQUIRED_PROMPTS:
        _content = getattr(polmod.prompts, _attr, "")
        _plogger.info("Prompt loaded: %-35s %d chars  (%s)", _fname, len(_content), env.PROMPTS_DIR / _fname)

    _FORMAT_SPECS: list[tuple[str, dict]] = [
        ("reviewer_system", {
            "workflow": "", "request": "", "review_brief": "",
            "quality_checks": "", "review_rubric": ""}),
    ]
    for _attr, _kwargs in _FORMAT_SPECS:
        _tmpl = getattr(polmod.prompts, _attr, "")
        try:
            _tmpl.format(**_kwargs)
        except (KeyError, IndexError) as _fe:
            raise ConfigurationError(
                f"Prompt '{_attr}' has an unmatched format placeholder: {_fe}. Edit the prompt "
                "file and make sure every {{ }} is a known key or is escaped as {{{{ }}}}.") from _fe

    import logging as _rlog
    _rlog.getLogger(__name__).info(
        "Config resolved: ENV=%s auth=%s cors=%s "
        "BUILDER_MAX_ROUNDS=%d BUILDER_RETRY_MAX_ROUNDS=%d "
        "BUILDER_LOOP_TIMEOUT=%ds OPENFOAM_COMMAND_TIMEOUT=%ds "
        "MAX_BUILDER_RETRIES=%d (%d base + 1 reviewer-feedback bonus = %d max) "
        "DEEPINFRA_CALL_TIMEOUT=%ds DEEPINFRA_READ_TIMEOUT=%ds "
        "SOLVABILITY_GATE_ENABLED=%s DATA_COLLECTION_ENABLED=%s",
        ENV, "enforced" if MESH_API_KEY else "DISABLED",
        "wildcard" if CORS_ORIGINS == ["*"] else ",".join(CORS_ORIGINS),
        BUILDER_MAX_ROUNDS, BUILDER_RETRY_MAX_ROUNDS, BUILDER_LOOP_TIMEOUT, OPENFOAM_COMMAND_TIMEOUT,
        MAX_BUILDER_RETRIES, MAX_BUILDER_RETRIES + 1, BUILDER_MAX_TOTAL_ATTEMPTS,
        DEEPINFRA_CALL_TIMEOUT, int(DEEPINFRA_READ_TIMEOUT),
        SOLVABILITY_GATE_ENABLED, _policy.MODES.data_collection_enabled)


def validate_and_summarize() -> str:
    validate()
    return summary()


def summary() -> str:
    return "\n".join([
        "=== Mesh application configuration ===",
        f"  DeepSeek model    : {DEEPSEEK_MODEL}",
        f"  Builder model     : {BUILDER_MODEL}",
        f"  Reviewer model    : {REVIEWER_MODEL}",
        f"  Search summarizer : {SEARCH_SUMMARIZER_MODEL}",
        f"  Web search        : {WEB_SEARCH_PROVIDER} @ {WEB_SEARCH_BASE_URL} (enabled={WEB_SEARCH_ENABLED})",
        "  Checkpointer      : postgres (auto-fallback: memory)",
        f"  Workspace base    : {WORKSPACE_BASE}",
        f"  Jobs dir          : {JOBS_DIR}",
        f"  Corpus dir : {rtmod.CORPUS_DIR}",
        f"  Max retries       : {MAX_BUILDER_RETRIES}",
        f"  Reviewer max rounds: {REVIEWER_MAX_ROUNDS}",
        f"  OpenFOAM bashrc   : {OPENFOAM_BASHRC}",
        "============================",
    ])
